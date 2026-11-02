"""Minimum 1:2 NET reward:risk for ORB and sweep_reversal: charges, spread and slippage included,
below 2 means NO TRADE, stops are never widened, and backtests apply the same rule."""

import json
import os
import re
import shutil
import tempfile
import unittest

import synth
import test_backtest_gate
import test_bot
from helpers import quote, t
from nse_bot import bot, config, contract_select, risk, rr
from nse_bot.backtest import engine
from nse_bot.costs import CostModel
from nse_bot.errors import ConfigError

DAY = test_bot.DAY
ROOT = os.path.join(os.path.dirname(__file__), "..")


class Arithmetic(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel.load(synth.COSTS)

    def test_net_includes_both_legs_charges(self):
        r = rr.net(100.1, 79.9, 140.0, 65, self.cm, DAY)
        buy = self.cm.leg("BUY", 100.1, 65, DAY)["total"]
        at_stop = self.cm.leg("SELL", 79.9, 65, DAY)["total"]
        at_tgt = self.cm.leg("SELL", 140.0, 65, DAY)["total"]
        risk_inr = (100.1 - 79.9) * 65 + buy + at_stop
        reward_inr = (140.0 - 100.1) * 65 - buy - at_tgt
        self.assertAlmostEqual(r["net_risk_inr"], risk_inr, places=2)
        self.assertAlmostEqual(r["net_reward_inr"], reward_inr, places=2)
        self.assertAlmostEqual(r["net_reward_risk"], round(reward_inr / risk_inr, 3))

    def test_exactly_2_gross_is_below_2_net(self):
        # Rs 1 risk for Rs 2 reward before costs is NOT enough once charges are counted
        r = rr.net(100.0, 90.0, 120.0, 65, self.cm, DAY)
        self.assertLess(r["net_reward_risk"], 2.0)
        self.assertFalse(rr.ok(r, {"min_reward_risk": 2.0}))
        self.assertIn("NO TRADE", rr.why(r, {"min_reward_risk": 2.0}))

    def test_minimum_never_below_2(self):
        self.assertEqual(rr.minimum({"min_reward_risk": 1.0}), 2.0)
        self.assertEqual(rr.minimum({"min_reward_risk": 2.5}), 2.5)

    def test_unordered_or_missing_prices_are_no_trade(self):
        self.assertFalse(rr.ok(rr.net(100.0, 101.0, 120.0, 65, self.cm, DAY), {"min_reward_risk": 2}))
        self.assertFalse(rr.ok(None, {"min_reward_risk": 2}))
        with self.assertRaises(Exception):
            rr.net(None, 90.0, 120.0, 65, self.cm, DAY)


class RiskCheck(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, plan):
        book = risk.RiskBook.open(self.dir, DAY)
        order = {"instrument_id": 1, "strategy": "s", "signal_known_at": t(9, 40).isoformat(), "side": "BUY"}
        return book.check(order, self.cfg, synth.calendar(), t(10, 0), "APPROVED", [],
                          {"lots": 1, "loss_at_stop_inr": 100}, plan)

    def test_gross_2_without_net_is_blocked(self):
        r = self.check({"reward_risk": 2.0})
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("NO TRADE" in x for x in r["reasons"]))

    def test_net_below_2_blocked_and_at_2_passes(self):
        self.assertEqual(self.check({"reward_risk": 2.5, "net": {"net_reward_risk": 1.99}})["verdict"], "BLOCK")
        self.assertEqual(self.check({"reward_risk": 2.5, "net": {"net_reward_risk": 2.0}})["verdict"], "PASS")

    def test_config_refuses_a_lower_minimum(self):
        c = synth.cfg(self.dir, min_reward_risk=1.5)
        with self.assertRaises(ConfigError):
            config.validate(c)

    def test_limits_unchanged(self):
        ex = json.load(open(os.path.join(ROOT, "config", "paper.example.json")))
        self.assertEqual((ex["capital_inr"], ex["daily_profit_stop_inr"], ex["max_daily_loss_inr"]),
                         (20000, 2000, 500))
        self.assertEqual(ex["min_reward_risk"], 2.0)


