"""Paper-loop wiring for sweep_reversal: fetch closed candles, evaluate, verify the
evidence, then the SAME gate -> budget -> risk -> paper broker chain as every
other strategy. Every evaluation is stored as a Trade Decision Report.

Data per underlying (read-only SAHI calls, fetched once per new closed minute):
  index 1m (previous session + today), index 1h (6 sessions), index 1d (40 sessions),
  current-month index future 1m (rvol_sessions + today), option chain (only when the
  setup passes every underlying rule).
"""

import datetime as dt

from . import budget, clock, contract_select, decisions, entry, feedcheck, forward, gate, options_desk, orderflow, quotes, regime, scanner, sweep, volume_profile
from .errors import NseBotError

_CACHE = {}


def _sessions_back(cal, day, n):
    d = day
    for _ in range(n):
        d = cal.previous_trading_day(d)
    return d


def fetch(ctx, u, now):
    """Closed candles for one underlying; cached per closed minute."""
    minute = now.replace(second=0, microsecond=0)
    key = (u, minute)
    if key in _CACHE:
        return _CACHE[key]
    for k in [k for k in _CACHE if k[1] != minute]:
        del _CACHE[k]
    day = now.date()
    p = ctx.strategy_params
    iid = ctx.master.index_id(ctx.cfg["index_names"][u])
    md = ctx.md
    u1 = md.candles(iid, "1m", clock.at(_sessions_back(ctx.cal, day, 1), "09:15"), now)
    u1h = md.candles(iid, "1h", clock.at(_sessions_back(ctx.cal, day, 6), "09:15"), now)
    u1d = md.candles(iid, "1d", clock.at(_sessions_back(ctx.cal, day, 40), "09:15"), now)
    fut = ctx.master.current_future(u, day)
    f1 = [] if fut is None else md.candles(fut.instrument_id, "1m",
                                           clock.at(_sessions_back(ctx.cal, day, p["rvol_sessions"]), "09:15"), now)
    out = {"iid": iid, "u1": u1, "u1h": u1h, "u1d": u1d, "f1": f1,
           "future": None if fut is None else {"symbol": fut.symbol, "instrument_id": fut.instrument_id,
                                               "expiry": fut.expiry.isoformat()}}
    _CACHE[key] = out
    return out


def look(ctx, now, strategy):
    """One sweep_reversal pass over the configured underlyings. Returns candidate rows like bot.look_for_entry."""
    sweep.validate(strategy)
    ctx.strategy_params = strategy["params"]
    out = []
    day = now.date()
    for u in ctx.cfg["underlyings"]:
        try:
            d = fetch(ctx, u, now)
        except NseBotError as e:
            ctx.risk.record_error(ctx.cfg, now)
            ctx.alerts.emit("system_error", "sweep-data-%s-%s" % (u, now.strftime("%H%M")),
                            "sweep_reversal data fetch failed for %s: %s" % (u, e), now)
            continue
        expiry = scanner.choose_expiry(ctx.master, ctx.cal, u, day, ctx.cfg["min_days_to_expiry"])

        def get_chain(iid=d["iid"], expiry=expiry):
            if expiry is None:
                raise NseBotError("no listed expiry passes the calendar cross-check")
            return contract_select.parse_chain(ctx.md.option_chain(iid, expiry))

        rep = sweep.evaluate(strategy, u, now, d["u1"], d["u1h"], d["u1d"], d["f1"], get_chain, ctx.master, ctx.cfg)
        rep["future"] = d["future"] or "no current-month index future in the scrip master"
        rep["data_source"] = ctx.cfg.get("data_source")
        rep["raw_refs"] = {"index_instrument_id": d["iid"], "future": d["future"], "expiry": str(expiry),
                           "windows": {"1m": len(d["u1"]), "1h": len(d["u1h"]), "1d": len(d["u1d"]),
                                       "fut1m": len(d["f1"])},
                           "fetched_for_minute": now.replace(second=0, microsecond=0).isoformat()}
        decisions.enforce(rep, decisions.series_from(d["u1"], d["u1h"], d["u1d"], d["f1"]))
        chosen = rep.pop("_selected", None)
        u_stop = rep.pop("_u_stop", None)
        if rep["decision"] == "SETUP" and chosen:
            # market regime from the daily bars already fetched (regime.py): recorded, never a filter
            rep.setdefault("execution", {})["regime"] = regime.fields(regime.classify(d["u1d"], day))
            out.extend(_execute(ctx, now, strategy, u, rep, chosen, u_stop))
        decisions.record(ctx.cfg["state_dir"], rep, day)
        forward.sweep_signal(ctx, now, strategy, u, rep)
        if any(x.get("entered") for x in out):
            break
    return out


