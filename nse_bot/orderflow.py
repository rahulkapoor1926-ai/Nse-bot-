"""Order-flow CONFIRMATION for ORB and sweep_reversal entries (never an entry signal on its own).

What FYERS actually provides was measured live on 2026-11-02 (scripts/probe_orderflow.py, GitHub run
41417570845): REST /depth returns 5 bid and 5 ask levels (price, quantity, order count), the whole-book
totalbuyqty / totalsellqty, the last trade's price, size and time, and cumulative day volume. It does
NOT say whether any trade hit the bid or lifted the offer, and between two snapshots 3 s apart
hundreds of trades are merged into one volume number. The NIFTY index itself has no depth or volume.

So, honestly:
  AVAILABLE      bid/ask volume imbalance (resting orders, 5 levels and whole book),
                 volume exhaustion (real traded volume of 1-minute NIFTY futures candles).
  NOT AVAILABLE  candle buy/sell volume, true bid-ask delta, CVD, price-delta divergence and
                 absorption: all need the aggressor side of each trade. Inferring it from candle
                 volume or price direction would be a fabricated delta, so this module never does.
                 (FYERS also offers WebSocket and tick-by-tick feeds; they have not been tested here.)

Reporting is per metric; the confirmation uses only AVAILABLE metrics. A metric whose inputs are
missing or stale reports NOT AVAILABLE, and when confirmation is required that blocks the entry.
"""

from . import clock

AVAILABLE, NOT_AVAILABLE = "AVAILABLE", "NOT AVAILABLE"
NEEDS_AGGRESSOR = ("FYERS REST gives no trade side (hit bid / lifted offer) and merges many trades per "
                   "snapshot; computing this from candle volume would be fake")
METRICS = {
    "candle_buy_sell_volume": (NOT_AVAILABLE, NEEDS_AGGRESSOR),
    "bid_ask_delta": (NOT_AVAILABLE, NEEDS_AGGRESSOR),
    "cvd": (NOT_AVAILABLE, "needs a true per-trade delta (see bid_ask_delta)"),
    "price_delta_divergence": (NOT_AVAILABLE, "needs a true delta / CVD"),
    "absorption": (NOT_AVAILABLE, "needs aggressive volume traded at a price that holds; "
                                  "trade side is not available"),
    "bid_ask_imbalance": (AVAILABLE, "FYERS /depth: 5 levels and whole-book total buy/sell quantity"),
    "volume_exhaustion": (AVAILABLE, "1-minute NIFTY futures candles: real traded volume"),
}

DEFAULTS = {"min_book_imbalance": 0.10,   # 5-level (bid - ask) / (bid + ask) in the trade's direction
            "max_total_against": 0.25,    # whole-book imbalance against the trade that vetoes it
            "exhaustion_recent_bars": 3, "exhaustion_lookback_bars": 20,
            "exhaustion_volume_ratio": 0.6}


def book_imbalance(q, now, max_age_seconds):
    """From a FYERS depth quote. Returns {"status", "five_level", "total", ...}."""
    d = (q.extra or {}).get("depth") if q is not None else None
    if not d:
        return {"status": NOT_AVAILABLE, "reason": "no market depth in the quote"}
    if q.ts is None:
        return {"status": NOT_AVAILABLE, "reason": "depth has no last-trade time"}
    from .quotes import _reference
    age = (_reference(q, now) - q.ts).total_seconds()
    if age > max_age_seconds:
        return {"status": NOT_AVAILABLE, "reason": "depth is stale: %.0fs old" % age}
    b = sum(x[1] for x in d["bids"] if x[0] and x[1])
    a = sum(x[1] for x in d["asks"] if x[0] and x[1])
    if b <= 0 or a <= 0:
        return {"status": NOT_AVAILABLE, "reason": "empty bid or ask side in the depth"}
    tb, ts = d.get("total_buy_qty"), d.get("total_sell_qty")
    total = (tb - ts) / float(tb + ts) if tb and ts else None
    return {"status": AVAILABLE, "bid_qty_5": b, "ask_qty_5": a, "five_level": round((b - a) / float(b + a), 3),
            "total_buy_qty": tb, "total_sell_qty": ts, "total": None if total is None else round(total, 3),
            "age_seconds": round(age, 1)}


