"""Hand-built candles for the sweep_reversal tests. Every number is chosen so the
expected rule outcome is known in advance; tests then break one rule at a time.

Scenario (bullish, today = 2026-10-29):
  previous session: rises 25000 -> 25250, falls back to 25070 (PDL 25000, PDH 25250)
  today 5m bars (09:15 + 5i):
    b0-b8   drift down from 25060 to ~25012, swing high 25050 at b5
    b9      10:00 sweep: low 24985 under PDL 25000, close 24995 below it
    b10     10:05 reclaim: close 25010 back above 25000
    b11     10:10 confirmation + displacement: close 25062 above the sweep high 25020
            and above the swing high 25050 (structure break); future volume x3
    b12     10:15 leaves a bullish FVG 25012-25030 (b10 high .. b12 low)
    b13     10:20 dips to 25025 in its 1st minute; 2nd minute (10:21) closes bullish -> 1m trigger
  now = 10:22:30
"""

import datetime as dt

from nse_bot import clock

import synth

DAY = dt.date(2026, 10, 29)
NOW = clock.at(DAY, "10:22") + dt.timedelta(seconds=30)
FUT_PREMIUM = 50.0

TODAY_5M = [
    (25060, 25065, 25040, 25045), (25045, 25055, 25035, 25050), (25050, 25052, 25042, 25044),
    (25044, 25048, 25025, 25030), (25030, 25040, 25020, 25036), (25036, 25050, 25030, 25034),
    (25034, 25040, 25015, 25020), (25020, 25030, 25008, 25012), (25012, 25022, 25004, 25018),
    (25018, 25020, 24985, 24995), (24995, 25012, 24990, 25010), (25010, 25066, 25008, 25062),
    (25062, 25084, 25030, 25080), (25080, 25088, 25025, 25084),
]


def minutes_for(o, h, l, c):
    """Five 1m bars whose 5m resample is exactly (o, h, l, c)."""
    pts = [o, l, (l + h) / 2.0, h, c, c] if c >= o else [o, h, (l + h) / 2.0, l, c, c]
    return [(pts[i], max(pts[i], pts[i + 1]), min(pts[i], pts[i + 1]), pts[i + 1]) for i in range(5)]


def sessions_before(day, n):
    cal = synth.calendar()
    out, d = [], day
    for _ in range(n):
        d = cal.previous_trading_day(d)
        out.append(d)
    return sorted(out)


def session_1m(day, path, vol=1000):
    """1m bars following `path` (list of closes, 375 long), open = previous close."""
    bars, prev = [], path[0]
    for i, c in enumerate(path):
        t = clock.at(day, "09:15") + dt.timedelta(minutes=i)
        o = prev
        bars.append((t, o, max(o, c) + 1, min(o, c) - 1, c, vol))
        prev = c
    return bars


def prev_path():
    up = [25001 + 249.0 * i / 187 for i in range(188)]
    down = [25250 - 180.0 * i / 186 for i in range(1, 188)]
    return up + down


