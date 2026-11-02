"""Backtest/live parity and the evidence framework (decisions of 2026-11-02):
same Rs 500 net-risk sizing, same entry point, regime recorded, drawdown in time order."""

import datetime as dt
import os
import shutil
import tempfile
import unittest

import synth
import test_bot
from helpers import contract, quote, t
from nse_bot import budget, bot, entry, patterns, regime
from nse_bot.backtest import engine, metrics
from nse_bot.costs import CostModel
from nse_bot.data import store

DAY = test_bot.DAY
FYERS_COSTS = os.path.join(synth.ROOT, "nse_bot", "data", "costs_india_fyers.json")


class Sizing(unittest.TestCase):
    """One NIFTY lot (65) with a 20% premium stop: loss at the stop FILL plus both legs' charges <= Rs 500."""

    def setUp(self):
        self.cfg = synth.cfg(tempfile.mkdtemp())
        self.cm = CostModel.load(synth.COSTS)
        self.c = contract()

    def size(self, ask):
        fill = budget.entry_fill(self.cfg, self.c, ask)
        stop = round(fill * 0.8, 2)
        return budget.size_at_fills(self.cfg, self.cm, self.c, fill, stop, DAY, 500)

    def test_cheap_premium_fits_and_expensive_is_refused(self):
        ok = self.size(30.0)
        self.assertEqual(ok["lots"], 1)
        self.assertLessEqual(ok["loss_at_stop_inr"], 500)
        for ask in (40.0, 120.0):
            r = self.size(ask)
            self.assertEqual(r["lots"], 0, ask)
            self.assertIn("> risk budget Rs 500.00", r["reasons"][0])

    def test_risk_includes_slippage_on_both_legs(self):
        fill = budget.entry_fill(self.cfg, self.c, 30.0)
        self.assertEqual(fill, 30.0 + 2 * 0.05)
        r = budget.size_at_fills(self.cfg, self.cm, self.c, fill, 24.0, DAY, 500)
        plain = budget.size(fill, 24.0, self.c, self.cfg, self.cm, DAY, 500)
        self.assertGreater(r["loss_at_stop_inr"], plain["loss_at_stop_inr"])        # stop fill is 0.10 lower

    def test_never_more_than_one_lot(self):
        self.cfg["max_lots_per_trade"] = 1
        self.assertEqual(self.size(5.0)["lots"], 1)

    def test_backtest_refuses_what_live_refuses(self):
        tmp = tempfile.mkdtemp()
        saved = synth.premium
        try:
            synth.premium = lambda S, K, typ: max(round((120.0 + 0.25 * ((S - K) if typ == "CE" else (K - S)))
                                                        / 0.05) * 0.05, 0.05)
            synth.write_dataset(tmp, synth.trading_days(10), strikes=4)
        finally:
            synth.premium = saved
        try:
            r = engine.run(store.load(tmp), synth.STRATEGY, self.cfg, self.cm, synth.calendar())
            self.assertEqual(r["counts"]["sizing_no_trade"], r["counts"]["independent"])
            self.assertEqual(r["trades"], [])
            self.assertNotIn("valid", r["counts"])               # refusals are never gate examples
        finally:
            shutil.rmtree(tmp)

    def test_fyers_costs_file_is_loaded_with_rs_20_brokerage(self):
        cm = CostModel.load(FYERS_COSTS)
        leg = cm.leg("BUY", 30.0, 65, dt.date(2026, 11, 2))
        self.assertEqual(leg["brokerage"], 20.0)
        with self.assertRaises(Exception):
            cm.leg("BUY", 30.0, 65, dt.date(2026, 3, 31))      # earlier dates are refused, not guessed


