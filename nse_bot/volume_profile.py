"""Volume profile and auction-market context for ORB and sweep_reversal entries. CONTEXT ONLY:
nothing here blocks or creates a trade; the result is recorded with each candidate and decision.

Data reality (measured live 2026-11-02, GitHub run 41418924295, scripts/probe_profile.py):
FYERS has no volume-at-price or market-profile endpoint (none in the official SDK either), and
/history refuses second resolutions (5S/10S/15S/30S -> HTTP 422 "Invalid input"). The finest
volume data is the 1-minute candle of the NIFTY future (the index has no volume); a 1-minute
NIFTY future bar typically spans about 11 points.

So every volume profile here is APPROXIMATE and labelled so: each 1-minute futures bar's volume is
placed at that bar's typical price (high + low + close) / 3, in bins of `bin_points` (10 by default,
about one bar's range, so the profile claims no finer detail than the data has). No tick-level
distribution is invented inside a bar. Profiles are in NIFTY FUTURE prices and are compared only
with NIFTY future prices.

Initial Balance needs no volume: it is the exact high/low of the first 60 minutes of the NIFTY
index (1-minute candles), so it is not approximate.
"""

import datetime as dt

from . import clock

LABEL = ("APPROXIMATE: built from 1-minute NIFTY futures candles (each bar's volume at its typical price); "
         "FYERS provides no volume-at-price data")
DEFAULTS = {"bin_points": 10.0, "value_area_pct": 70.0, "hvn_ratio": 1.5, "lvn_ratio": 0.5,
            "acceptance_bars": 15, "rejection_lookback_bars": 30, "ib_minutes": 60, "near_pct": 0.15}


# ---- profile building ---------------------------------------------------------------
def build(bars, bin_points=10.0):
    """bars: [(ts, o, h, l, c, volume, ...)] closed 1-minute futures bars."""
    bins, used = {}, 0
    for b in bars:
        if len(b) < 6 or b[5] is None or b[5] <= 0:
            continue
        tp = (b[2] + b[3] + b[4]) / 3.0
        k = round(round(tp / bin_points) * bin_points, 2)
        bins[k] = bins.get(k, 0) + b[5]
        used += 1
    return {"bins": dict(sorted(bins.items())), "bars": used, "bin_points": bin_points,
            "total_volume": sum(bins.values())}


def poc(prof):
    bins = prof["bins"]
    if not bins:
        return None
    top = max(bins.values())
    prices = [p for p, v in bins.items() if v == top]
    mid = (min(bins) + max(bins)) / 2.0
    return min(prices, key=lambda p: (abs(p - mid), p))       # ties: nearest the middle of the range


def value_area(prof, pct=70.0):
    """Expand from the POC one bin at a time towards the side with more volume until `pct` of the
    session's volume is inside (ties go up)."""
    bins = prof["bins"]
    if not bins:
        return None, None
    prices = sorted(bins)
    i = prices.index(poc(prof))
    lo = hi = i
    have, need = bins[prices[i]], prof["total_volume"] * pct / 100.0
    while have < need and (lo > 0 or hi < len(prices) - 1):
        up = bins[prices[hi + 1]] if hi < len(prices) - 1 else -1
        down = bins[prices[lo - 1]] if lo > 0 else -1
        if up >= down:
            hi += 1
            have += up
        else:
            lo -= 1
            have += down
    return prices[lo], prices[hi]


def nodes(prof, hvn_ratio=1.5, lvn_ratio=0.5):
    """High/low volume nodes: local peaks/troughs of the 3-bin smoothed profile versus its mean bin."""
    prices = sorted(prof["bins"])
    if len(prices) < 5:
        return [], []
    v = [prof["bins"][p] for p in prices]
    s = [(v[max(0, i - 1)] + v[i] + v[min(len(v) - 1, i + 1)]) / 3.0 for i in range(len(v))]
    mean = sum(v) / float(len(v))
    hvn = [prices[i] for i in range(1, len(s) - 1) if s[i] >= s[i - 1] and s[i] >= s[i + 1] and s[i] >= hvn_ratio * mean]
    lvn = [prices[i] for i in range(1, len(s) - 1) if s[i] <= s[i - 1] and s[i] <= s[i + 1] and s[i] <= lvn_ratio * mean]
    return hvn, lvn


