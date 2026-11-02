"""Historical replay of sweep_reversal over a SweepDataset (data/sweep_store.py).

No look-ahead: at each replay minute `now` the strategy sees only bars that
closed at or before `now` and the option-chain snapshot taken at `now`; 1h and
daily bars are rebuilt from the 1m bars of earlier minutes and sessions.
No survivorship bias: contracts come from the recorded snapshots (expired
ones included), never from today's scrip master.

Fills use recorded option quotes only:
  entry  the NEXT minute's recorded ask + slippage_ticks
  exit   the recorded bid (minus slippage) of the first minute where the bid is at or
         below the premium stop, at or above the target, the underlying closes beyond
         its stop, or flat_by is reached. Never the stop price itself: a bid that
         gapped below the stop fills at that lower bid.
  A minute without a bid delays the exit (flagged); it is never treated as filled.
Charges: costs.round_trip for index options. Size: budget.size on the conservative
(sizing) stop, so a trade the paper loop could not afford is not counted.

Baseline ('coin'): the same setup minutes, with CE/PE chosen by a seeded coin.
"""

import bisect
import datetime as dt
import hashlib
from collections import Counter

from .. import budget, clock, contract_select, rr, structure as st, sweep
from ..errors import CalendarError, DataError
from .engine import split_days  # noqa: F401  (same chronological split for the gate)

BASELINE = "same setup minutes, CE/PE chosen by a seeded coin, same stop/target distances"


def all_days(ds, _unused=None):
    return ds.days()


def _upto(bars, ends, now):
    return bars[:bisect.bisect_right(ends, now)]


def _daily(u1_before):
    out = {}
    for b in u1_before:
        d = b[0].date()
        if d not in out:
            out[d] = [clock.at(d, "09:15"), b[1], b[2], b[3], b[4], None]
        else:
            r = out[d]
            r[2], r[3], r[4] = max(r[2], b[2]), min(r[3], b[3]), b[4]
    return [tuple(v) for _, v in sorted(out.items())]


def _coin(day, k):
    return hashlib.sha256(("%s|%d" % (day, k)).encode()).digest()[0] % 2 == 1


def _simulate(ds, sel, now, plan, u_stop, bias, cfg, costs, day):
    c = sel["contract"]
    tick, slip = c.tick_size, cfg["slippage_ticks"] * c.tick_size
    t_entry = now + dt.timedelta(minutes=1)
    leg = next((l for l in ds.chains.get(t_entry, []) if l["instrument_id"] == c.instrument_id), None)
    if leg is None or leg["ask"] is None or leg["bid"] is None:
        return None, "no recorded quote one minute after the signal"
    if leg["bid"] <= 0 or leg["bid"] > leg["ask"]:
        return None, "crossed or empty quote at entry"
    if (leg["ask"] - leg["bid"]) / ((leg["ask"] + leg["bid"]) / 2) * 100 > cfg["max_spread_pct"]:
        return None, "spread too wide at entry"
    entry = round(leg["ask"] + slip, 2)
    shift = entry - sel["entry"]
    stop, target = plan["stop"] + shift, plan["target"] + shift        # same distances from the real fill
    sizing = budget.size(entry, sel["sizing_stop"] + shift, c, cfg, costs, day, cfg["max_daily_loss_inr"])
    if sizing.get("lots", 0) < 1:
        return None, "sizing gives zero lots (Rs %s risk limit)" % cfg["max_risk_per_trade_inr"]
    qty = sizing["quantity"]
    net = rr.net(entry, sel["sizing_stop"] + shift, round(target - slip, 2), qty, costs, day, c.cost_segment)
    if not rr.ok(net, cfg):
        return {"skipped": rr.why(net, cfg)}, None
    if leg["ask_qty"] is None or leg["ask_qty"] < qty:
        return None, "displayed ask size below the order size"
    flat = clock.at(day, plan["flat_by"])
    u_close = {b[0] + dt.timedelta(minutes=1): b[4] for b in ds.u1 if b[0].date() == day}
    delayed = False
    t = t_entry
    while t <= clock.at(day, "15:30"):
        t += dt.timedelta(minutes=1)
        q = next((l for l in ds.chains.get(t, []) if l["instrument_id"] == c.instrument_id), None)
        if q is None or q["bid"] is None or q["bid"] <= 0:
            delayed = True
            continue
        bid, reason = q["bid"], None
        uc = u_close.get(t)
        if bid <= stop:
            reason = "stop" if bid >= stop - tick else "stop_gap"
        elif bid >= target:
            reason = "target"
        elif t >= flat:
            reason = "time"
        elif uc is not None and ((bias == "bullish" and uc < u_stop) or (bias == "bearish" and uc > u_stop)):
            reason = "underlying_stop"
        if reason:
            px = round(max(bid - slip, tick), 2)
            ch = costs.round_trip(entry, px, qty, day, c.cost_segment)["total"]
            return {"unresolved": False, "day": day.isoformat(), "underlying": ds.underlying_name,
                    "contract": c.symbol, "option_type": c.option_type, "signal_time": now.isoformat(),
                    "entry_time": t_entry.isoformat(), "exit_time": t.isoformat(), "entry": entry, "exit": px,
                    "stop": round(stop, 2), "target": round(target, 2), "qty": qty, "reason": reason,
                    "gross": round((px - entry) * qty, 2), "charges": ch, "net": round((px - entry) * qty - ch, 2),
                    "exit_delayed_by_missing_data": delayed, "regime": "n/a"}, None
    return {"unresolved": True, "day": day.isoformat(), "underlying": ds.underlying_name}, None


