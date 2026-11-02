"""Options-desk context: real chain fields only, estimates labelled, no direction from OI, never a filter."""

import datetime as dt
import json
import unittest

import test_bot
import test_fyers
from helpers import quote, t
from nse_bot import clock
from nse_bot import options_desk as OD

NOW = clock.at(dt.date(2026, 11, 2), "11:00")
EXPIRY = dt.date(2026, 11, 3)


def leg(typ, k, oi, prev, iv, delta, gamma, theta=-10.0, vega=5.0, ltp=100.0, vol=1000):
    return {"option_type": typ, "strike": float(k), "oi": oi, "prev_oi": prev, "iv": iv, "delta": delta,
            "gamma": gamma, "theta": theta, "vega": vega, "ltp": ltp, "volume": vol}


def chain(spot=25010.0, put_gamma=0.002, call_gamma=0.001, expiry_epoch=None, vix=15.0):
    legs = []
    for i, k in enumerate(range(24800, 25250, 50)):          # 9 strikes, ATM 25000
        legs.append(leg("CE", k, 1000 + 100 * i, 900 + 100 * i, 15 - 0.2 * i, 0.5, call_gamma))
        legs.append(leg("PE", k, 2000 - 100 * i, 1500 - 100 * i, 13 + 0.5 * (8 - i), -0.5, put_gamma))
    legs[-2]["oi"] = 9000                                     # big CE OI at 25200
    return {"spot": spot, "legs": legs, "expiry": EXPIRY.isoformat(), "india_vix": vix,
            "totals": {"call_oi": 1, "put_oi": 2},
            "expiry_epoch": expiry_epoch or int(clock.at(EXPIRY, "15:30").timestamp())}


def vix_hist(n, lo=10.0):
    d0 = NOW.date() - dt.timedelta(days=n + 1)
    return [(d0 + dt.timedelta(days=i), lo + i * 0.02) for i in range(n)]


class Analyse(unittest.TestCase):
    def test_oi_pcr_change_and_concentration(self):
        d = OD.analyse(chain(), NOW)["open_interest"]
        ce = sum(1000 + 100 * i for i in range(8)) + 9000
        pe = sum(2000 - 100 * i for i in range(9))
        self.assertEqual((d["call_oi"], d["put_oi"]), (ce, pe))
        self.assertEqual(d["pcr_oi"], round(pe / ce, 3))
        self.assertEqual(d["put_oi_change"], 9 * 500)
        self.assertEqual(d["concentration"]["CE"][0]["strike"], 25200.0)
        self.assertEqual(d["fyers_chain_totals"], {"call_oi": 1, "put_oi": 2})
        self.assertIn("no direction is inferred", d["note"])

    def test_missing_previous_oi_is_not_available(self):
        c = chain()
        for l in c["legs"]:
            l["prev_oi"] = None
        d = OD.analyse(c, NOW)["open_interest"]
        self.assertIn("NOT AVAILABLE", d["change"])
        self.assertNotIn("call_oi_change", d)

    def test_iv_and_skew(self):
        iv = OD.analyse(chain(), NOW)["implied_volatility"]
        self.assertEqual(iv["atm_strike"], 25000.0)
        self.assertEqual(iv["atm_iv"], round(((15 - 0.8) + (13 + 2.0)) / 2, 2))
        s = iv["skew"]["2 strikes"]                           # put 24900 vs call 25100
        self.assertEqual((s["otm_put_iv"], s["otm_call_iv"]), (13 + 0.5 * 6, 15 - 0.2 * 6))

    def test_iv_percentile_needs_enough_history(self):
        short = OD.analyse(chain(), NOW, vix_hist(150))["implied_volatility"]["iv_percentile"]
        self.assertEqual(short["status"], "NOT AVAILABLE")
        full = OD.analyse(chain(vix=13.0), NOW, vix_hist(252))["implied_volatility"]["iv_percentile"]
        self.assertEqual(full["value"], 59.5)                 # 150 of 252 closes are below 13.0
        self.assertIn("not this option's own IV history", full["basis"])

    def test_greek_exposure_and_gex_estimate(self):
        d = OD.analyse(chain(), NOW)
        ce_oi = d["open_interest"]["call_oi"]
        pe_oi = d["open_interest"]["put_oi"]
        self.assertEqual(d["greek_exposure"]["CE"]["delta_units"], round(0.5 * ce_oi))
        self.assertEqual(d["greek_exposure"]["PE"]["theta_rs_per_day"], round(-10.0 * pe_oi))
        gex = (0.001 * ce_oi - 0.002 * pe_oi) * 25010.0 ** 2 * 0.01
        self.assertAlmostEqual(d["gex"]["rs_per_1pct_move"], round(gex), delta=1)
        self.assertIn("ESTIMATE", d["gex"]["label"])
        self.assertEqual(d["dealer_positioning"]["estimate"], "short gamma")
        self.assertIn("ESTIMATE", d["dealer_positioning"]["label"])
        self.assertIn("says nothing about direction", d["dealer_positioning"]["label"])
        json.dumps(d)

    def test_rounded_gamma_is_counted_and_missing_greeks_not_available(self):
        c = chain()
        c["legs"][0]["gamma"] = 0.0
        self.assertEqual(OD.analyse(c, NOW)["gex"]["legs_with_gamma_rounded_to_zero"], 1)
        c["legs"][1]["delta"] = None
        self.assertEqual(OD.analyse(c, NOW)["gex"]["status"], "NOT AVAILABLE")

    def test_expiry_risk(self):
        r = OD.analyse(chain(), NOW)["expiry_risk"]
        self.assertEqual((r["hours_to_expiry"], r["expiry_day"]), (28.5, False))
        self.assertEqual(r["atm_theta_pct_of_premium_per_day"], 10.0)
        on = OD.analyse(chain(expiry_epoch=int(clock.at(NOW.date(), "15:30").timestamp())), NOW)
        self.assertTrue(on["expiry_risk"]["expiry_day"])


