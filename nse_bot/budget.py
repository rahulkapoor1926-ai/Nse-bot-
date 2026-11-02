"""Lot sizing from capital, premium outlay, stop distance and estimated charges.

lots = the largest whole number of lots such that
  (a) loss at the stop + round-trip charges <= the risk budget for this trade, and
  (b) premium outlay + charges <= max_premium_outlay_pct of capital (and of cash),
  (c) lots <= max_lots_per_trade and quantity <= the exchange freeze quantity.
Zero lots is a refusal with the reason, never rounded up to one.

The risk budget is min(max_risk_per_trade_inr, what is left of the daily loss
limit). It never grows after a loss.
"""

from .errors import DataError


def size(entry_price, stop, contract, cfg, costs, day, remaining_daily_loss, cash=None):
    if getattr(contract, "kind", "index_options") != "index_options":
        return size_shares(entry_price, stop, contract, cfg, costs, day, remaining_daily_loss, cash)
    if entry_price is None or stop is None or entry_price <= 0 or stop <= 0:
        raise DataError("sizing needs a real entry and stop")
    if stop >= entry_price:
        raise DataError("stop %.2f must be below entry %.2f for a long option" % (stop, entry_price))
    lot = contract.lot_size
    risk_budget = min(cfg["max_risk_per_trade_inr"], max(0.0, remaining_daily_loss))
    cash = cfg["capital_inr"] if cash is None else cash
    outlay_cap = min(cfg["capital_inr"], cash) * cfg["max_premium_outlay_pct"] / 100.0
    out = {"lot_size": lot, "risk_budget_inr": round(risk_budget, 2),
           "outlay_cap_inr": round(outlay_cap, 2), "lots": 0, "reasons": []}
    best = None
    for n in range(1, cfg["max_lots_per_trade"] + 1):
        qty = n * lot
        if contract.freeze_quantity and qty > contract.freeze_quantity:
            out["reasons"].append("%d lots exceed the freeze quantity %d" % (n, contract.freeze_quantity))
            break
        charges_at_stop = costs.round_trip(entry_price, stop, qty, day, contract.cost_segment)["total"]
        loss_at_stop = (entry_price - stop) * qty + charges_at_stop
        buy_charges = costs.leg("BUY", entry_price, qty, day, contract.cost_segment)["total"]
        outlay = entry_price * qty + buy_charges
        if loss_at_stop > risk_budget:
            out["reasons"].append("%d lot(s): loss at stop Rs %.2f (incl. Rs %.2f charges) > risk budget Rs %.2f"
                                  % (n, loss_at_stop, charges_at_stop, risk_budget))
            break
        if outlay > outlay_cap:
            out["reasons"].append("%d lot(s): premium outlay Rs %.2f > cap Rs %.2f" % (n, outlay, outlay_cap))
            break
        best = {"lots": n, "quantity": qty, "outlay_inr": round(outlay, 2),
                "loss_at_stop_inr": round(loss_at_stop, 2), "charges_round_trip_inr": charges_at_stop}
    if best:
        out.update(best)
        out["reasons"] = []
    out["warning"] = ("Loss at stop assumes a fill at the stop price. Slippage and gaps can make the "
                      "actual loss larger than this and larger than the daily limit.")
    return out


def entry_fill(cfg, c, ask):
    """The price a paper buy really pays: the ask plus slippage ticks (broker/paper.py fills the same way)."""
    return round(ask + cfg["slippage_ticks"] * c.tick_size, 2)


def size_at_fills(cfg, costs, c, fill, stop, day, remaining_loss):
    """Position size on the prices a trade would really get: entry at `fill` (ask + slippage, see entry_fill)
    and a stop exit at the stop (judged on the bid) minus slippage, with both legs' charges. The loss at the
    stop must fit the per-trade risk (Rs 500) and what is left of the daily limit, else zero lots: never a
    wider stop or a bigger budget. The live bot and the ORB and equity backtests all call this function."""
    slip = cfg["slippage_ticks"] * c.tick_size
    return size(fill, round(stop - slip, 2), c, cfg, costs, day, remaining_loss)


def size_shares(entry_price, stop, stock, cfg, costs, day, remaining_daily_loss, cash=None):
    """Whole shares of a stock or ETF, cash only (no intraday leverage is used).

    Same rules as for options: loss at the stop plus round-trip charges within the
    risk budget, outlay within max_premium_outlay_pct of capital. Zero is a refusal.
    """
    if entry_price is None or stop is None or entry_price <= 0 or stop <= 0:
        raise DataError("sizing needs a real entry and stop")
    if stop >= entry_price:
        raise DataError("stop %.2f must be below entry %.2f for a long position" % (stop, entry_price))
    seg = stock.cost_segment
    risk_budget = min(cfg["max_risk_per_trade_inr"], max(0.0, remaining_daily_loss))
    cash = cfg["capital_inr"] if cash is None else cash
    outlay_cap = min(cfg["capital_inr"], cash) * cfg["max_premium_outlay_pct"] / 100.0
    out = {"lot_size": 1, "risk_budget_inr": round(risk_budget, 2), "outlay_cap_inr": round(outlay_cap, 2),
           "lots": 0, "quantity": 0, "reasons": []}

    def loss(q):
        return (entry_price - stop) * q + costs.round_trip(entry_price, stop, q, day, seg)["total"]

    def outlay(q):
        return entry_price * q + costs.leg("BUY", entry_price, q, day, seg)["total"]

    q = int(min(risk_budget / (entry_price - stop), outlay_cap / entry_price))
    while q > 0 and (loss(q) > risk_budget or outlay(q) > outlay_cap):
        q -= 1
    if q <= 0:
        if loss(1) > risk_budget:
            out["reasons"].append("1 share: loss at stop Rs %.2f (incl. charges) > risk budget Rs %.2f"
                                  % (loss(1), risk_budget))
        else:
            out["reasons"].append("1 share: outlay Rs %.2f > cap Rs %.2f" % (outlay(1), outlay_cap))
    else:
        out.update({"lots": q, "quantity": q, "outlay_inr": round(outlay(q), 2),
                    "loss_at_stop_inr": round(loss(q), 2),
                    "charges_round_trip_inr": costs.round_trip(entry_price, stop, q, day, seg)["total"]})
    out["warning"] = ("Loss at stop assumes a fill at the stop price. Slippage and gaps can make the "
                      "actual loss larger than this and larger than the daily limit.")
    return out
