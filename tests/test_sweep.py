"""sweep_reversal: the full bullish scenario passes; breaking any one rule produces SKIP with that rule."""

import copy
import datetime as dt
import unittest

import sweep_synth as S
import synth
from nse_bot import clock, contract_select, decisions, instruments, sweep
from test_bot import INDEX_ROW
from test_instruments import HEADER, chain_rows

MASTER = instruments.parse(HEADER + "\n" + INDEX_ROW + "\n" + chain_rows(), index_names=("Nifty 50",))
CFG = synth.cfg("/tmp/unused-state")


def run(d=None, chain=None, strategy=None, master=MASTER):
    d = d or S.build()
    chain = chain if chain is not None else S.chain_doc()
    return sweep.evaluate(strategy or S.STRATEGY, "NIFTY", d["now"], d["u1"], d["u1h"], d["u1d"], d["f1"],
                          lambda: contract_select.parse_chain(chain), master, CFG)


def rule(r, name):
    return next(x for x in r["rules"] if x["rule"] == name)


def bars5(changes):
    b = list(S.TODAY_5M)
    for i, v in changes.items():
        b[i] = v
    return b


class FullSetup(unittest.TestCase):
    def test_bullish_setup_passes_every_rule(self):
        r = run()
        self.assertEqual(r["decision"], "SETUP", r["skip_reason"])
        self.assertTrue(all(x["status"] == "pass" for x in r["rules"]))
        self.assertEqual(r["bias"], "bullish")
        self.assertEqual(r["liquidity"]["level_type"], "PDL")
        self.assertEqual(r["liquidity"]["sweep_extreme"], 24985)
        self.assertEqual(r["confirmation_candle"]["t"], clock.at(S.DAY, "10:10").isoformat())
        self.assertEqual(r["zone"]["kind"], "FVG")
        self.assertEqual((r["zone"]["bottom"], r["zone"]["top"]), (25012, 25030))
        self.assertEqual(r["contract"]["option_type"], "CE")
        self.assertIn(r["contract"]["moneyness"], ("ATM", "ITM"))
        self.assertEqual(r["underlying_plan"]["target_type"], "PDH")
        self.assertIn("swept", r["why"])

    def test_report_evidence_matches_the_data(self):
        d = S.build()
        r = run(d)
        src = decisions.series_from(d["u1"], d["u1h"], d["u1d"], d["f1"])
        self.assertEqual(decisions.verify(r, src), [])
        self.assertGreaterEqual(len(r["evidence"]), 8)

    def test_fabricated_candle_is_caught(self):
        d = S.build()
        r = run(d)
        src = decisions.series_from(d["u1"], d["u1h"], d["u1d"], d["f1"])
        bad = copy.deepcopy(r)
        bad["evidence"][0]["l"] = 24900.0                    # claim a deeper sweep than the data shows
        decisions.enforce(bad, src)
        self.assertEqual(bad["decision"], "SKIP")
        self.assertIn("evidence mismatch", bad["skip_reason"])
        fake = copy.deepcopy(r)
        fake["evidence"].append({"series": "5m", "t": clock.at(S.DAY, "11:00").isoformat(),
                                 "o": 1, "h": 1, "l": 1, "c": 1})
        decisions.enforce(fake, src)
        self.assertEqual(fake["decision"], "SKIP")

    def test_no_look_ahead_future_bars_are_ignored(self):
        d = S.build()
        later = S.build(today_5m=S.TODAY_5M + [(25084, 25090, 24900, 24910)] * 3)   # crash after now
        self.assertEqual(run(d)["decision"], run(later)["decision"])
        self.assertEqual(run(later)["contract"]["strike"], run(d)["contract"]["strike"])

    def test_bearish_mirror_selects_a_put(self):
        m = 50000.0

        def flip(bars):
            return [(b[0], m - b[1], m - b[3], m - b[2], m - b[4]) + tuple(b[5:]) for b in bars]
        d = S.build()
        d2 = {k: (flip(v) if isinstance(v, list) else v) for k, v in d.items()}
        spot = m - 25056.5
        r = run(d2, chain=S.chain_doc(spot=spot))
        self.assertEqual(r["decision"], "SETUP", r["skip_reason"])
        self.assertEqual(r["bias"], "bearish")
        self.assertEqual(r["liquidity"]["level_type"], "PDH")
        self.assertEqual(r["contract"]["option_type"], "PE")


