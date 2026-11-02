"""FYERS options recorder: one batched request a minute, ATM + 2 ITM CE/PE, honest flags, no duplicates."""

import csv
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import synth
from helpers import t
from nse_bot import cli, clock, instruments
from nse_bot.broker.fyers import FyersError
from nse_bot.data import fyers_recorder as FR
from test_bot import INDEX_ROW
from test_instruments import HEADER, chain_rows

DAY = dt.date(2026, 10, 29)


def master():
    return instruments.parse(HEADER + "\n" + INDEX_ROW + "\n" + chain_rows(), index_names=("Nifty 50",))


class FakeMD(object):
    """Stands in for FyersReadOnly: same option_chain / symbols / calls_made / login surface."""

    def __init__(self, m, spot=25030.0):
        self.m = m
        self.symbols = {c.instrument_id: "NSE:" + c.symbol for c in m.contracts}
        self.idx = m.index_id("Nifty 50")
        self.symbols[self.idx] = "NSE:NIFTY50-INDEX"
        self.spot, self.calls_made, self.requests, self.logins = spot, 0, [], 0
        self.fail, self.drop, self.override, self.tick = None, set(), {}, 0
        self._per_day = 450

    def option_chain(self, uid, expiry, enrich_depth=True):
        self.calls_made += 1
        self.requests.append((uid, expiry, enrich_depth))
        if self.fail:
            e, self.fail = self.fail, None
            raise e
        self.tick += 1                                  # trades every minute unless overridden
        legs = []
        for c in self.m.contracts:
            if c.expiry != expiry or c.instrument_id in self.drop:
                continue
            q = dict(ltp=50.0, bid=49.9, ask=50.1, volume=120000 + 650 * self.tick, oi=900000)
            q.update(self.override.get(c.instrument_id, {}))
            legs.append(dict(q, instrument_id=c.instrument_id, option_type=c.option_type, strike=c.strike,
                             symbol=self.symbols[c.instrument_id], bid_qty=None, ask_qty=None, ts=None))
        return {"format": "legs", "spot": self.spot + 0.05 * self.tick, "legs": legs}

    def login(self):
        self.logins += 1


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.m = master()
        self.cfg = synth.cfg(os.path.join(self.dir, "state"))
        self.sleeps = []
        self.md = FakeMD(self.m)
        self.rec = self.make()

    def make(self):
        return FR.Recorder(self.md, self.m, synth.calendar(), self.cfg, os.path.join(self.dir, "rec"),
                           now=lambda: t(10, 0, 3), sleep=self.sleeps.append, log=lambda x: None)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def rows(self, day=DAY):
        p = os.path.join(self.dir, "rec", day.isoformat(), FR.FILE)
        with open(p, newline="") as f:
            return list(csv.DictReader(f))