def levels(bars, p=None):
    p = dict(DEFAULTS, **(p or {}))
    prof = build(bars, p["bin_points"])
    if not prof["bins"]:
        return {"status": "NOT AVAILABLE", "reason": "no futures candles with volume"}
    val, vah = value_area(prof, p["value_area_pct"])
    hvn, lvn = nodes(prof, p["hvn_ratio"], p["lvn_ratio"])
    return {"status": "APPROXIMATE", "label": LABEL, "poc": poc(prof), "vah": vah, "val": val, "hvn": hvn,
            "lvn": lvn, "high": max(b[2] for b in bars), "low": min(b[3] for b in bars),
            "bars": prof["bars"], "bin_points": p["bin_points"]}


# ---- auction context --------------------------------------------------------------------
def migration(today, prev):
    """Where today's developing value sits relative to the previous session's value area."""
    if today.get("status") != "APPROXIMATE" or prev.get("status") != "APPROXIMATE":
        return "NOT AVAILABLE"
    if today["val"] > prev["vah"]:
        return "higher (value above yesterday's value)"
    if today["vah"] < prev["val"]:
        return "lower (value below yesterday's value)"
    if today["poc"] > prev["poc"]:
        return "overlapping, migrating higher"
    if today["poc"] < prev["poc"]:
        return "overlapping, migrating lower"
    return "overlapping, unchanged"


def acceptance(bars, ref, p=None):
    """Acceptance / rejection of the previous value area by today's closed 1-minute futures closes.
    accepted: the last `acceptance_bars` closes are all beyond VAH (or VAL);
    rejected: price traded beyond it within `rejection_lookback_bars` but now closes back inside."""
    p = dict(DEFAULTS, **(p or {}))
    if ref.get("status") != "APPROXIMATE" or not bars:
        return {"state": "NOT AVAILABLE"}
    n, look = p["acceptance_bars"], p["rejection_lookback_bars"]
    closes = [b[4] for b in bars]
    vah, val, last = ref["vah"], ref["val"], closes[-1]
    recent = bars[-look:]
    if len(closes) >= n and all(c > vah for c in closes[-n:]):
        return {"state": "accepted above value", "level": vah}
    if len(closes) >= n and all(c < val for c in closes[-n:]):
        return {"state": "accepted below value", "level": val}
    if val <= last <= vah and any(b[2] > vah for b in recent):
        return {"state": "rejected above value (back inside)", "level": vah}
    if val <= last <= vah and any(b[3] < val for b in recent):
        return {"state": "rejected below value (back inside)", "level": val}
    where = "above value" if last > vah else "below value" if last < val else "inside value"
    return {"state": "%s, not yet accepted" % where if where != "inside value" else "inside value", "level": None}


def initial_balance(index_bars, day, now, minutes=60):
    """Exact first-hour high/low of the NIFTY index (price only, no volume needed)."""
    start = clock.at(day, "09:15")
    end = start + dt.timedelta(minutes=minutes)
    first = [b for b in index_bars if start <= b[0] < end]
    if now < end or len(first) < minutes * 0.9:
        return {"status": "FORMING" if now < end else "NOT AVAILABLE",
                "high": max((b[2] for b in first), default=None), "low": min((b[3] for b in first), default=None)}
    hi, lo = max(b[2] for b in first), min(b[3] for b in first)
    after = [b for b in index_bars if b[0] >= end]
    return {"status": "EXACT", "high": hi, "low": lo, "range": round(hi - lo, 2),
            "broke_high": any(b[4] > hi for b in after), "broke_low": any(b[4] < lo for b in after)}


def sessions(bars):
    out = {}
    for b in bars:
        out.setdefault(b[0].date(), []).append(b)
    return out


