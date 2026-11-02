"""Synthetic test data. Clearly fake: every dataset written here has "synthetic": true.

Used only by tests. The trending market below is built so that an
opening-range breakout wins; it says nothing about real NIFTY behaviour.
"""

import csv
import datetime as dt
import os
import random

from nse_bot import clock
from nse_bot.data import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLIDAYS = os.path.join(ROOT, "nse_bot", "data", "nse_holidays.json")
COSTS = os.path.join(ROOT, "nse_bot", "data", "costs_india.json")
IST = clock.IST


def cfg(state_dir, **over):
    c = {
        "mode": "paper", "timezone": "Asia/Kolkata", "currency": "INR",
        "underlyings": ["NIFTY"], "index_names": {"NIFTY": "Nifty 50"},
        "capital_inr": 20000, "max_daily_loss_inr": 500, "daily_profit_stop_inr": 2000,
        "max_risk_per_trade_inr": 500, "max_trades_per_day": 3, "max_open_positions": 1,
        "max_lots_per_trade": 1, "max_premium_outlay_pct": 80, "min_reward_risk": 2.0,
        "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:15",
        "expiry_day_flat_by": "14:45", "min_days_to_expiry": 0, "max_quote_age_seconds": 10,
        "max_spread_pct": 3, "min_top_qty_lots": 2, "min_volume_today": 10000,
        "min_open_interest": 50000, "slippage_ticks": 2, "max_consecutive_errors": 3,
        "event_blackout_minutes": 30, "gate_min_examples": 10000, "gate_min_oos_trades": 100,
        "data_source": "sahi", "orderflow_confirmation": "off", "forward_validation": False, "state_dir": state_dir, "holidays_file": HOLIDAYS, "costs_file": COSTS,
        "events_file": os.path.join(ROOT, "config", "events.example.json"),
        "instruments_enabled": ["equity", "etf", "index_options"],
        "equity_universe_file": os.path.join(ROOT, "config", "universe.example.json"),
        "equity_product": "intraday", "equity_min_price_inr": 50, "equity_min_top_value_inr": 200000,
        "equity_min_volume_today": 200000, "gate_allow_estimated_spread": False, "live_orders_enabled": False,
    }
    c.update(over)
    return c


STRATEGY = {"name": "orb15", "instrument": "index_option", "pattern": "orb", "params": {"or_minutes": 15, "buffer_pct": 0.0},
            "interval_minutes": 5, "strike_offset": 0, "stop_pct": 20, "target_r": 3,
            "entry_delay_bars": 1, "max_signals_per_day": 1}


def calendar():
    return clock.Calendar.load(HOLIDAYS)


def session_starts(day, minutes=5):
    t = dt.datetime.combine(day, clock.SESSION_OPEN, IST)
    end = dt.datetime.combine(day, clock.SESSION_CLOSE, IST)
    out = []
    while t + dt.timedelta(minutes=minutes) <= end:
        out.append(t)
        t += dt.timedelta(minutes=minutes)
    return out


def trading_days(n, start=dt.date(2026, 1, 2)):
    cal, d, out = calendar(), start, []
    while len(out) < n:
        if cal.is_trading_day(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def premium(S, K, typ):
    v = 24.0 + 0.25 * ((S - K) if typ == "CE" else (K - S))    # cheap enough for 1 lot within Rs 500 risk
    return max(round(v / 0.05) * 0.05, 0.05)


def day_path(rng, s0, trend=True):
    """75 five-minute bars: a quiet 15-minute range, then a steady move one way."""
    bars, s = [], s0
    d = rng.choice((1, -1))
    for i in range(75):
        o = s
        if i < 3:
            c = s0 + rng.uniform(-10, 10)
        else:
            c = s + (d * 8 if trend else rng.choice((8, -8))) + rng.uniform(-2, 2)
        h, l = max(o, c) + 2, min(o, c) - 2
        bars.append((o, h, l, c))
        s = c
    return bars


def write_dataset(folder, days, synthetic=True, trend=True, seed=1, strikes=12, drop_quotes=False):
    os.makedirs(folder, exist_ok=True)
    rng = random.Random(seed)
    cal = calendar()
    s0 = 25000.0
    with open(os.path.join(folder, "underlying.csv"), "w", newline="") as fu, \
            open(os.path.join(folder, "options.csv"), "w", newline="") as fo:
        wu, wo = csv.writer(fu), csv.writer(fo)
        wu.writerow(store.U_COLS)
        wo.writerow(store.O_COLS)
        for day in days:
            path = day_path(rng, s0, trend)
            starts = session_starts(day)
            expiry = cal.weekly_expiry(day)
            base = round(s0 / 50) * 50
            ks = [base + 50 * j for j in range(-strikes, strikes + 1)]
            for t, (o, h, l, c) in zip(starts, path):
                wu.writerow([t.isoformat(), "NIFTY", round(o, 2), round(h, 2), round(l, 2), round(c, 2)])
                for K in ks:
                    for typ in ("CE", "PE"):
                        po, pc = premium(o, K, typ), premium(c, K, typ)
                        ph, pl = max(po, pc) + 0.5, max(min(po, pc) - 0.5, 0.05)
                        bid, ask = ("", "") if drop_quotes else (round(max(pc - 0.25, 0.05), 2), round(pc + 0.25, 2))
                        wo.writerow([t.isoformat(), "NIFTY", expiry.isoformat(), K, typ, po, ph, pl, pc,
                                     100000, 500000, bid, ask, 650, 650, 65, 0.05])
            s0 = path[-1][3]
    return store.write_manifest(folder, "synthetic-test", "generated by tests/nse/synth.py", synthetic, 5)


EQ_STRATEGY = {"name": "orb15-eq", "instrument": "equity", "pattern": "orb",
               "params": {"or_minutes": 15, "buffer_pct": 0.0}, "interval_minutes": 5, "strike_offset": 0,
               "stop_pct": 0.6, "target_r": 3, "entry_delay_bars": 1, "max_signals_per_day": 1}


def write_equity_dataset(folder, days, symbols=(("AAA", "equity"), ("BBB", "etf")), fill_model="quotes",
                         trend=True, seed=5, spread_pct=0.1):
    """Fake stock/ETF bars: quiet 15 minutes, then a steady move (up or down)."""
    os.makedirs(folder, exist_ok=True)
    rng = random.Random(seed)
    with open(os.path.join(folder, "equities.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(store.E_COLS)
        for sym, kind in symbols:
            p0 = 1000.0
            for day in days:
                d = rng.choice((1, -1))
                s = p0
                for i, t in enumerate(session_starts(day)):
                    o = s
                    if i < 3:
                        c = p0 + rng.uniform(-0.5, 0.5)
                    else:
                        c = s + (d * 0.6 if trend else rng.choice((0.6, -0.6))) + rng.uniform(-0.1, 0.1)
                    h, l = max(o, c) + 0.2, min(o, c) - 0.2
                    bid = ask = ""
                    if fill_model == "quotes":
                        bid, ask = round(c - 0.05, 2), round(c + 0.05, 2)
                    w.writerow([t.isoformat(), sym, kind, round(o, 2), round(h, 2), round(l, 2), round(c, 2),
                                500000, bid, ask, 5000 if bid else "", 5000 if ask else "", 0.05])
                    s = c
                p0 = s
    return store.write_equity_manifest(folder, "synthetic-equity", "generated by tests/synth.py", True, 5,
                                       fill_model, spread_pct if fill_model == "estimated_spread" else None)
