"""Record genuine bid/ask history from the read-only SAHI feed, bar by bar.

SAHI's candles endpoint gives OHLC/volume/OI but no historical bid/ask, and
expired contracts drop out of the scrip master. The backtester needs the quote
at each bar's close to fill at the ask/bid, so this recorder captures it going
forward: after each bar closes it stores the index bar, the option bars for the
strikes around the money, and the quote snapshot taken at that moment.

Rows go to <out>/<day>/underlying.csv and options.csv in the dataset format of
data/store.py; build() combines recorded days into a dataset with a manifest
marked synthetic=false and source "SAHI live recording". A missing quote is
written as empty fields, never zero.
"""

import csv
import datetime as dt
import os
import shutil

from . import store

STRIKES_EACH_SIDE = 5


def _append(path, cols, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerows(rows)


def _v(x):
    return "" if x is None else x


def record_bar(md, master, cfg, bar_start, minutes, out_dir, strikes_each_side=STRIKES_EACH_SIDE):
    """Record the bar that started at bar_start (call just after it closes). Returns rows written."""
    day = bar_start.date()
    end = bar_start + dt.timedelta(minutes=minutes)
    folder = os.path.join(out_dir, day.isoformat())
    os.makedirs(folder, exist_ok=True)
    size = "%dm" % minutes
    written = 0
    for u in cfg["underlyings"]:
        iid = master.index_id(cfg["index_names"][u])
        ub = [r for r in md.candles(iid, size, bar_start, end) if r[0] == bar_start]
        if not ub:
            continue
        b = ub[0]
        _append(os.path.join(folder, "underlying.csv"), store.U_COLS,
                [[b[0].isoformat(), u, b[1], b[2], b[3], b[4]]])
        written += 1
        expiries = [e for e in master.expiries(u, day)][:1]
        if not expiries:
            continue
        strikes = master.strikes(u, expiries[0])
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - b[4]))
        chosen = strikes[max(0, atm - strikes_each_side): atm + strikes_each_side + 1]
        contracts = [c for c in master.chain(u, expiries[0]) if c.strike in chosen]
        snaps = md.quotes([c.instrument_id for c in contracts])
        rows = []
        for c in contracts:
            ob = [r for r in md.candles(c.instrument_id, size, bar_start, end) if r[0] == bar_start]
            if not ob:
                continue
            o = ob[0]
            q = snaps.get(c.instrument_id)
            rows.append([o[0].isoformat(), u, c.expiry.isoformat(), c.strike, c.option_type, o[1], o[2], o[3], o[4],
                         _v(o[5]), _v(o[6] if len(o) > 6 else (q.oi if q else None)),
                         _v(q.bid if q else None), _v(q.ask if q else None),
                         _v(q.bid_qty if q else None), _v(q.ask_qty if q else None), c.lot_size, c.tick_size])
        _append(os.path.join(folder, "options.csv"), store.O_COLS, rows)
        written += len(rows)
    return written


def build(recordings_dir, out_dir, name, minutes):
    """Concatenate recorded days into one dataset folder with a manifest."""
    os.makedirs(out_dir, exist_ok=True)
    days = sorted(d for d in os.listdir(recordings_dir) if os.path.isdir(os.path.join(recordings_dir, d)))
    for fname, cols in (("underlying.csv", store.U_COLS), ("options.csv", store.O_COLS)):
        with open(os.path.join(out_dir, fname), "w", newline="", encoding="utf-8") as out:
            w = csv.writer(out)
            w.writerow(cols)
            for d in days:
                p = os.path.join(recordings_dir, d, fname)
                if not os.path.exists(p):
                    continue
                with open(p, encoding="utf-8") as f:
                    r = csv.reader(f)
                    next(r, None)
                    w.writerows(r)
    return store.write_manifest(out_dir, name, "SAHI live recording (%d sessions: %s to %s)" % (
        len(days), days[0] if days else "-", days[-1] if days else "-"), False, minutes)
