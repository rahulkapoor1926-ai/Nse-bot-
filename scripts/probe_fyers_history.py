"""Read-only probe: how much NIFTY OPTION history does FYERS actually serve? (No orders: GET /history only.)

1. NIFTY index: daily candles for 2 years, and 1-minute candles on sample days (2y, 1y, 3m, 1w ago).
2. Every NIFTY option expiry listed today: ATM CE daily history over 2 years -> first and last day served,
   and whether 1-minute candles exist on that first day.
3. EXPIRED contracts: the monthly expiry of each of the last 24 months and the recent weekly expiries,
   ATM CE by that day's close (FYERS ticker formats), 1-minute candles over the expiry's last 3 days.
Prints every result with FYERS's own status/message, then a JSON summary. About 100 calls.
"""

import calendar
import datetime as dt
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fyers_check as F  # noqa: E402
from nse_bot import instruments  # noqa: E402
from nse_bot.broker import fyers  # noqa: E402

TODAY = dt.date.today()
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
WEEKLY_M = "123456789OND"


def hist(hdr, sym, res, frm, to):
    time.sleep(1.3)                       # stays under FYERS's 50 calls/minute
    st, doc = F.call("GET", F.DATA + "/history", hdr, params={
        "symbol": sym, "resolution": res, "date_format": "1", "range_from": frm.isoformat(),
        "range_to": to.isoformat(), "cont_flag": "1"})
    c = doc.get("candles") or []
    return st, doc.get("s"), doc.get("message"), c


def first_last(c):
    if not c:
        return None, None
    f = lambda x: dt.datetime.fromtimestamp(int(x[0]), dt.timezone(dt.timedelta(hours=5, minutes=30))).date().isoformat()
    return f(c[0]), f(c[-1])


def last_weekday(y, m, wd):
    d = dt.date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != wd:
        d -= dt.timedelta(days=1)
    return d


def main():
    env = os.environ
    hdr, msg = F.login(env["FYERS_APP_ID"].strip(), env["FYERS_SECRET_ID"].strip(), env["FYERS_AUTH_CODE"].strip())
    print(msg)
    if not hdr:
        return 1
    out = {"probe_date": TODAY.isoformat()}

    # 1. index
    closes = {}
    for a, b in ((TODAY - dt.timedelta(days=730), TODAY - dt.timedelta(days=366)), (TODAY - dt.timedelta(days=365), TODAY)):
        st, s, m, c = hist(hdr, "NSE:NIFTY50-INDEX", "D", a, b)
        for x in c:
            closes[first_last([x])[0]] = x[4]
    days = sorted(closes)
    print("INDEX daily: %d sessions, %s .. %s" % (len(days), days[0] if days else None, days[-1] if days else None))
    out["index_daily"] = {"sessions": len(days), "first": days[0] if days else None, "last": days[-1] if days else None}
    out["index_1m_samples"] = {}
    for back in (730, 365, 90, 7):
        d = TODAY - dt.timedelta(days=back)
        st, s, m, c = hist(hdr, "NSE:NIFTY50-INDEX", "1", d, d + dt.timedelta(days=3))
        print("INDEX 1m from %s: HTTP %s %s %s, %d candles %s" % (d, st, s, m or "", len(c), first_last(c)))
        out["index_1m_samples"][d.isoformat()] = len(c)

    def spot_on(d):
        k = [x for x in days if x <= d.isoformat()]
        return closes[k[-1]] if k else None

    # 2. live expiries
    path = os.path.join(tempfile.mkdtemp(), "m.csv")
    fyers.build_master(path, ["NIFTY"], {"NIFTY": "Nifty 50"}, with_cash=False)
    m = instruments.load(path, ["NIFTY"], ["Nifty 50"])
    syms = fyers.load_symbols(path)
    spot = spot_on(TODAY)
    out["live_expiries"] = []
    for e in m.expiries("NIFTY", TODAY):
        try:
            ce = m.pick("NIFTY", e, "CE", spot, 0)
        except Exception as ex:                                       # noqa: BLE001
            print("LIVE %s: no strike near spot (%s)" % (e, ex))
            continue
        sym = syms.get(str(ce.instrument_id)) or syms.get(ce.instrument_id)
        st, s, msg_, c = hist(hdr, sym, "D", TODAY - dt.timedelta(days=365), TODAY)
        st2, s2, m2, c2 = hist(hdr, sym, "D", TODAY - dt.timedelta(days=730), TODAY - dt.timedelta(days=366))
        allc = c2 + c
        fd, ld = first_last(allc)
        n1 = None
        if fd:
            d0 = dt.date.fromisoformat(fd)
            _, _, _, c1 = hist(hdr, sym, "1", d0, d0 + dt.timedelta(days=1))
            n1 = len(c1)
        row = {"expiry": e.isoformat(), "symbol": sym, "daily_sessions": len(allc), "first": fd, "last": ld,
               "1m_candles_on_first_day": n1, "status": "%s %s %s" % (st, s, msg_ or "")}
        print("LIVE %s %s: %d daily candles %s..%s; 1m on first day: %s" % (e, sym, len(allc), fd, ld, n1))
        out["live_expiries"].append(row)

    # 3. expired contracts
    out["expired"] = []
    probes = []
    for k in range(1, 25):
        y, mo = TODAY.year, TODAY.month - k
        while mo <= 0:
            mo += 12
            y -= 1
        wd = 1 if dt.date(y, mo, 1) >= dt.date(2025, 9, 1) else 3      # NIFTY expiry: Tue from Sep 2025, Thu before
        e = last_weekday(y, mo, wd)
        probes.append(("monthly", e, "NSE:NIFTY%02d%s" % (y % 100, MON[mo - 1])))
    e = TODAY
    for _ in range(6):                                                   # recent weekly (Tuesday) expiries
        e -= dt.timedelta(days=1)
        while e.weekday() != 1:
            e -= dt.timedelta(days=1)
        if e == last_weekday(e.year, e.month, 1):
            continue
        probes.append(("weekly", e, "NSE:NIFTY%02d%s%02d" % (e.year % 100, WEEKLY_M[e.month - 1], e.day)))
    for kind, e, prefix in probes:
        s0 = spot_on(e - dt.timedelta(days=3))
        if s0 is None:
            continue
        strike = int(round(s0 / 50.0) * 50)
        sym = "%s%dCE" % (prefix, strike)
        st, s, msg_, c = hist(hdr, sym, "1", e - dt.timedelta(days=3), e)
        fd, ld = first_last(c)
        print("EXPIRED %-7s %s %s: HTTP %s %s %s -> %d 1m candles %s" % (kind, e, sym, st, s, msg_ or "", len(c), (fd, ld)))
        out["expired"].append({"kind": kind, "expiry": e.isoformat(), "symbol": sym, "http": st, "s": s,
                               "message": msg_, "candles_1m": len(c)})
    print("SUMMARY_JSON " + json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
