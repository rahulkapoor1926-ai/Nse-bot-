"""Signal study: real-bar replays of the live ORB / Sweep logic with no option fills and no P&L."""

import datetime as dt
import os
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


class IndexOutcomes(unittest.TestCase):
    """INDEX-BASED outcome of ORB signals on real-shaped 1m bars (no option prices)."""

    def sig(self, typ="CE"):
        d = DAYS[1]
        return {"family": "ORB", "day": d.isoformat(), "signal_time": clock.at(d, "09:45").isoformat(),
                "option_type": typ, "entry_window": [clock.at(d, "09:50").isoformat(), clock.at(d, "09:55").isoformat()],
                "invalidate_below": 25000.0 if typ == "CE" else None,
                "invalidate_above": 25000.0 if typ == "PE" else None}

    def bars(self, path, full=True):
        """1m bars from 09:50; with full=True the session continues flat at the last close until 15:14."""
        d, out = DAYS[1], []
        for i, (o, h, l, c) in enumerate(path):
            out.append((clock.at(d, "09:50") + dt.timedelta(minutes=i), o, h, l, c, 0))
        while full and out[-1][0] < clock.at(d, "15:14"):
            c = out[-1][4]
            out.append((out[-1][0] + dt.timedelta(minutes=1), c, c, c, c, 0))
        return out

    def run_(self, path, typ="CE", full=True):
        return SS.index_outcomes(self.bars(path, full), [self.sig(typ)], ORB, synth.cfg("/tmp/x"), CAL)[0]

    def test_target_sl_ambiguous_time_exit(self):
        r = self.run_([(25040, 25041, 25039, 25040), (25040, 25141, 25039, 25140)])   # risk 40 -> target 25140
        self.assertEqual((r["outcome"], r["entry"], r["target"], r["r_multiple"]), ("TARGET", 25040, 25140.0, 2.5))
        r = self.run_([(25040, 25041, 24999, 25000)])
        self.assertEqual((r["outcome"], r["index_points"], r["r_multiple"]), ("SL", -40.0, -1.0))
        r = self.run_([(25040, 25041, 25039, 25040), (24990, 24995, 24980, 24985)])     # opens below the stop
        self.assertEqual((r["outcome"], r["exit"], r["r_multiple"]), ("SL", 24990, -1.25))
        r = self.run_([(25040, 25050, 25030, 25040)] * 3)
        self.assertEqual(r["time_exit_result"], "breakeven")
        r = self.run_([(25040, 25141, 24999, 25000)])
        self.assertEqual((r["outcome"], r["exit"]), ("AMBIGUOUS", None))
        r = self.run_([(25040, 25050, 25030, 25045)] * 3)
        self.assertEqual((r["outcome"], r["exit"]), ("TIME EXIT", 25045))
        r = self.run_([(25060, 25061, 25058, 25059), (25059, 25060, 24999, 25000)], typ="PE")
        self.assertEqual(r["outcome"], "INVALID")                              # PE entry already above its stop

    def test_bars_after_the_time_exit_are_ignored(self):
        flat = clock.at(DAYS[1], "15:15")
        b = self.bars([(25040, 25041, 25039, 25040)])
        self.assertEqual(b[-1][0], clock.at(DAYS[1], "15:14"))
        b.append((flat, 25040, 25200, 25039, 25200, 0))                       # the 15:15 bar is after flat_by
        r = SS.index_outcomes(b, [self.sig()], ORB, synth.cfg("/tmp/x"), CAL)[0]
        self.assertEqual(r["outcome"], "TIME EXIT")

    def test_session_still_running_is_incomplete_not_a_time_exit(self):
        # data that stops at 11:40 (study run during the session) must not be marked to the last bar
        r = self.run_([(25040, 25050, 25030, 25045)] * 3, full=False)
        self.assertEqual((r["outcome"], r.get("r_multiple")), ("INCOMPLETE", None))
        self.assertIn("before the 15:15 time exit", r["reason"])
        # a stop or target already hit before the data ends is a known outcome
        r = self.run_([(25040, 25041, 24999, 25000)], full=False)
        self.assertEqual(r["outcome"], "SL")
        m = SS.index_model([dict(r, entry_time="2026-01-02T09:50"), {"day": "2026-01-03", "outcome": "INCOMPLETE"}],
                           synth.cfg("/tmp/x"))
        self.assertEqual((m["trades_counted"], m["excluded"]["INCOMPLETE"]), (1, 1))

    def test_summary_counts_and_label(self):
        rows = [{"day": "2026-10-29", "outcome": "TARGET", "index_points": 100.0},
                {"day": "2026-10-29", "outcome": "SL", "index_points": -40.0},
                {"day": "2026-11-02", "outcome": "AMBIGUOUS"}]
        sm = SS.summarise_outcomes(rows)
        self.assertEqual((sm["totals"]["TARGET"], sm["totals"]["SL"], sm["totals"]["AMBIGUOUS"]), (1, 1, 1))
        self.assertEqual((sm["target_hit_pct_of_target_or_sl"], sm["index_points_total"]), (50.0, 60.0))
        self.assertIn("INDEX-BASED, NOT OPTION P&L", sm["note"])


