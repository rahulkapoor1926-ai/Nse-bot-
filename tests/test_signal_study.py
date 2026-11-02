"""Signal study: real-bar replays of the live ORB / Sweep logic with no option fills and no P&L."""

import datetime as dt
import unittest
from unittest import mock

import synth
from nse_bot import clock, sweep
from nse_bot.backtest import signal_study as SS

CAL = synth.calendar()
DAYS = [dt.date(2026, 10, 28), dt.date(2026, 10, 29)]
ORB = {"name": "orb15-atm", "instrument": "index_option", "pattern": "orb",
       "params": {"or_minutes": 15, "buffer_pct": 0.05}, "interval_minutes": 5, "strike_offset": 0,
       "stop_pct": 20, "target_r": 2.5, "entry_delay_bars": 1, "max_signals_per_day": 1}


def session(day, closes):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append((clock.at(day, "09:15") + dt.timedelta(minutes=i), prev, max(prev, c) + 1, min(prev, c) - 1, c, 100))
        prev = c
    return out


def flat(day, level=25000.0):
    return session(day, [level + (i % 5) for i in range(375)])


def breakout(day):
    closes = [25000 + (i % 30) for i in range(30)]           # 09:15-09:44 inside 24999..25030
    closes += [25031 + 5 * i for i in range(10)]              # 09:45 onward: up and away
    closes += [25080.0] * (375 - len(closes))
    return session(day, closes)


class Orb(unittest.TestCase):
    def test_breakout_day_gives_one_ce_signal_with_the_bots_strike(self):
        u1 = flat(DAYS[0]) + breakout(DAYS[1])
        r = SS.orb(u1, ORB, synth.cfg("/tmp/x"), CAL)
        self.assertEqual(r["sessions"], 2)
        self.assertEqual(len(r["signals"]), 1)
        s = r["signals"][0]
        self.assertEqual((s["day"], s["option_type"], s["family"]), ("2026-10-29", "CE", "ORB"))
        self.assertGreaterEqual(s["signal_time"], clock.at(DAYS[1], "09:30").isoformat())
        self.assertEqual(s["target_strike"], int(round(s["trigger_close"] / 50.0) * 50))
        self.assertIn("regime", s)
        self.assertNotIn("pnl", s)                               # never a P&L
        self.assertNotIn("premium", s)

    def test_fyers_own_5m_bars_give_the_same_signal_as_resampled_1m(self):
        from nse_bot import structure as st
        u1 = flat(DAYS[0]) + breakout(DAYS[1])
        a = SS.orb(u1, ORB, synth.cfg("/tmp/x"), CAL)["signals"]
        b = SS.orb(st.resample(u1, 5), ORB, synth.cfg("/tmp/x"), CAL, native_minutes=5)["signals"]
        self.assertEqual([(x["day"], x["signal_time"], x["option_type"]) for x in a],
                         [(x["day"], x["signal_time"], x["option_type"]) for x in b])
        self.assertEqual((a[0]["data"], b[0]["data"]), ("1m", "5m"))
        with self.assertRaises(ValueError):
            SS.orb(u1, ORB, synth.cfg("/tmp/x"), CAL, native_minutes=15)

    def test_options_pnl_is_marked_not_available(self):
        self.assertEqual((SS.OPTIONS_PNL["status"], SS.OPTIONS_PNL["simulated_option_trades"]), ("NOT AVAILABLE", 0))

    def test_charts_are_the_real_5m_bars_of_signal_days_only(self):
        u1 = flat(DAYS[0]) + breakout(DAYS[1])
        sig = SS.orb(u1, ORB, synth.cfg("/tmp/x"), CAL)["signals"]
        ch = SS.charts(u1, sig)
        self.assertEqual(list(ch), ["2026-10-29"])
        self.assertEqual(len(ch["2026-10-29"]), 75)
        self.assertEqual(ch["2026-10-29"][0][1], u1[375][1])     # first 5m open = first 1m open of the day

    def test_summary_by_month(self):
        sm = SS.summarise([{"day": "2026-10-29", "option_type": "CE", "regime": "x"},
                           {"day": "2026-11-02", "option_type": "PE", "regime": "x"}], 40)
        self.assertEqual((sm["signals"], sm["CE"], sm["PE"], sm["signal_days_pct"]), (2, 1, 1, 5.0))
        self.assertEqual(list(sm["by_month"]), ["2026-10", "2026-11"])


