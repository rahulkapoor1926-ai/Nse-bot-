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
    return 0


if __name__ == "__main__":
    sys.exit(main())
