"""NIFTY index options only (2 Nov 2026). The equity/ETF signals seen on the dashboard (TCS, SBIN, RELIANCE,
NIFTYBEES, LT, KOTAKBANK, ITC) came from paper-day run 41422960546 at 15:13 IST, code c800040, whose config
still enabled equity/etf and whose workflow still ran orb15-equity. These tests prove such instruments
can no longer produce a signal or get past the shared RiskBook."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import synth
import test_cli
from helpers import contract, t
from nse_bot import bot, config, risk, scope
from nse_bot.errors import ConfigError
from nse_bot.instruments import Stock

DAY = dt.date(2026, 10, 29)
SEEN = [("TCS", "equity"), ("SBIN", "equity"), ("RELIANCE", "equity"), ("NIFTYBEES", "etf"), ("LT", "equity"),
        ("KOTAKBANK", "equity"), ("ITC", "equity"), ("BANKBEES", "etf")]
PLAN = {"reward_risk": 2.0, "net": {"net_reward_risk": 2.1}}
SIZING = {"lots": 1, "loss_at_stop_inr": 300.0}


class RiskBookRefuses(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir, instruments_enabled=["equity", "etf", "index_options"])   # even if a config says so

    def tearDown(self):
        shutil.rmtree(self.dir)

    def order(self, c, iid=1):
        return {"instrument_id": iid, "strategy": "orb15-equity", "signal_known_at": "x", "side": "BUY",
                "kind": scope.order_kind(c), "underlying": getattr(c, "underlying", getattr(c, "symbol", None))}

    def check(self, o):
        book = risk.RiskBook.open(self.dir, DAY)
        return book.check(o, self.cfg, synth.calendar(), t(10, 0), "APPROVED", [], SIZING, PLAN)

    def test_every_equity_and_etf_seen_is_blocked(self):
        for i, (sym, kind) in enumerate(SEEN):
            st = Stock(1000 + i, sym, sym, "EQ", 0.05, kind)
            v = self.check(self.order(st, 1000 + i))
            self.assertEqual(v["verdict"], "BLOCK", sym)
            self.assertIn("only NIFTY index options are enabled: %s entries are disabled" % kind, v["reasons"], sym)
            self.assertEqual(risk.RiskBook.open(self.dir, DAY).doc["trades"], 0)

    def test_an_order_that_does_not_say_what_it_is_is_blocked(self):
        v = self.check({"instrument_id": 1, "strategy": "s", "signal_known_at": "x", "side": "BUY"})
        self.assertIn("only NIFTY index options are enabled: unknown-kind entries are disabled", v["reasons"])

    def test_other_index_options_are_blocked(self):
        v = self.check({"instrument_id": 1, "strategy": "s", "signal_known_at": "x", "side": "BUY",
                        "kind": "index_options", "underlying": "BANKNIFTY"})
        self.assertIn("only NIFTY index options are enabled: underlying BANKNIFTY is not allowed", v["reasons"])

    def test_a_nifty_option_still_passes(self):
        c = contract()
        self.assertEqual((scope.order_kind(c), c.underlying), ("index_options", "NIFTY"))
        self.assertEqual(self.check(self.order(c))["verdict"], "PASS")


class NoSignalGeneration(unittest.TestCase):
    def test_equity_and_etf_strategies_never_scan(self):
        ctx = type("C", (), {"cfg": synth.cfg("/tmp", instruments_enabled=["equity", "etf", "index_options"])})()
        self.assertFalse(bot.enabled(ctx, synth.EQ_STRATEGY))
        self.assertFalse(bot.enabled(ctx, dict(synth.EQ_STRATEGY, instrument="etf")))
        self.assertTrue(bot.enabled(ctx, synth.STRATEGY))

    def test_paper_command_refuses_the_equity_strategy_file_before_connecting(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "c.json")
            json.dump(synth.cfg(os.path.join(d, "state"), instruments_enabled=["index_options"]), open(p, "w"))
            eq = os.path.join(synth.ROOT, "config", "strategies", "orb15-equity.example.json")
            rc, _, err = test_cli.run(["--config", p, "paper", "--strategy", eq, "--once"])
            self.assertEqual(rc, 2)
            self.assertIn("only NIFTY index-option strategies may run", err)
        finally:
            shutil.rmtree(d)

    def test_config_refuses_equity_etf_and_other_underlyings(self):
        ok = synth.cfg("/tmp/x", instruments_enabled=["index_options"])
        config.validate(ok)
        for over in ({"instruments_enabled": ["equity", "index_options"]}, {"instruments_enabled": ["etf", "index_options"]},
                     {"underlyings": ["NIFTY", "BANKNIFTY"], "index_names": {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank"}}):
            with self.assertRaises(ConfigError):
                config.validate(dict(ok, **over))

    def test_no_workflow_runs_an_equity_strategy(self):
        wf = os.path.join(synth.ROOT, ".github", "workflows")
        for name in os.listdir(wf):
            self.assertNotIn("orb15-equity", open(os.path.join(wf, name)).read(), name)
        ex = config.load(os.path.join(synth.ROOT, "config", "paper.example.json"))
        self.assertEqual((ex["instruments_enabled"], ex["underlyings"]), (["index_options"], ["NIFTY"]))


class DashboardSeparatesHistory(unittest.TestCase):
    def test_old_equity_rows_are_historical_not_current(self):
        try:
            from nse_bot.web.data import View, is_nifty_option
        except ImportError:
            self.skipTest("flask not installed")
        for sym, _ in SEEN:
            self.assertFalse(is_nifty_option(sym))
        self.assertTrue(is_nifty_option("NSE:NIFTY26N0323350CE"))
        self.assertFalse(is_nifty_option("NSE:NIFTY26N0323350CE", "orb15-equity"))
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "alerts"))
            rows = [{"kind": "candidate", "time": "2026-10-29T15:14:07+05:30",
                     "message": "REJECTED TCS: after the entry window (14:30)"},
                    {"kind": "candidate", "time": "2026-10-29T15:15:40+05:30",
                     "message": "REJECTED NSE:NIFTY26N0323350CE: after the entry window (14:30)"}]
            with open(os.path.join(d, "alerts", "alerts.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(r) for r in rows) + "\n")
            cfg = config.load(os.path.join(synth.ROOT, "config", "paper.example.json"))
            v = View(d, cfg, now=lambda: t(16, 0))
            s = v.signals("standard")
            self.assertEqual([x["contract"] for x in s["orb"]], ["NSE:NIFTY26N0323350CE"])
            self.assertEqual([x["contract"] for x in s["rejected"]], ["NSE:NIFTY26N0323350CE"])
            self.assertEqual([x["contract"] for x in s["historical"]], ["TCS"])
            self.assertIn("Kept for audit", s["historical"][0]["note"])
            logs = v.logs("standard")
            self.assertTrue(any(x["text"].startswith("[historical") and "TCS" in x["text"] for x in logs))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
