"""Order flow: only what FYERS really provides, NOT AVAILABLE for the rest, and confirmation-only."""

import dataclasses
import datetime as dt
import os
import unittest

import synth
import test_bot
from helpers import quote, t
from nse_bot import orderflow as OF
from nse_bot.broker.fyers import FyersReadOnly

CFG = {"max_quote_age_seconds": 10, "orderflow_confirmation": "required"}
SRC = open(os.path.join(os.path.dirname(__file__), "..", "nse_bot", "orderflow.py")).read()

# shape copied from the live probe (GitHub run 41417570845), NIFTY future /depth
DEPTH = {"ltp": 23343, "ltq": 65, "ltt": 0, "v": 3043365, "totalbuyqty": 263705, "totalsellqty": 270400,
         "bids": [{"price": 23342.3, "volume": 195, "ord": 1}, {"price": 23342.2, "volume": 65, "ord": 1},
                  {"price": 23340, "volume": 2665, "ord": 3}],
         "ask": [{"price": 23345, "volume": 65, "ord": 1}, {"price": 23346, "volume": 520, "ord": 3}]}


def book(bid_qty, ask_qty, at=None, tb=500000, ts=500000):
    q = quote(bid=100, ask=100.5, at=at or t(10, 0), iid=9)
    return dataclasses.replace(q, source="fyers", extra={"depth": {
        "bids": [(100.0, bid_qty, 3)], "asks": [(100.5, ask_qty, 3)], "total_buy_qty": tb, "total_sell_qty": ts}})


def bars(vols, highs=None, start=t(9, 30)):
    out = []
    for i, v in enumerate(vols):
        h = (highs or [100 + i * 0.1] * len(vols))[i] if highs else 100 + i * 0.1
        out.append((start + dt.timedelta(minutes=i), h - 0.5, h, h - 1, h - 0.2, v, None))
    return out


STEADY = bars([1000] * 23)                                  # new highs on normal volume
EXHAUSTED = bars([1000] * 20 + [300, 300, 300])             # new highs on 30% volume


class Availability(unittest.TestCase):
    def test_unsupported_metrics_are_not_available(self):
        for k in ("candle_buy_sell_volume", "bid_ask_delta", "cvd", "price_delta_divergence", "absorption"):
            self.assertEqual(OF.METRICS[k][0], OF.NOT_AVAILABLE, k)
        for k in ("bid_ask_imbalance", "volume_exhaustion"):
            self.assertEqual(OF.METRICS[k][0], OF.AVAILABLE, k)

    def test_reported_without_numbers(self):
        r = OF.confirm("orb", 1, book(3000, 1000), STEADY, t(10, 0), CFG)
        for k in ("candle_buy_sell_volume", "bid_ask_delta", "cvd", "price_delta_divergence", "absorption"):
            self.assertEqual(set(r["metrics"][k]), {"status", "note"})
            self.assertEqual(r["metrics"][k]["status"], "NOT AVAILABLE")

    def test_no_delta_computed_anywhere(self):
        import ast
        names = {n.id for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Attribute)} | \
                {n.name for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.FunctionDef)}
        for word in ("delta", "cvd", "buy_vol", "sell_vol", "absorb", "diverg"):
            self.assertFalse([x for x in names if word in x.lower() and x != "timedelta"], word)


class Imbalance(unittest.TestCase):
    def test_values(self):
        r = OF.book_imbalance(book(3000, 1000), t(10, 0), 10)
        self.assertEqual((r["status"], r["five_level"], r["total"]), ("AVAILABLE", 0.5, 0.0))

    def test_stale_missing_or_empty_is_not_available(self):
        self.assertEqual(OF.book_imbalance(book(3000, 1000, at=t(9, 59)), t(10, 0), 10)["status"], "NOT AVAILABLE")
        self.assertEqual(OF.book_imbalance(quote(at=t(10, 0)), t(10, 0), 10)["status"], "NOT AVAILABLE")
        self.assertEqual(OF.book_imbalance(None, t(10, 0), 10)["status"], "NOT AVAILABLE")
        self.assertEqual(OF.book_imbalance(book(0, 1000), t(10, 0), 10)["status"], "NOT AVAILABLE")

    def test_fyers_depth_kept_on_the_quote(self):
        md = FyersReadOnly(env={"FYERS_APP_ID": "APP-100", "FYERS_ACCESS_TOKEN": "x"}, now=lambda: t(10, 0))
        q = md.quote_from_depth(1, DEPTH)
        d = q.extra["depth"]
        self.assertEqual(d["bids"][2], (23340.0, 2665, 3))
        self.assertEqual((d["total_buy_qty"], d["total_sell_qty"], d["last_trade_qty"]), (263705, 270400, 65))


class Exhaustion(unittest.TestCase):
    def test_exhausted_and_not(self):
        self.assertTrue(OF.volume_exhaustion(EXHAUSTED, 1)["exhausted"])
        self.assertFalse(OF.volume_exhaustion(STEADY, 1)["exhausted"])
        self.assertFalse(OF.volume_exhaustion(EXHAUSTED, -1)["exhausted"])        # no new low

    def test_missing_volume_or_short_history(self):
        self.assertEqual(OF.volume_exhaustion(bars([None] * 23), 1)["status"], "NOT AVAILABLE")
        self.assertEqual(OF.volume_exhaustion(STEADY[:10], 1)["status"], "NOT AVAILABLE")


