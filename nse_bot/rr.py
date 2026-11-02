"""Net reward:risk after every cost, for the minimum 1:2 rule (both ORB and sweep_reversal).

All inputs are the prices the paper broker would actually get:
  entry_fill   the ASK plus slippage (so the bid/ask spread is paid on the way in)
  stop_fill    the exit at the stop on the BID minus slippage (sweep: the conservative sizing stop,
               which also allows for an IV drop, theta and a gap)
  target_fill  the exit at the target on the BID minus slippage

  net risk   = (entry_fill - stop_fill) * qty + entry charges + exit charges at the stop
  net reward = (target_fill - entry_fill) * qty - entry charges - exit charges at the target

Charges (brokerage, STT, exchange, SEBI, stamp, GST) come from the same CostModel as the paper
ledger. A ratio below the configured minimum (never below 2.0) means NO TRADE. Nothing here moves
a stop or a target; it only measures the plan the strategy already made.
"""

from .errors import DataError

FLOOR = 2.0


def net(entry_fill, stop_fill, target_fill, qty, costs, day, segment="index_options"):
    if None in (entry_fill, stop_fill, target_fill) or qty is None or qty <= 0:
        raise DataError("net reward:risk needs real entry, stop and target prices and a quantity")
    if not 0 < stop_fill < entry_fill < target_fill:
        return {"net_reward_risk": 0.0, "net_risk_inr": None, "net_reward_inr": None,
                "reason": "prices not ordered stop %.2f < entry %.2f < target %.2f" % (stop_fill, entry_fill, target_fill)}
    buy = costs.leg("BUY", entry_fill, qty, day, segment)["total"]
    at_stop = costs.leg("SELL", stop_fill, qty, day, segment)["total"]
    at_target = costs.leg("SELL", target_fill, qty, day, segment)["total"]
    risk = (entry_fill - stop_fill) * qty + buy + at_stop
    reward = (target_fill - entry_fill) * qty - buy - at_target
    return {"net_reward_risk": round(reward / risk, 3), "net_risk_inr": round(risk, 2),
            "net_reward_inr": round(reward, 2), "entry_fill": entry_fill, "stop_fill": stop_fill,
            "target_fill": target_fill, "qty": qty, "charges_inr": {"entry": buy, "exit_at_stop": at_stop,
                                                                     "exit_at_target": at_target}}


def minimum(cfg):
    return max(FLOOR, float(cfg["min_reward_risk"]))


def ok(result, cfg):
    return result is not None and result.get("net_reward_risk", 0) >= minimum(cfg)


def why(result, cfg):
    return "net reward:risk %.2f < %.2f after charges, spread and slippage: NO TRADE" % (
        (result or {}).get("net_reward_risk", 0), minimum(cfg))
