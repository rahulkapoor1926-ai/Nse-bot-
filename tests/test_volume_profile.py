"""Volume profile / auction context: honest approximation, correct levels, context only."""

import datetime as dt
import json
import unittest

import test_bot
from helpers import quote, t
from nse_bot import clock
from nse_bot import volume_profile as VP

DAY = test_bot.DAY                       # Thursday 2026-10-29
PREV = dt.date(2026, 10, 28)
LAST_WEEK = dt.date(2026, 10, 22)


def bar(ts, price, vol, span=4.0):
    # typical price (h + l + c) / 3 == price
    return (ts, price, price + span / 2, price - span / 2, price, vol, None)


def session(day, prices_vols, start="09:15"):
    t0 = clock.at(day, start)
    return [bar(t0 + dt.timedelta(minutes=i), p, v) for i, (p, v) in enumerate(prices_vols)]


# previous session: value 100-120 around POC 110, thin at 150, a little at 160
PREV_BARS = session(PREV, [(110, 150)] * 4 + [(100, 200), (120, 200), (90, 50), (130, 50), (150, 5), (160, 60)])


class Building(unittest.TestCase):
    def test_each_bar_goes_to_one_bin_at_its_typical_price(self):
        p = VP.build([bar(t(10, 0), 103, 70), (t(10, 1), 1, 2, 3, 4, None), bar(t(10, 2), 117, 30)], 10)
        self.assertEqual(p["bins"], {100.0: 70, 120.0: 30})
        self.assertEqual(p["bars"], 2)                      # a bar without volume is skipped, not guessed

    def test_poc_value_area_and_nodes(self):
        lv = VP.levels(PREV_BARS)
        self.assertEqual((lv["poc"], lv["val"], lv["vah"]), (110.0, 100.0, 120.0))
        self.assertEqual(lv["status"], "APPROXIMATE")
        self.assertIn("APPROXIMATE", lv["label"])
        self.assertIn(110.0, lv["hvn"])
        self.assertIn(150.0, lv["lvn"])

    def test_value_area_holds_at_least_70_pct(self):
        p = VP.build(PREV_BARS, 10)
        val, vah = VP.value_area(p)
        inside = sum(v for k, v in p["bins"].items() if val <= k <= vah)
        self.assertGreaterEqual(inside, 0.7 * p["total_volume"])

    def test_no_volume_is_not_available(self):
        self.assertEqual(VP.levels([(t(10, 0), 1, 2, 0.5, 1.5, None)])["status"], "NOT AVAILABLE")


class Auction(unittest.TestCase):
    prev = VP.levels(PREV_BARS)

    def test_migration(self):
        up = VP.levels(session(DAY, [(140, 500)] * 5))
        down = VP.levels(session(DAY, [(80, 500)] * 5))
        lean = VP.levels(session(DAY, [(120, 500)] * 5 + [(110, 100)]))
        self.assertTrue(VP.migration(up, self.prev).startswith("higher"))
        self.assertTrue(VP.migration(down, self.prev).startswith("lower"))
        self.assertEqual(VP.migration(lean, self.prev), "overlapping, migrating higher")

    def test_acceptance_and_rejection(self):
        above = session(DAY, [(140, 100)] * 15)
        self.assertEqual(VP.acceptance(above, self.prev)["state"], "accepted above value")
        poke = session(DAY, [(115, 100)] * 5 + [(135, 100)] * 2 + [(115, 100)] * 3)
        self.assertEqual(VP.acceptance(poke, self.prev)["state"], "rejected above value (back inside)")
        short = session(DAY, [(140, 100)] * 5)
        self.assertEqual(VP.acceptance(short, self.prev)["state"], "above value, not yet accepted")

    def test_initial_balance_is_exact_and_waits_for_the_first_hour(self):
        idx = session(DAY, [(25000 + (i % 7), 0) for i in range(60)] + [(25020, 0)])
        self.assertEqual(VP.initial_balance(idx, DAY, clock.at(DAY, "10:00"))["status"], "FORMING")
        ib = VP.initial_balance(idx, DAY, clock.at(DAY, "10:20"))
        self.assertEqual((ib["status"], ib["high"], ib["low"]), ("EXACT", 25008.0, 24998.0))
        self.assertTrue(ib["broke_high"])
        self.assertFalse(ib["broke_low"])


class Context(unittest.TestCase):
    def fut(self, today):
        week = session(LAST_WEEK, [(105, 300)] * 5 + [(95, 100)])
        return week + PREV_BARS + session(DAY, today)

    def idx(self, broke_high):
        return session(DAY, [(25000 + (i % 5), 0) for i in range(60)] + [(25030 if broke_high else 25001, 0)] * 5)

    def test_orb_long_accepted_above_value_with_ib_break_is_supportive(self):
        now = clock.at(DAY, "10:30")
        c = VP.context("orb", 1, self.fut([(140, 100)] * 60), self.idx(True), 25030, now)
        self.assertEqual(c["verdict"], "SUPPORTIVE", c)
        self.assertEqual(c["previous_week"]["poc"], 100.0)
        self.assertEqual(c["previous_session"]["poc"], 110.0)
        self.assertTrue(any("initial balance broken" in s for s in c["supports"]))
        json.dumps(c)                                        # stored in decision reports as JSON

    def test_orb_inside_value_is_caution_not_a_block(self):
        now = clock.at(DAY, "10:30")
        c = VP.context("orb", 1, self.fut([(110, 100)] * 60), self.idx(False), 25001, now)
        self.assertIn("breakout is still inside yesterday's value area", c["cautions"])
        self.assertIn("never blocks", c["note"])

    def test_sweep_long_rejected_below_value_is_supportive(self):
        today = [(110, 100)] * 50 + [(80, 100)] * 2 + [(105, 100)] * 3
        c = VP.context("sweep", 1, self.fut(today), self.idx(False), 25001, clock.at(DAY, "10:15"))
        self.assertTrue(any("responsive reversal" in s for s in c["supports"]), c)

    def test_open_bar_not_used(self):
        today = [(110, 100)] * 10
        now = clock.at(DAY, "09:24") + dt.timedelta(seconds=30)      # 09:24 bar still forming
        c = VP.context("orb", 1, self.fut(today), self.idx(False), None, now)
        self.assertEqual(c["session"]["bars"], 9)


class Integration(unittest.TestCase):
    """The ORB entry outcome is identical whether or not the context could be built."""
    approve = test_bot.Cycle.approve
    tearDown = test_bot.Cycle.tearDown
    ctx = test_bot.Cycle.ctx

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["max_lots_per_trade"] = 1

    def test_context_recorded_and_never_blocks(self):
        from nse_bot import bot
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        cand = bot.run_once(self.ctx(t(9, 41)), t(9, 41))["candidates"][0]
        self.assertEqual(cand["auction_context"]["verdict"], "NOT AVAILABLE")    # test master has no future
        self.assertEqual(cand["risk"]["verdict"], "PASS", cand["risk"]["reasons"])
        self.assertFalse(any("profile" in r or "value" in r for r in cand["risk"]["reasons"]))


if __name__ == "__main__":
    unittest.main()