def context(setup, direction, fut_bars, index_bars, index_price, now, p=None):
    """setup "orb" / "sweep", direction +1 (CE) / -1 (PE). fut_bars: closed 1-minute NIFTY future bars
    covering this and earlier sessions. Returns levels and supportive / caution notes. Never a filter."""
    p = dict(DEFAULTS, **(p or {}))
    day = now.date()
    by_day = sessions([b for b in fut_bars if b[0] + dt.timedelta(minutes=1) <= now])
    past = sorted(d for d in by_day if d < day)
    today = by_day.get(day, [])
    prev = levels(by_day[past[-1]], p) if past else {"status": "NOT AVAILABLE", "reason": "no previous session"}
    week_start = day - dt.timedelta(days=day.weekday())
    last_week = [d for d in past if week_start - dt.timedelta(days=7) <= d < week_start]
    weekly = levels([b for d in last_week for b in by_day[d]], p) if last_week else \
        {"status": "NOT AVAILABLE", "reason": "no previous-week sessions in the data"}
    dev = levels(today, p) if today else {"status": "NOT AVAILABLE", "reason": "no futures bars today"}
    ib = initial_balance(index_bars, day, now, p["ib_minutes"])
    acc = acceptance(today, prev, p)
    out = {"label": LABEL, "setup": setup, "direction": "up" if direction > 0 else "down",
           "session": dev, "previous_session": prev, "previous_week": weekly,
           "value_migration": migration(dev, prev), "acceptance": acc, "initial_balance": ib,
           "supports": [], "cautions": []}
    sup, cau = out["supports"], out["cautions"]
    up = direction > 0
    state = acc.get("state", "")
    if setup == "orb":
        if state.startswith("accepted above") and up or state.startswith("accepted below") and not up:
            sup.append("price accepted outside yesterday's value in the trade's direction")
        if state == "inside value":
            cau.append("breakout is still inside yesterday's value area")
        if state.startswith("rejected above") and up or state.startswith("rejected below") and not up:
            cau.append("the move beyond yesterday's value was rejected")
        if ib.get("status") == "EXACT":
            if (up and ib["broke_high"]) or (not up and ib["broke_low"]):
                sup.append("initial balance broken in the trade's direction")
            elif (up and ib["broke_low"]) or (not up and ib["broke_high"]):
                cau.append("initial balance broken against the trade")
    else:
        if state.startswith("rejected below") and up or state.startswith("rejected above") and not up:
            sup.append("the sweep was rejected back into yesterday's value (responsive reversal)")
        if state.startswith("accepted below") and up or state.startswith("accepted above") and not up:
            cau.append("price is accepted beyond value against the reversal")
    mig = out["value_migration"]
    if ("higher" in mig and up) or ("lower" in mig and not up):
        sup.append("value is migrating in the trade's direction")
    elif ("higher" in mig and not up) or ("lower" in mig and up):
        cau.append("value is migrating against the trade")
    fut_last = today[-1][4] if today else None
    if fut_last is not None and dev.get("status") == "APPROXIMATE":
        near = fut_last * p["near_pct"] / 100.0
        ahead = [x for x in dev["hvn"] + ([prev["poc"]] if prev.get("status") == "APPROXIMATE" else [])
                 if 0 < (x - fut_last) * direction <= near]
        if ahead:
            cau.append("high-volume level just ahead at %s (future price)" % ", ".join("%.0f" % x for x in ahead))
        thin = [x for x in dev["lvn"] if 0 < (x - fut_last) * direction <= near]
        if thin:
            sup.append("low-volume area just ahead at %s: price tends to move through it quickly"
                       % ", ".join("%.0f" % x for x in thin))
    out["future_price"] = fut_last
    out["index_price"] = index_price
    out["verdict"] = "SUPPORTIVE" if len(sup) > len(cau) else "CAUTION" if len(cau) > len(sup) else "NEUTRAL"
    out["note"] = "context only: recorded with the decision, never blocks or forces a trade"
    return out


def for_entry(ctx, setup, underlying, option_type, now, index_price=None):
    """Fetch inputs and build context. Any failure gives NOT AVAILABLE; it never affects the entry."""
    try:
        fut = ctx.master.current_future(underlying, now.date())
        if fut is None:
            return {"verdict": "NOT AVAILABLE", "reason": "no current %s future in the symbol master" % underlying}
        day0 = clock.at(now.date() - dt.timedelta(days=14), "09:15")
        fbars = ctx.md.candles(fut.instrument_id, "1m", day0, now)
        iid = ctx.master.index_id(ctx.cfg["index_names"][underlying])
        ibars = ctx.md.candles(iid, "1m", clock.at(now.date(), "09:15"), now)
        res = context(setup, 1 if option_type == "CE" else -1, fbars, ibars, index_price, now)
        res["future"] = fut.symbol
        return res
    except Exception as e:                      # noqa: BLE001  (context only)
        return {"verdict": "NOT AVAILABLE", "reason": "%s: %s" % (type(e).__name__, e)}
