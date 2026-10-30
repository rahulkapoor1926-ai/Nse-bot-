import datetime as dt
import unittest

import synth
from helpers import quote, t
from nse_bot import quotes
from nse_bot.errors import DataError

CFG = synth.cfg("/tmp/unused")


class Sahi(unittest.TestCase):
    def test_paise_and_none_preserved(self):
        q = quotes.from_sahi("1914891", {
            "last_trade_price": 12345, "last_trade_time": 1761815400000, "volume_today": 5,
            "open_interest": 0, "prev_open_interest": 0, "dpr_upper": 50000, "dpr_lower": 5,
            "best_buys": [{"price": 12340, "qty": 650, "num_orders": 3}], "best_sells": []})
        self.assertEqual((q.ltp, q.bid, q.ask, q.bid_qty, q.ask_qty, q.oi), (123.45, 123.4, None, 650, None, 0))
        self.assertEqual(q.ts.utcoffset(), dt.timedelta(hours=5, minutes=30))

    def test_unknown_instrument_and_bad_types(self):
        with self.assertRaises(DataError):
            quotes.from_sahi(1, None)
        with self.assertRaises(DataError):
            quotes.from_sahi(1, {"last_trade_price": 12.5, "best_buys": [], "best_sells": []})


class Checks(unittest.TestCase):
    def test_good(self):
        self.assertEqual(quotes.check(quote(), t(10, 0, 5), CFG, 65), [])

    def test_missing_prices_are_not_zero(self):
        p = quotes.check(quote(bid=None), t(10, 0, 5), CFG, 65)
        self.assertIn("missing bid", p)
        self.assertEqual(quotes.check(None, t(10, 0), CFG, 65), ["no quote"])

    def test_stale_future_and_untimed(self):
        self.assertTrue(quotes.check(quote(at=t(9, 59)), t(10, 0), CFG, 65)[0].startswith("stale"))
        self.assertIn("future", quotes.check(quote(at=t(10, 1)), t(10, 0), CFG, 65)[0])
        q = quote()
        q.ts = None
        self.assertIn("no timestamp", quotes.check(q, t(10, 0), CFG, 65)[0])

    def test_crossed_wide_thin_illiquid(self):
        self.assertIn("crossed", quotes.check(quote(bid=101, ask=100), t(10, 0), CFG, 65)[0])
        self.assertTrue(any("spread" in p for p in quotes.check(quote(bid=90, ask=100), t(10, 0), CFG, 65)))
        self.assertTrue(any("thin" in p for p in quotes.check(quote(qty=65), t(10, 0), CFG, 65)))
        self.assertTrue(any("volume" in p for p in quotes.check(quote(volume=None), t(10, 0), CFG, 65)))
        self.assertTrue(any("open interest" in p for p in quotes.check(quote(oi=10), t(10, 0), CFG, 65)))

    def test_price_band(self):
        q = quote(ltp=500)
        q.dpr_low, q.dpr_high = 5.0, 300.0
        self.assertTrue(any("band" in p for p in quotes.check(q, t(10, 0), CFG, 65)))

    def test_exit_needs_fresh_bid(self):
        self.assertEqual(quotes.usable_for_exit(quote(), t(10, 0, 5), CFG), (100.0, []))
        self.assertIsNone(quotes.usable_for_exit(quote(at=t(9, 50)), t(10, 0), CFG)[0])
        self.assertIsNone(quotes.usable_for_exit(quote(bid=None), t(10, 0), CFG)[0])
