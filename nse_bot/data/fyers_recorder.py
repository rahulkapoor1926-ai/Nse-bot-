"""FYERS live options recorder: one snapshot a minute of NIFTY and its ATM + 2 ITM CE and PE.

Read-only. Reuses the bot's FYERS client (broker/fyers.py) and symbol master. Each minute of the
session it makes ONE batched /depth request for the index and the strikes around the money, then
stores, per contract: timestamp, symbol, expiry, strike, CE/PE, moneyness, spot, bid, ask, the
displayed quantities, LTP, volume, OI, last-trade time and a status.

  status   OK        bid, ask and LTP present, last trade within STALE_SECONDS
           STALE     the last trade is older than STALE_SECONDS (FYERS gives no quote timestamp,
                     only the last-trade time, so staleness is measured on that)
           NO_BID / NO_ASK / NO_LTP   that field was missing (FYERS 0 = no quote; never stored as 0)
           MISSING   FYERS returned nothing for the symbol, or the request failed (reason given)
           Several flags are joined with ';'. A missing value is an empty cell, never zero.

Files (date-wise, CSV; Parquet is not used because the bot has no third-party dependencies):
  <out>/<YYYY-MM-DD>/fyers_options_1m.csv    one row per (minute, symbol); never duplicated, also
                                             across restarts (existing keys are read back first)
  <out>/<YYYY-MM-DD>/summary.json            calls used, rows, status counts, symbols seen

Budget: 1 request per minute, 375 minutes a session, plus a login and one start-up spot request,
about 380 calls a day. The process refuses to go past --max-calls-per-day (default 450), so the
recorder and the two paper bots together stay under FYERS's 5,000 calls a day.

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
           "spot", "atm_strike", "bid", "ask", "bid_qty", "ask_qty", "ltp", "volume", "oi", "last_trade_time",
           "last_trade_age_s", "status", "note", "source")
FILE = "fyers_options_1m.csv"
STALE_SECONDS = 120
ITM = 2
REQUEST_WIDTH = 3            # strikes each side of ATM requested, so a moving spot stays covered
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

    def request_ids(self, day):
        """The index plus REQUEST_WIDTH strikes each side of the last ATM, both types (one request)."""
        ids = [self.index_id]
        if self.spot is None:
            return ids
        e = self.expiry(day)
        if e is None:
            return ids
        strikes, atm = self.strikes_around(self.spot, e)
        lo, hi = max(0, atm - REQUEST_WIDTH), min(len(strikes), atm + REQUEST_WIDTH + 1)
        want = set(strikes[lo:hi])
        ids += [c.instrument_id for c in self.master.chain(self.u, e) if c.strike in want]
        return ids

    # -- files ----------------------------------------------------------------

    def _path(self, day):
        return os.path.join(self.out, day.isoformat(), FILE)

    def _keys(self, day):
        if day not in self.seen:
            keys = set()
            p = self._path(day)
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

    def _status(self, q, at):
        if q is None:
            return "MISSING", None
        flags = []
        for name, v in (("NO_BID", q.bid), ("NO_ASK", q.ask), ("NO_LTP", q.ltp)):
            if v is None:
                flags.append(name)
        age = None if q.ts is None else round((at - q.ts).total_seconds(), 1)
        if age is None or age > STALE_SECONDS:
            flags.insert(0, "STALE")
        return ";".join(flags) or "OK", age

    def _row(self, ts, received, kind, label, typ, strike, c, q, atm, note=""):
        status, age = self._status(q, received)
        if kind == "index" and q is not None:
            status = "STALE" if "STALE" in status else ("NO_LTP" if q.ltp is None else "OK")   # no book for an index
        if kind == "index":
            sym = self.md.symbols.get(self.index_id, "")
        elif c is not None:
            sym = self.md.symbols.get(c.instrument_id, "")
        else:
            sym, status, note = "(none listed: %s %s)" % (strike, typ), "MISSING", "no contract listed at this strike"
        return {"ts": ts.isoformat(), "received_at": received.isoformat(), "underlying": self.u,
                "symbol": sym, "kind": kind,
                "expiry": c.expiry.isoformat() if c is not None and c.expiry else None, "strike": strike,
                "option_type": typ, "moneyness": label, "spot": self.spot, "atm_strike": atm,
                "bid": q.bid if q else None, "ask": q.ask if q else None,
                "bid_qty": q.bid_qty if q else None, "ask_qty": q.ask_qty if q else None,
                "ltp": q.ltp if q else None, "volume": q.volume if q else None, "oi": q.oi if q else None,
                "last_trade_time": q.ts.isoformat() if q and q.ts else None, "last_trade_age_s": age,
                "status": status, "note": note,
                "source": "fyers"}

    def record_minute(self, ts):
        """One snapshot labelled `ts` (a minute boundary). Returns rows written (0 if already recorded)."""
        day = ts.date()
        keys = self._keys(day)
        if self.selection is not None and all((ts.isoformat(), self.md.symbols.get(c.instrument_id, ""))
                                              in keys for _, _, _, c in self.selection[1] if c is not None):
            return 0                                   # this minute is already on disk
        try:
            ids = self.request_ids(day)
            if len(ids) == 1:                          # start-up: learn the spot first (one extra call)
                q = self.md.depth_batch(ids).get(self.index_id)
                self.spot = q.ltp if q else None
                ids = self.request_ids(day)
            got = self.md.depth_batch(ids)
            received = self.now()
            self.failures = 0
        except NseBotError as e:
            return self._failed(ts, day, e)
        iq = got.get(self.index_id)
        if iq is not None and iq.ltp is not None:
            self.spot = iq.ltp
        rows = [self._row(ts, received, "index", "", None, None, None, iq, None)]
        if self.spot is None:
            rows[0]["note"] = "no NIFTY spot: options not selectable this minute"
            return self._write(day, rows)
        atm, sel = self.select(self.spot, day)
        self.selection = (atm, sel)
        for label, typ, strike, c in sel:
            q = got.get(c.instrument_id) if c is not None else None
            note = "" if q is not None or c is None else "not returned by FYERS /depth"
            rows.append(self._row(ts, received, "option", label, typ, strike, c, q, atm, note))
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
                 "note": "request failed: %s" % msg, "source": "fyers"}]
        if self.selection is not None:
            atm, sel = self.selection
            for label, typ, strike, c in sel:
                if c is not None:
                    rows.append({"ts": ts.isoformat(), "received_at": rows[0]["received_at"], "underlying": self.u,
                                 "symbol": self.md.symbols.get(c.instrument_id, ""), "kind": "option",
                                 "expiry": c.expiry.isoformat(), "strike": strike, "option_type": typ,
                                 "moneyness": label, "atm_strike": atm, "status": "MISSING",
                                 "note": "request failed: %s" % msg, "source": "fyers"})
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
