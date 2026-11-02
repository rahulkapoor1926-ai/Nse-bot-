"""The FYERS check is read-only, masks every credential and reports failures as failures."""

import contextlib
import io
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fyers_check as F  # noqa: E402

SRC = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "fyers_check.py")).read()
ENV = {"FYERS_APP_ID": "APP-100", "FYERS_SECRET_ID": "s3cret", "FYERS_AUTH_CODE": "authcode123"}


def fake(responses):
    calls = []

    def http(req):
        calls.append(req)
        path = req.full_url.split("?")[0].rsplit("/", 1)[-1]
        return responses.get(path, (404, {"s": "error", "code": -1, "message": "not found"}))
    return http, calls


GOOD = {
    "validate-authcode": (200, {"s": "ok", "access_token": "TOKEN-XYZ", "refresh_token": "REFRESH-XYZ"}),
    "quotes": (200, {"s": "ok", "d": [{"v": {"lp": 25010.5, "tt": 1793600000}}]}),
    "options-chain-v3": (200, {"s": "ok", "data": {"expiryData": [{"date": "04-11-2026"}], "optionsChain": [
        {"symbol": "NSE:NIFTY50-INDEX", "ltp": 25010.5, "option_type": ""},
        {"symbol": "NSE:NIFTY26N0425000CE", "option_type": "CE", "strike_price": 25000, "bid": 100, "ask": 101,
         "ltp": 100.5, "oi": 5, "volume": 9, "greeks": {"delta": .5, "gamma": .001, "theta": -9, "vega": 10, "iv": 13}},
        {"symbol": "NSE:NIFTY26N0425000PE", "option_type": "PE", "strike_price": 25000, "bid": 90, "ask": 91,
         "ltp": 90.5, "oi": 5, "volume": 9, "greeks": {"delta": -.5, "gamma": .001, "theta": -9, "vega": 10, "iv": 13}}]}}),
    "history": (200, {"s": "ok", "candles": [[1793600000, 1, 2, 1, 2, 10]]}),
    "futures-chain": (200, {"s": "ok", "data": [{"symbol": "NSE:NIFTY50-INDEX", "expiry": ""},
                                                {"symbol": "NSE:NIFTY26NOVFUT", "expiry": "1795000000", "lp": 25050, "volume": 3}]}),
    "depth": (200, {"s": "ok", "d": {"NSE:NIFTY26N0425000CE": {"bids": [{"price": 100, "volume": 650}],
                                                               "ask": [{"price": 101, "volume": 650}]}}}),
}


def run(responses, env=ENV):
    http, calls = fake(responses)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = F.main(env, http)
    return code, buf.getvalue(), calls


class ReadOnly(unittest.TestCase):
    def test_no_order_or_account_calls_in_source(self):
        for bad in ("/orders", "place_order", "/positions", "/funds", "/holdings", "/multi-order",
                    "/gtt", "/profile", "/tradebook", "/convert"):
            self.assertNotIn(bad, SRC)

    def test_only_get_and_the_token_post(self):
        posts = re.findall(r'call\("POST", ([^,]+),', SRC)
        self.assertEqual(posts, ['API + "/validate-authcode"'])

    def test_every_call_goes_to_the_documented_host(self):
        code, out, calls = run(GOOD)
        self.assertTrue(all(c.full_url.startswith("https://api-t1.fyers.in/") for c in calls))
        self.assertEqual([c.get_method() for c in calls].count("POST"), 1)


class Masking(unittest.TestCase):
    def test_secrets_and_tokens_only_appear_in_mask_lines(self):
        code, out, calls = run(GOOD)
        self.assertEqual(code, 0, out)
        for v in ("s3cret", "authcode123", "TOKEN-XYZ", "REFRESH-XYZ"):
            lines = [l for l in out.splitlines() if v in l]
            self.assertEqual(lines, ["::add-mask::%s" % v])
        auth = calls[1].get_header("Authorization")
        self.assertEqual(auth, "APP-100:TOKEN-XYZ")

    def test_missing_secrets_named_not_printed(self):
        code, out, _ = run(GOOD, {"FYERS_APP_ID": "APP-100", "FYERS_SECRET_ID": "", "FYERS_AUTH_CODE": ""})
        self.assertEqual(code, 2)
        self.assertIn("FYERS_SECRET_ID", out)


class Reporting(unittest.TestCase):
    def test_all_work(self):
        code, out, _ = run(GOOD)
        self.assertIn("6 of 6 data checks work", out)
        self.assertIn("best bid 100 x 650", out)

    def test_login_failure_stops_everything(self):
        bad = dict(GOOD, **{"validate-authcode": (400, {"s": "error", "code": -413, "message": "invalid auth code"})})
        code, out, calls = run(bad)
        self.assertEqual(code, 1)
        self.assertIn("login failed", out)
        self.assertEqual(len(calls), 1)

    def test_endpoint_failure_and_missing_greeks_reported(self):
        chain = json.loads(json.dumps(GOOD["options-chain-v3"][1]))
        for r in chain["data"]["optionsChain"][1:]:
            del r["greeks"]
        bad = dict(GOOD, **{"options-chain-v3": (200, chain), "depth": (403, {"s": "error", "code": -99})})
        code, out, _ = run(bad)
        self.assertEqual(code, 1)
        self.assertIn("Greeks missing: delta, gamma, theta, vega, iv", out)
        self.assertRegex(out, r"ATM option depth\s+FAILS")

    def test_missing_bid_is_a_failed_chain(self):
        chain = json.loads(json.dumps(GOOD["options-chain-v3"][1]))
        del chain["data"]["optionsChain"][1]["bid"]
        code, out, _ = run(dict(GOOD, **{"options-chain-v3": (200, chain)}))
        self.assertRegex(out, r"NIFTY option chain\s+FAILS")
        self.assertIn("missing: bid", out)


if __name__ == "__main__":
    unittest.main()
