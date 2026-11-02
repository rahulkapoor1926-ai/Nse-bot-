"""FYERS read-only adapter: no order paths, credentials masked, missing/stale data never
treated as good, one source per session, OTM never allowed. Response shapes follow the
fields seen in the live checks of 2026-11-02 (runs 41403687715, 41404326502)."""

import copy
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import unittest

import synth
from helpers import quote
from nse_bot import clock, config, contract_select, feedcheck, instruments, patterns
from nse_bot.broker import fyers
from nse_bot.errors import ConfigError, DataError

SRC = open(os.path.join(os.path.dirname(fyers.__file__), "fyers.py")).read()
ENV = {"FYERS_APP_ID": "APP-100", "FYERS_SECRET_ID": "s3cret", "FYERS_AUTH_CODE": "code123"}
NOW = clock.at(dt.date(2026, 11, 2), "11:00")
EPOCH = int(NOW.timestamp())
IDX, CE, PE, FUT = 101000000026000, 101126110400001, 101126110400002, 101126112361471


class Http(object):
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        path = url.split("?")[0].rsplit("/", 1)[-1]
        r = self.routes.get(path)
        if callable(r):
            r = r(url)
        status, doc = r if r else (404, {"s": "error", "code": -1, "message": "no route"})
        return status, json.dumps(doc).encode()


def depth(sym, bid=100.0, ask=100.5, qty=1300, ltt=EPOCH - 2, ltp=100.2):
    return {"s": "ok", "d": {sym: {"bids": [{"price": bid, "volume": qty}], "ask": [{"price": ask, "volume": qty}],
                                   "ltp": ltp, "ltt": ltt, "v": 500000, "oi": 900000,
                                   "lower_ckt": 1.0, "upper_ckt": 500.0}}}


def chain(spot=25010.0, bid=100.0):
    legs = [{"symbol": "NSE:NIFTY50-INDEX", "ltp": spot, "option_type": ""}]
    for k in (24950, 25000, 25050):
        for typ, tok in (("CE", CE), ("PE", PE)):
            legs.append({"symbol": "NSE:NIFTY26N04%d%s" % (k, typ), "fyToken": str(tok + k), "option_type": typ,
                         "strike_price": k, "bid": bid, "ask": bid + 0.5, "ltp": bid, "oi": 900000,
                         "volume": 500000, "prev_oi": 1,
                         "greeks": {"delta": 0.5, "gamma": 0.001, "theta": -9.0, "vega": 10.0, "iv": 13.0}})
    return {"s": "ok", "data": {"expiryData": [{"date": "04-11-2026", "expiry": "1793786400"}],
                                "optionsChain": legs}}


def routes(**over):
    r = {"validate-authcode": (200, {"s": "ok", "access_token": "TOKEN-XYZ", "refresh_token": "R"}),
         "depth": lambda url: (200, depth(_q(url, "symbol"))),
         "history": (200, {"s": "ok", "candles": [[EPOCH - 120, 1, 2, 0.5, 1.5, 10], [EPOCH - 60, 1.5, 2, 1, 1.8, 12]]}),
         "options-chain-v3": (200, chain())}
    r.update(over)
    return r


def _q(url, key):
    import urllib.parse
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)[key][0]


def client(http, **kw):
    md = fyers.FyersReadOnly(env=ENV, transport=http, sleep=lambda s: None, now=lambda: NOW, **kw)
    return md.bind(None, {IDX: "NSE:NIFTY50-INDEX", CE: "NSE:NIFTY26N0425000CE", FUT: "NSE:NIFTY26NOVFUT"})


