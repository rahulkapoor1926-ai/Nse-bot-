"""sweep_reversal replay: fills only from recorded option bids/asks, never at the stop price;
no look-ahead; contracts from the recorded data; the gate stays unchanged."""

import copy
import csv
import datetime as dt
import os
import shutil
import tempfile
import unittest
from unittest import mock

import sweep_synth as S
import synth
from nse_bot import clock, contract_select, gate, sweep
from nse_bot.backtest import sweep_engine
from nse_bot.costs import CostModel
from nse_bot.data import sweep_store
from nse_bot.errors import DataError

DAY = S.DAY
PDAY = synth.calendar().previous_trading_day(DAY)
SIGNAL = clock.at(DAY, "10:00")


def default_quote(t):
    return 100.0, 100.0


def write_ds(folder, quotes=default_quote, index=lambda t: 25000.0, synthetic=True, spread=0.1,
             lot=15, missing=()):
    """Two sessions of 1m index/future bars and one CE + one PE snapshot per minute.
    quotes(t) -> (ce_bid_mid, pe_mid) for the snapshot at the close of the bar starting at t."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index_1m.csv"), "w", newline="") as fi, \
            open(os.path.join(folder, "future_1m.csv"), "w", newline="") as ff, \
            open(os.path.join(folder, "chain_1m.csv"), "w", newline="") as fc:
        wi, wf, wc = csv.writer(fi), csv.writer(ff), csv.writer(fc)
        wi.writerow(sweep_store.FILES["index_1m.csv"])
        wf.writerow(sweep_store.FILES["future_1m.csv"])
        wc.writerow(sweep_store.FILES["chain_1m.csv"])
        for d in (PDAY, DAY):
            for i in range(375):
                t = clock.at(d, "09:15") + dt.timedelta(minutes=i)
                px = index(t)
                wi.writerow([t.isoformat(), px, px + 2, px - 2, px])
                wf.writerow([t.isoformat(), "NIFTY26NOVFUT", "2026-11-24", px + 50, px + 52, px + 48, px + 50, 1000])
                if d != DAY:
                    continue
                ce, pe = quotes(t)
                for iid, typ, k, mid in ((1, "CE", 25000, ce), (2, "PE", 25000, pe)):
                    gone = (t, iid) in missing
                    bid = "" if gone or mid is None else round(mid - spread / 2, 2)
                    ask = "" if gone or mid is None else round(mid + spread / 2, 2)
                    sym = "NIFTY03NOV26%s25000" % typ[0]
                    wc.writerow([t.isoformat(), iid, sym, "2026-11-03", k, typ, lot, 0.05, bid, ask, 1300, 1300,
                                 200000, 900000, 14, 0.5 if typ == "CE" else -0.5, 0.001, -8, 10])
    sweep_store.write_manifest(folder, "replay-test", "unit test fixture", synthetic, "NIFTY")
    return sweep_store.load(folder)


def selected(ds, entry=100.15, stop=92.0, sizing_stop=92.0, target=125.0):
    return {"contract": ds.by_id(1), "entry": entry, "premium_stop": stop, "sizing_stop": sizing_stop,
            "premium_target": target, "reward_risk": 2.0, "strike": 25000, "option_type": "CE"}


class Replay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(os.path.join(self.dir, "state"))
        self.costs = CostModel.load(synth.COSTS)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def sim(self, ds, sel=None, u_stop=24900.0):
        sel = sel or selected(ds)
        plan = contract_select.plan_for(sel, self.cfg, DAY)
        return sweep_engine._simulate(ds, sel, SIGNAL, plan, u_stop, "bullish", self.cfg, self.costs, DAY)

    def test_entry_is_next_minute_ask_plus_slippage(self):
        tr, why = self.sim(write_ds(os.path.join(self.dir, "a")))
        self.assertIsNone(why)
        self.assertEqual(tr["entry_time"], clock.at(DAY, "10:01").isoformat())
        self.assertEqual(tr["entry"], round(100.05 + 2 * 0.05, 2))

    def test_gap_through_stop_fills_at_the_lower_bid_not_the_stop(self):
        def q(t):
            return (80.0 if t >= clock.at(DAY, "10:05") else 100.0), 100.0
        tr, why = self.sim(write_ds(os.path.join(self.dir, "a"), quotes=q))
        self.assertEqual(tr["reason"], "stop_gap")
        self.assertEqual(tr["exit"], round(79.95 - 0.10, 2))               # recorded bid minus slippage
        self.assertLess(tr["exit"], tr["stop"])
        self.assertAlmostEqual(tr["net"], tr["gross"] - tr["charges"], places=2)
        self.assertGreater(tr["charges"], 0)

    def test_iv_crush_hits_the_premium_stop_with_the_underlying_flat(self):
        def q(t):
            return (max(91.0, 100.0 - 0.5 * max(0, (t - clock.at(DAY, "10:02")).total_seconds() / 60)), 100.0)
        tr, why = self.sim(write_ds(os.path.join(self.dir, "a"), quotes=q))
        self.assertIn(tr["reason"], ("stop", "stop_gap"))
        self.assertLessEqual(tr["exit"], 92.0 + tr["entry"] - 100.15)

    def test_missing_quotes_delay_the_exit_and_are_flagged(self):
        def q(t):
            return (130.0 if t >= clock.at(DAY, "10:10") else 100.0), 100.0
        gaps = {(clock.at(DAY, "10:%02d" % m), 1) for m in range(5, 15)}
        tr, why = self.sim(write_ds(os.path.join(self.dir, "a"), quotes=q, missing=gaps))
        self.assertTrue(tr["exit_delayed_by_missing_data"])
        self.assertEqual(tr["exit_time"], clock.at(DAY, "10:16").isoformat())   # first recorded bid after the gap
        self.assertEqual(tr["reason"], "target")

    def test_no_quote_or_wide_spread_at_entry_is_invalid(self):
        ds = write_ds(os.path.join(self.dir, "a"), missing={(clock.at(DAY, "10:00"), 1)})
        self.assertEqual(self.sim(ds)[1], "no recorded quote one minute after the signal")
        ds = write_ds(os.path.join(self.dir, "b"), spread=8.0)
        self.assertEqual(self.sim(ds)[1], "spread too wide at entry")

    def test_underlying_stop_and_time_exit(self):
        ds = write_ds(os.path.join(self.dir, "a"),
                      index=lambda t: 24850.0 if t.date() == DAY and t >= clock.at(DAY, "10:20") else 25000.0)
        self.assertEqual(self.sim(ds)[0]["reason"], "underlying_stop")
        tr, _ = self.sim(write_ds(os.path.join(self.dir, "b")))
        self.assertEqual(tr["reason"], "time")
        self.assertEqual(tr["exit_time"], clock.at(DAY, self.cfg["flat_by"]).isoformat())

    def test_budget_refuses_a_lot_that_risks_more_than_the_limit(self):
        ds = write_ds(os.path.join(self.dir, "a"), lot=65)
        self.assertIn("zero lots", self.sim(ds)[1])

    def test_contracts_come_from_the_recording_not_todays_master(self):
        ds = write_ds(os.path.join(self.dir, "a"))
        self.assertEqual(ds.by_id(1).symbol, "NIFTY03NOV26C25000")
        self.assertEqual(ds.by_id(1).lot_size, 15)

    def test_tampered_file_is_refused(self):
        folder = os.path.join(self.dir, "a")
        write_ds(folder)
        with open(os.path.join(folder, "chain_1m.csv"), "a") as f:
            f.write("tampered\n")
        with self.assertRaises(DataError):
            sweep_store.load(folder)


class NoLookAhead(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(os.path.join(self.dir, "state"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_evaluate_only_sees_closed_bars_and_the_current_snapshot(self):
        ds = write_ds(os.path.join(self.dir, "a"))
        seen = []

        def fake(strategy, u, now, u1, u1h, u1d, f1, get_chain, master, cfg):
            for bars, m in ((u1, 1), (f1, 1), (u1h, 60)):
                self.assertTrue(all(b[0] + dt.timedelta(minutes=m) <= now for b in bars))
            self.assertTrue(all(b[0].date() < now.date() for b in u1d))
            if now.date() == DAY:
                self.assertIs(get_chain()["legs"], ds.chains[now])
            else:
                with self.assertRaises(DataError):               # no snapshot recorded that day -> never invented
                    get_chain()
            seen.append(now)
            return {"decision": "SKIP"}
        with mock.patch.object(sweep, "evaluate", fake):
            r = sweep_engine.run(ds, S.STRATEGY, self.cfg, CostModel.load(synth.COSTS), synth.calendar())
        self.assertGreater(len(seen), 100)
        self.assertEqual(r["counts"].get("independent", 0), 0)

    def test_one_setup_becomes_one_trade_with_the_real_fill(self):
        ds = write_ds(os.path.join(self.dir, "a"), quotes=lambda t: ((130.0 if t >= clock.at(DAY, "10:30") else 100.0), 100.0))

        def fake(strategy, u, now, *a, **k):
            if now != SIGNAL:
                return {"decision": "SKIP"}
            return {"decision": "SETUP", "bias": "bullish", "_selected": selected(ds), "_u_stop": 24900.0,
                    "underlying_plan": {"spot": 25000.0, "target": 25200.0}}
        with mock.patch.object(sweep, "evaluate", fake):
            r = sweep_engine.run(ds, S.STRATEGY, self.cfg, CostModel.load(synth.COSTS), synth.calendar())
        self.assertEqual((r["counts"]["independent"], r["counts"]["valid"], r["counts"]["executed"]), (1, 1, 1))
        [tr] = r["trades"]
        self.assertEqual(tr["reason"], "target")
        self.assertEqual(tr["exit"], round(129.95 - 0.10, 2))


class Gate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(os.path.join(self.dir, "state"))
        self.costs = CostModel.load(synth.COSTS)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_synthetic_never_approves_and_real_small_data_is_insufficient(self):
        ds = write_ds(os.path.join(self.dir, "syn"), synthetic=True)
        doc = gate.evaluate(ds, S.STRATEGY, self.cfg, self.costs, synth.calendar(), self.cfg["state_dir"])
        self.assertEqual(doc["status"], "REJECTED")
        ds = write_ds(os.path.join(self.dir, "real"), synthetic=False)
        doc = gate.evaluate(ds, S.STRATEGY, self.cfg, self.costs, synth.calendar(), self.cfg["state_dir"])
        self.assertEqual(doc["status"], "INSUFFICIENT EVIDENCE")
        self.assertIn("at least 30 are needed", doc["reasons"][0])
        self.assertEqual(self.cfg["gate_min_oos_trades"], 100)

    def test_strategy_and_dataset_must_match(self):
        ds = write_ds(os.path.join(self.dir, "real"), synthetic=False)
        doc = gate.evaluate(ds, synth.STRATEGY, self.cfg, self.costs, synth.calendar(), self.cfg["state_dir"])
        self.assertEqual(doc["status"], "REJECTED")

    def test_neighbours_keep_integer_parameters_whole(self):
        for label, s in gate.neighbours(S.STRATEGY):
            self.assertIsInstance(s["params"]["swing_n"], int, label)
            sweep.validate(s)
        orb = gate.neighbours(synth.STRATEGY)
        self.assertTrue(orb)

    def test_strategies_are_reported_separately(self):
        a = copy.deepcopy(S.STRATEGY)
        self.assertNotEqual(gate.strategy_hash(a), gate.strategy_hash(synth.STRATEGY))


if __name__ == "__main__":
    unittest.main()
