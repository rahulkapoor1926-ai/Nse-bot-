"""Forward validation book: gate handling, signal log, per-strategy statistics, honest sample labels."""

import json
import os
import shutil
import tempfile
import unittest

import synth
import test_bot
from helpers import quote, t
from nse_bot import bot, config, forward, risk
from nse_bot.errors import ConfigError

DAY = test_bot.DAY


class Gate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, gate, fwd):
        self.cfg["forward_validation"] = fwd
        book = risk.RiskBook.open(self.dir, DAY)
        order = {"instrument_id": 1, "strategy": "s", "signal_known_at": t(9, 40).isoformat(), "side": "BUY"}
        return book.check(order, self.cfg, synth.calendar(), t(10, 0), gate, [], {"lots": 1, "loss_at_stop_inr": 100},
                          {"reward_risk": 2.5, "net": {"net_reward_risk": 2.2}})

    def test_forward_book_trades_through_missing_evidence_only(self):
        r = self.check("INSUFFICIENT DATA", True)
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("forward validation book", r["forward_note"])
        self.assertEqual(self.check("NOT EVALUATED", True)["verdict"], "PASS")
        for g in ("REJECTED", "REJECTED (synthetic data)", "STALE (dataset changed since evaluation)"):
            self.assertEqual(self.check(g, True)["verdict"], "BLOCK", g)

    def test_standard_book_unchanged(self):
        self.assertIn("validation gate: INSUFFICIENT DATA", self.check("INSUFFICIENT DATA", False)["reasons"])

    def test_forward_needs_its_own_state_dir(self):
        with self.assertRaises(ConfigError):
            config.validate(synth.cfg(self.dir, forward_validation=True))
        ok = synth.cfg(os.path.join(self.dir, "forward", "orb"), forward_validation=True)
        self.assertIs(config.validate(ok), ok)

    def test_other_limits_still_apply(self):
        ex = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config", "paper.example.json")))
        self.assertEqual((ex["capital_inr"], ex["max_daily_loss_inr"], ex["daily_profit_stop_inr"],
                          ex["min_reward_risk"], ex["forward_validation"]), (20000, 500, 2000, 2.0, False))


class OrbForward(unittest.TestCase):
    """A real ORB signal through bot.run_once in a forward book: entered without gate approval, logged once."""
    tearDown = test_bot.Cycle.tearDown
    ctx = test_bot.Cycle.ctx

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["max_lots_per_trade"] = 1
        self.cfg["forward_validation"] = True
        self.strat = dict(synth.STRATEGY)

    def signals(self):
        p = os.path.join(self.dir, "forward", "signals.jsonl")
        return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []

    def test_entered_logged_once_and_counted(self):
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        r = bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        cand = r["candidates"][0]
        self.assertEqual(cand["risk"]["verdict"], "PASS", cand["risk"]["reasons"])
        fills = [x for x in self.broker.ledger.records() if x["event"] == "fill"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["price"], 30.5 + 2 * self.ce.tick_size)          # ask + slippage
        self.assertEqual([(s["family"], s["decision"]) for s in self.signals()], [("ORB", "ACCEPTED")])
        bot.run_once(self.ctx(t(9, 42)), t(9, 42))                                   # same signal again
        self.assertEqual(len([s for s in self.signals() if s["decision"] == "ACCEPTED"]), 1)
        self.md.q[self.ce.instrument_id] = quote(bid=20, ask=20.5, at=t(10, 0), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))                                  # stop: exit at the bid
        [tr] = forward.closed_trades(self.broker.ledger.records())
        self.assertEqual(tr["exit"], 20 - 2 * self.ce.tick_size)
        self.assertEqual((tr["family"], tr["strategy"]), ("ORB", self.strat["name"]))
        self.assertAlmostEqual(tr["net"], self.broker.ledger.positions()[str(self.ce.instrument_id)]["realized"], 2)

    def test_rejected_signal_logged_with_reasons(self):
        self.cfg["forward_validation"] = False
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        [s] = self.signals()
        self.assertEqual(s["decision"], "REJECTED")
        self.assertIn("validation gate: NOT EVALUATED", s["reasons"])
        self.assertEqual(self.broker.ledger.records(), [])


def trade(family, net, i, strategy=None):
    return {"family": family, "strategy": strategy or family.lower(), "net": net, "gross": net + 30, "costs": 30.0,
            "exit_time": "2026-11-02T10:%02d:00+05:30" % i}


class Stats(unittest.TestCase):
    def test_numbers(self):
        s = forward.stats([trade("ORB", 300, 1), trade("ORB", -100, 2), trade("ORB", -150, 3), trade("ORB", 200, 4)],
                          20000)
        self.assertEqual((s["trades"], s["wins"], s["losses"], s["win_rate_pct"]), (4, 2, 2, 50.0))
        self.assertEqual(s["net"], 250.0)
        self.assertEqual(s["profit_factor"], 2.0)                                     # 500 / 250
        self.assertEqual(s["max_drawdown_inr"], 250.0)                                # 300 -> 50
        self.assertEqual(s["max_drawdown_pct_of_capital"], 1.25)
        self.assertIn("INSUFFICIENT SAMPLE: 4 completed trades", s["sample"])

    def test_no_trades_and_no_losses(self):
        self.assertEqual(forward.stats([], 20000)["profit_factor_note"], "no trades")
        self.assertEqual(forward.stats([trade("ORB", 10, 1)], 20000)["profit_factor_note"], "no losing trades yet")
        self.assertIn("SMALL SAMPLE", forward.stats([trade("ORB", 10, i % 60) for i in range(40)], 20000)["sample"])

    def test_report_keeps_families_apart(self):
        d = tempfile.mkdtemp()
        try:
            forward.record_signal(d, t(10, 0), "sweep-reversal", "NIFTY", "X", "10:00", "REJECTED", ["quote: stale"])
            doc, md = forward.report([("sweep", d)], 20000, t(15, 35))
            self.assertEqual(doc["families"]["ORB"]["trades"], 0)
            self.assertEqual(doc["signals"]["Sweep"]["rejected"], 1)
            self.assertIn("INSUFFICIENT SAMPLE: 0 completed trades", md)
            self.assertIn("No synthetic or artificial trades", md)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
