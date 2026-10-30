"""Stocks and ETFs: instruments, costs, quotes, sizing, scanning, backtest and gate."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import synth
from helpers import quote, t
from nse_bot import budget, gate, instruments, quotes, scanner
from nse_bot.backtest import equity_engine
from nse_bot.costs import CostModel
from nse_bot.data import store
from nse_bot.errors import DataError
from test_instruments import HEADER
from test_patterns_scanner import bars

DAY = dt.date(2026, 10, 29)
CASH = """12027,INE002A01018,RELIANCE,Reliance Industries,EQ,,NSE,NSE,2885,STK,,,0.00,1,0.10,0,2,1
10914,INE748C01038,3IINFOLTD-BE,3i Infotech,BE,,NSE,NSE,6240,STK,,,0.00,1,0.01,0,2,1
725069,INE002A01018,RELIANCE,Reliance Industries,A,,BSE,BSE,500325,STK,,,0.00,1,0.05,0,2,1
40001,INF204KB14I2,NIFTYBEES,Nippon India ETF Nifty 50 BeES,EQ,,NSE,NSE,10576,STK,,,0.00,1,0.01,0,2,1"""
UNIVERSE = [{"symbol": "RELIANCE", "kind": "equity"}, {"symbol": "NIFTYBEES", "kind": "etf"}]


def master():
    m = instruments.parse(HEADER + "\n" + CASH + "\n")
    m.bind_universe(UNIVERSE)
    return m


class Instruments(unittest.TestCase):
    def test_cash_rows_nse_eq_only(self):
        m = master()
        self.assertEqual(sorted(m.cash), ["NIFTYBEES", "RELIANCE"])     # BE series and BSE row excluded
        st = m.stocks["RELIANCE"]
        self.assertEqual((st.instrument_id, st.tick_size, st.lot_size, st.kind, st.expiry), (12027, 0.10, 1, "equity", None))
        self.assertEqual(m.stocks["NIFTYBEES"].kind, "etf")
        self.assertIs(m.by_id(40001), m.stocks["NIFTYBEES"])

    def test_unknown_universe_symbol_refused(self):
        m = instruments.parse(HEADER + "\n" + CASH + "\n")
        with self.assertRaises(DataError):
            m.bind_universe([{"symbol": "NOSUCH", "kind": "equity"}])

    def test_universe_file_validation(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "u.json")
            for bad in ({"symbols": [{"symbol": "X", "kind": "bond"}]}, {"symbols": [{"symbol": "X", "kind": "etf"}] * 2},
                        {"nope": []}):
                json.dump(bad, open(p, "w"))
                with self.assertRaises(DataError):
                    instruments.load_universe(p)
            self.assertEqual(len(instruments.load_universe(os.path.join(synth.ROOT, "config", "universe.example.json"))), 12)
        finally:
            shutil.rmtree(d)


class Costs(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel.load(synth.COSTS)

    def test_intraday(self):
        r = self.cm.round_trip(1000, 1000, 10, DAY, "equity_intraday")
        self.assertEqual(r["buy"]["brokerage"], 5.0)              # 0.05% of 10,000 < Rs 10
        self.assertAlmostEqual(r["sell"]["stt"], 2.5, places=3)    # 0.025% sell
        self.assertEqual(r["buy"]["stt"], 0)
        self.assertAlmostEqual(r["buy"]["stamp"], 0.3, places=3)
        big = self.cm.leg("BUY", 1000, 100, DAY, "equity_intraday")
        self.assertEqual(big["brokerage"], 10.0)                   # capped at Rs 10

    def test_delivery(self):
        b = self.cm.leg("BUY", 1000, 10, DAY, "equity_delivery")
        s = self.cm.leg("SELL", 1000, 10, DAY, "equity_delivery")
        self.assertAlmostEqual(b["stt"], 10.0, places=3)
        self.assertEqual(b["exchange"], 0.0)                       # sell side only, as SAHI lists it
        self.assertEqual(s["dp"], 13.5)
        self.assertAlmostEqual(b["stamp"], 1.5, places=3)

    def test_unknown_segment_and_dates(self):
        with self.assertRaises(DataError):
            self.cm.leg("BUY", 1, 1, DAY, "crypto")
        with self.assertRaises(DataError):
            self.cm.leg("BUY", 1, 1, dt.date(2024, 9, 30), "equity_intraday")
        with self.assertRaises(DataError):
            CostModel([dict(self.cm.schedule(DAY, "equity_intraday"), segment="futures")])


class Quotes(unittest.TestCase):
    def test_stock_liquidity_rules(self):
        cfg = synth.cfg("/tmp/x")
        st = master().stocks["RELIANCE"]
        ok = quote(bid=1000, ask=1000.1, at=t(10, 0), qty=500, oi=None, volume=1_000_000)
        self.assertEqual(quotes.check_for(st, ok, t(10, 0, 2), cfg), [])           # no OI needed
        thin = quote(bid=1000, ask=1000.1, at=t(10, 0), qty=50, oi=None, volume=1_000_000)
        self.assertTrue(any("thin" in p for p in quotes.check_for(st, thin, t(10, 0), cfg)))
        cheap = quote(bid=20, ask=20.05, at=t(10, 0), qty=50000, oi=None, volume=1_000_000)
        self.assertTrue(any("below equity_min_price" in p for p in quotes.check_for(st, cheap, t(10, 0), cfg)))
        self.assertIn("missing bid", quotes.check_for(st, quote(bid=None, at=t(10, 0)), t(10, 0), cfg))


class Sizing(unittest.TestCase):
    def test_shares_within_risk_and_outlay(self):
        cfg = synth.cfg("/tmp/x")
        st = master().stocks["RELIANCE"]
        cm = CostModel.load(synth.COSTS)
        r = budget.size(1000.0, 994.0, st, cfg, cm, DAY, 500)
        self.assertEqual(r["quantity"], 15)                       # outlay cap 16,000 incl. charges binds first
        self.assertLessEqual(r["outlay_inr"], 16000)
        r = budget.size(1000.0, 950.0, st, cfg, cm, DAY, 500)
        self.assertLessEqual(r["loss_at_stop_inr"], 500)
        self.assertEqual(r["quantity"], 9)
        self.assertEqual(budget.size(1000.0, 400.0, st, cfg, cm, DAY, 500)["lots"], 0)
        self.assertLess(budget.size(1000.0, 950.0, st, cfg, cm, DAY, 100)["quantity"], 9)


class Scan(unittest.TestCase):
    def test_long_signal_and_ignored_short(self):
        m = master()
        cfg = synth.cfg("/tmp/x")
        up = bars([1000, 1000.5, 999.5, 1000.2, 1003])
        down = bars([1000, 1000.5, 999.5, 1000.2, 997])
        rows = scanner.scan_equities({"RELIANCE": up, "NIFTYBEES": down}, {}, m, t(9, 41), cfg, synth.EQ_STRATEGY)
        by = {r["underlying"]: r for r in rows}
        self.assertEqual(by["RELIANCE"]["status"], "SIGNAL")
        self.assertIs(by["RELIANCE"]["contract"], m.stocks["RELIANCE"])
        self.assertEqual(by["NIFTYBEES"]["status"], "SHORT_IGNORED")
        cfg2 = synth.cfg("/tmp/x", instruments_enabled=["equity"])
        rows = scanner.scan_equities({"RELIANCE": up}, {}, m, t(9, 41), cfg2, synth.EQ_STRATEGY)
        self.assertEqual([r["underlying"] for r in rows], ["RELIANCE"])     # ETF disabled


class Backtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.days = synth.trading_days(60)
        cls.q = os.path.join(cls.tmp, "q")
        cls.e = os.path.join(cls.tmp, "e")
        synth.write_equity_dataset(cls.q, cls.days)
        synth.write_equity_dataset(cls.e, cls.days, fill_model="estimated_spread")
        cls.cm = CostModel.load(synth.COSTS)
        cls.cal = synth.calendar()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.state = tempfile.mkdtemp(dir=self.tmp)
        self.cfg = synth.cfg(self.state)

    def test_load_and_validation(self):
        ds = store.load_equity(self.q)
        self.assertEqual((ds.fill_model, ds.candle_count(), ds.kinds["BBB"]), ("quotes", 2 * 60 * 75, "etf"))
        bad = os.path.join(self.tmp, "bad")
        synth.write_equity_dataset(bad, self.days[:2], fill_model="estimated_spread", spread_pct=0.01)
        with self.assertRaises(DataError):
            store.load_equity(bad)

    def test_counts_and_costs(self):
        ds = store.load_equity(self.q)
        r = equity_engine.run(ds, synth.EQ_STRATEGY, self.cfg, self.cm, self.cal)
        c = r["counts"]
        self.assertGreater(c["raw_occurrences"], c["independent"])
        self.assertLessEqual(c["independent"], 2 * 60)
        self.assertEqual(c["executed"], len(r["trades"]))
        for x in r["trades"]:
            self.assertAlmostEqual(x["net"], x["gross"] - x["charges"], places=2)
            self.assertGreater(x["charges"], 0)
            self.assertGreater(x["entry_time"], x["signal_time"])

    def test_estimated_spread_fills_are_worse_than_close(self):
        ds = store.load_equity(self.e)
        tr = equity_engine.run(ds, synth.EQ_STRATEGY, self.cfg, self.cm, self.cal)["trades"][0]
        rec = [r for r in ds.bars[tr["underlying"]][dt.date.fromisoformat(tr["day"])]
               if r["ts"].isoformat() < tr["entry_time"]][-1]
        self.assertGreater(tr["entry"], rec["close"])
        self.assertEqual(tr["fill_model"], "estimated_spread")

    def test_options_strategy_rejected_on_equity_data(self):
        ds = store.load_equity(self.q)
        with self.assertRaises(DataError):
            equity_engine.run(ds, synth.STRATEGY, self.cfg, self.cm, self.cal)
        d = gate.evaluate(ds, synth.STRATEGY, self.cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertEqual(d["status"], "REJECTED")

    def test_gate_estimated_spread_needs_permission(self):
        ds = store.load_equity(self.e)
        cfg = dict(self.cfg, gate_allow_estimated_spread=False)
        d = gate.evaluate(ds, synth.EQ_STRATEGY, cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertEqual(d["status"], "INSUFFICIENT DATA")
        self.assertIn("no historical bid/ask", d["reasons"][0])
        cfg = dict(self.cfg, gate_allow_estimated_spread=True)
        d = gate.evaluate(ds, synth.EQ_STRATEGY, cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertEqual(d["status"], "INSUFFICIENT DATA")         # 10,000 still required
        self.assertIn("required", d["reasons"][0])
        self.assertIn("assumed spread", d["fill_model"])

    def test_gate_full_path_in_memory_threshold(self):
        ds = store.load_equity(self.q)
        cfg = dict(self.cfg, gate_min_examples=40, gate_min_oos_trades=5)
        d = gate.evaluate(ds, synth.EQ_STRATEGY, cfg, self.cm, self.cal, self.state, allow_synthetic=True)
        self.assertIn(d["status"], ("APPROVED", "REJECTED"))
        for k in ("train", "validation", "test", "baseline_test", "sensitivity", "test_by_regime"):
            self.assertIn(k, d)
        self.assertIn("random entry bar", d["baseline"])
        self.assertEqual(gate.status_for(self.state, synth.EQ_STRATEGY), "REJECTED (synthetic data)")


class PaperCycle(unittest.TestCase):
    """A stock strategy and an option strategy in one process; stocks tried first."""

    def setUp(self):
        from nse_bot.broker.base import MarketData

        class MD(MarketData):
            def __init__(s):
                s.series = {}
                s.q = {}

            def quotes(s, ids):
                return {i: s.q[i] for i in ids if i in s.q}

            def candles(s, iid, size, start, end):
                return [b + (0, None) for b in s.series.get(iid, []) if b[0] + dt.timedelta(minutes=5) <= end]

            def expiries(s, u):
                return []

            def option_chain(s, u, e):
                return {}

        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir, instruments_enabled=["equity", "etf"])
        self.m = master()
        self.md = MD()
        self.md.series[12027] = bars([1000, 1000.5, 999.5, 1000.2, 1003])
        self.cm = CostModel.load(synth.COSTS)
        from nse_bot import state
        state.write_json(os.path.join(self.dir, "gate", "%s.json" % gate.strategy_hash(synth.EQ_STRATEGY)),
                         {"status": "APPROVED", "synthetic_data": False, "dataset_digest": None})

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_at(self, now, strategies):
        from nse_bot import bot, risk
        from nse_bot.alerts import Alerts
        from nse_bot.broker.paper import PaperBroker
        self.broker = PaperBroker(self.dir, self.cm, 2)
        ctx = bot.Context(self.cfg, synth.calendar(), self.m, self.md, self.cm, self.broker,
                          risk.RiskBook.open(self.dir, now.date()), Alerts(self.dir, now.date(), sink=lambda a: None),
                          strategies)
        return bot.run_once(ctx, now)

    def test_buys_shares_and_skips_disabled_options(self):
        self.md.q[12027] = quote(bid=1003.0, ask=1003.1, at=t(9, 41), iid=12027, qty=2000, oi=None, volume=2_000_000)
        r = self.run_at(t(9, 41), [synth.EQ_STRATEGY, synth.STRATEGY])
        fills = [x for x in self.broker.ledger.records() if x["event"] == "fill"]
        self.assertEqual(len(fills), 1)
        self.assertEqual((fills[0]["instrument_id"], fills[0]["segment"]), (12027, "equity_intraday"))
        self.assertGreater(fills[0]["qty"], 1)
        self.assertEqual(fills[0]["price"], 1003.3)              # ask + 2 ticks of 0.10
        # exit on the target later in the day
        plan_target = fills[0]["meta"]["plan"]["target"]
        self.md.q[12027] = quote(bid=plan_target + 1, ask=plan_target + 1.1, at=t(10, 30), iid=12027,
                                 qty=2000, oi=None, volume=2_000_000)
        self.run_at(t(10, 30), [synth.EQ_STRATEGY])
        pos = self.broker.ledger.positions()["12027"]
        self.assertEqual(pos["qty"], 0)
        self.assertGreater(pos["realized"], 0)

    def test_unapproved_stock_strategy_blocks(self):
        other = dict(synth.EQ_STRATEGY, name="not-validated")
        self.md.q[12027] = quote(bid=1003.0, ask=1003.1, at=t(9, 41), iid=12027, qty=2000, oi=None, volume=2_000_000)
        r = self.run_at(t(9, 41), [other])
        self.assertEqual(r["candidates"][0]["risk"]["verdict"], "BLOCK")
        self.assertEqual(self.broker.ledger.records(), [])


class History(unittest.TestCase):
    def test_fetch_history_writes_estimated_spread_dataset(self):
        from nse_bot.data import history
        from nse_bot.errors import DataError as DE

        class MD(object):
            def candles(s, iid, size, start, end):
                if iid == 40001:
                    raise DE("HTTP 500")
                return [b + (1000, None) for b in bars([1000, 1001, 1002], start=(9, 15))]
        d = tempfile.mkdtemp()
        try:
            res = history.fetch_equity_history(MD(), master().stocks.values(), synth.calendar(), DAY, DAY, 5, d)
            self.assertEqual(res["symbols"]["RELIANCE"]["bars"], 3)
            self.assertIn("NIFTYBEES", res["errors"])
            ds = store.load_equity(d)
            self.assertFalse(ds.synthetic)
            self.assertEqual((ds.fill_model, ds.spread_pct), ("estimated_spread", 0.10))
        finally:
            shutil.rmtree(d)

    def test_recorder_keeps_real_bid_ask_for_stocks(self):
        from nse_bot.data import recorder

        class MD(object):
            def candles(s, iid, size, start, end):
                return [b + (1000, None) for b in bars([1000, 1001, 1002]) if b[0] == start]

            def quotes(s, ids):
                return {12027: quote(bid=1001.9, ask=1002.1, at=t(9, 25), iid=12027)}
        d = tempfile.mkdtemp()
        try:
            cfg = synth.cfg(d, instruments_enabled=["equity", "etf"])
            for b in bars([1000, 1001, 1002]):
                recorder.record_bar(MD(), master(), cfg, b[0], 5, os.path.join(d, "rec"))
            man = recorder.build_equity(os.path.join(d, "rec"), os.path.join(d, "ds"), "rec", 5)
            self.assertEqual(man["fill_model"], "quotes")
            ds = store.load_equity(os.path.join(d, "ds"))
            self.assertEqual(ds.candle_count(), 6)
            self.assertEqual(ds.bars["RELIANCE"][DAY][0]["ask"], 1002.1)
            self.assertIsNone(ds.bars["NIFTYBEES"][DAY][0]["ask"])      # no quote -> empty, not zero
        finally:
            shutil.rmtree(d)
