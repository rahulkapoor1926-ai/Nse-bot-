"""Market-structure building blocks for the sweep_reversal strategy. Pure functions.

A bar is (start, open, high, low, close, volume) with `start` an aware IST
datetime; volume may be None (an index has no traded volume). Every function
looks only at the bars it is given; callers pass closed bars only, and a value
derived from bar i is "known" only when bar i (or the bar that confirms it)
has closed. Nothing here estimates a missing price or volume.
"""

import datetime as dt

from . import clock

SESSION_MINUTES = 375


def bar_end(b, minutes):
    return b[0] + dt.timedelta(minutes=minutes)


def closed(bars, now, minutes):
    return [b for b in bars if bar_end(b, minutes) <= now]


def resample(bars1, minutes):
    """1-minute bars -> `minutes` bars aligned to 09:15 IST, complete buckets only.

    A bucket is emitted only when every one of its minutes is present, so a gap
    in the 1m data never becomes a silently shortened bar. Volume is summed if
    every minute has volume, else None.
    """
    buckets = {}
    for b in bars1:
        day = b[0].date()
        k = int((b[0] - clock.at(day, "09:15")).total_seconds() // 60) // minutes
        buckets.setdefault((day, k), []).append(b)
    out = []
    for (day, k), bs in sorted(buckets.items()):
        bs.sort(key=lambda b: b[0])
        if len(bs) != minutes:
            continue
        vols = [b[5] if len(b) > 5 else None for b in bs]
        out.append((bs[0][0], bs[0][1], max(b[2] for b in bs), min(b[3] for b in bs), bs[-1][4],
                    None if any(v is None for v in vols) else sum(vols)))
    return out


def ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / float(n)
    out = [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    """Wilder's RSI of the last close, or None with fewer than n+1 closes."""
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def vwap(bars):
    """Session VWAP from bars with volume: sum(typical x volume) / sum(volume). None if any volume is missing."""
    num = den = 0.0
    for b in bars:
        v = b[5] if len(b) > 5 else None
        if v is None:
            return None
        num += (b[2] + b[3] + b[4]) / 3.0 * v
        den += v
    return num / den if den > 0 else None


def swings(bars, n, minutes):
    """Fractal swing highs/lows. Each is known only when bar i+n has closed."""
    out = []
    for i in range(n, len(bars) - n):
        h, l = bars[i][2], bars[i][3]
        around = bars[i - n:i] + bars[i + 1:i + n + 1]
        if all(h > b[2] for b in around):
            out.append({"kind": "high", "i": i, "t": bars[i][0], "price": h,
                        "known_at": bar_end(bars[i + n], minutes)})
        if all(l < b[3] for b in around):
            out.append({"kind": "low", "i": i, "t": bars[i][0], "price": l,
                        "known_at": bar_end(bars[i + n], minutes)})
    return out


def equal_levels(sw, kind, tol_pct):
    """Groups of 2+ swing highs (or lows) within tol_pct of each other -> one level each."""
    pts = sorted((s for s in sw if s["kind"] == kind), key=lambda s: s["price"])
    groups, cur = [], []
    for s in pts:
        if cur and abs(s["price"] - cur[0]["price"]) > cur[0]["price"] * tol_pct / 100.0:
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        price = max(s["price"] for s in g) if kind == "high" else min(s["price"] for s in g)
        out.append({"kind": "equal_" + kind + "s", "price": price, "members": [s["t"] for s in g],
                    "known_at": max(s["known_at"] for s in g)})
    return out


def find_sweep(bars, level, side, min_pen_pct, reclaim_bars, start=0):
    """First sweep of `level` in bars[start:].

    side "sell" (level below price, bullish case): a bar's low goes below the
    level by >= min_pen_pct, then a close back above the level within
    reclaim_bars bars (the sweep bar itself counts as bar 1). If closes stay
    beyond the level for longer, the break was accepted.
    Returns {"status": "sweep"|"accepted", ...} or None.
    """
    pen = level * min_pen_pct / 100.0
    for i in range(start, len(bars)):
        b = bars[i]
        hit = b[3] <= level - pen if side == "sell" else b[2] >= level + pen
        if not hit:
            continue
        extreme_i = i
        for j in range(i, min(len(bars), i + reclaim_bars)):
            bj = bars[j]
            if side == "sell" and bj[3] < bars[extreme_i][3]:
                extreme_i = j
            if side == "buy" and bj[2] > bars[extreme_i][2]:
                extreme_i = j
            back = bj[4] > level if side == "sell" else bj[4] < level
            if back:
                ext = bars[extreme_i][3] if side == "sell" else bars[extreme_i][2]
                return {"status": "sweep", "sweep_i": i, "reclaim_i": j, "extreme_i": extreme_i,
                        "extreme": ext, "level": level}
        if i + reclaim_bars <= len(bars):
            return {"status": "accepted", "sweep_i": i, "level": level}
        return None              # not enough closed bars yet to know
    return None


def confirmation(bars, sweep_i, reclaim_i, side, within, min_body_pct):
    """First bar after the reclaim (within `within` bars of it) that closes beyond the
    sweep bar's opposite extreme with a body >= min_body_pct of its range."""
    sb = bars[sweep_i]
    for k in range(reclaim_i, min(len(bars), reclaim_i + within + 1)):
        b = bars[k]
        rng = b[2] - b[3]
        if rng <= 0:
            continue
        body = abs(b[4] - b[1]) / rng * 100.0
        if side == "sell" and b[4] > sb[2] and b[4] > b[1] and body >= min_body_pct:
            return k
        if side == "buy" and b[4] < sb[3] and b[4] < b[1] and body >= min_body_pct:
            return k
    return None


def trend_from_swings(sw, before_i):
    """'up' (HH+HL), 'down' (LH+LL) or 'range' from the last two swing highs and lows before bar before_i."""
    hs = [s for s in sw if s["kind"] == "high" and s["i"] < before_i][-2:]
    ls = [s for s in sw if s["kind"] == "low" and s["i"] < before_i][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return "range"
    if hs[1]["price"] > hs[0]["price"] and ls[1]["price"] > ls[0]["price"]:
        return "up"
    if hs[1]["price"] < hs[0]["price"] and ls[1]["price"] < ls[0]["price"]:
        return "down"
    return "range"


def structure_break(bars, sw, from_i, to_i, side, minutes):
    """BOS or CHoCH in the trade direction between bars from_i..to_i (inclusive).

    Bullish (side "sell" swept): the first close above the most recent swing high
    that was already known when that bar closed. CHoCH if the prior structure was
    down, BOS if it was up or ranging.
    """
    for k in range(from_i, min(to_i, len(bars) - 1) + 1):
        t_close = bar_end(bars[k], minutes)
        kind = "high" if side == "sell" else "low"
        known = [s for s in sw if s["kind"] == kind and s["known_at"] <= bars[k][0] and s["i"] < k]
        if not known:
            continue
        ref = known[-1]
        crossed = bars[k][4] > ref["price"] if side == "sell" else bars[k][4] < ref["price"]
        if crossed:
            prior = trend_from_swings(sw, ref["i"] + 1)
            against = "down" if side == "sell" else "up"
            return {"type": "CHoCH" if prior == against else "BOS", "break_i": k, "swing": ref,
                    "prior_trend": prior, "known_at": t_close}
    return None


def fair_value_gaps(bars, lo_i, hi_i, side, min_pct):
    """Three-candle gaps fully inside bars lo_i..hi_i. Bullish: low[c3] > high[c1]."""
    out = []
    for k in range(max(lo_i, 0), min(hi_i, len(bars) - 1) - 1):
        a, c = bars[k], bars[k + 2]
        if side == "sell" and c[3] > a[2] and (c[3] - a[2]) >= a[2] * min_pct / 100.0:
            out.append({"i": k, "bottom": a[2], "top": c[3], "bars": [bars[k][0], bars[k + 1][0], bars[k + 2][0]]})
        if side == "buy" and c[2] < a[3] and (a[3] - c[2]) >= a[3] * min_pct / 100.0:
            out.append({"i": k, "bottom": c[2], "top": a[3], "bars": [bars[k][0], bars[k + 1][0], bars[k + 2][0]]})
    return out


def order_block(bars, lo_i, disp_i, side):
    """Last opposite-colour candle at or after lo_i and before the displacement bar."""
    for k in range(disp_i - 1, lo_i - 1, -1):
        b = bars[k]
        if (side == "sell" and b[4] < b[1]) or (side == "buy" and b[4] > b[1]):
            return {"i": k, "bottom": b[3], "top": b[2], "bar": b[0]}
    return None
