"""End-of-day NSE report: real file layouts (columns copied from the 2026-10-30 files),
fail closed on anything missing, never lists OTM, never simulates a trade."""

import csv
import datetime as dt
import io
import os
import shutil
import tempfile
import unittest
import zipfile

from nse_bot import clock, eod
from nse_bot.errors import DataError

CAL = clock.Calendar.load(os.path.join(os.path.dirname(__file__), "..", "nse_bot", "data", "nse_holidays.json"))
DAY = dt.date(2026, 10, 30)
FO_HEAD = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
           "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
           "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
           "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4").split(",")
IDX_HEAD = ["Index Name", "Index Date", "Open Index Value", "High Index Value", "Low Index Value",
            "Closing Index Value", "Points Change", "Change(%)", "Volume", "Turnover (Rs. Cr.)", "P/E", "P/B",
            "Div Yield"]


def fo_row(day, tp, expiry, strike, opt, close, oi=100000, vol=5000, name=None, spot=23053.1):
    r = dict.fromkeys(FO_HEAD, "")
    r.update({"TradDt": day.isoformat(), "BizDt": day.isoformat(), "Sgmt": "FO", "Src": "NSE",
              "FinInstrmTp": tp, "TckrSymb": "NIFTY", "XpryDt": expiry, "StrkPric": "%.2f" % strike if strike else "",
              "OptnTp": opt, "FinInstrmNm": name or "NIFTY%s%s%s" % (expiry, int(strike or 0), opt or "FUT"),
              "OpnPric": close, "HghPric": close, "LwPric": close, "ClsPric": close, "SttlmPric": close,
              "UndrlygPric": spot, "OpnIntrst": oi, "ChngInOpnIntrst": 10, "TtlTradgVol": vol,
              "TtlNbOfTxsExctd": 7, "NewBrdLotQty": 65})
    return r


def fo_zip(rows):
    s = io.StringIO()
    w = csv.DictWriter(s, FO_HEAD)
    w.writeheader()
    w.writerows(rows)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("BhavCopy.csv", s.getvalue())
    return b.getvalue()


def default_fo(day=DAY, futures=True):
    rows = []
    for k in range(22800, 23350, 50):
        rows.append(fo_row(day, "IDO", "2026-11-03", k, "CE", max(1.0, 23053.1 - k + 60), oi=200000 if k == 23200 else 1000))
        rows.append(fo_row(day, "IDO", "2026-11-03", k, "PE", max(1.0, k - 23053.1 + 60), oi=300000 if k == 22900 else 2000))
        rows.append(fo_row(day, "IDO", "2026-10-30", k, "CE", 5))     # expiring today: never the "nearest" expiry
    if futures:
        rows.append(fo_row(day, "IDF", "2026-11-24", None, "", 23120.0, name="NIFTY26NOVFUT"))
    rows.append(fo_row(day, "STO", "2026-11-24", 100, "CE", 2, name="OTHER"))   # another instrument type
    return fo_zip(rows)


def idx_csv(day, o, h, l, c):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(IDX_HEAD)
    w.writerow(["Nifty Bank", day.strftime("%d-%m-%Y"), 1, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0])
    w.writerow(["Nifty 50", day.strftime("%d-%m-%Y"), o, h, l, c, 0, 0, 0, 0, 0, 0, 0])
    return s.getvalue().encode()


class FakeHttp(object):
    def __init__(self, fo=None, bars=None, missing=()):
        self.fo = fo if fo is not None else default_fo()
        self.bars, self.missing, self.calls = bars or {}, set(missing), []

    def __call__(self, url):
        self.calls.append(url)
        if url == eod.fo_url(DAY):
            return (200, self.fo) if self.fo else (404, b"")
        for d, bar in self.bars.items():
            if url == eod.index_url(d):
                return (404, b"") if d in self.missing else (200, idx_csv(d, *bar))
        return 404, b""


def uptrend(n=40, last=None):
    """n daily bars ending at DAY, rising 20 points a day; `last` overrides the final bar."""
    days, d = [], DAY
    while len(days) < n:
        days.append(d)
        d = CAL.previous_trading_day(d)
    days.reverse()
    bars = {d: (22200 + 20 * i, 22230 + 20 * i, 22190 + 20 * i, 22220 + 20 * i) for i, d in enumerate(days)}
    if last:
        bars[DAY] = last
    return bars


