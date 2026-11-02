"""Daily NIFTY end-of-day report from NSE's free public files. No login, no trades.

Sources (verified reachable from GitHub Actions on 2026-11-02, run 41391300516):
  F&O bhavcopy  https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
  index close   https://nsearchives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv

These files carry daily open/high/low/close, settlement, OI, volume and trade counts.
They do NOT carry bid/ask, IV, Greeks or intraday candles, so this module never
simulates a fill and never estimates those fields: the report lists them as missing.

Raw files are kept privately (state/eod-raw, Actions cache). The report keeps only
the handful of rows it analyses plus derived figures.
"""

import csv
import datetime as dt
import hashlib
import io
import os
import urllib.error
import urllib.request
import zipfile

from . import state, structure as st
from .errors import DataError

BASE = "https://nsearchives.nseindia.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MISSING_FIELDS = ("bid/ask quotes", "implied volatility", "Greeks (delta, gamma, theta, vega)",
                  "intraday candles (1m to 1h)", "intraday futures volume and VWAP")
LABEL = ("End-of-day data from NSE's public files. Analysis only: no trades were simulated, "
         "because these files have no bid/ask prices.")
TREND_EMA = 20
ITM_STRIKES = 2


def fo_url(day):
    return "%s/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip" % (BASE, day.strftime("%Y%m%d"))


def index_url(day):
    return "%s/content/indices/ind_close_all_%s.csv" % (BASE, day.strftime("%d%m%Y"))


def _http(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                               "Referer": "https://www.nseindia.com/"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError) as e:
        raise DataError("could not reach %s: %s" % (url, e))


