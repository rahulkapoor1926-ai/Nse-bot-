"""One shared RiskBook for ORB and Liquidity Sweep (approved by jojo 2026-11-02).

Both strategy processes keep their own ledger (state_dir) but name the same risk_dir, so every
daily limit is for the two strategies combined: 5 filled entries, 2 stop-loss exits, Rs 1,000 net
loss, the Rs 2,000 realised profit stop and one open NIFTY lot.
"""

import datetime as dt
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import synth
from helpers import quote, t
from nse_bot import bot, config, risk, state
from nse_bot.alerts import Alerts
from nse_bot.broker.paper import PaperBroker

DAY = dt.date(2026, 10, 29)
SIZING = {"lots": 1, "loss_at_stop_inr": 400.0}
PLAN = {"reward_risk": 2.5, "net": {"net_reward_risk": 2.1}}
ROOT = synth.ROOT


def order(iid, strategy="orb15-atm", known=None):
    return {"instrument_id": iid, "strategy": strategy, "signal_known_at": known or "k%d" % iid, "side": "BUY"}


def approved_cfg(risk_dir, state_dir, **over):
    """The user's approved limits, as in config/paper.example.json."""
    c = synth.cfg(state_dir, max_daily_loss_inr=1000, max_trades_per_day=5, max_stop_exits_per_day=2,
                  max_open_lots=1, max_open_positions=1, max_lots_per_trade=1, daily_profit_stop_inr=2000,
                  max_risk_per_trade_inr=500, capital_inr=20000)
    c["risk_dir"] = risk_dir
    c.update(over)
    return c


def _race_worker(risk_dir, iid, barrier, out_q):
    """One process: wait for the others, then try to enter through the same guard the bot uses."""
    cfg = approved_cfg(risk_dir, os.path.join(risk_dir, "book%d" % iid))
    book = risk.RiskBook.open(risk_dir, DAY, owner="book%d" % iid)
    barrier.wait()
    entered = False
    with book.entry_guard():
        v = book.check(order(iid), cfg, synth.calendar(), t(10, 0), "APPROVED", [], SIZING, PLAN)
        if v["verdict"] == "PASS":
            book.record_entry(order(iid), t(10, 0))
            entered = True
    out_q.put(entered)


def _churn_worker(risk_dir, n, barrier, out_q):
    """Enter and immediately exit (no stop) as often as allowed; returns how many entries got through."""
    cfg = approved_cfg(risk_dir, os.path.join(risk_dir, "churn%d" % n))
    book = risk.RiskBook.open(risk_dir, DAY, owner="churn%d" % n)
    barrier.wait()
    got = 0
    for k in range(10):
        iid = 1000 * (n + 1) + k
        with book.entry_guard():
            v = book.check(order(iid), cfg, synth.calendar(), t(10, 0), "APPROVED", [], SIZING, PLAN)
            if v["verdict"] == "PASS":
                book.record_entry(order(iid), t(10, 0))
                got += 1
            else:
                continue
        book.record_exit(iid, 1.0, cfg, t(10, 1), reason="target")
    out_q.put(got)