def build(today_5m=None, now=NOW, fut=True, vol_mult=3, sessions=10, extra=None):
    today_5m = today_5m or TODAY_5M
    pday = sessions_before(DAY, 1)[0]
    u_prev = session_1m(pday, prev_path())
    u_prev[0] = (u_prev[0][0], 25001, 25002, 25000, 25001, 1000)          # PDL exactly 25000
    u_today = []
    for i, ohlc in enumerate(today_5m):
        start = clock.at(DAY, "09:15") + dt.timedelta(minutes=5 * i)
        for j, (o, h, l, c) in enumerate(minutes_for(*ohlc)):
            u_today.append((start + dt.timedelta(minutes=j), o, h, l, c, None))
    u1 = [b[:5] + (None,) for b in u_prev] + u_today
    f1 = []
    if fut:
        for d in sessions_before(DAY, sessions):
            f1 += [(t, o + FUT_PREMIUM, h + FUT_PREMIUM, l + FUT_PREMIUM, c + FUT_PREMIUM, 1000)
                   for (t, o, h, l, c, _) in session_1m(d, prev_path())]
        for b in u_today:
            v = 1000 * (vol_mult if dt.time(10, 10) <= b[0].time() < dt.time(10, 15) else 1)
            f1.append((b[0], b[1] + FUT_PREMIUM, b[2] + FUT_PREMIUM, b[3] + FUT_PREMIUM, b[4] + FUT_PREMIUM, v))
    days = sessions_before(DAY, 30)
    u1d = [(clock.at(d, "09:15"), 24000 + 35 * k, 24020 + 35 * k, 23980 + 35 * k, 24010 + 35 * k, None)
           for k, d in enumerate(days)]
    u1h = []
    for k, d in enumerate(days[-6:]):
        for h in range(6):
            base = 24640 + 11 * (6 * k + h)
            u1h.append((clock.at(d, "09:15") + dt.timedelta(hours=h), base, base + 15, base - 10, base + 10, None))
    u1h.append((clock.at(DAY, "09:15"), 25060, 25066, 24985, 25062, None))
    if extra:
        extra(locals())
    return {"u1": u1, "u1h": u1h, "u1d": u1d, "f1": f1, "now": now}


def chain_doc(spot=25047, iv=14.0, theta=-800, vega=1000, spread_paise=10, missing=None, oi=900000):
    """SAHI-shaped option chain (paise) for the test scrip master (ids from test_instruments.chain_rows)."""
    rows, i = [], 3000000
    for k in range(24800, 25250, 50):
        legs = {}
        for t in ("CE", "PE"):
            i += 1
            itm_steps = (spot - k) / 50.0 if t == "CE" else (k - spot) / 50.0
            delta = max(0.05, min(0.95, 0.5 + 0.1 * itm_steps)) * (1 if t == "CE" else -1)
            prem = max(5.0, 150 + 50 * itm_steps)
            leg = {"instrument_id": i, "top_bid_price": int(prem * 100), "top_ask_price": int(prem * 100) + spread_paise,
                   "top_bid_qty": 1300, "top_ask_qty": 1300, "last_trade_price": int(prem * 100),
                   "volume_today": 200000, "open_interest": oi, "implied_volatility": iv,
                   "greeks": {"delta": round(delta, 3), "gamma": 0.001, "theta": theta, "vega": vega}}
            if missing:
                for m in missing:
                    if m in leg:
                        leg[m] = None
                    else:
                        leg["greeks"][m] = None
            legs[t.lower()] = leg
        rows.append({"strike": k * 100, **legs})
    return {"underlying_id": 26000, "expiry": "2026-11-03", "last_price": spot * 100, "strikes": rows}


STRATEGY = {
    "name": "sweep-reversal-test", "instrument": "index_option", "pattern": "sweep_reversal",
    "interval_minutes": 5, "max_signals_per_day": 1,
    "params": {"swing_n": 2, "equal_tol_pct": 0.03, "sweep_min_pen_pct": 0.02, "reclaim_bars": 2,
               "confirm_within_bars": 6, "confirm_body_pct": 50, "fvg_min_pct": 0.02, "vol_expansion_x": 1.5,
               "vol_lookback_bars": 20, "rvol_min": 1.0, "rvol_sessions": 10, "rsi_n": 14, "ema_n": 20,
               "h1_slope_bars": 3, "entry_window_minutes": 30, "stop_buffer_pct": 0.02, "max_itm_strikes": 2,
               "delta_min": 0.45, "delta_max": 0.8, "delta_target": 0.55, "max_theta_pct_per_day": 8,
               "iv_shock_points": 1, "gap_allowance_pct": 5, "fut_min_coverage_pct": 95,
               "fut_max_gap_minutes": 5, "signal_max_age_minutes": 2},
}
