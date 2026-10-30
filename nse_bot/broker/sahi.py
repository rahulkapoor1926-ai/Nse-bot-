"""Read-only SAHI market-data client (standard library only).

Verified against the official documentation (https://docs.sahi.com/, read
2026-10-30) and the official SDK `sahiapi` 0.3.0 from PyPI (sahi/env.py,
sahi/auth.py, sahi/marketdata.py):
  auth host     https://apis.sahi.com   POST /api/v1/apikeys/token
  API host      https://direct.sahi.com GET  /v1/marketdata/{ltp,quotes,candles,optionchain,optionchain/expiries}
  scrip master  https://assets.sahi.com/external/instruments.csv
Money is integer paise, times are epoch milliseconds. Tokens expire at the next
06:00 IST and are not refreshable: mint again.

Safety:
  * the token is minted with scopes=["marketdata.read"] ONLY and refused if the
    server grants anything else, so even a bug here cannot place an order;
  * only paths in READ_PATHS can be requested, and only with GET;
  * credentials come from environment variables, are never stored, logged or
    printed, and are scrubbed from error messages.

A live connection was NOT verified from the build environment (its network
policy blocks *.sahi.com). Run `python -m nse_bot sahi-check` on your machine.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import clock, quotes
from ..errors import ConfigError, DataError
from .base import MarketData

AUTH_BASE = "https://apis.sahi.com"
API_BASE = "https://direct.sahi.com"
SCRIP_MASTER_URL = "https://assets.sahi.com/external/instruments.csv"
TOKEN_PATH = "/api/v1/apikeys/token"
READ_PATHS = ("/v1/marketdata/ltp", "/v1/marketdata/quotes", "/v1/marketdata/candles",
              "/v1/marketdata/optionchain", "/v1/marketdata/optionchain/expiries")
SCOPE = "marketdata.read"
MIN_INTERVAL = {"/v1/marketdata/ltp": 1.0, "/v1/marketdata/quotes": 1.0, "/v1/marketdata/candles": 0.5,
                "/v1/marketdata/optionchain": 0.5, "/v1/marketdata/optionchain/expiries": 0.2}
MAX_IDS = 100
MAX_RETRY_AFTER = 10
ENV_KEY_ID, ENV_SECRET, ENV_TOTP = "SAHI_KEY_ID", "SAHI_KEY_SECRET", "SAHI_TOTP"
UA = "nse-bot/0.1 (read-only)"


class SahiError(DataError):
    def __init__(self, msg, status=None, code=None):
        super().__init__(msg)
        self.status, self.code = status, code


def urllib_transport(method, url, headers, body, timeout):
    """Returns (status, headers dict, body bytes). Raises OSError on network failure."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