def fetch(url, path, http=_http):
    """Download once; a cached file is reused. Anything but a non-empty 200 is a DataError."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    status, body = http(url)
    if status != 200 or not body:
        raise DataError("%s returned %s (%d bytes): not published or not available" % (url, status, len(body)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)
    return path


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _csv_rows(path):
    with open(path, "rb") as f:
        body = f.read()
    if path.endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(body))
            body = z.read(z.namelist()[0])
        except (zipfile.BadZipFile, IndexError) as e:
            raise DataError("%s is not a readable zip: %s" % (path, e))
    return list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))))


def _num(v):
    v = (v or "").strip()
    if v in ("", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


FO_COLUMNS = ("TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "FinInstrmNm",
              "OpnPric", "HghPric", "LwPric", "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst",
              "ChngInOpnIntrst", "TtlTradgVol", "TtlNbOfTxsExctd", "NewBrdLotQty")


def parse_fo(path, day, symbol="NIFTY"):
    rows = _csv_rows(path)
    if not rows or any(c not in rows[0] for c in FO_COLUMNS):
        raise DataError("F&O file %s lacks expected columns %s" % (path, ", ".join(FO_COLUMNS)))
    options, futures, other_dates = [], [], 0
    for r in rows:
        if r["TckrSymb"].strip() != symbol:
            continue
        if r["TradDt"].strip() != day.isoformat():
            other_dates += 1
            continue
        rec = {"symbol": r["FinInstrmNm"].strip(), "expiry": r["XpryDt"].strip(),
               "open": _num(r["OpnPric"]), "high": _num(r["HghPric"]), "low": _num(r["LwPric"]),
               "close": _num(r["ClsPric"]), "settle": _num(r["SttlmPric"]),
               "underlying": _num(r["UndrlygPric"]), "oi": _num(r["OpnIntrst"]),
               "oi_change": _num(r["ChngInOpnIntrst"]), "volume": _num(r["TtlTradgVol"]),
               "trades": _num(r["TtlNbOfTxsExctd"]), "lot_size": _num(r["NewBrdLotQty"])}
        tp = r["FinInstrmTp"].strip()
        if tp == "IDO" and r["OptnTp"].strip() in ("CE", "PE"):
            rec.update({"strike": _num(r["StrkPric"]), "option_type": r["OptnTp"].strip()})
            options.append(rec)
        elif tp == "IDF":
            futures.append(rec)
    if other_dates:
        raise DataError("F&O file %s has %d %s rows for another trade date" % (path, other_dates, symbol))
    if not options:
        raise DataError("F&O file %s has no %s option rows for %s" % (path, symbol, day))
    return options, futures


def parse_index(path, day, name="Nifty 50"):
    for r in _csv_rows(path):
        if (r.get("Index Name") or "").strip() != name:
            continue
        try:
            d = dt.datetime.strptime(r["Index Date"].strip(), "%d-%m-%Y").date()
        except (KeyError, ValueError):
            raise DataError("index file %s has an unreadable date" % path)
        if d != day:
            raise DataError("index file %s is dated %s, expected %s" % (path, d, day))
        bar = {k: _num(r.get(c)) for k, c in (("open", "Open Index Value"), ("high", "High Index Value"),
                                              ("low", "Low Index Value"), ("close", "Closing Index Value"))}
        if None in bar.values() or not (bar["low"] <= min(bar["open"], bar["close"])
                                        and bar["high"] >= max(bar["open"], bar["close"])):
            raise DataError("index file %s has a missing or inconsistent %s bar" % (path, name))
        bar["date"] = day.isoformat()
        return bar
    raise DataError("index file %s has no %s row" % (path, name))


def history(cal, day, raw_dir, sessions, http=_http):
    """Daily NIFTY bars for `sessions` trading days ending at `day`. Missing days are listed, never filled."""
    days, d = [], day
    while len(days) < sessions:
        days.append(d)
        d = cal.previous_trading_day(d)
    bars, gaps = [], []
    for d in reversed(days):
        try:
            p = fetch(index_url(d), os.path.join(raw_dir, "index", "ind_close_all_%s.csv" % d.strftime("%Y%m%d")), http)
            bars.append(parse_index(p, d))
        except DataError as e:
            if d == day:
                raise
            gaps.append({"date": d.isoformat(), "reason": str(e)})
    return bars, gaps


def trend(bars):
    closes = [b["close"] for b in bars]
    e = st.ema(closes, TREND_EMA)
    if not e or len(e) < 6:
        return {"status": "insufficient history", "detail": "needs %d daily closes, have %d"
                % (TREND_EMA + 5, len(closes))}
    slope = e[-1] - e[-6]
    up, down = closes[-1] > e[-1] and slope > 0, closes[-1] < e[-1] and slope < 0
    return {"status": "up" if up else "down" if down else "sideways",
            "close": closes[-1], "ema": round(e[-1], 2), "ema_change_5d": round(slope, 2),
            "detail": "close %s EMA%d %.2f; EMA%d %s %.2f points over 5 sessions" % (
                "above" if closes[-1] > e[-1] else "below", TREND_EMA, e[-1], TREND_EMA,
                "rose" if slope > 0 else "fell", abs(slope))}


def daily_sweep(prev, cur):
    """A daily-bar sweep: price traded beyond the previous day's high/low and closed back inside."""
    out = []
    if cur["high"] > prev["high"] and cur["close"] < prev["high"]:
        out.append({"side": "bearish", "level_type": "previous day high", "level": prev["high"],
                    "extreme": cur["high"], "close": cur["close"],
                    "detail": "high %.2f went above previous high %.2f, close %.2f back below it" % (
                        cur["high"], prev["high"], cur["close"])})
    if cur["low"] < prev["low"] and cur["close"] > prev["low"]:
        out.append({"side": "bullish", "level_type": "previous day low", "level": prev["low"],
                    "extreme": cur["low"], "close": cur["close"],
                    "detail": "low %.2f went below previous low %.2f, close %.2f back above it" % (
                        cur["low"], prev["low"], cur["close"])})
    return {"found": bool(out), "sweeps": out, "evidence": {"previous_day": prev, "day": cur},
            "note": "daily candles only; intraday confirmation (BOS/CHoCH, FVG, VWAP) needs intraday data, "
                    "which these files do not have"}


