import datetime as dt
import unittest

import synth
from helpers import t
from nse_bot import instruments, patterns, scanner
from nse_bot.errors import DataError
from test_instruments import HEADER, chain_rows

DAY = dt.date(2026, 10, 29)


def bars(closes, start=(9, 15)):
    out, s = [], t(*start, day=DAY)
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append((s + dt.timedelta(minutes=5 * i), o, max(o, c) + 1, min(o, c) - 1, c))
        prev = c
    return out


S = dict(synth.STRATEGY)


class Patterns(unittest.TestCase):
    def test_orb_breakout_up(self):
        b = bars([25000, 25005, 24995, 25003, 25020])
        sig = patterns.detect(S, b, 5)
        self.assertEqual((sig["option_type"], sig["bar_index"]), ("CE", 4))
        self.assertEqual(sig["known_at"], t(9, 40, day=DAY))
        self.assertEqual(sig["levels"]["or_high"], 25006)

    def test_orb_breakdown(self):
        sig = patterns.detect(S, bars([25000, 25005, 24995, 25003, 24980]), 5)
        self.assertEqual(sig["option_type"], "PE")
        self.assertIsNotNone(sig["invalidate_above"])

    def test_no_look_ahead(self):
        b = bars([25000, 25005, 24995, 25003, 25004, 25050])
        now = t(9, 44, day=DAY)                       # the 09:40 bar has not closed
        closed = patterns.closed_bars(b, now, 5)
        self.assertEqual(len(closed), 5)
        self.assertIsNone(patterns.detect(S, closed, 5))
        self.assertIsNotNone(patterns.detect(S, patterns.closed_bars(b, t(9, 45, day=DAY), 5), 5))

    def test_pdh_pdl(self):
        s = {"name": "p", "pattern": "pdh_pdl", "params": {"skip_first_minutes": 5}, "interval_minutes": 5,
             "strike_offset": 0, "stop_pct": 20, "target_r": 2}
        sig = patterns.detect(s, bars([25000, 25100, 25200]), 5, prev_high=25150, prev_low=24800)
        self.assertEqual((sig["option_type"], sig["bar_index"]), ("CE", 2))
        self.assertIsNone(patterns.detect(s, bars([25000, 25100]), 5, None, None))

    def test_raw_vs_first(self):
        b = bars([25000, 25005, 24995, 25020, 25030, 25040])
        self.assertEqual(patterns.raw_occurrences(S, b, 5), 3)

    def test_bad_bars(self):
        b = bars([25000, 25005])
        with self.assertRaises(DataError):
            patterns.validate_bars([b[1], b[0]], 5)
        with self.assertRaises(DataError):
            patterns.validate_bars([(b[0][0], 1, 0.5, 2, 1)], 5)
        with self.assertRaises(DataError):
            patterns.validate_bars([(b[0][0], None, 1, 1, 1)], 5)

    def test_strategy_validation(self):
        for bad in ({"pattern": "magic"}, {"stop_pct": 0}, {"target_r": 0}, {"interval_minutes": 7},
                    {"params": {"or_minutes": 3}}):
            s = dict(S, **bad)
            with self.assertRaises(DataError):
                patterns.validate_strategy(s)
        with self.assertRaises(DataError):
            patterns.validate_strategy({"name": "x"})


class Scanner(unittest.TestCase):
    def setUp(self):
        self.m = instruments.parse(HEADER + "\n" + chain_rows())
        self.cal = synth.calendar()
        self.cfg = synth.cfg("/tmp/unused")

    def test_signal_maps_to_listed_contract(self):
        b = bars([25000, 25005, 24995, 25003, 25030])
        rows = scanner.scan({"NIFTY": b}, {}, self.m, self.cal, t(9, 41, day=DAY), self.cfg, S)
        self.assertEqual(rows[0]["status"], "SIGNAL")
        self.assertEqual((rows[0]["contract"].strike, rows[0]["contract"].option_type), (25050.0, "CE"))
        self.assertEqual(rows[0]["contract"].expiry, dt.date(2026, 11, 3))

    def test_no_data_no_signal_error(self):
        rows = scanner.scan({}, {}, self.m, self.cal, t(9, 41, day=DAY), self.cfg, S)
        self.assertEqual(rows[0]["status"], "NO_DATA")
        b = bars([25000, 25001, 25000, 25001])
        self.assertEqual(scanner.scan({"NIFTY": b}, {}, self.m, self.cal, t(10, 0, day=DAY), self.cfg, S)[0]["status"],
                         "NO_SIGNAL")
        bad = bars([25000, 25001])
        bad = [bad[1], bad[0]]
        self.assertEqual(scanner.scan({"NIFTY": bad}, {}, self.m, self.cal, t(10, 0, day=DAY), self.cfg, S)[0]["status"],
                         "ERROR")

    def test_expiry_failing_calendar_is_skipped(self):
        rows = chain_rows().replace("03-11-2026", "05-11-2026")       # a Thursday: not a NIFTY expiry
        m = instruments.parse(HEADER + "\n" + rows)
        b = bars([25000, 25005, 24995, 25003, 25030])
        r = scanner.scan({"NIFTY": b}, {}, m, self.cal, t(9, 41, day=DAY), self.cfg, S)[0]
        self.assertEqual(r["status"], "NO_CONTRACT")