def volume_exhaustion(bars, direction, p=None):
    """bars: closed 1-minute futures candles [ts, o, h, l, c, v]. direction: +1 up, -1 down.
    Exhausted = price still pushed to a new extreme in `direction` over the last few bars while their
    average volume fell below `exhaustion_volume_ratio` of the preceding bars' average."""
    p = dict(DEFAULTS, **(p or {}))
    n, look = p["exhaustion_recent_bars"], p["exhaustion_lookback_bars"]
    if len(bars) < n + look:
        return {"status": NOT_AVAILABLE, "reason": "%d futures bars, need %d" % (len(bars), n + look)}
    recent, before = bars[-n:], bars[-(n + look):-n]
    if any(len(b) < 6 or b[5] is None for b in recent + before):
        return {"status": NOT_AVAILABLE, "reason": "futures candles have no volume"}
    base = sum(b[5] for b in before) / float(look)
    if base <= 0:
        return {"status": NOT_AVAILABLE, "reason": "no futures volume in the lookback"}
    ratio = sum(b[5] for b in recent) / float(n) / base
    if direction > 0:
        pushing = max(b[2] for b in recent) > max(b[2] for b in before)
    else:
        pushing = min(b[3] for b in recent) < min(b[3] for b in before)
    return {"status": AVAILABLE, "volume_ratio": round(ratio, 3), "new_extreme": pushing,
            "exhausted": bool(pushing and ratio < p["exhaustion_volume_ratio"])}


def confirm(setup, direction, fut_quote, fut_bars, now, cfg, p=None):
    """setup: "orb" (breakout continuation) or "sweep" (reversal). direction: +1 CE / -1 PE.
    Returns {"status": CONFIRMED | NOT CONFIRMED | NOT AVAILABLE, "reasons", "metrics"}."""
    p = dict(DEFAULTS, **(p or {}))
    book = book_imbalance(fut_quote, now, cfg["max_quote_age_seconds"])
    ex = volume_exhaustion(fut_bars, direction if setup == "orb" else -direction, p)
    metrics = {k: {"status": v[0], "note": v[1]} for k, v in METRICS.items()}
    metrics["bid_ask_imbalance"].update(book)
    metrics["volume_exhaustion"].update(ex)
    out = {"setup": setup, "direction": "up" if direction > 0 else "down", "metrics": metrics, "reasons": []}
    r = out["reasons"]
    if book["status"] != AVAILABLE:
        r.append("bid/ask imbalance NOT AVAILABLE: %s" % book["reason"])
    if ex["status"] != AVAILABLE:
        r.append("volume exhaustion NOT AVAILABLE: %s" % ex["reason"])
    if r:
        out["status"] = NOT_AVAILABLE
        return out
    with_trade = book["five_level"] * direction
    if with_trade < p["min_book_imbalance"]:
        r.append("futures book imbalance %+.2f does not favour the %s (need %+.2f)" % (
            book["five_level"], out["direction"], p["min_book_imbalance"] * direction))
    if book["total"] is not None and book["total"] * direction < -p["max_total_against"]:
        r.append("whole-book imbalance %+.2f is against the trade" % book["total"])
    if setup == "orb" and ex["exhausted"]:
        r.append("breakout volume exhausted: new %s on %.0f%% of the usual futures volume" % (
            "high" if direction > 0 else "low", ex["volume_ratio"] * 100))
    if setup == "sweep" and ex["exhausted"]:
        out["note"] = "the swept move ran out of volume (supports the reversal)"
    out["status"] = "NOT CONFIRMED" if r else "CONFIRMED"
    return out


def for_entry(ctx, setup, underlying, option_type, now, fut_bars=None):
    """Fetch what confirm() needs for an index-option entry. Returns (result, problems for risk.check)."""
    if ctx.cfg.get("orderflow_confirmation", "off") != "required":
        return {"status": "OFF"}, []
    fut = ctx.master.current_future(underlying, now.date())
    if fut is None:
        res = {"status": NOT_AVAILABLE, "reasons": ["no current %s future in the symbol master" % underlying]}
        return res, ["order flow: " + res["reasons"][0]]
    try:
        q = ctx.md.quotes([fut.instrument_id]).get(fut.instrument_id)
        if fut_bars is None:
            fut_bars = ctx.md.candles(fut.instrument_id, "1m", clock.at(now.date(), "09:15"), now)
        fut_bars = [b for b in fut_bars if b[0] + _minute() <= now]          # closed bars only
    except Exception as e:                      # noqa: BLE001  (missing data is NOT AVAILABLE, never a pass)
        res = {"status": NOT_AVAILABLE, "reasons": ["futures data unavailable: %s" % e]}
        return res, ["order flow: " + res["reasons"][0]]
    res = confirm(setup, 1 if option_type == "CE" else -1, q, fut_bars, now, ctx.cfg)
    res["future"] = fut.symbol
    if res["status"] == "CONFIRMED":
        return res, []
    return res, ["order flow %s: %s" % (res["status"], "; ".join(res["reasons"]))]


def _minute():
    import datetime as dt
    return dt.timedelta(minutes=1)
