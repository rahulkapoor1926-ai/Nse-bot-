import json
import os
import tempfile
import unittest

import synth
from nse_bot import config
from nse_bot.errors import ConfigError

EXAMPLE = os.path.join(synth.ROOT, "config", "paper.example.json")


class Config(unittest.TestCase):
    def good(self):
        with open(EXAMPLE) as f:
            return json.load(f)

    def bad(self, **changes):
        c = self.good()
        for k, v in changes.items():
            if v is KeyError:
                del c[k]
            else:
                c[k] = v
        with self.assertRaises(ConfigError) as cm:
            config.validate(c)
        return str(cm.exception)

    def test_example_is_valid(self):
        self.assertEqual(config.load(EXAMPLE)["capital_inr"], 20000)

    def test_user_figures(self):
        c = config.load(EXAMPLE)
        self.assertEqual((c["max_daily_loss_inr"], c["daily_profit_stop_inr"], c["timezone"], c["currency"]),
                         (1000, 2000, "Asia/Kolkata", "INR"))

    def test_missing_key_fails_closed(self):
        self.assertIn("missing required key 'max_daily_loss_inr'", self.bad(max_daily_loss_inr=KeyError))

    def test_unknown_key_fails_closed(self):
        self.assertIn("unknown key 'risk_multiplier'", self.bad(risk_multiplier=2))

    def test_gate_floor_cannot_be_lowered(self):
        self.assertIn("gate_min_oos_trades must be >= 100", self.bad(gate_min_oos_trades=99))
        self.assertIn("unknown key 'gate_min_examples'", self.bad(gate_min_examples=10000))   # replaced 2026-11-02

    def test_live_orders_cannot_be_enabled(self):
        self.assertIn("live_orders_enabled", self.bad(live_orders_enabled=True))
        self.assertIn("mode must be 'paper'", self.bad(mode="live"))

    def test_types_and_ranges(self):
        self.assertIn("must be a number", self.bad(capital_inr="20000"))
        self.assertIn("must be a number", self.bad(capital_inr=True))
        self.assertIn("HH:MM", self.bad(flat_by="3:15pm"))
        self.assertIn("not supported", self.bad(underlyings=["NIFTY", "FINNIFTY"]))
        self.assertIn("whole number", self.bad(max_trades_per_day=2.5))

    def test_cross_checks(self):
        self.assertIn("exceeds max_daily_loss_inr", self.bad(max_risk_per_trade_inr=1100))
        self.assertIn("no_entries_before < no_entries_after < flat_by", self.bad(no_entries_after="15:20"))
        self.assertIn("inside the NSE session", self.bad(no_entries_before="09:00"))

    def test_every_problem_reported_at_once(self):
        msg = self.bad(capital_inr=KeyError, flat_by="x", extra=1)
        self.assertIn("capital_inr", msg)
        self.assertIn("extra", msg)

    def test_unreadable_files(self):
        with self.assertRaises(ConfigError):
            config.load("/nonexistent/config.json")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        with self.assertRaises(ConfigError):
            config.load(f.name)
        os.unlink(f.name)
        with self.assertRaises(ConfigError):
            config.validate([])
