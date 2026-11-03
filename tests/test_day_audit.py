"""Day verdict: the replay of the day's bars set against what the bots logged."""
import unittest

from nse_bot.web import day_audit as DA


def audit(lots=None, orb_sig=True, sweep_fail="sweep", bars=190, premium=True):
    sig = {"signal_time": "2026-11-03T10:20:00+05:30", "option_type": "CE",
           "entry_window": ["2026-11-03T10:25:00+05:30", "2026-11-03T10:30:00+05:30"],
           "contract": {"symbol": "NSE:NIFTY26N0323650CE"}}
    if premium:
        sig["entry_window_premium"] = {"bar": "2026-11-03T10:25:00+05:30", "open_ltp": 50.9,
                                       "lots_under_rs500_rule": lots,
                                       "sizing": {"reasons": ["1 lot(s): loss at stop Rs 721.96 > risk budget Rs 500.00"]}}
    else:
        sig["entry_window_premium"] = {"status": "NO DATA", "why": "no option bar"}
    return {"day": "2026-11-03", "generated": "2026-11-03T12:25:25+05:30",
            "data": {"index_1m_bars_today": bars, "last_bar": "2026-11-03T12:24:00+05:30"},
            "ORB": {"opening_range": {"high": 23628.3, "low": 23482.8, "ce_trigger_close_above": 23640.11,
                                      "pe_trigger_close_below": 23471.06},
                    "signals": [sig] if orb_sig else []},
            "Sweep": {"signals": [], "day_audit": [{"evaluated_at": "2026-11-03T09:30:00+05:30",
                                                    "skip_reason": "sweep: no liquidity sweep with a reclaim today",
                                                    "rules": {"data_underlying": "pass", sweep_fail: "fail"}}]}}


REJ = {"orb": [{"time": "2026-11-03T11:27:48+05:30", "status": "REJECTED", "contract": "NSE:NIFTY26N0323650CE",
                "reasons": "signal is past its entry window 10:25-10:30 IST"}]}


class Verdict(unittest.TestCase):
    def test_todays_case_risk_rule_blocks_the_only_setup(self):
        v = DA.verdict(audit(lots=0), REJ, {"entries": 0})
        self.assertEqual(v["verdict"], "NO VALID SETUP")
        orb = v["findings"][0]
        self.assertEqual((orb["strategy"], orb["rule"]), ("ORB", "max_risk_per_trade_inr"))
        self.assertIn("721.96", orb["reason"])
        sw = v["findings"][1]
        self.assertEqual((sw["strategy"], sw["rule"], sw["verdict"]), ("Sweep", "sweep", "NO VALID SETUP"))

    def test_executable_setup_rejected_by_the_bot_is_execution_failure(self):
        v = DA.verdict(audit(lots=1), REJ, {"entries": 0})
        self.assertEqual(v["verdict"], "ENTRY EXECUTION FAILURE")
        self.assertIn("past its entry window", v["findings"][0]["reason"])

    def test_executable_setup_the_bot_never_saw_is_engine_failure(self):
        self.assertEqual(DA.verdict(audit(lots=1), {"orb": []}, {"entries": 0})["verdict"], "STRATEGY ENGINE FAILURE")

    def test_traded(self):
        self.assertEqual(DA.verdict(audit(lots=1), REJ, {"entries": 1})["findings"][0]["verdict"], "TRADED")

    def test_missing_bars_or_premium_is_data_failure(self):
        self.assertEqual(DA.verdict(audit(bars=0), {}, {})["verdict"], "DATA FAILURE")
        self.assertEqual(DA.verdict(audit(premium=False), {}, {})["verdict"], "DATA FAILURE")

    def test_no_breakout_and_not_audited(self):
        v = DA.verdict(audit(orb_sig=False), {}, {})
        self.assertEqual(v["verdict"], "NO VALID SETUP")
        self.assertIn("23640.11", v["findings"][0]["reason"])
        self.assertEqual(DA.verdict(None, {}, {})["verdict"], "NOT AUDITED YET")


if __name__ == "__main__":
    unittest.main()
