"""NSE clock: Asia/Kolkata time, the 09:15-15:30 session, holidays and expiries.

IST has no daylight saving, so the US DST helpers of the upstream skills are
not needed. Holidays come only from nse_bot/data/nse_holidays.json; a date in a
year that file does not cover raises CalendarError instead of assuming "open".

Expiry dates of real contracts come from the SAHI scrip master. The rules here
(NIFTY weekly = Tuesday, monthly = last Tuesday, moved to the previous trading
day when Tuesday is a holiday) are a cross-check only: see check_expiry().
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

from .errors import CalendarError

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)
TUESDAY = 1


class Calendar(object):
    def __init__(self, years):
        """years: {"2026": {"holidays": {"YYYY-MM-DD": name}, ...}}"""
        self.years = {}
        for y, doc in years.items():
            try:
                hol = {dt.date.fromisoformat(d): name for d, name in doc["holidays"].items()}
            except (KeyError, ValueError, TypeError) as e:
                raise CalendarError("holiday list for %s is malformed: %s" % (y, e))
            for d in hol:
                if str(d.year) != str(y):
                    raise CalendarError("holiday %s is filed under year %s" % (d, y))
            self.years[int(y)] = hol

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise CalendarError("cannot read holiday file %s: %s" % (path, e))
        return cls(doc.get("years") or {})

    def _year(self, d):
        if d.year not in self.years:
            raise CalendarError("no NSE holiday list for %d; add it from the official NSE circular "
                                "before trading or backtesting that year" % d.year)
        return self.years[d.year]

    def holiday_name(self, d):
        return self._year(d).get(d)

    def is_trading_day(self, d):
        hol = self._year(d)
        return d.weekday() < 5 and d not in hol

    def previous_trading_day(self, d):
        x = d - dt.timedelta(days=1)
        while not self.is_trading_day(x):
            x -= dt.timedelta(days=1)
        return x

    def next_trading_day(self, d):
        x = d + dt.timedelta(days=1)
        while not self.is_trading_day(x):
            x += dt.timedelta(days=1)
        return x

    def session(self, d):
        """(open, close) as aware IST datetimes, or None on a non-trading day."""
        if not self.is_trading_day(d):
            return None
        return (dt.datetime.combine(d, SESSION_OPEN, IST), dt.datetime.combine(d, SESSION_CLOSE, IST))

    def status(self, now):
        """Market status at an aware datetime."""
        now = to_ist(now)
        d = now.date()
        s = self.session(d)
        if s and s[0] <= now < s[1]:
            return {"open": True, "now": now.isoformat(), "closes": s[1].isoformat()}
        nxt = d if (s and now < s[0]) else self.next_trading_day(d)
        why = self.holiday_name(d) or ("weekend" if d.weekday() >= 5 else
                                        "before open" if s and now < s[0] else "after close")
        return {"open": False, "now": now.isoformat(), "reason": why,
                "next_open": dt.datetime.combine(nxt, SESSION_OPEN, IST).isoformat()}

    # expiries -----------------------------------------------------------------

    def _adjust(self, d):
        return d if self.is_trading_day(d) else self.previous_trading_day(d)

    def last_tuesday(self, year, month):
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        d = nxt - dt.timedelta(days=1)
        while d.weekday() != TUESDAY:
            d -= dt.timedelta(days=1)
        return d

    def monthly_expiry(self, year, month):
        return self._adjust(self.last_tuesday(year, month))

    def weekly_expiry(self, d):
        """The NIFTY weekly expiry that covers date d (Tuesday of d's week, or later week if past)."""
        t = d + dt.timedelta(days=(TUESDAY - d.weekday()) % 7)
        e = self._adjust(t)
        if e < d:
            e = self._adjust(t + dt.timedelta(days=7))
        return e

    def expected_expiries(self, underlying, year, month):
        if underlying == "BANKNIFTY":
            return {self.monthly_expiry(year, month)}
        if underlying == "NIFTY":
            first = dt.date(year, month, 1)
            out, d = set(), first
            while d.month == month:
                if d.weekday() == TUESDAY:
                    out.add(self._adjust(d))
                d += dt.timedelta(days=1)
            return out
        raise CalendarError("no expiry rule for %s" % underlying)

    def check_expiry(self, underlying, expiry):
        """True if a contract's expiry (from the scrip master) matches the published rule.

        False means the master and the rule disagree; callers treat that contract as
        unverified and skip it. A holiday moving an expiry into the previous month
        is handled by also checking the next month's set.
        """
        if not self.is_trading_day(expiry):
            return False
        cands = self.expected_expiries(underlying, expiry.year, expiry.month)
        if expiry.month < 12:
            try:
                cands |= self.expected_expiries(underlying, expiry.year, expiry.month + 1)
            except CalendarError:
                pass
        return expiry in cands


def to_ist(t):
    if t.tzinfo is None:
        raise CalendarError("naive datetime %s: every time must carry a timezone" % t)
    return t.astimezone(IST)


def now_ist():
    return dt.datetime.now(IST)


def from_epoch_ms(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).astimezone(IST)


def at(d, hhmm):
    h, m = hhmm.split(":")
    return dt.datetime.combine(d, dt.time(int(h), int(m)), IST)