class IndexModel(unittest.TestCase):
    def test_r_metrics_drawdown_streak_and_labelled_rupees(self):
        rows = [{"day": "2026-01-02", "outcome": "SL", "r_multiple": -1.0},
                {"day": "2026-01-05", "outcome": "SL", "r_multiple": -1.0},
                {"day": "2026-01-06", "outcome": "TARGET", "r_multiple": 2.5},
                {"day": "2026-02-02", "outcome": "TIME EXIT", "r_multiple": 0.4},
                {"day": "2026-02-03", "outcome": "AMBIGUOUS"}]
        m = SS.index_model(rows, synth.cfg("/tmp/x", capital_inr=20000, max_risk_per_trade_inr=500))
        self.assertEqual((m["trades_counted"], m["wins"], m["losses"], m["excluded"]["AMBIGUOUS"]), (4, 2, 2, 1))
        self.assertEqual((m["gross_gain_r"], m["gross_loss_r"], m["net_r"], m["profit_factor"]), (2.9, 2.0, 0.9, 1.45))
        self.assertEqual((m["max_drawdown_r"], m["max_consecutive_losses"], m["win_rate_pct"]), (2.0, 2, 50.0))
        h = m["hypothetical_inr"]
        self.assertEqual((h["start"], h["net"], h["end"], h["max_drawdown"]), (20000.0, 450.0, 20450.0, 1000.0))
        self.assertEqual((h["gross_gain"], h["gross_loss"], h["max_drawdown_pct_of_peak"]), (1450.0, 1000.0, 5.0))
        self.assertEqual((m["max_drawdown_peak_day"], m["max_drawdown_trough_day"]), (None, "2026-01-05"))
        self.assertIn("NOT OPTION P&L", m["label"])
        self.assertEqual(m["by_month"]["2026-01"]["net_r"], 0.5)

    def test_daily_limits_with_one_signal_a_day(self):
        rows = [{"day": "2026-01-02", "r_multiple": -1.0}, {"day": "2026-01-05", "r_multiple": 2.5}]
        c = synth.cfg("/tmp/x", max_risk_per_trade_inr=500, max_daily_loss_inr=1000, max_trades_per_day=5,
                      max_stop_exits_per_day=2, daily_profit_stop_inr=2000)
        d = SS.daily_limit_check(rows, c)
        self.assertEqual((d["blocked_by_daily_limits"], d["worst_single_day_inr_index_model"]), (0, -500.0))

    def test_risk_fit_uses_the_live_sizing(self):
        from nse_bot import budget
        from nse_bot.costs import CostModel
        from nse_bot.instruments import Contract
        cfg = synth.cfg("/tmp/x")
        costs = CostModel.load(os.path.join(synth.ROOT, cfg["costs_file"]))
        c = Contract(1, "NSE:NIFTY26N0325000CE", "NIFTY", dt.date(2026, 11, 3), 25000.0, "CE", 65, 0.05, 1800)
        r = SS.per_trade_risk_fit(cfg, costs, c, dt.date(2026, 11, 2), 20)
        p = r["max_premium_for_one_lot"]
        ok = budget.size_at_fills(cfg, costs, c, p - 0.5, round((p - 0.5) * 0.8, 2), dt.date(2026, 11, 2), 1000)["lots"]
        no = budget.size_at_fills(cfg, costs, c, p + 1, round((p + 1) * 0.8, 2), dt.date(2026, 11, 2), 1000)["lots"]
        self.assertEqual((ok >= 1, no), (True, 0))


