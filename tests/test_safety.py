"""Acceptance 8: no live order placement is possible in this phase."""

import os
import re
import unittest

import synth
from nse_bot.broker import sahi

PKG = os.path.join(synth.ROOT, "nse_bot")


def sources():
    for dirpath, _, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                yield os.path.relpath(path, PKG), open(path).read()


class NoLiveOrders(unittest.TestCase):
    def test_no_order_endpoint_in_code(self):
        for name, src in sources():
            for bad in ("/v1/orders", "/v1/margins", "orders.write", "place_order", "place_stock_order"):
                self.assertNotIn(bad, src, "%s mentions %s" % (name, bad))

    def test_sahi_client_only_reads(self):
        self.assertTrue(all(p.startswith("/v1/marketdata/") for p in sahi.READ_PATHS))
        self.assertEqual(sahi.SCOPE, "marketdata.read")

    def test_no_secret_literals(self):
        for name, src in sources():
            self.assertIsNone(re.search(r"(secret|token|password)\s*=\s*[\"'][A-Za-z0-9]{12,}", src, re.I), name)

    def test_example_config_has_no_secret_fields(self):
        txt = open(os.path.join(synth.ROOT, "config", "paper.example.json")).read().lower()
        for w in ("secret", "password", "token", "totp", "api_key"):
            self.assertNotIn(w, txt)

    def test_gitignore_keeps_state_and_secrets_out(self):
        gi = open(os.path.join(synth.ROOT, ".gitignore")).read()
        for p in ("state/", ".env", "config/paper.json"):
            self.assertIn(p, gi)