def run(ds, strategy, cfg, costs, cal, days=None, direction=None):
    sweep.validate(strategy)
    if getattr(ds, "kind", None) != "sweep":
        raise DataError("sweep_reversal replays need a sweep dataset (1m index, future and option snapshots)")
    p = strategy["params"]
    per_day = int(strategy.get("max_signals_per_day", 1))
    u_ends = [b[0] + dt.timedelta(minutes=1) for b in ds.u1]
    f_ends = [b[0] + dt.timedelta(minutes=1) for b in ds.f1]
    counts, invalid, trades, unresolved = Counter(), Counter(), [], 0
    for day in ds.days():
        if days is not None and day not in days:
            continue
        try:
            if not cal.is_trading_day(day):
                invalid["data on a non-trading day"] += 1
                continue
        except CalendarError as e:
            raise DataError(str(e))
        counts["candles"] += sum(1 for b in ds.u1 if b[0].date() == day)
        taken, busy_until = 0, None
        t = clock.at(day, cfg["no_entries_before"])
        last = clock.at(day, cfg["no_entries_after"])
        while t <= last and taken < per_day:
            now = t
            t += dt.timedelta(minutes=1)
            if busy_until and now < busy_until:
                continue
            u1 = _upto(ds.u1, u_ends, now)
            f1 = _upto(ds.f1, f_ends, now)
            before = [b for b in u1 if b[0].date() < day]
            u1d = _daily(before)
            u1h = st.closed(st.resample(u1, 60), now, 60)
            u1_eval = [b for b in u1 if b[0].date() >= (before[-1][0].date() if before else day)]

            def chain(now=now):
                legs = ds.chains.get(now)
                if not legs:
                    raise DataError("no recorded option chain at %s" % now.isoformat())
                return {"spot": None, "legs": legs}

            rep = sweep.evaluate(strategy, ds.underlying_name, now, u1_eval, u1h, u1d, f1, chain, ds, cfg)
            if rep["decision"] != "SETUP":
                continue
            counts["raw_occurrences"] += 1
            counts["independent"] += 1
            sel, u_stop, bias = rep["_selected"], rep["_u_stop"], rep["bias"]
            if direction == "coin" and _coin(day, taken):
                bias = "bearish" if bias == "bullish" else "bullish"
                spot = rep["underlying_plan"]["spot"]
                u_stop = 2 * spot - u_stop
                tgt = 2 * spot - rep["underlying_plan"]["target"]
                sel, _ = contract_select.select({"legs": ds.chains[now]}, ds, bias, spot, u_stop, tgt, now, cfg, p)
                if sel is None:
                    invalid["baseline: no eligible contract for the coin side"] += 1
                    taken += 1
                    continue
            plan = contract_select.plan_for(sel, cfg, day)
            tr, why = _simulate(ds, sel, now, plan, u_stop, bias, cfg, costs, day)
            taken += 1
            if why:
                invalid[why] += 1
                continue
            if tr.get("skipped"):
                counts["net_rr_no_trade"] += 1                 # a valid NO TRADE: never counted as a gate example
                continue
            counts["valid"] += 1
            if tr["unresolved"]:
                unresolved += 1
                continue
            trades.append(tr)
            counts["executed"] += 1
            busy_until = dt.datetime.fromisoformat(tr["exit_time"])
    return {"counts": dict(counts), "invalid": dict(invalid), "trades": trades, "unresolved": unresolved}