class ReadOnlySafety(unittest.TestCase):
    def test_no_order_account_or_profile_endpoints(self):
        for bad in ("/orders", "place_order", "/positions", "/funds", "/holdings", "/multi-order", "/gtt",
                    "/profile", "/tradebook", "/convert", "/logout"):
            self.assertNotIn(bad, SRC)

    def test_only_whitelisted_paths(self):
        md = client(Http(routes()))
        with self.assertRaises(ConfigError):
            md._request("GET", fyers.API, "/profile")
        with self.assertRaises(ConfigError):
            md._request("POST", fyers.DATA, "/quotes")
        with self.assertRaises(ConfigError):
            md._request("DELETE", fyers.DATA, "/depth")

    def test_every_call_is_get_except_the_token(self):
        http = Http(routes())
        md = client(http)
        md.quotes([CE])
        md.candles(IDX, "1m", NOW - dt.timedelta(minutes=5), NOW)
        self.assertEqual([c[0] for c in http.calls], ["POST", "GET", "GET"])
        self.assertTrue(all(c[1].startswith("https://api-t1.fyers.in/") for c in http.calls))
        self.assertEqual(http.calls[1][2]["Authorization"], "APP-100:TOKEN-XYZ")

    def test_credentials_scrubbed_from_errors(self):
        http = Http(routes(depth=(500, {"s": "error", "code": -1, "message": "bad token TOKEN-XYZ s3cret"})))
        md = client(http)
        with self.assertRaises(DataError) as e:
            md.quotes([CE])
        self.assertNotIn("TOKEN-XYZ", str(e.exception))
        self.assertNotIn("s3cret", str(e.exception))

    def test_missing_credentials_refused(self):
        with self.assertRaises(ConfigError):
            fyers.FyersReadOnly(env={"FYERS_APP_ID": "APP-100"})
        with self.assertRaises(ConfigError):
            fyers.FyersReadOnly(env={})

    def test_rate_limit_is_missing_data_and_throttle_waits(self):
        md = client(Http(routes(depth=(429, {"s": "error", "code": 429}))))
        with self.assertRaises(DataError):
            md.quotes([CE])
        waits, clock_t = [], [0.0]
        md = fyers.FyersReadOnly(env=ENV, transport=Http(routes()), per_minute=3,
                                 sleep=lambda s: (waits.append(s), clock_t.__setitem__(0, clock_t[0] + s)),
                                 monotonic=lambda: clock_t[0])
        md.bind(None, {CE: "NSE:X"})
        for _ in range(3):
            md.quotes([CE])              # login + 3 depth = 4 calls > 3/minute
        self.assertTrue(waits and max(waits) > 50)


class Quotes(unittest.TestCase):
    def test_depth_quote_uses_last_trade_time(self):
        q = client(Http(routes())).quotes([CE])[CE]
        self.assertEqual((q.bid, q.ask, q.bid_qty, q.ask_qty, q.source), (100.0, 100.5, 1300, 1300, "fyers"))
        self.assertEqual(q.ts, clock.from_epoch_ms((EPOCH - 2) * 1000))

    def test_day_start_stamp_or_missing_ltt_is_stale(self):
        start = int(clock.at(NOW.date(), "00:00").timestamp())
        for ltt in (None, 0, start, int(clock.at(NOW.date(), "05:30").timestamp())):
            md = client(Http(routes(depth=lambda url, l=ltt: (200, depth(_q(url, "symbol"), ltt=l)))))
            q = md.quotes([CE])[CE]
            self.assertIsNone(q.ts, ltt)

    def test_zero_bid_is_missing_not_zero(self):
        md = client(Http(routes(depth=lambda url: (200, depth(_q(url, "symbol"), bid=0, qty=0)))))
        q = md.quotes([CE])[CE]
        self.assertIsNone(q.bid)
        self.assertIsNone(q.bid_qty)

    def test_unknown_instrument_refused(self):
        with self.assertRaises(DataError):
            client(Http(routes())).quotes([123])


class Candles(unittest.TestCase):
    def test_parsed_in_ist_and_bounded(self):
        rows = client(Http(routes())).candles(IDX, "1m", NOW - dt.timedelta(minutes=5), NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1][0], clock.from_epoch_ms((EPOCH - 60) * 1000))
        self.assertEqual(rows[-1][4], 1.8)

    def test_malformed_or_unsupported(self):
        md = client(Http(routes(history=(200, {"s": "ok", "candles": [[EPOCH, None, 1, 1, 1, 1]]}))))
        with self.assertRaises(DataError):
            md.candles(IDX, "1m", NOW - dt.timedelta(minutes=5), NOW)
        with self.assertRaises(DataError):
            md.candles(IDX, "2m", NOW - dt.timedelta(minutes=5), NOW)

    def test_long_ranges_are_chunked(self):
        http = Http(routes())
        client(http).candles(IDX, "1m", NOW - dt.timedelta(days=200), NOW)
        self.assertEqual(sum(1 for c in http.calls if "/history" in c[1]), 3)


