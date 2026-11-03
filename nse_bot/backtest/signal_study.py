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
OPTIONS_PNL = {
    "status": "NOT AVAILABLE",
    "simulated_option_trades": 0,
    "reason": ("Genuine historical NIFTY option prices with executable bid/ask are not available: FYERS refuses "
               "expired option contracts (HTTP 422, 30 of 30 probed on 2 Nov 2026), live contracts only have "
               "LTP candles since listing, and FYERS /history has no bid/ask at all. Trade count, wins, losses, "
               "SL hits, win rate, net P&L, drawdown and profit factor therefore cannot be computed without "
               "inventing prices, so none are shown."),
    "what_will_fill_it": ("The weekday FYERS recorder (09:10-15:31 IST) stores real ATM/ITM bid/ask every minute "
                          "from 3 Nov 2026; option P&L is computed only for signals on recorded days."),
}


def observed_calendar(file_years, daily_dates, bars_per_day, today, full_session_bars=300):
    """Trading calendar for PAST years the holiday file lacks, from what the exchange actually did:
    a weekday with no FYERS daily candle was a holiday; a weekday whose 1-minute data is a short special
    session (fewer than `full_session_bars` bars, e.g. Muhurat) is treated as a non-trading day too.
    Years in the holiday file (from NSE circulars) are kept exactly as they are. Only dates up to `today`
    are judged; a year is added only when the daily data covers it from its first weekday."""
    from .. import clock
    years = dict(file_years)
    sessions = set(daily_dates)
    added = {}
    for y in sorted({d.year for d in sessions}):
        if str(y) in years:
            continue
        d, last = dt.date(y, 1, 1), min(dt.date(y, 12, 31), today)
        first = min(x for x in sessions if x.year == y)
        hol, partial = {}, False
        while d <= last:
            if d.weekday() < 5:
                if d < first:
                    partial = True
                elif d not in sessions:
                    hol[d.isoformat()] = "no NSE session in FYERS daily data"
                elif d in bars_per_day and bars_per_day[d] < full_session_bars:
                    hol[d.isoformat()] = "special/short session (%d one-minute bars): not traded" % bars_per_day[d]
            d += dt.timedelta(days=1)
        years[str(y)] = {"holidays": hol}
        added[str(y)] = {"source": "observed: FYERS NIFTY 50 daily candles%s" % (
            " (from %s; earlier weekdays assumed trading, used only for warm-up)" % first if partial else ""),
            "holidays": hol}
    return clock.Calendar(years), added


def _atm(price):
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)


def _iso(x):
    return x.isoformat() if isinstance(x, dt.datetime) else x


def _sessions(bars):
    out = collections.OrderedDict()
    for b in bars:
        out.setdefault(b[0].date(), []).append(b)
    return out


def orb(u1, strategy, cfg, cal, native_minutes=1):
    """u1: sorted bars of `native_minutes` (1 = resampled to the strategy's interval; equal to the
    interval = FYERS's own bars of that size, used as they are)."""
    minutes = strategy["interval_minutes"]
    if native_minutes not in (1, minutes):
        raise ValueError("ORB needs 1-minute bars or %d-minute bars, not %d-minute" % (minutes, native_minutes))
    per_day = int(strategy.get("max_signals_per_day", 1))
    days = _sessions(u1)
    history = list(days)
    out, skipped = [], collections.Counter()
    for di, day in enumerate(history):
        if not cal.is_trading_day(day):
            skipped["not a trading day"] += 1
            continue
        bars = st.resample(days[day], minutes) if native_minutes == 1 else days[day]
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
                        "data": "%dm" % native_minutes,
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
    furthest = collections.Counter()           # per day: the furthest rule any minute of the day failed at
    days_audit = []
    order = {r: i for i, r in enumerate(sweep.RULES)}
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
        best = None
        best_rep = None
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
            st_ = why.split(":")[0]
            if st_ in order and (best is None or order[st_] > order[best]):
                best, best_rep = st_, rep
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
                furthest[best or stage] += 1
                days_audit.append(_day_audit(day, best_rep or final))
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
        furthest["SIGNAL"] += 1
        days_audit.append(_day_audit(day, found))
    return {"signals": out, "sessions": len(days), "funnel_last_failed_rule": dict(funnel),
            "furthest_rule_failed": {k: furthest[k] for k in list(sweep.RULES) + ["SIGNAL"] if furthest[k]},
            "rule_audit": rule_audit(days_audit), "day_audit": days_audit,
            "not_evaluable": dict(not_eval)}