class Shared(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.rdir = os.path.join(self.dir, "state")
        self.cfg = approved_cfg(self.rdir, self.rdir)
        self.cal = synth.calendar()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def books(self):
        return (risk.RiskBook.open(self.rdir, DAY, owner="state"),
                risk.RiskBook.open(self.rdir, DAY, owner="state/sweep"))

    def check(self, book, o, sizing=SIZING, now=None):
        return book.check(o, self.cfg, self.cal, now or t(10, 0), "APPROVED", [], sizing, PLAN)

    # combined position / lot limit ------------------------------------------------

    def test_open_lot_in_one_strategy_blocks_the_other(self):
        orb, sweep = self.books()
        orb.record_entry(order(101), t(10, 0))
        r = self.check(sweep, order(202, "sweep-reversal"))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("open positions at the maximum" in x for x in r["reasons"]))
        self.assertTrue(any("open lots 1 + 1 would exceed the maximum 1" in x for x in r["reasons"]))
        orb.record_exit(101, 50.0, self.cfg, t(10, 30), reason="target")
        self.assertEqual(self.check(sweep, order(202, "sweep-reversal"))["verdict"], "PASS")

    def test_two_lots_are_refused_even_with_no_position_open(self):
        orb, _ = self.books()
        r = self.check(orb, order(101), sizing={"lots": 2, "loss_at_stop_inr": 400.0})
        self.assertTrue(any("would exceed the maximum 1" in x for x in r["reasons"]))

    def test_per_trade_risk_is_checked_again(self):
        orb, _ = self.books()
        r = self.check(orb, order(101), sizing={"lots": 1, "loss_at_stop_inr": 500.01})
        self.assertTrue(any("exceeds the per-trade risk Rs 500.00" in x for x in r["reasons"]))

    # entries / stop-loss exits / loss / profit, combined ----------------------------

    def test_five_entries_across_both_strategies(self):
        orb, sweep = self.books()
        for n in range(5):
            b = orb if n % 2 == 0 else sweep
            self.assertEqual(self.check(b, order(100 + n))["verdict"], "PASS")
            b.record_entry(order(100 + n), t(10, n))
            b.record_exit(100 + n, 5.0, self.cfg, t(10, n), reason="target")
        r = self.check(sweep, order(200, "sweep-reversal"))
        self.assertTrue(any("trade count 5 reached the daily maximum" in x for x in r["reasons"]), r["reasons"])
        self.assertEqual(self.check(orb, order(201))["verdict"], "BLOCK")

    def test_two_stop_loss_exits_one_per_strategy_lock_the_day(self):
        orb, sweep = self.books()
        orb.record_entry(order(101), t(9, 45))
        orb.record_exit(101, -300.0, self.cfg, t(10, 0), reason="stop")
        self.assertFalse(sweep.locked)
        self.assertEqual(sweep.doc["stop_exits"], 1)
        sweep.record_entry(order(202, "sweep-reversal"), t(10, 5))
        sweep.record_exit(202, -300.0, self.cfg, t(10, 20), reason="stop (gapped through: bid 9.00 below stop 9.50)")
        self.assertTrue(orb.locked)
        self.assertIn("2 stop-loss exits today", orb.doc["lockout"]["reason"])
        r = self.check(orb, order(103))
        self.assertTrue(any("stop-loss exits 2 reached the daily maximum 2" in x for x in r["reasons"]))

    def test_target_time_and_invalidation_exits_are_not_stop_losses(self):
        orb, _ = self.books()
        for n, why in enumerate(["target", "time exit at 15:15", "invalidated: underlying 1 < 2"]):
            orb.record_entry(order(110 + n), t(10, n))
            orb.record_exit(110 + n, -10.0, self.cfg, t(10, n), reason=why)
        self.assertEqual(orb.doc["stop_exits"], 0)
        self.assertFalse(orb.locked)

    def test_daily_loss_1000_is_combined(self):
        orb, sweep = self.books()
        orb.record_entry(order(101), t(9, 45))
        orb.record_exit(101, -600.0, self.cfg, t(10, 0), reason="time exit at 15:15")
        self.assertEqual(round(sweep.remaining_loss(self.cfg), 2), 400.0)
        # Rs 500 risk no longer fits what is left of the combined daily limit
        r = self.check(sweep, order(202, "sweep-reversal"), sizing={"lots": 1, "loss_at_stop_inr": 450.0})
        self.assertTrue(any("left of the daily limit Rs 400.00" in x for x in r["reasons"]))
        sweep.record_entry(order(203, "sweep-reversal"), t(10, 5))
        sweep.mark(-400.0, self.cfg, t(10, 6), 203)              # open loss counts too
        self.assertTrue(orb.locked)
        self.assertIn("daily loss limit", orb.doc["lockout"]["reason"])

    def test_profit_stop_blocks_entries_but_open_trade_is_still_managed(self):
        orb, sweep = self.books()
        sweep.record_entry(order(202, "sweep-reversal"), t(10, 0))
        orb.record_exit(1, 2000.0, self.cfg, t(10, 30), reason="target")   # e.g. an earlier ORB trade
        self.assertTrue(sweep.locked)
        self.assertIn("daily profit stop", sweep.doc["lockout"]["reason"])
        self.assertEqual(self.check(orb, order(105))["verdict"], "BLOCK")
        # the open Sweep position keeps being marked and is closed normally
        self.assertTrue(sweep.mark(-50.0, self.cfg, t(10, 40), 202))
        sweep.record_exit(202, -60.0, self.cfg, t(10, 45), reason="stop")
        again = risk.RiskBook.open(self.rdir, DAY)
        self.assertEqual(again.doc["open"], [])
        self.assertEqual(again.doc["realized_pnl"], 1940.0)
        self.assertEqual(again.doc["stop_exits"], 1)

    def test_unrealised_marks_from_two_processes_do_not_overwrite_each_other(self):
        orb, sweep = self.books()
        orb.mark(-100.0, self.cfg, t(10, 0), 101)
        sweep.mark(-200.0, self.cfg, t(10, 0), 202)
        self.assertEqual(risk.RiskBook.open(self.rdir, DAY).doc["unrealized_pnl"], -300.0)

    def test_system_errors_are_counted_per_process(self):
        orb, sweep = self.books()
        orb.record_error(self.cfg, t(10, 0))
        orb.record_error(self.cfg, t(10, 1))
        sweep.record_ok()                            # Sweep's own success must not hide ORB's errors
        orb.record_error(self.cfg, t(10, 2))
        self.assertTrue(sweep.locked)
        self.assertIn("3 consecutive system errors", sweep.doc["lockout"]["reason"])

    # restart, corruption, daily reset ------------------------------------------------

    def test_restart_recovers_every_counter(self):
        orb, sweep = self.books()
        orb.record_entry(order(101), t(9, 45))
        orb.record_exit(101, -200.0, self.cfg, t(10, 0), reason="stop")
        sweep.record_entry(order(202, "sweep-reversal"), t(10, 5))
        sweep.mark(-80.0, self.cfg, t(10, 6), 202)
        del orb, sweep                                   # both processes "crash"
        orb2, sweep2 = self.books()
        for b in (orb2, sweep2):
            self.assertEqual((b.doc["trades"], b.doc["stop_exits"], b.doc["open"], b.open_lots()), (2, 1, [202], 1))
            self.assertEqual(b.doc["realized_pnl"], -200.0)
            self.assertEqual(b.doc["unrealized_pnl"], -80.0)
        r = self.check(orb2, order(101))
        self.assertIn("duplicate: this signal was already entered today", r["reasons"])
        self.assertTrue(any("open positions at the maximum" in x for x in r["reasons"]))

    def test_daily_reset(self):
        orb, sweep = self.books()
        for n in range(2):
            orb.record_entry(order(101 + n), t(10, n))
            orb.record_exit(101 + n, -100.0, self.cfg, t(10, n), reason="stop")
        self.assertTrue(sweep.locked)
        nxt = dt.date(2026, 10, 30)
        fresh = risk.RiskBook.open(self.rdir, nxt, owner="state/sweep")
        self.assertFalse(fresh.locked)
        self.assertEqual((fresh.doc["trades"], fresh.doc["stop_exits"], fresh.doc["realized_pnl"]), (0, 0, 0.0))
        self.assertEqual(fresh.check(order(300), self.cfg, self.cal, t(10, 0, day=nxt), "APPROVED", [], SIZING,
                                     PLAN)["verdict"], "PASS")
        self.assertTrue(risk.RiskBook.open(self.rdir, DAY).locked)        # yesterday stays locked

    def test_corrupt_shared_state_blocks_both_and_is_kept(self):
        orb, sweep = self.books()
        orb.record_entry(order(101), t(10, 0))
        with open(orb.path, "w") as f:
            f.write('{"date": "2026-10-29", "trades": ')
        self.assertTrue(sweep.locked)                    # the running process sees it on its next read
        self.assertEqual(self.check(sweep, order(202, "sweep-reversal"))["verdict"], "BLOCK")
        self.assertTrue(risk.RiskBook.open(self.rdir, DAY).locked)        # and after a restart
        sweep.record_exit(101, 10.0, self.cfg, t(10, 30), reason="target")
        with open(orb.path) as f:
            self.assertEqual(f.read(), '{"date": "2026-10-29", "trades": ')   # evidence never overwritten

    def test_unavailable_lock_blocks_the_entry_but_is_not_sticky(self):
        import fcntl
        orb, _ = self.books()
        old = risk.LOCK_TIMEOUT_SECONDS
        holder = open(os.path.join(self.rdir, "risk", ".lock"), "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            orb._lock.timeout = 0.2
            r = self.check(orb, order(101))
            self.assertEqual(r["verdict"], "BLOCK")
            self.assertTrue(any("risk state unavailable" in x for x in r["reasons"]), r["reasons"])
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
            risk.LOCK_TIMEOUT_SECONDS = old
        self.assertEqual(self.check(orb, order(101))["verdict"], "PASS")

    def test_file_from_the_older_code_is_upgraded(self):
        path = os.path.join(self.rdir, "risk", "2026-10-29.json")
        state.write_json(path, {"date": "2026-10-29", "realized_pnl": -100.0, "unrealized_pnl": -20.0, "trades": 1,
                                "entries": ["x"], "open": [101], "consecutive_errors": 0,
                                "lockout": {"locked": False, "reason": None, "at": None}})
        b = risk.RiskBook.open(self.rdir, DAY)
        self.assertEqual((b.doc["stop_exits"], b.open_lots(), round(b.day_pnl(), 2)), (0, 1, -120.0))

    # simultaneous processes ----------------------------------------------------------

    def _run(self, target, n):
        ctx = multiprocessing.get_context("fork")
        barrier, q = ctx.Barrier(n), ctx.Queue()
        procs = [ctx.Process(target=target, args=(self.rdir, i, barrier, q)) for i in range(n)]
        for p in procs:
            p.start()
        got = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)
        return got

    def test_simultaneous_entries_from_separate_processes_take_one_slot(self):
        os.makedirs(os.path.join(self.rdir, "risk"), exist_ok=True)
        got = self._run(_race_worker, 6)
        self.assertEqual(sum(got), 1)
        b = risk.RiskBook.open(self.rdir, DAY)
        self.assertEqual((b.doc["trades"], len(b.doc["open"]), b.open_lots()), (1, 1, 1))

    def test_simultaneous_churn_never_exceeds_five_entries(self):
        os.makedirs(os.path.join(self.rdir, "risk"), exist_ok=True)
        got = self._run(_churn_worker, 4)
        b = risk.RiskBook.open(self.rdir, DAY)
        self.assertEqual(sum(got), 5)
        self.assertEqual((b.doc["trades"], len(b.doc["entries"]), b.doc["open"]), (5, 5, []))


