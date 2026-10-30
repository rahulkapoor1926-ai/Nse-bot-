"""Build a stock/ETF dataset from SAHI's historical candles (read-only).

SAHI candles carry OHLC and volume but no bid/ask, so the dataset is written
with fill_model "estimated_spread": the backtester buys at close x (1 + s/2) and
sells at close x (1 - s/2), rounded against us to the tick, plus slippage. The
gate uses such a dataset only when gate_allow_estimated_spread is true (stocks
and ETFs only; options always need recorded bid/ask).

How far back SAHI's 5-minute history goes is not documented; the summary
reports the first and last session actually received per symbol.
"""

import csv
import datetime as dt
import os

from .. import clock
from ..errors import NseBotError
from . import store

DEFAULT_SPREAD_PCT = 0.10


def fetch_equity_history(md, stocks, cal, start, end, interval, out_dir, spread_pct=DEFAULT_SPREAD_PCT):
    """stocks: iterable of instruments.Stock. Returns {"manifest", "symbols": {sym: summary}, "errors"}."""
    os.makedirs(out_dir, exist_ok=True)
    summary, errors = {}, {}
    path = os.path.join(out_dir, "equities.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(store.E_COLS)
        for st in stocks:
            try:
                rows = md.candles(st.instrument_id, "%dm" % interval, clock.at(start, "09:15"),
                                  clock.at(end, "15:30"))
            except NseBotError as e:
                errors[st.symbol] = str(e)
                continue
            days = set()
            n = 0
            for r in rows:
                t = r[0]
                try:
                    if not cal.is_trading_day(t.date()):
                        continue
                except NseBotError:
                    continue
                w.writerow([t.isoformat(), st.symbol, st.kind, r[1], r[2], r[3], r[4],
                            "" if r[5] is None else r[5], "", "", "", "", st.tick_size])
                days.add(t.date())
                n += 1
            summary[st.symbol] = {"bars": n, "sessions": len(days),
                                  "first": min(days).isoformat() if days else None,
                                  "last": max(days).isoformat() if days else None}
    man = store.write_equity_manifest(
        out_dir, "sahi-equity-%s-%s" % (start, end),
        "SAHI /v1/marketdata/candles %dm, %s to %s, fetched %s" % (interval, start, end, dt.date.today()),
        False, interval, "estimated_spread", spread_pct)
    return {"manifest": man, "symbols": summary, "errors": errors}
