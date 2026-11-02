"""Read-only FYERS market-data client (standard library only).

Hosts and paths are taken from FYERS's official Python SDK (fyers-apiv3 3.2.1,
fyersModel.Config) and were verified live from GitHub Actions on 2026-11-02 (runs
41403687715, 41404326502):
  token     POST https://api-t1.fyers.in/api/v3/validate-authcode
  data      GET  https://api-t1.fyers.in/data/{quotes,depth,history,options-chain-v3,futures-chain}
  symbols   https://public.fyers.in/sym_details/NSE_FO_sym_master.json (public, no login)

Safety:
  * only the paths in READ_PATHS can be requested, with GET; the single POST is the
    token exchange. There is no order, position, funds or profile call in this file,
    and tests/test_fyers.py fails if one is added;
  * credentials come from environment variables only, are never stored, logged or
    printed, and are scrubbed from error messages;
  * a missing price is None, never 0. A FYERS bid/ask of 0 means "no quote".

Freshness: the `tt` field of /data/quotes is the start of the trading day, not the
time of the quote, so it is never used. Quote.ts is the last-trade time (`ltt`) from
/data/depth for that exact symbol; a missing ltt, or one that falls on midnight, makes
the quote stale. Candle timestamps are never used as quote freshness.
"""

import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import clock, quotes
from ..errors import ConfigError, DataError
from .base import MarketData

API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"
FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"
CM_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM_sym_master.json"
READ_PATHS = ("/quotes", "/depth", "/history", "/options-chain-v3", "/futures-chain")
TOKEN_PATH = "/validate-authcode"
# Index symbols verified live. Others are refused rather than guessed.
INDEX_SYMBOLS = {"NIFTY": "NSE:NIFTY50-INDEX"}
VIX_SYMBOL = "NSE:INDIAVIX-INDEX"      # verified live 2026-11-02 (247 daily candles for one year)
MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}
RESOLUTION = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "1d": "D"}
MAX_DAYS = {"D": 365}                 # per request; intraday uses 90 (FYERS allows up to 100)
UA = "Mozilla/5.0 (X11; Linux x86_64) nse-bot-readonly/1.0"
ENV_APP, ENV_SECRET, ENV_CODE, ENV_TOKEN = "FYERS_APP_ID", "FYERS_SECRET_ID", "FYERS_AUTH_CODE", "FYERS_ACCESS_TOKEN"
PER_SECOND = 5                        # FYERS free plan: 5/s, 50/min, 5,000/day (data APIs), per app
UNITS_NOTE = ("FYERS: prices in rupees; theta is per day and vega per 1% IV in rupees; iv in percent "
              "(FYERS option-chain docs). Verify against live values before relying on them.")


class FyersError(DataError):
    def __init__(self, msg, status=None, code=None):
        super().__init__(msg)
        self.status, self.code = status, code


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _price(v):
    """FYERS uses 0 for 'no price'; that is missing, never a real zero."""
    if v is None or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _int(v):
    try:
        return None if v is None or isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


