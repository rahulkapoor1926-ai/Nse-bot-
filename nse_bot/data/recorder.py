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
    for u in (cfg["underlyings"] if "index_options" in cfg["instruments_enabled"] else []):
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
    written += _record_stocks(md, master, cfg, bar_start, end, size, folder)
    return written


def _record_stocks(md, master, cfg, bar_start, end, size, folder):
    stocks = [s for s in master.stocks.values() if s.kind in cfg["instruments_enabled"]]
    if not stocks:
        return 0
    snaps = md.quotes([s.instrument_id for s in stocks])
    rows = []
    for s in stocks:
        b = [r for r in md.candles(s.instrument_id, size, bar_start, end) if r[0] == bar_start]
        if not b:
            continue
        o = b[0]
        q = snaps.get(s.instrument_id)
        rows.append([o[0].isoformat(), s.symbol, s.kind, o[1], o[2], o[3], o[4], _v(o[5]),
                     _v(q.bid if q else None), _v(q.ask if q else None),
                     _v(q.bid_qty if q else None), _v(q.ask_qty if q else None), s.tick_size])
    _append(os.path.join(folder, "equities.csv"), store.E_COLS, rows)
    return len(rows)


def build_equity(recordings_dir, out_dir, name, minutes):
    """Concatenate recorded stock/ETF sessions into a dataset with real bid/ask (fill_model quotes)."""
    os.makedirs(out_dir, exist_ok=True)
    days = sorted(d for d in os.listdir(recordings_dir)
                  if os.path.exists(os.path.join(recordings_dir, d, "equities.csv")))
    with open(os.path.join(out_dir, "equities.csv"), "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(store.E_COLS)
        for d in days:
            with open(os.path.join(recordings_dir, d, "equities.csv"), encoding="utf-8") as f:
                r = csv.reader(f)
                next(r, None)
                w.writerows(r)
    return store.write_equity_manifest(out_dir, name, "SAHI live recording (%d sessions)" % len(days),
                                       False, minutes, "quotes")


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


# ---------------------------------------------------------------------------
# sweep_reversal replay data (data/sweep_store.py format), one row set per minute:
# the closed 1m index bar, the closed 1m current-month future bar (with volume) and
# the option-chain snapshot (bid/ask, OI, volume, IV, Greeks) for the nearest
# expiry, strikes_each_side around the money. Taken right after the minute closes.

def record_sweep_minute(md, master, cfg, cal, minute_start, out_dir, strikes_each_side=STRIKES_EACH_SIDE):
    from .. import contract_select, scanner
    from . import sweep_store
    day = minute_start.date()
    end = minute_start + dt.timedelta(minutes=1)
    written = 0
    for u in cfg["underlyings"]:
        folder = os.path.join(out_dir, u, day.isoformat())
        os.makedirs(folder, exist_ok=True)
        iid = master.index_id(cfg["index_names"][u])
        ub = [r for r in md.candles(iid, "1m", minute_start, end) if r[0] == minute_start]
        if not ub:
            continue
        b = ub[0]
        _append(os.path.join(folder, "index_1m.csv"), sweep_store.FILES["index_1m.csv"],
                [[b[0].isoformat(), b[1], b[2], b[3], b[4]]])
        written += 1
        fut = master.current_future(u, day)
        if fut is not None:
            fb = [r for r in md.candles(fut.instrument_id, "1m", minute_start, end) if r[0] == minute_start]
            if fb:
                f = fb[0]
                _append(os.path.join(folder, "future_1m.csv"), sweep_store.FILES["future_1m.csv"],
                        [[f[0].isoformat(), fut.symbol, fut.expiry.isoformat(), f[1], f[2], f[3], f[4], _v(f[5])]])
                written += 1
        expiry = scanner.choose_expiry(master, cal, u, day, cfg["min_days_to_expiry"])
        if expiry is None:
            continue
        chain = contract_select.parse_chain(md.option_chain(iid, expiry))
        strikes = sorted({l["strike"] for l in chain["legs"]})
        if not strikes:
            continue
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - b[4]))
        keep = set(strikes[max(0, atm - strikes_each_side): atm + strikes_each_side + 1])
        rows = []
        for l in chain["legs"]:
            c = master.by_id(int(l["instrument_id"])) if l["instrument_id"] is not None else None
            if l["strike"] not in keep or c is None:
                continue
            rows.append([minute_start.isoformat(), c.instrument_id, c.symbol, c.expiry.isoformat(), c.strike,
                         c.option_type, c.lot_size, c.tick_size] +
                        [_v(l[k]) for k in ("bid", "ask", "bid_qty", "ask_qty", "volume", "oi", "iv",
                                            "delta", "gamma", "theta", "vega")])
        _append(os.path.join(folder, "chain_1m.csv"), sweep_store.FILES["chain_1m.csv"], rows)
        written += len(rows)
    return written


def build_sweep(recordings_dir, out_dir, name, underlying):
    """Concatenate recorded sweep sessions of one underlying into a sweep dataset (synthetic=false)."""
    from . import sweep_store
    src = os.path.join(recordings_dir, underlying)
    os.makedirs(out_dir, exist_ok=True)
    days = sorted(d for d in os.listdir(src) if os.path.isdir(os.path.join(src, d))) if os.path.isdir(src) else []
    for fname, cols in sweep_store.FILES.items():
        with open(os.path.join(out_dir, fname), "w", newline="", encoding="utf-8") as out:
            w = csv.writer(out)
            w.writerow(cols)
            for d in days:
                p = os.path.join(src, d, fname)
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        r = csv.reader(f)
                        next(r, None)
                        w.writerows(r)
    return sweep_store.write_manifest(out_dir, name, "SAHI live recording, 1m (%d sessions: %s to %s)" % (
        len(days), days[0] if days else "-", days[-1] if days else "-"), False, underlying)
