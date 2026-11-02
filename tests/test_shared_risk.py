"""ONE shared RiskBook for ORB and Liquidity Sweep (approved specification of 2 Nov 2026).

Both strategy processes (own state_dir, own ledger) open the same risk file, so every limit is combined:
5 filled entries, 2 stop-loss exits, Rs 500 planned net risk a trade, Rs 1,000 daily net loss,
Rs 2,000 realised profit stop (entries blocked, open positions still managed), 1 NIFTY option lot open.
"""

import datetime as dt
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest

import synth
import test_bot
from helpers import quote, t
from nse_bot import bot, config, risk
from nse_bot.alerts import Alerts

DAY = dt.date(2026, 10, 29)
PLAN = {"reward_risk": 2.0, "net": {"net_reward_risk": 2.1}}
ROOT = synth.ROOT


def sizing(loss=300.0, lots=1):
    return {"lots": lots, "loss_at_stop_inr": loss}


def order(iid, strategy, known="2026-10-29T09:40:00+05:30"):
    return {"instrument_id": iid, "strategy": strategy, "signal_known_at": known, "side": "BUY",
            "kind": "index_options", "underlying": "NIFTY"}


class Base(unittest.TestCase):
    """Two configs as the paper-day workflow builds them: ORB in state/, Sweep in state/sweep/,
    both naming the same risk_state_dir."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shared = os.path.join(self.dir, "shared-risk")
        lim = dict(max_daily_loss_inr=1000, max_trades_per_day=5, max_stop_exits_per_day=2,
                   max_risk_per_trade_inr=500, daily_profit_stop_inr=2000, max_open_lots=1,
                   max_open_positions=1, max_lots_per_trade=1, instruments_enabled=["index_options"],
                   risk_state_dir=shared)
        self.orb_cfg = synth.cfg(os.path.join(self.dir, "state"), **lim)
        self.sweep_cfg = synth.cfg(os.path.join(self.dir, "state", "sweep"), **lim)
        self.cal = synth.calendar()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def orb(self):
        return risk.open_for(self.orb_cfg, DAY)

    def sweep(self):
        return risk.open_for(self.sweep_cfg, DAY)

    def check(self, book, o, cfg=None, s=None, now=None):
        return book.check(o, cfg or self.orb_cfg, self.cal, now or t(10, 0), "APPROVED", [], s or sizing(), PLAN)

    def enter(self, book, iid, strategy, cfg, known):
        with book.entry_lock():
            v = self.check(book, order(iid, strategy, known), cfg)
            if v["verdict"] == "PASS":
                book.record_entry(order(iid, strategy, known), t(10, 0), 1)
        return v


class SameBook(Base):
    def test_both_strategies_open_the_same_file(self):
        a, b = self.orb(), self.sweep()
        self.assertEqual(a.path, b.path)
        self.assertEqual(a.path, os.path.join(self.dir, "shared-risk", "risk", "2026-10-29.json"))
        self.assertNotEqual(self.orb_cfg["state_dir"], self.sweep_cfg["state_dir"])     # ledgers stay separate

    def test_example_config_and_the_workflow_sweep_copy_share_one_book(self):
        ex = config.load(os.path.join(ROOT, "config", "paper.example.json"))
        sw = dict(ex, state_dir="state/sweep")             # exactly what paper-day does for the sweep book
        self.assertEqual(risk.dir_for(ex), risk.dir_for(sw))
        self.assertEqual(risk.dir_for(ex), "state/shared-risk")
        wf = open(os.path.join(ROOT, ".github", "workflows", "paper-day.yml")).read()
        self.assertIn("c['state_dir']='state/sweep'", wf)
        self.assertIn("state/forward/shared-risk", wf)    # forward books share their own single book too

    def test_an_orb_entry_is_seen_by_the_sweep_process(self):
        orb, sw = self.orb(), self.sweep()
        self.assertEqual(self.enter(orb, 101, "orb15", self.orb_cfg, "a")["verdict"], "PASS")
        v = self.check(sw, order(202, "sweep_reversal"), self.sweep_cfg)       # sweep's book object, opened earlier
        self.assertEqual(v["verdict"], "BLOCK")
        self.assertIn("open positions at the maximum (1, both strategies combined)", v["reasons"])
        self.assertIn("open lots would be 2, above the maximum 1 (both strategies combined)", v["reasons"])


class CombinedLimits(Base):
    def test_five_filled_entries_across_both_strategies(self):
        books = [(self.orb(), self.orb_cfg, "orb15"), (self.sweep(), self.sweep_cfg, "sweep_reversal")]
        for n in range(5):
            book, cfg, name = books[n % 2]
            self.assertEqual(self.enter(book, 100 + n, name, cfg, "k%d" % n)["verdict"], "PASS", n)
            book.record_exit(100 + n, 10.0, cfg, t(10, 1), reason="target")
        v = self.check(self.sweep(), order(199, "sweep_reversal", "z"), self.sweep_cfg)
        self.assertIn("trade count 5 reached the daily maximum (both strategies combined)", v["reasons"])
        self.assertEqual(self.orb().doc["trades"], 5)

    def test_two_actual_stop_exits_stop_new_entries(self):
        orb, sw = self.orb(), self.sweep()
        self.enter(orb, 101, "orb15", self.orb_cfg, "a")
        orb.record_exit(101, -100.0, self.orb_cfg, t(10, 5), reason="time exit at 15:15")    # not a stop
        self.enter(orb, 102, "orb15", self.orb_cfg, "b")
        orb.record_exit(102, -150.0, self.orb_cfg, t(10, 10), reason="stop")
        self.assertEqual(self.orb().doc["stop_exits"], 1)
        self.assertFalse(self.sweep().locked)
        self.enter(sw, 201, "sweep_reversal", self.sweep_cfg, "c")
        sw.record_exit(201, -150.0, self.sweep_cfg, t(10, 20), reason="stop (gapped through: bid 9.00 below stop 10.00)")
        again = self.orb()
        self.assertEqual(again.doc["stop_exits"], 2)
        self.assertTrue(again.locked)
        self.assertIn("2 stop-loss exits today", again.doc["lockout"]["reason"])
        self.assertEqual(self.check(again, order(103, "orb15", "d"))["verdict"], "BLOCK")

    def test_rs_500_planned_net_risk_per_trade(self):
        v = self.check(self.orb(), order(101, "orb15"), s=sizing(loss=500.01))
        self.assertIn("planned net risk Rs 500.01 exceeds the per-trade maximum Rs 500.00", v["reasons"])
        self.assertEqual(self.check(self.orb(), order(101, "orb15"), s=sizing(loss=500.0))["verdict"], "PASS")

    def test_rs_1000_daily_net_loss_is_combined(self):
        orb, sw = self.orb(), self.sweep()
        self.enter(orb, 101, "orb15", self.orb_cfg, "a")
        orb.record_exit(101, -600.0, self.orb_cfg, t(10, 5), reason="invalidated")
        self.assertFalse(self.sweep().locked)                    # Rs 600 alone is inside Rs 1,000
        self.assertEqual(self.sweep().remaining_loss(self.sweep_cfg), 400.0)
        v = self.check(sw, order(201, "sweep_reversal"), self.sweep_cfg, s=sizing(loss=450.0))
        self.assertIn("loss at stop Rs 450.00 exceeds what is left of the daily limit Rs 400.00", v["reasons"])
        self.enter(sw, 201, "sweep_reversal", self.sweep_cfg, "b")
        sw.mark(-420.0, self.sweep_cfg, t(10, 30))                 # open loss of the other strategy counts
        book = self.orb()
        self.assertEqual(book.day_pnl(), -1020.0)
        self.assertTrue(book.locked)
        self.assertIn("daily loss limit reached: Rs -1020.00 (both strategies combined)", book.doc["lockout"]["reason"])

    def test_rs_2000_profit_stop_blocks_entries_but_open_positions_are_managed(self):
        orb, sw = self.orb(), self.sweep()
        self.enter(sw, 201, "sweep_reversal", self.sweep_cfg, "s")    # still open when ORB books its profit
        sw.record_exit(201, 0.0, self.sweep_cfg, t(10, 0), reason="x")
        self.enter(sw, 202, "sweep_reversal", self.sweep_cfg, "s2")
        orb.record_exit(999, 2000.0, self.orb_cfg, t(11, 0), reason="target")
        book = self.sweep()
        self.assertTrue(book.locked)
        self.assertIn("daily profit stop reached", book.doc["lockout"]["reason"])
        self.assertIn("open positions are still managed", book.doc["lockout"]["reason"])
        sw.mark(-50.0, self.sweep_cfg, t(11, 5))                       # the open sweep trade is still marked
        sw.record_exit(202, -80.0, self.sweep_cfg, t(11, 10), reason="stop")   # and closed
        end = self.orb()
        self.assertEqual((end.doc["realized_pnl"], end.doc["open"], end.doc["stop_exits"]), (1920.0, [], 1))
        self.assertEqual(end.doc["unrealized_pnl"], 0.0)

    def test_one_lot_open_across_both(self):
        orb = self.orb()
        v = self.check(orb, order(101, "orb15"), s=sizing(lots=2))
        self.assertIn("open lots would be 2, above the maximum 1 (both strategies combined)", v["reasons"])


def _racer(cfg, iid, name, barrier, out):
    """One strategy process: lock, check, paper-fill, record (what bot.py and sweep_live.py do)."""
    book = risk.open_for(cfg, DAY)
    barrier.wait()
    with book.entry_lock():
        v = book.check(order(iid, name, "race"), cfg, synth.calendar(), t(10, 0), "APPROVED", [], sizing(), PLAN)
        if v["verdict"] == "PASS":
            book.record_entry(order(iid, name, "race"), t(10, 0), 1)
    out.put((name, v["verdict"]))


class Simultaneous(Base):
    def test_two_processes_cannot_both_take_the_one_slot(self):
        ctx = multiprocessing.get_context("fork")
        for rnd in range(5):
            day_dir = os.path.join(self.dir, "shared-risk", "risk")
            shutil.rmtree(day_dir, ignore_errors=True)
            barrier, out = ctx.Barrier(2), ctx.Queue()
            ps = [ctx.Process(target=_racer, args=(self.orb_cfg, 101, "orb15", barrier, out)),
                  ctx.Process(target=_racer, args=(self.sweep_cfg, 202, "sweep_reversal", barrier, out))]
            for p in ps:
                p.start()
            for p in ps:
                p.join(30)
            got = sorted(out.get(timeout=5)[1] for _ in ps)
            self.assertEqual(got, ["BLOCK", "PASS"], rnd)
            doc = self.orb().doc
            self.assertEqual((doc["trades"], len(doc["open"]), sum(doc["open_lots"].values())), (1, 1, 1), rnd)

    def test_a_held_lock_blocks_instead_of_waiting_forever(self):
        saved = risk.LOCK_TIMEOUT_S
        risk.LOCK_TIMEOUT_S = 0.3
        try:
            holder = self.orb()
            with holder.transaction():
                ctx = multiprocessing.get_context("fork")
                out = ctx.Queue()

                def other():
                    b = risk.open_for(self.sweep_cfg, DAY)
                    out.put((b.doc["lockout"]["locked"], self.check(b, order(202, "sweep_reversal"),
                                                                    self.sweep_cfg)["verdict"]))
                p = ctx.Process(target=other)
                p.start()
                p.join(10)
                locked, verdict = out.get(timeout=5)
            self.assertTrue(locked)
            self.assertEqual(verdict, "BLOCK")
        finally:
            risk.LOCK_TIMEOUT_S = saved


class Restart(Base):
    def test_counts_and_open_lot_survive_a_restart(self):
        orb = self.orb()
        self.enter(orb, 101, "orb15", self.orb_cfg, "a")
        orb.record_exit(101, -200.0, self.orb_cfg, t(10, 5), reason="stop")
        self.enter(orb, 102, "orb15", self.orb_cfg, "b")
        del orb                                                  # process gone
        back = self.sweep()                                      # any process, after a restart
        self.assertEqual((back.doc["trades"], back.doc["stop_exits"], back.doc["realized_pnl"],
                          back.doc["open"], back.open_lots()), (2, 1, -200.0, [102], 1))
        self.assertIn("duplicate: this signal was already entered today",
                      self.check(self.orb(), order(102, "orb15", "b"))["reasons"])

    def test_corrupt_shared_file_locks_both_strategies_and_is_kept(self):
        self.orb()
        path = self.orb().path
        with open(path, "w") as f:
            f.write('{"date": "2026-10-29", "trades": 3')
        for b, cfg in ((self.orb(), self.orb_cfg), (self.sweep(), self.sweep_cfg)):
            self.assertTrue(b.locked)
            self.assertEqual(self.check(b, order(101, "x"), cfg)["verdict"], "BLOCK")
            b.record_exit(101, -10.0, cfg, t(10, 0), reason="stop")
        with open(path) as f:
            self.assertEqual(f.read(), '{"date": "2026-10-29", "trades": 3')     # evidence untouched

    def test_unreadable_risk_folder_fails_closed(self):
        blocker = os.path.join(self.dir, "shared-risk")
        with open(blocker, "w") as f:                            # a file where the folder should be
            f.write("x")
        b = self.orb()
        self.assertTrue(b.locked)
        self.assertEqual(self.check(b, order(101, "orb15"))["verdict"], "BLOCK")

    def test_an_older_book_file_is_upgraded_not_reset(self):
        path = self.orb().path
        old = {"date": "2026-10-29", "realized_pnl": -300.0, "unrealized_pnl": 0.0, "trades": 2,
               "entries": ["a", "b"], "open": [], "consecutive_errors": 0,
               "lockout": {"locked": False, "reason": None, "at": None}}
        with open(path, "w") as f:
            json.dump(old, f)
        b = self.sweep()
        self.assertEqual((b.doc["trades"], b.doc["realized_pnl"], b.doc["stop_exits"]), (2, -300.0, 0))
        self.assertFalse(b.locked)


class LiveBotUsesTheSharedBook(unittest.TestCase):
    """bot.run_once (ORB) is blocked by a position the sweep process holds in the shared book."""
    tearDown = test_bot.Cycle.tearDown
    approve = test_bot.Cycle.approve

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg.update(max_lots_per_trade=1, max_open_lots=1, risk_state_dir=os.path.join(self.dir, "shared"))
        self.sweep_cfg = dict(self.cfg, state_dir=os.path.join(self.dir, "sweep"))

    def ctx(self, now):
        return bot.Context(self.cfg, synth.calendar(), self.master, self.md, self.cm, self.broker,
                           risk.open_for(self.cfg, now.date()), Alerts(self.dir, now.date(), sink=self.rang.append),
                           synth.STRATEGY)

    def test_orb_blocked_while_sweep_holds_the_lot_then_enters_after(self):
        self.approve()
        sw = risk.open_for(self.sweep_cfg, DAY)
        sw.record_entry(order(555, "sweep_reversal", "s"), t(9, 35), 1)
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        r = bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        self.assertIn("open positions at the maximum (1, both strategies combined)", r["candidates"][0]["risk"]["reasons"])
        self.assertEqual(self.broker.ledger.records(), [])
        sw.record_exit(555, 40.0, self.sweep_cfg, t(9, 42), reason="target")
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 43), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(9, 43)), t(9, 43))
        fills = [x for x in self.broker.ledger.records() if x["event"] == "fill"]
        self.assertEqual(len(fills), 1)
        doc = risk.open_for(self.sweep_cfg, DAY).doc
        self.assertEqual((doc["trades"], doc["open"], doc["open_owner"][str(self.ce.instrument_id)]),
                         (2, [self.ce.instrument_id], self.cfg["state_dir"]))


if __name__ == "__main__":
    unittest.main()
