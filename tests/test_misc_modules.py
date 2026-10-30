import ast
import datetime as dt
import os
import shutil
import tempfile
import unittest

import synth
from helpers import t
from nse_bot import alerts, analyst, events, news, pelosi, report
from nse_bot.broker.paper import PaperBroker
from nse_bot.costs import CostModel
from nse_bot.errors import DataError

PKG = os.path.join(synth.ROOT, "nse_bot")
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>RBI keeps repo rate unchanged; Nifty Bank rises</title><link>https://example.in/a?utm=1</link>
<pubDate>Thu, 29 Oct 2026 04:30:00 GMT</pubDate></item>
<item><title>RBI keeps repo rate unchanged (dup)</title><link>https://example.in/a</link>
<pubDate>Thu, 29 Oct 2026 04:31:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://example.in/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>No date</title><link>https://example.in/nd</link></item>
</channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>GDP data due</title>
<link href="https://example.in/gdp"/><updated>2026-10-29T10:00:00+05:30</updated></entry></feed>"""


class News(unittest.TestCase):
    def test_collect_tags_dedupes_and_keeps_links(self):
        feeds = [{"name": "rss", "url": "https://x/rss"}, {"name": "atom", "url": "https://x/atom"},
                 {"name": "broken", "url": "https://x/broken"}]
        data = {"https://x/rss": RSS, "https://x/atom": ATOM}

        def fetch(url):
            if url not in data:
                raise OSError("timeout")
            return data[url]
        items, errors = news.collect(feeds, t(12, 0), 24, fetcher=fetch)
        titles = [i["title"] for i in items]
        self.assertIn("RBI keeps repo rate unchanged; Nifty Bank rises", titles)
        self.assertNotIn("Old story", titles)
        self.assertEqual(sum(1 for i in items if "example.in/a" in i["link"]), 1)
        rbi = [i for i in items if i["title"].startswith("RBI")][0]
        self.assertEqual(rbi["published"].strftime("%H:%M"), "10:00")    # 04:30 GMT = 10:00 IST
        self.assertIn("rbi", rbi["tags"])
        self.assertIn("banks", rbi["tags"])
        self.assertEqual(len(errors), 1)
        self.assertFalse([i for i in items if i["title"] == "No date"][0]["timestamp_known"])

    def test_bad_xml(self):
        with self.assertRaises(DataError):
            news.parse_feed(b"<rss", "x")


class Events(unittest.TestCase):
    def test_validation(self):
        ok = {"time": "2026-12-04T10:00:00+05:30", "title": "RBI MPC", "impact": "high",
              "source_url": "https://rbi.org.in/x"}
        self.assertEqual(events.validate(ok)["time"].utcoffset(), dt.timedelta(hours=5, minutes=30))
        for bad in ({"source_url": ""}, {"source_url": "http://x"}, {"impact": "huge"},
                    {"time": "2026-12-04T10:00:00"}, {"time": "tomorrow"}):
            with self.assertRaises(DataError):
                events.validate(dict(ok, **bad))
        self.assertEqual(events.load(os.path.join(synth.ROOT, "config", "events.example.json")), [])


class Alerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_ring_once_survives_restart(self):
        got = []
        a = alerts.Alerts(self.dir, dt.date(2026, 10, 29), sink=got.append)
        self.assertIsNotNone(a.emit("stop", "k", "stop hit", t(10, 0)))
        self.assertIsNone(a.emit("stop", "k", "stop hit", t(10, 1)))
        b = alerts.Alerts(self.dir, dt.date(2026, 10, 29), sink=got.append)
        self.assertIsNone(b.emit("stop", "k", "stop hit", t(10, 2)))
        self.assertEqual(len(got), 1)
        with self.assertRaises(ValueError):
            a.emit("party", "k", "x", t(10, 0))

    def test_price_rules_and_missing_price(self):
        got = []
        a = alerts.Alerts(self.dir, dt.date(2026, 10, 29), sink=got.append)
        a.check_price_rules([{"id": "r1", "instrument_id": 1, "above": 100},
                             {"id": "r2", "instrument_id": 2, "below": 50}], {1: 101.0, 2: None}, t(10, 0))
        self.assertEqual(sorted(x["kind"] for x in got), ["price", "stale_data"])

    def test_corrupt_alert_state_rings_rather_than_silences(self):
        os.makedirs(os.path.join(self.dir, "alerts"))
        with open(os.path.join(self.dir, "alerts", "2026-10-29.json"), "w") as f:
            f.write("{")
        got = []
        alerts.Alerts(self.dir, dt.date(2026, 10, 29), sink=got.append).emit("price", "k", "x", t(10, 0))
        self.assertEqual(len(got), 1)


class Analyst(unittest.TestCase):
    def test_context_is_informational(self):
        ev = [events.validate({"time": "2026-10-29T14:00:00+05:30", "title": "RBI MPC", "impact": "high",
                               "source_url": "https://rbi.org.in/x"})]
        c = analyst.context("NIFTY", t(10, 0), dt.date(2026, 10, 29), ev, iv=14.0, iv_history=[10, 12, 14, 16])
        self.assertFalse(c["affects_trading"])
        self.assertEqual(c["days_to_expiry"], 0)
        self.assertEqual(c["iv_percentile"], 75.0)
        self.assertTrue(any("RBI MPC" in n for n in c["notes"]))


class Pelosi(unittest.TestCase):
    def test_not_applicable_and_isolated(self):
        a = pelosi.assess()
        self.assertEqual((a["status"], a["affects_trading"]), ("NOT_APPLICABLE", False))
        for name in os.listdir(PKG):
            if name.endswith(".py") and name not in ("pelosi.py", "cli.py"):
                tree = ast.parse(open(os.path.join(PKG, name)).read())
                names = {n.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for n in node.names}
                self.assertNotIn("pelosi", names, name)


class Report(unittest.TestCase):
    def test_labelled_simulated_and_read_only(self):
        d = tempfile.mkdtemp()
        try:
            PaperBroker(d, CostModel.load(synth.COSTS), 2)
            before = sorted(os.walk(d))
            doc = report.build(d)
            text = report.render(doc)
            self.assertEqual(sorted(os.walk(d)), before)
            self.assertTrue(text.startswith("=") and "SIMULATED RESULTS" in text.splitlines()[1])
            self.assertTrue(text.rstrip().endswith("Not evidence of future profit."))
            self.assertEqual(doc["ledger"]["integrity"], "ok")
        finally:
            shutil.rmtree(d)
