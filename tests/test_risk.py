import datetime as dt
import os
import shutil
import tempfile
import unittest

import synth
from helpers import t
from nse_bot import risk

DAY = dt.date(2026, 10, 29)
SIZING = {"lots": 1, "loss_at_stop_inr": 400.0}
PLAN = {"reward_risk": 2.0, "net": {"net_reward_risk": 2.1}}


def order(iid=101, known="2026-10-29T09:40:00+05:30"):
    return {"instrument_id": iid, "strategy": "orb15", "signal_known_at": known, "side": "BUY"}


class Risk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)
        self.cal = synth.calendar()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, book, o=None, gate="APPROVED", q=(), sizing=SIZING, now=None):
        return book.check(o or order(), self.cfg, self.cal, now or t(10, 0), gate, list(q), sizing, PLAN)

    def test_pass(self):
        book = risk.RiskBook.open(self.dir, DAY)
        r = self.check(book)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertIn("larger than the configured daily limit", r["warning"])

    def test_refusals(self):
        book = risk.RiskBook.open(self.dir, DAY)
        self.assertIn("validation gate: INSUFFICIENT EVIDENCE", self.check(book, gate="INSUFFICIENT EVIDENCE")["reasons"])
        self.assertIn("quote: stale", self.check(book, q=["stale"])["reasons"])
        self.assertTrue(any("zero lots" in x for x in self.check(book, sizing={"lots": 0, "reasons": ["x"]})["reasons"]))
        self.assertTrue(any("entry window" in x for x in self.check(book, now=t(15, 0))["reasons"]))
        self.assertIn("only long option entries (BUY) are allowed",
                      self.check(book, o=dict(order(), side="SELL"))["reasons"])
        self.assertIn("not an NSE trading day", self.check(book, now=t(10, 0, day=dt.date(2026, 11, 10)))["reasons"])

    def test_duplicates_and_counts(self):
        book = risk.RiskBook.open(self.dir, DAY)
        book.record_entry(order(), t(10, 0))
        r = self.check(book)["reasons"]
        self.assertIn("duplicate: this signal was already entered today", r)
        self.assertIn("already holding this contract", r)
        book.record_exit(101, 10.0, self.cfg, t(10, 30))
        book.record_entry(order(102, "a"), t(10, 31)); book.record_exit(102, 10.0, self.cfg, t(10, 40))
        book.record_entry(order(103, "b"), t(10, 41)); book.record_exit(103, 10.0, self.cfg, t(10, 50))
        self.assertTrue(any("daily maximum" in x for x in self.check(book, o=order(104, "c"))["reasons"]))

    def test_daily_loss_lockout_persists_across_restart(self):
        book = risk.RiskBook.open(self.dir, DAY)
        book.record_entry(order(), t(10, 0))
        book.record_exit(101, -520.0, self.cfg, t(10, 30))
        self.assertTrue(book.locked)
        again = risk.RiskBook.open(self.dir, DAY)          # simulated restart
        self.assertTrue(again.locked)
        self.assertIn("daily loss limit", again.doc["lockout"]["reason"])
        self.assertEqual(self.check(again, o=order(105, "z"))["verdict"], "BLOCK")
        self.assertFalse(risk.RiskBook.open(self.dir, dt.date(2026, 10, 30)).locked)   # next trading day

    def test_open_loss_counts_and_unknown_mark_is_not_zero(self):
        book = risk.RiskBook.open(self.dir, DAY)
        self.assertFalse(book.mark(None, self.cfg, t(10, 0)))
        book.mark(-300.0, self.cfg, t(10, 0))
        self.assertEqual(book.remaining_loss(self.cfg), 200.0)
        book.mark(-500.0, self.cfg, t(10, 1))
        self.assertTrue(book.locked)

    def test_profit_stop(self):
        book = risk.RiskBook.open(self.dir, DAY)
        book.record_exit(1, 2000.0, self.cfg, t(11, 0))
        self.assertIn("not a target to repeat", book.doc["lockout"]["reason"])

    def test_consecutive_errors_lock(self):
        book = risk.RiskBook.open(self.dir, DAY)
        for _ in range(2):
            book.record_error(self.cfg, t(10, 0))
        book.record_ok()
        book.record_error(self.cfg, t(10, 0))
        self.assertFalse(book.locked)
        for _ in range(2):
            book.record_error(self.cfg, t(10, 0))
        self.assertTrue(book.locked)

    def test_corrupt_state_fails_closed_and_is_kept(self):
        path = os.path.join(self.dir, "risk", "2026-10-29.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write('{"date": "2026-10-29", "realized_pnl": -4')
        book = risk.RiskBook.open(self.dir, DAY)
        self.assertTrue(book.locked)
        self.assertEqual(self.check(book)["verdict"], "BLOCK")
        book.lock("x", t(10, 0))
        with open(path) as f:
            self.assertEqual(f.read(), '{"date": "2026-10-29", "realized_pnl": -4')
        with open(path, "w") as f:
            f.write('{"date": "2026-10-28"}')
        self.assertTrue(risk.RiskBook.open(self.dir, DAY).locked)

    def test_non_paper_config_blocks(self):
        book = risk.RiskBook.open(self.dir, DAY)
        self.cfg["live_orders_enabled"] = True
        self.assertIn("configuration is not paper mode", self.check(book)["reasons"])