class BotShares(unittest.TestCase):
    """The ORB paper cycle (bot.run_once) uses the shared book: a Sweep position blocks ORB, and an ORB
    stop-loss exit is counted for both."""

    def setUp(self):
        import test_bot
        test_bot.Cycle.setUp(self)
        self.rdir = tempfile.mkdtemp()
        self.cfg = approved_cfg(self.rdir, self.dir)
        self.approve = lambda: test_bot.Cycle.approve(self)

    def tearDown(self):
        shutil.rmtree(self.dir)
        shutil.rmtree(self.rdir)

    def ctx(self, now):
        return bot.Context(self.cfg, synth.calendar(), self.master, self.md, self.cm, self.broker,
                           risk.RiskBook.open(self.rdir, now.date(), owner=self.dir),
                           Alerts(self.dir, now.date(), sink=self.rang.append), synth.STRATEGY)

    def test_sweep_position_blocks_orb_entry(self):
        self.approve()
        risk.RiskBook.open(self.rdir, DAY, owner="sweep").record_entry(order(999, "sweep-reversal"), t(9, 40))
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        r = bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        c = r["candidates"][0]
        self.assertEqual(c["risk"]["verdict"], "BLOCK")
        self.assertTrue(any("open positions at the maximum" in x for x in c["risk"]["reasons"]))
        self.assertEqual(self.broker.ledger.records(), [])

    def test_orb_stop_exit_is_counted_in_the_shared_book(self):
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))
        b = risk.RiskBook.open(self.rdir, DAY)
        self.assertEqual((b.doc["trades"], b.open_lots()), (1, 1))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "risk", "2026-10-29.json")))   # not per book
        self.md.q[self.ce.instrument_id] = quote(bid=24, ask=24.5, at=t(10, 0), iid=self.ce.instrument_id)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        b = risk.RiskBook.open(self.rdir, DAY)
        self.assertEqual((b.doc["stop_exits"], b.doc["open"], b.open_lots()), (1, [], 0))


