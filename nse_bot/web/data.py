"""Read-only views for the web dashboard. Every number comes from files the bot itself writes:

  <root>/                         standard ORB book   (paper ledger, alerts, dashboard/live.json)
  <root>/sweep/                   standard Sweep book
  <root>/forward/orb, /sweep      forward-validation books
  <root>/shared-risk, /forward/shared-risk   the ONE shared risk book per mode (risk.py)
  <root>/eod/latest.json          NSE end-of-day report (public files, previous session; NEVER live)
  <recordings>/<day>/fyers_options_1m.csv    FYERS 1-minute REST snapshots (data/fyers_recorder.py)
  <live_feed>/<day>/live_quotes.json         FYERS WebSocket ticks (live_feed.py), display only

Nothing here trades, sizes, prices or decides; the trading rules live only in the bot. A file that
is missing gives an empty section, a broken one gives an error message: never a made-up number.
"""

import csv
import datetime as dt
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
SPOT_SYMBOL = "NSE:NIFTY50-INDEX"
STALE_SECONDS = 15        # a WebSocket quote older than this is flagged STALE (never shown as fresh)
REST_STALE_SECONDS = 90   # the REST snapshot is taken once a minute: older than this is STALE
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
    def __init__(self, root, cfg, recordings=None, control_path=None, now=clock.now_ist, study_path=None,
                 live_feed=None):
        self.root, self.cfg, self.recordings, self.control_path, self.now = root, cfg, recordings, control_path, now
        self.study_path, self.live_feed = study_path, live_feed

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
        return {"feed": "NSE END-OF-DAY", "live": False, "session_date": days[-1] if days else None,
                "label": "NSE public end-of-day files (daily OHLC, OI, settlement) for a CLOSED session. NOT LIVE, "
                         "not FYERS, and never used for fills, P&L or signals.",
                "latest": eod, "days": days, "latest_markdown": md}

    def _live_quotes(self):
        """FYERS WebSocket ticks written by live_feed.py, or None."""
        if not self.live_feed:
            return None
        return _read(os.path.join(self.live_feed, self.now().date().isoformat(), "live_quotes.json"))

    def _quote_time(self, q):
        t = q.get("exch_feed_time") or q.get("last_traded_time")
        if not t:
            return None, None
        when = clock.from_epoch_ms(float(t) * 1000.0)
        return when.isoformat(timespec="seconds"), round((self.now() - when).total_seconds(), 1)

    def market(self):
        """FYERS LIVE feed only: NIFTY spot and ATM/ITM CE/PE. Each quote carries its own exchange time,
        age and a STALE flag. WebSocket ticks are used where present; otherwise the 1-minute REST snapshot
        of the recorder. NSE end-of-day files are never used here (see reports())."""
        live = self._live_quotes() or {}
        lq = live.get("quotes") or {}
        rows, last = [], None
        if self.recordings:
            path = os.path.join(self.recordings, self.now().date().isoformat(), RECORD_FILE)
            if os.path.exists(path):
                with open(path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
        if rows:
            last = rows[-1]["ts"]
        if not rows and not lq:
            return {"available": False, "feed": "FYERS LIVE",
                    "reason": "no FYERS quote received today yet" if self.recordings or self.live_feed
                    else "no live FYERS feed in this deployment"}
        snap = [r for r in rows if r["ts"] == last] if last else []
        summ = (_read(os.path.join(self.recordings, self.now().date().isoformat(), "summary.json"), {}) or {}) \
            if self.recordings else {}

        def num(v):
            return None if v in (None, "") else float(v)

        def rest_age(ts):
            try:
                return round((self.now() - dt.datetime.fromisoformat(ts)).total_seconds(), 1)
            except (TypeError, ValueError):
                return None

        def flag(age, status="OK", limit=STALE_SECONDS):
            if age is None:
                return "MISSING"
            if age > limit:
                return "STALE"
            return status

        idx = next((r for r in snap if r["kind"] == "index"), None)
        ws_spot = lq.get(SPOT_SYMBOL)
        if ws_spot and ws_spot.get("ltp") is not None:
            t, age = self._quote_time(ws_spot)
            spot = {"spot": ws_spot["ltp"], "spot_time": t, "spot_age_s": age, "spot_source": "WebSocket",
                    "spot_status": flag(age)}
        elif idx:
            age = rest_age(last)
            spot = {"spot": num(idx["ltp"]), "spot_time": last, "spot_age_s": age, "spot_source": "REST 1-minute",
                    "spot_status": flag(age, limit=REST_STALE_SECONDS) if idx["status"] == "OK" else idx["status"]}
        else:
            spot = {"spot": None, "spot_time": None, "spot_age_s": None, "spot_source": None, "spot_status": "MISSING"}
        opts = []
        for r in (x for x in snap if x["kind"] == "option"):
            o = {"symbol": r["symbol"], "type": r["option_type"], "moneyness": r["moneyness"],
                 "strike": num(r["strike"]), "expiry": r["expiry"], "note": r["note"]}
            q = lq.get(r["symbol"])
            if q and (q.get("ltp") is not None or q.get("bid_price") is not None):
                t, age = self._quote_time(q)
                o.update(bid=q.get("bid_price"), ask=q.get("ask_price"), ltp=q.get("ltp"),
                         volume=q.get("vol_traded_today"), oi=q.get("OI"), quote_time=t, age_s=age,
                         source="WebSocket", status=flag(age))
                if o["oi"] is None and num(r["oi"]) is not None:      # the socket sends no OI for some ticks
                    o.update(oi=num(r["oi"]), oi_source="REST 1-minute", oi_time=last)
            else:
                age = rest_age(last)
                o.update(bid=num(r["bid"]), ask=num(r["ask"]), ltp=num(r["ltp"]), volume=num(r["volume"]),
                         oi=num(r["oi"]), quote_time=last, age_s=age, source="REST 1-minute",
                         status=flag(age, limit=REST_STALE_SECONDS) if r["status"] == "OK" else r["status"])
            opts.append(o)
        spot_hist = [{"ts": r["ts"], "spot": num(r["ltp"])} for r in rows if r["kind"] == "index" and r["ltp"]][-120:]
        written = live.get("written_at")
        feed_age = rest_age(written) if written else None
        ws = {"running": bool(live), "connected": bool(live.get("connected")), "written_at": written,
              "file_age_s": feed_age, "ticks_received": live.get("ticks_received"),
              "subscribed": len(live.get("subscribed") or []),
              "last_event": (live.get("events") or [None])[-1]} if live else {"running": False}
        return dict(spot, available=True, feed="FYERS LIVE", snapshot_time=last,
                    atm_strike=num(snap[-1].get("atm_strike")) if snap else None, options=opts,
                    spot_history=spot_hist, websocket=ws, stale_after_s=STALE_SECONDS,
                    rest_stale_after_s=REST_STALE_SECONDS,
                    api_calls_today=summ.get("api_calls_this_process"), status_counts=summ.get("status_counts"),
                    source="FYERS WebSocket ticks where marked, else FYERS /options-chain-v3 1-minute snapshot "
                           "(read-only)")

    # -- historical signal study (backtest/signal_study.py; no fills, no P&L) ------------

    def _study(self):
        if not self.study_path or not os.path.exists(self.study_path):
            return None
        return _read(self.study_path)

    def study(self):
        doc = self._study()
        if doc is None:
            return {"available": False, "reason": "no signal study in this deployment yet (run the signal-study workflow)"}
        if "error" in doc:
            return {"available": False, "reason": doc["error"]}
        out = {"available": True, "generated": doc.get("generated"), "source": doc.get("source"),
               "note": doc.get("note"), "coverage": doc.get("coverage"), "chart_days": sorted(doc.get("charts") or {}),
               "requested": doc.get("requested"), "index_daily": doc.get("index_daily"),
               "fetch_audit": doc.get("fetch_audit") or [], "calendar_added": doc.get("calendar_added") or {},
               "options_pnl": doc.get("options_pnl") or {"status": "NOT AVAILABLE",
                                                         "reason": "the study file has no option P&L section"}}
        for fam in ("ORB", "Sweep"):
            x = doc.get(fam) or {}
            out[fam] = {"summary": x.get("summary"), "signals": x.get("signals") or [],
                        "not_evaluable": x.get("not_evaluable"), "skipped": x.get("skipped"),
                        "funnel_last_failed_rule": x.get("funnel_last_failed_rule"),
                        "furthest_rule_failed": x.get("furthest_rule_failed"),
                        "rule_audit": x.get("rule_audit"), "day_audit": x.get("day_audit")}
        out["ORB_index_outcomes"] = doc.get("ORB_index_outcomes")
        out["option_audit"] = doc.get("option_audit")
        return out

    def study_csv(self):
        """Trade-by-trade audit CSV of the ORB index model (raw outcomes, marked filled/rejected by the
        portfolio simulation). None when the study has no outcomes."""
        doc = self._study()
        io_ = (doc or {}).get("ORB_index_outcomes") if doc and "error" not in doc else None
        if not io_ or "rows" not in io_:
            return None
        from ..backtest import signal_study as SS
        return SS.trades_csv(io_["rows"], io_.get("portfolio") or {}, self.cfg["max_risk_per_trade_inr"])

    def study_chart(self, day):
        doc = self._study()
        if not doc or "error" in doc:
            return {"available": False}
        bars = (doc.get("charts") or {}).get(day)
        if bars is None:
            return {"available": False, "reason": "no chart for %s" % day}
        sig = [s for fam in ("ORB", "Sweep") for s in ((doc.get(fam) or {}).get("signals") or []) if s.get("day") == day]
        return {"available": True, "day": day, "interval": "5m", "bars": bars, "signals": sig,
                "source": "NIFTY 50 index 1-minute bars from FYERS, resampled to 5 minutes"}

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
