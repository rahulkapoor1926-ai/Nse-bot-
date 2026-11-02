"""Historical SIGNAL study for the live NIFTY strategies, on real index (and index-future) 1-minute bars.

Why a signal study and not a P&L backtest: FYERS serves no expired option contracts and no historical
bid/ask (measured 2 Nov 2026, docs/nse/VALIDATION.md). Fills would have to be invented, so this study
stops where the option data would be needed. It reports WHEN each strategy would have signalled,
in which direction, which strike the bot would have aimed at, and the market regime. No premium, no
fill, no P&L: those come only from recorded bid/ask (data/fyers_recorder.py) as it accumulates.

Same logic as live, nothing re-implemented:
  ORB    patterns.detect on 5-minute bars resampled from the 1-minute bars (structure.resample, complete
         buckets only), first signal inside the entry hours (no_entries_before .. no_entries_after),
         entry window from patterns.entry_window. max_signals_per_day applies; later signals of a day are
         not looked for, because the first trade's exit (which frees the slot) cannot be simulated.
  Sweep  sweep.evaluate minute by minute with the bars that had closed by then and NO option chain: a day
         counts as a signal when every rule up to the option contract passed (the report then says
         "contract: no option chain source"). Days whose future data fail the quality checks are counted
         as not evaluable, never as "no signal".
"""

import bisect
import collections
import datetime as dt

from .. import clock, patterns, regime, structure as st, sweep
from .sweep_engine import _daily

NOT_SIMULATED = ("No option premium, fill or P&L: FYERS has no expired NIFTY options and no historical bid/ask. "
                 "The strike is the bot's target strike (nearest 50 to the trigger price), not a traded contract.")
STRIKE_STEP = 50


def _atm(price):
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)


def _iso(x):
    return x.isoformat() if isinstance(x, dt.datetime) else x


def _sessions(bars):
    out = collections.OrderedDict()
    for b in bars:
        out.setdefault(b[0].date(), []).append(b)
    return out


def orb(u1, strategy, cfg, cal):
    minutes = strategy["interval_minutes"]
    per_day = int(strategy.get("max_signals_per_day", 1))
    days = _sessions(u1)
    history = list(days)
    out, skipped = [], collections.Counter()
    for di, day in enumerate(history):
        if not cal.is_trading_day(day):
            skipped["not a trading day"] += 1
            continue
        bars = st.resample(days[day], minutes)
        if len(bars) < 6:
            skipped["incomplete session data"] += 1
            continue
        prev = history[di - 1] if di else None
        ph = pl = None
        if prev is not None and prev == cal.previous_trading_day(day):
            ph, pl = max(b[2] for b in days[prev]), min(b[3] for b in days[prev])
        reg = regime.fields(regime.classify([b for d in history[max(0, di - 30):di] for b in days[d]], day))
        start, k = 0, 0
        while k < per_day:
            sig = patterns.detect(strategy, bars, minutes, ph, pl, start=start)
            if sig is None:
                break
            if sig["known_at"] < clock.at(day, cfg["no_entries_before"]):
                start = sig["bar_index"] + 1
                continue
            if sig["known_at"] > clock.at(day, cfg["no_entries_after"]):
                break
            a, b = patterns.entry_window(sig, strategy)
            out.append({"strategy": strategy["name"], "family": "ORB", "day": day.isoformat(),
                        "signal_time": _iso(sig["known_at"]), "direction": sig["option_type"],
                        "option_type": sig["option_type"], "trigger_close": sig["trigger_close"],
                        "target_strike": _atm(sig["trigger_close"]), "entry_window": [_iso(a), _iso(b)],
                        "invalidate_below": sig["invalidate_below"], "invalidate_above": sig["invalidate_above"],
                        "levels": {k2: v for k2, v in (sig.get("levels") or {}).items()}, **reg})
            k += 1
    return {"signals": out, "sessions": len(history), "skipped": dict(skipped)}