def _day_audit(day, rep):
    """The day's furthest-reaching evaluation: when, each rule's status, and why it stopped."""
    rep = rep or {}
    return {"day": day.isoformat(), "evaluated_at": rep.get("evaluated_at"), "skip_reason": rep.get("skip_reason"),
            "rules": {r["rule"]: r["status"] for r in rep.get("rules") or []}}


def rule_audit(days_audit):
    """Rule by rule over the evaluated days: how many days reached the rule, passed it, failed it."""
    out = []
    for name in sweep.RULES:
        st_ = [d["rules"].get(name) for d in days_audit]
        passed, failed = st_.count("pass"), st_.count("fail")
        out.append({"rule": name, "days_reached": passed + failed, "passed": passed, "failed": failed})
    return out


WEEKLY_MONTH = "123456789OND"
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def nifty_expiry(day, cal):
    """NIFTY weekly expiry on or after `day`: Tuesday (NSE, from 1 Sep 2025), moved to the previous
    trading day when that Tuesday is a holiday. Derived from the exchange rule, labelled as derived."""
    t = day + dt.timedelta(days=(1 - day.weekday()) % 7)
    while True:
        e = t
        while not cal.is_trading_day(e):
            e -= dt.timedelta(days=1)
        if e >= day:
            return e
        t += dt.timedelta(days=7)


def option_symbol(day, option_type, strike, cal):
    """FYERS ticker of the contract the ORB bot would have aimed at (nearest expiry, min_days_to_expiry 0)."""
    e = nifty_expiry(day, cal)
    nxt = nifty_expiry(e + dt.timedelta(days=1), cal)
    monthly = nxt.month != e.month
    if monthly:
        sym = "NSE:NIFTY%02d%s%d%s" % (e.year % 100, MONTHS[e.month - 1], strike, option_type)
    else:
        sym = "NSE:NIFTY%02d%s%02d%d%s" % (e.year % 100, WEEKLY_MONTH[e.month - 1], e.day, strike, option_type)
    return {"symbol": sym, "expiry": e.isoformat(), "expiry_kind": "monthly" if monthly else "weekly",
            "strike": strike, "option_type": option_type,
            "basis": "derived: nearest NIFTY expiry (Tuesday, previous trading day if a holiday); ATM strike "
                     "nearest 50 to the signal's trigger close; strike_offset from the strategy"}


INDEX_OUTCOME_NOTE = (
    "INDEX-BASED, NOT OPTION P&L. The live ORB stop (-20% of premium) and target (2.5R on premium) are option "
    "prices and cannot be read from the index. This analysis uses the ORB signal's own index levels instead: "
    "stop = the opening-range midpoint (patterns.py invalidate level), target = entry +/- 2.5 x the index "
    "distance to that stop (the strategy's target_r), time exit at flat_by (expiry_day_flat_by on expiry day). "
    "Entry = open of the first 1-minute bar of the entry window. Hits are 1-minute high/low touches; a bar touching "
    "both is AMBIGUOUS. A stop is filled at the stop, or at the bar's open if the bar opened beyond it; a target is "
    "filled at the target. Points are NIFTY index points, not rupees.")


