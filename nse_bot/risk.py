"""Pre-trade risk checks and the persistent daily lockout, shared by every strategy process.

One RiskBook per day lives in <risk_dir>/risk/<YYYY-MM-DD>.json. ORB and Liquidity Sweep run as
separate processes with separate ledgers (state_dir), but both configs name the SAME risk_dir, so
the daily limits apply to both strategies combined:

  * filled entries per day            max_trades_per_day
  * stop-loss exits per day           max_stop_exits_per_day
  * daily net loss (realised + open)  max_daily_loss_inr
  * realised net profit stop          daily_profit_stop_inr (blocks new entries; open trades are still managed)
  * open positions / open lots        max_open_positions / max_open_lots

Concurrency: every read-modify-write happens under an exclusive OS file lock
(<risk_dir>/risk/.lock) and starts by re-reading the file, so one process never overwrites the
other's update. A new entry holds the lock from the final check to record_entry (entry_guard), so
two processes cannot both pass the check for the last free slot. Writes are atomic (state.write_json).

Fail closed: a corrupt or wrong-shaped state file locks the day and is left in place for
inspection (docs/nse/RECOVERY.md); a lock that cannot be taken blocks the entry. A lockout lasts for
the rest of that trading day; there is no command that clears it.

The daily loss counts realised P&L after charges plus open mark-to-market loss.
Risk per trade never increases after a loss: the budget is the smaller of the
per-trade cap and what is left of the daily limit.
"""

import contextlib
import os
import time

from . import clock, rr, state

try:                                    # POSIX (Linux, macOS, GitHub Actions)
    import fcntl
except ImportError:                     # pragma: no cover  (Windows)
    fcntl = None
    import msvcrt

# gate states a forward-validation book may trade through (no evidence yet); REJECTED/STALE always block
FORWARD_GATE_OK = ("INSUFFICIENT EVIDENCE", "NOT EVALUATED")
from .errors import StateCorrupt

WARNING = ("Limits are enforced on fills the bot knows about. Slippage, gaps and failed exits can "
           "make an actual loss larger than the configured daily limit.")

LOCK_TIMEOUT_SECONDS = 20


def fingerprint(order):
    return "%s|%s|%s|%s" % (order["instrument_id"], order["strategy"], order["signal_known_at"], order["side"])


def is_stop_exit(reason):
    """True for an exit at the stop-loss (including a gap through it), as named by exit.evaluate."""
    return isinstance(reason, str) and reason.startswith("stop")


class _FileLock(object):
    """Exclusive inter-process lock on one file; raises StateCorrupt when it cannot be taken in time."""

    def __init__(self, path, timeout=LOCK_TIMEOUT_SECONDS):
        self.path, self.timeout, self.f = path, timeout, None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        f = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:                                           # pragma: no cover
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                self.f = f
                return
            except OSError:
                if time.monotonic() >= deadline:
                    f.close()
                    raise StateCorrupt("risk state lock %s not acquired within %ss" % (self.path, self.timeout))
                time.sleep(0.05)

    def release(self):
        f, self.f = self.f, None
        if f is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:                                               # pragma: no cover
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.close()