def sweep_reversal(u1, f1, strategy, cfg, cal):
    """u1/f1 sorted 1-minute bars (index, current-month future with volume)."""
    p = strategy["params"]
    u_ends = [b[0] + dt.timedelta(minutes=1) for b in u1]
    f_ends = [b[0] + dt.timedelta(minutes=1) for b in f1]
    u_starts, f_starts = [b[0] for b in u1], [b[0] for b in f1]
    days = list(_sessions(u1))
    out, funnel, not_eval = [], collections.Counter(), collections.Counter()
    for di, day in enumerate(days):
        if not cal.is_trading_day(day) or di < 6:
            not_eval["warm-up or non-trading day"] += 1
            continue
        lo = clock.at(days[max(0, di - max(p["rvol_sessions"], 6) - 1)], "09:15")
        u_lo = bisect.bisect_left(u_starts, lo)
        f_lo = bisect.bisect_left(f_starts, lo)
        day_i = bisect.bisect_left(u_starts, clock.at(day, "00:00"))
        before = u1[:day_i]                                    # sessions before today: fixed all day
        u1d = _daily(before)
        prev_day = before[-1][0].date() if before else day
        t = clock.at(day, cfg["no_entries_before"])
        last = clock.at(day, cfg["no_entries_after"])
        final = None
        found = None
        while t <= last:
            now = t
            t += dt.timedelta(minutes=1)
            uu = u1[u_lo:bisect.bisect_right(u_ends, now)]
            ff = f1[f_lo:bisect.bisect_right(f_ends, now)]
            u1h = st.closed(st.resample(uu, 60), now, 60)
            u_eval = [b for b in uu if b[0].date() >= prev_day]
            rep = sweep.evaluate(strategy, "NIFTY", now, u_eval, u1h, u1d, ff, None, None, cfg)
            final = rep
            why = rep.get("skip_reason") or ""
            if why.startswith("data_future") or why.startswith("data_underlying"):
                continue
            if why.startswith("contract: no option chain source"):
                found = rep
                break
        if found is None:
            why = (final or {}).get("skip_reason") or "no evaluation"
            stage = why.split(":")[0]
            if stage in ("data_future", "data_underlying"):
                not_eval[why[:80]] += 1
            else:
                funnel[stage] += 1
            continue
        plan = found.get("underlying_plan") or {}
        spot = plan.get("spot") or (found.get("liquidity") or {}).get("reclaim_close")
        typ = "CE" if found.get("bias") == "bullish" else "PE"
        reg = regime.fields(regime.classify(before[-30 * 375:], day))
        out.append({"strategy": strategy["name"], "family": "Sweep", "day": day.isoformat(),
                    "signal_time": found.get("evaluated_at"), "direction": found.get("bias"), "option_type": typ,
                    "trigger_close": spot, "target_strike": _atm(spot) if spot else None,
                    "liquidity": found.get("liquidity"), "zone": found.get("zone"),
                    "structure": found.get("structure"), "underlying_plan": plan, **reg})
        funnel["SIGNAL"] += 1
    return {"signals": out, "sessions": len(days), "funnel_last_failed_rule": dict(funnel),
            "not_evaluable": dict(not_eval)}


def summarise(signals, sessions):
    by_month = collections.OrderedDict()
    for s in sorted(signals, key=lambda x: x["day"]):
        m = by_month.setdefault(s["day"][:7], {"signals": 0, "CE": 0, "PE": 0})
        m["signals"] += 1
        m[s["option_type"]] += 1
    reg = collections.Counter(s.get("regime") for s in signals)
    return {"signals": len(signals), "sessions": sessions,
            "signal_days_pct": round(100.0 * len({s["day"] for s in signals}) / sessions, 1) if sessions else None,
            "CE": sum(1 for s in signals if s["option_type"] == "CE"),
            "PE": sum(1 for s in signals if s["option_type"] == "PE"),
            "by_month": by_month, "by_regime": dict(reg)}


def charts(u1, signals, minutes=5):
    """5-minute OHLC of each signal day (for the dashboard's charts): real index bars only."""
    days = _sessions(u1)
    out = {}
    for d in sorted({s["day"] for s in signals}):
        bars = st.resample(days.get(dt.date.fromisoformat(d), []), minutes)
        out[d] = [[_iso(b[0]), b[1], b[2], b[3], b[4]] for b in bars]
    return out
