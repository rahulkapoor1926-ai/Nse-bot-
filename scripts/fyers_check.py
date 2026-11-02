"""Read-only FYERS market-data check. No orders: this file has no order, position or
funds call, and tests/test_fyers_check.py fails if one is added.

Hosts and paths are taken from FYERS's official Python SDK (fyers-apiv3 3.2.1,
fyersModel.Config): API https://api-t1.fyers.in/api/v3, DATA https://api-t1.fyers.in/data.

Environment (GitHub Actions secrets; never printed):
  FYERS_APP_ID      app id, e.g. XXXX-100
  FYERS_SECRET_ID   app secret
  FYERS_AUTH_CODE   one-time auth_code from today's FYERS login redirect

Prints, for each data endpoint, whether it worked and which fields came back.
Tokens are masked in GitHub logs before anything else is printed.
"""

import datetime as dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"
INDEX = "NSE:NIFTY50-INDEX"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CHAIN_FIELDS = ("bid", "ask", "ltp", "oi", "volume", "strike_price", "option_type", "symbol")
GREEK_FIELDS = ("delta", "gamma", "theta", "vega", "iv")


def mask(value):
    if value:
        print("::add-mask::%s" % value, flush=True)


def call(method, url, header=None, body=None, params=None, http=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("version", "3")
    if header:
        req.add_header("Authorization", header)
    if http:
        return http(req)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "error", {"message": type(e).__name__}


def login(app_id, secret, auth_code, http=None):
    h = hashlib.sha256(("%s:%s" % (app_id, secret)).encode()).hexdigest()
    status, doc = call("POST", API + "/validate-authcode",
                       body={"grant_type": "authorization_code", "appIdHash": h, "code": auth_code}, http=http)
    tok = doc.get("access_token")
    mask(tok)
    mask(doc.get("refresh_token"))
    if status != 200 or doc.get("s") != "ok" or not tok:
        return None, "login failed: HTTP %s, FYERS code %s, %s" % (status, doc.get("code"), _safe(doc.get("message")))
    return "%s:%s" % (app_id, tok), "login ok (token masked; refresh token %s)" % (
        "received" if doc.get("refresh_token") else "not returned")


def _safe(msg):
    return str(msg or "")[:120]


def result(name, ok, detail):
    print("%-26s %s  %s" % (name, "WORKS" if ok else "FAILS", detail), flush=True)
    return {"endpoint": name, "works": ok, "detail": detail}


def _bad(status, doc):
    return "HTTP %s, FYERS code %s, %s" % (status, doc.get("code"), _safe(doc.get("message")))


def check_quote(hdr, http=None):
    status, doc = call("GET", DATA + "/quotes", hdr, params={"symbols": INDEX}, http=http)
    d = (doc.get("d") or [{}])[0].get("v") or {}
    if status != 200 or doc.get("s") != "ok" or d.get("lp") is None:
        return result("NIFTY spot quote", False, _bad(status, doc))
    t = d.get("tt")
    when = dt.datetime.fromtimestamp(int(t), IST).isoformat() if t else "no timestamp"
    return result("NIFTY spot quote", True, "last %s at %s; fields: %s" % (d.get("lp"), when, ", ".join(sorted(d))))


def check_chain(hdr, http=None):
    status, doc = call("GET", DATA + "/options-chain-v3", hdr,
                       params={"symbol": INDEX, "strikecount": 5, "greeks": 1}, http=http)
    rows = ((doc.get("data") or {}).get("optionsChain")) or []
    legs = [r for r in rows if r.get("option_type") in ("CE", "PE")]
    if status != 200 or doc.get("s") != "ok" or not legs:
        return result("NIFTY option chain", False, _bad(status, doc)), None
    have = [f for f in CHAIN_FIELDS if all(r.get(f) is not None for r in legs)]
    missing = [f for f in CHAIN_FIELDS if f not in have]
    greeks = [g for g in GREEK_FIELDS if all((r.get("greeks") or {}).get(g, r.get(g)) is not None for r in legs)]
    no_greeks = [g for g in GREEK_FIELDS if g not in greeks]
    zero_quotes = sum(1 for r in legs if not r.get("bid") or not r.get("ask"))
    exp = (doc.get("data") or {}).get("expiryData") or []
    detail = "%d contracts, nearest expiry %s; fields present: %s; missing: %s; Greeks present: %s; Greeks missing: %s; " \
             "legs with zero/empty bid or ask: %d" % (
                 len(legs), exp[0].get("date") if exp else "?", ", ".join(have) or "none", ", ".join(missing) or "none",
                 ", ".join(greeks) or "none", ", ".join(no_greeks) or "none", zero_quotes)
    atm = min(legs, key=lambda r: abs((r.get("strike_price") or 0) - (rows[0].get("ltp") or 0))) if rows else None
    return result("NIFTY option chain", not missing, detail), atm and atm.get("symbol")


def check_history(hdr, symbol, label, http=None):
    today = dt.datetime.now(IST).date()
    start = today - dt.timedelta(days=5)
    status, doc = call("GET", DATA + "/history", hdr, params={
        "symbol": symbol, "resolution": "1", "date_format": "1", "range_from": start.isoformat(),
        "range_to": today.isoformat(), "cont_flag": "1"}, http=http)
    c = doc.get("candles") or []
    if status != 200 or doc.get("s") != "ok" or not c:
        return result(label, False, _bad(status, doc))
    last = dt.datetime.fromtimestamp(int(c[-1][0]), IST)
    vol = sum(1 for x in c if len(x) > 5 and x[5])
    return result(label, True, "%d one-minute candles, last at %s, %d with volume" % (len(c), last.isoformat(), vol))


def check_futures(hdr, http=None):
    status, doc = call("GET", DATA + "/futures-chain", hdr, params={"symbol": INDEX}, http=http)
    rows = [r for r in (doc.get("data") or []) if r.get("expiry")]
    if status != 200 or doc.get("s") != "ok" or not rows:
        return result("NIFTY futures chain", False, _bad(status, doc)), None
    near = min(rows, key=lambda r: int(r["expiry"]))
    return result("NIFTY futures chain", True, "%d futures; nearest %s, last %s, volume %s" % (
        len(rows), near.get("symbol"), near.get("lp"), near.get("volume"))), near.get("symbol")


def check_depth(hdr, symbol, http=None):
    if not symbol:
        return result("ATM option depth", False, "no option symbol from the chain")
    status, doc = call("GET", DATA + "/depth", hdr, params={"symbol": symbol, "ohlcv_flag": 1}, http=http)
    d = (doc.get("d") or {}).get(symbol) or {}
    bids, asks = d.get("bids") or [], d.get("ask") or []
    if status != 200 or doc.get("s") != "ok" or not bids or not asks:
        return result("ATM option depth", False, _bad(status, doc))
    return result("ATM option depth", True, "%s best bid %s x %s, best ask %s x %s" % (
        symbol, bids[0].get("price"), bids[0].get("volume"), asks[0].get("price"), asks[0].get("volume")))


def main(env=os.environ, http=None):
    app_id, secret, code = (env.get(k, "").strip() for k in ("FYERS_APP_ID", "FYERS_SECRET_ID", "FYERS_AUTH_CODE"))
    mask(secret)
    mask(code)
    missing = [k for k, v in (("FYERS_APP_ID", app_id), ("FYERS_SECRET_ID", secret), ("FYERS_AUTH_CODE", code)) if not v]
    if missing:
        print("missing GitHub secrets: %s (values are never printed)" % ", ".join(missing))
        return 2
    hdr, msg = login(app_id, secret, code, http)
    print(msg, flush=True)
    if not hdr:
        return 1
    out = [check_quote(hdr, http)]
    chain, atm = check_chain(hdr, http)
    out.append(chain)
    out.append(check_history(hdr, INDEX, "NIFTY 1m candles", http))
    fut, fsym = check_futures(hdr, http)
    out.append(fut)
    out.append(check_history(hdr, fsym, "NIFTY future 1m candles", http) if fsym
               else result("NIFTY future 1m candles", False, "no future symbol"))
    out.append(check_depth(hdr, atm, http))
    ok = sum(r["works"] for r in out)
    print("summary: %d of %d data checks work. No orders were or can be placed." % (ok, len(out)))
    return 0 if ok == len(out) else 1


if __name__ == "__main__":
    sys.exit(main())