class IndependentCheck(unittest.TestCase):
    """Every metric recomputed here from the raw R list with separate, plain code."""

    def rows(self, n=400, seed=11):
        import random
        rnd = random.Random(seed)
        out, d = [], dt.date(2025, 11, 3)
        for i in range(n):
            kind = rnd.choice(["TARGET", "SL", "SL", "TIME EXIT", "TIME EXIT"])
            r = 2.5 if kind == "TARGET" else -1.0 if kind == "SL" else round(rnd.uniform(-0.9, 2.4), 4)
            if kind == "TIME EXIT" and rnd.random() < 0.05:
                r = 0.0
            row = {"day": (d + dt.timedelta(days=i)).isoformat(), "outcome": kind, "r_multiple": r,
                   "entry_time": (d + dt.timedelta(days=i)).isoformat() + "T10:00:00+05:30"}
            if kind == "TIME EXIT":
                row["time_exit_result"] = "profit" if r > 0 else "loss" if r < 0 else "breakeven"
            out.append(row)
        return out

    def test_all_metrics(self):
        rows = self.rows()
        cfg = synth.cfg("/tmp/x", capital_inr=20000, max_risk_per_trade_inr=500)
        m = SS.index_model(rows, cfg)
        R = [r["r_multiple"] for r in rows]
        w = [x for x in R if x > 0]
        lo = [x for x in R if x < 0]
        self.assertEqual((m["wins"], m["losses"], m["breakeven"]), (len(w), len(lo), R.count(0.0)))
        self.assertEqual(m["wins"] + m["losses"] + m["breakeven"], len(R))
        self.assertAlmostEqual(m["win_rate_pct"], round(100.0 * len(w) / len(R), 1))
        self.assertAlmostEqual(m["gross_gain_r"], round(sum(w), 2))
        self.assertAlmostEqual(m["gross_loss_r"], round(-sum(lo), 2))
        self.assertAlmostEqual(m["net_r"], round(sum(R), 2))
        self.assertAlmostEqual(m["profit_factor"], round(sum(w) / -sum(lo), 3))
        self.assertAlmostEqual(m["expectancy_r"], round(sum(R) / len(R), 4))
        b = m["breakdown"]
        self.assertEqual(sum(v["trades"] for v in b.values()), len(R))
        self.assertAlmostEqual(sum(v["net_r"] for v in b.values()), sum(R), places=1)
        # drawdown: brute force over every (peak, later trough) pair of the cumulative curve, starting at 0
        cum = [0.0]
        for x in R:
            cum.append(cum[-1] + x)
        dd = max(cum[i] - cum[j] for i in range(len(cum)) for j in range(i, len(cum)))
        self.assertAlmostEqual(m["max_drawdown_r"], round(dd, 2))
        self.assertAlmostEqual(m["hypothetical_inr"]["end"], round(20000 + sum(R) * 500, 2))
        streak = best = 0
        for x in R:
            streak = streak + 1 if x < 0 else 0
            best = max(best, streak)
        self.assertEqual(m["max_consecutive_losses"], best)
        self.assertAlmostEqual(sum(v["net_r"] for v in m["by_month"].values()), sum(R), places=1)


