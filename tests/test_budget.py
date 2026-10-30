import datetime as dt
import unittest

import synth
from helpers import contract
from nse_bot import budget
from nse_bot.costs import CostModel
from nse_bot.errors import DataError

DAY = dt.date(2026, 10, 29)


class Budget(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel.load(synth.COSTS)
        self.cfg = synth.cfg("/tmp/unused", max_lots_per_trade=5)

    def test_one_lot_nifty(self):
        r = budget.size(100.0, 95.0, contract(), self.cfg, self.cm, DAY, 500)
        self.assertEqual(r["lots"], 1)                       # 5 pts x 65 = 325 + charges < 500; 2 lots > 500
        self.assertLessEqual(r["loss_at_stop_inr"], 500)
        self.assertIn("Slippage and gaps", r["warning"])

    def test_zero_lots_is_a_refusal(self):
        r = budget.size(100.0, 90.0, contract(), self.cfg, self.cm, DAY, 500)   # 650 > 500
        self.assertEqual(r["lots"], 0)
        self.assertTrue(r["reasons"])

    def test_outlay_cap(self):
        r = budget.size(300.0, 299.0, contract(), self.cfg, self.cm, DAY, 500)   # 19,500 > 16,000 cap
        self.assertEqual(r["lots"], 0)
        self.assertIn("outlay", r["reasons"][0])

    def test_remaining_daily_loss_shrinks_risk_never_grows(self):
        full = budget.size(100.0, 98.0, contract(), self.cfg, self.cm, DAY, 500)
        after_loss = budget.size(100.0, 98.0, contract(), self.cfg, self.cm, DAY, 200)
        self.assertEqual(after_loss["risk_budget_inr"], 200)
        self.assertLess(after_loss["lots"], full["lots"])
        self.assertEqual(budget.size(100.0, 98.0, contract(), self.cfg, self.cm, DAY, 5000)["risk_budget_inr"], 500)

    def test_freeze_quantity(self):
        r = budget.size(10.0, 9.95, contract(freeze=130), self.cfg, self.cm, DAY, 500)
        self.assertLessEqual(r["quantity"], 130)

    def test_invalid(self):
        with self.assertRaises(DataError):
            budget.size(100.0, 100.0, contract(), self.cfg, self.cm, DAY, 500)
        with self.assertRaises(DataError):
            budget.size(None, 90.0, contract(), self.cfg, self.cm, DAY, 500)
