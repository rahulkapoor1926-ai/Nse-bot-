"""One paper-trading cycle: manage open positions, then look for one new entry.

run_once() is called by the CLI loop every few seconds. Every step that can
fail is caught, alerted and counted toward the consecutive-error lockout; a
failure never turns into "position closed" or "price = 0".
"""

import logging

from . import budget, clock, entry, exit as exit_mod, gate, quotes, scanner
from .errors import NseBotError

log = logging.getLogger("nse_bot")


class Context(object):
    def __init__(self, cfg, cal, master, md, costs, broker, risk, alerts, strategy, events=(),
                 dataset_digest=None):
        self.cfg, self.cal, self.master, self.md, self.costs = cfg, cal, master, md, costs
        self.broker, self.risk, self.alerts, self.strategy = broker, risk, alerts, strategy
        self.events, self.dataset_digest = list(events), dataset_digest


def _underlying_bars(ctx, now):
    """Today's bars per index, the last close, and the previous session's high/low."""
    out, last, prev = {}, {}, {}
    day = now.date()
    pday = ctx.cal.previous_trading_day(day)
    for u in ctx.cfg["underlyings"]:
        iid = ctx.master.index_id(ctx.cfg["index_names"][u])
        rows = ctx.md.candles(iid, "%dm" % ctx.strategy["interval_minutes"], clock.at(pday, "09:15"), now)
        out[u] = [r[:5] for r in rows if r[0].date() == day]
        before = [r for r in rows if r[0].date() == pday]
        if before:
            prev[u] = (max(r[2] for r in before), min(r[3] for r in before))
        if out[u]:
            last[u] = out[u][-1][4]
    return out, last, prev


def manage_positions(ctx, now, ulast):
    events = []
    for iid, p in ctx.broker.ledger.positions().items():
        if p["qty"] <= 0:
            continue
        c = ctx.master.by_id(int(iid))
        meta = p.get("meta", {})
        if c is None:
            ctx.alerts.emit("system_error", "unknown-%s" % iid, "open position %s not in today's scrip master; "
                            "close it manually in the paper ledger review" % iid, now)
            continue
        try:
            q = ctx.md.quotes([c.instrument_id]).get(c.instrument_id)
            ctx.risk.record_ok()
        except NseBotError as e:
            ctx.risk.record_error(ctx.cfg, now)
            ctx.alerts.emit("system_error", "quote-%s-%s" % (iid, now.strftime("%H%M")),
                            "quote fetch failed for open %s: %s" % (c.symbol, e), now)
            q = None
        position = {"plan": meta["plan"], "invalidate_below": meta.get("invalidate_below"),
                    "invalidate_above": meta.get("invalidate_above")}
        decision = exit_mod.evaluate(position, q, now, ctx.cfg, ulast.get(c.underlying))
        if decision["action"] == "NO_PRICE":
            ctx.alerts.emit("stale_data", "exit-%s-%s" % (iid, now.strftime("%H%M")), decision["reason"], now)
            events.append(("no_price", iid))
            continue
        bid = decision["price"]
        avg = p["cost"] / p["qty"]
        est_exit = ctx.costs.leg("SELL", bid, p["qty"], now.date())["total"]
        ctx.risk.mark((bid - avg) * p["qty"] - p["charges"] - est_exit, ctx.cfg, now)
        if decision["action"] != "EXIT":
            continue
        res = ctx.broker.sell(c, p["qty"], q, now, meta={"reason": decision["reason"]})
        if res["status"] == "FAILED":
            ctx.alerts.emit("system_error", "exitfail-%s-%s" % (iid, now.strftime("%H%M")),
                            "EXIT FAILED for %s (%s): position is STILL OPEN" % (c.symbol, res["reason"]), now)
            ctx.risk.record_error(ctx.cfg, now)
            events.append(("exit_failed", iid))
            continue
        after = ctx.broker.ledger.positions()[iid]
        realized_now = after["realized"] - p["realized"]
        kind = "stop" if decision["reason"].startswith("stop") else "target" if decision["reason"] == "target" else "price"
        ctx.alerts.emit(kind, "%s-%s" % (iid, res["record"][:8]), "%s %s: sold %d @ %.2f (%s), P&L Rs %.2f%s" % (
            "PAPER", c.symbol, res["filled_qty"], res["price"], decision["reason"], realized_now,
            "; %d STILL OPEN (partial fill)" % res["unfilled_qty"] if res["status"] == "PARTIAL" else ""), now)
        if after["qty"] == 0:
            ctx.risk.record_exit(int(iid), realized_now, ctx.cfg, now)
        else:
            ctx.risk.doc["realized_pnl"] = round(ctx.risk.doc["realized_pnl"] + realized_now, 2)
            ctx.risk.save()
        events.append(("exit", iid, res["status"]))
    return events


