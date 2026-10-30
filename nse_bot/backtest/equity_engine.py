"""Backtest of a long-only stock/ETF strategy over an EquityDataset.

Same no-look-ahead rules as the option engine: patterns see closed bars only,
entry uses the quote one full bar after the signal is known, exits use later
bars, a bar touching stop and target counts as the stop.

Fills:
  fill_model "quotes"            buy at the recorded ask, sell at the recorded bid
  fill_model "estimated_spread"  buy at close x (1 + spread/2), sell at close x (1 - spread/2),
                                 rounded against us to the tick
plus slippage_ticks on every fill. Never the mid or the last trade.

Position size is what budget.size_shares allows with the full per-trade risk
budget, so charges (brokerage is a minimum of Rs 10 or 0.05%) weigh as they
would in paper trading. One example per symbol per session at most
`max_signals_per_day`; downside (short) signals are skipped because the
strategy is long only.

Baseline: the same symbol and day, entered at a seeded random bar inside the
entry window with the same stop and target rules; it tests whether the
signal's timing adds anything.
"""

import datetime as dt
import hashlib
import math
from collections import Counter

from .. import budget, clock, patterns
from ..errors import CalendarError, DataError
from ..exit import plan as exit_plan
from ..instruments import Stock
from .engine import split_days  # noqa: F401  (same chronological split for the gate)

BASELINE = "same symbol and day, random entry bar in the entry window (seeded), same stop/target"


def all_days(ds, _unused=None):
    return sorted({d for days in ds.bars.values() for d in days})


def _floor(x, tick):
    return round(math.floor(x / tick + 1e-9) * tick, 2)


def _ceil(x, tick):
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def _ask(ds, rec):
    if ds.fill_model == "quotes":
        return rec["ask"]
    return _ceil(rec["close"] * (1 + ds.spread_pct / 200.0), rec["tick_size"])


def _bid(ds, rec):
    if ds.fill_model == "quotes":
        return rec["bid"]
    return _floor(rec["close"] * (1 - ds.spread_pct / 200.0), rec["tick_size"])


def _random_signal(sym, day, bars, cfg, minutes):
    lo = clock.at(day, cfg["no_entries_before"])
    hi = clock.at(day, cfg["no_entries_after"])
    idx = [i for i, b in enumerate(bars) if lo <= b[0] + dt.timedelta(minutes=minutes) <= hi]
    if not idx:
        return None
    h = int(hashlib.sha256(("%s|%s" % (sym, day)).encode()).hexdigest(), 16)
    i = idx[h % len(idx)]
    b = bars[i]
    return {"pattern": "random", "option_type": "CE", "bar_start": b[0],
            "known_at": b[0] + dt.timedelta(minutes=minutes), "trigger_close": b[4], "levels": {},
            "invalidate_below": None, "invalidate_above": None, "bar_index": i}


def simulate_trade(ds, sym, day, sig, strategy, cfg, costs, recs):
    minutes = ds.interval
    delay = max(1, int(strategy.get("entry_delay_bars", 1)))
    snap_time = sig["known_at"] + dt.timedelta(minutes=minutes * delay)
    if snap_time > clock.at(day, cfg["no_entries_after"]):
        return None, "entry would fall after the entry window", None
    by_t = {r["ts"]: (i, r) for i, r in enumerate(recs)}
    hit = by_t.get(snap_time - dt.timedelta(minutes=minutes))
    if hit is None:
        return None, "no bar at entry time", None
    ei, eb = hit
    ask, bid = _ask(ds, eb), _bid(ds, eb)
    if ask is None or bid is None or ask <= 0 or bid <= 0:
        return None, "missing bid/ask at entry", None
    if bid > ask:
        return None, "crossed quote at entry", None
    if (ask - bid) / ((ask + bid) / 2) * 100 > cfg["max_spread_pct"]:
        return None, "spread too wide at entry", None
    if ask < cfg["equity_min_price_inr"]:
        return None, "price below equity_min_price_inr", None
    tick = eb["tick_size"]
    slip = cfg["slippage_ticks"] * tick
    entry = round(ask + slip, 2)
    st = Stock(0, sym, "", "EQ", tick, ds.kinds.get(sym, "equity"))
    p = exit_plan(entry, st, strategy, cfg, day)
    size = budget.size_shares(entry, p["stop"], st, cfg, costs, day, cfg["max_daily_loss_inr"])
    qty = size["quantity"]
    if qty < 1:
        return None, "sizing gives zero shares", None
    if ds.fill_model == "quotes" and (eb["ask_qty"] is None or eb["ask_qty"] < qty):
        return None, "displayed ask size below the order size", None
    flat = clock.at(day, p["flat_by"])
    exit_px = reason = exit_time = None
    exit_idx = None
    delayed = False
    for i in range(ei + 1, len(recs)):
        r = recs[i]
        close_t = r["ts"] + dt.timedelta(minutes=minutes)
        b = _bid(ds, r)
        if b is None or b <= 0:
            delayed = True
            continue
        if r["low"] <= p["stop"] or b <= p["stop"]:
            gap_px = r["open"] if r["open"] < p["stop"] else p["stop"]
            if ds.fill_model == "quotes":
                px = min(gap_px, p["stop"], b)
            else:
                px = _floor(min(gap_px, p["stop"]) * (1 - ds.spread_pct / 200.0), tick)
            exit_px, reason = px - slip, "stop" if gap_px == p["stop"] else "stop_gap"
        elif b >= p["target"]:
            exit_px, reason = b - slip, "target"
        elif close_t >= flat:
            exit_px, reason = b - slip, "time"
        elif sig.get("invalidate_below") is not None and r["close"] < sig["invalidate_below"]:
            exit_px, reason = b - slip, "invalidated"
        if exit_px is not None:
            exit_time, exit_idx = close_t, i
            break
    if exit_px is None:
        return {"unresolved": True, "day": day.isoformat(), "underlying": sym}, None, None
    exit_px = round(max(exit_px, tick), 2)
    ch = costs.round_trip(entry, exit_px, qty, day, st.cost_segment)["total"]
    gross = (exit_px - entry) * qty
    return {
        "unresolved": False, "day": day.isoformat(), "underlying": sym, "kind": st.kind,
        "contract": sym, "signal_time": sig["known_at"].isoformat(), "entry_time": snap_time.isoformat(),
        "exit_time": exit_time.isoformat(), "entry": entry, "exit": exit_px, "stop": p["stop"],
        "target": p["target"], "qty": qty, "reason": reason, "gross": round(gross, 2), "charges": ch,
        "net": round(gross - ch, 2), "exit_delayed_by_missing_data": delayed, "fill_model": ds.fill_model,
    }, None, exit_idx