class Chain(unittest.TestCase):
    def test_chain_in_bot_shape_with_depth_for_atm_and_itm(self):
        http = Http(routes())
        doc = client(http).option_chain(IDX, dt.date(2026, 11, 4))
        parsed = contract_select.parse_chain(doc)
        self.assertIs(parsed, doc)
        self.assertEqual(doc["spot"], 25010.0)
        self.assertEqual(len(doc["legs"]), 6)
        enriched = {(l["option_type"], l["strike"]) for l in doc["legs"] if l["bid_qty"] is not None}
        # ATM 25000; CE ITM = lower strikes, PE ITM = higher strikes; OTM legs are not enriched
        self.assertEqual(enriched, {("CE", 24950.0), ("CE", 25000.0), ("PE", 25000.0), ("PE", 25050.0)})
        leg = next(l for l in doc["legs"] if l["option_type"] == "CE" and l["strike"] == 25000)
        self.assertEqual((leg["delta"], leg["theta"], leg["vega"], leg["iv"]), (0.5, -9.0, 10.0, 13.0))

    def test_missing_greeks_stay_missing(self):
        c = chain()
        for l in c["data"]["optionsChain"][1:]:
            del l["greeks"]
        doc = client(Http(routes(**{"options-chain-v3": (200, c)}))).option_chain(IDX, dt.date(2026, 11, 4))
        self.assertTrue(all(l["delta"] is None and l["iv"] is None for l in doc["legs"]))

    def test_unknown_expiry_refused(self):
        with self.assertRaises(DataError):
            client(Http(routes())).option_chain(IDX, dt.date(2026, 11, 11))


class Master(unittest.TestCase):
    FO = {
        "NSE:NIFTY26NOVFUT": {"fyToken": str(FUT), "underSym": "NIFTY", "underFyTok": str(IDX),
                              "expiryDate": "1795428600", "optType": "XX", "exInstType": 11, "strikePrice": -1.0,
                              "minLotSize": 65, "tickSize": 0.1, "qtyFreeze": "1800", "symbolDetails": "NIFTY 23 Nov 26 FUT"},
        "NSE:NIFTY26N0425000CE": {"fyToken": str(CE), "underSym": "NIFTY", "underFyTok": str(IDX),
                                  "expiryDate": "1793786400", "optType": "CE", "exInstType": 14, "strikePrice": 25000.0,
                                  "minLotSize": 65, "tickSize": 0.05, "qtyFreeze": "1800"},
        "NSE:NIFTY26N0425000PE": {"fyToken": str(PE), "underSym": "NIFTY", "underFyTok": str(IDX),
                                  "expiryDate": "1793786400", "optType": "PE", "exInstType": 14, "strikePrice": 25000.0,
                                  "minLotSize": 65, "tickSize": 0.05, "qtyFreeze": "1800"},
        "NSE:BANKNIFTY26NOVFUT": {"fyToken": "9", "underSym": "BANKNIFTY", "expiryDate": "1795428600",
                                  "optType": "XX", "exInstType": 11, "minLotSize": 30, "tickSize": 0.2},
    }
    CM = {"NSE:NIFTYBEES-EQ": {"fyToken": "101000000010576", "exSymbol": "NIFTYBEES", "tickSize": 0.01,
                               "exInstType": 9, "symbolDesc": "NIPPON NIFTY ETF"},
          "NSE:NIFTY50-INDEX": {"fyToken": str(IDX), "tickSize": 0.05}}

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_master_round_trip_into_the_bot_format(self):
        def http(method, url, headers, body, timeout):
            return 200, json.dumps(self.FO if "NSE_FO" in url else self.CM).encode()
        path = os.path.join(self.dir, "m.csv")
        info = fyers.build_master(path, ["NIFTY"], {"NIFTY": "Nifty 50"}, transport=http)
        self.assertTrue(info["cash_included"])
        m = instruments.load(path, ["NIFTY"], ["Nifty 50"])
        self.assertEqual(m.index_id("Nifty 50"), IDX)
        c = m.by_id(CE)
        self.assertEqual((c.strike, c.option_type, c.lot_size, c.expiry), (25000.0, "CE", 65, dt.date(2026, 11, 4)))
        self.assertEqual(m.current_future("NIFTY", dt.date(2026, 11, 2)).instrument_id, FUT)
        self.assertNotIn(9, [f.instrument_id for f in m.futures])            # BANKNIFTY filtered out
        self.assertIn("NIFTYBEES", m.cash)
        syms = fyers.load_symbols(path)
        self.assertEqual(syms[IDX], "NSE:NIFTY50-INDEX")
        self.assertEqual(syms[101000000010576], "NSE:NIFTYBEES-EQ")

    def test_unverified_index_refused(self):
        with self.assertRaises(DataError):
            fyers.master_rows(self.FO, None, ["BANKNIFTY"], {"BANKNIFTY": "Nifty Bank"})