class SahiReadOnly(MarketData):
    def __init__(self, env=None, transport=urllib_transport, sleep=time.sleep, monotonic=time.monotonic,
                 timeout=15):
        env = os.environ if env is None else env
        self._key_id = env.get(ENV_KEY_ID)
        self._secret = env.get(ENV_SECRET)
        self._totp = env.get(ENV_TOTP)
        if not self._key_id or not self._secret:
            raise ConfigError("set %s and %s in the environment (never in a file under version control)"
                              % (ENV_KEY_ID, ENV_SECRET))
        self._transport, self._sleep, self._mono = transport, sleep, monotonic
        self._timeout = timeout
        self._token = None
        self._last = {}

    # -- plumbing ---------------------------------------------------------------

    def _scrub(self, text):
        text = str(text)
        for s in (self._secret, self._token, self._totp):
            if s:
                text = text.replace(s, "***")
        return text

    def _call(self, method, base, path, params=None, body=None, auth=True):
        if method == "GET" and path not in READ_PATHS:
            raise ConfigError("refusing GET %s: not a read-only market-data path" % path)
        if method == "POST" and path != TOKEN_PATH:
            raise ConfigError("refusing POST %s: only the token mint may POST" % path)
        if method not in ("GET", "POST"):
            raise ConfigError("refusing %s: read-only client" % method)
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer %s" % self._token
        gap = MIN_INTERVAL.get(path, 0)
        last = self._last.get(path)
        if last is not None and self._mono() - last < gap:
            self._sleep(gap - (self._mono() - last))
        self._last[path] = self._mono()
        try:
            status, hdrs, raw = self._transport(method, url, headers, data, self._timeout)
        except OSError as e:
            raise SahiError("network error calling %s: %s" % (path, self._scrub(e)))
        try:
            doc = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            doc = None
        if 200 <= status < 300:
            if not isinstance(doc, dict):
                raise SahiError("%s returned a non-JSON body" % path, status)
            return doc, hdrs
        code = doc.get("code") if isinstance(doc, dict) else None
        msg = doc.get("message") if isinstance(doc, dict) else None
        err = SahiError("SAHI %s %s -> HTTP %s %s: %s" % (method, path, status, code or "", self._scrub(msg or "")),
                        status, code)
        err.retry_after = None
        ra = {k.lower(): v for k, v in (hdrs or {}).items()}.get("retry-after")
        if ra and str(ra).isdigit():
            err.retry_after = int(ra)
        raise err

    def mint(self):
        body = {"key_id": self._key_id, "secret": self._secret, "scopes": [SCOPE]}
        if self._totp:
            body["totp"] = self._totp
        doc, _ = self._call("POST", AUTH_BASE, TOKEN_PATH, body=body, auth=False)
        tok, scope = doc.get("access_token"), doc.get("scope")
        if not tok:
            raise SahiError("token response had no access_token")
        if (scope or "").split() != [SCOPE]:
            raise SahiError("token carries scopes %r; refusing anything beyond %s" % (scope, SCOPE))
        self._token = tok
        return {"scope": scope, "expires_at": doc.get("expires_at")}   # never the token itself

    def _get(self, path, params):
        if self._token is None:
            self.mint()
        for attempt in range(3):
            try:
                return self._call("GET", API_BASE, path, params)[0]
            except SahiError as e:
                if e.status == 401 and attempt == 0:
                    self.mint()                     # expired at 06:00 IST or revoked
                    continue
                if e.status == 429 and e.retry_after is not None and e.retry_after <= MAX_RETRY_AFTER and attempt < 2:
                    self._sleep(e.retry_after)
                    continue
                raise
        raise SahiError("gave up on %s after retries" % path)

    # -- MarketData -----------------------------------------------------------

    def quotes(self, instrument_ids):
        ids = [int(i) for i in instrument_ids]
        out = {}
        for k in range(0, len(ids), MAX_IDS):
            doc = self._get("/v1/marketdata/quotes", {"i": ids[k:k + MAX_IDS]})
            data = doc.get("data")
            if not isinstance(data, dict):
                raise SahiError("quotes response has no data object")
            for iid, d in data.items():
                if d is not None:
                    out[int(iid)] = quotes.from_sahi(iid, d)
        return out

    def candles(self, instrument_id, size, start, end, oi=False):
        rows, to_ms = [], int(end.timestamp() * 1000)
        frm = int(start.timestamp() * 1000)
        while to_ms >= frm:
            doc = self._get("/v1/marketdata/candles", {"instrument_id": int(instrument_id), "size": size,
                                                       "from": frm, "to": to_ms, "oi": str(oi).lower()})
            page = doc.get("candles") or []
            if not page:
                break
            rows = page + rows
            if not doc.get("has_more"):
                break
            to_ms = int(page[0][0]) - 1
        out = []
        for c in rows:
            if len(c) < 6 or any(v is None for v in c[:5]):
                raise SahiError("malformed candle %r" % (c,))
            out.append((clock.from_epoch_ms(c[0]), c[1] / 100.0, c[2] / 100.0, c[3] / 100.0, c[4] / 100.0,
                        c[5], c[6] if len(c) > 6 else None))
        return out

    def expiries(self, underlying_id):
        import datetime as dt
        doc = self._get("/v1/marketdata/optionchain/expiries", {"underlying_id": int(underlying_id)})
        return [dt.date.fromisoformat(x) for x in doc.get("expiries", [])]

    def option_chain(self, underlying_id, expiry):
        params = {"underlying_id": int(underlying_id)}
        if expiry is not None:
            params["expiry"] = expiry.isoformat()
        return self._get("/v1/marketdata/optionchain", params)


def download_scrip_master(path, transport=urllib_transport, timeout=60):
    status, _, raw = transport("GET", SCRIP_MASTER_URL, {"User-Agent": UA}, None, timeout)
    if status != 200 or not raw:
        raise SahiError("scrip master download failed: HTTP %s" % status, status)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)
    return len(raw)