class EntryTiming(unittest.TestCase):
    def setUp(self):
        self.cfg = synth.cfg(tempfile.mkdtemp())
        self.sig = {"pattern": "orb", "option_type": "CE", "bar_start": t(9, 35), "known_at": t(9, 40),
                    "trigger_close": 25030.0, "levels": {}, "invalidate_below": 25000.0, "invalidate_above": None,
                    "bar_index": 4}

    def row(self):
        start, until = patterns.entry_window(self.sig, synth.STRATEGY)
        return {"underlying": "NIFTY", "strategy": "orb15", "status": "SIGNAL", "signal": self.sig,
                "contract": contract(), "spot": 25030.0, "entry_from": start, "entry_until": until}

    def cand(self, now):
        return entry.candidate(self.row(), quote(bid=30, ask=30.5, at=now), now, self.cfg, synth.calendar())

    def test_window_is_one_bar_after_the_delay(self):
        self.assertEqual(patterns.entry_window(self.sig, synth.STRATEGY), (t(9, 45), t(9, 50)))

    def test_waits_then_enters_then_expires(self):
        self.assertEqual(self.cand(t(9, 41))["status"], "WAITING")
        self.assertEqual(self.cand(t(9, 45, 10))["status"], "CANDIDATE")
        late = self.cand(t(9, 50, 5))
        self.assertEqual(late["status"], "REJECTED")
        self.assertIn("past its entry window 09:45-09:50", late["reasons"][0])

    def test_backtest_enters_at_the_same_point(self):
        tmp = tempfile.mkdtemp()
        try:
            synth.write_dataset(tmp, synth.trading_days(3), strikes=4)
            cfg = synth.cfg(tempfile.mkdtemp())
            tr = engine.run(store.load(tmp), synth.STRATEGY, cfg, CostModel.load(synth.COSTS),
                            synth.calendar())["trades"]
            for x in tr:
                sig = {"known_at": dt.datetime.fromisoformat(x["signal_time"])}
                self.assertEqual(x["entry_time"], patterns.entry_window(sig, synth.STRATEGY)[0].isoformat())
        finally:
            shutil.rmtree(tmp)


class LiveRegimeRecorded(unittest.TestCase):
    tearDown = test_bot.Cycle.tearDown
    ctx = test_bot.Cycle.ctx
    approve = test_bot.Cycle.approve

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["max_lots_per_trade"] = 1
        regime._CACHE.clear()

    def test_entry_meta_and_candidate_carry_the_regime(self):
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        cand = bot.run_once(self.ctx(t(9, 41)), t(9, 41))["candidates"][0]
        self.assertEqual(set(cand["regime"]), {"regime", "trend", "volatility"})
        self.assertEqual(cand["regime"]["trend"], "unknown")          # the fixture has no earlier sessions
        buy = [r for r in self.broker.ledger.records() if r["event"] == "fill"][0]
        self.assertEqual(buy["meta"]["regime"], cand["regime"])


def daily(closes, ranges=None, start=dt.date(2026, 9, 1)):
    out = []
    for i, c in enumerate(closes):
        r = (ranges or [100] * len(closes))[i]
        out.append((start + dt.timedelta(days=i), c, c + r / 2.0, c - r / 2.0, c))
    return out


class Regime(unittest.TestCase):
    day = dt.date(2026, 10, 29)

    def test_trending_and_sideways(self):
        up = regime.classify(daily([25000 + 50 * i for i in range(25)]), self.day)
        self.assertEqual((up["trend"], up["direction"]), ("trending", "up"))
        chop = regime.classify(daily([25000 + (60 if i % 2 else -60) for i in range(25)]), self.day)
        self.assertEqual(chop["trend"], "sideways")

    def test_high_volatility_from_the_previous_session_only(self):
        ranges = [100] * 24 + [400]
        hv = regime.classify(daily([25000] * 25, ranges), self.day)
        self.assertEqual(hv["volatility"], "high_volatility")
        nv = regime.classify(daily([25000] * 25), self.day)
        self.assertEqual(nv["volatility"], "normal_volatility")

    def test_too_few_sessions_is_unknown(self):
        r = regime.classify(daily([25000] * 5), self.day)
        self.assertEqual((r["trend"], r["volatility"]), ("unknown", "unknown"))

    def test_no_look_ahead(self):
        past = daily([25000 + 50 * i for i in range(25)])
        future = [(self.day + dt.timedelta(days=k), 1, 99999, 0, 99999) for k in range(3)]
        self.assertEqual(regime.classify(past, self.day), regime.classify(past + future, self.day))

    def test_intraday_bars_are_rolled_up_by_day(self):
        intraday = []
        for d, o, h, l, c in daily([25000 + 50 * i for i in range(25)]):
            t0 = dt.datetime.combine(d, dt.time(9, 15))
            intraday += [(t0, o, h, l, o), (t0 + dt.timedelta(minutes=5), o, h, l, c)]
        self.assertEqual(regime.classify(intraday, self.day)["trend"], "trending")


class Drawdown(unittest.TestCase):
    def test_same_result_in_any_listing_order(self):
        tr = [{"net": n, "gross": n, "charges": 0, "exit_time": "2026-10-%02dT10:00:00+05:30" % d}
              for d, n in ((1, 100), (2, -150), (3, 50), (4, -80))]
        a = metrics.compute(tr, ci=False)["max_drawdown"]
        b = metrics.compute(list(reversed(tr)), ci=False)["max_drawdown"]
        self.assertEqual(a, b)
        self.assertEqual(a, 180.0)


if __name__ == "__main__":
    unittest.main()
