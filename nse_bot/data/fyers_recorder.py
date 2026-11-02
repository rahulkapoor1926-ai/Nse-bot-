"""FYERS live options recorder: one snapshot a minute of NIFTY and its ATM + 2 ITM CE and PE.

Read-only. Reuses the bot's FYERS client (broker/fyers.py) and symbol master. Each minute of the
session it makes ONE /options-chain-v3 request for the nearest expiry (FYERS's /depth refuses more
than one symbol per request: "More than one symbol is not allowed", live run 41458942560). From it,
per contract: timestamp, symbol, expiry, strike, CE/PE, moneyness, spot, bid, ask, LTP, volume, OI
and a status. The chain gives no quote time and no top-of-book quantities, so those stay empty.

  status   OK          bid, ask and LTP present and the contract traded in the last STALE_SECONDS
           STALE       no trade for more than STALE_SECONDS (its cumulative volume - for the index its
                       value - has not changed), or the snapshot was taken outside the session
           AGE_UNKNOWN first sighting this process: no earlier snapshot to compare volume with
           NO_BID / NO_ASK / NO_LTP   that field was missing (FYERS 0 = no quote; never stored as 0)
           MISSING     FYERS returned nothing for the contract, or the request failed (reason given)
           Several flags are joined with ';'. A missing value is an empty cell, never zero.

Files (date-wise, CSV; Parquet is not used because the bot has no third-party dependencies):
  <out>/<YYYY-MM-DD>/fyers_options_1m.csv    one row per (minute, symbol); never duplicated, also
                                             across restarts (existing keys are read back first)
  <out>/<YYYY-MM-DD>/summary.json            calls used, rows, status counts, symbols seen

Budget: 1 request per minute (2 only if FYERS's first listed expiry differs from the master's),
375 minutes a session plus a login: about 380 calls a day. The process refuses to go past
--max-calls-per-day (default 450), so the recorder and the two paper bots together stay under
FYERS's 5,000 calls a day.

Reconnect: a failed request (network, HTTP error, rate limit) writes MISSING rows for that minute,
waits with a growing back-off (5 s to 60 s) and tries again next minute; an expired token
(HTTP 401 / auth error) triggers one fresh login when today's auth code is available. The loop only
stops at --until, at the end of the session, or when the call budget is used up.
"""

import csv
import datetime as dt
import json
import os
import time

from .. import clock, scanner
from ..errors import NseBotError

COLUMNS = ("ts", "received_at", "underlying", "symbol", "kind", "expiry", "strike", "option_type", "moneyness",
           "spot", "atm_strike", "bid", "ask", "bid_qty", "ask_qty", "ltp", "volume", "oi", "last_change_at",
           "no_change_s", "status", "note", "source")
FILE = "fyers_options_1m.csv"
STALE_SECONDS = 120
ITM = 2
SESSION_START, SESSION_END = "09:15", "15:30"
BACKOFF = (5, 10, 20, 40, 60)


def _v(x):
    return "" if x is None else x


