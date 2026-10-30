"""Technical patterns on underlying index bars. Pure functions, no look-ahead.

A bar is (start, open, high, low, close) with `start` an aware IST datetime.
A bar is only usable once it has closed: start + interval <= now. Every detector
receives the bars of ONE session that have already closed and looks only at
them, so the same code drives the live scanner and the backtester.

Patterns (choose and tune in a strategy spec):
  orb       first close above the opening-range high (-> CE) or below its low (-> PE)
  pdh_pdl   first close above the previous session's high (-> CE) or below its low (-> PE)
"""

import datetime as dt

from .errors import DataError

PATTERNS = ("orb", "pdh_pdl")
INSTRUMENTS = ("index_option", "equity")


def closed_bars(bars, now, minutes):
    step = dt.timedelta(minutes=minutes)
    return [b for b in bars if b[0] + step <= now]


def validate_bars(bars, minutes):
    """Raise DataError on unordered, duplicated or impossible bars."""
    prev = None
    for b in bars:
        if len(b) < 5:
            raise DataError("bar %r has fewer than 5 fields" % (b,))
        t, o, h, l, c = b[:5]
        if None in (o, h, l, c):
            raise DataError("bar at %s has a missing price" % t)
        if not (l <= min(o, c) and h >= max(o, c) and l > 0):
            raise DataError("bar at %s is inconsistent: o=%s h=%s l=%s c=%s" % (t, o, h, l, c))
        if prev is not None and t <= prev:
            raise DataError("bars out of order or duplicated at %s" % t)
        prev = t
    return bars


def opening_range(bars, or_minutes, minutes):
    n = or_minutes // minutes
    if n < 1 or len(bars) < n:
        return None
    first = bars[:n]
    return max(b[2] for b in first), min(b[3] for b in first), n


def detect_orb(bars, params, minutes, start=0):
    """First breakout of the opening range among the closed bars, or None."""
    rng = opening_range(bars, params["or_minutes"], minutes)
    if rng is None:
        return None
    hi, lo, n = rng
    buf = params.get("buffer_pct", 0.0) / 100.0
    for i in range(max(n, start), len(bars)):
        c = bars[i][4]
        if c > hi * (1 + buf):
            return _signal("orb", "CE", bars[i], minutes, {"or_high": hi, "or_low": lo},
                           invalidate_below=(hi + lo) / 2.0, index=i)
        if c < lo * (1 - buf):
            return _signal("orb", "PE", bars[i], minutes, {"or_high": hi, "or_low": lo},
                           invalidate_above=(hi + lo) / 2.0, index=i)
    return None


def detect_pdh_pdl(bars, params, minutes, prev_high, prev_low, start=0):
    if prev_high is None or prev_low is None:
        return None
    buf = params.get("buffer_pct", 0.0) / 100.0
    skip = params.get("skip_first_minutes", 0) // minutes
    for i in range(max(skip, start), len(bars)):
        c = bars[i][4]
        if c > prev_high * (1 + buf):
            return _signal("pdh_pdl", "CE", bars[i], minutes, {"prev_high": prev_high, "prev_low": prev_low},
                           invalidate_below=prev_high, index=i)
        if c < prev_low * (1 - buf):
            return _signal("pdh_pdl", "PE", bars[i], minutes, {"prev_high": prev_high, "prev_low": prev_low},
                           invalidate_above=prev_low, index=i)
    return None


def _signal(name, side, bar, minutes, levels, index, invalidate_below=None, invalidate_above=None):
    return {
        "pattern": name,
        "option_type": side,
        "bar_start": bar[0],
        "known_at": bar[0] + dt.timedelta(minutes=minutes),
        "trigger_close": bar[4],
        "levels": levels,
        "invalidate_below": invalidate_below,
        "invalidate_above": invalidate_above,
        "bar_index": index,
    }


def detect(strategy, bars, minutes, prev_high=None, prev_low=None, start=0):
    """First signal at or after bar index `start` among closed bars."""
    p = strategy["pattern"]
    if p == "orb":
        return detect_orb(bars, strategy["params"], minutes, start)
    if p == "pdh_pdl":
        return detect_pdh_pdl(bars, strategy["params"], minutes, prev_high, prev_low, start)
    raise DataError("unknown pattern %r (known: %s)" % (p, ", ".join(PATTERNS)))


def validate_strategy(s):
    need = ("name", "instrument", "pattern", "params", "interval_minutes", "strike_offset",
            "stop_pct", "target_r")
    miss = [k for k in need if k not in s]
    if miss:
        raise DataError("strategy is missing %s" % ", ".join(miss))
    if s["instrument"] not in INSTRUMENTS:
        raise DataError("strategy instrument must be one of %s" % ", ".join(INSTRUMENTS))
    if s["pattern"] not in PATTERNS:
        raise DataError("unknown pattern %r" % s["pattern"])
    if s["interval_minutes"] not in (1, 3, 5, 15, 30):
        raise DataError("interval_minutes must be one of 1, 3, 5, 15, 30")
    if not 0 < s["stop_pct"] < 100:
        raise DataError("stop_pct must be between 0 and 100")
    if s["target_r"] <= 0:
        raise DataError("target_r must be positive")
    if s["pattern"] == "orb" and s["params"].get("or_minutes", 0) < s["interval_minutes"]:
        raise DataError("orb needs params.or_minutes >= interval_minutes")
    return s


def raw_occurrences(strategy, bars, minutes, prev_high=None, prev_low=None):
    """Every closed bar where the pattern condition holds (overlapping; for reporting only)."""
    n, i = 0, 0
    while True:
        sig = detect(strategy, bars, minutes, prev_high, prev_low, start=i)
        if sig is None:
            return n
        n += 1
        i = sig["bar_index"] + 1
