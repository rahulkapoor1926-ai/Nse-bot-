"""Dashboard data comes from the real paper ledger and risk files, never from made-up numbers."""

import json
import os
import unittest

from helpers import quote, t
from nse_bot import bot, dashboard, risk
import test_bot
from test_bot import DAY


class Dashboard(unittest.TestCase):
    # same fixture as the bot cycle tests, without re-running those tests here
    setUp, tearDown = test_bot.Cycle.setUp, test_bot.Cycle.tearDown
    ctx, approve = test_bot.Cycle.ctx, test_bot.Cycle.approve

    def test_empty_state_is_zero_and_stopped(self):
        d = dashboard.build(self.dir, self.cfg, t(10, 0))
        self.assertEqual(d["capital_inr"], 20000)
        self.assertEqual(d["totals"]["completed_trades"], 0)
        self.assertIsNone(d["totals"]["win_rate_pct"])
        self.assertEqual(d["totals"]["available_inr"], 20000)
        self.assertEqual(d["bot"]["status"], "Stopped")
        self.assertFalse(d["live_orders_enabled"])
        self.assertIn("SIMULATED", d["label"])

    def test_open_position_then_closed_trade(self):
        self.approve()
        self.cfg["max_lots_per_trade"] = 1
        iid = self.ce.instrument_id
        self.md.q[iid] = quote(bid=30, ask=30.5, at=t(9, 41), iid=iid)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        self.md.q[iid] = quote(bid=31, ask=31.5, at=t(9, 45), iid=iid)
        ctx = self.ctx(t(9, 45))
        bot.run_once(ctx, t(9, 45))
        dashboard.write_heartbeat(self.dir, t(9, 45), "running", ctx.marks, None, self.master)
        d = dashboard.build(self.dir, self.cfg, t(9, 46))
        self.assertEqual(d["bot"]["status"], "Running")
        [p] = d["open_positions"]
        self.assertEqual(p["qty"], 65)
        self.assertEqual(p["current_bid"], 31)
        self.assertEqual(p["symbol"], self.ce.label())
        self.assertIsNotNone(p["stop"])
        self.assertAlmostEqual(d["totals"]["unrealized_pnl"], ctx.marks[str(iid)]["unrealized"])
        self.assertGreater(d["totals"]["deployed_inr"], 30.5 * 65)
        self.assertAlmostEqual(d["totals"]["available_inr"], 20000 - d["totals"]["deployed_inr"], places=2)
        # heartbeat goes stale -> Stopped, last price kept with its time
        dashboard.write_heartbeat(self.dir, t(9, 50), "running", {}, None, self.master)
        d = dashboard.build(self.dir, self.cfg, t(9, 55))
        self.assertEqual(d["bot"]["status"], "Stopped")
        self.assertEqual(d["open_positions"][0]["current_bid"], 31)
        # stop hit -> one completed losing trade that matches the ledger and the risk file
        self.md.q[iid] = quote(bid=20, ask=20.5, at=t(10, 0), iid=iid)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        d = dashboard.build(self.dir, self.cfg, t(10, 1))
        self.assertEqual(d["open_positions"], [])
        [tr] = d["trades"]
        book = risk.RiskBook.open(self.dir, DAY)
        self.assertAlmostEqual(tr["net_pnl"], book.doc["realized_pnl"], places=2)
        self.assertEqual(tr["qty"], 65)
        self.assertLess(tr["exit_price"], tr["entry_price"])
        self.assertEqual(d["totals"]["win_rate_pct"], 0.0)
        self.assertEqual(d["equity_curve"][-1]["equity"], round(20000 + tr["net_pnl"], 2))
        self.assertEqual(d["today"]["remaining_loss_inr"], 0.0)
        self.assertTrue(d["today"]["locked"])
        self.assertEqual(d["bot"]["status"], "Error")            # a locked day shows as Error

    def test_corrupt_ledger_is_an_error_not_zero(self):
        os.makedirs(os.path.join(self.dir, "paper"))
        with open(os.path.join(self.dir, "paper", "ledger.jsonl"), "w") as f:
            f.write(json.dumps({"event": "fill", "hash": "x", "prev": "y"}) + "\n")
        d = dashboard.build(self.dir, self.cfg, t(10, 0))
        self.assertEqual(d["ledger_integrity"], "CORRUPT")
        self.assertEqual(d["bot"]["status"], "Error")
        self.assertTrue(d["errors"])

    def test_error_status_reported(self):
        dashboard.write_heartbeat(self.dir, t(10, 0), "error", None, "network down")
        d = dashboard.write(self.dir, self.cfg, t(10, 0, 30))
        self.assertEqual(d["bot"]["status"], "Error")
        self.assertEqual(d["bot"]["last_error"], "network down")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "dashboard", "data.json")))