def index_outcomes(u1, signals, strategy, cfg, cal):
    """Index-based outcome of each ORB signal on the real 1-minute bars after its entry window opens."""
    tr = float(strategy["target_r"])
    by_day = _sessions(u1)
    out = []
    for s in signals:
        if s.get("family") != "ORB":
            continue
        day = dt.date.fromisoformat(s["day"])
        bars = by_day.get(day, [])
        start = dt.datetime.fromisoformat(s["entry_window"][0])
        expiry = nifty_expiry(day, cal)
        flat = clock.at(day, cfg["expiry_day_flat_by"] if expiry == day else cfg["flat_by"])
        row = {"day": s["day"], "signal_time": s["signal_time"], "option_type": s["option_type"],
               "flat_by": flat.isoformat(), "expiry": expiry.isoformat()}
        after = [b for b in bars if start <= b[0] and b[0] + dt.timedelta(minutes=1) <= flat]
        if not after:
            out.append(dict(row, outcome="NO DATA", reason="no 1-minute bars between the entry window and the time exit"))
            continue
        ce = s["option_type"] == "CE"
        entry = after[0][1]
        stop = s["invalidate_below"] if ce else s["invalidate_above"]
        risk = (entry - stop) if ce else (stop - entry)
        row.update(entry_time=after[0][0].isoformat(), entry=entry, stop=round(stop, 2))
        if risk <= 0:
            out.append(dict(row, outcome="INVALID", reason="entry already beyond the stop level"))
            continue
        target = entry + tr * risk if ce else entry - tr * risk
        row.update(target=round(target, 2), risk_points=round(risk, 2))
        res = None
        for b in after:
            hit_t = b[2] >= target if ce else b[3] <= target
            hit_s = b[3] <= stop if ce else b[2] >= stop
            if hit_t and hit_s:
                res = ("AMBIGUOUS", b[0], None, "target and stop both inside the %s bar" % b[0].strftime("%H:%M"))
            elif hit_s:
                gap = (b[1] < stop) if ce else (b[1] > stop)          # the bar OPENED beyond the stop
                res = ("SL", b[0], b[1] if gap else stop,
                       "gapped through the stop: exit at the bar open %.2f" % b[1] if gap else None)
            elif hit_t:
                res = ("TARGET", b[0], target, None)
            if res:
                break
        if res is None:
            last = after[-1]
            res = ("TIME EXIT", last[0] + dt.timedelta(minutes=1), last[4], "flat at %s" % flat.strftime("%H:%M"))
        kind, when, price, why = res
        row.update(outcome=kind, exit_time=when.isoformat(), exit=None if price is None else round(price, 2),
                   reason=why, minutes_held=int((when - after[0][0]).total_seconds() // 60))
        if price is not None:
            pts = (price - entry) if ce else (entry - price)
            row.update(index_points=round(pts, 2), r_multiple=round(pts / risk, 4))
            if kind == "TIME EXIT":
                row["time_exit_result"] = "profit" if pts > 0 else "loss" if pts < 0 else "breakeven"
        out.append(row)
    return out


def summarise_outcomes(rows):
    kinds = ("TARGET", "SL", "TIME EXIT", "AMBIGUOUS", "INVALID", "NO DATA")
    tot = {k: sum(1 for r in rows if r["outcome"] == k) for k in kinds}
    by_month = collections.OrderedDict()
    for r in sorted(rows, key=lambda x: x["day"]):
        m = by_month.setdefault(r["day"][:7], dict({k: 0 for k in kinds}, signals=0, index_points=0.0))
        m["signals"] += 1
        m[r["outcome"]] += 1
        m["index_points"] = round(m["index_points"] + (r.get("index_points") or 0.0), 2)
    resolved = tot["TARGET"] + tot["SL"]
    return {"note": INDEX_OUTCOME_NOTE, "signals": len(rows), "totals": tot,
            "target_hit_pct_of_target_or_sl": round(100.0 * tot["TARGET"] / resolved, 1) if resolved else None,
            "index_points_total": round(sum(r.get("index_points") or 0.0 for r in rows), 2),
            "time_exit_points": round(sum(r.get("index_points") or 0.0 for r in rows if r["outcome"] == "TIME EXIT"), 2),
            "by_month": by_month}


def index_model(rows, cfg, risk_inr=None, label="INDEX-BASED MODEL"):
    """Performance of the INDEX-BASED model in R (1R = the index distance to the stop) and, ONLY as a
    labelled hypothetical, in rupees with 1R = max_risk_per_trade_inr, before charges and slippage.
    AMBIGUOUS, INVALID and NO DATA outcomes are excluded (counted separately), never guessed.
    Every figure is derived from the per-trade R list in chronological order (entry time)."""
    risk_inr = float(cfg["max_risk_per_trade_inr"] if risk_inr is None else risk_inr)
    cap = float(cfg["capital_inr"])
    rs = sorted((r for r in rows if r.get("r_multiple") is not None), key=lambda x: x.get("entry_time") or x["day"])
    n = len(rs)
    R = [r["r_multiple"] for r in rs]
    wins = [x for x in R if x > 0]
    losses = [x for x in R if x < 0]
    flat = [x for x in R if x == 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    net = sum(R)

    def group(kind, res=None):
        g = [r["r_multiple"] for r in rs if r["outcome"] == kind and (res is None or r.get("time_exit_result") == res)]
        return {"trades": len(g), "net_r": round(sum(g), 2)}
    eq, peak, peak_day, mdd, mdd_peak_day, mdd_day, mdd_peak_eq = 0.0, 0.0, None, 0.0, None, None, 0.0
    run, worst_run, curve = 0, 0, []
    for r in rs:
        eq += r["r_multiple"]
        if eq > peak:
            peak, peak_day = eq, r["day"]
        if peak - eq > mdd:
            mdd, mdd_day, mdd_peak_day, mdd_peak_eq = peak - eq, r["day"], peak_day, peak
        run = run + 1 if r["r_multiple"] < 0 else 0
        worst_run = max(worst_run, run)
        curve.append([r["day"], round(eq, 4)])
    by_month = collections.OrderedDict()
    for r in rs:
        m = by_month.setdefault(r["day"][:7], {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_r": 0.0,
                                                "targets": 0, "stops": 0, "time_exits": 0})
        m["trades"] += 1
        m["wins" if r["r_multiple"] > 0 else "losses" if r["r_multiple"] < 0 else "breakeven"] += 1
        m[{"TARGET": "targets", "SL": "stops", "TIME EXIT": "time_exits"}[r["outcome"]]] += 1
        m["net_r"] += r["r_multiple"]
    for m in by_month.values():
        m["net_r"] = round(m["net_r"], 2)
        m["hypothetical_inr"] = round(m["net_r"] * risk_inr, 2)
        m["win_rate_pct"] = round(100.0 * m["wins"] / m["trades"], 1) if m["trades"] else None
    peak_cap = cap + mdd_peak_eq * risk_inr
    return {
        "label": "%s, NOT OPTION P&L (1R = Rs %d of risk; before brokerage, taxes and slippage)" % (label, risk_inr),
        "trades_counted": n, "wins": len(wins), "losses": len(losses), "breakeven": len(flat),
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else None,
        "win_rate_formula": "wins / trades counted = %d / %d; a win is any trade with R > 0 (targets plus "
                            "time exits that closed in profit)" % (len(wins), n),
        "breakdown": {"target": group("TARGET"), "stop_loss": group("SL"),
                      "time_exit_profit": group("TIME EXIT", "profit"), "time_exit_loss": group("TIME EXIT", "loss"),
                      "time_exit_breakeven": group("TIME EXIT", "breakeven")},
        "excluded": {k: sum(1 for r in rows if r["outcome"] == k) for k in ("AMBIGUOUS", "INVALID", "NO DATA")},
        "gross_gain_r": round(gross_win, 2), "gross_loss_r": round(gross_loss, 2), "net_r": round(net, 2),
        "expectancy_r": round(net / n, 4) if n else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "avg_win_r": round(gross_win / len(wins), 3) if wins else None,
        "avg_loss_r": round(gross_loss / len(losses), 3) if losses else None,
        "max_drawdown_r": round(mdd, 2), "max_drawdown_peak_day": mdd_peak_day, "max_drawdown_trough_day": mdd_day,
        "max_consecutive_losses": worst_run,
        "profitable_months": sum(1 for m in by_month.values() if m["net_r"] > 0), "months": len(by_month),
        "hypothetical_inr": {"start": cap, "net": round(net * risk_inr, 2), "end": round(cap + net * risk_inr, 2),
                             "gross_gain": round(gross_win * risk_inr, 2), "gross_loss": round(gross_loss * risk_inr, 2),
                             "max_drawdown": round(mdd * risk_inr, 2),
                             "max_drawdown_pct_of_peak": round(100.0 * mdd * risk_inr / peak_cap, 2) if peak_cap else None,
                             "peak_capital_before_drawdown": round(peak_cap, 2),
                             "formula": "end = start + net R x Rs %d = %.0f + (%.2f x %d) = %.2f" % (
                                 risk_inr, cap, net, risk_inr, cap + net * risk_inr)},
        "by_month": by_month, "equity_curve_r": [[d, round(e, 2)] for d, e in curve],
    }


def portfolio(rows, cfg, risk_inr=None):
    """Executable portfolio simulation of the index model with the approved limits applied in time order:
    at most max_trades_per_day filled entries, max_stop_exits_per_day stop exits (no new entry after the
    limit), no new entry once the day's net loss reaches max_daily_loss_inr or realised profit reaches
    daily_profit_stop_inr, one lot open at a time (no entry while another trade is open), and the planned
    loss at the stop must fit max_risk_per_trade_inr (index model: 1R = that amount, so it always fits).
    A rejected signal is never counted as a trade."""
    risk_inr = float(cfg["max_risk_per_trade_inr"] if risk_inr is None else risk_inr)
    taken, rejected = [], []
    day_state = {}
    open_until = None
    for r in sorted(rows, key=lambda x: x.get("entry_time") or x["day"]):
        if r.get("r_multiple") is None:
            rejected.append(dict(r, rejected_because="no resolvable index outcome (%s)" % r["outcome"]))
            continue
        d = day_state.setdefault(r["day"], {"entries": 0, "stops": 0, "pnl": 0.0})
        why = None
        if d["entries"] >= cfg["max_trades_per_day"]:
            why = "max %d filled entries a day" % cfg["max_trades_per_day"]
        elif d["stops"] >= cfg["max_stop_exits_per_day"]:
            why = "max %d stop exits a day" % cfg["max_stop_exits_per_day"]
        elif d["pnl"] <= -cfg["max_daily_loss_inr"]:
            why = "daily net loss limit Rs %d reached" % cfg["max_daily_loss_inr"]
        elif d["pnl"] >= cfg["daily_profit_stop_inr"]:
            why = "daily profit stop Rs %d reached" % cfg["daily_profit_stop_inr"]
        elif open_until and r["entry_time"] < open_until:
            why = "one lot already open"
        elif risk_inr > cfg["max_risk_per_trade_inr"]:
            why = "planned risk above Rs %d" % cfg["max_risk_per_trade_inr"]
        if why:
            rejected.append(dict(r, rejected_because=why))
            continue
        d["entries"] += 1
        d["stops"] += 1 if r["outcome"] == "SL" else 0
        d["pnl"] += r["r_multiple"] * risk_inr
        open_until = r.get("exit_time")
        taken.append(r)
    model = index_model(taken, cfg, risk_inr, label="EXECUTABLE PORTFOLIO (index model, approved limits applied)")
    reasons = collections.Counter(x["rejected_because"] for x in rejected)
    return {"model": model, "signals": len(rows), "filled": len(taken), "rejected": len(rejected),
            "rejected_by_reason": dict(reasons),
            "limits": {k: cfg[k] for k in ("max_trades_per_day", "max_stop_exits_per_day", "max_daily_loss_inr",
                                           "daily_profit_stop_inr", "max_risk_per_trade_inr", "max_open_lots",
                                           "capital_inr")},
            "rejected_rows": [{k: x.get(k) for k in ("day", "signal_time", "outcome", "rejected_because")}
                              for x in rejected]}


CSV_COLUMNS = ("day", "signal_time", "option_type", "expiry", "entry_time", "entry", "stop", "target", "risk_points",
               "outcome", "time_exit_result", "exit_time", "exit", "index_points", "r_multiple", "minutes_held",
               "reason", "portfolio", "hypothetical_inr")


def trades_csv(rows, portfolio_doc, risk_inr):
    """Trade-by-trade audit CSV text (index model; never option prices)."""
    import csv
    import io
    rej = {(x["day"], x["signal_time"]): x["rejected_because"] for x in portfolio_doc.get("rejected_rows") or []}
    buf = io.StringIO()
    buf.write("# INDEX-BASED MODEL, NOT OPTION P&L. 1R = Rs %d, before brokerage, taxes and slippage.\n" % risk_inr)
    w = csv.DictWriter(buf, CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in sorted(rows, key=lambda x: x.get("entry_time") or x["day"]):
        why = rej.get((r["day"], r["signal_time"]))
        out = dict(r, portfolio="rejected: " + why if why else "filled",
                   hypothetical_inr=None if r.get("r_multiple") is None else round(r["r_multiple"] * risk_inr, 2))
        w.writerow(out)
    return buf.getvalue()


def daily_limit_check(rows, cfg, risk_inr=None):
    """Would the approved daily limits have blocked any ORB trade? (index model: a stop costs 1R = Rs 500)."""
    risk_inr = float(cfg["max_risk_per_trade_inr"] if risk_inr is None else risk_inr)
    per_day = collections.Counter(r["day"] for r in rows)
    worst_day = min((r.get("r_multiple") or 0.0 for r in rows), default=0.0) * risk_inr
    return {"max_signals_on_one_day": max(per_day.values()) if per_day else 0,
            "max_trades_per_day": cfg["max_trades_per_day"],
            "max_stop_exits_per_day": cfg["max_stop_exits_per_day"],
            "max_daily_loss_inr": cfg["max_daily_loss_inr"], "daily_profit_stop_inr": cfg["daily_profit_stop_inr"],
            "worst_single_day_inr_index_model": round(worst_day, 2),
            "blocked_by_daily_limits": 0 if (not per_day or max(per_day.values()) <= 1) else None,
            "why": ("ORB allows max_signals_per_day = 1, so each day has at most one entry and at most one stop "
                    "(limits 5 entries and 2 stops); one stop costs at most Rs %d, below the Rs %d daily loss "
                    "limit; a target day (+2.5R = Rs %d) stays under the Rs %d profit stop, which only blocks "
                    "NEW entries anyway." % (risk_inr, cfg["max_daily_loss_inr"], 2.5 * risk_inr,
                                              cfg["daily_profit_stop_inr"]))}


def per_trade_risk_fit(cfg, costs, contract, day, stop_pct):
    """The highest option premium at which ONE lot fits the Rs 500 risk rule with the live sizing
    (budget.size_at_fills: 20% premium stop, slippage, both legs' charges). Historical premiums are not
    available, so this gives the threshold, not a per-trade answer."""
    from .. import budget
    lo, hi, best = 0.05, 2000.0, None
    for _ in range(40):
        mid = (lo + hi) / 2
        stop = round(mid * (1 - stop_pct / 100.0), 2)
        try:
            lots = budget.size_at_fills(cfg, costs, contract, mid, stop, day, cfg["max_daily_loss_inr"])["lots"]
        except Exception:                                   # noqa: BLE001
            lots = 0
        if lots >= 1:
            best, lo = mid, mid
        else:
            hi = mid
    return {"lot_size": contract.lot_size, "contract": contract.symbol if hasattr(contract, "symbol") else None,
            "max_premium_for_one_lot": round(best, 2) if best else None, "stop_pct": stop_pct,
            "note": "A signal whose ATM premium was above this could not be taken at all under the Rs 500 rule; "
                    "historical premiums are unavailable, so the number of such signals is unknown."}


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


def charts(u1, signals, minutes=5, native_minutes=1):
    """5-minute OHLC of each signal day (for the dashboard's charts): real index bars only."""
    days = _sessions(u1)
    out = {}
    for d in sorted({s["day"] for s in signals}):
        day_bars = days.get(dt.date.fromisoformat(d), [])
        if not day_bars:
            continue
        bars = st.resample(day_bars, minutes) if native_minutes == 1 else day_bars
        out[d] = [[_iso(b[0]), b[1], b[2], b[3], b[4]] for b in bars]
    return out
