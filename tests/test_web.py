"""Web dashboard (Flask) and dashboard controls: protected APIs, real data only, controls that can only
block new entries, no secret in any response."""

import csv
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import synth
from helpers import t
from nse_bot import control, risk, state

try:
    import flask  # noqa: F401
    from nse_bot.web import app as webapp
    from nse_bot.web.data import RECORD_FILE
    HAVE_FLASK = True
except ImportError:                                     # the trading bot itself needs no third-party code
    HAVE_FLASK = False

DAY = dt.date(2026, 10, 29)
KEY = "k" * 32
NOW = lambda: t(10, 5)                                  # noqa: E731


def write_recording(root):
    from nse_bot.data import fyers_recorder as FR
    d = os.path.join(root, DAY.isoformat())
    os.makedirs(d)
    rows = [dict(ts=t(10, 4).isoformat(), received_at=t(10, 4, 3).isoformat(), underlying="NIFTY",
                 symbol="NSE:NIFTY50-INDEX", kind="index", ltp="25030.5", status="OK", source="fyers")]
    for typ, mny, k in (("CE", "ATM", 25050), ("CE", "ITM1", 25000), ("PE", "ATM", 25050)):
        rows.append(dict(ts=t(10, 4).isoformat(), received_at=t(10, 4, 3).isoformat(), underlying="NIFTY",
                         symbol="NSE:NIFTY26N03%d%s" % (k, typ), kind="option", expiry="2026-11-03", strike=str(k),
                         option_type=typ, moneyness=mny, atm_strike="25050", bid="30.1", ask="30.4", ltp="30.2",
                         volume="1000", oi="5000", status="OK" if typ == "CE" else "STALE", source="fyers"))
    with open(os.path.join(d, RECORD_FILE), "w", newline="") as f:
        w = csv.DictWriter(f, FR.COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FR.COLUMNS})


