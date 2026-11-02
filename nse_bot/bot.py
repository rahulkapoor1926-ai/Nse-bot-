"""One paper-trading cycle: manage open positions, then look for one new entry.

Several strategies can run in one process (one risk book, one ledger), for
example an equity/ETF strategy and an index-option strategy. They are tried in
the order given; with max_open_positions = 1 the first entry that passes every
check is the only one taken. Index options are therefore used only when an
option trade is suitable on its own terms: at least one lot fits the risk and
outlay budget, the quote passes the liquidity checks, and the option strategy
itself passed the validation gate.

run_once() is called by the CLI loop every few seconds. Every step that can
fail is caught, alerted and counted toward the consecutive-error lockout; a
failure never turns into "position closed" or "price = 0".
"""

import logging

from . import budget, clock, entry, exit as exit_mod, feedcheck, gate, quotes, rr, scanner
from .errors import NseBotError

log = logging.getLogger("nse_bot")


class Context(object):
    def __init__(self, cfg, cal, master, md, costs, broker, risk, alerts, strategy, events=(),
                 dataset_digest=None, notifier=None):
        from .notify import Silent
        # P&L notifications: reporting only, never consulted for a trading decision
        self.notify = notifier or Silent()
        self.cfg, self.cal, self.master, self.md, self.costs = cfg, cal, master, md, costs
        self.broker, self.risk, self.alerts = broker, risk, alerts
        self.strategies = list(strategy) if isinstance(strategy, (list, tuple)) else [strategy]
        self.strategy = self.strategies[0]
        self.events = list(events)
        # {strategy name: digest} or one digest for every strategy
        self.dataset_digest = dataset_digest
        # latest bid and unrealised P&L per open position, for the dashboard only
        self.marks = {}


def enabled(ctx, strategy):
    kinds = ctx.cfg["instruments_enabled"]
    if strategy["instrument"] == "index_option":
        return "index_options" in kinds
    return "equity" in kinds or "etf" in kinds


def series_for(ctx, strategy):
    """[(key, instrument_id)] whose bars the strategy's pattern runs on."""
    if strategy["instrument"] == "index_option":
        return [(u, ctx.master.index_id(ctx.cfg["index_names"][u])) for u in ctx.cfg["underlyings"]]
    kinds = ctx.cfg["instruments_enabled"]
    return [(s.symbol, s.instrument_id) for s in ctx.master.stocks.values() if s.kind in kinds]


def _bars(ctx, now, strategy):
    """Today's bars per series, the last close, and the previous session's high/low."""
    out, last, prev = {}, {}, {}
    day = now.date()
    pday = ctx.cal.previous_trading_day(day)
    for key, iid in series_for(ctx, strategy):
        rows = ctx.md.candles(iid, "%dm" % strategy["interval_minutes"], clock.at(pday, "09:15"), now)
        out[key] = [r[:5] for r in rows if r[0].date() == day]
        before = [r for r in rows if r[0].date() == pday]
        if before:
            prev[key] = (max(r[2] for r in before), min(r[3] for r in before))
        if out[key]:
            last[key] = out[key][-1][4]
    return out, last, prev


def _digest(ctx, strategy):
    d = ctx.dataset_digest
    return d.get(strategy["name"]) if isinstance(d, dict) else d


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
            ctx.marks[iid] = {"bid": None, "time": now.isoformat(), "unrealized": None,
                              "note": decision["reason"]}
            ctx.alerts.emit("stale_data", "exit-%s-%s" % (iid, now.strftime("%H%M")), decision["reason"], now)
            ctx.notify.update(c, p, q, decision, ctx.marks[iid], now)
            events.append(("no_price", iid))
            continue
        bid = decision["price"]
        avg = p["cost"] / p["qty"]
        est_exit = ctx.costs.leg("SELL", bid, p["qty"], now.date(), c.cost_segment)["total"]
        unrealized = (bid - avg) * p["qty"] - p["charges"] - est_exit
        ctx.marks[iid] = {"bid": bid, "time": now.isoformat(), "unrealized": round(unrealized, 2)}
        ctx.risk.mark(unrealized, ctx.cfg, now)
        if decision["action"] != "EXIT":
            ctx.notify.update(c, p, q, decision, ctx.marks[iid], now)
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
        ctx.notify.closed(c, p, res, _last_buy(ctx, iid), decision["reason"], realized_now, now)
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