class ObservedCalendar(unittest.TestCase):
    def test_past_year_from_real_sessions_file_years_untouched(self):
        file_years = {"2026": {"holidays": {"2026-01-26": "Republic Day"}}}
        weekdays = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(365)]
        weekdays = [d for d in weekdays if d.weekday() < 5]
        sessions = [d for d in weekdays if d not in (dt.date(2025, 2, 26), dt.date(2025, 10, 21))]
        sessions += [dt.date(2026, 1, 2)]
        cal, added = SS.observed_calendar(file_years, sessions, {dt.date(2025, 10, 22): 60, dt.date(2025, 3, 3): 375},
                                          dt.date(2026, 11, 2))
        self.assertEqual(sorted(added), ["2025"])                   # 2026 stays the NSE-circular list
        self.assertFalse(cal.is_trading_day(dt.date(2025, 2, 26)))   # no session that day
        self.assertFalse(cal.is_trading_day(dt.date(2025, 10, 22)))  # short special session
        self.assertTrue(cal.is_trading_day(dt.date(2025, 3, 3)))
        self.assertFalse(cal.is_trading_day(dt.date(2026, 1, 26)))
        self.assertEqual(len(added["2025"]["holidays"]), 3)

    def test_partial_year_is_labelled(self):
        sessions = [dt.date(2024, 11, 4), dt.date(2024, 11, 5)]
        cal, added = SS.observed_calendar({}, sessions, {}, dt.date(2024, 11, 5))
        self.assertIn("from 2024-11-04", added["2024"]["source"])
        self.assertTrue(cal.is_trading_day(dt.date(2024, 11, 1)))


class Sweep(unittest.TestCase):
    """The study calls the unchanged sweep.evaluate; here it is observed to check wiring and look-ahead."""

    def setUp(self):
        self.days = []
        d = dt.date(2026, 10, 29)
        for _ in range(8):
            self.days.insert(0, d)
            d = CAL.previous_trading_day(d)
        self.u1 = [b for d in self.days for b in flat(d)]
        self.f1 = [b[:5] + (500,) for b in self.u1]
        self.seen = []

    def fake(self, setup_at="10:22", bias="bearish", fut_bad_day=None):
        def evaluate(strategy, u, now, u1, u1h, u1d, f1, get_chain, master, cfg):
            self.seen.append((now, max(b[0] for b in u1), max(b[0] for b in f1)))
            self.assertIsNone(get_chain)                           # never an option chain: no fills possible
            if fut_bad_day and now.date() == fut_bad_day:
                return {"decision": "SKIP", "skip_reason": "data_future: future bars without volume"}
            if now >= clock.at(now.date(), setup_at):
                return {"decision": "SKIP", "skip_reason": "contract: no option chain source", "bias": bias,
                        "evaluated_at": now.isoformat(), "underlying_plan": {"spot": 25012.0}}
            return {"decision": "SKIP", "skip_reason": "sweep: no liquidity sweep with a reclaim today"}
        return evaluate

    def test_one_signal_per_day_direction_and_no_look_ahead(self):
        with mock.patch.object(sweep, "evaluate", self.fake()):
            r = SS.sweep_reversal(self.u1, self.f1, {"name": "sweep-reversal", "params": {"rvol_sessions": 10}},
                                  synth.cfg("/tmp/x"), CAL)
        self.assertEqual([s["day"] for s in r["signals"]], ["2026-10-28", "2026-10-29"])   # after 6 warm-up sessions
        s = r["signals"][-1]
        self.assertEqual((s["option_type"], s["target_strike"], s["family"]), ("PE", 25000, "Sweep"))
        self.assertEqual(s["signal_time"], clock.at(dt.date(2026, 10, 29), "10:22").isoformat())
        for now, last_u, last_f in self.seen:
            self.assertLessEqual(last_u + dt.timedelta(minutes=1), now)
            self.assertLessEqual(last_f + dt.timedelta(minutes=1), now)

    def test_bad_future_data_is_not_evaluable_not_no_signal(self):
        bad = dt.date(2026, 10, 29)
        with mock.patch.object(sweep, "evaluate", self.fake(setup_at="23:00", fut_bad_day=bad)):
            r = SS.sweep_reversal(self.u1, self.f1, {"name": "s", "params": {"rvol_sessions": 10}},
                                  synth.cfg("/tmp/x"), CAL)
        self.assertEqual(r["signals"], [])
        self.assertEqual(r["not_evaluable"].get("data_future: future bars without volume"), 1)
        self.assertEqual(r["funnel_last_failed_rule"], {"sweep": 1})