def _execute(ctx, now, strategy, u, rep, chosen, u_stop):
    c = chosen["contract"]
    bullish = rep["bias"] == "bullish"
    known = dt.datetime.fromisoformat(rep["signal_known_at"])
    signal = {"pattern": sweep.PATTERN, "option_type": c.option_type, "known_at": known,
              "trigger_close": rep["underlying_plan"]["spot"], "levels": {"level": rep["liquidity"]["level"]},
              "invalidate_below": u_stop if bullish else None, "invalidate_above": None if bullish else u_stop}
    # the backtest (backtest/sweep_engine.py) fills at the recorded quote one minute after the signal is
    # known; live enters in that same minute and not before
    row = {"underlying": u, "strategy": strategy["name"], "status": "SIGNAL", "signal": signal, "contract": c,
           "spot": signal["trigger_close"], "entry_from": known + dt.timedelta(minutes=1),
           "entry_until": known + dt.timedelta(minutes=2)}
    ex = rep.setdefault("execution", {})
    try:
        q = ctx.md.quotes([c.instrument_id]).get(c.instrument_id)
    except NseBotError as e:
        ctx.risk.record_error(ctx.cfg, now)
        ex["result"] = "quote fetch failed: %s" % e
        rep["decision"] = "BLOCKED"
        return []
    cand = entry.candidate(row, q, now, ctx.cfg, ctx.cal, ctx.events)
    if cand["status"] == "WAITING":
        ex["entry_check"] = {"status": "WAITING", "reasons": cand["reasons"]}
        rep["decision"] = "WAIT"
        return []
    key = "%s-%s" % (c.instrument_id, known.strftime("%H%M"))
    ctx.alerts.emit("candidate", key, "%s %s (sweep_reversal): %s" % (cand["status"], c.symbol, rep["why"]), now)
    ex["entry_check"] = {"status": cand["status"], "reasons": cand["reasons"]}
    if cand["status"] != "CANDIDATE":
        rep["decision"] = "BLOCKED"
        return [cand]
    plan = contract_select.plan_for(chosen, ctx.cfg, now.date())
    # size on the conservative stop (IV drop, theta, gap allowance, slippage), never on the planned stop
    sizing = budget.size(chosen["entry"], chosen["sizing_stop"], c, ctx.cfg, ctx.costs, now.date(),
                         ctx.risk.remaining_loss(ctx.cfg))
    plan["net"] = contract_select.net_rr(chosen, sizing.get("quantity") or c.lot_size, ctx.cfg, ctx.costs, now.date())
    gstatus = gate.status_for(ctx.cfg["state_dir"], strategy, _digest(ctx, strategy), ctx.cfg, ctx.costs)
    order = {"instrument_id": c.instrument_id, "strategy": strategy["name"],
             "signal_known_at": known.isoformat(), "side": "BUY"}
    qp = quotes.check_for(c, q, now, ctx.cfg) + feedcheck.entry_problems(ctx, u, q, signal["trigger_close"], now)
    of, ofp = orderflow.for_entry(ctx, "sweep", u, c.option_type, now)
    ex["orderflow"] = of
    qp = qp + ofp
    # auction context (volume profile, initial balance): recorded, never a filter
    ex["auction_context"] = volume_profile.for_entry(ctx, "sweep", u, c.option_type, now, signal["trigger_close"])
    # options-desk context (OI, PCR, IV, Greeks, GEX estimate): recorded, never a filter
    ex["options_desk"] = options_desk.for_entry(ctx, "sweep", u, c.option_type, c.expiry, now)
    verdict = ctx.risk.check(order, ctx.cfg, ctx.cal, now, gstatus, qp, sizing, plan)
    ex.update({"gate": gstatus, "sizing": {k: v for k, v in sizing.items() if k != "warning"},
               "risk": verdict["verdict"], "risk_reasons": verdict["reasons"], "plan": plan})
    cand.update({"plan": plan, "sizing": sizing, "risk": verdict})
    if verdict["verdict"] != "PASS":
        rep["decision"] = "BLOCKED"
        return [cand]
    res = ctx.broker.buy(c, sizing["lots"], q, now, meta={
        "plan": plan, "strategy": strategy["name"], "signal_known_at": order["signal_known_at"],
        "invalidate_below": signal["invalidate_below"], "invalidate_above": signal["invalidate_above"],
        "decision_report": rep["evaluated_at"], "data_source": ctx.cfg.get("data_source"),
        "regime": ex.get("regime")})
    ex["fill"] = {k: v for k, v in res.items() if k != "charges"}
    if res["status"] == "FAILED":
        ctx.alerts.emit("system_error", "entryfail-" + key, "paper entry failed: %s" % res["reason"], now)
        rep["decision"] = "BLOCKED"
        return [cand]
    ctx.risk.record_entry(order, now)
    rep["decision"] = "TAKE"
    ctx.alerts.emit("price", "filled-" + key, "PAPER bought %d %s @ %.2f; stop %.2f target %.2f (estimates)" % (
        res["filled_qty"], c.symbol, res["price"], plan["stop"], plan["target"]), now)
    ctx.notify.opened(c, res, plan, now, strategy["name"])
    return [cand, {"entered": True}]


def _digest(ctx, strategy):
    d = ctx.dataset_digest
    return d.get(strategy["name"]) if isinstance(d, dict) else d
