"""Indian transaction charges by segment and by the date of the trade.

Segments: index_options, equity_intraday, equity_delivery (ETFs use the equity
segments). All rates come from nse_bot/data/costs_india.json with their
sources. A trade dated before the earliest schedule of its segment raises
DataError: an old backtest is not silently charged today's (or zero) costs.

Spread and slippage are not here: they come from the fill model (bid/ask plus
slippage ticks), so they are never double counted.
"""

import datetime as dt
import json

from .errors import DataError

SEGMENTS = ("index_options", "equity_intraday", "equity_delivery")
FIELDS = ("brokerage_per_order_inr", "stt_sell_pct", "exchange_txn_pct", "sebi_pct",
          "ipft_pct", "stamp_buy_pct", "gst_pct")
OPTIONAL = ("brokerage_pct", "stt_buy_pct", "dp_per_sell_inr")


class CostModel(object):
    def __init__(self, schedules):
        if not schedules:
            raise DataError("no cost schedules")
        self.by_segment = {}
        for s in schedules:
            seg = s.get("segment", "index_options")
            if seg not in SEGMENTS:
                raise DataError("unknown cost segment %r" % seg)
            for k in FIELDS + ("effective_from",):
                if k not in s:
                    raise DataError("cost schedule is missing %s" % k)
            for k in FIELDS + tuple(x for x in OPTIONAL if x in s):
                v = s[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise DataError("cost field %s must be a non-negative number, got %r" % (k, v))
            if s.get("exchange_sides", "both") not in ("both", "sell"):
                raise DataError("exchange_sides must be both or sell")
            self.by_segment.setdefault(seg, []).append((dt.date.fromisoformat(s["effective_from"]), s))
        for seg in self.by_segment:
            self.by_segment[seg].sort(key=lambda x: x[0], reverse=True)

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f)["schedules"])
        except (OSError, ValueError, KeyError) as e:
            raise DataError("cannot read cost file %s: %s" % (path, e))

    def schedule(self, day, segment="index_options"):
        rows = self.by_segment.get(segment)
        if not rows:
            raise DataError("no cost schedule for segment %s" % segment)
        for start, s in rows:
            if day >= start:
                return s
        raise DataError("no %s cost schedule covers %s (earliest is %s)" % (segment, day, rows[-1][0]))

    def leg(self, side, price, qty, day, segment="index_options"):
        """Charges for one executed order leg. side is 'BUY' or 'SELL'; price is per unit in rupees."""
        if side not in ("BUY", "SELL"):
            raise DataError("side must be BUY or SELL")
        if price is None or price < 0 or qty is None or qty <= 0:
            raise DataError("leg needs a real price and a positive quantity (price=%r qty=%r)" % (price, qty))
        s = self.schedule(day, segment)
        turnover = price * qty
        brokerage = float(s["brokerage_per_order_inr"])
        if "brokerage_pct" in s:
            brokerage = min(brokerage, turnover * s["brokerage_pct"] / 100.0)
        stt_pct = s["stt_sell_pct"] if side == "SELL" else s.get("stt_buy_pct", 0.0)
        stt = turnover * stt_pct / 100.0
        exch = turnover * s["exchange_txn_pct"] / 100.0
        if s.get("exchange_sides", "both") == "sell" and side == "BUY":
            exch = 0.0
        sebi = turnover * s["sebi_pct"] / 100.0
        ipft = turnover * s["ipft_pct"] / 100.0
        stamp = turnover * s["stamp_buy_pct"] / 100.0 if side == "BUY" else 0.0
        dp = float(s.get("dp_per_sell_inr", 0.0)) if side == "SELL" else 0.0
        gst = (brokerage + exch + sebi + ipft + dp) * s["gst_pct"] / 100.0
        parts = {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi,
                 "ipft": ipft, "stamp": stamp, "dp": dp, "gst": gst}
        parts = {k: round(v, 4) for k, v in parts.items()}
        parts["total"] = round(sum(parts.values()), 2)
        parts["turnover"] = round(turnover, 2)
        return parts

    def round_trip(self, buy_price, sell_price, qty, day, segment="index_options"):
        b = self.leg("BUY", buy_price, qty, day, segment)
        s = self.leg("SELL", sell_price, qty, day, segment)
        return {"buy": b, "sell": s, "total": round(b["total"] + s["total"], 2)}

    def per_lot_estimate(self, premium, lot_size, day, segment="index_options"):
        """Charges for a 1-lot round trip at the same price, used for sizing before a trade."""
        return self.round_trip(premium, premium, lot_size, day, segment)["total"]
