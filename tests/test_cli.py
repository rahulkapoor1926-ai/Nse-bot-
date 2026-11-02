import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import synth
from nse_bot import cli


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfgpath = os.path.join(self.dir, "paper.json")
        with open(self.cfgpath, "w") as f:
            json.dump(synth.cfg(os.path.join(self.dir, "state"), instruments_enabled=["index_options"]), f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_validate_ok_and_refused(self):
        self.assertEqual(run(["--config", self.cfgpath, "validate-config"])[0], 0)
        bad = os.path.join(self.dir, "bad.json")
        c = synth.cfg(self.dir, instruments_enabled=["index_options"])
        del c["max_daily_loss_inr"]
        json.dump(c, open(bad, "w"))
        rc, _, err = run(["--config", bad, "validate-config"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)
        self.assertEqual(run(["--config", "/missing.json", "validate-config"])[0], 2)

    def test_clock_and_costs(self):
        rc, out, _ = run(["--config", self.cfgpath, "--now", "2026-10-30T10:00", "clock"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["open"])
        rc, out, _ = run(["--config", self.cfgpath, "costs", "--buy", "150", "--sell", "150", "--qty", "65",
                          "--date", "2026-10-30"])
        self.assertEqual(json.loads(out)["total"], 46.71)

    def test_gate_on_synthetic_dataset_refuses(self):
        ds = os.path.join(self.dir, "ds")
        synth.write_dataset(ds, synth.trading_days(3), strikes=1)
        sp = os.path.join(self.dir, "s.json")
        json.dump(synth.STRATEGY, open(sp, "w"))
        rc, out, _ = run(["--config", self.cfgpath, "gate", "--dataset", ds, "--strategy", sp])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["status"], "REJECTED")
        rc, out, _ = run(["--config", self.cfgpath, "backtest", "--dataset", ds, "--strategy", sp])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["synthetic_data"])

    def test_report_and_risk_status(self):
        rc, out, _ = run(["--config", self.cfgpath, "report"])
        self.assertEqual(rc, 0)
        self.assertIn("SIMULATED", out)
        rc, out, _ = run(["--config", self.cfgpath, "--now", "2026-10-30T10:00", "risk-status"])
        self.assertFalse(json.loads(out)["lockout"]["locked"])

    def test_paper_without_credentials_refuses(self):
        env = {k: os.environ.pop(k) for k in ("SAHI_KEY_ID", "SAHI_KEY_SECRET") if k in os.environ}
        try:
            master = os.path.join(self.dir, "m.csv")
            open(master, "w").write("id,symbol,company_name,underlying_symbol,exchange,segment,instrument_type,"
                                    "option_type,expiry_date,strike_price,lot_size,tick_size,freeze_quantity\n")
            sp = os.path.join(self.dir, "s.json")
            json.dump(synth.STRATEGY, open(sp, "w"))
            rc, _, err = run(["--config", self.cfgpath, "paper", "--master", master, "--strategy", sp, "--once"])
            self.assertEqual(rc, 2)
            self.assertIn("SAHI_KEY_ID", err)
        finally:
            os.environ.update(env)


class ScheduledStop(unittest.TestCase):
    """--until and the non-trading-day exit let a scheduled job end cleanly."""

    def setUp(self):
        from nse_bot import clock
        self.clock = clock
        self.cal = clock.Calendar.load("nse_bot/data/nse_holidays.json")

    def args(self, until):
        return cli.build_parser().parse_args(["paper", "--strategy", "x.json"] + (["--until", until] if until else []))

    def at(self, day, hhmm):
        import datetime as dt
        return self.clock.at(dt.date.fromisoformat(day), hhmm)

    def test_until(self):
        a = self.args("12:15")
        with redirect_stdout(io.StringIO()):
            self.assertFalse(cli._should_stop(a, self.cal, self.at("2026-11-12", "12:14")))
            self.assertTrue(cli._should_stop(a, self.cal, self.at("2026-11-12", "12:15")))
            self.assertFalse(cli._should_stop(self.args(None), self.cal, self.at("2026-11-12", "15:00")))

    def test_holiday_and_weekend_exit(self):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(cli._should_stop(self.args(None), self.cal, self.at("2026-11-10", "10:00")))  # Diwali
            self.assertTrue(cli._should_stop(self.args(None), self.cal, self.at("2026-11-14", "10:00")))  # Saturday

    def test_bad_until_refused(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args("25:99")