def _net_rr(ctx, c, ask, stop, target, sizing, now):
    """Net 1:2 check input (rr.py): fills at ask+slippage and bid-slippage, all charges."""
    slip = ctx.cfg["slippage_ticks"] * c.tick_size
    qty = (sizing or {}).get("quantity") or c.lot_size           # zero lots is blocked anyway; measure one lot
    try:
        return rr.net(round(ask + slip, 2), round(stop - slip, 2), round(target - slip, 2), qty, ctx.costs,
                      now.date(), c.cost_segment)
    except Exception as e:                      # noqa: BLE001  (no number means NO TRADE)
        return {"net_reward_risk": 0.0, "reason": str(e)}


def _last_buy(ctx, iid):
    buys = [r for r in ctx.broker.ledger.records()
            if r.get("event") == "fill" and r.get("side") == "BUY" and str(r.get("instrument_id")) == str(iid)]
    return buys[-1] if buys else None


def look_for_entry(ctx, now, ubars, prev=None, strategy=None):
    prev = prev or {}
    strategy = strategy or ctx.strategy
    if strategy["pattern"] == "sweep_reversal":
        from . import sweep_live
        return sweep_live.look(ctx, now, strategy)
    if strategy["instrument"] == "index_option":
        rows = scanner.scan(ubars, prev, ctx.master, ctx.cal, now, ctx.cfg, strategy)
    else:
        rows = scanner.scan_equities(ubars, prev, ctx.master, now, ctx.cfg, strategy)
    gstatus = gate.status_for(ctx.cfg["state_dir"], strategy, _digest(ctx, strategy))
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
        plan = exit_mod.plan(cand["entry_price"], c, strategy, ctx.cfg, now.date())
        sizing = budget.size(cand["entry_price"], plan["stop"], c, ctx.cfg, ctx.costs, now.date(),
                             ctx.risk.remaining_loss(ctx.cfg))
        plan["net"] = _net_rr(ctx, c, cand["entry_price"], plan["stop"], plan["target"], sizing, now)
        order = {"instrument_id": c.instrument_id, "strategy": strategy["name"],
                 "signal_known_at": row["signal"]["known_at"].isoformat(), "side": "BUY"}
        qp = quotes.check_for(c, q, now, ctx.cfg) + feedcheck.entry_problems(
            ctx, row["underlying"], q, row.get("spot"), now, getattr(c, "kind", "index_options"))
        verdict = ctx.risk.check(order, ctx.cfg, ctx.cal, now, gstatus, qp, sizing, plan)
        cand.update({"plan": plan, "sizing": sizing, "risk": verdict})
        out.append(cand)
        if verdict["verdict"] != "PASS":
            continue
        res = ctx.broker.buy(c, sizing["lots"], q, now, meta={
            "plan": plan, "strategy": strategy["name"], "signal_known_at": order["signal_known_at"],
            "invalidate_below": row["signal"]["invalidate_below"],
            "invalidate_above": row["signal"]["invalidate_above"], "data_source": ctx.cfg.get("data_source")})
        cand["fill"] = res
        if res["status"] == "FAILED":
            ctx.alerts.emit("system_error", "entryfail-" + key, "paper entry failed: %s" % res["reason"], now)
            continue
        ctx.risk.record_entry(order, now)
        ctx.alerts.emit("price", "filled-" + key, "PAPER bought %d %s @ %.2f; stop %.2f target %.2f" % (
            res["filled_qty"], c.symbol, res["price"], plan["stop"], plan["target"]), now)
        ctx.notify.opened(c, res, plan, now, strategy["name"])
        out.append({"entered": True})
        break                                   # at most one new entry per cycle
    return out


def run_once(ctx, now):
    now = clock.to_ist(now)
    st = ctx.cal.status(now)
    if not st["open"]:
        return {"market": st, "actions": []}
    series, last = [], {}
    for strat in ctx.strategies:
        if not enabled(ctx, strat):
            continue
        try:
            b, l, p = _bars(ctx, now, strat)
            ctx.risk.record_ok()
            series.append((strat, b, p))
            last.update(l)
        except NseBotError as e:
            ctx.risk.record_error(ctx.cfg, now)
            ctx.alerts.emit("system_error", "bars-%s-%s" % (strat["name"], now.strftime("%H%M")),
                            "market data failed for %s: %s" % (strat["name"], e), now)
    managed = manage_positions(ctx, now, last)
    if ctx.risk.locked or not series:
        return {"market": st, "managed": managed, "locked": ctx.risk.locked}
    cands = []
    for strat, b, p in series:
        got = look_for_entry(ctx, now, b, p, strat)
        entered = any(x.get("entered") for x in got)
        cands.extend(x for x in got if not x.get("entered"))
        if entered or ctx.risk.locked:
            break
    return {"market": st, "managed": managed, "candidates": cands}
