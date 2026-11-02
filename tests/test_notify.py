"""P&L notifications: correct figures from the ledger, no P&L on unverified prices, never affect trading."""

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import unittest

import test_bot
from helpers import quote, t
from nse_bot import bot, notify, risk
from nse_bot.alerts import Alerts
from nse_bot.notify import Notifier

SRC = open(os.path.join(os.path.dirname(__file__), "..", "nse_bot", "notify.py")).read()
ENV = {"TELEGRAM_BOT_TOKEN": "123:SECRET-TOKEN", "TELEGRAM_CHAT_ID": "999"}


def fq(bid, ask, at, iid, source="fyers"):
    return dataclasses.replace(quote(bid=bid, ask=ask, at=at, iid=iid), source=source)


class Phone(object):
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, token, chat, text):
        if self.fail:
            raise OSError("network down for token %s" % token)
        self.sent.append(text)


class Flow(unittest.TestCase):
    """Full paper flow through bot.run_once with a notifier attached (fixtures from test_bot.Cycle)."""
    approve = test_bot.Cycle.approve
    tearDown = test_bot.Cycle.tearDown

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["data_source"] = "fyers"
        self.phone = Phone()
        self.printed = []
        self.iid = self.ce.instrument_id

    def ctx(self, now):
        n = Notifier(self.dir, now.date(), self.cfg, ENV, self.phone, self.printed.append)
        return bot.Context(self.cfg, test_bot.synth.calendar(), self.master, self.md, self.cm, self.broker,
                           risk.RiskBook.open(self.dir, now.date()),
                           Alerts(self.dir, now.date(), sink=self.rang.append), test_bot.synth.STRATEGY,
                           notifier=n)

    def enter(self):
        self.approve()
        self.cfg["max_lots_per_trade"] = 1
        self.md.q[self.iid] = fq(30, 30.5, t(9, 41), self.iid)
        bot.run_once(self.ctx(t(9, 41)), t(9, 41))

    def test_open_message_has_every_field(self):
        self.enter()
        fill = [r for r in self.broker.ledger.records() if r["event"] == "fill"][0]
        msg = self.phone.sent[0]
        self.assertIn("PAPER TRADE OPENED", msg)
        for part in (self.ce.symbol, " CE", "%.0f" % self.ce.strike, self.ce.expiry.isoformat(),
                     "Rs %.2f x 65" % fill["price"], "Initial stop-loss: Rs", "Trailing stop: none"):
            self.assertIn(part, msg)
        stop = fill["meta"]["plan"]["stop"]
        self.assertIn("Initial stop-loss: Rs %.2f" % stop, msg)

    def test_update_uses_the_verified_bid_and_is_throttled(self):
        self.enter()
        self.md.q[self.iid] = fq(33, 33.5, t(9, 50), self.iid)
        bot.run_once(self.ctx(t(9, 50)), t(9, 50))
        upd = [m for m in self.phone.sent if m.startswith("PAPER P&L UPDATE")]
        self.assertEqual(len(upd), 1)
        pos = self.broker.ledger.positions()[str(self.iid)]
        est_exit = self.cm.leg("SELL", 33, 65, t(9, 50).date(), self.ce.cost_segment)["total"]
        u = (33 - pos["cost"] / 65) * 65 - pos["charges"] - est_exit
        self.assertIn("Current bid: Rs 33.00 (fyers, quote time 09:50:00 IST)", upd[0])
        self.assertIn("Unrealized P&L: %s%s" % ("+" if u >= 0 else "-", notify._rs(abs(round(u, 2)))), upd[0])
        pct = 100 * round(u, 2) / (pos["cost"] + pos["charges"])
        self.assertIn("(%+.1f%% of" % pct, upd[0])
        self.assertIn("Stop-loss: Rs", upd[0])
        # 2 minutes later: no new update (every 5 minutes)
        self.md.q[self.iid] = fq(33, 33.5, t(9, 52), self.iid)
        bot.run_once(self.ctx(t(9, 52)), t(9, 52))
        self.assertEqual(len([m for m in self.phone.sent if m.startswith("PAPER P&L UPDATE")]), 1)
        self.md.q[self.iid] = fq(33, 33.5, t(9, 55), self.iid)
        bot.run_once(self.ctx(t(9, 55)), t(9, 55))
        self.assertEqual(len([m for m in self.phone.sent if m.startswith("PAPER P&L UPDATE")]), 2)

    def test_stale_price_reports_no_pnl(self):
        self.enter()
        self.md.q[self.iid] = fq(33, 33.5, t(9, 40), self.iid)          # 10 minutes old
        bot.run_once(self.ctx(t(9, 50)), t(9, 50))
        msg = self.phone.sent[-1]
        self.assertIn("PRICE NOT VERIFIED", msg)
        self.assertIn("P&L not shown", msg)
        self.assertNotIn("Unrealized", msg)
        self.assertEqual(self.broker.ledger.positions()[str(self.iid)]["qty"], 65)   # still open

    def test_other_source_is_not_verified(self):
        self.enter()
        self.md.q[self.iid] = fq(33, 33.5, t(9, 50), self.iid, source="sahi")
        bot.run_once(self.ctx(t(9, 50)), t(9, 50))
        self.assertIn("PRICE NOT VERIFIED", self.phone.sent[-1])
        self.assertNotIn("Unrealized", self.phone.sent[-1])

    def test_close_report_matches_the_ledger(self):
        self.enter()
        self.md.q[self.iid] = fq(20, 20.5, t(10, 0), self.iid)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        fills = [r for r in self.broker.ledger.records() if r["event"] == "fill"]
        buy, sell = fills
        msg = [m for m in self.phone.sent if m.startswith("PAPER TRADE CLOSED")][0]
        gross = (sell["price"] - buy["price"]) * 65
        costs = buy["charges"]["total"] + sell["charges"]["total"]
        realized = self.broker.ledger.positions()[str(self.iid)]["realized"]
        self.assertAlmostEqual(gross - costs, realized, places=2)
        self.assertIn("Entry: Rs %.2f | Exit: Rs %.2f (bid - slippage) | Qty: 65" % (buy["price"], sell["price"]), msg)
        self.assertIn("Gross P&L: -%s" % notify._rs(abs(gross)), msg)
        self.assertIn("Entry charges: %s (brokerage" % notify._rs(buy["charges"]["total"]), msg)
        self.assertIn("Exit charges: %s (brokerage" % notify._rs(sell["charges"]["total"]), msg)
        self.assertIn("stt", msg)
        self.assertIn("NET P&L: -%s" % notify._rs(abs(realized)), msg)
        self.assertIn("Reason: stop", msg)

    def test_daily_summary(self):
        self.enter()
        self.md.q[self.iid] = fq(20, 20.5, t(10, 0), self.iid)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        n = Notifier(self.dir, t(15, 35).date(), self.cfg, ENV, self.phone, self.printed.append)
        n.daily_summary([("paper", self.broker.ledger.records())], t(15, 35).date(), t(15, 35),
                        {str(self.iid): self.ce.symbol})
        msg = self.phone.sent[-1]
        realized = self.broker.ledger.positions()[str(self.iid)]["realized"]
        self.assertIn("Trades closed: 1 | Winning: 0 | Losing: 1", msg)
        self.assertIn("NET P&L: -%s" % notify._rs(abs(realized)), msg)
        self.assertIn(self.ce.symbol, msg)

    def test_telegram_failure_never_affects_trading(self):
        self.phone.fail = True
        self.enter()
        self.assertEqual(self.broker.ledger.positions()[str(self.iid)]["qty"], 65)
        self.md.q[self.iid] = fq(20, 20.5, t(10, 0), self.iid)
        bot.run_once(self.ctx(t(10, 0)), t(10, 0))
        self.assertEqual(self.broker.ledger.positions()[str(self.iid)]["qty"], 0)
        with open(os.path.join(self.dir, "notify", "notify.jsonl")) as f:
            raw = f.read()
        log = [json.loads(l) for l in raw.splitlines()]
        self.assertTrue(log and not any(x["delivered"] for x in log))
        everything = raw + "".join(self.printed)
        self.assertNotIn("SECRET-TOKEN", everything)

    def test_broken_notifier_never_affects_trading(self):
        self.approve()
        self.cfg["max_lots_per_trade"] = 1
        self.md.q[self.iid] = fq(30, 30.5, t(9, 41), self.iid)
        ctx = self.ctx(t(9, 41))
        ctx.notify.send = None              # any internal error
        bot.run_once(ctx, t(9, 41))
        self.assertEqual(self.broker.ledger.positions()[str(self.iid)]["qty"], 65)


