"""Event-driven backtest of one strategy over a Dataset, one lot per trade.

No look-ahead:
  * a pattern sees only bars that have closed (patterns.detect on bars[:i+1]);
  * entry uses the quote snapshot one full bar AFTER the signal is known
    (entry_delay_bars, minimum 1), at the ASK plus slippage ticks;
  * exits use later bars only, at the BID minus slippage ticks; a bar that
    touches both stop and target counts as the stop;
  * regime labels use the previous session and today's open only.

Independence: within a session a new signal is looked for only after the
previous trade's exit bar, and at most `max_signals_per_day` per underlying.
A signal whose trade cannot be simulated (missing quote, wide spread, no
listed contract) is counted as invalid and ends that session's scan, because
its trade window is unknown.

Counts reported: candles scanned, raw (overlapping) pattern occurrences,
independent examples, valid examples and executed trades with a resolved exit.
"""

import datetime as dt
import hashlib
from collections import Counter

from .. import clock, patterns, rr
from ..errors import CalendarError, DataError
from ..instruments import Contract
from ..exit import plan as exit_plan


def split_days(days, train=0.6, validation=0.2):
    """Chronological train / validation / test split of session dates."""
    days = sorted(set(days))
    n = len(days)
    a, b = int(n * train), int(n * (train + validation))
    return {"train": days[:a], "validation": days[a:b], "test": days[b:]}


def all_days(ds, underlyings):
    return sorted({d for u in underlyings if u in ds.underlying for d in ds.underlying[u]})


def coin_flip(u, day, k):
    h = hashlib.sha256(("%s|%s|%d" % (u, day, k)).encode()).digest()
    return "CE" if h[0] % 2 == 0 else "PE"


def _regime(ds, u, day, history):
    """Volatility regime from data known before the session; 'gap' from today's open."""
    prev = [d for d in history if d < day][-21:]
    if len(prev) < 21:
        vol = "unknown"
    else:
        def rng(d):
            b = ds.underlying[u][d]
            return (max(x[2] for x in b) - min(x[3] for x in b)) / b[-1][4]
        last = rng(prev[-1])
        trailing = sorted(rng(d) for d in prev[:-1])
        vol = "high_vol" if last > trailing[len(trailing) // 2] else "low_vol"
    gap = "no_gap"
    if prev:
        pc = ds.underlying[u][prev[-1]][-1][4]
        op = ds.underlying[u][day][0][1]
        if abs(op - pc) / pc > 0.005:
            gap = "gap"
    return "%s/%s" % (vol, gap)


def _contract(u, key, rec):
    expiry, strike, typ = key
    return Contract(instrument_id=0, symbol="%s %s %.0f %s" % (u, expiry, strike, typ), underlying=u,
                    expiry=expiry, strike=strike, option_type=typ, lot_size=rec["lot_size"],
                    tick_size=rec["tick_size"], freeze_quantity=0)


def _pick(chains, day, typ, spot, offset, min_dte, cal, u):
    expiries = sorted({k[0] for k in chains if k[0] >= day + dt.timedelta(days=min_dte)})
    for e in expiries:
        if not cal.check_expiry(u, e):
            continue
        strikes = sorted({k[1] for k in chains if k[0] == e})
        if not strikes:
            continue
        atm = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i]))
        i = atm + (offset if typ == "CE" else -offset)
        if 0 <= i < len(strikes) and (e, strikes[i], typ) in chains:
            return (e, strikes[i], typ)
        return None
    return None


