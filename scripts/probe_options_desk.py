"""Read-only audit of FYERS option-chain fields for options-desk analytics (OI, change in OI, PCR,
IV, Greeks) and of India VIX history (for an IV percentile). Prints field names and a few values."""

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fyers_check as F  # noqa: E402


def main():
    env = os.environ
    hdr, msg = F.login(env["FYERS_APP_ID"].strip(), env["FYERS_SECRET_ID"].strip(), env["FYERS_AUTH_CODE"].strip())
    print(msg)
    if not hdr:
        return 1
    st, doc = F.call("GET", F.DATA + "/options-chain-v3", hdr, params={"symbol": F.INDEX, "strikecount": 15, "greeks": 1})
    data = doc.get("data") or {}
    print("chain HTTP %s; data keys: %s" % (st, sorted(data)))
    for k in sorted(data):
        if k != "optionsChain":
            print("  %s = %s" % (k, json.dumps(data[k])[:400]))
    rows = data.get("optionsChain") or []
    legs = [r for r in rows if r.get("option_type") in ("CE", "PE")]
    print("rows %d, legs %d" % (len(rows), len(legs)))
    print("underlying row: %s" % json.dumps(rows[0])[:600] if rows else "none")
    if legs:
        print("leg keys: %s" % sorted(legs[0]))
        print("sample leg: %s" % json.dumps(legs[len(legs) // 2])[:900])
        for f in ("oi", "oich", "oichp", "prev_oi", "volume", "iv", "ltp", "bid", "ask"):
            n = sum(1 for r in legs if r.get(f) is not None or (r.get("greeks") or {}).get(f) is not None)
            print("  field %-8s present on %d/%d legs" % (f, n, len(legs)))
    print("expiries: %s" % [e.get("date") for e in data.get("expiryData") or []][:6])
    for sym in ("NSE:INDIAVIX-INDEX",):
        st, h = F.call("GET", F.DATA + "/history", hdr, params={
            "symbol": sym, "resolution": "D", "date_format": "1",
            "range_from": (dt.date.today() - dt.timedelta(days=365)).isoformat(),
            "range_to": dt.date.today().isoformat(), "cont_flag": "1"})
        c = h.get("candles") or []
        print("%s daily history: HTTP %s, %d candles, first %s last %s" % (
            sym, st, len(c), c[0] if c else None, c[-1] if c else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