def nearest_expiry(options, day):
    exp = sorted({o["expiry"] for o in options if o["expiry"] > day.isoformat()})
    if not exp:
        raise DataError("no NIFTY option expiry after %s in the F&O file" % day)
    return exp[0]


def contracts(options, expiry, spot):
    """ATM plus up to ITM_STRIKES in-the-money strikes for CE and PE. OTM is never listed."""
    legs = [o for o in options if o["expiry"] == expiry and o["strike"] is not None]
    both = sorted({o["strike"] for o in legs if o["option_type"] == "CE"}
                  & {o["strike"] for o in legs if o["option_type"] == "PE"})
    if not both:
        raise DataError("expiry %s has no strike with both CE and PE" % expiry)
    atm = min(both, key=lambda k: (abs(k - spot), k))
    i = both.index(atm)
    pick = {"CE": both[max(0, i - ITM_STRIKES):i + 1][::-1], "PE": both[i:i + ITM_STRIKES + 1]}
    by = {(o["strike"], o["option_type"]): o for o in legs}
    out = []
    for typ in ("CE", "PE"):
        for k in pick[typ]:
            o = dict(by[(k, typ)])
            o["moneyness"] = "ATM" if k == atm else "ITM"
            if o["moneyness"] == "ITM" and not ((typ == "CE" and k < spot) or (typ == "PE" and k > spot)):
                continue            # never list an OTM strike
            o["premium_per_lot"] = round(o["close"] * o["lot_size"], 2) if o["close"] and o["lot_size"] else None
            o["traded_today"] = bool(o["volume"])
            out.append(o)
    return atm, out


def oi_summary(options, expiry):
    legs = [o for o in options if o["expiry"] == expiry and o["oi"] is not None]
    ce = sum(o["oi"] for o in legs if o["option_type"] == "CE")
    pe = sum(o["oi"] for o in legs if o["option_type"] == "PE")
    top = {t: max((o for o in legs if o["option_type"] == t), key=lambda o: o["oi"], default=None) for t in ("CE", "PE")}
    return {"expiry": expiry, "ce_oi": ce, "pe_oi": pe, "pcr_oi": round(pe / ce, 3) if ce else None,
            "max_ce_oi_strike": top["CE"] and top["CE"]["strike"], "max_pe_oi_strike": top["PE"] and top["PE"]["strike"],
            "note": "highest CE OI is often read as resistance and highest PE OI as support; descriptive only"}


def future(futures, day, index_close):
    live = sorted((f for f in futures if f["expiry"] >= day.isoformat()), key=lambda f: f["expiry"])
    if not live:
        return {"status": "missing", "detail": "no NIFTY index future (IDF) rows in the F&O file"}
    f = dict(live[0])
    f["status"] = "ok"
    f["basis"] = round(f["close"] - index_close, 2) if f["close"] is not None else None
    return f


