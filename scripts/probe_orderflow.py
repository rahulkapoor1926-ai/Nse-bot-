"""Read-only probe: which order-flow fields does FYERS REST actually return?

Prints field names and values (market data only; credentials are masked by fyers_check.login).
Takes 6 depth snapshots 3 s apart for the NIFTY near future and the ATM CE, and one /quotes
call, so we can see whether trade-side (aggressor) data, book totals and per-trade sizes exist.
"""

import json
import sys
import time
import os

sys.path.insert(0, os.path.dirname(__file__))
import fyers_check as F  # noqa: E402


def depth(hdr, sym):
    status, doc = F.call("GET", F.DATA + "/depth", hdr, params={"symbol": sym, "ohlcv_flag": 1})
    return status, (doc.get("d") or {}).get(sym) or doc


def main():
    env = os.environ
    hdr, msg = F.login(env["FYERS_APP_ID"].strip(), env["FYERS_SECRET_ID"].strip(), env["FYERS_AUTH_CODE"].strip())
    print(msg)
    if not hdr:
        return 1
    _, fut = F.check_futures(hdr)
    _, atm = F.check_chain(hdr)
    for sym in (fut, atm, F.INDEX):
        print("\n== %s" % sym)
        prev = None
        for i in range(6):
            st, d = depth(hdr, sym)
            if i == 0:
                print("HTTP %s; top-level keys: %s" % (st, sorted(d)))
                print("bid levels: %s" % json.dumps(d.get("bids")))
                print("ask levels: %s" % json.dumps(d.get("ask")))
            row = {k: d.get(k) for k in ("ltp", "ltq", "ltt", "v", "totalbuyqty", "totalsellqty", "tbq", "tsq",
                                          "atp", "o", "h", "l", "c", "oi")}
            vol = d.get("v")
            row["volume_change"] = None if prev is None or vol is None else vol - prev
            prev = vol
            print("snapshot %d: %s" % (i, json.dumps(row)))
            time.sleep(3)
    evaluate(hdr, fut)
    st, doc = F.call("GET", F.DATA + "/quotes", hdr, params={"symbols": ",".join(s for s in (fut, atm) if s)})
    for x in doc.get("d") or []:
        print("\n/quotes %s fields: %s" % (x.get("n"), json.dumps(x.get("v"))))
    return 0


def evaluate(hdr, fut):
    """Run the bot's order-flow confirmation on the live NIFTY future, both directions."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import datetime as dt
    from nse_bot import clock, orderflow
    from nse_bot.broker.fyers import FyersReadOnly
    md = FyersReadOnly(env={"FYERS_APP_ID": "probe", "FYERS_ACCESS_TOKEN": "unused"})
    now = clock.now_ist()
    _, d = depth(hdr, fut)
    q = md.quote_from_depth(1, d)
    st, doc = F.call("GET", F.DATA + "/history", hdr, params={
        "symbol": fut, "resolution": "1", "date_format": "0", "range_from": int(clock.at(now.date(), "09:15").timestamp()),
        "range_to": int(now.timestamp()), "cont_flag": "1"})
    bars = [(clock.from_epoch_ms(int(c[0]) * 1000), c[1], c[2], c[3], c[4], c[5], None) for c in doc.get("candles") or []]
    bars = [b for b in bars if b[0] + dt.timedelta(minutes=1) <= now]
    print("\n== order-flow evaluation on %s at %s (%d closed 1m bars)" % (fut, now.strftime("%H:%M:%S"), len(bars)))
    for setup in ("orb", "sweep"):
        for direction in (1, -1):
            r = orderflow.confirm(setup, direction, q, bars, clock.now_ist(), {"max_quote_age_seconds": 10})
            m = r["metrics"]
            print("%-5s %-4s %-14s imbalance5=%s total=%s exhaustion=%s reasons=%s" % (
                setup, r["direction"], r["status"], m["bid_ask_imbalance"].get("five_level"),
                m["bid_ask_imbalance"].get("total"),
                {k: m["volume_exhaustion"].get(k) for k in ("volume_ratio", "new_extreme", "exhausted")},
                r["reasons"]))
    for k, v in orderflow.METRICS.items():
        print("%-24s %s" % (k, v[0]))


if __name__ == "__main__":
    sys.exit(main())