class RiskBook(object):
    def __init__(self, path, doc, corrupt=None, owner=None):
        self.path = path
        self.doc = doc
        self.corrupt = corrupt
        self.owner = owner or "default"
        self.day = None
        self._lock = _FileLock(os.path.join(os.path.dirname(path), ".lock"))
        self._depth = 0

    @classmethod
    def open(cls, risk_dir, day, owner=None):
        """The shared book for `day`. `owner` names the calling process's book (its state_dir) so that
        consecutive system errors are counted per process; every other limit is combined."""
        path = os.path.join(risk_dir, "risk", "%s.json" % day.isoformat())
        book = cls(path, None, owner=owner)
        book.day = day
        book.refresh()                                  # loads the file under the lock
        return book

    # loading / saving ------------------------------------------------------------

    @staticmethod
    def _fresh(day, locked=None):
        return {"date": day.isoformat(), "realized_pnl": 0.0, "unrealized_pnl": 0.0, "unrealized": {},
                "trades": 0, "stop_exits": 0, "entries": [], "open": [], "open_lots": {},
                "consecutive_errors": 0, "errors_by_book": {},
                "lockout": {"locked": bool(locked), "reason": locked, "at": None}}

    @staticmethod
    def _shape_ok(doc):
        try:
            return (isinstance(doc["realized_pnl"], (int, float)) and isinstance(doc["trades"], int)
                    and isinstance(doc["entries"], list) and isinstance(doc["open"], list)
                    and isinstance(doc["lockout"]["locked"], bool)
                    and isinstance(doc.get("stop_exits", 0), int)
                    and isinstance(doc.get("unrealized", {}), dict)
                    and isinstance(doc.get("open_lots", {}), dict)
                    and isinstance(doc.get("errors_by_book", {}), dict))
        except (KeyError, TypeError):
            return False

    @staticmethod
    def _upgrade(doc):
        """Fill fields added on 2026-11-02 (shared book) into a file written by the older code."""
        doc.setdefault("stop_exits", 0)
        doc.setdefault("open_lots", {str(i): 1 for i in doc["open"]})
        if "unrealized" not in doc:
            u = doc.get("unrealized_pnl", 0.0) or 0.0
            doc["unrealized"] = {"_legacy": float(u)} if u and doc["open"] else {}
        doc.setdefault("errors_by_book", {})
        doc.setdefault("consecutive_errors", 0)
        doc["unrealized_pnl"] = round(sum(doc["unrealized"].values()), 2)
        return doc

    def _unavailable(self, reason):
        """The lock could not be taken: block entries now. Not sticky, and never saved: the next locked
        operation re-reads the file and replaces this placeholder."""
        self.doc = self._fresh(self.day or clock.now_ist().date(), locked="risk state unavailable: %s" % reason)

    def _fail(self, reason):
        """Fail closed for the rest of this process's day; the file on disk is never overwritten."""
        if not self.corrupt:
            self.corrupt = reason
        day = self.day or (clock.now_ist().date())
        self.doc = self._fresh(day, locked="risk state unusable: %s" % self.corrupt)

    def _load(self):
        if self.corrupt:                                # sticky: never trust a book that was corrupt
            self._fail(self.corrupt)
            return
        day_s = os.path.basename(self.path)[:-5]
        try:
            doc = state.read_json(self.path)
        except StateCorrupt as e:
            self._fail("state file corrupt: %s" % e)
            return
        if doc is None:
            self.doc = self._fresh(self.day) if self.day else self._fresh_from(day_s)
        elif doc.get("date") != day_s or not self._shape_ok(doc):
            self._fail("state file has the wrong date or shape")
        else:
            self.doc = self._upgrade(doc)

    def _fresh_from(self, day_s):
        import datetime as dt
        self.day = dt.date.fromisoformat(day_s)
        return self._fresh(self.day)

    @contextlib.contextmanager
    def _locked(self):
        """Exclusive lock (re-entrant within this object) with a fresh read of the file on entry."""
        if self._depth == 0:
            self._lock.acquire()
            try:
                self._load()
            except BaseException:
                self._lock.release()
                raise
        self._depth += 1
        try:
            yield self.doc
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._lock.release()

    def save(self):
        if self.corrupt:
            return           # never overwrite evidence; the in-memory lock still applies
        self.doc["unrealized_pnl"] = round(sum(self.doc.get("unrealized", {}).values()), 2)
        state.write_json(self.path, self.doc)

    def refresh(self):
        """Re-read the shared file (another strategy process may have changed it). Fails closed."""
        try:
            with self._locked():
                pass
        except StateCorrupt as e:
            self._unavailable(str(e))
        return self.doc

    @contextlib.contextmanager
    def entry_guard(self):
        """Hold the shared lock from the final risk check through the fill and record_entry, so ORB and
        Sweep cannot both take the last free entry or open slot. Yields False if the lock is unavailable
        (the caller must then not enter: check() also blocks on it)."""
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(self._locked())
                ok = True
            except StateCorrupt as e:
                self._unavailable(str(e))
                ok = False
            yield ok

    # state changes -------------------------------------------------------------

    @property
    def locked(self):
        if self._depth == 0:
            self.refresh()
        return self.doc["lockout"]["locked"]

    def lock(self, reason, now):
        with self._locked():
            if not self.doc["lockout"]["locked"]:
                self.doc["lockout"] = {"locked": True, "reason": reason, "at": clock.to_ist(now).isoformat()}
            self.save()

    def day_pnl(self):
        return self.doc["realized_pnl"] + sum(self.doc.get("unrealized", {}).values())

    def remaining_loss(self, cfg):
        if self._depth == 0:
            self.refresh()
        return cfg["max_daily_loss_inr"] + min(0.0, self.day_pnl())

    def open_lots(self):
        return sum(int(v) for v in self.doc.get("open_lots", {}).values())

    def _limits(self, cfg, now):
        """Lock the day when a combined limit is reached. Called with the lock held."""
        reason = None
        if self.day_pnl() <= -cfg["max_daily_loss_inr"]:
            reason = "daily loss limit reached: Rs %.2f" % self.day_pnl()
        elif self.doc["realized_pnl"] >= cfg["daily_profit_stop_inr"]:
            reason = ("daily profit stop reached: Rs %.2f (a stop-for-the-day level, not a target to repeat)"
                      % self.doc["realized_pnl"])
        elif self.doc["stop_exits"] >= cfg["max_stop_exits_per_day"]:
            reason = "%d stop-loss exits today (daily maximum %d)" % (self.doc["stop_exits"],
                                                                     cfg["max_stop_exits_per_day"])
        elif self.doc["consecutive_errors"] >= cfg["max_consecutive_errors"]:
            reason = "%d consecutive system errors" % self.doc["consecutive_errors"]
        if reason and not self.doc["lockout"]["locked"]:
            self.doc["lockout"] = {"locked": True, "reason": reason, "at": clock.to_ist(now).isoformat()}

    def record_entry(self, order, now, lots=1):
        with self._locked():
            self.doc["entries"].append(fingerprint(order))
            self.doc["open"].append(order["instrument_id"])
            key = str(order["instrument_id"])
            self.doc["open_lots"][key] = int(self.doc["open_lots"].get(key, 0)) + int(lots)
            self.doc["trades"] += 1
            self.save()

    def mark(self, unrealized, cfg, now, instrument_id=None):
        """Open mark-to-market P&L (after estimated exit charges). None = unknown -> keep last, alert."""
        if unrealized is None:
            return False
        with self._locked():
            u = self.doc["unrealized"]
            u.pop("_legacy", None)
            u[str(instrument_id) if instrument_id is not None else "_"] = float(unrealized)
            self._limits(cfg, now)
            self.save()
        return True

    def record_partial_exit(self, instrument_id, realized_pnl, cfg, now):
        """Part of a position sold (the rest is still open): realised P&L counts, the slot stays taken."""
        with self._locked():
            self.doc["realized_pnl"] = round(self.doc["realized_pnl"] + realized_pnl, 2)
            self._limits(cfg, now)
            self.save()

    def record_exit(self, instrument_id, realized_pnl, cfg, now, reason=None):
        """A position fully closed. A stop-loss exit (reason 'stop...') counts toward max_stop_exits_per_day."""
        with self._locked():
            if instrument_id in self.doc["open"]:
                self.doc["open"].remove(instrument_id)
            key = str(instrument_id)
            self.doc["open_lots"].pop(key, None)
            self.doc["unrealized"].pop(key, None)
            self.doc["unrealized"].pop("_", None)
            if not self.doc["open"]:
                self.doc["unrealized"] = {}
            self.doc["realized_pnl"] = round(self.doc["realized_pnl"] + realized_pnl, 2)
            if is_stop_exit(reason):
                self.doc["stop_exits"] += 1
            self._limits(cfg, now)
            self.save()

    def record_error(self, cfg, now):
        with self._locked():
            eb = self.doc["errors_by_book"]
            eb[self.owner] = int(eb.get(self.owner, 0)) + 1
            self.doc["consecutive_errors"] = max(eb.values())
            self._limits(cfg, now)
            self.save()

    def record_ok(self):
        if self.corrupt or not self.doc.get("errors_by_book", {}).get(self.owner):
            return
        with self._locked():
            eb = self.doc["errors_by_book"]
            if eb.get(self.owner):
                eb[self.owner] = 0
                self.doc["consecutive_errors"] = max(eb.values()) if eb else 0
                self.save()

    # the check -----------------------------------------------------------------

    def check(self, order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan):
        """PASS or BLOCK with every reason. Order keys: instrument_id, strategy, signal_known_at, side, expiry.
        Re-reads the shared file first; inside entry_guard the result stays valid until record_entry."""
        now = clock.to_ist(now)
        r = []
        try:
            with self._locked():
                self._limits(cfg, now)
                if self.doc["lockout"]["locked"] and not self.corrupt:
                    self.save()
                r = self._reasons(order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan)
        except StateCorrupt as e:                    # lock unavailable: no entry this time
            self._unavailable(str(e))
            r = ["LOCKED: %s" % self.doc["lockout"]["reason"]]
        forward_note = self._forward_note
        return {"verdict": "BLOCK" if r else "PASS", "reasons": r, "warning": WARNING, "forward_note": forward_note,
                "day_pnl": round(self.day_pnl(), 2), "trades": self.doc["trades"],
                "stop_exits": self.doc["stop_exits"], "open_lots": self.open_lots(),
                "remaining_loss_inr": round(cfg["max_daily_loss_inr"] + min(0.0, self.day_pnl()), 2)}

    def _reasons(self, order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan):
        r = []
        self._forward_note = None
        if cfg.get("live_orders_enabled") is not False or cfg.get("mode") != "paper":
            r.append("configuration is not paper mode")
        if self.doc["lockout"]["locked"]:
            r.append("LOCKED for today: %s" % self.doc["lockout"]["reason"])
        try:
            if not cal.is_trading_day(now.date()):
                r.append("not an NSE trading day")
        except Exception as e:                       # calendar gaps fail closed
            r.append("calendar: %s" % e)
        d = now.date()
        if not clock.at(d, cfg["no_entries_before"]) <= now <= clock.at(d, cfg["no_entries_after"]):
            r.append("outside the entry window %s-%s IST" % (cfg["no_entries_before"], cfg["no_entries_after"]))
        if order.get("side") != "BUY":
            r.append("only long option entries (BUY) are allowed")
        if gate_status != "APPROVED":
            if cfg.get("forward_validation") and str(gate_status).startswith(FORWARD_GATE_OK):
                self._forward_note = ("validation gate: %s (forward validation book, paper only: not blocking)"
                                      % gate_status)
            else:
                r.append("validation gate: %s" % gate_status)
        r.extend("quote: " + p for p in (quote_problems or []))
        if fingerprint(order) in self.doc["entries"]:
            r.append("duplicate: this signal was already entered today")
        if order["instrument_id"] in self.doc["open"]:
            r.append("already holding this contract")
        if self.doc["trades"] >= cfg["max_trades_per_day"]:
            r.append("trade count %d reached the daily maximum (all strategies combined)" % self.doc["trades"])
        if self.doc["stop_exits"] >= cfg["max_stop_exits_per_day"]:
            r.append("stop-loss exits %d reached the daily maximum %d (all strategies combined)"
                     % (self.doc["stop_exits"], cfg["max_stop_exits_per_day"]))
        if len(self.doc["open"]) >= cfg["max_open_positions"]:
            r.append("open positions at the maximum (%d, all strategies combined)" % cfg["max_open_positions"])
        if not sizing or sizing.get("lots", 0) < 1:
            r.append("sizing gives zero lots: %s" % "; ".join((sizing or {}).get("reasons", ["no sizing"])))
        else:
            # option lots only (a stock/ETF "lot" is one share); stocks are held to max_open_positions
            if order.get("kind", "index_options") == "index_options" and \
                    self.open_lots() + sizing["lots"] > cfg["max_open_lots"]:
                r.append("open lots %d + %d would exceed the maximum %d (all strategies combined)"
                         % (self.open_lots(), sizing["lots"], cfg["max_open_lots"]))
            if sizing.get("loss_at_stop_inr", 0) > cfg["max_risk_per_trade_inr"]:
                r.append("loss at stop Rs %.2f exceeds the per-trade risk Rs %.2f"
                         % (sizing["loss_at_stop_inr"], cfg["max_risk_per_trade_inr"]))
            remaining = cfg["max_daily_loss_inr"] + min(0.0, self.day_pnl())
            if sizing["loss_at_stop_inr"] > remaining:
                r.append("loss at stop Rs %.2f exceeds what is left of the daily limit Rs %.2f"
                         % (sizing["loss_at_stop_inr"], remaining))
        if not exit_plan or exit_plan.get("reward_risk", 0) < cfg["min_reward_risk"]:
            r.append("reward/risk below %.2f" % cfg["min_reward_risk"])
        if not exit_plan or not rr.ok(exit_plan.get("net"), cfg):
            r.append(rr.why((exit_plan or {}).get("net"), cfg))
        return r

    _forward_note = None
