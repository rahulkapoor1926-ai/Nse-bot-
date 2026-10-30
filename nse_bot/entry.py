"""Entry candidates: a scanner signal becomes CANDIDATE or REJECTED with explicit reasons.

Long options only (buy CE or PE). Selling options needs margin far above the
Rs 20,000 capital assumption and has unbounded risk, so it is not offered.
A CANDIDATE is not an approval: the validation gate, budget and risk still run.
"""

from . import clock, events, quotes
from .errors import CalendarError


def candidate(scan_row, quote, now, cfg, cal, event_list=(), received_at=None):
    now = clock.to_ist(now)
    out = {"underlying": scan_row.get("underlying"), "strategy": scan_row.get("strategy"),
           "status": "REJECTED", "reasons": [], "invalidation": [], "time": now.isoformat()}
    if scan_row.get("status") != "SIGNAL":
        out["reasons"].append("no signal (%s)" % scan_row.get("status"))
        return out
    sig, c = scan_row["signal"], scan_row["contract"]
    out["contract"] = c.to_dict()
    out["signal"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in sig.items()}
    why = out["reasons"]
    try:
        if not cal.is_trading_day(now.date()):
            why.append("not an NSE trading day")
    except CalendarError as e:
        why.append(str(e))
    d = now.date()
    if now < clock.at(d, cfg["no_entries_before"]):
        why.append("before the entry window (%s)" % cfg["no_entries_before"])
    if now > clock.at(d, cfg["no_entries_after"]):
        why.append("after the entry window (%s)" % cfg["no_entries_after"])
    if c.expiry == d and now > clock.at(d, cfg["expiry_day_flat_by"]):
        why.append("expiry day after %s" % cfg["expiry_day_flat_by"])
    if (now - sig["known_at"]).total_seconds() > 2 * 60 * 60:
        why.append("signal is more than 2 hours old")
    blocked = events.blackout(event_list, now, cfg["event_blackout_minutes"])
    if blocked:
        why.append("scheduled event blackout: %s" % blocked["title"])
    problems = quotes.check(quote, now, cfg, c.lot_size, received_at)
    why.extend("quote: " + p for p in problems)
    if not why:
        out["status"] = "CANDIDATE"
        out["entry_price"] = quote.ask          # a buy pays the ask, never the mid
        out["quote"] = {"bid": quote.bid, "ask": quote.ask, "ltp": quote.ltp,
                        "ts": quote.ts.isoformat(), "spread_pct": round(quote.spread_pct(), 3)}
        out["reasons_for"] = [
            "%s %s on %s: bar close %.2f, known at %s IST" % (
                sig["pattern"], "breakout up" if c.option_type == "CE" else "breakdown",
                c.underlying, sig["trigger_close"], sig["known_at"].strftime("%H:%M")),
            "contract %s, expiry %s, strike %.0f %s" % (c.symbol, c.expiry, c.strike, c.option_type),
        ]
    inv = out["invalidation"]
    if sig.get("invalidate_below") is not None:
        inv.append("%s closes below %.2f" % (c.underlying, sig["invalidate_below"]))
    if sig.get("invalidate_above") is not None:
        inv.append("%s closes above %.2f" % (c.underlying, sig["invalidate_above"]))
    inv.append("time passes %s without a fill" % cfg["no_entries_after"])
    inv.append("quote becomes stale (> %ss), crossed, or spread > %s%%" % (
        cfg["max_quote_age_seconds"], cfg["max_spread_pct"]))
    return out
