"""Read-only dashboard data: turns the paper ledger, the daily risk files and the
loop's heartbeat into one JSON document for the web page.

Nothing here trades, sizes or decides. Every number comes from files the paper
bot already writes:

  paper/ledger.jsonl        simulated fills (hash-chained; verified here)
  risk/<date>.json          daily realised/unrealised P&L, trades, lockout
  dashboard/live.json       heartbeat, last error, latest bid per open position
  dashboard/names.json      instrument id -> symbol, kept so expired options keep their names

Output: dashboard/data.json. A broken ledger or risk file is reported as an
error in the document, never shown as zero.
"""

import datetime as dt
import glob
import os

from . import clock, state
from .broker.paper import Ledger
from .errors import StateCorrupt

LABEL = "SIMULATED (paper trading). No real orders. Not evidence of future profit."
STALE_AFTER_SECONDS = 180


def _dir(state_dir):
    return os.path.join(state_dir, "dashboard")


def remember_names(state_dir, master, ids):
    """Add symbols for instrument ids to names.json; returns the whole map."""
    path = os.path.join(_dir(state_dir), "names.json")
    names = state.read_json(path, missing={}) or {}
    changed = False
    for iid in ids:
        iid = str(iid)
        if iid in names or master is None:
            continue
        inst = master.by_id(int(iid))
        if inst is not None:
            names[iid] = inst.label()
            changed = True
    if changed:
        state.write_json(path, names)
    return names


def write_heartbeat(state_dir, now, status, marks=None, error=None, master=None):
    """status: running | stopped | error. Called by the paper loop after every cycle."""
    led = Ledger(state_dir)
    try:
        open_ids = [k for k, p in led.positions().items() if p["qty"] > 0]
        all_ids = {str(r["instrument_id"]) for r in led.records() if r.get("event") == "fill"}
    except StateCorrupt:
        open_ids, all_ids = [], set()
    remember_names(state_dir, master, all_ids)
    path = os.path.join(_dir(state_dir), "live.json")
    try:
        old = (state.read_json(path, missing={}) or {}).get("marks", {})
    except StateCorrupt:
        old = {}
    merged = dict(old, **(marks or {}))         # a cycle without a new price keeps the last one, with its time
    state.write_json(path, {
        "heartbeat": clock.to_ist(now).isoformat(), "status": status, "last_error": error,
        "marks": {k: v for k, v in merged.items() if k in open_ids},
    })


def _trades(fills, names):
    """Pair fills into round trips per instrument, with the same arithmetic as Ledger.positions."""
    book, done = {}, []
    for r in fills:
        iid = str(r["instrument_id"])
        p = book.setdefault(iid, {"qty": 0, "cost": 0.0, "charges": 0.0, "realized": 0.0,
                                  "bought": 0, "buy_value": 0.0, "sold": 0, "sell_value": 0.0,
                                  "opened": None, "meta": {}, "reasons": []})
        if r["side"] == "BUY":
            if p["qty"] == 0:
                p.update(opened=r["time"], meta=r.get("meta", {}))
            p["qty"] += r["qty"]
            p["cost"] += r["price"] * r["qty"]
            p["charges"] += r["charges"]["total"]
            p["bought"] += r["qty"]
            p["buy_value"] += r["price"] * r["qty"]
            continue
        avg = p["cost"] / p["qty"] if p["qty"] else 0.0
        share = r["qty"] / p["qty"] if p["qty"] else 0.0
        entry_ch = p["charges"] * share
        p["realized"] += (r["price"] - avg) * r["qty"] - r["charges"]["total"] - entry_ch
        p["cost"] -= avg * r["qty"]
        p["charges"] -= entry_ch
        p["qty"] -= r["qty"]
        p["sold"] += r["qty"]
        p["sell_value"] += r["price"] * r["qty"]
        if r.get("meta", {}).get("reason"):
            p["reasons"].append(r["meta"]["reason"])
        if p["qty"] == 0:
            done.append({
                "instrument_id": iid, "symbol": names.get(iid, iid),
                "strategy": p["meta"].get("strategy"), "qty": p["bought"],
                "entry_time": p["opened"], "entry_price": round(p["buy_value"] / p["bought"], 2),
                "exit_time": r["time"], "exit_price": round(p["sell_value"] / p["sold"], 2),
                "exit_reason": ", ".join(p["reasons"]) or None, "net_pnl": round(p["realized"], 2),
            })
            del book[iid]
    return done, book


def by_strategy(trades):
    """Completed-trade results per strategy, so strategies are never pooled."""
    out = {}
    for t in trades:
        s = out.setdefault(t.get("strategy") or "unknown", {"completed_trades": 0, "wins": 0, "net_pnl": 0.0})
        s["completed_trades"] += 1
        s["wins"] += t["net_pnl"] > 0
        s["net_pnl"] = round(s["net_pnl"] + t["net_pnl"], 2)
    for s in out.values():
        s["win_rate_pct"] = round(100.0 * s["wins"] / s["completed_trades"], 1)
    return out


def _decisions(state_dir, day):
    """Public one-line decision summaries for today (sweep_reversal), newest first."""
    try:
        rows = state.read_jsonl(os.path.join(_dir(state_dir), "decisions-%s.jsonl" % day))
    except StateCorrupt as e:
        return [{"error": str(e)}]
    return list(reversed(rows[-50:]))


