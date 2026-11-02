import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import synth
from nse_bot import gate
from nse_bot.backtest import engine, metrics
from nse_bot.costs import CostModel
from nse_bot.data import store
from nse_bot.errors import DataError


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.days = synth.trading_days(80)
        cls.ds_path = os.path.join(cls.tmp, "ds")
        synth.write_dataset(cls.ds_path, cls.days, strikes=4)
        cls.ds = store.load(cls.ds_path)
        cls.cm = CostModel.load(synth.COSTS)
        cls.cal = synth.calendar()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.state = tempfile.mkdtemp(dir=self.tmp)
        self.cfg = synth.cfg(self.state)


class Store(Base):
    def test_loaded_and_marked_synthetic(self):
        self.assertTrue(self.ds.synthetic)
        self.assertEqual(self.ds.candle_count(), 80 * 75)
        self.assertEqual(self.ds.rejected, {})

    def copy(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        for n in ("manifest.json", "underlying.csv", "options.csv"):
            shutil.copy(os.path.join(self.ds_path, n), d)
        return d

    def test_hash_mismatch_refused(self):
        d = self.copy()
        with open(os.path.join(d, "underlying.csv"), "a") as f:
            f.write("2026-01-02T09:15:00+05:30,NIFTY,1,1,1,1\n")
        with self.assertRaises(DataError):
            store.load(d)

    def test_duplicates_and_bad_rows_rejected_and_counted(self):
        d = self.copy()
        with open(os.path.join(d, "underlying.csv")) as f:
            lines = f.read().splitlines()
        lines += [lines[1], "2026-01-02T08:00:00+05:30,NIFTY,1,1,1,1", "2026-01-02T09:20:00,NIFTY,1,1,1,1",
                  "2026-01-02T15:25:00+05:30,NIFTY,10,5,8,9"]
        with open(os.path.join(d, "underlying.csv"), "w") as f:
            f.write("\n".join(lines) + "\n")
        store.write_manifest(d, "x", "test", True, 5)
        ds = store.load(d)
        self.assertEqual(ds.rejected, {"underlying: duplicate bar": 1, "underlying: outside session": 1,
                                       "underlying: unparseable": 1, "underlying: missing or inconsistent price": 1})
        self.assertEqual(ds.candle_count(), 80 * 75)

    def test_missing_manifest(self):
        with self.assertRaises(DataError):
            store.load(tempfile.mkdtemp(dir=self.tmp))


class Engine(Base):
    def test_counts_distinguish_kinds(self):
        r = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal)
        c = r["counts"]
        self.assertEqual(c["candles"], 80 * 75)
        self.assertGreater(c["raw_occurrences"], c["independent"])
        self.assertEqual(c["independent"], 80)          # one per session
        self.assertEqual(c["valid"], 80)
        self.assertEqual(c["executed"], 80)

    def test_rerun_does_not_add_examples(self):
        a = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal)["counts"]
        b = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal)["counts"]
        self.assertEqual(a, b)

    def test_costs_and_realistic_fills(self):
        tr = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal)["trades"]
        for x in tr:
            self.assertAlmostEqual(x["net"], x["gross"] - x["charges"], places=2)
            self.assertGreater(x["charges"], 20)
            self.assertGreater(x["entry_time"], x["signal_time"])   # one bar of latency
        # entry is ask + 2 ticks: with a 0.50 spread the entry is 0.35 above mid
        t0 = tr[0]
        self.assertEqual(t0["entry_spread"], 0.5)

    def test_no_look_ahead_future_bars_do_not_change_entry(self):
        base = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal, days={self.days[5]})["trades"][0]
        u = self.ds.underlying["NIFTY"][self.days[5]]
        saved = list(u)
        try:
            k = [i for i, b in enumerate(u) if b[0] + dt.timedelta(minutes=5) > dt.datetime.fromisoformat(base["signal_time"])][0]
            for i in range(k + 1, len(u)):
                t, o, h, l, c = u[i]
                u[i] = (t, o, h + 500, l, c)                  # wild future highs
            again = engine.run(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal, days={self.days[5]})["trades"][0]
        finally:
            self.ds.underlying["NIFTY"][self.days[5]][:] = saved
        self.assertEqual((again["signal_time"], again["entry"]), (base["signal_time"], base["entry"]))

    def test_missing_quotes_make_examples_invalid(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        synth.write_dataset(d, self.days[:5], strikes=2, drop_quotes=True)
        r = engine.run(store.load(d), synth.STRATEGY, self.cfg, self.cm, self.cal)
        self.assertEqual(r["counts"].get("valid", 0), 0)
        self.assertEqual(r["invalid"], {"missing bid/ask at entry": 5})

    def test_chronological_split(self):
        s = engine.split_days(self.days)
        self.assertLess(max(s["train"]), min(s["validation"]))
        self.assertLess(max(s["validation"]), min(s["test"]))
        self.assertEqual(len(s["train"]) + len(s["validation"]) + len(s["test"]), 80)

    def test_interval_mismatch(self):
        with self.assertRaises(DataError):
            engine.run(self.ds, dict(synth.STRATEGY, interval_minutes=15, params={"or_minutes": 15}),
                       self.cfg, self.cm, self.cal)


class Metrics(unittest.TestCase):
    def test_values(self):
        tr = [{"net": n, "gross": n + 40, "charges": 40} for n in (100, -50, -50, 200, -50)]
        m = metrics.compute(tr)
        self.assertEqual(m["trades"], 5)
        self.assertEqual(m["win_rate"], 0.4)
        self.assertEqual(m["expectancy"], 30.0)
        self.assertEqual(m["profit_factor"], 2.0)
        self.assertEqual(m["max_drawdown"], 100.0)
        self.assertEqual(m["longest_losing_streak"], 2)
        self.assertEqual(m["charges"], 200)
        lo, hi = m["expectancy_ci95"]
        self.assertLess(lo, 30)
        self.assertGreater(hi, 30)
        self.assertEqual(metrics.compute([]), {"trades": 0})
        self.assertEqual(metrics.wilson(0, 0), (None, None))


class Gate(Base):
    def test_synthetic_data_never_approves(self):
        d = gate.evaluate(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal, self.state)
        self.assertEqual(d["status"], "REJECTED")
        self.assertIn("synthetic", d["reasons"][0])
        self.assertTrue(gate.status_for(self.state, synth.STRATEGY).startswith("REJECTED"))

    def test_small_sample_is_insufficient_evidence_and_test_period_untouched(self):
        d = gate.evaluate(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertEqual(d["status"], "INSUFFICIENT EVIDENCE")
        self.assertEqual(d["evidence"], "INSUFFICIENT EVIDENCE")
        self.assertIn("out-of-sample trades to be expected", d["reasons"][0])
        self.assertEqual(d["counts"]["independent_examples"], 80)
        self.assertNotIn("test", d)                  # the test period is not touched
        self.assertNotIn("10000", json.dumps(d))     # no example-count requirement any more

    def test_no_option_bid_ask_is_insufficient_evidence(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        synth.write_dataset(d, self.days[:5], strikes=2, drop_quotes=True)
        doc = gate.evaluate(store.load(d), synth.STRATEGY, self.cfg, self.cm, self.cal, self.state,
                            allow_synthetic=True)
        self.assertEqual(doc["status"], "INSUFFICIENT EVIDENCE")
        self.assertIn("no genuine historical option bid/ask", doc["reasons"][0])

    def test_record_carries_code_config_costs_and_orderflow_limitation(self):
        cfg = dict(self.cfg, orderflow_confirmation="required")
        d = gate.evaluate(self.ds, synth.STRATEGY, cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        for k in ("code_version", "config_hash", "costs_hash"):
            self.assertTrue(d[k])
        self.assertTrue(any("order-flow filter NOT SIMULATED" in x for x in d["limitations"]))


class BigGate(unittest.TestCase):
    """A full 2026 calendar of synthetic sessions: enough out-of-sample trades to reach each verdict."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.days = synth.trading_days(240)
        synth.write_dataset(os.path.join(cls.tmp, "up"), cls.days, strikes=4)
        cls.ds = store.load(os.path.join(cls.tmp, "up"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.state = tempfile.mkdtemp(dir=self.tmp)
        self.cfg = synth.cfg(self.state)
        self.cm = CostModel.load(synth.COSTS)
        self.cal = synth.calendar()

    def test_initial_evidence_is_not_approval(self):
        d = gate.evaluate(self.ds, synth.STRATEGY, self.cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertGreaterEqual(d["test"]["trades"], 30)
        self.assertLess(d["test"]["trades"], 100)
        self.assertEqual(d["status"], "INSUFFICIENT EVIDENCE", d["reasons"])
        self.assertEqual(d["evidence"], "INITIAL EVIDENCE")
        self.assertIn("INITIAL EVIDENCE only", d["reasons"][0])

    def test_full_path_with_lowered_approval_threshold_in_memory_only(self):
        cfg = dict(self.cfg, gate_min_oos_trades=30)
        d = gate.evaluate(self.ds, synth.STRATEGY, cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertEqual(d["status"], "APPROVED", d["reasons"])
        for k in ("train", "validation", "test", "baseline_test", "test_by_regime", "test_by_trend",
                  "test_by_volatility", "sensitivity"):
            self.assertIn(k, d)
        self.assertTrue(set(d["test_by_trend"]) <= {"trending", "sideways", "unknown"})
        for v in d["test_by_trend"].values():
            self.assertIn(v["evidence"], ("INSUFFICIENT EVIDENCE", "INITIAL EVIDENCE", "STRONGER EVIDENCE"))
        self.assertGreater(d["test"]["charges"], 0)
        self.assertIn("not evidence of future profitability", d["disclaimer"])
        self.assertEqual(gate.status_for(self.state, synth.STRATEGY), "REJECTED (synthetic data)")
        self.assertTrue(gate.status_for(self.state, dict(synth.STRATEGY, name="other")).startswith("NOT EVALUATED"))

    def test_losing_strategy_rejected(self):
        p = os.path.join(tempfile.mkdtemp(dir=self.tmp), "flat")
        synth.write_dataset(p, self.days, strikes=4, trend=False, seed=3)
        doc = gate.evaluate(store.load(p), synth.STRATEGY, dict(self.cfg, gate_min_oos_trades=30), self.cm,
                            self.cal, self.state, allow_synthetic=True)
        self.assertEqual(doc["status"], "REJECTED")

    def test_test_period_reuse_rejected(self):
        cfg = dict(self.cfg, gate_min_oos_trades=30)
        for i in range(4):
            s = dict(synth.STRATEGY, name="v%d" % i)
            d = gate.evaluate(self.ds, s, cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertEqual(d["status"], "REJECTED")
        self.assertIn("no longer untouched", d["reasons"][0])


class Staleness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)
        self.cm = CostModel.load(synth.COSTS)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, status, **over):
        from nse_bot import state
        doc = dict({"status": status, "synthetic_data": False, "dataset_digest": None},
                   **gate.fingerprint(self.cfg, self.cm))
        doc.update(over)
        state.write_json(os.path.join(self.dir, "gate", "%s.json" % gate.strategy_hash(synth.STRATEGY)), doc)

    def test_same_code_config_costs_keeps_the_status(self):
        self.write("APPROVED")
        self.assertEqual(gate.status_for(self.dir, synth.STRATEGY, None, self.cfg, self.cm), "APPROVED")

    def test_other_code_config_or_costs_is_stale(self):
        self.write("APPROVED", code_version="old")
        self.assertTrue(gate.status_for(self.dir, synth.STRATEGY, None, self.cfg, self.cm).startswith("STALE"))
        self.write("APPROVED")
        cfg = dict(self.cfg, slippage_ticks=1)
        self.assertIn("config_hash", gate.status_for(self.dir, synth.STRATEGY, None, cfg, self.cm))
        self.write("REJECTED", costs_hash="old")
        self.assertEqual(gate.status_for(self.dir, synth.STRATEGY, None, self.cfg, self.cm),
                         "STALE (costs_hash changed since evaluation; was REJECTED)")
        self.write("INSUFFICIENT EVIDENCE", code_version="old")
        self.assertTrue(gate.status_for(self.dir, synth.STRATEGY, None, self.cfg, self.cm).startswith("NOT EVALUATED"))

    def test_paths_and_state_do_not_count_as_config(self):
        self.write("APPROVED")
        cfg = dict(self.cfg, state_dir="/elsewhere", forward_validation=True)
        self.assertEqual(gate.status_for(self.dir, synth.STRATEGY, None, cfg, self.cm), "APPROVED")

    def test_neighbours_respect_interval(self):
        for label, s in gate.neighbours(synth.STRATEGY):
            if "or_minutes" in label:
                self.assertEqual(s["params"]["or_minutes"] % 5, 0)
                self.assertNotEqual(s["params"]["or_minutes"], 15)
