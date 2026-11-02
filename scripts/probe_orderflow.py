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
    st, doc = F.call("GET", F.DATA + "/quotes", hdr, params={"symbols": ",".join(s for s in (fut, atm) if s)})
    for x in doc.get("d") or []:
        print("\n/quotes %s fields: %s" % (x.get("n"), json.dumps(x.get("v"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