def build(state_dir, cfg, now):
    now = clock.to_ist(now)
    today = now.date().isoformat()
    capital = float(cfg["capital_inr"])
    doc = {"label": LABEL, "generated_at": now.isoformat(), "capital_inr": capital,
           "daily_loss_limit_inr": cfg["max_daily_loss_inr"],
           "daily_profit_stop_inr": cfg["daily_profit_stop_inr"],
           "live_orders_enabled": False, "errors": [], "data_source": cfg.get("data_source")}
    names = state.read_json(os.path.join(_dir(state_dir), "names.json"), missing={}) or {}
    try:
        live = state.read_json(os.path.join(_dir(state_dir), "live.json"), missing={}) or {}
    except StateCorrupt as e:
        live = {}
        doc["errors"].append(str(e))
    led = Ledger(state_dir)
    try:
        doc["ledger_records"] = led.verify()
        fills = [r for r in led.records() if r.get("event") == "fill"]
        doc["ledger_integrity"] = "ok"
    except StateCorrupt as e:
        fills = []
        doc["ledger_integrity"] = "CORRUPT"
        doc["errors"].append(str(e))
    trades, open_book = _trades(fills, names)

    marks = live.get("marks", {})
    positions, deployed, unrealized = [], 0.0, 0.0
    for iid, p in sorted(open_book.items()):
        if p["qty"] <= 0:
            continue
        plan = p["meta"].get("plan", {})
        m = marks.get(iid, {})
        cost = p["cost"] + p["charges"]
        deployed += cost
        if m.get("unrealized") is not None:
            unrealized += m["unrealized"]
        positions.append({
            "instrument_id": iid, "symbol": names.get(iid, iid), "strategy": p["meta"].get("strategy"),
            "qty": p["qty"], "entry_time": p["opened"], "entry_price": round(p["cost"] / p["qty"], 2),
            "current_bid": m.get("bid"), "price_time": m.get("time"), "price_note": m.get("note"),
            "stop": plan.get("stop"), "target": plan.get("target"),
            "unrealized_pnl": m.get("unrealized"), "realized_so_far": round(p["realized"], 2),
        })
    realized_total = round(sum(t["net_pnl"] for t in trades) + sum(p["realized"] for p in open_book.values()), 2)

    risk_today = None
    days = []
    # the shared risk book (ORB + Sweep combined) lives in risk_dir, not in this book's state_dir
    for path in sorted(glob.glob(os.path.join(cfg.get("risk_dir", state_dir), "risk", "*.json"))):
        try:
            d = state.read_json(path)
            days.append({"date": d["date"], "realized_pnl": d["realized_pnl"], "trades": d["trades"],
                         "locked": d["lockout"]["locked"], "lock_reason": d["lockout"]["reason"]})
            if d["date"] == today:
                risk_today = d
        except (StateCorrupt, KeyError, TypeError) as e:
            doc["errors"].append("%s: %s" % (os.path.basename(path), e))
    if risk_today:
        day_pnl = risk_today["realized_pnl"] + risk_today["unrealized_pnl"]
        locked, lock_reason = risk_today["lockout"]["locked"], risk_today["lockout"]["reason"]
        day_realized, trades_today = risk_today["realized_pnl"], risk_today["trades"]
    else:
        day_pnl, locked, lock_reason, day_realized, trades_today = 0.0, False, None, 0.0, 0
    loss_room = cfg["max_daily_loss_inr"] + min(0.0, day_pnl)

    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    equity, curve = capital, [{"time": None, "equity": capital}]
    for t in sorted(trades, key=lambda t: t["exit_time"]):
        equity += t["net_pnl"]
        curve.append({"time": t["exit_time"], "equity": round(equity, 2)})

    hb = live.get("heartbeat")
    age = None
    if hb:
        age = (now - clock.to_ist(dt.datetime.fromisoformat(hb))).total_seconds()
    if live.get("status") == "error" or locked or doc["errors"]:
        status = "Error"
    elif live.get("status") == "running" and age is not None and age <= STALE_AFTER_SECONDS:
        status = "Running"
    else:
        status = "Stopped"

    doc.update({
        "bot": {"status": status, "heartbeat": hb, "last_error": live.get("last_error"),
                "stale_after_seconds": STALE_AFTER_SECONDS, "reported_status": live.get("status")},
        "today": {"date": today, "pnl": round(day_pnl, 2), "realized_pnl": day_realized,
                  "trades": trades_today, "locked": locked, "lock_reason": lock_reason,
                  "remaining_loss_inr": round(max(0.0, loss_room), 2)},
        "totals": {"realized_pnl": realized_total, "unrealized_pnl": round(unrealized, 2),
                   "total_pnl": round(realized_total + unrealized, 2),
                   "deployed_inr": round(deployed, 2),
                   "available_inr": round(capital + realized_total - deployed, 2),
                   "completed_trades": len(trades), "wins": wins,
                   "win_rate_pct": round(100.0 * wins / len(trades), 1) if trades else None},
        "by_strategy": by_strategy(trades),
        "decisions_today": _decisions(state_dir, today),
        "open_positions": positions,
        "trades": sorted(trades, key=lambda t: t["exit_time"], reverse=True),
        "equity_curve": curve,
        "days": days,
    })
    return doc


def write(state_dir, cfg, now):
    doc = build(state_dir, cfg, now)
    state.write_json(os.path.join(_dir(state_dir), "data.json"), doc)
    return doc