class OrbLive(unittest.TestCase):
    """ORB through bot.run_once: target_r 2 is 1:2 gross, below 1:2 net -> NO TRADE; target_r 3 trades."""
    approve = test_bot.Cycle.approve
    tearDown = test_bot.Cycle.tearDown
    ctx = test_bot.Cycle.ctx

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["max_lots_per_trade"] = 1

    def run_with(self, target_r):
        import nse_bot.bot as b
        strat = dict(synth.STRATEGY, target_r=target_r)
        self.approve_for(strat)
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        ctx = self.ctx(t(9, 41))
        ctx.strategies, ctx.strategy = [strat], strat
        return b.run_once(ctx, t(9, 41))

    def approve_for(self, strat):
        from nse_bot import gate, state
        state.write_json(os.path.join(self.dir, "gate", "%s.json" % gate.strategy_hash(strat)),
                         {"status": "APPROVED", "synthetic_data": False, "dataset_digest": None})

    def test_gross_1_to_2_is_no_trade(self):
        r = self.run_with(2)
        cand = r["candidates"][0]
        self.assertEqual(cand["risk"]["verdict"], "BLOCK")
        self.assertTrue(any("NO TRADE" in x for x in cand["risk"]["reasons"]), cand["risk"]["reasons"])
        self.assertLess(cand["plan"]["net"]["net_reward_risk"], 2.0)
        self.assertEqual([x for x in self.broker.ledger.records() if x["event"] == "fill"], [])

    def test_wide_enough_target_trades_and_stop_is_not_widened(self):
        r = self.run_with(3)
        cand = r["candidates"][0]
        self.assertEqual(cand["risk"]["verdict"], "PASS", cand["risk"]["reasons"])
        self.assertGreaterEqual(cand["plan"]["net"]["net_reward_risk"], 2.0)
        [fill] = [x for x in self.broker.ledger.records() if x["event"] == "fill"]
        # the stop traded is the stop the strategy planned (20% on the premium), not a wider one
        self.assertEqual(fill["meta"]["plan"]["stop"], cand["plan"]["stop"])
        self.assertLessEqual(30.5 - fill["meta"]["plan"]["stop"], 30.5 * 0.20 + 0.05)


class SweepLive(unittest.TestCase):
    def test_net_rr_uses_the_conservative_stop_and_exit_slippage(self):
        cm = CostModel.load(synth.COSTS)
        c = test_bot.Cycle("test_closed_market_does_nothing")
        c.setUp()
        try:
            row = {"contract": c.ce, "entry": 100.15, "sizing_stop": 90.0, "premium_target": 125.0}
            cfg = synth.cfg(c.dir)
            r = contract_select.net_rr(row, 65, cfg, cm, DAY)
            self.assertEqual(r["stop_fill"], 90.0)
            self.assertEqual(r["target_fill"], round(125.0 - cfg["slippage_ticks"] * c.ce.tick_size, 2))
            self.assertTrue(rr.ok(r, cfg))                    # 2.44 gross, still >= 2 net
            r2 = contract_select.net_rr(dict(row, premium_target=120.5), 65, cfg, cm, DAY)
            self.assertFalse(rr.ok(r2, cfg))                  # 2.0 gross, below 2 once costed: NO TRADE
        finally:
            c.tearDown()


class Backtest(test_backtest_gate.Base):
    def test_no_trade_signals_are_not_gate_examples(self):
        a = engine.run(self.ds, dict(synth.STRATEGY, target_r=2), self.cfg, self.cm, self.cal)["counts"]
        self.assertEqual(a.get("valid", 0), 0)
        self.assertEqual(a.get("executed", 0), 0)
        self.assertEqual(a["net_rr_no_trade"], a["independent"])
        b = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal)["counts"]
        self.assertGreater(b["executed"], 0)


class NeverWiden(unittest.TestCase):
    def test_no_code_moves_a_stop_after_planning(self):
        hits = []
        for folder, _, files in os.walk(os.path.join(ROOT, "nse_bot")):
            for f in files:
                if f.endswith(".py"):
                    src = open(os.path.join(folder, f)).read()
                    hits += ["%s: %s" % (f, m) for m in re.findall(r'\[\s*"(?:stop|sizing_stop|premium_stop)"\s*\]\s*[-+*/]?=(?!=)', src)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
