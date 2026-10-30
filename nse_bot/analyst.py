"""Market context for NIFTY/BANKNIFTY option trades. Informational only.

Upstream trader-analyst reads SEC earnings filings for single US stocks. For
index options the useful context is different: scheduled macro events (RBI
policy, budget, CPI/GDP), heavyweight bank results for BANKNIFTY, days to
expiry (theta), and implied volatility. This module turns those inputs into
notes. It returns affects_trading=False: only the event blackout in entry.py
and the risk checks can stop a trade, so context never quietly changes a
decision.
"""

from . import events


def context(underlying, now, expiry, event_list=(), iv=None, iv_history=None):
    notes = []
    dte = (expiry - now.date()).days
    if dte == 0:
        notes.append("expiry day: option premium decays fastest today; gamma risk is highest")
    elif dte <= 2:
        notes.append("%d day(s) to expiry: time decay is steep" % dte)
    for ev in events.upcoming(event_list, now, hours=48):
        if underlying == "NIFTY" or ev.get("affects") in (None, underlying, "ALL"):
            notes.append("%s event %s at %s IST (%s)" % (ev["impact"], ev["title"],
                                                         ev["time"].strftime("%d %b %H:%M"), ev["source_url"]))
    pct = None
    if iv is not None and iv_history:
        below = sum(1 for x in iv_history if x <= iv)
        pct = round(100.0 * below / len(iv_history), 1)
        notes.append("implied volatility %.1f%% is at the %.0fth percentile of the %d supplied readings"
                     % (iv, pct, len(iv_history)))
    elif iv is not None:
        notes.append("implied volatility %.1f%% (no history supplied for comparison)" % iv)
    return {"underlying": underlying, "days_to_expiry": dte, "iv_percentile": pct,
            "notes": notes, "affects_trading": False}
