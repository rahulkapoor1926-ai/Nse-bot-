"""Building blocks for sweep_reversal: each checked against a hand calculation."""

import datetime as dt
import unittest

import sweep_synth as S
from nse_bot import clock, contract_select, structure as st
from test_sweep import MASTER, CFG


def b5(i, o, h, l, c, v=None):
    return (clock.at(S.DAY, "09:15") + dt.timedelta(minutes=5 * i), o, h, l, c, v)


class Structure(unittest.TestCase):
    def test_swing_known_only_after_n_bars(self):
        bars = [b5(0, 10, 11, 9, 10), b5(1, 10, 12, 9, 11), b5(2, 11, 15, 10, 14), b5(3, 14, 14, 12, 13),
                b5(4, 13, 13, 11, 12)]
        [s] = [x for x in st.swings(bars, 2, 5) if x["kind"] == "high"]
        self.assertEqual((s["i"], s["price"]), (2, 15))
        self.assertEqual(s["known_at"], clock.at(S.DAY, "09:40"))          # end of bar 4
        self.assertEqual(st.swings(bars[:4], 2, 5), [])                     # bar 4 not closed yet -> unknown

    def test_equal_levels_tolerance(self):
        sw = [{"kind": "high", "price": p, "t": clock.at(S.DAY, "10:00"), "known_at": clock.at(S.DAY, "10:00")}
              for p in (25000, 25006, 25030)]
        [e] = st.equal_levels(sw, "high", 0.03)                             # 7.5 points
        self.assertEqual(e["price"], 25006)
        self.assertEqual(len(e["members"]), 2)
        self.assertEqual(st.equal_levels(sw, "high", 0.01), [])

    def test_resample_skips_incomplete_buckets(self):
        ones = [(clock.at(S.DAY, "09:15") + dt.timedelta(minutes=i), 10, 11, 9, 10, 5) for i in range(10)]
        self.assertEqual(len(st.resample(ones, 5)), 2)
        self.assertEqual(st.resample(ones, 5)[0][5], 25)
        del ones[7]                                                          # a missing minute
        self.assertEqual(len(st.resample(ones, 5)), 1)
        ones[0] = ones[0][:5] + (None,)
        self.assertIsNone(st.resample(ones, 5)[0][5])

    def test_vwap_hand_calculation(self):
        bars = [b5(0, 10, 12, 8, 10, 100), b5(1, 10, 14, 10, 12, 300)]
        self.assertAlmostEqual(st.vwap(bars), (10 * 100 + 12 * 300) / 400.0)
        self.assertIsNone(st.vwap([b5(0, 10, 12, 8, 10, None)]))

    def test_rsi_extremes_and_short_input(self):
        self.assertEqual(st.rsi(list(range(1, 20)), 14), 100.0)
        self.assertLess(st.rsi(list(range(20, 1, -1)), 14), 1)
        self.assertIsNone(st.rsi([1, 2, 3], 14))

    def test_fvg_min_size_and_order_block(self):
        bars = [b5(0, 100, 101, 99, 100), b5(1, 100, 106, 100, 105), b5(2, 105, 108, 102, 107)]
        [g] = st.fair_value_gaps(bars, 0, 3, "sell", 0.5)
        self.assertEqual((g["bottom"], g["top"]), (101, 102))
        self.assertEqual(st.fair_value_gaps(bars, 0, 3, "sell", 2.0), [])
        bars = [b5(0, 100, 101, 98, 99), b5(1, 99, 100, 97, 98.5), b5(2, 98.5, 106, 98, 105)]
        self.assertEqual(st.order_block(bars, 0, 2, "sell")["i"], 1)


class Contracts(unittest.TestCase):
    def leg(self, **kw):
        d = {"delta": 0.5, "gamma": 0.001, "vega": 10.0, "theta": -8.0}
        d.update(kw)
        return d

    def test_projection_uses_all_four_greeks(self):
        p = contract_select.project(self.leg(), 150.0, -60.0, -2.0, 0.25)
        self.assertAlmostEqual(p, 150 - 30 + 0.5 * 0.001 * 3600 - 20 - 2)
        self.assertGreater(contract_select.project(self.leg(gamma=0.0), 150, -60, 0, 0),
                           contract_select.project(self.leg(gamma=0.0), 150, -60, -2, 0))   # IV drop hurts

    def test_otm_is_never_selected_for_any_spot(self):
        now = S.NOW
        for spot in range(24830, 25200, 7):
            for bias in ("bullish", "bearish"):
                chain = contract_select.parse_chain(S.chain_doc(spot=spot))
                stop, tgt = (spot - 60, spot + 200) if bias == "bullish" else (spot + 60, spot - 200)
                chosen, cands = contract_select.select(chain, MASTER, bias, spot, stop, tgt, now, CFG, S.STRATEGY["params"])
                self.assertTrue(all(c["moneyness"] in ("ATM", "ITM") for c in cands))
                if chosen:
                    self.assertEqual(chosen["option_type"], "CE" if bias == "bullish" else "PE")
                    otm = chosen["strike"] > spot if bias == "bullish" else chosen["strike"] < spot
                    if otm:
                        self.assertEqual(chosen["moneyness"], "ATM")       # only the nearest strike may sit above/below spot
                        self.assertLessEqual(abs(chosen["strike"] - spot), 25)

    def test_stop_is_an_estimate_and_sizing_stop_is_lower(self):
        chain = contract_select.parse_chain(S.chain_doc(spot=25047))
        chosen, _ = contract_select.select(chain, MASTER, "bullish", 25047, 24980, 25250, S.NOW, CFG,
                                           S.STRATEGY["params"])
        self.assertLess(chosen["sizing_stop"], chosen["premium_stop"])
        self.assertLess(chosen["premium_stop"], chosen["entry"])
        self.assertEqual(chosen["entry"], round(chosen["ask"] + CFG["slippage_ticks"] * 0.05, 2))   # buy at the ask + slippage
        plan = contract_select.plan_for(chosen, CFG, S.DAY)
        self.assertIn("ESTIMATES", plan["note"])

    def test_units_converted_from_paise(self):
        chain = contract_select.parse_chain(S.chain_doc(spot=25047))
        leg = [l for l in chain["legs"] if l["strike"] == 25050 and l["option_type"] == "CE"][0]
        self.assertEqual(leg["theta"], -8.0)
        self.assertEqual(leg["vega"], 10.0)
        self.assertEqual(chain["spot"], 25047)
