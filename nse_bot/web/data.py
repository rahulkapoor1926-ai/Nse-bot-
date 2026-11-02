"""Read-only views for the web dashboard. Every number comes from files the bot itself writes:

  <root>/                         standard ORB book   (paper ledger, alerts, dashboard/live.json)
  <root>/sweep/                   standard Sweep book
  <root>/forward/orb, /sweep      forward-validation books
  <root>/shared-risk, /forward/shared-risk   the ONE shared risk book per mode (risk.py)
  <root>/eod/latest.json          daily end-of-day report
  <recordings>/<day>/fyers_options_1m.csv    live FYERS snapshots (data/fyers_recorder.py)

Nothing here trades, sizes, prices or decides; the trading rules live only in the bot. A file that
is missing gives an empty section, a broken one gives an error message: never a made-up number.
"""

import csv
import glob
import json
import os

from .. import clock, dashboard, state
from ..errors import StateCorrupt

MODES = {
    "standard": {"ORB": "", "Sweep": "sweep", "risk": "shared-risk"},
    "forward": {"ORB": "forward/orb", "Sweep": "forward/sweep", "risk": "forward/shared-risk"},
}
RECORD_FILE = "fyers_options_1m.csv"
LIMITS_KEYS = ("capital_inr", "max_risk_per_trade_inr", "max_trades_per_day", "max_stop_exits_per_day",
               "max_daily_loss_inr", "daily_profit_stop_inr", "max_open_lots", "live_orders_enabled")


def _read(path, missing=None):
    try:
        return state.read_json(path, missing=missing)
    except StateCorrupt as e:
        return {"error": str(e)}


LEGACY_NOTE = ("historical record from before 2 Nov 2026 (equity/ETF scanning was switched off that day: "
               "NIFTY index options only). Kept for audit; not a current signal.")


def is_nifty_option(contract, strategy=None):
    """Current-scope record: a NIFTY index option, not written by an equity/ETF strategy."""
    if strategy and "equity" in str(strategy):
        return False
    c = str(contract or "")
    return c.startswith("NSE:NIFTY") and c[-2:] in ("CE", "PE") and "BANKNIFTY" not in c


def _jsonl_tail(path, n):
    try:
        return state.read_jsonl(path)[-n:]
    except StateCorrupt as e:
        return [{"error": str(e)}]


