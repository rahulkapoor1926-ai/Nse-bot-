"""Pre-trade risk checks and the persistent daily lockout: ONE book shared by every strategy.

ORB and Liquidity Sweep run as two processes (own ledgers, own state_dir) but both open the SAME
risk file, <risk_state_dir>/risk/<YYYY-MM-DD>.json (risk_state_dir is one folder for both configs),
so every limit below is combined across the strategies:

  filled entries        max_trades_per_day (5)
  stop-loss exits       max_stop_exits_per_day (2): counted when a position is closed by its stop
  risk per trade        max_risk_per_trade_inr (500) planned net loss at the stop, all costs included
  daily net loss        max_daily_loss_inr (1000), realised after charges plus open mark-to-market
  profit stop           daily_profit_stop_inr (2000) realised: new entries blocked, open positions
                        are still managed to their exits
  open exposure         max_open_lots (1) NIFTY option lot open across both strategies

Concurrency: every read-modify-write holds an exclusive OS lock (fcntl.flock on <file>.lock), re-reads
the file from disk under the lock and writes it back atomically (temp file, fsync, rename). An entry
holds the lock from the risk check through the paper fill to record_entry (`transaction()`), so two
processes can never both pass a check for the last slot.

Fail closed: a corrupt file, a file with the wrong date or shape, or a lock that cannot be taken
locks the day; a corrupt file is left in place for inspection (docs/nse/RECOVERY.md). A lockout lasts
for the rest of that trading day; there is no command that clears it. Risk per trade never increases
after a loss: the budget is the smaller of the per-trade cap and what is left of the daily limit.
"""

import contextlib
import fcntl
import time
import os

from . import clock, rr, state

# gate states a forward-validation book may trade through (no evidence yet); REJECTED/STALE always block
FORWARD_GATE_OK = ("INSUFFICIENT EVIDENCE", "NOT EVALUATED")
from .errors import NseBotError, StateCorrupt

LOCK_TIMEOUT_S = 15


class RiskStateUnavailable(NseBotError):
    """The shared risk file could not be locked or read: entries are blocked (fail closed)."""


def dir_for(cfg):
    """The folder holding the shared risk book (both strategies' configs name the same one)."""
    return cfg.get("risk_state_dir") or cfg["state_dir"]


def open_for(cfg, day):
    """The shared RiskBook for `day`, owned (for open mark-to-market) by this config's book."""
    return RiskBook.open(dir_for(cfg), day, owner=cfg["state_dir"])


WARNING = ("Limits are enforced on fills the bot knows about. Slippage, gaps and failed exits can "
           "make an actual loss larger than the configured daily limit.")


def fingerprint(order):
    return "%s|%s|%s|%s" % (order["instrument_id"], order["strategy"], order["signal_known_at"], order["side"])