class Minute(Base):
    def test_atm_and_two_itm_each_side_from_one_chain_request(self):
        n = self.rec.record_minute(t(10, 0))
        self.assertEqual(n, 7)
        self.assertEqual(self.md.calls_made, 1)                 # one /options-chain-v3 request
        self.assertEqual(self.md.requests[0][2], False)         # no per-leg /depth calls
        rows = self.rows()
        opt = {(r["option_type"], r["moneyness"]): r for r in rows if r["kind"] == "option"}
        # spot 25030 -> ATM 25050; CE ITM are lower strikes, PE ITM are higher strikes
        self.assertEqual([opt[("CE", m)]["strike"] for m in ("ATM", "ITM1", "ITM2")], ["25050.0", "25000.0", "24950.0"])
        self.assertEqual([opt[("PE", m)]["strike"] for m in ("ATM", "ITM1", "ITM2")], ["25050.0", "25100.0", "25150.0"])
        r = opt[("CE", "ATM")]
        for k in ("ts", "symbol", "expiry", "bid", "ask", "ltp", "volume", "oi"):
            self.assertTrue(r[k], k)
        self.assertEqual((r["status"], r["expiry"], r["bid"], r["ask"]), ("AGE_UNKNOWN", "2026-11-03", "49.9", "50.1"))
        self.assertEqual((r["bid_qty"], r["ask_qty"]), ("", ""))   # the chain has no quantities: empty, not 0
        self.assertEqual(rows[0]["kind"], "index")
        self.assertEqual(rows[0]["ltp"], "25030.05")
        self.rec.record_minute(t(10, 1))
        self.assertEqual(self.md.calls_made, 2)                 # exactly one request per minute
        now = [r for r in self.rows() if r["ts"] == t(10, 1).isoformat()]
        self.assertEqual({r["status"] for r in now}, {"OK"})    # volume moved: traded within the minute

    def test_missing_stale_and_empty_fields_are_flagged_never_zero(self):
        ids = {(c.strike, c.option_type): c.instrument_id for c in self.m.contracts}
        self.md.override[ids[(25000.0, "CE")]] = {"volume": 5000}      # never trades
        for m in range(0, 4):
            self.rec.now = lambda m=m: t(10, m, 3)
            if m == 3:
                self.md.drop.add(ids[(25050.0, "PE")])
                self.md.override[ids[(25050.0, "CE")]] = {"bid": None}
                self.md.override[ids[(24950.0, "CE")]] = {"ask": None}
            self.rec.record_minute(t(10, m))
        st = {(r["option_type"], r["strike"]): r for r in self.rows() if r["ts"] == t(10, 3).isoformat()
              and r["kind"] == "option"}
        self.assertEqual(st[("PE", "25050.0")]["status"], "MISSING")
        self.assertEqual(st[("PE", "25050.0")]["bid"], "")
        self.assertEqual(st[("PE", "25050.0")]["note"], "not returned by FYERS /options-chain-v3")
        self.assertEqual(st[("CE", "25050.0")]["status"], "NO_BID")
        self.assertEqual(st[("CE", "25050.0")]["bid"], "")
        self.assertEqual(st[("CE", "25000.0")]["status"], "STALE")          # no volume change for 180 s
        self.assertEqual(st[("CE", "25000.0")]["no_change_s"], "180.0")
        self.assertEqual(st[("CE", "24950.0")]["status"], "NO_ASK")
        self.assertEqual(st[("PE", "25100.0")]["status"], "OK")

    def test_outside_market_hours_is_stale(self):
        self.rec.now = lambda: t(19, 37, 3)
        self.rec.record_minute(t(19, 37))
        self.assertTrue(all(r["status"].startswith("STALE") and "outside market hours" in r["note"]
                            for r in self.rows()))

    def test_no_duplicates_in_a_minute_or_after_a_restart(self):
        self.assertEqual(self.rec.record_minute(t(10, 0)), 7)
        self.assertEqual(self.rec.record_minute(t(10, 0)), 0)
        again = self.make()                                     # a restarted process reads the file back
        self.assertEqual(again.record_minute(t(10, 0)), 0)
        self.assertEqual(again.record_minute(t(10, 1)), 7)
        rows = self.rows()
        self.assertEqual(len(rows), 14)
        self.assertEqual(len({(r["ts"], r["symbol"]) for r in rows}), 14)

    def test_an_older_column_layout_is_moved_aside_not_mixed(self):
        p = os.path.join(self.dir, "rec", DAY.isoformat(), FR.FILE)
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as f:
            f.write("ts,symbol,last_trade_time\n2026-10-29T09:20:00+05:30,X,\n")
        self.assertEqual(self.rec.record_minute(t(10, 0)), 7)
        self.assertEqual(len(self.rows()), 7)
        self.assertTrue(os.path.exists(p.replace(".csv", ".old1.csv")))

    def test_failed_request_marks_missing_backs_off_and_recovers(self):
        self.rec.record_minute(t(10, 0))
        self.md.fail = FyersError("FYERS /options-chain-v3 failed: HTTP 500, code None, boom")
        self.assertEqual(self.rec.record_minute(t(10, 1)), 7)
        miss = [r for r in self.rows() if r["ts"] == t(10, 1).isoformat()]
        self.assertTrue(all(r["status"] == "MISSING" and "request failed" in r["note"] for r in miss))
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(self.rec.record_minute(t(10, 2)), 7)    # reconnects on the next minute
        ok = [r for r in self.rows() if r["ts"] == t(10, 2).isoformat()]
        self.assertTrue(all(r["status"] == "OK" for r in ok))

    def test_auth_failure_logs_in_again(self):
        self.rec.record_minute(t(10, 0))
        self.md.fail = FyersError("FYERS /options-chain-v3 failed: HTTP 401, code -16, token expired")
        self.rec.record_minute(t(10, 1))
        self.assertEqual(self.md.logins, 1)

    def test_summary_counts(self):
        self.rec.record_minute(t(10, 0))
        doc = json.load(open(os.path.join(self.dir, "rec", DAY.isoformat(), "summary.json")))
        self.assertEqual((doc["rows_in_file"], doc["api_calls_this_process"]), (7, 1))
        self.assertEqual(doc["status_counts"], {"AGE_UNKNOWN": 7})
        self.assertEqual(len(doc["rows_per_symbol"]), 7)


