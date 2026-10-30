"""Integration: one paper cycle end to end with a fake read-only market-data source."""

import datetime as dt
import os
import shutil
import tempfile
import unittest

import synth
from helpers import quote, t
from nse_bot import bot, gate, instruments, risk, state
from nse_bot.alerts import Alerts
from nse_bot.broker.base import MarketData
from nse_bot.broker.paper import PaperBroker
from nse_bot.costs import CostModel
from nse_bot.errors import DataError
from test_instruments import HEADER, chain_rows
from test_patterns_scanner import bars

DAY = dt.date(2026, 10, 29)
INDEX_ROW = "26000,,,Nifty 50,,,NSE,NSE,26000,IDX,,,0.00,0,0.00,0,0,0"


class FakeMD(MarketData):
    def __init__(self):
        self.bars = bars([25000, 25005, 24995, 25003, 25030, 25040, 25045, 25050])
        self.q = {}
        self.fail = False

    def quotes(self, ids):
        if self.fail:
            raise DataError("network down")
        return {i: self.q[i] for i in ids if i in self.q}

    def candles(self, iid, size, start, end):
        return [b + (0, None) for b in self.bars if b[0] + dt.timedelta(minutes=5) <= end]

    def expiries(self, u):
        return []

    def option_chain(self, u, e):
        return {}


class Cycle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)
        self.master = instruments.parse(HEADER + "\n" + INDEX_ROW + "\n" + chain_rows(), index_names=("Nifty 50",))
        self.md = FakeMD()
        self.cm = CostModel.load(synth.COSTS)
        self.broker = PaperBroker(self.dir, self.cm, 2)
        self.rang = []
        self.ce = self.master.pick("NIFTY", dt.date(2026, 11, 3), "CE", 25030, 0)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def ctx(self, now):
        return bot.Context(self.cfg, synth.calendar(), self.master, self.md, self.cm, self.broker,
                           risk.RiskBook.open(self.dir, now.date()), Alerts(self.dir, now.date(), sink=self.rang.append),
                           synth.STRATEGY)

    def approve(self):
        state.write_json(os.path.join(self.dir, "gate", "%s.json" % gate.strategy_hash(synth.STRATEGY)),
                         {"status": "APPROVED", "synthetic_data": False, "dataset_digest": None})

    def test_closed_market_does_nothing(self):
        r = bot.run_once(self.ctx(t(8, 0)), t(8, 0))
        self.assertFalse(r["market"]["open"])

    def test_not_validated_blocks_but_alerts_candidate(self):
        now = t(9, 41)
        self.md.q[self.ce.instrument_id] = quote(bid=100, ask=100.5, at=t(9, 41), iid=self.ce.instrument_id)
        r = bot.run_once(self.ctx(now), now)
        c = r["candidates"][0]
        self.assertEqual(c["risk"]["verdict"], "BLOCK")
        self.assertIn("validation gate: NOT EVALUATED", c["risk"]["reasons"])
        self.assertEqual(self.broker.ledger.records(), [])
        self.assertEqual(self.rang[0]["kind"], "candidate")

    def test_approved_entry_then_stop_exit(self):
        self.approve()
        self.cfg["max_lots_per_trade"] = 1
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        pos = self.broker.ledger.positions()[str(self.ce.instrument_id)]
        self.assertEqual(pos["qty"], 65)
        self.assertEqual(risk.RiskBook.open(self.dir, DAY).doc["trades"], 1)
        # duplicate cycle: same signal, already holding -> no second buy
        bot.run_once(self.ctx(t(9, 42)), t(9, 42))
        self.assertEqual(len([r for r in self.broker.ledger.records() if r["event"] == "fill"]), 1)
        # price falls through the stop
        self.md.q[self.ce.instrument_id] = quote(bid=20, ask=20.5, at=t(10, 0), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        self.assertEqual(self.broker.ledger.positions()[str(self.ce.instrument_id)]["qty"], 0)
        book = risk.RiskBook.open(self.dir, DAY)
        self.assertLess(book.doc["realized_pnl"], -600)          # gap through the stop: worse than planned
        self.assertTrue(book.locked)
        self.assertTrue(any(a["kind"] == "stop" for a in self.rang))

    def test_failed_quote_keeps_position_open_and_alerts(self):
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        self.md.fail = True
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        self.assertEqual(self.broker.ledger.positions()[str(self.ce.instrument_id)]["qty"], 65)
        kinds = [a["kind"] for a in self.rang]
        self.assertIn("system_error", kinds)
        self.assertIn("stale_data", kinds)

    def test_stale_quote_at_exit_is_not_an_exit(self):
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        self.md.q[self.ce.instrument_id] = quote(bid=1, ask=1.5, at=t(9, 41), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(15, 20)), t(15, 20))          # time exit due, but quote is 40 minutes old
        self.assertEqual(self.broker.ledger.positions()[str(self.ce.instrument_id)]["qty"], 65)

    def test_lockout_stops_new_entries(self):
        self.approve()
        book = risk.RiskBook.open(self.dir, DAY)
        book.record_exit(1, -501.0, self.cfg, t(9, 35))
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        r = bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        self.assertTrue(r["locked"])
        self.assertEqual(self.broker.ledger.records(), [])

    def test_repeated_data_failures_lock_the_day(self):
        self.approve()
        self.md.fail = True
        orig = self.md.candles
        self.md.candles = lambda *a: (_ for _ in ()).throw(DataError("feed down"))
        for m in range(3):
            bot.run_once(self.ctx(t(9, 41 + m)), t(9, 41 + m))
        self.assertTrue(risk.RiskBook.open(self.dir, DAY).locked)
        self.md.candles = orig


class Recorder(unittest.TestCase):
    def test_records_real_shape_and_builds_non_synthetic_dataset(self):
        from nse_bot.data import recorder, store
        d = tempfile.mkdtemp()
        try:
            master = instruments.parse(HEADER + "\n" + INDEX_ROW + "\n" + chain_rows(), index_names=("Nifty 50",))
            md = FakeMD()
            ce = master.pick("NIFTY", dt.date(2026, 11, 3), "CE", 25030, 0)
            md.q[ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 40), iid=ce.instrument_id)
            for i, b in enumerate(md.bars[:5]):
                recorder.record_bar(md, master, synth.cfg(d), b[0], 5, os.path.join(d, "rec"), 2)
            ds_dir = os.path.join(d, "ds")
            man = recorder.build(os.path.join(d, "rec"), ds_dir, "rec-test", 5)
            self.assertFalse(man["synthetic"])
            ds = store.load(ds_dir)
            self.assertEqual(ds.candle_count(), 5)
            rows = ds.options[("NIFTY", DAY)]
            with_quote = [r for k, bars_ in rows.items() for r in bars_.values() if r["bid"] is not None]
            without = [r for k, bars_ in rows.items() for r in bars_.values() if r["bid"] is None]
            self.assertTrue(with_quote)
            self.assertTrue(without)                 # unknown quotes stay empty, not zero
        finally:
            shutil.rmtree(d)