class Portfolio(unittest.TestCase):
    def cfg(self):
        return synth.cfg("/tmp/x", capital_inr=20000, max_risk_per_trade_inr=500, max_trades_per_day=5,
                         max_stop_exits_per_day=2, max_daily_loss_inr=1000, daily_profit_stop_inr=2000, max_open_lots=1)

    def row(self, day, hh, outcome, r, exit_hh):
        return {"day": day, "signal_time": day + "T%s:00+05:30" % hh, "outcome": outcome, "r_multiple": r,
                "entry_time": day + "T%s:00+05:30" % hh, "exit_time": day + "T%s:00+05:30" % exit_hh}

    def test_limits_applied_in_order_and_rejections_not_counted(self):
        d = "2026-01-02"
        rows = [self.row(d, "09:40", "SL", -1.0, "09:50"),
                self.row(d, "09:45", "TARGET", 2.5, "10:30"),     # overlaps the open trade: one lot
                self.row(d, "10:00", "SL", -1.0, "10:10"),        # 2nd stop -> day loss Rs 1000
                self.row(d, "10:20", "TARGET", 2.5, "11:00"),     # stop limit / loss limit reached
                self.row("2026-01-05", "09:40", "AMBIGUOUS", None, "09:41")]
        p = SS.portfolio(rows, self.cfg())
        self.assertEqual((p["signals"], p["filled"], p["rejected"]), (5, 2, 3))
        self.assertEqual(p["model"]["trades_counted"], 2)
        self.assertEqual(p["model"]["net_r"], -2.0)
        self.assertEqual(p["rejected_by_reason"]["one lot already open"], 1)
        self.assertEqual(p["rejected_by_reason"]["max 2 stop exits a day"], 1)

    def test_profit_stop_and_entry_cap(self):
        d = "2026-01-02"
        rows = [self.row(d, "09:%02d" % (30 + 2 * i), "TARGET", 2.5, "09:%02d" % (31 + 2 * i)) for i in range(3)]
        p = SS.portfolio(rows, self.cfg())
        self.assertEqual(p["filled"], 2)                    # +Rs 2,500 >= Rs 2,000 profit stop after two
        self.assertIn("daily profit stop", list(p["rejected_by_reason"])[0])
        rows = [self.row(d, "09:%02d" % (30 + 2 * i), "TIME EXIT", 0.1, "09:%02d" % (31 + 2 * i)) for i in range(7)]
        self.assertEqual(SS.portfolio(rows, self.cfg())["filled"], 5)

    def test_csv_marks_rejections_and_label(self):
        d = "2026-01-02"
        rows = [self.row(d, "09:40", "SL", -1.0, "09:50"), self.row(d, "09:45", "TARGET", 2.5, "10:30")]
        p = SS.portfolio(rows, self.cfg())
        text = SS.trades_csv(rows, p, 500)
        self.assertTrue(text.startswith("# INDEX-BASED MODEL, NOT OPTION P&L"))
        self.assertIn("rejected: one lot already open", text)
        self.assertIn(",-500.0", text)


class Contracts(unittest.TestCase):
    def test_expiry_and_fyers_symbols(self):
        self.assertEqual(SS.nifty_expiry(dt.date(2026, 10, 29), CAL), dt.date(2026, 11, 3))
        self.assertEqual(SS.nifty_expiry(dt.date(2026, 11, 3), CAL), dt.date(2026, 11, 3))   # expiry day itself
        w = SS.option_symbol(dt.date(2026, 10, 29), "CE", 25000, CAL)
        self.assertEqual((w["symbol"], w["expiry_kind"]), ("NSE:NIFTY26N0325000CE", "weekly"))
        m = SS.option_symbol(dt.date(2026, 10, 22), "PE", 25000, CAL)        # last Tuesday of October
        self.assertEqual((m["symbol"], m["expiry"]), ("NSE:NIFTY26OCT25000PE", "2026-10-27"))

    def test_holiday_tuesday_moves_to_the_previous_trading_day(self):
        cal = synth.calendar()
        hol = dt.date(2026, 11, 24)                                       # Tuesday, Prakash Gurpurb (NSE list)
        if cal.is_trading_day(hol):
            self.skipTest("synthetic calendar has no 24 Nov holiday")
        self.assertEqual(SS.nifty_expiry(dt.date(2026, 11, 20), cal), dt.date(2026, 11, 23))


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
        self.assertEqual(r["furthest_rule_failed"], {"sweep": 1})
        audit = {x["rule"]: x for x in r["rule_audit"]}
        self.assertEqual(len(r["day_audit"]), 1)
        self.assertIn("data_future", audit)


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


