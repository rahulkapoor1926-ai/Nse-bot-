"""Paper broker: simulated fills from real bid/ask, with a persistent, hash-chained ledger.

Fill rules (no mid-price fills):
  BUY  at the ask + slippage_ticks * tick, at most the displayed ask quantity
  SELL at the bid - slippage_ticks * tick, at most the displayed bid quantity
Quantities are whole lots. A partial fill leaves the rest unfilled (entry) or
still open (exit). A missing or unusable quote is a FAILED order: an exit that
failed leaves the position open and says so. Charges come from CostModel.

Every event is appended to <state_dir>/paper/ledger.jsonl with the SHA-256 of
the previous line, so truncation or edits are detected by verify().
"""

import hashlib
import json
import os

from .. import clock, state
from ..errors import StateCorrupt

GENESIS = "0" * 64
LABEL = "SIMULATED (paper)"


def _h(rec):
    return hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()


class Ledger(object):
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "paper", "ledger.jsonl")

    def records(self):
        return state.read_jsonl(self.path)

    def verify(self):
        prev = GENESIS
        recs = self.records()
        for i, r in enumerate(recs, 1):
            body = {k: v for k, v in r.items() if k != "hash"}
            if r.get("prev") != prev or _h(body) != r.get("hash"):
                raise StateCorrupt("paper ledger broken at record %d: hash chain mismatch" % i)
            prev = r["hash"]
        return len(recs)

    def append(self, rec):
        recs = self.records()
        prev = recs[-1]["hash"] if recs else GENESIS
        body = dict(rec, prev=prev, label=LABEL)
        body["hash"] = _h(body)
        state.append_jsonl(self.path, body)
        return body

    def positions(self):
        """{instrument_id: {"qty", "avg_price", "entry_charges", "plan", ...}} from the fills."""
        pos = {}
        for r in self.records():
            if r["event"] != "fill":
                continue
            iid = str(r["instrument_id"])
            p = pos.setdefault(iid, {"qty": 0, "cost": 0.0, "charges": 0.0, "realized": 0.0})
            if r["side"] == "BUY":
                p["qty"] += r["qty"]
                p["cost"] += r["price"] * r["qty"]
                p["charges"] += r["charges"]["total"]
                p["meta"] = r.get("meta", {})
            else:
                avg = p["cost"] / p["qty"] if p["qty"] else 0.0
                share = r["qty"] / p["qty"] if p["qty"] else 0.0
                entry_ch = p["charges"] * share
                p["realized"] += (r["price"] - avg) * r["qty"] - r["charges"]["total"] - entry_ch
                p["cost"] -= avg * r["qty"]
                p["charges"] -= entry_ch
                p["qty"] -= r["qty"]
        return pos


class PaperBroker(object):
    def __init__(self, state_dir, costs, slippage_ticks):
        self.ledger = Ledger(state_dir)
        self.costs = costs
        self.slip = slippage_ticks

    def _fail(self, now, side, iid, reason, meta=None):
        self.ledger.append({"event": "order_failed", "time": clock.to_ist(now).isoformat(),
                            "side": side, "instrument_id": iid, "reason": reason, "meta": meta or {}})
        return {"status": "FAILED", "reason": reason, "filled_qty": 0}

    def _fill(self, side, iid, want_qty, lot, tick, quote, now, meta):
        if quote is None:
            return self._fail(now, side, iid, "no quote (network or feed failure)", meta)
        px = quote.ask if side == "BUY" else quote.bid
        avail = quote.ask_qty if side == "BUY" else quote.bid_qty
        if px is None or px <= 0 or avail is None:
            return self._fail(now, side, iid, "no usable %s price" % ("ask" if side == "BUY" else "bid"), meta)
        fill_px = px + self.slip * tick if side == "BUY" else px - self.slip * tick
        fill_px = round(fill_px, 2)
        if fill_px <= 0:
            return self._fail(now, side, iid, "price after slippage is not positive", meta)
        qty = min(want_qty, (avail // lot) * lot)
        if qty <= 0:
            return self._fail(now, side, iid, "displayed size %s below one lot (%d)" % (avail, lot), meta)
        ch = self.costs.leg(side, fill_px, qty, clock.to_ist(now).date())
        rec = self.ledger.append({"event": "fill", "time": clock.to_ist(now).isoformat(), "side": side,
                                  "instrument_id": iid, "qty": qty, "price": fill_px,
                                  "quote_bid": quote.bid, "quote_ask": quote.ask,
                                  "charges": ch, "meta": meta or {}})
        return {"status": "FILLED" if qty == want_qty else "PARTIAL", "filled_qty": qty,
                "unfilled_qty": want_qty - qty, "price": fill_px, "charges": ch, "record": rec["hash"]}

    def buy(self, contract, lots, quote, now, meta=None):
        return self._fill("BUY", contract.instrument_id, lots * contract.lot_size, contract.lot_size,
                          contract.tick_size, quote, now, meta)

    def sell(self, contract, qty, quote, now, meta=None):
        held = self.ledger.positions().get(str(contract.instrument_id), {}).get("qty", 0)
        if qty > held:
            return self._fail(now, "SELL", contract.instrument_id, "sell %d exceeds held %d" % (qty, held), meta)
        return self._fill("SELL", contract.instrument_id, qty, contract.lot_size, contract.tick_size,
                          quote, now, meta)
