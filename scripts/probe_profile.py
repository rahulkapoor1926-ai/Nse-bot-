"""Read-only probe for volume profile inputs: is there volume-at-price data, and how fine are candles?

The official SDK (fyers-apiv3 3.2.1) has no volume-profile / volume-at-price endpoint; its documented
history resolutions start at 1 minute. This probe asks /history for the NIFTY near future at second
resolutions anyway (to report what really happens) and at 1 minute for the last 10 days.
"""

import datetime as dt
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
    _, fut = F.check_futures(hdr)
    now = dt.datetime.now(F.IST)
    today = int(now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
    for res in ("5S", "10S", "15S", "30S", "1"):
        st, doc = F.call("GET", F.DATA + "/history", hdr, params={
            "symbol": fut, "resolution": res, "date_format": "0", "range_from": today,
            "range_to": int(now.timestamp()), "cont_flag": "1"})
        c = doc.get("candles") or []
        spans = [x[2] - x[3] for x in c if len(c) and len(x) > 5]
        print("resolution %-4s HTTP %s s=%s candles=%d with volume=%d median range=%s message=%s" % (
            res, st, doc.get("s"), len(c), sum(1 for x in c if len(x) > 5 and x[5]),
            sorted(spans)[len(spans) // 2] if spans else None, F._safe(doc.get("message"))))
    st, doc = F.call("GET", F.DATA + "/history", hdr, params={
        "symbol": fut, "resolution": "1", "date_format": "1",
        "range_from": (now.date() - dt.timedelta(days=10)).isoformat(), "range_to": now.date().isoformat(), "cont_flag": "1"})
    c = doc.get("candles") or []
    days = sorted({dt.datetime.fromtimestamp(x[0], F.IST).date().isoformat() for x in c})
    print("1m history last 10 days: HTTP %s, %d candles, sessions %s" % (st, len(c), days))
    evaluate(hdr, fut, now)
    return 0


def evaluate(hdr, fut, now):
    """Run the bot's auction context on live FYERS data (futures volume, index for the initial balance)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import json
    from nse_bot import clock, volume_profile

    def bars(sym, days):
        st, doc = F.call("GET", F.DATA + "/history", hdr, params={
            "symbol": sym, "resolution": "1", "date_format": "0",
            "range_from": int(clock.at(now.date() - dt.timedelta(days=days), "09:15").timestamp()),
            "range_to": int(now.timestamp()), "cont_flag": "1"})
        return [(clock.from_epoch_ms(int(x[0]) * 1000), x[1], x[2], x[3], x[4], x[5] if len(x) > 5 else None)
                for x in doc.get("candles") or []]
    fb, ib = bars(fut, 14), bars(F.INDEX, 0)
    t = clock.now_ist()
    for setup in ("orb", "sweep"):
        for d in (1, -1):
            c = volume_profile.context(setup, d, fb, ib, ib[-1][4] if ib else None, t)
            if setup == "orb" and d == 1:
                for k in ("session", "previous_session", "previous_week"):
                    v = c[k]
                    print("%-17s %s POC %s VAL %s VAH %s HVN %s LVN %s (%s bars)" % (
                        k, v.get("status"), v.get("poc"), v.get("val"), v.get("vah"), v.get("hvn"), v.get("lvn"), v.get("bars")))
                print("value migration:  %s" % c["value_migration"])
                print("acceptance:       %s" % json.dumps(c["acceptance"]))
                print("initial balance:  %s" % json.dumps(c["initial_balance"]))
                print("future %s, index %s" % (c["future_price"], c["index_price"]))
            print("%-5s %-4s %-12s supports=%s cautions=%s" % (setup, c["direction"], c["verdict"], c["supports"], c["cautions"]))


if __name__ == "__main__":
    sys.exit(main())
