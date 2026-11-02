"""Extra entry checks on the data feed itself, added for the FYERS switch.

* One source per session: a quote tagged with a different source than the session's
  `data_source` is refused, so SAHI and FYERS data are never mixed silently.
* Spot freshness (adapters that set `verifies_spot`, i.e. FYERS): the index quote must
  carry its own recent last-trade time and agree with the price the signal was computed
  from. Candle freshness is never taken as quote freshness.
Any problem returned here blocks the entry; nothing is retried with another source.
"""

from . import quotes
from .errors import NseBotError

SPOT_TOLERANCE_PCT = 0.5


def entry_problems(ctx, underlying, q, ref_spot, now, kind="index_options"):
    p = []
    want = ctx.cfg.get("data_source")
    if q is not None and q.source and want and q.source != want:
        p.append("quote came from %s but this session uses %s: refusing to mix sources" % (q.source, want))
    if kind != "index_options" or not getattr(ctx.md, "verifies_spot", False):
        return p
    try:
        iid = ctx.master.index_id(ctx.cfg["index_names"][underlying])
        s = ctx.md.quotes([iid]).get(iid)
    except (NseBotError, KeyError) as e:
        return p + ["spot quote unavailable: %s" % e]
    if s is None or s.ltp is None:
        return p + ["no spot quote for %s" % underlying]
    if want and s.source and s.source != want:
        p.append("spot quote came from %s but this session uses %s" % (s.source, want))
    if s.ts is None:
        p.append("spot quote has no last-trade time: freshness cannot be verified")
    else:
        age = (quotes._reference(s, now) - s.ts).total_seconds()
        if age > ctx.cfg["max_quote_age_seconds"]:
            p.append("spot quote stale: %.0fs old (max %ss)" % (age, ctx.cfg["max_quote_age_seconds"]))
    if ref_spot:
        diff = abs(s.ltp - ref_spot) / ref_spot * 100
        if diff > SPOT_TOLERANCE_PCT:
            p.append("spot %.2f differs from the signal's price %.2f by %.2f%% (max %.1f%%)"
                     % (s.ltp, ref_spot, diff, SPOT_TOLERANCE_PCT))
    return p