def build(day, cal, raw_dir, sessions=40, http=_http):
    if not cal.is_trading_day(day):
        raise DataError("%s is not an NSE trading day" % day)
    fo_path = fetch(fo_url(day), os.path.join(raw_dir, "fo", "BhavCopy_NSE_FO_%s.csv.zip" % day.strftime("%Y%m%d")), http)
    bars, gaps = history(cal, day, raw_dir, sessions, http)
    cur = bars[-1]
    prev = bars[-2] if len(bars) > 1 and bars[-2]["date"] == cal.previous_trading_day(day).isoformat() else None
    options, futures = parse_fo(fo_path, day)
    spot_fo = options[0]["underlying"]
    expiry = nearest_expiry(options, day)
    atm, picked = contracts(options, expiry, cur["close"])
    sweep = daily_sweep(prev, cur) if prev else {"found": False, "sweeps": [],
                                                 "note": "previous trading day's index file unavailable"}
    tr = trend(bars)
    summary = []
    summary.append("NIFTY closed %.2f (%+.2f)." % (cur["close"], cur["close"] - prev["close"]) if prev
                   else "NIFTY closed %.2f." % cur["close"])
    summary.append("Daily trend: %s." % tr["status"])
    summary.append("Daily sweep: %s." % ("; ".join(s["detail"] for s in sweep["sweeps"]) if sweep["found"] else "none"))
    for s in sweep["sweeps"]:
        aligned = (s["side"] == "bullish" and tr["status"] == "up") or (s["side"] == "bearish" and tr["status"] == "down")
        summary.append("%s sweep %s the daily trend; the %s contracts below are the ones the strategy would look at, "
                       "but no trade is simulated without bid/ask." % (
                           s["side"].capitalize(), "agrees with" if aligned else "goes against",
                           "CE" if s["side"] == "bullish" else "PE"))
    return {
        "label": LABEL, "date": day.isoformat(), "generated_for": "NIFTY",
        "sources": {"fo": {"url": fo_url(day), "sha256": sha256(fo_path)},
                    "index": {"url": index_url(day)}},
        "index": cur, "previous_day": prev, "history_sessions": len(bars), "history_gaps": gaps,
        "underlying_in_fo_file": spot_fo, "trend": tr, "daily_sweep": sweep,
        "expiry": expiry, "atm_strike": atm, "contracts": picked,
        "open_interest": oi_summary(options, expiry), "future": future(futures, day, cur["close"]),
        "trades_simulated": 0, "missing_fields": list(MISSING_FIELDS), "summary": summary,
    }


def markdown(r):
    i = r["index"]
    lines = ["# NIFTY end-of-day report, %s" % r["date"], "", "_%s_" % r["label"], ""]
    lines += ["- " + s for s in r["summary"]] + [""]
    lines += ["## NIFTY", "", "| open | high | low | close |", "|---:|---:|---:|---:|",
              "| %.2f | %.2f | %.2f | %.2f |" % (i["open"], i["high"], i["low"], i["close"]), ""]
    lines += ["Trend: **%s** (%s)" % (r["trend"]["status"], r["trend"]["detail"]), ""]
    lines += ["## Contracts (expiry %s, ATM %s; OTM never listed)" % (r["expiry"], r["atm_strike"]), "",
              "| contract | type | close | settle | OI | OI change | volume | premium/lot |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for c in r["contracts"]:
        lines.append("| %s | %s %s | %s | %s | %s | %s | %s | %s |" % (
            c["symbol"], c["moneyness"], c["option_type"], c["close"], c["settle"], _int(c["oi"]),
            _int(c["oi_change"]), _int(c["volume"]), c["premium_per_lot"]))
    o, f = r["open_interest"], r["future"]
    lines += ["", "## Open interest", "", "PCR (OI) %s; highest CE OI at %s, highest PE OI at %s." % (
        o["pcr_oi"], o["max_ce_oi_strike"], o["max_pe_oi_strike"]), ""]
    lines += ["## Future", "", ("%s close %s, OI %s, volume %s, basis %s" % (
        f["symbol"], f["close"], _int(f["oi"]), _int(f["volume"]), f["basis"])) if f["status"] == "ok" else f["detail"], ""]
    lines += ["## Not in the free data", ""] + ["- " + m for m in r["missing_fields"]]
    if r["history_gaps"]:
        lines += ["", "History gaps: " + ", ".join(g["date"] for g in r["history_gaps"])]
    return "\n".join(lines) + "\n"


def _int(x):
    return "-" if x is None else "{:,}".format(int(x))


def write(report, out_dir):
    day = report["date"]
    state.write_json(os.path.join(out_dir, "%s.json" % day), report)
    state.write_json(os.path.join(out_dir, "latest.json"), report)
    path = os.path.join(out_dir, "%s.md" % day)
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown(report))
    return path
