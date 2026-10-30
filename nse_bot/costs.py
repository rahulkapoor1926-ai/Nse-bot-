"""Indian index-option transaction charges, by the date of the trade.

All rates come from nse_bot/data/costs_india.json with their sources. A trade
dated before the earliest schedule raises DataError: an old backtest is not
silently charged today's (or zero) costs.

Spread and slippage are not here: they come from the fill model (bid/ask plus
slippage ticks), so they are never double counted.
"""

import datetime as dt
import json

from .errors import DataError

FIELDS = ("brokerage_per_order_inr", "stt_sell_pct", "exchange_txn_pct", "sebi_pct",
          "ipft_pct", "stamp_buy_pct", "gst_pct")


class CostModel(object):
    def __init__(self, schedules):
        if not schedules:
            raise DataError("no cost schedules")
        out = []
        for s in schedules:
            for k in FIELDS + ("effective_from",):
                if k not in s:
                    raise DataError("cost schedule is missing %s" % k)
            for k in FIELDS:
                v = s[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise DataError("cost field %s must be a non-negative number, got %r" % (k, v))
            out.append((dt.date.fromisoformat(s["effective_from"]), s))
        self.schedules = sorted(out, key=lambda x: x[0], reverse=True)

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f)["schedules"])
        except (OSError, ValueError, KeyError) as e:
            raise DataError("cannot read cost file %s: %s" % (path, e))

    def schedule(self, day):
        for start, s in self.schedules:
            if day >= start:
                return s
        raise DataError("no cost schedule covers %s (earliest is %s)" % (day, self.schedules[-1][0]))

    def leg(self, side, price, qty, day):
        """Charges for one executed order leg. side is 'BUY' or 'SELL'; price is per unit in rupees."""
        if side not in ("BUY", "SELL"):
            raise DataError("side must be BUY or SELL")
        if price is None or price < 0 or qty is None or qty <= 0:
            raise DataError("leg needs a real price and a positive quantity (price=%r qty=%r)" % (price, qty))
        s = self.schedule(day)
        turnover = price * qty
        brokerage = float(s["brokerage_per_order_inr"])
        stt = turnover * s["stt_sell_pct"] / 100.0 if side == "SELL" else 0.0
        exch = turnover * s["exchange_txn_pct"] / 100.0
        sebi = turnover * s["sebi_pct"] / 100.0
        ipft = turnover * s["ipft_pct"] / 100.0
        stamp = turnover * s["stamp_buy_pct"] / 100.0 if side == "BUY" else 0.0
        gst = (brokerage + exch + sebi + ipft) * s["gst_pct"] / 100.0
        parts = {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi,
                 "ipft": ipft, "stamp": stamp, "gst": gst}
        parts = {k: round(v, 4) for k, v in parts.items()}
        parts["total"] = round(sum(parts.values()), 2)
        parts["turnover"] = round(turnover, 2)
        return parts

    def round_trip(self, buy_price, sell_price, qty, day):
        b = self.leg("BUY", buy_price, qty, day)
        s = self.leg("SELL", sell_price, qty, day)
        return {"buy": b, "sell": s, "total": round(b["total"] + s["total"], 2)}

    def per_lot_estimate(self, premium, lot_size, day):
        """Charges for a 1-lot round trip at the same premium, used for sizing before a trade."""
        return self.round_trip(premium, premium, lot_size, day)["total"]