def _regime(ds, sym, day, history):
    """high_vol / low_vol from the previous session's range vs the 20 before it (known before the open)."""
    prev = [d for d in history if d < day][-21:]
    if len(prev) < 21:
        return "unknown"

    def rng(d):
        b = ds.bars[sym][d]
        return (max(r["high"] for r in b) - min(r["low"] for r in b)) / b[-1]["close"]
    trailing = sorted(rng(d) for d in prev[:-1])
    return "high_vol" if rng(prev[-1]) > trailing[len(trailing) // 2] else "low_vol"


def run(ds, strategy, cfg, costs, cal, days=None, direction=None):
    """direction: None = follow the signal; 'coin' = random-entry-time baseline."""
    patterns.validate_strategy(strategy)
    if strategy["instrument"] != "equity":
        raise DataError("equity engine needs a strategy with instrument 'equity'")
    if strategy["interval_minutes"] != ds.interval:
        raise DataError("strategy interval %d != dataset interval %d" % (strategy["interval_minutes"], ds.interval))
    minutes = ds.interval
    per_day = int(strategy.get("max_signals_per_day", 1))
    counts, invalid = Counter(), Counter()
    trades, unresolved = [], 0
    for sym in ds.symbols():
        if ds.kinds.get(sym) not in cfg["instruments_enabled"]:
            continue
        history = sorted(ds.bars[sym])
        for day in (d for d in history if days is None or d in days):
            try:
                if not cal.is_trading_day(day):
                    invalid["data on a non-trading day"] += 1
                    continue
                pday = cal.previous_trading_day(day)
            except CalendarError as e:
                raise DataError(str(e))
            recs = ds.bars[sym][day]
            bars = [(r["ts"], r["open"], r["high"], r["low"], r["close"]) for r in recs]
            counts["candles"] += len(bars)
            ph = pl = None
            if pday in ds.bars[sym]:
                pb = ds.bars[sym][pday]
                ph, pl = max(r["high"] for r in pb), min(r["low"] for r in pb)
            counts["raw_occurrences"] += patterns.raw_occurrences(strategy, bars, minutes, ph, pl)
            start, k = 0, 0
            while k < per_day:
                sig = patterns.detect(strategy, bars, minutes, ph, pl, start=start)
                if sig is None:
                    break
                if sig["option_type"] != "CE" or sig["known_at"] < clock.at(day, cfg["no_entries_before"]):
                    start = sig["bar_index"] + 1
                    continue
                if sig["known_at"] > clock.at(day, cfg["no_entries_after"]):
                    break
                counts["independent"] += 1
                if direction == "coin":
                    sig = _random_signal(sym, day, bars, cfg, minutes) or sig
                t, why, idx = simulate_trade(ds, sym, day, sig, strategy, cfg, costs, recs)
                k += 1
                if why:
                    invalid[why] += 1
                    break
                counts["valid"] += 1
                if t["unresolved"]:
                    unresolved += 1
                    break
                t["regime"] = "%s/%s" % (t["kind"], _regime(ds, sym, day, history))
                trades.append(t)
                counts["executed"] += 1
                start = (idx if idx is not None else sig["bar_index"]) + 1
    return {"counts": dict(counts), "invalid": dict(invalid), "trades": trades, "unresolved": unresolved}