class DayAudit(unittest.TestCase):
    """day-audit: today's real bars through the unchanged ORB rule, the option premium in the entry window
    and the Rs 500 one-lot check; a flat day is proven NO VALID SETUP."""

    def run_audit(self, day_fn, opt_open):
        import json
        import os
        import shutil
        import tempfile
        from nse_bot import cli
        from nse_bot.instruments import Contract
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        day = dt.date(2026, 10, 29)
        days, d = [], day
        while len(days) < 8:
            if CAL.is_trading_day(d):
                days.insert(0, d)
            d -= dt.timedelta(days=1)
        one = {x: (day_fn(x) if x == day else flat(x)) for x in days}
        c = Contract(9, "NSE:NIFTY26N0325050CE", "NIFTY", dt.date(2026, 11, 3), 25050.0, "CE", 65, 0.05, 1800)

        class MD(object):
            symbols, calls_made = {1: "NSE:NIFTY50-INDEX", 9: c.symbol}, 4

            def candles(self, iid, size, a, b):
                if size == "1d":
                    return [(clock.at(x, "00:00"), 1, 2, 0, 1, 0) for x in days]
                if iid == 9:
                    return [(clock.at(day, "09:15") + dt.timedelta(minutes=i), opt_open, opt_open + 1,
                             opt_open - 1, opt_open, 10) for i in range(375)]
                return [bar for x in days if a <= clock.at(x, "09:15") <= b for bar in one[x]]

        class M(object):
            def index_id(self, name):
                return 1

            def current_future(self, u, day_):
                return None

            def expiries(self, u, today):
                return [dt.date(2026, 11, 3)]

            def pick(self, u, ex, typ, spot, off):
                return c

        cfgp = os.path.join(synth.ROOT, "config", "paper.example.json")
        ctx = lambda a, cfg, cal, cm: (dict(cfg, data_source="fyers"), cal, cm, M(), MD())   # noqa: E731
        with mock.patch.object(cli, "_fyers_ctx", ctx), \
                mock.patch.object(cli.clock, "now_ist", lambda: clock.at(day, "16:00")), \
                mock.patch.object(cli.config, "load", lambda p: dict(synth.cfg(out), data_source="fyers")):
            rc = cli.main(["--config", cfgp, "day-audit", "--date", day.isoformat(), "--out", out])
        self.assertEqual(rc, 0)
        with open(os.path.join(out, day.isoformat() + ".json")) as f:
            return json.load(f)

    def test_setup_with_premium_and_lot_check(self):
        doc = self.run_audit(breakout, 30.0)
        o = doc["ORB"]
        self.assertEqual(len(o["signals"]), 1)
        sg = o["signals"][0]
        self.assertEqual(sg["contract"]["symbol"], "NSE:NIFTY26N0325050CE")
        p = sg["entry_window_premium"]
        self.assertEqual((p["bar"], p["open_ltp"], p["stop_20pct"]), (sg["entry_window"][0], 30.0, 24.0))
        self.assertEqual(p["lots_under_rs500_rule"], 1)
        self.assertGreater(o["opening_range"]["high"], o["opening_range"]["low"])
        self.assertEqual(doc["data"]["index_1m_bars_today"], 375)

    def test_expensive_premium_cannot_fit_one_lot(self):
        p = self.run_audit(breakout, 120.0)["ORB"]["signals"][0]["entry_window_premium"]
        self.assertEqual(p["lots_under_rs500_rule"], 0)                    # 65 x 24 = Rs 1,560 > Rs 500

    def test_flat_day_is_no_valid_setup(self):
        o = self.run_audit(flat, 30.0)["ORB"]
        self.assertEqual((o["signals"], o["verdict"]), ([], "NO VALID SETUP"))
