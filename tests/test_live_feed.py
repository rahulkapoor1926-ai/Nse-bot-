"""FYERS WebSocket feed for the dashboard: ticks kept per symbol with their own times, symbols follow
the recorder's ATM/ITM set, nothing secret written, the bots never read it."""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from helpers import t
from nse_bot import live_feed as LF
import test_web


class FakeSocket(object):
    def __init__(self, store):
        self.store, self.subs, self.unsubs, self.closed = store, [], [], False

    def connect(self):
        self.store.event("connected")

    def subscribe(self, symbols, data_type):
        self.subs.append((list(symbols), data_type))
        for s in symbols:
            self.store.on_message({"symbol": s, "ltp": 10.0, "bid_price": 9.9, "ask_price": 10.1,
                                   "exch_feed_time": 1, "type": "sf", "precision": 2})

    def unsubscribe(self, symbols, data_type):
        self.unsubs.append(list(symbols))

    def close_connection(self):
        self.closed = True


class LiveFeed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_symbols_follow_the_recorder_and_index_comes_first(self):
        self.assertEqual(LF.recorder_symbols(self.dir, test_web.DAY), [LF.SPOT])
        test_web.write_recording(self.dir)
        syms = LF.recorder_symbols(self.dir, test_web.DAY)
        self.assertEqual(syms[0], LF.SPOT)
        self.assertEqual(len(syms), 4)

    def test_run_writes_ticks_with_times_and_no_secrets(self):
        test_web.write_recording(os.path.join(self.dir, "rec"))
        clockv = [t(10, 5)]

        def now():
            return clockv[0]

        def sleep(_):
            clockv[0] += dt.timedelta(seconds=1)
        socks = []

        def factory(store):
            socks.append(FakeSocket(store))
            return socks[0]
        os.environ["FYERS_ACCESS_TOKEN"] = "SECRET-TOKEN-XYZ"
        try:
            LF.run(os.path.join(self.dir, "rec"), os.path.join(self.dir, "out"), t(10, 5, 3), factory,
                   now=now, sleep=sleep, log=lambda *a: None)
        finally:
            del os.environ["FYERS_ACCESS_TOKEN"]
        path = os.path.join(self.dir, "out", test_web.DAY.isoformat(), LF.FILE)
        raw = open(path).read()
        self.assertNotIn("SECRET", raw)
        doc = json.loads(raw)
        self.assertEqual(len(doc["subscribed"]), 4)
        q = doc["quotes"]["NSE:NIFTY26N0325050CE"]
        self.assertEqual((q["ltp"], q["bid_price"], q["ask_price"], q["exch_feed_time"]), (10.0, 9.9, 10.1, 1))
        self.assertIn("received_at", q)
        self.assertNotIn("precision", q)
        self.assertEqual(socks[0].subs[0][1], "SymbolUpdate")
        self.assertTrue(socks[0].closed)

    def test_non_tick_messages_are_events_not_quotes(self):
        s = LF.TickStore(lambda: t(10, 5))
        s.on_message({"type": "cn", "code": 200, "message": "Authentication done", "s": "ok"})
        s.on_message("garbage")
        self.assertEqual(s.quotes, {})
        self.assertEqual(s.events[-1]["detail"]["message"], "Authentication done")

    def test_bots_never_read_the_feed(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nse_bot")
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.endswith(".py") and f != "live_feed.py" and "web" not in dirpath:
                    self.assertNotIn("live_feed", open(os.path.join(dirpath, f)).read(), f)


if __name__ == "__main__":
    unittest.main()