class Units(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_no_credentials_means_log_only(self):
        out = []
        n = Notifier(self.dir, dt.date(2026, 11, 2), {}, {}, Phone(), out.append)
        self.assertEqual(n.channel, "log only")
        self.assertFalse(n.send("summary", "hello", t(10, 0)))
        self.assertEqual(out, ["[NOTIFY summary] hello"])

    def test_stops_trying_after_repeated_failures(self):
        calls = []

        def bad(*a):
            calls.append(1)
            raise OSError("down")
        n = Notifier(self.dir, dt.date(2026, 11, 2), {}, ENV, bad, lambda s: None)
        for _ in range(6):
            n.send("update", "x", t(10, 0))
        self.assertEqual(len(calls), notify.MAX_FAILURES)

    def test_round_trips_partial_exit(self):
        recs = [
            {"event": "fill", "side": "BUY", "instrument_id": 1, "qty": 130, "price": 10.0,
             "charges": {"total": 20.0}, "time": "2026-11-02T09:30:00+05:30"},
            {"event": "fill", "side": "SELL", "instrument_id": 1, "qty": 65, "price": 12.0,
             "charges": {"total": 15.0}, "time": "2026-11-02T10:00:00+05:30"},
        ]
        trades, still_open = notify.round_trips(recs, dt.date(2026, 11, 2))
        self.assertEqual(trades[0]["gross"], 130.0)
        self.assertEqual(trades[0]["costs"], 25.0)
        self.assertEqual(trades[0]["net"], 105.0)
        self.assertEqual(still_open, 1)

    def test_paper_only_source(self):
        for bad in ("place_order", "/orders", "broker.buy", "broker.sell", ".buy(", ".sell("):
            self.assertNotIn(bad, SRC)
        self.assertEqual(re.findall(r"https://[\w.\-/]+", SRC), ["https://api.telegram.org"])


if __name__ == "__main__":
    unittest.main()