class View(object):
    def __init__(self, root, cfg, recordings=None, control_path=None, now=clock.now_ist):
        self.root, self.cfg, self.recordings, self.control_path, self.now = root, cfg, recordings, control_path, now

    def _dir(self, rel):
        return os.path.join(self.root, rel) if rel else self.root

    def _cfg(self, mode, book):
        m = MODES[mode]
        return dict(self.cfg, state_dir=self._dir(m[book]), risk_state_dir=self._dir(m["risk"]))

    # -- sections -------------------------------------------------------------

    def active_mode(self):
        """The mode whose bot wrote the newest heartbeat (standard if none)."""
        best, when = "standard", ""
        for mode, m in MODES.items():
            for book in ("ORB", "Sweep"):
                hb = (_read(os.path.join(self._dir(m[book]), "dashboard", "live.json"), {}) or {}).get("heartbeat")
                if hb and hb > when:
                    best, when = mode, hb
        return best

    def books(self, mode):
        now = self.now()
        out = {}
        for book in ("ORB", "Sweep"):
            c = self._cfg(mode, book)
            if not os.path.isdir(c["state_dir"]):
                out[book] = {"missing": True}
                continue
            try:
                out[book] = dashboard.build(c["state_dir"], c, now)
            except Exception as e:                  # noqa: BLE001  (shown, never hidden as zero)
                out[book] = {"error": "could not read the %s book: %s" % (book, e)}
        return out

    def risk(self, mode):
        day = self.now().date().isoformat()
        path = os.path.join(self._dir(MODES[mode]["risk"]), "risk", "%s.json" % day)
        doc = _read(path)
        lim = {k: self.cfg.get(k) for k in LIMITS_KEYS}
        if doc is None:
            return {"date": day, "file": "none yet today (no paper run has started)", "limits": lim}
        if "error" in doc:
            return {"date": day, "error": doc["error"], "limits": lim, "locked": True}
        realized = doc.get("realized_pnl", 0.0)
        unreal = doc.get("unrealized_pnl", 0.0)
        return {"date": day, "limits": lim, "realized_pnl": realized, "unrealized_pnl": unreal,
                "day_pnl": round(realized + unreal, 2), "entries": doc.get("trades", 0),
                "stop_exits": doc.get("stop_exits", 0), "open": doc.get("open", []),
                "open_lots": sum(int(v) for v in (doc.get("open_lots") or {}).values()),
                "locked": doc.get("lockout", {}).get("locked"), "lock_reason": doc.get("lockout", {}).get("reason"),
                "remaining_loss_inr": round(max(0.0, self.cfg["max_daily_loss_inr"] + min(0.0, realized + unreal)), 2),
                "profit_stop_hit": realized >= self.cfg["daily_profit_stop_inr"]}

    def signals(self, mode):
        """ORB candidates (bot alerts / forward signal log) and Sweep decisions, newest first."""
        day = self.now().date().isoformat()
        m = MODES[mode]
        orb = []
        for a in reversed(_jsonl_tail(os.path.join(self._dir(m["ORB"]), "alerts", "alerts.jsonl"), 300)):
            if a.get("kind") == "candidate" and str(a.get("time", "")).startswith(day):
                msg = a.get("message", "")
                status, _, rest = msg.partition(" ")
                sym, _, why = rest.partition(": ")
                orb.append({"time": a["time"], "status": status, "contract": sym, "reasons": why})
        fwd = [s for s in _jsonl_tail(os.path.join(self._dir(m["ORB"]), "forward", "signals.jsonl"), 300)
               if str(s.get("time", "")).startswith(day)]
        sweep = []
        sw_dir = os.path.join(self._dir(m["Sweep"]), "dashboard", "decisions-%s.jsonl" % day)
        for d in reversed(_jsonl_tail(sw_dir, 200)):
            sweep.append(d)
        hist = [dict(x, note=LEGACY_NOTE) for x in orb if not is_nifty_option(x["contract"])]
        hist += [dict(contract=x.get("contract"), time=x.get("time"), status=x.get("decision"),
                      reasons="; ".join(x.get("reasons") or []), strategy=x.get("strategy"), note=LEGACY_NOTE)
                 for x in reversed(fwd) if not is_nifty_option(x.get("contract"), x.get("strategy"))]
        orb = [x for x in orb if is_nifty_option(x["contract"])]
        fwd = [x for x in fwd if is_nifty_option(x.get("contract"), x.get("strategy"))]
        return {"date": day, "scope": "NIFTY index options only", "orb": orb[:60],
                "orb_forward_log": list(reversed(fwd))[:60], "sweep": sweep[:60],
                "rejected": [x for x in orb if x["status"] in ("REJECTED", "BLOCKED")][:60],
                "historical": hist[:100]}

    def performance(self, mode):
        rep = _read(os.path.join(self._dir(MODES[mode]["ORB"]), "forward", "report.json"))
        b = self.books(mode)
        return {"forward_report": rep, "by_book": {k: {"totals": v.get("totals"), "by_strategy": v.get("by_strategy")}
                                                   for k, v in b.items()}}

    def logs(self, mode, n=80):
        """The bot's own alert log and the exact Telegram message log, both books, newest first."""
        m = MODES[mode]
        out = []
        for book in ("ORB", "Sweep"):
            d = self._dir(m[book])
            for a in _jsonl_tail(os.path.join(d, "alerts", "alerts.jsonl"), n):
                text = a.get("message") or a.get("error")
                if a.get("kind") == "candidate" and not is_nifty_option(str(text or "").partition(" ")[2].partition(":")[0]):
                    text = "[historical, equity/ETF scanning since switched off] %s" % text
                out.append({"book": book, "source": "alert", "time": a.get("time"), "kind": a.get("kind"),
                            "text": text})
            for t in _jsonl_tail(os.path.join(d, "notify", "notify.jsonl"), n):
                out.append({"book": book, "source": "telegram", "time": t.get("time"), "kind": t.get("kind"),
                            "text": t.get("text") or t.get("message") or t.get("error")})
        out.sort(key=lambda x: str(x.get("time")), reverse=True)
        return out[:n]

    def reports(self):
        eod = _read(os.path.join(self.root, "eod", "latest.json"))
        days = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(self.root, "eod", "*.md")))
        md = None
        if days:
            with open(os.path.join(self.root, "eod", days[-1] + ".md"), encoding="utf-8") as f:
                md = f.read()[:20000]
        return {"latest": eod, "days": days, "latest_markdown": md}

    def market(self):
        """Latest minute of the live FYERS recorder: NIFTY spot, ATM + 2 ITM CE/PE, with status flags."""
        if not self.recordings:
            return {"available": False, "reason": "no live recorder in this deployment"}
        day = self.now().date().isoformat()
        path = os.path.join(self.recordings, day, RECORD_FILE)
        if not os.path.exists(path):
            return {"available": False, "reason": "no FYERS snapshot recorded today yet"}
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return {"available": False, "reason": "recording file is empty"}
        last = rows[-1]["ts"]
        snap = [r for r in rows if r["ts"] == last]
        summ = _read(os.path.join(self.recordings, day, "summary.json"), {}) or {}

        def num(v):
            return None if v in (None, "") else float(v)
        idx = next((r for r in snap if r["kind"] == "index"), None)
        opts = [{"symbol": r["symbol"], "type": r["option_type"], "moneyness": r["moneyness"],
                 "strike": num(r["strike"]), "expiry": r["expiry"], "bid": num(r["bid"]), "ask": num(r["ask"]),
                 "ltp": num(r["ltp"]), "volume": num(r["volume"]), "oi": num(r["oi"]), "status": r["status"],
                 "note": r["note"]} for r in snap if r["kind"] == "option"]
        spot_hist = [{"ts": r["ts"], "spot": num(r["ltp"])} for r in rows if r["kind"] == "index" and r["ltp"]][-120:]
        return {"available": True, "snapshot_time": last, "received_at": (idx or snap[0]).get("received_at"),
                "spot": num(idx["ltp"]) if idx else None, "spot_status": idx["status"] if idx else "MISSING",
                "atm_strike": num(snap[-1].get("atm_strike")), "options": opts, "spot_history": spot_hist,
                "api_calls_today": summ.get("api_calls_this_process"), "status_counts": summ.get("status_counts"),
                "source": "FYERS /options-chain-v3 (read-only)"}

    def controls(self):
        if not self.control_path:
            return {"available": False}
        from .. import control
        try:
            return dict(control.read_local(self.control_path), available=True)
        except Exception as e:                      # noqa: BLE001
            return {"available": True, "error": str(e)}

    def health(self, mode):
        out = {"mode": mode, "now": self.now().isoformat(), "live_orders_enabled": False}
        for book, rel in (("ORB", MODES[mode]["ORB"]), ("Sweep", MODES[mode]["Sweep"])):
            live = _read(os.path.join(self._dir(rel), "dashboard", "live.json"), {}) or {}
            out[book] = {"heartbeat": live.get("heartbeat"), "reported_status": live.get("status"),
                         "last_error": live.get("last_error")}
        mk = self.market()
        out["fyers"] = {"connected": bool(mk.get("available")) and mk.get("spot") is not None,
                        "last_snapshot": mk.get("snapshot_time"), "detail": mk.get("reason") or mk.get("spot_status")}
        sync = os.path.join(self.root, ".synced_at")
        out["state_synced_at"] = open(sync).read().strip() if os.path.exists(sync) else None
        return out


def dumps(doc):
    return json.dumps(doc, default=str)