class FyersReadOnly(MarketData):
    source = "fyers"
    verifies_spot = True

    def __init__(self, env=os.environ, transport=urllib_transport, per_minute=20, sleep=time.sleep,
                 monotonic=time.monotonic, per_day=2400, per_second=PER_SECOND, now=clock.now_ist):
        self._app = (env.get(ENV_APP) or "").strip()
        self._secret = (env.get(ENV_SECRET) or "").strip()
        self._code = (env.get(ENV_CODE) or "").strip()
        self._token = (env.get(ENV_TOKEN) or "").strip() or None
        if not self._app:
            raise ConfigError("%s is not set (add it as a GitHub Actions repository secret)" % ENV_APP)
        if not self._token and not (self._secret and self._code):
            raise ConfigError("FYERS needs %s, or %s and today's %s" % (ENV_TOKEN, ENV_SECRET, ENV_CODE))
        self._transport, self._sleep, self._mono, self._now = transport, sleep, monotonic, now
        self._per_minute, self._per_day, self._per_second = int(per_minute), int(per_day), int(per_second)
        self._candle_cache = {}
        self._calls = []
        self.calls_made = 0
        self.symbols = {}                 # instrument_id -> FYERS ticker, from bind()
        self.index_ids = {}               # underlying -> index instrument_id
        self.master = None

    # -- plumbing --------------------------------------------------------------

    def _scrub(self, text):
        text = str(text)
        for s in (self._secret, self._token, self._code):
            if s:
                text = text.replace(s, "***")
        return text[:200]

    def _throttle(self):
        if self.calls_made >= self._per_day:
            raise FyersError("this process used its %d FYERS calls for the day (free plan: 5,000/day in total); "
                             "treating further data as missing" % self._per_day)
        while True:
            now = self._mono()
            self._calls = [t for t in self._calls if now - t < 60]
            last_sec = [t for t in self._calls if now - t < 1]
            if len(last_sec) >= self._per_second:
                self._sleep(1 - (now - min(last_sec)) + 0.01)
            elif len(self._calls) >= self._per_minute:
                self._sleep(60 - (now - min(self._calls)) + 0.01)
            else:
                break
        self._calls.append(now)
        self.calls_made += 1

    def _request(self, method, base, path, params=None, body=None, auth=True):
        if method == "GET" and (base != DATA or path not in READ_PATHS):
            raise ConfigError("refusing GET %s%s: not a read-only market-data path" % (base, path))
        if method == "POST" and (base != API or path != TOKEN_PATH):
            raise ConfigError("refusing POST %s%s: only the token exchange may POST" % (base, path))
        if method not in ("GET", "POST"):
            raise ConfigError("refusing %s" % method)
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
                   "version": "3"}
        if auth:
            if self._token is None:
                self.login()
            headers["Authorization"] = "%s:%s" % (self._app, self._token)
        self._throttle()
        try:
            status, raw = self._transport(method, url, headers, json.dumps(body).encode() if body else None, 30)
        except OSError as e:
            raise FyersError("FYERS %s unreachable: %s" % (path, self._scrub(e)))
        try:
            doc = json.loads((raw or b"{}").decode("utf-8", "replace"))
        except ValueError:
            raise FyersError("FYERS %s returned HTTP %s with a non-JSON body" % (path, status), status)
        if status == 429:
            raise FyersError("FYERS rate limit hit on %s: treated as missing data" % path, status, doc.get("code"))
        if status != 200 or doc.get("s") != "ok":
            raise FyersError("FYERS %s failed: HTTP %s, code %s, %s" % (
                path, status, doc.get("code"), self._scrub(doc.get("message"))), status, doc.get("code"))
        return doc

    def login(self):
        """Exchange today's auth code for an access token. The token is kept in memory only."""
        if not (self._secret and self._code):
            raise FyersError("no FYERS access token and no auth code to exchange")
        h = hashlib.sha256(("%s:%s" % (self._app, self._secret)).encode()).hexdigest()
        doc = self._request("POST", API, TOKEN_PATH, body={"grant_type": "authorization_code", "appIdHash": h,
                                                           "code": self._code}, auth=False)
        tok = doc.get("access_token")
        if not tok:
            raise FyersError("FYERS token response had no access_token")
        self._token = tok
        return {"refresh_token_received": bool(doc.get("refresh_token"))}    # never the token itself

    def bind(self, master, symbols):
        """Attach the instrument master and its id -> FYERS ticker map (see build_master)."""
        self.master = master
        self.symbols = {int(k): v for k, v in symbols.items()}
        self.index_ids = {u: iid for u, s in INDEX_SYMBOLS.items() for iid, t in self.symbols.items() if t == s}
        return self

    def _sym(self, iid):
        s = self.symbols.get(int(iid))
        if not s:
            raise FyersError("instrument %s has no FYERS symbol in the bound master" % iid)
        return s

    # -- MarketData ------------------------------------------------------------

    def quote_from_depth(self, iid, d):
        bids, asks = d.get("bids") or [], d.get("ask") or []
        ltt = _int(d.get("ltt"))
        ts = None
        if ltt:
            ts = clock.from_epoch_ms(ltt * 1000)
            if ts.time() == dt.time(0, 0) or ts.time() == dt.time(5, 30):
                ts = None                       # a day-start stamp, not a quote time
        return quotes.Quote(
            instrument_id=int(iid), ltp=_price(d.get("ltp")),
            bid=_price(bids[0].get("price")) if bids else None,
            ask=_price(asks[0].get("price")) if asks else None,
            bid_qty=_int(bids[0].get("volume")) if bids and _price(bids[0].get("price")) else None,
            ask_qty=_int(asks[0].get("volume")) if asks and _price(asks[0].get("price")) else None,
            ts=ts, volume=_int(d.get("v")), oi=_int(d.get("oi")),
            dpr_low=_price(d.get("lower_ckt")), dpr_high=_price(d.get("upper_ckt")), source="fyers",
            extra={"received_at": self._now().isoformat(),
                   # order-flow inputs (resting orders only; FYERS gives no trade side)
                   "depth": {"bids": [(_price(x.get("price")), _int(x.get("volume")), _int(x.get("ord"))) for x in bids],
                             "asks": [(_price(x.get("price")), _int(x.get("volume")), _int(x.get("ord"))) for x in asks],
                             "total_buy_qty": _int(d.get("totalbuyqty")), "total_sell_qty": _int(d.get("totalsellqty")),
                             "last_trade_qty": _int(d.get("ltq"))}})

    def quotes(self, instrument_ids):
        out = {}
        for iid in instrument_ids:
            sym = self._sym(iid)
            doc = self._request("GET", DATA, "/depth", {"symbol": sym, "ohlcv_flag": 1})
            d = (doc.get("d") or {}).get(sym)
            if d:
                out[int(iid)] = self.quote_from_depth(iid, d)
        return out

    def candles(self, instrument_id, size, start, end, oi=False):
        res = RESOLUTION.get(size)
        if res is None:
            raise FyersError("unsupported candle size %r" % size)
        sym = self._sym(instrument_id)
        # A bar cannot change until the next one is due, so the same request inside one bar
        # period is answered from memory. This keeps the bot inside FYERS's daily call limit.
        bucket = int(end.timestamp() // (MINUTES[size] * 60))
        key = (int(instrument_id), size, start, bool(oi))
        hit = self._candle_cache.get(key)
        if hit and hit[0] == bucket:
            return [r for r in hit[1] if r[0] <= end]
        step = dt.timedelta(days=MAX_DAYS.get(res, 90))
        rows, frm = {}, start
        while frm <= end:
            to = min(end, frm + step)
            params = {"symbol": sym, "resolution": res, "date_format": 0, "range_from": int(frm.timestamp()),
                      "range_to": int(to.timestamp()), "cont_flag": 1}
            if oi:
                params["oi_flag"] = 1
            doc = self._request("GET", DATA, "/history", params)
            for c in doc.get("candles") or []:
                if len(c) < 6 or any(v is None for v in c[:5]):
                    raise FyersError("malformed candle from FYERS for %s" % sym)
                t = clock.from_epoch_ms(int(c[0]) * 1000)
                if start <= t <= end:
                    rows[t] = (t, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                               _int(c[5]), _int(c[6]) if len(c) > 6 else None)
            frm = to + dt.timedelta(seconds=1)
        out = [rows[k] for k in sorted(rows)]
        self._candle_cache[key] = (bucket, out)
        return out

    def expiries(self, underlying_id):
        if self.master is None:
            raise FyersError("bind() the instrument master first")
        u = next((k for k, v in self.index_ids.items() if v == int(underlying_id)), None)
        if u is None:
            raise FyersError("instrument %s is not a bound index" % underlying_id)
        return self.master.expiries(u, clock.now_ist().date())

    def option_chain(self, underlying_id, expiry, enrich_depth=True, max_itm=2):
        """Chain for one expiry as {"format": "legs", "spot", "legs": [...]} in rupees.

        The chain endpoint has no top-of-book quantities or quote times, so the ATM strike and
        up to `max_itm` ITM strikes on each side are re-read from /depth (bid, ask, quantities,
        last-trade time). Other legs keep bid_qty/ask_qty None and can never be selected."""
        sym = self._sym(underlying_id)
        params = {"symbol": sym, "strikecount": 10, "greeks": 1}
        doc = self._request("GET", DATA, "/options-chain-v3", params)
        data = doc.get("data") or {}
        if expiry is not None:
            exp = [e for e in data.get("expiryData") or [] if e.get("date") == expiry.strftime("%d-%m-%Y")]
            if not exp:
                raise FyersError("FYERS lists no expiry %s for %s" % (expiry, sym))
            first = (data.get("expiryData") or [{}])[0]
            if first.get("date") != exp[0].get("date"):
                params["timestamp"] = exp[0].get("expiry")
                data = self._request("GET", DATA, "/options-chain-v3", params).get("data") or {}
        rows = data.get("optionsChain") or []
        spot = next((_price(r.get("ltp")) for r in rows if not r.get("option_type")), None)
        legs = []
        for r in rows:
            if r.get("option_type") not in ("CE", "PE"):
                continue
            g = r.get("greeks") or {}

            def gv(k):
                v = g.get(k, r.get(k))
                return None if v is None or isinstance(v, bool) else float(v)
            legs.append({"instrument_id": _int(r.get("fyToken")), "option_type": r["option_type"],
                         "strike": float(r["strike_price"]) if r.get("strike_price") is not None else None,
                         "bid": _price(r.get("bid")), "ask": _price(r.get("ask")), "bid_qty": None, "ask_qty": None,
                         "ltp": _price(r.get("ltp")), "volume": _int(r.get("volume")), "oi": _int(r.get("oi")),
                         "prev_oi": _int(r.get("prev_oi")), "iv": gv("iv"), "delta": gv("delta"),
                         "gamma": gv("gamma"), "theta": gv("theta"), "vega": gv("vega"),
                         "symbol": r.get("symbol"), "ts": None})
        if enrich_depth and spot and legs:
            strikes = sorted({l["strike"] for l in legs if l["strike"] is not None})
            atm = min(strikes, key=lambda k: (abs(k - spot), k))
            i = strikes.index(atm)
            want = {("CE", k) for k in strikes[max(0, i - max_itm):i + 1]} | {("PE", k) for k in strikes[i:i + max_itm + 1]}
            for l in legs:
                if (l["option_type"], l["strike"]) not in want or not l["symbol"]:
                    continue
                d = (self._request("GET", DATA, "/depth", {"symbol": l["symbol"], "ohlcv_flag": 1}).get("d")
                     or {}).get(l["symbol"])
                if not d:
                    l["bid"] = l["ask"] = None          # no depth: never selectable
                    continue
                q = self.quote_from_depth(l["instrument_id"], d)
                l.update({"bid": q.bid, "ask": q.ask, "bid_qty": q.bid_qty, "ask_qty": q.ask_qty,
                          "ts": q.ts.isoformat() if q.ts else None})
        vix = data.get("indiavixData") or {}
        return {"format": "legs", "source": "fyers", "spot": spot, "legs": legs, "units_note": UNITS_NOTE,
                "expiry": expiry.isoformat() if expiry else None,
                # chain-level figures exactly as FYERS reports them (for the options-desk context)
                "totals": {"call_oi": _int(data.get("callOi")), "put_oi": _int(data.get("putOi"))},
                "india_vix": _price(vix.get("ltp")),
                "expiry_epoch": next((_int(e.get("expiry")) for e in data.get("expiryData") or []
                                      if expiry is None or e.get("date") == expiry.strftime("%d-%m-%Y")), None)}

    def vix_daily(self, end, days=400):
        """India VIX daily closes [(date, close)] up to `end` (one call, cached for the day)."""
        key = ("vix", end.date())
        if key not in self._candle_cache:
            start = end - dt.timedelta(days=days)
            doc = self._request("GET", DATA, "/history", {
                "symbol": VIX_SYMBOL, "resolution": "D", "date_format": 0, "range_from": int(start.timestamp()),
                "range_to": int(end.timestamp()), "cont_flag": 1})
            rows = [(clock.from_epoch_ms(int(c[0]) * 1000).date(), float(c[4])) for c in doc.get("candles") or []
                    if len(c) > 4 and c[4] is not None]
            self._candle_cache[key] = rows
        return self._candle_cache[key]


# -- instrument master -----------------------------------------------------------

MASTER_COLUMNS = ("id", "symbol", "company_name", "underlying_symbol", "exchange", "segment", "instrument_type",
                  "option_type", "expiry_date", "strike_price", "lot_size", "tick_size", "freeze_quantity", "series")


def _fetch_json(url, transport):
    status, raw = transport("GET", url, {"User-Agent": UA}, None, 120)
    if status != 200 or not raw:
        raise FyersError("FYERS symbol master %s: HTTP %s" % (url, status), status)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise FyersError("FYERS symbol master %s is not JSON" % url)


def master_rows(fo, cm, underlyings, index_names):
    """FYERS symbol-master JSON -> rows in the same columns instruments.parse reads, plus the
    id -> FYERS ticker map. Only fields named in the JSON are used; nothing is inferred."""
    rows, symbols = [], {}
    for u in underlyings:
        if u not in INDEX_SYMBOLS:
            raise FyersError("no verified FYERS index symbol for %s" % u)
    index_tok = {}
    for ticker, r in fo.items():
        u = r.get("underSym")
        if u not in underlyings:
            continue
        try:
            iid = int(r["fyToken"])
            exp = clock.from_epoch_ms(int(r["expiryDate"]) * 1000).date()
            lot, tick = int(r["minLotSize"]), float(r["tickSize"])
            freeze = int(float(r.get("qtyFreeze") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        opt = r.get("optType")
        if opt in ("CE", "PE") and float(r.get("strikePrice") or 0) > 0:
            itype, strike = "OPTIDX", float(r["strikePrice"])
        elif opt == "XX" and r.get("exInstType") == 11:
            itype, strike = "FUTIDX", 0.0
        else:
            continue
        if r.get("underFyTok"):
            index_tok[u] = int(r["underFyTok"])
        rows.append({"id": iid, "symbol": ticker, "company_name": r.get("symbolDetails") or ticker,
                     "underlying_symbol": u, "exchange": "NSE", "segment": "NFO", "instrument_type": itype,
                     "option_type": opt if itype == "OPTIDX" else "", "expiry_date": exp.strftime("%d-%m-%Y"),
                     "strike_price": strike, "lot_size": lot, "tick_size": tick, "freeze_quantity": freeze,
                     "series": ""})
        symbols[iid] = ticker
    for u in underlyings:
        if u not in index_tok:
            raise FyersError("FYERS master has no %s derivatives (cannot find the index token)" % u)
        rows.append({"id": index_tok[u], "symbol": INDEX_SYMBOLS[u], "company_name": index_names[u],
                     "underlying_symbol": u, "exchange": "NSE", "segment": "NSE", "instrument_type": "IDX",
                     "option_type": "", "expiry_date": "", "strike_price": "", "lot_size": "", "tick_size": "",
                     "freeze_quantity": "", "series": ""})
        symbols[index_tok[u]] = INDEX_SYMBOLS[u]
    for ticker, r in (cm or {}).items():
        if not ticker.endswith("-EQ"):
            continue
        try:
            iid, tick = int(r["fyToken"]), float(r["tickSize"])
        except (KeyError, TypeError, ValueError):
            continue
        name = r.get("exSymbol") or ticker[4:-3]
        rows.append({"id": iid, "symbol": name, "company_name": r.get("symbolDesc") or name,
                     "underlying_symbol": name, "exchange": "NSE", "segment": "NSE",
                     "instrument_type": "ETF" if r.get("exInstType") == 9 else "STK", "option_type": "",
                     "expiry_date": "", "strike_price": "", "lot_size": 1, "tick_size": tick, "freeze_quantity": 0,
                     "series": "EQ"})
        symbols[iid] = ticker
    return rows, symbols


def build_master(path, underlyings, index_names, with_cash=True, transport=urllib_transport):
    """Download FYERS's public symbol master and write it in the bot's master format
    (path) plus the ticker map (path + '.symbols.json'). Returns the number of rows."""
    import csv
    fo = _fetch_json(FO_MASTER_URL, transport)
    cm = None
    if with_cash:
        try:
            cm = _fetch_json(CM_MASTER_URL, transport)
        except FyersError:
            cm = None                               # stocks/ETFs then simply are not available
    rows, symbols = master_rows(fo, cm, underlyings, index_names)
    tmp = path + ".part"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, MASTER_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    with open(path + ".symbols.json.part", "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in symbols.items()}, f)
    os.replace(path + ".symbols.json.part", path + ".symbols.json")
    return {"rows": len(rows), "cash_included": cm is not None}


def load_symbols(path):
    try:
        with open(path + ".symbols.json", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError) as e:
        raise FyersError("FYERS ticker map %s.symbols.json unreadable: %s" % (path, e))
