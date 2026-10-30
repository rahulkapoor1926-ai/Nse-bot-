"""Option scanner: run a pattern on each underlying, map the signal to a listed contract.

Input: closed underlying bars per index, the scrip master, quotes for candidate
contracts, the calendar and a strategy spec. Output: one row per underlying with
either a contract to consider or the reason there is none. Nothing here decides
to trade; that is entry -> gate -> budget -> risk.
"""

import datetime as dt

from . import patterns
from .errors import CalendarError, DataError


def choose_expiry(master, cal, underlying, today, min_days):
    for e in master.expiries(underlying, today):
        if (e - today).days < min_days:
            continue
        if not cal.check_expiry(underlying, e):
            continue
        return e
    return None


def scan(underlying_bars, prev_levels, master, cal, now, cfg, strategy):
    """underlying_bars: {"NIFTY": [bars of today's session]}; prev_levels: {"NIFTY": (high, low)}."""
    patterns.validate_strategy(strategy)
    minutes = strategy["interval_minutes"]
    today = now.date()
    rows = []
    for u in cfg["underlyings"]:
        row = {"underlying": u, "strategy": strategy["name"]}
        rows.append(row)
        try:
            bars = patterns.validate_bars(underlying_bars.get(u) or [], minutes)
            bars = [b for b in patterns.closed_bars(bars, now, minutes) if b[0].date() == today]
            if not bars:
                row["status"] = "NO_DATA"
                row["reason"] = "no closed %d-minute bars for %s today" % (minutes, u)
                continue
            ph, pl = prev_levels.get(u, (None, None))
            sig = patterns.detect(strategy, bars, minutes, ph, pl)
            if sig is None:
                row["status"] = "NO_SIGNAL"
                continue
            row["signal"] = sig
            expiry = choose_expiry(master, cal, u, today, cfg["min_days_to_expiry"])
            if expiry is None:
                row["status"] = "NO_CONTRACT"
                row["reason"] = "no listed %s expiry passes the calendar cross-check" % u
                continue
            spot = bars[-1][4]
            c = master.pick(u, expiry, sig["option_type"], spot, strategy["strike_offset"])
            row["status"] = "SIGNAL"
            row["contract"] = c
            row["spot"] = spot
        except (DataError, CalendarError) as e:
            row["status"] = "ERROR"
            row["reason"] = str(e)
    return rows
