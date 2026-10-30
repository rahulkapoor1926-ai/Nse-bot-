import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import synth
from helpers import contract, quote, t
from nse_bot.broker.base import LiveOrders
from nse_bot.broker.paper import PaperBroker
from nse_bot.costs import CostModel
from nse_bot.errors import LiveTradingDisabled, StateCorrupt


class Paper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.b = PaperBroker(self.dir, CostModel.load(synth.COSTS), 2)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_buy_at_ask_plus_slippage_never_mid(self):
        r = self.b.buy(contract(), 1, quote(bid=100, ask=100.5), t(10, 0))
        self.assertEqual((r["status"], r["price"], r["filled_qty"]), ("FILLED", 100.6, 65))
        rec = self.b.ledger.records()[-1]
        self.assertEqual(rec["label"], "SIMULATED (paper)")

    def test_partial_and_failed(self):
        r = self.b.buy(contract(), 3, quote(qty=150), t(10, 0))
        self.assertEqual((r["status"], r["filled_qty"], r["unfilled_qty"]), ("PARTIAL", 130, 65))
        self.assertEqual(self.b.buy(contract(), 1, None, t(10, 0))["status"], "FAILED")
        self.assertEqual(self.b.buy(contract(), 1, quote(qty=10), t(10, 0))["status"], "FAILED")
        self.assertEqual(self.b.buy(contract(), 1, quote(ask=None), t(10, 0))["status"], "FAILED")

    def test_failed_exit_leaves_position_open(self):
        self.b.buy(contract(), 1, quote(), t(10, 0))
        r = self.b.sell(contract(), 65, None, t(10, 5))
        self.assertEqual(r["status"], "FAILED")
        self.assertEqual(self.b.ledger.positions()["101"]["qty"], 65)
        self.assertEqual(self.b.sell(contract(), 130, quote(), t(10, 5))["status"], "FAILED")

    def test_realized_pnl_after_costs(self):
        self.b.buy(contract(), 1, quote(bid=100, ask=100.5), t(10, 0))
        self.b.sell(contract(), 65, quote(bid=110, ask=110.5, at=t(10, 30)), t(10, 30))
        p = self.b.ledger.positions()["101"]
        ch = sum(r["charges"]["total"] for r in self.b.ledger.records())
        self.assertEqual(p["qty"], 0)
        self.assertAlmostEqual(p["realized"], (109.9 - 100.6) * 65 - ch, places=2)

    def test_ledger_tamper_and_truncation_detected(self):
        self.b.buy(contract(), 1, quote(), t(10, 0))
        self.b.sell(contract(), 65, quote(at=t(10, 30)), t(10, 30))
        self.assertEqual(self.b.ledger.verify(), 2)
        path = self.b.ledger.path
        lines = open(path).read().splitlines()
        rec = json.loads(lines[0]); rec["price"] = 1.0
        with open(path, "w") as f:
            f.write("\n".join([json.dumps(rec, sort_keys=True)] + lines[1:]) + "\n")
        with self.assertRaises(StateCorrupt):
            self.b.ledger.verify()
        with open(path, "w") as f:
            f.write(lines[1] + "\n")
        with self.assertRaises(StateCorrupt):
            self.b.ledger.verify()
        with open(path, "w") as f:
            f.write(lines[0][:20] + "\n")
        with self.assertRaises(StateCorrupt):
            self.b.ledger.records()


class NoLive(unittest.TestCase):
    def test_live_orders_always_refused(self):
        for fn in (LiveOrders().place, LiveOrders().modify, LiveOrders().cancel):
            with self.assertRaises(LiveTradingDisabled):
                fn({"symbol": "NIFTY", "qty": 65})