class Report(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def build(self, http, sessions=40):
        return eod.build(DAY, CAL, self.dir, sessions, http)

    def test_full_report_from_real_layout(self):
        bars = uptrend()
        prev = bars[CAL.previous_trading_day(DAY)]
        # swept the previous low, closed back above it
        bars[DAY] = (prev[3] + 5, 23100.0, prev[2] - 30, 23053.1)
        r = self.build(FakeHttp(bars=bars))
        self.assertEqual(r["trades_simulated"], 0)
        self.assertIn("bid/ask quotes", r["missing_fields"])
        self.assertIn("Greeks (delta, gamma, theta, vega)", r["missing_fields"])
        self.assertEqual(r["expiry"], "2026-11-03")                # the 2026-10-30 expiry is skipped
        self.assertEqual(r["atm_strike"], 23050.0)
        self.assertEqual(r["trend"]["status"], "up")
        self.assertTrue(r["daily_sweep"]["found"])
        self.assertEqual(r["daily_sweep"]["sweeps"][0]["side"], "bullish")
        self.assertEqual(r["daily_sweep"]["evidence"]["day"]["low"], prev[2] - 30)
        self.assertEqual(r["open_interest"]["max_ce_oi_strike"], 23200.0)
        self.assertEqual(r["open_interest"]["max_pe_oi_strike"], 22900.0)
        self.assertEqual(r["future"]["status"], "ok")
        self.assertEqual(r["future"]["basis"], round(23120.0 - 23053.1, 2))
        self.assertTrue(any("agrees with" in s for s in r["summary"]))
        self.assertEqual(len(r["sources"]["fo"]["sha256"]), 64)

    def test_never_lists_otm(self):
        r = self.build(FakeHttp(bars=uptrend()))
        spot = r["index"]["close"]
        self.assertEqual(len(r["contracts"]), 6)
        for c in r["contracts"]:
            if c["moneyness"] == "ITM":
                self.assertTrue(c["strike"] < spot if c["option_type"] == "CE" else c["strike"] > spot, c)
            else:
                self.assertEqual(c["strike"], r["atm_strike"])
        ce = [c["strike"] for c in r["contracts"] if c["option_type"] == "CE"]
        self.assertTrue(all(k <= r["atm_strike"] for k in ce))

    def test_no_sweep_and_trend_against(self):
        bars = uptrend()
        prev = bars[CAL.previous_trading_day(DAY)]
        bars[DAY] = (prev[3], prev[1] + 40, prev[3] - 5, prev[1] + 30)       # broke above and held: not a sweep
        r = self.build(FakeHttp(bars=bars))
        self.assertFalse(r["daily_sweep"]["found"])
        self.assertIn("Daily sweep: none.", r["summary"])

    def test_short_history_says_insufficient(self):
        r = self.build(FakeHttp(bars=uptrend(10)), sessions=10)
        self.assertEqual(r["trend"]["status"], "insufficient history")

    def test_missing_history_day_is_listed_not_filled(self):
        bars = uptrend()
        gap = sorted(bars)[5]
        r = self.build(FakeHttp(bars=bars, missing={gap}))
        self.assertEqual([g["date"] for g in r["history_gaps"]], [gap.isoformat()])
        self.assertEqual(r["history_sessions"], 39)

    def test_previous_day_missing_means_no_sweep_claim(self):
        bars = uptrend()
        r = self.build(FakeHttp(bars=bars, missing={CAL.previous_trading_day(DAY)}))
        self.assertFalse(r["daily_sweep"]["found"])
        self.assertIn("unavailable", r["daily_sweep"]["note"])

    def test_no_futures_rows_reported_missing(self):
        r = self.build(FakeHttp(fo=default_fo(futures=False), bars=uptrend()))
        self.assertEqual(r["future"]["status"], "missing")

    def test_markdown_and_files(self):
        r = self.build(FakeHttp(bars=uptrend()))
        path = eod.write(r, os.path.join(self.dir, "out"))
        text = open(path).read()
        self.assertIn("no trades were simulated", text)
        self.assertIn("OTM never listed", text)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "out", "latest.json")))

    def test_downloads_are_cached(self):
        http = FakeHttp(bars=uptrend())
        self.build(http)
        n = len(http.calls)
        self.build(http)
        self.assertEqual(len(http.calls), n)


class FailClosed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_fo_not_published(self):
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, FakeHttp(fo=b"", bars=uptrend()))

    def test_today_index_missing(self):
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, FakeHttp(bars=uptrend(), missing={DAY}))

    def test_wrong_trade_date_in_fo_file(self):
        fo = fo_zip([fo_row(CAL.previous_trading_day(DAY), "IDO", "2026-11-03", 23050, "CE", 100)])
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, FakeHttp(fo=fo, bars=uptrend()))

    def test_missing_columns_and_bad_zip(self):
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, FakeHttp(fo=b"not a zip", bars=uptrend()))
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("x.csv", "TradDt,TckrSymb\n2026-10-30,NIFTY\n")
        shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, FakeHttp(fo=b.getvalue(), bars=uptrend()))

    def test_inconsistent_index_bar(self):
        bars = uptrend()
        bars[DAY] = (23000, 22900, 23100, 23050)          # high below low
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, FakeHttp(bars=bars))

    def test_non_trading_day(self):
        with self.assertRaises(DataError):
            eod.build(dt.date(2026, 11, 1), CAL, self.dir, 40, FakeHttp())

    def test_network_failure_is_a_data_error(self):
        def boom(url):
            raise DataError("could not reach %s" % url)
        with self.assertRaises(DataError):
            eod.build(DAY, CAL, self.dir, 40, boom)


if __name__ == "__main__":
    unittest.main()