class Ctx(object):
    def __init__(self, md, source="fyers"):
        self.cfg = synth.cfg("/tmp/unused", data_source=source)
        self.md = md
        self.master = type("M", (), {"index_id": staticmethod(lambda name: IDX)})()


class FeedChecks(unittest.TestCase):
    def md(self, **depth_kw):
        return client(Http(routes(depth=lambda url: (200, depth(_q(url, "symbol"), **depth_kw)))))

    def test_fresh_spot_passes(self):
        ctx = Ctx(self.md(ltp=25010.0))
        q = quote(at=NOW)
        q.source = "fyers"
        self.assertEqual(feedcheck.entry_problems(ctx, "NIFTY", q, 25000.0, NOW), [])

    def test_stale_or_unstamped_spot_blocks(self):
        p = feedcheck.entry_problems(Ctx(self.md(ltp=25010.0, ltt=EPOCH - 60)), "NIFTY", None, 25000.0, NOW)
        self.assertTrue(any("stale" in x for x in p))
        p = feedcheck.entry_problems(Ctx(self.md(ltp=25010.0, ltt=None)), "NIFTY", None, 25000.0, NOW)
        self.assertTrue(any("cannot be verified" in x for x in p))

    def test_spot_disagreeing_with_signal_blocks(self):
        p = feedcheck.entry_problems(Ctx(self.md(ltp=25300.0)), "NIFTY", None, 25000.0, NOW)
        self.assertTrue(any("differs" in x for x in p))

    def test_mixed_sources_refused(self):
        q = quote(at=NOW)
        q.source = "sahi"
        p = feedcheck.entry_problems(Ctx(self.md(ltp=25010.0)), "NIFTY", q, 25000.0, NOW)
        self.assertTrue(any("refusing to mix" in x for x in p))

    def test_spot_fetch_failure_blocks(self):
        md = client(Http(routes(depth=(500, {"s": "error"}))))
        p = feedcheck.entry_problems(Ctx(md), "NIFTY", None, 25000.0, NOW)
        self.assertTrue(any("unavailable" in x for x in p))


class Rules(unittest.TestCase):
    def test_otm_strike_offset_refused(self):
        s = copy.deepcopy(synth.STRATEGY)
        for off in (1, 2, 0.5, True):
            s["strike_offset"] = off
            with self.assertRaises(DataError):
                patterns.validate_strategy(s)
        for off in (0, -1, -2):
            s["strike_offset"] = off
            patterns.validate_strategy(s)

    def test_config_needs_a_single_known_source(self):
        c = synth.cfg("/tmp/x")
        for bad in ("both", "", None, ["sahi", "fyers"]):
            c["data_source"] = bad
            with self.assertRaises(ConfigError):
                config.validate(c)
        c = synth.cfg("/tmp/x")
        del c["data_source"]
        with self.assertRaises(ConfigError):
            config.validate(c)

    def test_example_config_is_fyers_nifty_only_with_unchanged_limits(self):
        c = config.load(os.path.join(synth.ROOT, "config", "paper.example.json"))
        self.assertEqual(c["data_source"], "fyers")
        self.assertEqual(c["underlyings"], ["NIFTY"])
        self.assertEqual((c["capital_inr"], c["max_daily_loss_inr"], c["max_lots_per_trade"],
                          c["max_open_positions"], c["gate_min_examples"]), (20000, 500, 1, 1, 10000))
        self.assertFalse(c["live_orders_enabled"])


if __name__ == "__main__":
    unittest.main()


