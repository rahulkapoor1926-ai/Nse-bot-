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
    evaluate(data, c)
    return 0


def evaluate(data, vix_candles):
    """The bot's options-desk analysis on this live chain (nearest expiry)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from nse_bot import clock, options_desk
    rows = data.get("optionsChain") or []
    legs = [{"option_type": r["option_type"], "strike": float(r["strike_price"]), "oi": r.get("oi"),
             "prev_oi": r.get("prev_oi"), "volume": r.get("volume"), "ltp": r.get("ltp"),
             **{g: (r.get("greeks") or {}).get(g) for g in ("delta", "gamma", "theta", "vega", "iv")}}
            for r in rows if r.get("option_type") in ("CE", "PE")]
    exp = (data.get("expiryData") or [{}])[0]
    chain = {"spot": rows[0].get("ltp"), "legs": legs, "expiry": None, "india_vix": (data.get("indiavixData") or {}).get("ltp"),
             "totals": {"call_oi": data.get("callOi"), "put_oi": data.get("putOi")}, "expiry_epoch": int(exp.get("expiry"))}
    vix = [(clock.from_epoch_ms(int(x[0]) * 1000).date(), float(x[4])) for x in vix_candles]
    now = clock.now_ist()
    desk = options_desk.analyse(chain, now, vix)
    print("\n== options desk on the live chain at %s" % now.strftime("%H:%M:%S"))
    print(json.dumps({k: v for k, v in desk.items() if k != "status"}, indent=1, default=str)[:3500])
    for setup in ("orb", "sweep"):
        for d in (1, -1):
            c = options_desk.context(setup, d, desk)
            print("%-5s %-4s %-10s supports=%s cautions=%s notes=%s" % (setup, c["direction"], c["verdict"],
                                                                       c["supports"], c["cautions"], c["notes"]))


if __name__ == "__main__":
    sys.exit(main())