class Loop(Base):
    def test_records_each_session_minute_until_the_stop_time(self):
        now = [t(9, 14, 50)]

        def sleep(s):
            now[0] = now[0] + dt.timedelta(seconds=s)
        rec = FR.Recorder(self.md, self.m, synth.calendar(), self.cfg, os.path.join(self.dir, "rec"),
                          now=lambda: now[0], sleep=sleep, log=lambda x: None)
        res = rec.run("09:20")
        ts = sorted({r["ts"] for r in self.rows()})
        self.assertEqual(ts, [t(9, m).isoformat() for m in range(16, 21)])     # 09:15 is the open, not a close
        self.assertEqual(res["api_calls"], 5)                   # one request a minute
        self.assertEqual(res["rows_written"], 35)

    def test_closed_market_records_nothing(self):
        now = [clock.at(dt.date(2026, 10, 31), "10:00")]       # a Saturday
        rec = FR.Recorder(self.md, self.m, synth.calendar(), self.cfg, os.path.join(self.dir, "rec"),
                          now=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + dt.timedelta(seconds=s)),
                          log=lambda x: None)
        self.assertEqual(rec.run("10:05")["rows_written"], 0)
        self.assertEqual(self.md.calls_made, 0)

    def test_budget_stops_the_recorder(self):
        now = [t(10, 0, 0)]
        self.md._per_day = 3
        rec = FR.Recorder(self.md, self.m, synth.calendar(), self.cfg, os.path.join(self.dir, "rec"),
                          now=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + dt.timedelta(seconds=s)),
                          log=lambda x: None)
        rec.run("15:31")
        self.assertEqual(self.md.calls_made, 3)

    def test_smoke_records_right_away(self):
        res = self.rec.run("15:31", smoke=1)
        self.assertEqual((res["snapshots"], res["rows_written"]), (1, 7))


class Safety(unittest.TestCase):
    def test_no_order_calls_in_the_recorder(self):
        src = open(FR.__file__).read().lower()
        for bad in ("place_order", "/orders", "buy(", "sell(", "live_orders_enabled\": true"):
            self.assertNotIn(bad, src)

    def test_command_refuses_a_non_fyers_config(self):
        d = tempfile.mkdtemp()
        try:
            cfg = synth.cfg(os.path.join(d, "state"))
            cfg["data_source"] = "sahi"
            p = os.path.join(d, "c.json")
            json.dump(cfg, open(p, "w"))
            self.assertEqual(cli.main(["--config", p, "fyers-record", "--smoke", "1", "--out", d]), 2)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