class RiskBook(object):
    def __init__(self, path, doc, corrupt=None, owner="default"):
        self.path = path
        self.doc = doc
        self.corrupt = corrupt
        self.owner = owner
        self.paused = None                     # set by control.py: new entries paused (reason), in memory only
        self._depth = 0
        self._fd = None

    @classmethod
    def open(cls, state_dir, day, owner="default"):
        path = os.path.join(state_dir, "risk", "%s.json" % day.isoformat())
        book = cls(path, cls._fresh(day), owner=owner)
        book.day = day
        try:
            with book.transaction():
                pass                                     # loads (or creates) the file under the lock
        except RiskStateUnavailable as e:
            book._fail(str(e))
        return book

    def _fail(self, why):
        self.corrupt = self.corrupt or why
        self.doc = self._fresh(self.day, locked=why)

    @staticmethod
    def _fresh(day, locked=None):
        return {"date": day.isoformat(), "realized_pnl": 0.0, "unrealized_pnl": 0.0, "unrealized": {},
                "trades": 0, "stop_exits": 0, "entries": [], "open": [], "open_lots": {}, "open_owner": {},
                "consecutive_errors": 0, "lockout": {"locked": bool(locked), "reason": locked, "at": None}}

    @staticmethod
    def _shape_ok(doc):
        try:
            return (isinstance(doc["realized_pnl"], (int, float)) and isinstance(doc["trades"], int)
                    and isinstance(doc["entries"], list) and isinstance(doc["open"], list)
                    and isinstance(doc["lockout"]["locked"], bool)
                    and isinstance(doc.get("stop_exits", 0), int) and isinstance(doc.get("unrealized", {}), dict)
                    and isinstance(doc.get("open_lots", {}), dict) and isinstance(doc.get("open_owner", {}), dict))
        except (KeyError, TypeError):
            return False

    # shared file: lock, reload, write -------------------------------------------

    def _load(self):
        if self.corrupt:
            return                                   # stays locked in memory; the evidence is not touched
        try:
            doc = state.read_json(self.path)
        except StateCorrupt as e:
            self._fail("state file corrupt: %s" % e)
            return
        except OSError as e:
            self._fail("risk state unavailable: %s" % e)
            return
        if doc is None:
            if not os.path.exists(self.path):
                self.doc = self._fresh(self.day)
                state.write_json(self.path, self.doc)
            return
        if doc.get("date") != self.day.isoformat() or not self._shape_ok(doc):
            self._fail("state file has the wrong date or shape")
            return
        for k, v in (("unrealized", {}), ("stop_exits", 0), ("open_lots", {}), ("open_owner", {})):
            doc.setdefault(k, v)
        self.doc = doc

    @contextlib.contextmanager
    def transaction(self):
        """Hold the exclusive lock on the shared file and work on its current contents. Re-entrant."""
        if self._depth == 0:
            lock_path = self.path + ".lock"
            try:
                os.makedirs(os.path.dirname(lock_path), exist_ok=True)
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as e:
                raise RiskStateUnavailable("cannot open the risk lock %s: %s" % (lock_path, e))
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        raise RiskStateUnavailable("risk book busy for %ds (another process holds the lock)"
                                                   % LOCK_TIMEOUT_S)
                    time.sleep(0.05)
            self._fd = fd
            try:
                self._load()
            except BaseException:
                self._release()
                raise
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._release()

    @contextlib.contextmanager
    def entry_lock(self):
        """Hold the shared lock from the risk check through the paper fill to record_entry, so two
        strategy processes can never both take the last slot. If the lock cannot be taken the book
        locks itself for the day and the check inside blocks (fail closed)."""
        with contextlib.ExitStack() as st:
            try:
                st.enter_context(self.transaction())
            except RiskStateUnavailable as e:
                self._fail(str(e))
            yield self

    def _release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def refresh(self):
        """Re-read the shared file (another strategy's process may have changed it)."""
        try:
            with self.transaction():
                pass
        except RiskStateUnavailable as e:
            self._fail(str(e))
        return self

    def save(self):
        if self.corrupt:
            return           # never overwrite evidence; the in-memory lock still applies
        self.doc["unrealized_pnl"] = round(sum(self.doc["unrealized"].values()), 2)
        state.write_json(self.path, self.doc)

    def _apply(self, fn):
        """Lock, reload, change, check limits, write. A lock failure locks the day (fail closed)."""
        try:
            with self.transaction():
                out = fn()
                self.save()
                return out
        except RiskStateUnavailable as e:
            self._fail(str(e))
            return None

    # state changes -------------------------------------------------------------

    @property
    def locked(self):
        if self._depth == 0:
            self.refresh()
        return self.doc["lockout"]["locked"]

    def _lock(self, reason, now):
        if not self.doc["lockout"]["locked"]:
            self.doc["lockout"] = {"locked": True, "reason": reason, "at": clock.to_ist(now).isoformat()}

    def lock(self, reason, now):
        self._apply(lambda: self._lock(reason, now))

    def day_pnl(self):
        return self.doc["realized_pnl"] + sum(self.doc.get("unrealized", {}).values())

    def remaining_loss(self, cfg):
        return cfg["max_daily_loss_inr"] + min(0.0, self.day_pnl())

    def open_lots(self):
        return sum(int(v) for v in self.doc.get("open_lots", {}).values())

    def _limits(self, cfg, now):
        if self.day_pnl() <= -cfg["max_daily_loss_inr"]:
            self._lock("daily loss limit reached: Rs %.2f (both strategies combined)" % self.day_pnl(), now)
        elif self.doc["realized_pnl"] >= cfg["daily_profit_stop_inr"]:
            self._lock("daily profit stop reached: Rs %.2f (a stop-for-the-day level, not a target to repeat); "
                       "open positions are still managed" % self.doc["realized_pnl"], now)
        elif self.doc["stop_exits"] >= cfg.get("max_stop_exits_per_day", 10 ** 6):
            self._lock("%d stop-loss exits today (both strategies combined)" % self.doc["stop_exits"], now)
        elif self.doc["consecutive_errors"] >= cfg["max_consecutive_errors"]:
            self._lock("%d consecutive system errors" % self.doc["consecutive_errors"], now)

    def record_entry(self, order, now, lots=1):
        def fn():
            iid = order["instrument_id"]
            self.doc["entries"].append(fingerprint(order))
            self.doc["open"].append(iid)
            self.doc["open_lots"][str(iid)] = self.doc["open_lots"].get(str(iid), 0) + int(lots)
            self.doc["open_owner"][str(iid)] = self.owner
            self.doc["trades"] += 1
        self._apply(fn)

    def mark(self, unrealized, cfg, now):
        """Open mark-to-market P&L of this book's positions (after estimated exit charges).
        None = unknown -> keep last, alert."""
        if unrealized is None:
            return False

        def fn():
            self.doc["unrealized"][self.owner] = float(unrealized)
            self._limits(cfg, now)
        self._apply(fn)
        return True

    def add_realized(self, realized_pnl, cfg, now):
        """A partial exit: realised P&L counts at once, the position stays open."""
        def fn():
            self.doc["realized_pnl"] = round(self.doc["realized_pnl"] + realized_pnl, 2)
            self._limits(cfg, now)
        self._apply(fn)

    def record_exit(self, instrument_id, realized_pnl, cfg, now, reason=None):
        def fn():
            if instrument_id in self.doc["open"]:
                self.doc["open"].remove(instrument_id)
            if instrument_id not in self.doc["open"]:
                self.doc["open_lots"].pop(str(instrument_id), None)
                self.doc["open_owner"].pop(str(instrument_id), None)
            self.doc["realized_pnl"] = round(self.doc["realized_pnl"] + realized_pnl, 2)
            if reason and str(reason).startswith("stop"):
                self.doc["stop_exits"] += 1
            if self.owner not in self.doc["open_owner"].values():
                self.doc["unrealized"].pop(self.owner, None)
            self._limits(cfg, now)
        self._apply(fn)

    def record_error(self, cfg, now):
        def fn():
            self.doc["consecutive_errors"] += 1
            self._limits(cfg, now)
        self._apply(fn)

    def record_ok(self):
        if self.doc["consecutive_errors"]:
            def fn():
                self.doc["consecutive_errors"] = 0
            self._apply(fn)

    # the check -----------------------------------------------------------------

    def check(self, order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan):
        """PASS or BLOCK with every reason. Order keys: instrument_id, strategy, signal_known_at, side, expiry."""
        now = clock.to_ist(now)
        try:
            with self.transaction():
                return self._check(order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan)
        except RiskStateUnavailable as e:
            self._fail(str(e))
            return self._check(order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan)

    def _check(self, order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan):
        self._limits(cfg, now)
        if self.doc["lockout"]["locked"] and self._fd is not None:
            self.save()
        r = []
        if cfg.get("live_orders_enabled") is not False or cfg.get("mode") != "paper":
            r.append("configuration is not paper mode")
        if self.doc["lockout"]["locked"]:
            r.append("LOCKED for today: %s" % self.doc["lockout"]["reason"])
        if self.paused:
            r.append(self.paused)
        try:
            if not cal.is_trading_day(now.date()):
                r.append("not an NSE trading day")
        except Exception as e:                       # calendar gaps fail closed
            r.append("calendar: %s" % e)
        d = now.date()
        if not clock.at(d, cfg["no_entries_before"]) <= now <= clock.at(d, cfg["no_entries_after"]):
            r.append("outside the entry window %s-%s IST" % (cfg["no_entries_before"], cfg["no_entries_after"]))
        if order.get("kind", "index_options") != "index_options":
            r.append("only NIFTY options are enabled: equity and ETF entries are disabled")
        if order.get("side") != "BUY":
            r.append("only long option entries (BUY) are allowed")
        forward_note = None
        if gate_status != "APPROVED":
            if cfg.get("forward_validation") and str(gate_status).startswith(FORWARD_GATE_OK):
                forward_note = "validation gate: %s (forward validation book, paper only: not blocking)" % gate_status
            else:
                r.append("validation gate: %s" % gate_status)
        r.extend("quote: " + p for p in (quote_problems or []))
        if fingerprint(order) in self.doc["entries"]:
            r.append("duplicate: this signal was already entered today")
        if order["instrument_id"] in self.doc["open"]:
            r.append("already holding this contract")
        if self.doc["trades"] >= cfg["max_trades_per_day"]:
            r.append("trade count %d reached the daily maximum (both strategies combined)" % self.doc["trades"])
        if self.doc["stop_exits"] >= cfg.get("max_stop_exits_per_day", 10 ** 6):
            r.append("%d stop-loss exits today: no new entries" % self.doc["stop_exits"])
        if len(self.doc["open"]) >= cfg["max_open_positions"]:
            r.append("open positions at the maximum (%d, both strategies combined)" % cfg["max_open_positions"])
        lots = (sizing or {}).get("lots", 0) or 0
        if lots and self.open_lots() + lots > cfg.get("max_open_lots", 10 ** 6):
            r.append("open lots would be %d, above the maximum %d (both strategies combined)"
                     % (self.open_lots() + lots, cfg["max_open_lots"]))
        if not sizing or sizing.get("lots", 0) < 1:
            r.append("sizing gives zero lots: %s" % "; ".join((sizing or {}).get("reasons", ["no sizing"])))
        elif sizing["loss_at_stop_inr"] > cfg["max_risk_per_trade_inr"]:
            r.append("planned net risk Rs %.2f exceeds the per-trade maximum Rs %.2f"
                     % (sizing["loss_at_stop_inr"], cfg["max_risk_per_trade_inr"]))
        elif sizing["loss_at_stop_inr"] > self.remaining_loss(cfg):
            r.append("loss at stop Rs %.2f exceeds what is left of the daily limit Rs %.2f"
                     % (sizing["loss_at_stop_inr"], self.remaining_loss(cfg)))
        if not exit_plan or exit_plan.get("reward_risk", 0) < cfg["min_reward_risk"]:
            r.append("reward/risk below %.2f" % cfg["min_reward_risk"])
        if not exit_plan or not rr.ok(exit_plan.get("net"), cfg):
            r.append(rr.why((exit_plan or {}).get("net"), cfg))
        return {"verdict": "BLOCK" if r else "PASS", "reasons": r, "warning": WARNING, "forward_note": forward_note,
                "day_pnl": round(self.day_pnl(), 2), "trades": self.doc["trades"],
                "stop_exits": self.doc["stop_exits"], "open_lots": self.open_lots(), "risk_file": self.path,
                "remaining_loss_inr": round(self.remaining_loss(cfg), 2)}
