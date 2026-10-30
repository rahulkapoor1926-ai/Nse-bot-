import datetime as dt
import json
import unittest

from nse_bot.broker import sahi
from nse_bot.errors import ConfigError

ENV = {"SAHI_KEY_ID": "kid-123", "SAHI_KEY_SECRET": "s3cr3t-value"}
TOKEN = "eyJ.fake.token"


class Fake(object):
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        for key, resp in self.routes:
            if key in url:
                r = resp.pop(0) if isinstance(resp, list) else resp
                if isinstance(r, Exception):
                    raise r
                status, doc = r[0], r[1]
                hdrs = r[2] if len(r) > 2 else {}
                return status, hdrs, json.dumps(doc).encode()
        raise AssertionError("unexpected url %s" % url)


def token(scope="marketdata.read"):
    return (200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600,
                  "expires_at": "2026-10-31T06:00:00+05:30", "scope": scope})


def client(routes):
    f = Fake(routes)
    return sahi.SahiReadOnly(env=ENV, transport=f, sleep=lambda s: None, monotonic=lambda: 1e9), f


class Auth(unittest.TestCase):
    def test_hosts_match_official_sdk(self):
        self.assertEqual((sahi.AUTH_BASE, sahi.API_BASE), ("https://apis.sahi.com", "https://direct.sahi.com"))

    def test_mint_requests_read_scope_only_and_hides_token(self):
        c, f = client([("apikeys/token", token())])
        info = c.mint()
        method, url, headers, body = f.calls[0]
        self.assertEqual((method, url), ("POST", "https://apis.sahi.com/api/v1/apikeys/token"))
        self.assertEqual(body["scopes"], ["marketdata.read"])
        self.assertNotIn("Authorization", headers)
        self.assertNotIn(TOKEN, json.dumps(info))

    def test_wider_scope_refused(self):
        c, _ = client([("apikeys/token", token("marketdata.read orders.write"))])
        with self.assertRaises(sahi.SahiError):
            c.mint()

    def test_missing_credentials(self):
        with self.assertRaises(ConfigError):
            sahi.SahiReadOnly(env={"SAHI_KEY_ID": "x"})

    def test_secret_scrubbed_from_errors(self):
        c, _ = client([("apikeys/token", (401, {"code": "Unauthorized", "message": "bad secret s3cr3t-value"}))])
        with self.assertRaises(sahi.SahiError) as cm:
            c.mint()
        self.assertNotIn("s3cr3t-value", str(cm.exception))
        self.assertIn("401", str(cm.exception))


class ReadOnly(unittest.TestCase):
    def test_only_whitelisted_paths(self):
        c, _ = client([])
        for m, p in (("GET", "/v1/orders"), ("POST", "/v1/orders"), ("PUT", "/v1/orders/1"),
                     ("DELETE", "/v1/orders/1"), ("GET", "/v1/funds")):
            with self.assertRaises(ConfigError):
                c._call(m, sahi.API_BASE, p)

    def test_quotes_batch_and_convert(self):
        q = {"last_trade_price": 10050, "last_trade_time": 1761800000000, "volume_today": 1, "open_interest": 2,
             "best_buys": [{"price": 10000, "qty": 650, "num_orders": 1}],
             "best_sells": [{"price": 10050, "qty": 650, "num_orders": 1}]}
        c, f = client([("apikeys/token", token()), ("marketdata/quotes", [(200, {"data": {str(i): q for i in range(100)}}),
                                                                         (200, {"data": {"100": q, "999": None}})])])
        out = c.quotes(range(101))
        self.assertEqual(len(out), 101)
        self.assertEqual((out[5].bid, out[5].ask), (100.0, 100.5))
        self.assertEqual(sum(1 for x in f.calls if "quotes" in x[1]), 2)
        self.assertEqual(f.calls[1][2]["Authorization"], "Bearer " + TOKEN)

    def test_401_remints_once(self):
        c, f = client([("apikeys/token", [token(), token()]),
                       ("expiries", [(401, {"code": "UNAUTHORIZED"}), (200, {"expiries": ["2026-11-03"]})])])
        self.assertEqual(c.expiries(26000), [dt.date(2026, 11, 3)])
        self.assertEqual(sum(1 for x in f.calls if "token" in x[1]), 2)

    def test_429_retry_after(self):
        slept = []
        f = Fake([("apikeys/token", token()),
                  ("expiries", [(429, {"code": "RATE_LIMITED"}, {"Retry-After": "2"}), (200, {"expiries": []})])])
        c = sahi.SahiReadOnly(env=ENV, transport=f, sleep=slept.append, monotonic=lambda: 1e9)
        self.assertEqual(c.expiries(1), [])
        self.assertIn(2, slept)

    def test_long_retry_after_not_waited(self):
        c, _ = client([("apikeys/token", token()), ("expiries", (429, {"code": "RATE_LIMITED"}, {"Retry-After": "3600"}))])
        with self.assertRaises(sahi.SahiError):
            c.expiries(1)

    def test_network_failure(self):
        c, _ = client([("apikeys/token", token()), ("expiries", OSError("connection reset"))])
        with self.assertRaises(sahi.SahiError) as cm:
            c.expiries(1)
        self.assertIn("network error", str(cm.exception))

    def test_candles_paging_and_paise(self):
        p1 = {"candles": [[1793334000000, 2500000, 2501000, 2499000, 2500500, 0]], "has_more": True}
        p0 = {"candles": [[1793332200000, 2490000, 2491000, 2489000, 2490500, 0]], "has_more": False}
        c, f = client([("apikeys/token", token()), ("candles", [(200, p1), (200, p0)])])
        s = dt.datetime(2026, 10, 30, 9, 15, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
        rows = c.candles(26000, "5m", s, s + dt.timedelta(hours=1))
        self.assertEqual([r[4] for r in rows], [24905.0, 25005.0])
        self.assertIn("to=1793333999999", f.calls[-1][1])

    def test_malformed_candle(self):
        c, _ = client([("apikeys/token", token()), ("candles", (200, {"candles": [[1, None, 1, 1, 1, 0]], "has_more": False}))])
        s = dt.datetime(2026, 10, 30, 9, 15, tzinfo=dt.timezone.utc)
        with self.assertRaises(sahi.SahiError):
            c.candles(1, "5m", s, s)
