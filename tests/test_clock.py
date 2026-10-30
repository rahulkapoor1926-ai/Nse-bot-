import datetime as dt
import unittest

import synth
from nse_bot import clock
from nse_bot.errors import CalendarError

D = dt.date


class Calendar(unittest.TestCase):
    def setUp(self):
        self.cal = synth.calendar()

    def test_holidays_and_weekends(self):
        self.assertFalse(self.cal.is_trading_day(D(2026, 10, 20)))   # Dussehra
        self.assertEqual(self.cal.holiday_name(D(2026, 1, 26)), "Republic Day")
        self.assertFalse(self.cal.is_trading_day(D(2026, 10, 31)))   # Saturday
        self.assertTrue(self.cal.is_trading_day(D(2026, 10, 30)))

    def test_unknown_year_fails_closed(self):
        with self.assertRaises(CalendarError):
            self.cal.is_trading_day(D(2027, 1, 4))
        with self.assertRaises(CalendarError):
            self.cal.is_trading_day(D(2025, 12, 31))

    def test_session_status(self):
        st = self.cal.status(dt.datetime(2026, 10, 30, 9, 15, tzinfo=clock.IST))
        self.assertTrue(st["open"])
        st = self.cal.status(dt.datetime(2026, 10, 30, 15, 30, tzinfo=clock.IST))
        self.assertFalse(st["open"])
        self.assertTrue(st["next_open"].startswith("2026-11-02T09:15"))
        st = self.cal.status(dt.datetime(2026, 10, 30, 4, 0, tzinfo=dt.timezone.utc))   # 09:30 IST
        self.assertTrue(st["open"])
        st = self.cal.status(dt.datetime(2026, 11, 10, 11, 0, tzinfo=clock.IST))
        self.assertEqual(st["reason"], "Diwali-Balipratipada")

    def test_naive_time_rejected(self):
        with self.assertRaises(CalendarError):
            self.cal.status(dt.datetime(2026, 10, 30, 10, 0))

    def test_weekly_expiry_tuesday_and_holiday_shift(self):
        self.assertEqual(self.cal.weekly_expiry(D(2026, 10, 29)), D(2026, 11, 3))
        self.assertEqual(self.cal.weekly_expiry(D(2026, 10, 27)), D(2026, 10, 27))
        self.assertEqual(self.cal.weekly_expiry(D(2026, 3, 2)), D(2026, 3, 2))       # Holi Tue 3 Mar -> Mon 2 Mar
        self.assertEqual(self.cal.weekly_expiry(D(2026, 10, 16)), D(2026, 10, 19))   # Dussehra Tue 20 Oct -> Mon 19 Oct

    def test_monthly_expiry_last_tuesday(self):
        self.assertEqual(self.cal.monthly_expiry(2026, 10), D(2026, 10, 27))
        self.assertEqual(self.cal.monthly_expiry(2026, 3), D(2026, 3, 30))           # 31 Mar holiday
        self.assertEqual(self.cal.monthly_expiry(2026, 11), D(2026, 11, 24 - 1))     # 24 Nov holiday -> 23 Nov

    def test_check_expiry(self):
        self.assertTrue(self.cal.check_expiry("NIFTY", D(2026, 11, 3)))
        self.assertTrue(self.cal.check_expiry("BANKNIFTY", D(2026, 10, 27)))
        self.assertFalse(self.cal.check_expiry("BANKNIFTY", D(2026, 11, 3)))       # no BANKNIFTY weekly
        self.assertFalse(self.cal.check_expiry("NIFTY", D(2026, 10, 29)))          # a Thursday

    def test_epoch_ms_to_ist(self):
        t = clock.from_epoch_ms(1747798200000)
        self.assertEqual(t.utcoffset(), dt.timedelta(hours=5, minutes=30))
        self.assertEqual(t.strftime("%Y-%m-%d %H:%M"), "2025-05-21 09:00")

    def test_malformed_holiday_file(self):
        with self.assertRaises(CalendarError):
            clock.Calendar({"2026": {"holidays": {"2027-01-01": "x"}}})
        with self.assertRaises(CalendarError):
            clock.Calendar.load("/nonexistent.json")
