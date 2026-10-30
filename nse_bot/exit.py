"""Exit planning and exit decisions for a long option position.

plan():     stop, target and time exit, fixed BEFORE entry.
evaluate(): what to do now given a quote. Exits are judged on the BID (what a
            sale would get). A stale or missing quote never counts as an exit:
            it returns NO_PRICE so the caller alerts and retries.

Gaps: if the bid is already below the stop, the exit is at that bid and the
loss is larger than planned. That is reported, not hidden.
"""

import math

from . import clock
from .errors import DataError


def _tick_floor(x, tick):
    return round(math.floor(x / tick + 1e-9) * tick, 2)


def _tick_ceil(x, tick):
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def plan(entry_price, contract, strategy, cfg, day):
    if entry_price is None or entry_price <= 0:
        raise DataError("cannot plan an exit without a real entry price")
    tick = contract.tick_size
    stop = _tick_ceil(entry_price * (1 - strategy["stop_pct"] / 100.0), tick)
    if stop >= entry_price:
        stop = _tick_floor(entry_price - tick, tick)
    if stop <= 0:
        raise DataError("stop %.2f is not a valid premium" % stop)
    risk_pts = entry_price - stop
    target = _tick_ceil(entry_price + strategy["target_r"] * risk_pts, tick)
    flat = cfg["expiry_day_flat_by"] if contract.expiry == day else cfg["flat_by"]
    return {
        "entry": entry_price, "stop": stop, "target": target,
        "risk_points": round(risk_pts, 2), "reward_points": round(target - entry_price, 2),
        "reward_risk": round((target - entry_price) / risk_pts, 3),
        "flat_by": flat, "expiry": contract.expiry.isoformat() if contract.expiry else None,
        "note": "stop and target are on the %s, judged on the bid; a gap or fast market can fill "
                "below the stop" % ("option premium" if contract.expiry else "share price"),
    }


def evaluate(position, quote, now, cfg, underlying_close=None):
    """Return {"action": HOLD|EXIT|NO_PRICE, "reason", "price"}."""
    now = clock.to_ist(now)
    p = position["plan"]
    flat_at = clock.at(now.date(), p["flat_by"])
    bid, problems = None, []
    if quote is None:
        problems = ["no quote"]
    else:
        from .quotes import usable_for_exit
        bid, problems = usable_for_exit(quote, now, cfg)
    if bid is None:
        reason = "cannot evaluate exit: " + "; ".join(problems)
        if now >= flat_at:
            reason += " (time exit is due: escalate, do not assume flat)"
        return {"action": "NO_PRICE", "reason": reason, "price": None}
    inv_b = position.get("invalidate_below")
    inv_a = position.get("invalidate_above")
    if bid <= p["stop"]:
        gap = bid < p["stop"]
        return {"action": "EXIT", "reason": "stop" + (" (gapped through: bid %.2f below stop %.2f)" % (bid, p["stop"]) if gap else ""),
                "price": bid, "gap": gap}
    if bid >= p["target"]:
        return {"action": "EXIT", "reason": "target", "price": bid}
    if now >= flat_at:
        return {"action": "EXIT", "reason": "time exit at %s" % p["flat_by"], "price": bid}
    if underlying_close is not None:
        if inv_b is not None and underlying_close < inv_b:
            return {"action": "EXIT", "reason": "invalidated: underlying %.2f < %.2f" % (underlying_close, inv_b), "price": bid}
        if inv_a is not None and underlying_close > inv_a:
            return {"action": "EXIT", "reason": "invalidated: underlying %.2f > %.2f" % (underlying_close, inv_a), "price": bid}
    return {"action": "HOLD", "reason": "inside plan", "price": bid}
