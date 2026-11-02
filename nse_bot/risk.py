"""Pre-trade risk checks and the persistent daily lockout.

State lives in <state_dir>/risk/<YYYY-MM-DD>.json, written atomically after every
change, so a restart sees the same realised loss, trade count and lockout.
A lockout lasts for the rest of that trading day; there is no command that
clears it. A corrupt state file locks the day (fail closed) and is left in
place for inspection (docs/nse/RECOVERY.md).

The daily loss counts realised P&L after charges plus open mark-to-market loss.
Risk per trade never increases after a loss: the budget is the smaller of the
per-trade cap and what is left of the daily limit.
"""

import os

from . import clock, rr, state

# gate states a forward-validation book may trade through (no evidence yet); REJECTED/STALE always block
FORWARD_GATE_OK = ("INSUFFICIENT DATA", "NOT EVALUATED")
from .errors import StateCorrupt

WARNING = ("Limits are enforced on fills the bot knows about. Slippage, gaps and failed exits can "
           "make an actual loss larger than the configured daily limit.")


def fingerprint(order):
    return "%s|%s|%s|%s" % (order["instrument_id"], order["strategy"], order["signal_known_at"], order["side"])


class RiskBook(object):
    def __init__(self, path, doc, corrupt=None):
        self.path = path
        self.doc = doc
        self.corrupt = corrupt

    @classmethod
    def open(cls, state_dir, day):
        path = os.path.join(state_dir, "risk", "%s.json" % day.isoformat())
        try:
            doc = state.read_json(path)
        except StateCorrupt as e:
            return cls(path, cls._fresh(day, locked="state file corrupt: %s" % e), corrupt=str(e))
        if doc is None:
            doc = cls._fresh(day)
        elif doc.get("date") != day.isoformat() or not cls._shape_ok(doc):
            return cls(path, cls._fresh(day, locked="state file has the wrong date or shape"),
                       corrupt="bad shape")
        return cls(path, doc)

    @staticmethod
    def _fresh(day, locked=None):
        return {"date": day.isoformat(), "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "trades": 0, "entries": [], "open": [], "consecutive_errors": 0,
                "lockout": {"locked": bool(locked), "reason": locked, "at": None}}

    @staticmethod
    def _shape_ok(doc):
        try:
            return (isinstance(doc["realized_pnl"], (int, float)) and isinstance(doc["trades"], int)
                    and isinstance(doc["entries"], list) and isinstance(doc["open"], list)
                    and isinstance(doc["lockout"]["locked"], bool))
        except (KeyError, TypeError):
            return False

    def save(self):
        if self.corrupt:
            return           # never overwrite evidence; the in-memory lock still applies
        state.write_json(self.path, self.doc)

    # state changes -------------------------------------------------------------

    @property
    def locked(self):
        return self.doc["lockout"]["locked"]

    def lock(self, reason, now):
        if not self.locked:
            self.doc["lockout"] = {"locked": True, "reason": reason, "at": clock.to_ist(now).isoformat()}
        self.save()

    def day_pnl(self):
        return self.doc["realized_pnl"] + self.doc["unrealized_pnl"]

    def remaining_loss(self, cfg):
        return cfg["max_daily_loss_inr"] + min(0.0, self.day_pnl())

    def _limits(self, cfg, now):
        if self.day_pnl() <= -cfg["max_daily_loss_inr"]:
            self.lock("daily loss limit reached: Rs %.2f" % self.day_pnl(), now)
        elif self.doc["realized_pnl"] >= cfg["daily_profit_stop_inr"]:
            self.lock("daily profit stop reached: Rs %.2f (a stop-for-the-day level, not a target to repeat)"
                      % self.doc["realized_pnl"], now)
        elif self.doc["consecutive_errors"] >= cfg["max_consecutive_errors"]:
            self.lock("%d consecutive system errors" % self.doc["consecutive_errors"], now)

    def record_entry(self, order, now):
        self.doc["entries"].append(fingerprint(order))
        self.doc["open"].append(order["instrument_id"])
        self.doc["trades"] += 1
        self.save()

    def mark(self, unrealized, cfg, now):
        """Open mark-to-market P&L (after estimated exit charges). None = unknown -> keep last, alert."""
        if unrealized is None:
            return False
        self.doc["unrealized_pnl"] = float(unrealized)
        self._limits(cfg, now)
        self.save()
        return True

    def record_exit(self, instrument_id, realized_pnl, cfg, now):
        if instrument_id in self.doc["open"]:
            self.doc["open"].remove(instrument_id)
        self.doc["realized_pnl"] = round(self.doc["realized_pnl"] + realized_pnl, 2)
        if not self.doc["open"]:
            self.doc["unrealized_pnl"] = 0.0
        self._limits(cfg, now)
        self.save()

    def record_error(self, cfg, now):
        self.doc["consecutive_errors"] += 1
        self._limits(cfg, now)
        self.save()

    def record_ok(self):
        if self.doc["consecutive_errors"]:
            self.doc["consecutive_errors"] = 0
            self.save()

    # the check -----------------------------------------------------------------

    def check(self, order, cfg, cal, now, gate_status, quote_problems, sizing, exit_plan):
        """PASS or BLOCK with every reason. Order keys: instrument_id, strategy, signal_known_at, side, expiry."""
        now = clock.to_ist(now)
        self._limits(cfg, now)
        r = []
        if cfg.get("live_orders_enabled") is not False or cfg.get("mode") != "paper":
            r.append("configuration is not paper mode")
        if self.locked:
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
            r.append("trade count %d reached the daily maximum" % self.doc["trades"])
        if len(self.doc["open"]) >= cfg["max_open_positions"]:
            r.append("open positions at the maximum (%d)" % cfg["max_open_positions"])
        if not sizing or sizing.get("lots", 0) < 1:
            r.append("sizing gives zero lots: %s" % "; ".join((sizing or {}).get("reasons", ["no sizing"])))
        elif sizing["loss_at_stop_inr"] > self.remaining_loss(cfg):
            r.append("loss at stop Rs %.2f exceeds what is left of the daily limit Rs %.2f"
                     % (sizing["loss_at_stop_inr"], self.remaining_loss(cfg)))
        if not exit_plan or exit_plan.get("reward_risk", 0) < cfg["min_reward_risk"]:
            r.append("reward/risk below %.2f" % cfg["min_reward_risk"])
        if not exit_plan or not rr.ok(exit_plan.get("net"), cfg):
            r.append(rr.why((exit_plan or {}).get("net"), cfg))
        return {"verdict": "BLOCK" if r else "PASS", "reasons": r, "warning": WARNING, "forward_note": forward_note,
                "day_pnl": round(self.day_pnl(), 2), "trades": self.doc["trades"],
                "remaining_loss_inr": round(self.remaining_loss(cfg), 2)}