class Recorder(object):
    def __init__(self, md, master, cal, cfg, out_dir, underlying="NIFTY", now=clock.now_ist, sleep=time.sleep,
                 log=print):
        self.md, self.master, self.cal, self.cfg = md, master, cal, cfg
        self.out, self.u = out_dir, underlying
        self.now, self.sleep, self.log = now, sleep, log
        self.index_id = master.index_id(cfg["index_names"][underlying])
        self.spot = None
        self.selection = None                  # last chosen [(label, contract)]
        self.seen = {}                         # day -> set of (ts, symbol)
        self.failures = 0
        self.stats = {}
        self.changed = {}                      # symbol -> (last volume / index value, time it last changed)

    # -- selection ------------------------------------------------------------

    def expiry(self, day):
        return scanner.choose_expiry(self.master, self.cal, self.u, day, self.cfg.get("min_days_to_expiry", 0))

    def strikes_around(self, spot, expiry):
        strikes = self.master.strikes(self.u, expiry)
        if not strikes:
            raise NseBotError("no %s strikes for expiry %s" % (self.u, expiry))
        atm = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i]))
        return strikes, atm

    def select(self, spot, day):
        """ATM, ITM1, ITM2 for CE (strikes below ATM) and PE (strikes above ATM)."""
        e = self.expiry(day)
        if e is None:
            raise NseBotError("no %s expiry passes the calendar cross-check" % self.u)
        strikes, atm = self.strikes_around(spot, e)
        chain = {(c.strike, c.option_type): c for c in self.master.chain(self.u, e)}
        out = []
        for typ, step in (("CE", -1), ("PE", 1)):
            for k in range(ITM + 1):
                i = atm + step * k
                c = chain.get((strikes[i], typ)) if 0 <= i < len(strikes) else None
                out.append(("ATM" if k == 0 else "ITM%d" % k, typ, strikes[i] if 0 <= i < len(strikes) else None, c))
        return strikes[atm], out

    # -- files ----------------------------------------------------------------

    def _path(self, day):
        return os.path.join(self.out, day.isoformat(), FILE)

    def _keys(self, day):
        if day not in self.seen:
            keys = set()
            p = self._path(day)
            if os.path.exists(p):
                with open(p, newline="", encoding="utf-8") as f:
                    head = next(csv.reader(f), None)
                if head and tuple(head) != COLUMNS:          # older layout: kept aside, never mixed in
                    n = 1
                    while os.path.exists(p.replace(".csv", ".old%d.csv" % n)):
                        n += 1
                    os.replace(p, p.replace(".csv", ".old%d.csv" % n))
                    self.log("%s had an older column layout; moved to .old%d.csv" % (p, n))
            if os.path.exists(p):
                with open(p, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        if r.get("ts") and r.get("symbol"):
                            keys.add((r["ts"], r["symbol"]))
            self.seen[day] = keys
        return self.seen[day]

    def _write(self, day, rows):
        keys = self._keys(day)
        fresh = [r for r in rows if (r["ts"], r["symbol"]) not in keys]
        if not fresh:
            return 0
        p = self._path(day)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        new = not os.path.exists(p) or os.path.getsize(p) == 0
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, COLUMNS)
            if new:
                w.writeheader()
            for r in fresh:
                w.writerow({k: _v(r.get(k)) for k in COLUMNS})
            f.flush()
            os.fsync(f.fileno())
        for r in fresh:
            keys.add((r["ts"], r["symbol"]))
        st = self.stats.setdefault(day, {"rows": 0, "status": {}, "symbols": {}, "minutes": set()})
        for r in fresh:
            st["rows"] += 1
            st["minutes"].add(r["ts"])
            for flag in r["status"].split(";"):
                st["status"][flag] = st["status"].get(flag, 0) + 1
            st["symbols"][r["symbol"]] = st["symbols"].get(r["symbol"], 0) + 1
        self._summary(day)
        return len(fresh)

    def _summary(self, day):
        st = self.stats.get(day) or {"rows": 0, "status": {}, "symbols": {}, "minutes": set()}
        doc = {"day": day.isoformat(), "underlying": self.u, "rows_written_this_process": st["rows"],
               "minutes_this_process": len(st["minutes"]), "rows_in_file": len(self._keys(day)),
               "status_counts": st["status"], "rows_per_symbol": st["symbols"],
               "api_calls_this_process": getattr(self.md, "calls_made", None),
               "file": os.path.relpath(self._path(day), self.out), "updated": self.now().isoformat(),
               "note": "read-only FYERS recording; missing values are empty, never zero"}
        p = os.path.join(self.out, day.isoformat(), "summary.json")
        with open(p + ".part", "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
        os.replace(p + ".part", p)
        return doc

    # -- one minute -------------------------------------------------------------

    def _age(self, sym, mark, at):
        """Seconds since `mark` (cumulative volume; the value for the index) last changed, or None at
        the first sighting. A missing mark never counts as a change."""
        prev = self.changed.get(sym)
        if mark is None:
            return (round((at - prev[1]).total_seconds(), 1), prev[1]) if prev else (None, None)
        if prev is None:
            self.changed[sym] = (mark, at)
            return None, None
        if mark != prev[0]:
            self.changed[sym] = (mark, at)
            return 0.0, at
        return round((at - prev[1]).total_seconds(), 1), prev[1]

    def _status(self, fields, sym, mark, at, in_session):
        if fields is None:
            return "MISSING", None, None
        flags = [n for n, k in (("NO_BID", "bid"), ("NO_ASK", "ask"), ("NO_LTP", "ltp")) if k in fields
                 and fields[k] is None]
        age, since = self._age(sym, mark, at)
        if not in_session or (age is not None and age > STALE_SECONDS):
            flags.insert(0, "STALE")
        elif age is None:
            flags.insert(0, "AGE_UNKNOWN")
        return ";".join(flags) or "OK", age, since

    def _in_session(self, at):
        d = at.date()
        return self.cal.is_trading_day(d) and clock.at(d, SESSION_START) <= at <= clock.at(d, SESSION_END) + \
            dt.timedelta(minutes=1)

    def _base(self, ts, received, kind, sym):
        return {"ts": ts.isoformat(), "received_at": received.isoformat(), "underlying": self.u, "symbol": sym,
                "kind": kind, "spot": self.spot, "source": "fyers /options-chain-v3"}

    def record_minute(self, ts):
        """One snapshot labelled `ts` (a minute boundary). Returns rows written (0 if already recorded)."""
        day = ts.date()
        keys = self._keys(day)
        if self.selection is not None and all((ts.isoformat(), self.md.symbols.get(c.instrument_id, ""))
                                              in keys for _, _, _, c in self.selection[1] if c is not None):
            return 0                                   # this minute is already on disk
        e = self.expiry(day)
        if e is None:
            raise NseBotError("no %s expiry passes the calendar cross-check" % self.u)
        try:
            ch = self.md.option_chain(self.index_id, e, enrich_depth=False)
            received = self.now()
            self.failures = 0
        except NseBotError as err:
            return self._failed(ts, day, err)
        live = self._in_session(received)
        self.spot = ch.get("spot")
        isym = self.md.symbols.get(self.index_id, "index")
        st, age, since = self._status(None if self.spot is None else {"ltp": self.spot}, isym, self.spot,
                                      received, live)
        idx = self._base(ts, received, "index", isym)
        idx.update({"ltp": self.spot, "status": st, "no_change_s": age,
                    "last_change_at": since.isoformat() if since else None,
                    "note": "" if live else "outside market hours"})
        rows = [idx]
        if self.spot is None:
            idx["note"] = "no NIFTY spot in the chain: options not selectable this minute"
            return self._write(day, rows)
        legs = {(l["option_type"], l["strike"]): l for l in ch.get("legs") or []}
        atm, sel = self.select(self.spot, day)
        self.selection = (atm, sel)
        for label, typ, strike, c in sel:
            if c is None:
                r = self._base(ts, received, "option", "(none listed: %s %s)" % (strike, typ))
                r.update({"strike": strike, "option_type": typ, "moneyness": label, "atm_strike": atm,
                          "status": "MISSING", "note": "no contract listed at this strike"})
                rows.append(r)
                continue
            sym = self.md.symbols.get(c.instrument_id, "")
            leg = legs.get((typ, strike))
            f = None if leg is None else {k: leg.get(k) for k in ("bid", "ask", "ltp")}
            st, age, since = self._status(f, sym, None if leg is None else leg.get("volume"), received, live)
            r = self._base(ts, received, "option", sym)
            r.update({"expiry": c.expiry.isoformat() if c.expiry else None, "strike": strike, "option_type": typ,
                      "moneyness": label, "atm_strike": atm, "status": st, "no_change_s": age,
                      "last_change_at": since.isoformat() if since else None})
            if leg is None:
                r["note"] = "not returned by FYERS /options-chain-v3"
            else:
                r.update({k: leg.get(k) for k in ("bid", "ask", "ltp", "volume", "oi")})
                notes = [] if live else ["outside market hours"]
                if leg.get("symbol") and leg["symbol"] != sym:
                    notes.append("chain symbol %s" % leg["symbol"])
                r["note"] = "; ".join(notes)
            rows.append(r)
        return self._write(day, rows)

    def _failed(self, ts, day, err):
        self.failures += 1
        msg = str(err)[:200]
        self.log("minute %s: FYERS request failed (%s); rows marked MISSING" % (ts.strftime("%H:%M"), msg))
        if "401" in msg or "auth" in msg.lower() or "token" in msg.lower():
            try:
                self.md.login()                            # fresh token from today's auth code, if available
                self.log("re-login done")
            except Exception as e:                         # noqa: BLE001
                self.log("re-login not possible: %s" % str(e)[:120])
        rows = [{"ts": ts.isoformat(), "received_at": self.now().isoformat(), "underlying": self.u,
                 "symbol": self.md.symbols.get(self.index_id, "index"), "kind": "index", "status": "MISSING",
                 "note": "request failed: %s" % msg, "source": "fyers /options-chain-v3"}]
        if self.selection is not None:
            atm, sel = self.selection
            for label, typ, strike, c in sel:
                if c is not None:
                    rows.append({"ts": ts.isoformat(), "received_at": rows[0]["received_at"], "underlying": self.u,
                                 "symbol": self.md.symbols.get(c.instrument_id, ""), "kind": "option",
                                 "expiry": c.expiry.isoformat(), "strike": strike, "option_type": typ,
                                 "moneyness": label, "atm_strike": atm, "status": "MISSING",
                                 "note": "request failed: %s" % msg, "source": "fyers /options-chain-v3"})
        n = self._write(day, rows)
        self.sleep(BACKOFF[min(self.failures, len(BACKOFF)) - 1])
        return n

    # -- loop ---------------------------------------------------------------

    def run(self, until, smoke=0):
        """Record each minute boundary of the session until `until` (HH:MM IST). smoke=N records N
        snapshots straight away whatever the market state (for a connectivity test)."""
        written, snaps = 0, 0
        while True:
            now = self.now()
            day = now.date()
            end = clock.at(day, until)
            if now >= end and not smoke:
                break
            nxt = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
            if smoke:
                ts = now.replace(second=0, microsecond=0)
            else:
                self.sleep(max(0.0, (nxt - now).total_seconds()) + 3)      # just after the minute closes
                ts = nxt
                if not self.cal.is_trading_day(day) or not \
                        clock.at(day, SESSION_START) < ts <= clock.at(day, SESSION_END):
                    if ts > clock.at(day, SESSION_END):
                        break
                    continue
            try:
                written += self.record_minute(ts)
            except NseBotError as e:
                self.log("minute %s: %s" % (ts.strftime("%H:%M"), e))
                if "calls for the day" in str(e):
                    break
            snaps += 1
            if smoke:
                if snaps >= smoke:
                    break
                self.sleep(60)
            budget = getattr(self.md, "_per_day", None)
            if budget and getattr(self.md, "calls_made", 0) >= budget:
                self.log("call budget for the day used (%d); stopping" % budget)
                break
        return {"rows_written": written, "snapshots": snaps, "api_calls": getattr(self.md, "calls_made", None)}
