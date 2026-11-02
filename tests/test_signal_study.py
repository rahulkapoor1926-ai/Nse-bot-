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
