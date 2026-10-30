import datetime as dt
import unittest

import synth
from nse_bot.costs import CostModel
from nse_bot.errors import DataError


class Costs(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel.load(synth.COSTS)

    def test_round_trip_after_budget_2026(self):
        r = self.cm.round_trip(150, 150, 65, dt.date(2026, 10, 30))
        self.assertAlmostEqual(r["sell"]["stt"], 9750 * 0.0015, places=3)       # 0.15% on sell premium
        self.assertEqual(r["buy"]["stt"], 0)
        self.assertAlmostEqual(r["buy"]["stamp"], 9750 * 0.00003, places=3)     # 0.003% on buy
        self.assertEqual(r["sell"]["stamp"], 0)
        self.assertAlmostEqual(r["buy"]["exchange"], 9750 * 0.000355299, places=3)
        gst = (10 + 9750 * 0.000355299 + 9750 * 0.000001 + 9750 * 0.000000001) * 0.18
        self.assertAlmostEqual(r["buy"]["gst"], gst, places=3)
        self.assertAlmostEqual(r["total"], 46.71, places=2)

    def test_rates_follow_trade_date(self):
        before = self.cm.leg("SELL", 100, 75, dt.date(2026, 3, 30))["stt"]
        after = self.cm.leg("SELL", 100, 75, dt.date(2026, 4, 1))["stt"]
        self.assertAlmostEqual(before, 7.5, places=3)
        self.assertAlmostEqual(after, 11.25, places=3)

    def test_no_schedule_fails_closed(self):
        with self.assertRaises(DataError):
            self.cm.leg("BUY", 100, 75, dt.date(2024, 9, 30))

    def test_bad_inputs(self):
        for args in (("HOLD", 1, 1), ("BUY", None, 1), ("BUY", 1, 0), ("BUY", -1, 1)):
            with self.assertRaises(DataError):
                self.cm.leg(args[0], args[1], args[2], dt.date(2026, 10, 30))
        with self.assertRaises(DataError):
            CostModel([{"effective_from": "2026-01-01"}])
        with self.assertRaises(DataError):
            CostModel([])