class Context(unittest.TestCase):
    def test_gamma_regime_reads_opposite_for_breakout_and_reversal(self):
        desk = OD.analyse(chain(), NOW)                       # short gamma estimate
        self.assertTrue(OD.context("orb", 1, desk)["supports"])
        self.assertTrue(OD.context("sweep", 1, desk)["cautions"])
        long_g = OD.analyse(chain(put_gamma=0.0005, call_gamma=0.003), NOW)
        self.assertEqual(long_g["dealer_positioning"]["estimate"], "long gamma")
        self.assertEqual(OD.context("sweep", 1, long_g)["verdict"], "SUPPORTIVE")

    def test_expiry_day_and_expensive_iv_are_cautions_oi_is_only_a_note(self):
        c = chain(spot=25190.0, expiry_epoch=int(clock.at(NOW.date(), "15:30").timestamp()), vix=20.0)
        r = OD.context("orb", 1, OD.analyse(c, NOW, vix_hist(252)))
        self.assertIn("expiry day: fast time decay and gamma risk", r["cautions"])
        self.assertTrue(any("India VIX in its top" in x for x in r["cautions"]))
        self.assertTrue(any("largest CE OI at 25200" in n and "not direction" in n for n in r["notes"]))
        self.assertFalse(any("OI" in x for x in r["supports"] + r["cautions"]))
        self.assertIn("never blocks", r["note"])

    def test_no_chain_is_not_available(self):
        self.assertEqual(OD.context("orb", 1, OD.analyse({"spot": None, "legs": []}, NOW))["verdict"], "NOT AVAILABLE")


class FyersChainFields(unittest.TestCase):
    def test_chain_carries_totals_vix_and_expiry_time(self):
        c = test_fyers.chain()
        c["data"].update({"callOi": 200946590, "putOi": 275844985,
                          "indiavixData": {"ltp": 15.56, "symbol": "NSE:INDIAVIX-INDEX"}})
        http = test_fyers.Http(test_fyers.routes(**{"options-chain-v3": (200, c)}))
        doc = test_fyers.client(http).option_chain(test_fyers.IDX, dt.date(2026, 11, 4), enrich_depth=False)
        self.assertEqual(doc["totals"], {"call_oi": 200946590, "put_oi": 275844985})
        self.assertEqual(doc["india_vix"], 15.56)


class Integration(unittest.TestCase):
    approve = test_bot.Cycle.approve
    tearDown = test_bot.Cycle.tearDown
    ctx = test_bot.Cycle.ctx

    def setUp(self):
        test_bot.Cycle.setUp(self)
        self.cfg["max_lots_per_trade"] = 1

    def test_recorded_and_never_blocks(self):
        from nse_bot import bot
        self.approve()
        self.md.q[self.ce.instrument_id] = quote(bid=30, ask=30.5, at=t(9, 41), iid=self.ce.instrument_id)
        cand = bot.run_once(self.ctx(t(9, 41)), t(9, 41))["candidates"][0]
        self.assertIn("verdict", cand["options_desk"])
        self.assertEqual(cand["risk"]["verdict"], "PASS", cand["risk"]["reasons"])


if __name__ == "__main__":
    unittest.main()