class Confirm(unittest.TestCase):
    def test_orb_long_confirmed_by_bid_heavy_book(self):
        self.assertEqual(OF.confirm("orb", 1, book(3000, 1000), STEADY, t(10, 0), CFG)["status"], "CONFIRMED")

    def test_orb_long_rejected_by_ask_heavy_book(self):
        r = OF.confirm("orb", 1, book(1000, 3000), STEADY, t(10, 0), CFG)
        self.assertEqual(r["status"], "NOT CONFIRMED")
        self.assertIn("does not favour the up", r["reasons"][0])

    def test_orb_put_mirror(self):
        lows = bars([1000] * 23)
        self.assertEqual(OF.confirm("orb", -1, book(1000, 3000), lows, t(10, 0), CFG)["status"], "CONFIRMED")

    def test_orb_exhausted_breakout_rejected(self):
        r = OF.confirm("orb", 1, book(3000, 1000), EXHAUSTED, t(10, 0), CFG)
        self.assertEqual(r["status"], "NOT CONFIRMED")
        self.assertIn("exhausted", r["reasons"][0])

    def test_whole_book_strongly_against_vetoes(self):
        r = OF.confirm("orb", 1, book(3000, 1000, tb=100000, ts=900000), STEADY, t(10, 0), CFG)
        self.assertEqual(r["status"], "NOT CONFIRMED")

    def test_sweep_reversal_uses_exhaustion_of_the_swept_move(self):
        # bullish reversal after a sweep of the lows: the down move ran out of volume
        falling = [(t(9, 30) + dt.timedelta(minutes=i), 100 - i * 0.1, 100.5 - i * 0.1, 99 - i * 0.1, 99.8 - i * 0.1,
                    1000 if i < 20 else 300, None) for i in range(23)]
        r = OF.confirm("sweep", 1, book(3000, 1000), falling, t(10, 0), CFG)
        self.assertEqual(r["status"], "CONFIRMED")
        self.assertIn("supports the reversal", r["note"])

    def test_missing_data_is_not_available_never_a_pass(self):
        r = OF.confirm("orb", 1, None, STEADY, t(10, 0), CFG)
        self.assertEqual(r["status"], "NOT AVAILABLE")


class FakeCtx(object):
    def __init__(self, q, rows, fut=True, cfg=None):
        self.cfg = dict(CFG, **(cfg or {}))
        fut_obj = type("F", (), {"instrument_id": 9, "symbol": "NIFTY26NOVFUT"})() if fut else None
        self.master = type("M", (), {"current_future": lambda s, u, d: fut_obj})()
        self.md = type("MD", (), {"quotes": lambda s, ids: {9: q}, "candles": lambda s, *a, **k: rows})()


class Wiring(unittest.TestCase):
    def test_off_changes_nothing(self):
        res, problems = OF.for_entry(FakeCtx(None, [], cfg={"orderflow_confirmation": "off"}), "orb", "NIFTY", "CE", t(10, 0))
        self.assertEqual((res["status"], problems), ("OFF", []))

    def test_confirmed_adds_no_problem_and_rejection_blocks(self):
        now = STEADY[-1][0] + dt.timedelta(minutes=1)
        ok, p = OF.for_entry(FakeCtx(book(3000, 1000, at=now), STEADY), "orb", "NIFTY", "CE", now)
        self.assertEqual((ok["status"], p), ("CONFIRMED", []))
        bad, p = OF.for_entry(FakeCtx(book(1000, 3000, at=now), STEADY), "orb", "NIFTY", "CE", now)
        self.assertTrue(p and p[0].startswith("order flow NOT CONFIRMED"))

    def test_open_minute_bar_is_ignored(self):
        forming = (STEADY[-1][0] + dt.timedelta(minutes=1), 105, 110, 104, 109, 1, None)   # new high, tiny volume
        now = forming[0] + dt.timedelta(seconds=30)
        res, _ = OF.for_entry(FakeCtx(book(3000, 1000, at=now), STEADY + [forming]), "orb", "NIFTY", "CE", now)
        self.assertFalse(res["metrics"]["volume_exhaustion"]["exhausted"])
        self.assertEqual(res["status"], "CONFIRMED")

    def test_no_future_is_not_available(self):
        res, p = OF.for_entry(FakeCtx(None, [], fut=False), "sweep", "NIFTY", "PE", t(10, 0))
        self.assertEqual(res["status"], "NOT AVAILABLE")
        self.assertTrue(p)


class OrbIntegration(unittest.TestCase):
    """An approved ORB entry that passes every other check is still blocked when order flow is unavailable."""
    approve = test_bot.Cycle.approve
    tearDown = test_bot.Cycle.tearDown
    ctx = test_bot.Cycle.ctx

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["max_lots_per_trade"] = 1

    def run_at(self, mode):
        from nse_bot import bot
        self.cfg["orderflow_confirmation"] = mode
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        return bot.run_once(self.ctx(t(9, 41)), t(9, 41))["candidates"][0]

    def test_required_without_futures_data_blocks(self):
        cand = self.run_at("required")
        self.assertEqual(cand["risk"]["verdict"], "BLOCK")
        self.assertTrue(any("order flow" in r for r in cand["risk"]["reasons"]), cand["risk"]["reasons"])
        self.assertEqual(cand["orderflow"]["status"], "NOT AVAILABLE")
        self.assertEqual([x for x in self.broker.ledger.records() if x["event"] == "fill"], [])

    def test_off_leaves_the_entry_unchanged(self):
        cand = self.run_at("off")
        self.assertEqual(cand["risk"]["verdict"], "PASS", cand["risk"]["reasons"])


if __name__ == "__main__":
    unittest.main()