if __name__ == "__main__":
    unittest.main()


class Cli(unittest.TestCase):
    """signal-study end to end with a fake FYERS: 1m refused before a date, 5m served older, no future."""

    def test_cli_writes_coverage_audit_and_not_available_pnl(self):
        import json
        import os
        import shutil
        import tempfile
        from nse_bot import cli
        from nse_bot import structure as st
        from nse_bot.errors import DataError
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        end = clock.at(dt.date(2026, 10, 30), "16:00")
        cut = dt.date(2026, 10, 27)
        days = []
        d = dt.date(2026, 10, 30)
        while len(days) < 12:
            if CAL.is_trading_day(d):
                days.insert(0, d)
            d -= dt.timedelta(days=1)
        one = {x: (breakout(x) if x.day % 2 else flat(x)) for x in days}

        class MD(object):
            symbols, calls_made = {1: "NSE:NIFTY50-INDEX"}, 7

            def candles(self, iid, size, a, b):
                if size == "1d":
                    return [(clock.at(x, "00:00"), 1, 2, 0, 1, 0) for x in days]
                if size == "1m":
                    if a.date() < cut:
                        raise DataError("FYERS /history failed: HTTP 422 Invalid input")
                    return [bar for x in days if a <= clock.at(x, "09:15") <= b for bar in one[x]]
                return [bar for x in days if a <= clock.at(x, "09:15") < b for bar in st.resample(one[x], 5)]

        class M(object):
            def index_id(self, name):
                return 1

            def current_future(self, u, day):
                return None

        cfgp = os.path.join(synth.ROOT, "config", "paper.example.json")
        ctx = lambda a, cfg, cal, cm: (dict(cfg, data_source="fyers"), cal, cm, M(), MD())   # noqa: E731
        with mock.patch.object(cli, "_fyers_ctx", ctx), mock.patch.object(cli.clock, "now_ist", lambda: end), \
                mock.patch.object(cli.config, "load", lambda p: dict(synth.cfg(out), data_source="fyers")):
            rc = cli.main(["--config", cfgp, "signal-study", "--out", out, "--days", "6", "--older-days", "20"])
        self.assertEqual(rc, 0)
        with open(os.path.join(out, "study.json")) as f:
            doc = json.load(f)
        self.assertEqual(doc["options_pnl"]["status"], "NOT AVAILABLE")
        self.assertTrue(any(r["result"] != "ok" for r in doc["fetch_audit"]))       # refusal recorded
        self.assertGreater(doc["coverage"]["index_5m_older"]["sessions"], 0)
        self.assertEqual(doc["coverage"]["future"]["bars"], 0)
        self.assertIn("no current NIFTY future", doc["coverage"]["future"]["note"])
        self.assertEqual({s["data"] for s in doc["ORB"]["signals"]}, {"1m", "5m"})
        for s in doc["ORB"]["signals"]:
            self.assertIn(s["day"], doc["charts"])
