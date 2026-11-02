"""sweep_reversal inside the paper loop: same gate/budget/risk chain, reports saved, nothing entered unvalidated."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import sweep_synth as S
import synth
from helpers import quote
from nse_bot import bot, gate, instruments, risk, state, structure as st, sweep_live
from nse_bot.alerts import Alerts
from nse_bot.broker.base import MarketData
from nse_bot.broker.paper import PaperBroker
from nse_bot.costs import CostModel
from test_bot import INDEX_ROW
from test_instruments import HEADER, chain_rows

FUT_ROW = "4000001,,NIFTY24NOV26FUT,,,NIFTY,NSE,NFO,1,FUTIDX,,24-11-2026,0.00,65,0.10,1800,2,1"


class FakeMD(MarketData):
    def __init__(self, data, chain=None):
        self.d, self.chain = data, chain or S.chain_doc()
        self.calls = []
        self.at = S.NOW                                   # quote time: the cycle's time (a live, fresh quote)

    def candles(self, iid, size, start, end):
        self.calls.append((iid, size))
        if iid == 4000001:
            src = self.d["f1"]
        else:
            src = {"1m": self.d["u1"], "1h": self.d["u1h"], "1d": self.d["u1d"],
                   "5m": st.resample(self.d["u1"], 5)}[size]
        return [tuple(b[:5]) + (b[5] if len(b) > 5 else None, None) for b in src if start <= b[0] <= end]

    def quotes(self, ids):
        return {i: quote(bid=150.0, ask=150.1, at=self.at, iid=i, qty=1300) for i in ids}

    def expiries(self, u):
        return []

    def option_chain(self, u, e):
        return self.chain


ENTRY = S.NOW + dt.timedelta(minutes=1)          # one minute after the signal minute, as the backtest fills


class Paper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)
        self.master = instruments.parse(HEADER + "\n" + INDEX_ROW + "\n" + FUT_ROW + "\n" + chain_rows(),
                                        index_names=("Nifty 50",))
        self.md = FakeMD(S.build())
        self.cm = CostModel.load(synth.COSTS)
        self.broker = PaperBroker(self.dir, self.cm, 2)
        sweep_live._CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def cycle(self, now=S.NOW):
        self.md.at = now
        ctx = bot.Context(self.cfg, synth.calendar(), self.master, self.md, self.cm, self.broker,
                          risk.RiskBook.open(self.dir, now.date()), Alerts(self.dir, now.date(), sink=lambda a: None),
                          [S.STRATEGY])
        return bot.run_once(ctx, now)

    def reports(self):
        return state.read_jsonl(os.path.join(self.dir, "decisions", "%s.jsonl" % S.DAY.isoformat()))

    def test_future_is_found_in_the_master(self):
        f = self.master.current_future("NIFTY", S.DAY)
        self.assertEqual(f.symbol, "NIFTY24NOV26FUT")

    def test_setup_waits_for_the_backtest_entry_minute(self):
        self.cycle()                                      # the signal minute itself: the backtest fills a minute later
        [rep] = self.reports()
        self.assertEqual(rep["decision"], "WAIT")
        self.assertIn("waiting for the entry point", rep["execution"]["entry_check"]["reasons"][0])

    def test_setup_without_gate_approval_is_blocked_and_reported(self):
        self.cycle(ENTRY)
        [rep] = self.reports()
        self.assertEqual(rep["decision"], "BLOCKED")
        self.assertEqual(rep["evidence_check"], "ok")
        self.assertEqual(rep["execution"]["gate"], "NOT EVALUATED")
        self.assertTrue(any("validation gate" in r for r in rep["execution"]["risk_reasons"]))
        self.assertEqual([r for r in self.broker.ledger.records() if r["event"] == "fill"], [])
        self.assertEqual(rep["raw_refs"]["future"]["symbol"], "NIFTY24NOV26FUT")

    def test_approved_but_too_risky_for_rs_500_is_still_blocked(self):
        state.write_json(os.path.join(self.dir, "gate", "%s.json" % gate.strategy_hash(S.STRATEGY)),
                         {"status": "APPROVED", "synthetic_data": False, "dataset_digest": None})
        self.cycle(ENTRY)
        [rep] = self.reports()
        self.assertEqual(rep["decision"], "BLOCKED")
        self.assertEqual(rep["execution"]["sizing"]["lots"], 0)
        self.assertTrue(any("zero lots" in r for r in rep["execution"]["risk_reasons"]))
        self.assertEqual([r for r in self.broker.ledger.records() if r["event"] == "fill"], [])

    def test_repeated_identical_outcome_is_stored_once_and_public_summary_has_no_quotes(self):
        self.cycle()
        self.cycle(S.NOW.replace(second=50))
        self.assertEqual(len(self.reports()), 1)
        pub = state.read_jsonl(os.path.join(self.dir, "dashboard", "decisions-%s.jsonl" % S.DAY.isoformat()))
        self.assertEqual(len(pub), 1)
        text = json.dumps(pub)
        for k in ("bid", "ask", "delta", "contract_candidates", "evidence"):
            self.assertNotIn('"%s"' % k, text)

    def test_missing_future_fails_closed(self):
        self.master.futures = []
        self.cycle()
        [rep] = self.reports()
        self.assertEqual(rep["decision"], "SKIP")
        self.assertTrue(rep["skip_reason"].startswith("data_future"))
        self.assertEqual(rep["future"], "no current-month index future in the scrip master")

    def test_candles_fetched_once_per_minute(self):
        self.cycle()
        n = len(self.md.calls)
        self.cycle(S.NOW.replace(second=50))
        self.assertEqual(len([c for c in self.md.calls[n:] if c[1] != "5m"]), 0)


class Recorder(unittest.TestCase):
    """record --sweep writes what sweep_store.load reads back, from real-shaped SAHI rows only."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(os.path.join(self.dir, "state"))
        self.master = instruments.parse(HEADER + "\n" + INDEX_ROW + "\n" + FUT_ROW + "\n" + chain_rows(),
                                        index_names=("Nifty 50",))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_round_trip_is_real_and_loadable(self):
        from nse_bot.data import recorder, sweep_store
        md = FakeMD(S.build())
        rec = os.path.join(self.dir, "rec")
        cal = synth.calendar()
        starts = [S.NOW.replace(second=0) - dt.timedelta(minutes=k) for k in (3, 2, 1)]
        for t in starts:
            self.assertGreater(recorder.record_sweep_minute(md, self.master, self.cfg, cal, t, rec), 2)
        out = os.path.join(self.dir, "ds")
        doc = recorder.build_sweep(rec, out, "rec-test", "NIFTY")
        self.assertIs(doc["synthetic"], False)
        ds = sweep_store.load(out)
        self.assertEqual([b[0] for b in ds.u1], starts)
        self.assertEqual(len(ds.f1), 3)
        self.assertTrue(all(b[5] is not None for b in ds.f1))
        self.assertEqual(sorted(ds.chains), [t + dt.timedelta(minutes=1) for t in starts])
        self.assertTrue(ds.contracts)
        self.assertEqual(ds.rejected, {})

    def test_missing_future_or_chain_records_only_what_exists(self):
        from nse_bot.data import recorder, sweep_store
        d = S.build()
        d["f1"] = []
        rec = os.path.join(self.dir, "rec")
        t = S.NOW.replace(second=0) - dt.timedelta(minutes=1)
        recorder.record_sweep_minute(FakeMD(d), self.master, self.cfg, synth.calendar(), t, rec)
        recorder.build_sweep(rec, os.path.join(self.dir, "ds"), "x", "NIFTY")
        ds = sweep_store.load(os.path.join(self.dir, "ds"))
        self.assertEqual((len(ds.u1), len(ds.f1)), (1, 0))      # nothing invented for the gap
