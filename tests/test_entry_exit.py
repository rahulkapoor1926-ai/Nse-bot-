import datetime as dt
import unittest

import synth
from helpers import contract, quote, t
from nse_bot import entry, exit as exit_mod
from nse_bot.errors import DataError

DAY = dt.date(2026, 10, 29)
CFG = synth.cfg("/tmp/unused")


def row(known=(9, 40)):
    return {"underlying": "NIFTY", "strategy": "orb15", "status": "SIGNAL", "contract": contract(),
            "signal": {"pattern": "orb", "option_type": "CE", "known_at": t(*known), "trigger_close": 25030.0,
                       "levels": {"or_high": 25006, "or_low": 24994}, "invalidate_below": 25000.0,
                       "invalidate_above": None, "bar_index": 4}}


class Entry(unittest.TestCase):
    def setUp(self):
        self.cal = synth.calendar()

    def test_candidate_pays_the_ask_with_reasons_and_invalidation(self):
        c = entry.candidate(row(), quote(at=t(9, 41)), t(9, 41, 3), CFG, self.cal)
        self.assertEqual(c["status"], "CANDIDATE", c["reasons"])
        self.assertEqual(c["entry_price"], 100.5)
        self.assertTrue(c["reasons_for"])
        self.assertTrue(any("closes below 25000.00" in x for x in c["invalidation"]))

    def test_rejections(self):
        self.assertIn("before the entry window", " ".join(
            entry.candidate(row((9, 20)), quote(at=t(9, 21)), t(9, 21), CFG, self.cal)["reasons"]))
        self.assertIn("after the entry window", " ".join(
            entry.candidate(row(), quote(at=t(14, 40)), t(14, 40), CFG, self.cal)["reasons"]))
        r = entry.candidate(row(), quote(bid=None, at=t(9, 41)), t(9, 41), CFG, self.cal)
        self.assertEqual(r["status"], "REJECTED")
        self.assertIn("quote: missing bid", r["reasons"])
        self.assertEqual(entry.candidate({"status": "NO_SIGNAL"}, None, t(9, 41), CFG, self.cal)["status"], "REJECTED")

    def test_event_blackout(self):
        ev = [{"time": t(10, 0), "title": "RBI policy", "impact": "high", "source_url": "https://rbi.org.in"}]
        r = entry.candidate(row(), quote(at=t(9, 41)), t(9, 41), CFG, self.cal, ev)
        self.assertIn("scheduled event blackout: RBI policy", r["reasons"])

    def test_holiday(self):
        d = dt.date(2026, 11, 10)
        r = entry.candidate(row(), quote(at=t(9, 41, day=d)), t(9, 41, day=d), CFG, self.cal)
        self.assertIn("not an NSE trading day", r["reasons"])


class Exit(unittest.TestCase):
    def plan(self, entry_px=100.5, c=None):
        return exit_mod.plan(entry_px, c or contract(), synth.STRATEGY, CFG, DAY)

    def test_plan_on_ticks(self):
        p = self.plan()
        self.assertEqual((p["stop"], p["target"], p["flat_by"]), (80.4, 140.7, "15:15"))
        self.assertGreaterEqual(p["reward_risk"], 2)
        self.assertEqual(self.plan(c=contract(expiry=DAY))["flat_by"], "14:45")
        with self.assertRaises(DataError):
            self.plan(None)

    def pos(self):
        return {"plan": self.plan(), "invalidate_below": 25000.0, "invalidate_above": None}

    def test_actions(self):
        ev = exit_mod.evaluate
        self.assertEqual(ev(self.pos(), quote(bid=100, at=t(11, 0)), t(11, 0, 2), CFG)["action"], "HOLD")
        self.assertEqual(ev(self.pos(), quote(bid=80.4, at=t(11, 0)), t(11, 0), CFG)["reason"], "stop")
        gap = ev(self.pos(), quote(bid=60, at=t(11, 0)), t(11, 0), CFG)
        self.assertTrue(gap["gap"])
        self.assertEqual(gap["price"], 60)
        self.assertEqual(ev(self.pos(), quote(bid=141, at=t(11, 0)), t(11, 0), CFG)["reason"], "target")
        self.assertTrue(ev(self.pos(), quote(bid=100, at=t(15, 15)), t(15, 15), CFG)["reason"].startswith("time"))
        self.assertTrue(ev(self.pos(), quote(bid=100, at=t(11, 0)), t(11, 0), CFG, 24990)["reason"].startswith("invalidated"))

    def test_stale_or_missing_never_counts_as_exit(self):
        for q in (None, quote(bid=None, at=t(11, 0)), quote(bid=50, at=t(10, 0))):
            d = exit_mod.evaluate(self.pos(), q, t(11, 0), CFG)
            self.assertEqual(d["action"], "NO_PRICE")
            self.assertIsNone(d["price"])
        d = exit_mod.evaluate(self.pos(), None, t(15, 20), CFG)
        self.assertIn("do not assume flat", d["reason"])