@unittest.skipUnless(HAVE_FLASK, "flask not installed (pip install -r requirements-dashboard.txt)")
class Web(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, "ps")
        os.makedirs(self.root)
        self.ctl = os.path.join(self.dir, "ctl")
        os.makedirs(self.ctl)
        self.rec = os.path.join(self.dir, "rec")
        write_recording(self.rec)
        self.published, self.gh = [], []
        cfg = os.path.join(synth.ROOT, "config", "paper.example.json")
        self.env = {"DASH_KEY": KEY, "DASH_STATE_ROOT": self.root, "DASH_CONTROL_DIR": self.ctl,
                    "DASH_RECORDINGS": self.rec, "DASH_CONFIG": cfg, "DASH_INSECURE": "1",
                    "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "ghs_SECRETTOKEN123",
                    "FYERS_ACCESS_TOKEN": "FYERS-SECRET-XYZ", "TELEGRAM_BOT_TOKEN": "TG-SECRET-ABC"}

        def publish(doc):
            state.write_json(os.path.join(self.ctl, control.FILE), doc)
            self.published.append(doc)
            return True

        def github(method, path, token, body=None):
            self.gh.append((method, path, body))
            if method == "GET":
                return 200, {"total_count": 0, "workflow_runs": []}
            return 204, None
        self.app = webapp.create_app(self.env, publish=publish, github=github, now=NOW)
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def login(self):
        r = self.c.get("/login?k=" + KEY)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["Location"], "/")            # key dropped from the address bar
        return self.c.get("/api/session").get_json()["csrf"]

    def test_everything_needs_the_key(self):
        for path in ("/", "/api/overview", "/api/signals", "/api/trades", "/api/logs", "/api/reports", "/api/market"):
            self.assertEqual(self.c.get(path).status_code, 401, path)
        self.assertEqual(self.c.post("/api/control/pause").status_code, 401)
        self.assertEqual(self.c.get("/login?k=wrong").status_code, 401)
        self.assertEqual(self.c.get("/healthz").get_json()["live_orders_enabled"], False)

    def test_controls_need_csrf(self):
        self.login()
        self.assertEqual(self.c.post("/api/control/pause").status_code, 403)
        self.assertEqual(self.published, [])

    def test_security_headers(self):
        r = self.c.get("/healthz")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_market_is_the_recorded_snapshot_only(self):
        self.login()
        m = self.c.get("/api/market").get_json()
        self.assertEqual((m["spot"], m["snapshot_time"], m["atm_strike"]), (25030.5, t(10, 4).isoformat(), 25050.0))
        self.assertEqual([o["status"] for o in m["options"]], ["OK", "OK", "STALE"])
        shutil.rmtree(self.rec)
        m = self.c.get("/api/market").get_json()
        self.assertFalse(m["available"])
        self.assertNotIn("spot", m)                              # no number at all, never a 0

    def test_overview_shows_the_shared_risk_book_and_real_limits(self):
        cfg = synth.cfg(os.path.join(self.root), risk_state_dir=os.path.join(self.root, "shared-risk"),
                        max_daily_loss_inr=1000, max_trades_per_day=5)
        book = risk.open_for(cfg, DAY)
        book.record_entry({"instrument_id": 7, "strategy": "orb15", "signal_known_at": "a", "side": "BUY", "kind": "index_options", "underlying": "NIFTY"}, t(9, 50), 1)
        book.record_exit(7, -120.0, cfg, t(10, 0), reason="stop")
        self.login()
        j = self.c.get("/api/overview?mode=standard").get_json()
        r = j["risk"]
        self.assertEqual((r["entries"], r["stop_exits"], r["realized_pnl"], r["remaining_loss_inr"]), (1, 1, -120.0, 880.0))
        self.assertEqual((r["limits"]["max_trades_per_day"], r["limits"]["max_daily_loss_inr"],
                          r["limits"]["max_risk_per_trade_inr"], r["limits"]["capital_inr"]), (5, 1000, 500, 20000))
        self.assertFalse(r["limits"]["live_orders_enabled"])
        self.assertTrue(j["health"]["fyers"]["connected"])

    def test_pause_resume_and_halt_publish_controls(self):
        csrf = self.login()
        h = {"X-CSRF-Token": csrf}
        self.assertTrue(self.c.post("/api/control/pause", headers=h).get_json()["ok"])
        self.assertTrue(self.published[-1]["entries_paused"])
        self.c.post("/api/control/resume", headers=h)
        self.assertFalse(self.published[-1]["entries_paused"])
        self.assertEqual(self.c.post("/api/control/halt", headers=h, json={}).status_code, 400)   # needs confirmation
        self.c.post("/api/control/halt", headers=h, json={"confirm": "HALT"})
        self.assertEqual(self.published[-1]["halt_day"], DAY.isoformat())
        r = self.c.post("/api/control/resume", headers=h)
        self.assertEqual(r.status_code, 409)                     # a halt cannot be undone the same day

    def test_start_dispatches_paper_day_only(self):
        csrf = self.login()
        j = self.c.post("/api/control/start", headers={"X-CSRF-Token": csrf}, json={"book": "forward"}).get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(self.gh[-1], ("POST", "/repos/o/r/actions/workflows/paper-day.yml/dispatches",
                                       {"ref": "main", "inputs": {"book": "forward"}}))

    def test_no_secret_in_any_response(self):
        csrf = self.login()
        bodies = [self.c.get(p).get_data(as_text=True) for p in
                  ("/", "/static/app.js", "/api/session", "/api/overview", "/api/signals", "/api/trades",
                   "/api/performance", "/api/logs", "/api/reports", "/api/market")]
        bodies.append(self.c.post("/api/control/pause", headers={"X-CSRF-Token": csrf}).get_data(as_text=True))
        for b in bodies:
            for secret in (KEY, "ghs_SECRETTOKEN123", "FYERS-SECRET-XYZ", "TG-SECRET-ABC"):
                self.assertNotIn(secret, b)

    def test_web_code_has_no_order_path(self):
        here = os.path.join(synth.ROOT, "nse_bot", "web")
        for name in ("app.py", "data.py", os.path.join("static", "app.js")):
            src = open(os.path.join(here, name)).read().lower()
            for bad in ("place_order", "/orders", "broker.buy", "broker.sell", "live_orders_enabled\": true"):
                self.assertNotIn(bad, src, name)

    def test_study_tab_serves_the_real_study_file_and_never_a_pnl(self):
        study = os.path.join(self.dir, "study.json")
        bars = [["2026-10-29T09:15:00+05:30", 25000, 25010, 24990, 25005]]
        sig = {"family": "ORB", "day": "2026-10-29", "signal_time": "2026-10-29T09:45:00+05:30",
               "option_type": "CE", "target_strike": 25000, "trigger_close": 25004}
        state.write_json(study, {"generated": "x", "note": "No option premium, fill or P&L", "coverage": {},
                                 "ORB": {"signals": [sig], "summary": {"signals": 1}},
                                 "Sweep": {"signals": []}, "charts": {"2026-10-29": bars}})
        self.app = webapp.create_app(dict(self.env, DASH_STUDY=study), publish=lambda d: True,
                                     github=lambda *a, **k: (204, None), now=NOW)
        self.c = self.app.test_client()
        self.assertEqual(self.c.get("/api/study").status_code, 401)
        self.login()
        j = self.c.get("/api/study").get_json()
        self.assertTrue(j["available"])
        self.assertEqual(j["chart_days"], ["2026-10-29"])
        self.assertEqual(j["options_pnl"]["status"], "NOT AVAILABLE")   # no option P&L without real bid/ask
        for fam in ("ORB", "Sweep"):
            for sig in j[fam]["signals"]:
                self.assertFalse({"pnl", "net_pnl", "premium", "fill"} & set(sig))
        ch = self.c.get("/api/study/chart?day=2026-10-29").get_json()
        self.assertEqual((ch["bars"], len(ch["signals"])), (bars, 1))
        self.assertFalse(self.c.get("/api/study/chart?day=2026-10-30").get_json()["available"])
        self.assertEqual(self.c.get("/api/study/chart?day=../../x").status_code, 400)
        self.assertEqual(self.c.get("/api/study/trades.csv").status_code, 404)    # no outcomes in this file

    def test_trade_csv_download_needs_sign_in(self):
        from nse_bot.backtest import signal_study as SS
        study = os.path.join(self.dir, "study.json")
        rows = [{"day": "2026-01-02", "signal_time": "2026-01-02T09:40:00+05:30", "outcome": "SL", "r_multiple": -1.0,
                 "entry_time": "2026-01-02T09:45:00+05:30", "exit_time": "2026-01-02T09:50:00+05:30"}]
        cfg = synth.cfg(self.dir, capital_inr=20000, max_risk_per_trade_inr=500, max_trades_per_day=5,
                        max_stop_exits_per_day=2, max_daily_loss_inr=1000, daily_profit_stop_inr=2000, max_open_lots=1)
        state.write_json(study, {"ORB_index_outcomes": {"rows": rows, "portfolio": SS.portfolio(rows, cfg)},
                                 "ORB": {"signals": []}, "Sweep": {"signals": []}, "charts": {}})
        app = webapp.create_app(dict(self.env, DASH_STUDY=study), publish=lambda d: True,
                                github=lambda *a, **k: (204, None), now=NOW)
        c = app.test_client()
        self.assertEqual(c.get("/api/study/trades.csv").status_code, 401)
        c.get("/login?k=" + KEY)
        r = c.get("/api/study/trades.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("NOT OPTION P&L", r.get_data(as_text=True))

    def test_no_study_file_says_so(self):
        self.login()
        j = self.c.get("/api/study").get_json()
        self.assertFalse(j["available"])
        self.assertIn("signal-study", j["reason"])


class Controls(unittest.TestCase):
    """The paper loop side: controls only ever block new entries."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = synth.cfg(self.dir)
        self.cal = synth.calendar()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, book):
        o = {"instrument_id": 9, "strategy": "orb15", "signal_known_at": "z", "side": "BUY", "kind": "index_options", "underlying": "NIFTY"}
        return book.check(o, self.cfg, self.cal, t(10, 0), "APPROVED", [], {"lots": 1, "loss_at_stop_inr": 300.0},
                          {"reward_risk": 2.0, "net": {"net_reward_risk": 2.1}})

    def test_pause_blocks_entries_and_resume_allows(self):
        doc = {"entries_paused": True}
        p = control.Poller(lambda: doc)
        book = risk.RiskBook.open(self.dir, DAY)
        p.apply(book, t(10, 0))
        self.assertIn("new entries paused from the dashboard", self.check(book)["reasons"])
        self.assertFalse(book.locked)                            # reversible: not a day lockout
        doc["entries_paused"] = False
        p.apply(book, t(10, 1))
        self.assertEqual(self.check(book)["verdict"], "PASS")

    def test_emergency_halt_locks_the_day_but_exits_still_record(self):
        book = risk.RiskBook.open(self.dir, DAY)
        book.record_entry({"instrument_id": 5, "strategy": "orb15", "signal_known_at": "a", "side": "BUY", "kind": "index_options", "underlying": "NIFTY"}, t(9, 50))
        control.Poller(lambda: {"halt_day": DAY.isoformat()}).apply(book, t(10, 0))
        again = risk.RiskBook.open(self.dir, DAY)
        self.assertTrue(again.locked)
        self.assertIn("EMERGENCY HALT", again.doc["lockout"]["reason"])
        again.record_exit(5, 40.0, self.cfg, t(10, 30), reason="target")      # the open trade is still managed
        self.assertEqual((again.doc["open"], again.doc["realized_pnl"]), ([], 40.0))
        tomorrow = risk.RiskBook.open(self.dir, dt.date(2026, 10, 30))
        control.Poller(lambda: {"halt_day": DAY.isoformat()}).apply(tomorrow, t(10, 0, day=dt.date(2026, 10, 30)))
        self.assertFalse(tomorrow.locked)                        # a halt is for its own day only

    def test_unreadable_controls_fail_closed_after_three_cycles(self):
        def boom():
            raise OSError("network down")
        p = control.Poller(boom)
        book = risk.RiskBook.open(self.dir, DAY)
        p.apply(book, t(10, 0))
        p.apply(book, t(10, 1))
        self.assertIsNone(book.paused)
        p.apply(book, t(10, 2))
        self.assertIn("controls unreadable 3 cycles in a row", book.paused)
        self.assertEqual(self.check(book)["verdict"], "BLOCK")

    def test_no_controls_file_means_no_controls(self):
        p = control.Poller(lambda: None)
        book = risk.RiskBook.open(self.dir, DAY)
        self.assertTrue(p.apply(book, t(10, 0))["read_ok"])
        self.assertIsNone(book.paused)

    def test_from_env(self):
        path = os.path.join(self.dir, "c.json")
        with open(path, "w") as f:
            json.dump({"entries_paused": True}, f)
        self.assertTrue(control.Poller.from_env({"BOT_CONTROL_FILE": path}).get()["entries_paused"])
        self.assertIsNone(control.Poller.from_env({}))


if __name__ == "__main__":
    unittest.main()