class Configuration(unittest.TestCase):
    def test_example_config_has_the_approved_limits_and_options_only(self):
        c = config.load(os.path.join(ROOT, "config", "paper.example.json"))
        self.assertEqual((c["capital_inr"], c["max_risk_per_trade_inr"], c["max_trades_per_day"],
                          c["max_stop_exits_per_day"], c["max_daily_loss_inr"], c["daily_profit_stop_inr"],
                          c["max_open_lots"], c["max_open_positions"], c["max_lots_per_trade"]),
                         (20000, 500, 5, 2, 1000, 2000, 1, 1, 1))
        self.assertEqual(c["instruments_enabled"], ["index_options"])
        self.assertEqual(c["underlyings"], ["NIFTY"])
        self.assertFalse(c["live_orders_enabled"])
        self.assertEqual(c["mode"], "paper")

    def test_more_than_one_open_lot_cannot_be_configured(self):
        c = synth.cfg("/tmp/x", max_open_lots=2)
        with self.assertRaises(config.ConfigError):
            config.validate(c)
        with self.assertRaises(config.ConfigError):
            config.validate(synth.cfg("/tmp/x", max_lots_per_trade=2))

    def test_forward_and_standard_never_share_a_risk_book(self):
        c = synth.cfg(os.path.join("state", "forward", "orb"), forward_validation=True)
        c["risk_dir"] = "state"
        with self.assertRaises(config.ConfigError):
            config.validate(c)
        c["risk_dir"] = os.path.join("state", "forward")
        self.assertIs(config.validate(c), c)

    def test_workflow_gives_orb_and_sweep_one_risk_dir_and_no_equity_strategy(self):
        with open(os.path.join(ROOT, ".github", "workflows", "paper-day.yml")) as f:
            wf = f.read()
        self.assertNotIn("orb15-equity", wf)
        self.assertNotIn("schedule:\n", wf.replace("#   schedule:", ""))     # automatic runs stay off
        # run the workflow's own config steps (standard and forward) and compare the results
        lines = [l.strip() for l in wf.splitlines()]
        sweep_cmd = next(l for l in lines if l.startswith("python3 -c") and "paper-sweep.json','w'" in l)
        fwd_cmd = next(l for l in lines if l.startswith("python3 -c") and "forward_validation']=True" in l)
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "config"))
            shutil.copy(os.path.join(ROOT, "config", "paper.example.json"), os.path.join(d, "config", "paper.json"))
            subprocess.check_call(sweep_cmd, shell=True, cwd=d)
            orb, sw = (state.read_json(os.path.join(d, "config", n)) for n in ("paper.json", "paper-sweep.json"))
            self.assertNotEqual(orb["state_dir"], sw["state_dir"])          # separate ledgers
            self.assertEqual(orb["risk_dir"], sw["risk_dir"])               # one risk book
            for name, sd in (("paper.json", "state/forward/orb"), ("paper-sweep.json", "state/forward/sweep")):
                subprocess.check_call(fwd_cmd.replace('"$1" "$2"', '%s %s' % (name, sd)), shell=True, cwd=d)
            orb = config.load(os.path.join(d, "config", "paper.json"))
            sw = config.load(os.path.join(d, "config", "paper-sweep.json"))
            self.assertEqual((orb["risk_dir"], sw["risk_dir"]), ("state/forward", "state/forward"))
            self.assertNotEqual(orb["state_dir"], sw["state_dir"])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
