"""Market regime of a session: trending or sideways, high or normal volatility.

One definition, used by the live bot, the forward report and every backtest, computed ONLY from
sessions that closed before the trade day (no look-ahead, identical live and in a replay):

  trend       efficiency ratio of the last TREND_SESSIONS + 1 daily closes:
              |last close - first close| / sum of |close-to-close moves|.
              >= TRENDING_ER -> "trending" (with its direction), else "sideways".
  volatility  the previous session's high-low range (as % of its close) ranked against the
              VOL_SESSIONS sessions before it. In the top (1 - HIGH_VOL_RANK) of them ->
              "high_volatility", else "normal_volatility".

Too few sessions -> "unknown", never a guess. The label is recorded for every signal and trade
and used to report results per regime. It does not block or change any trade.
"""

import datetime as dt

from . import clock

TREND_SESSIONS = 10
TRENDING_ER = 0.4
VOL_SESSIONS = 20
HIGH_VOL_RANK = 0.8
UNKNOWN = "unknown"
BASIS = ("from daily bars of sessions before the trade day only: trend = %d-session efficiency ratio "
         ">= %.2f; high volatility = previous session's range in the top %d%% of the %d sessions before it"
         % (TREND_SESSIONS, TRENDING_ER, round(100 * (1 - HIGH_VOL_RANK)), VOL_SESSIONS))


def _day(k):
    return k.date() if isinstance(k, dt.datetime) else k


def daily(bars):
    """Daily (day, open, high, low, close) from intraday or daily bars (ts, o, h, l, c, ...), ascending."""
    out = {}
    for b in bars:
        d = _day(b[0])
        if d not in out:
            out[d] = [d, b[1], b[2], b[3], b[4]]
        else:
            r = out[d]
            r[2], r[3], r[4] = max(r[2], b[2]), min(r[3], b[3]), b[4]
    return [tuple(v) for _, v in sorted(out.items())]


def classify(sessions, day):
    """sessions: daily or intraday bars; only sessions before `day` are used."""
    d = [s for s in daily(sessions) if s[0] < day]
    out = {"trend": UNKNOWN, "volatility": UNKNOWN, "basis": BASIS, "sessions_used": len(d)}
    closes = [s[4] for s in d[-(TREND_SESSIONS + 1):]]
    if len(closes) == TREND_SESSIONS + 1:
        path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
        er = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0
        out["efficiency_ratio"] = round(er, 3)
        if er >= TRENDING_ER:
            out["trend"] = "trending"
            out["direction"] = "up" if closes[-1] > closes[0] else "down"
        else:
            out["trend"] = "sideways"
    ranges = [(s[2] - s[3]) / s[4] for s in d[-(VOL_SESSIONS + 1):] if s[4]]
    if len(ranges) == VOL_SESSIONS + 1:
        last, prior = ranges[-1], ranges[:-1]
        rank = sum(1 for r in prior if r < last) / float(len(prior))
        out["range_rank"] = round(rank, 2)
        out["previous_range_pct"] = round(100 * last, 3)
        out["volatility"] = "high_volatility" if rank >= HIGH_VOL_RANK else "normal_volatility"
    out["label"] = "%s/%s" % (out["trend"], out["volatility"])
    return out


_CACHE = {}


def for_day(ctx, key, instrument_id, day):
    """Live: the regime for `day` from the instrument's daily candles (the index for index options, the
    stock itself for stocks and ETFs, as in the backtests), fetched once per day. Reporting only: a data
    failure gives 'unknown' and never affects a trade."""
    ck = (key, day)
    if ck in _CACHE:
        return _CACHE[ck]
    try:
        start = day
        for _ in range(VOL_SESSIONS + 6):
            start = ctx.cal.previous_trading_day(start)
        rows = ctx.md.candles(instrument_id, "1d", clock.at(start, "09:15"), clock.at(day, "09:14"))
        out = classify(rows, day)
    except Exception as e:                      # noqa: BLE001  (unknown regime, never a blocked trade)
        out = classify([], day)
        out["error"] = str(e)[:200]
        return out                              # not cached: tried again next cycle
    out["of"] = key
    for k in [k for k in _CACHE if k[1] != day]:
        del _CACHE[k]
    _CACHE[ck] = out
    return out


def fields(r):
    """The three regime fields stored on a signal, a ledger entry and a trade."""
    r = r or {}
    return {"regime": r.get("label", "%s/%s" % (UNKNOWN, UNKNOWN)), "trend": r.get("trend", UNKNOWN),
            "volatility": r.get("volatility", UNKNOWN)}