class BotWithSpotCheck(unittest.TestCase):
    """The ORB entry path on a feed that requires spot verification (as FYERS does)."""

    def setUp(self):
        import test_bot as TB
        self.TB = TB
        self.case = TB.Cycle("test_closed_market_does_nothing")
        self.case.setUp()
        self.case.cfg["data_source"] = "fyers"
        self.case.md.verifies_spot = True
        self.case.approve()
        self.ce = self.case.ce

    def tearDown(self):
        self.case.tearDown()

    def opt(self, now):
        q = quote(bid=30, ask=30.5, at=now, iid=self.ce.instrument_id)
        q.source = "fyers"
        self.case.md.q[self.ce.instrument_id] = q

    def run_at(self, now):
        from nse_bot import bot
        return bot.run_once(self.case.ctx(now), now)

    def test_approved_signal_still_blocked_without_a_fresh_spot(self):
        now = self.TB.t(9, 41)
        self.opt(now)
        r = self.run_at(now)
        c = r["candidates"][0]
        self.assertEqual(c["risk"]["verdict"], "BLOCK")
        self.assertTrue(any("spot" in x for x in c["risk"]["reasons"]), c["risk"]["reasons"])
        self.assertEqual(self.case.broker.ledger.records(), [])

    def test_fresh_matching_spot_lets_the_approved_paper_entry_through_with_one_lot(self):
        now = self.TB.t(9, 41)
        self.opt(now)
        s = quote(bid=None, ask=None, at=now, iid=26000, ltp=25050.0)
        s.source = "fyers"
        self.case.md.q[26000] = s
        self.run_at(now)
        pos = self.case.broker.ledger.positions()[str(self.ce.instrument_id)]
        self.assertEqual(pos["qty"], 65)                                  # exactly one lot
        fill = [r for r in self.case.broker.ledger.records() if r["event"] == "fill"][0]
        self.assertEqual(fill.get("meta", {}).get("data_source"), "fyers")
        # same signal next cycle: no second buy (no averaging)
        self.opt(self.TB.t(9, 42))
        self.run_at(self.TB.t(9, 42))
        self.assertEqual(len([r for r in self.case.broker.ledger.records() if r["event"] == "fill"]), 1)

    def test_sahi_quote_in_a_fyers_session_is_refused(self):
        now = self.TB.t(9, 41)
        q = quote(bid=30, ask=30.5, at=now, iid=self.ce.instrument_id)
        q.source = "sahi"
        self.case.md.q[self.ce.instrument_id] = q
        s = quote(bid=None, ask=None, at=now, iid=26000, ltp=25050.0)
        s.source = "fyers"
        self.case.md.q[26000] = s
        r = self.run_at(now)
        self.assertTrue(any("mix" in x for x in r["candidates"][0]["risk"]["reasons"]))
        self.assertEqual(self.case.broker.ledger.records(), [])


class Budget(unittest.TestCase):
    def test_candles_reused_within_one_bar_and_refetched_after(self):
        http = Http(routes())
        md = client(http)
        start = NOW - dt.timedelta(minutes=30)
        md.candles(IDX, "5m", start, NOW)
        md.candles(IDX, "5m", start, NOW + dt.timedelta(seconds=30))
        n = sum(1 for c in http.calls if "/history" in c[1])
        self.assertEqual(n, 1)
        md.candles(IDX, "5m", start, NOW + dt.timedelta(minutes=5))
        self.assertEqual(sum(1 for c in http.calls if "/history" in c[1]), 2)

    def test_daily_cap_fails_closed(self):
        md = client(Http(routes()), per_day=3)
        md.quotes([CE])
        md.quotes([CE])                      # login + 2 depth = 3
        with self.assertRaises(DataError):
            md.quotes([CE])


class ReceiptTime(unittest.TestCase):
    """Found in the first live run: quotes fetched a few seconds after the cycle started
    were flagged 'from the future'. Age is now measured from the later of the two times."""

    def setUp(self):
        self.cfg = synth.cfg("/tmp/x")

    def test_quote_received_after_cycle_start_is_not_future(self):
        from nse_bot import quotes as Q
        q = quote(at=NOW + dt.timedelta(seconds=8))
        q.extra = {"received_at": (NOW + dt.timedelta(seconds=9)).isoformat()}
        self.assertEqual(Q.check(q, NOW, self.cfg, 65), [])

    def test_age_is_never_understated(self):
        from nse_bot import quotes as Q
        q = quote(at=NOW)
        q.extra = {"received_at": (NOW + dt.timedelta(seconds=30)).isoformat()}
        self.assertTrue(any("stale" in p for p in Q.check(q, NOW, self.cfg, 65)))
        bid, probs = Q.usable_for_exit(q, NOW, self.cfg)
        self.assertIsNone(bid)

    def test_genuinely_future_stamp_still_flagged(self):
        from nse_bot import quotes as Q
        q = quote(at=NOW + dt.timedelta(seconds=30))
        q.extra = {"received_at": NOW.isoformat()}
        self.assertTrue(any("future" in p for p in Q.check(q, NOW, self.cfg, 65)))