def look_for_entry(ctx, now, ubars, prev=None):
    prev = prev or {}
    rows = scanner.scan(ubars, prev, ctx.master, ctx.cal, now, ctx.cfg, ctx.strategy)
    gstatus = gate.status_for(ctx.cfg["state_dir"], ctx.strategy, ctx.dataset_digest)
    out = []
    for row in rows:
        if row["status"] != "SIGNAL":
            if row["status"] == "ERROR":
                ctx.alerts.emit("system_error", "scan-%s-%s" % (row["underlying"], now.strftime("%H%M")),
                                "scan error: %s" % row["reason"], now)
            continue
        c = row["contract"]
        try:
            q = ctx.md.quotes([c.instrument_id]).get(c.instrument_id)
        except NseBotError as e:
            ctx.risk.record_error(ctx.cfg, now)
            ctx.alerts.emit("system_error", "q-%s-%s" % (c.instrument_id, now.strftime("%H%M")),
                            "quote fetch failed for %s: %s" % (c.symbol, e), now)
            continue
        cand = entry.candidate(row, q, now, ctx.cfg, ctx.cal, ctx.events)
        key = "%s-%s" % (c.instrument_id, row["signal"]["known_at"].strftime("%H%M"))
        ctx.alerts.emit("candidate", key, "%s %s: %s" % (cand["status"], c.symbol, "; ".join(
            cand.get("reasons_for") or cand["reasons"])), now)
        if cand["status"] != "CANDIDATE":
            out.append(cand)
            continue
        plan = exit_mod.plan(cand["entry_price"], c, ctx.strategy, ctx.cfg, now.date())
        sizing = budget.size(cand["entry_price"], plan["stop"], c, ctx.cfg, ctx.costs, now.date(),
                             ctx.risk.remaining_loss(ctx.cfg))
        order = {"instrument_id": c.instrument_id, "strategy": ctx.strategy["name"],
                 "signal_known_at": row["signal"]["known_at"].isoformat(), "side": "BUY"}
        verdict = ctx.risk.check(order, ctx.cfg, ctx.cal, now, gstatus,
                                 quotes.check(q, now, ctx.cfg, c.lot_size), sizing, plan)
        cand.update({"plan": plan, "sizing": sizing, "risk": verdict})
        out.append(cand)
        if verdict["verdict"] != "PASS":
            continue
        res = ctx.broker.buy(c, sizing["lots"], q, now, meta={
            "plan": plan, "strategy": ctx.strategy["name"], "signal_known_at": order["signal_known_at"],
            "invalidate_below": row["signal"]["invalidate_below"],
            "invalidate_above": row["signal"]["invalidate_above"]})
        cand["fill"] = res
        if res["status"] == "FAILED":
            ctx.alerts.emit("system_error", "entryfail-" + key, "paper entry failed: %s" % res["reason"], now)
            continue
        ctx.risk.record_entry(order, now)
        ctx.alerts.emit("price", "filled-" + key, "PAPER bought %d %s @ %.2f; stop %.2f target %.2f" % (
            res["filled_qty"], c.symbol, res["price"], plan["stop"], plan["target"]), now)
        break                                   # at most one new entry per cycle
    return out


def run_once(ctx, now):
    now = clock.to_ist(now)
    st = ctx.cal.status(now)
    if not st["open"]:
        return {"market": st, "actions": []}
    try:
        ubars, ulast, prev = _underlying_bars(ctx, now)
        ctx.risk.record_ok()
    except NseBotError as e:
        ctx.risk.record_error(ctx.cfg, now)
        ctx.alerts.emit("system_error", "bars-%s" % now.strftime("%H%M"), "underlying data failed: %s" % e, now)
        ubars, ulast, prev = None, {}, {}
    managed = manage_positions(ctx, now, ulast)
    if ctx.risk.locked or ubars is None:
        return {"market": st, "managed": managed, "locked": ctx.risk.locked}
    return {"market": st, "managed": managed, "candidates": look_for_entry(ctx, now, ubars, prev)}