def simulate_trade(ds, u, day, sig, typ, strategy, cfg, costs, cal, ubars):
    """Returns (trade dict or None, invalid reason or None, exit bar index in ubars or None)."""
    minutes = ds.interval
    delay = max(1, int(strategy.get("entry_delay_bars", 1)))
    chains = ds.options.get((u, day))
    if not chains:
        return None, "no option data for the session", None
    key = _pick(chains, day, typ, sig["trigger_close"], strategy["strike_offset"],
                cfg["min_days_to_expiry"], cal, u)
    if key is None:
        return None, "no listed contract at the chosen strike/expiry", None
    bars = chains[key]
    snap_time = sig["known_at"] + dt.timedelta(minutes=minutes * delay)
    entry_bar_start = snap_time - dt.timedelta(minutes=minutes)
    eb = bars.get(entry_bar_start)
    if eb is None:
        return None, "no option bar at entry time", None
    if snap_time > clock.at(day, cfg["no_entries_after"]):
        return None, "entry would fall after the entry window", None
    if eb["bid"] is None or eb["ask"] is None or eb["ask"] <= 0 or eb["bid"] <= 0:
        return None, "missing bid/ask at entry", None
    if eb["bid"] > eb["ask"]:
        return None, "crossed quote at entry", None
    mid = (eb["bid"] + eb["ask"]) / 2
    if (eb["ask"] - eb["bid"]) / mid * 100 > cfg["max_spread_pct"]:
        return None, "spread too wide at entry", None
    if eb["ask_qty"] is None or eb["ask_qty"] < eb["lot_size"]:
        return None, "displayed ask size below one lot", None
    c = _contract(u, key, eb)
    tick = c.tick_size
    slip = cfg["slippage_ticks"] * tick
    entry = round(eb["ask"] + slip, 2)
    p = exit_plan(entry, c, strategy, cfg, day)
    net = rr.net(entry, round(p["stop"] - slip, 2), round(p["target"] - slip, 2), c.lot_size, costs, day, c.cost_segment)
    if not rr.ok(net, cfg):
        return {"skipped": rr.why(net, cfg)}, None, None
    flat = clock.at(day, p["flat_by"])
    ucloses = {b[0]: (i, b[4]) for i, b in enumerate(ubars)}
    later = sorted(t for t in bars if t > entry_bar_start)
    exit_px = reason = exit_time = None
    exit_idx = None
    delayed = False
    for t in later:
        b = bars[t]
        close_t = t + dt.timedelta(minutes=minutes)
        ui = ucloses.get(t)
        if b["bid"] is None or b["bid"] <= 0:
            delayed = True
            continue
        bid = b["bid"]
        low = b["low"] if b["low"] is not None else bid
        if low <= p["stop"] or bid <= p["stop"]:
            gap_px = b["open"] if b["open"] is not None and b["open"] < p["stop"] else p["stop"]
            exit_px, reason = min(gap_px, bid, p["stop"]) - slip, "stop" if gap_px == p["stop"] else "stop_gap"
        elif bid >= p["target"]:
            exit_px, reason = bid - slip, "target"
        elif close_t >= flat:
            exit_px, reason = bid - slip, "time"
        elif ui is not None and sig.get("invalidate_below") is not None and ui[1] < sig["invalidate_below"]:
            exit_px, reason = bid - slip, "invalidated"
        elif ui is not None and sig.get("invalidate_above") is not None and ui[1] > sig["invalidate_above"]:
            exit_px, reason = bid - slip, "invalidated"
        if exit_px is not None:
            exit_time = close_t
            exit_idx = ui[0] if ui else None
            break
    if exit_px is None:
        return {"unresolved": True, "day": day.isoformat(), "underlying": u}, None, None
    exit_px = round(max(exit_px, tick), 2)
    qty = c.lot_size
    ch = costs.round_trip(entry, exit_px, qty, day)["total"]
    gross = (exit_px - entry) * qty
    if exit_idx is None:
        exit_idx = max((i for i, b in enumerate(ubars) if b[0] < exit_time), default=len(ubars) - 1)
    return {
        "unresolved": False, "day": day.isoformat(), "underlying": u, "contract": c.symbol,
        "option_type": typ, "signal_time": sig["known_at"].isoformat(), "entry_time": snap_time.isoformat(),
        "exit_time": exit_time.isoformat(), "entry": entry, "exit": exit_px, "stop": p["stop"],
        "target": p["target"], "qty": qty, "reason": reason, "gross": round(gross, 2), "charges": ch,
        "net": round(gross - ch, 2), "exit_delayed_by_missing_data": delayed,
        "entry_spread": round(eb["ask"] - eb["bid"], 2),
    }, None, exit_idx


def run(ds, strategy, cfg, costs, cal, days=None, direction=None):
    """direction: None = follow the signal; 'coin' = seeded coin-flip baseline."""
    patterns.validate_strategy(strategy)
    if strategy["interval_minutes"] != ds.interval:
        raise DataError("strategy interval %d != dataset interval %d" % (strategy["interval_minutes"], ds.interval))
    minutes = ds.interval
    per_day = int(strategy.get("max_signals_per_day", 1))
    counts = Counter()
    invalid = Counter()
    trades, unresolved = [], []
    for u in cfg["underlyings"]:
        if u not in ds.underlying:
            continue
        history = ds.days(u)
        for day in (d for d in history if days is None or d in days):
            try:
                if not cal.is_trading_day(day):
                    invalid["data on a non-trading day"] += 1
                    continue
            except CalendarError as e:
                raise DataError(str(e))
            ubars = ds.underlying[u][day]
            counts["candles"] += len(ubars)
            prev = [d for d in history if d < day]
            ph = pl = None
            if prev and prev[-1] == cal.previous_trading_day(day):
                pb = ds.underlying[u][prev[-1]]
                ph, pl = max(b[2] for b in pb), min(b[3] for b in pb)
            counts["raw_occurrences"] += patterns.raw_occurrences(strategy, ubars, minutes, ph, pl)
            regime = _regime(ds, u, day, history)
            start, k = 0, 0
            while k < per_day:
                sig = patterns.detect(strategy, ubars, minutes, ph, pl, start=start)
                if sig is None:
                    break
                if sig["known_at"] < clock.at(day, cfg["no_entries_before"]):
                    start = sig["bar_index"] + 1
                    continue
                if sig["known_at"] > clock.at(day, cfg["no_entries_after"]):
                    break
                counts["independent"] += 1
                typ = sig["option_type"] if direction is None else coin_flip(u, day, k)
                t, why, idx = simulate_trade(ds, u, day, sig, typ, strategy, cfg, costs, cal, ubars)
                k += 1
                if why:
                    invalid[why] += 1
                    break
                if t.get("skipped"):
                    counts["net_rr_no_trade"] += 1             # a valid NO TRADE: never counted as a gate example
                    start = sig["bar_index"] + 1
                    continue
                counts["valid"] += 1
                if t["unresolved"]:
                    unresolved.append(t)
                    break
                t["regime"] = regime
                trades.append(t)
                counts["executed"] += 1
                start = (idx if idx is not None else sig["bar_index"]) + 1
    return {"counts": dict(counts), "invalid": dict(invalid), "trades": trades,
            "unresolved": len(unresolved)}