class EachRuleFails(unittest.TestCase):
    def assertSkip(self, r, name):
        self.assertEqual(r["decision"], "SKIP")
        self.assertEqual(rule(r, name)["status"], "fail", r["skip_reason"])
        self.assertTrue(r["skip_reason"].startswith(name))
        after = [x for x in r["rules"] if x["status"] == "not_reached"]
        self.assertTrue(all(x["detail"] is None for x in after))

    def test_no_previous_session(self):
        d = S.build()
        d["u1"] = [b for b in d["u1"] if b[0].date() == S.DAY]
        self.assertSkip(run(d), "data_underlying")

    def test_future_missing_or_bad_fails_closed(self):
        self.assertSkip(run(S.build(fut=False)), "data_future")
        d = S.build()
        d["f1"] = [b[:5] + (None,) if b[0].date() == S.DAY else b for b in d["f1"]]
        self.assertSkip(run(d), "data_future")
        d = S.build()
        d["f1"] = [b for b in d["f1"] if not (b[0].date() == S.DAY and dt.time(9, 40) <= b[0].time() < dt.time(9, 50))]
        self.assertSkip(run(d), "data_future")

    def test_wick_too_small_is_not_a_sweep(self):
        r = run(S.build(today_5m=bars5({9: (25018, 25020, 24997, 25003), 10: (25003, 25012, 24998, 25010)})))
        self.assertSkip(r, "sweep")

    def test_accepted_break_is_not_a_sweep(self):
        r = run(S.build(today_5m=bars5({10: (24995, 24999, 24980, 24990), 11: (24990, 24998, 24970, 24975)})))
        self.assertSkip(r, "sweep")
        self.assertIn("accepted", r["skip_reason"])

    def test_no_confirmation_candle(self):
        r = run(S.build(today_5m=bars5({11: (25010, 25018, 25005, 25012), 12: (25012, 25019, 25006, 25015),
                                        13: (25015, 25019, 25008, 25010)})))
        self.assertSkip(r, "confirmation")

    def test_confirmation_without_structure_break(self):
        r = run(S.build(today_5m=bars5({11: (25010, 25048, 25008, 25045)})))
        self.assertSkip(r, "structure")

    def test_no_fvg_and_no_order_block(self):
        r = run(S.build(today_5m=bars5({9: (24990, 25020, 24985, 24995), 12: (25062, 25084, 25010, 25080)})))
        self.assertSkip(r, "zone")

    def test_future_below_vwap(self):
        def push(env):
            f1 = env["f1"]
            for i, b in enumerate(f1):
                if b[0].date() == S.DAY and b[0].time() < dt.time(9, 30):
                    f1[i] = (b[0], b[1] + 60, b[2] + 60, b[3] + 60, b[4] + 60, 60000)
        self.assertSkip(run(S.build(extra=push)), "vwap")

    def test_no_volume_expansion(self):
        self.assertSkip(run(S.build(vol_mult=1)), "volume_expansion")

    def test_relative_volume_needs_ten_sessions(self):
        self.assertSkip(run(S.build(sessions=5)), "relative_volume")

    def test_momentum(self):
        r = run(S.build(today_5m=bars5({11: (25010, 25060, 25008, 25056), 12: (25056, 25070, 25030, 25062),
                                        13: (25062, 25070, 25025, 25065)})))
        self.assertSkip(r, "momentum")

    def test_daily_trend_against(self):
        d = S.build()
        d["u1d"] = [(b[0], 26000 - (b[1] - 24000), 26000 - (b[3] - 24000), 26000 - (b[2] - 24000),
                     26000 - (b[4] - 24000), None) for b in d["u1d"]]
        self.assertSkip(run(d), "tf_daily")

    def test_hourly_trend_against(self):
        d = S.build()
        d["u1h"] = [(b[0], 50000 - b[1], 50000 - b[3], 50000 - b[2], 50000 - b[4], None) for b in d["u1h"]]
        self.assertSkip(run(d), "tf_1h")

    def test_15m_new_low_after_sweep(self):
        self.assertSkip(run(S.build(today_5m=bars5({11: (25010, 25066, 24980, 25062)}))), "tf_15m")

    def test_entry_waits_then_expires(self):
        r = run(S.build(now=clock.at(S.DAY, "10:20") + dt.timedelta(seconds=30)))
        self.assertSkip(r, "entry_1m")
        self.assertIn("waiting", r["skip_reason"])
        r = run(S.build(now=clock.at(S.DAY, "10:30")))
        self.assertSkip(r, "entry_1m")

    def test_contract_greeks_missing_are_not_estimated(self):
        r = run(chain=S.chain_doc(missing=("delta",)))
        self.assertSkip(r, "contract")
        self.assertTrue(all("delta missing" in c["verdict"] for c in r["contract_candidates"]))

    def test_contract_wide_spread_and_missing_quote(self):
        self.assertSkip(run(chain=S.chain_doc(spread_paise=900)), "contract")
        self.assertSkip(run(chain=S.chain_doc(missing=("top_bid_price",))), "contract")

    def test_iv_shock_can_make_reward_risk_fail(self):
        s = copy.deepcopy(S.STRATEGY)
        s["params"]["iv_shock_points"] = 8
        r = run(strategy=s)
        self.assertSkip(r, "contract")
        self.assertTrue(any("reward:risk" in c["verdict"] for c in r["contract_candidates"]))

    def test_chain_failure_is_a_skip(self):
        d = S.build()

        def boom():
            raise RuntimeError("HTTP 503")
        r = sweep.evaluate(S.STRATEGY, "NIFTY", d["now"], d["u1"], d["u1h"], d["u1d"], d["f1"], boom, MASTER, CFG)
        self.assertSkip(r, "contract")


class Strategy(unittest.TestCase):
    def test_validate(self):
        sweep.validate(S.STRATEGY)
        bad = copy.deepcopy(S.STRATEGY)
        del bad["params"]["rsi_n"]
        with self.assertRaises(Exception):
            sweep.validate(bad)
        bad = copy.deepcopy(S.STRATEGY)
        bad["instrument"] = "equity"
        with self.assertRaises(Exception):
            sweep.validate(bad)


if __name__ == "__main__":
    unittest.main()
