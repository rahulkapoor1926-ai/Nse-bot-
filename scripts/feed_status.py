"""Summary of the dashboard's /api/market answer (read on stdin) for the status file: which feed each
quote came from, its age and STALE flags. Prints JSON; never a token or key (none is in the input)."""
import collections
import json
import sys

m = json.load(sys.stdin)
opts = m.get("options") or []
print(json.dumps({
    "available": m.get("available"), "reason": m.get("reason"),
    "websocket": m.get("websocket"),
    "spot": m.get("spot"), "spot_source": m.get("spot_source"), "spot_time": m.get("spot_time"),
    "spot_age_s": m.get("spot_age_s"), "spot_status": m.get("spot_status"),
    "options": len(opts),
    "options_by_source": dict(collections.Counter(o.get("source") for o in opts)),
    "options_by_status": dict(collections.Counter(o.get("status") for o in opts)),
    "option_quotes": [{k: o.get(k) for k in ("symbol", "moneyness", "ltp", "bid", "ask", "volume", "oi",
                                              "quote_time", "age_s", "source", "status")} for o in opts],
}))
