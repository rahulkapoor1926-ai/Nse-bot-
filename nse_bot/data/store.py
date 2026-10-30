"""Historical dataset loader with provenance checks.

A dataset is a folder:
  manifest.json   {"name", "source", "synthetic": bool, "interval_minutes",
                   "files": {"underlying.csv": "<sha256>", "options.csv": "<sha256>"}}
  underlying.csv  ts,underlying,open,high,low,close
  options.csv     ts,underlying,expiry,strike,option_type,open,high,low,close,volume,oi,
                  bid,ask,bid_qty,ask_qty,lot_size,tick_size

`ts` is the bar START, ISO 8601 with offset. bid/ask/bid_qty/ask_qty are the
quote snapshot at the bar's CLOSE; empty means unknown (never zero).

Loading refuses: files whose hash differs from the manifest, rows that repeat
a key, bars outside the NSE session, and prices that are missing or
inconsistent. Each rejected row is counted; nothing is patched or invented.
"""

import csv
import datetime as dt
import hashlib
import json
import os
from collections import defaultdict

from ..clock import IST, SESSION_CLOSE, SESSION_OPEN
from ..errors import DataError

U_COLS = ("ts", "underlying", "open", "high", "low", "close")
O_COLS = ("ts", "underlying", "expiry", "strike", "option_type", "open", "high", "low", "close",
          "volume", "oi", "bid", "ask", "bid_qty", "ask_qty", "lot_size", "tick_size")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _f(v):
    if v is None or v == "":
        return None
    x = float(v)
    if x != x:
        raise ValueError("NaN")
    return x


def _i(v):
    x = _f(v)
    return None if x is None else int(x)


def _ts(v):
    t = dt.datetime.fromisoformat(v)
    if t.tzinfo is None:
        raise ValueError("timestamp %r has no offset" % v)
    return t.astimezone(IST)


def _in_session(t, minutes):
    start = t.timetz().replace(tzinfo=None)
    end = (t + dt.timedelta(minutes=minutes)).timetz().replace(tzinfo=None)
    return SESSION_OPEN <= start and end <= SESSION_CLOSE


class Dataset(object):
    def __init__(self, manifest, underlying, options, rejected, digest):
        self.manifest = manifest
        self.synthetic = bool(manifest.get("synthetic"))
        self.interval = int(manifest["interval_minutes"])
        self.underlying = underlying     # {u: {day: [bar tuples]}}
        self.options = options           # {(u, day): {(expiry, strike, type): {ts: row}}}
        self.rejected = rejected         # {"reason": count}
        self.digest = digest             # hash of manifest + file hashes

    def days(self, u):
        return sorted(self.underlying.get(u, {}))

    def underlyings(self):
        return sorted(self.underlying)

    def candle_count(self):
        return sum(len(b) for d in self.underlying.values() for b in d.values())


def load(folder):
    mpath = os.path.join(folder, "manifest.json")
    try:
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise DataError("dataset %s has no readable manifest.json: %s" % (folder, e))
    for k in ("name", "source", "synthetic", "interval_minutes", "files"):
        if k not in manifest:
            raise DataError("manifest is missing %s" % k)
    if not isinstance(manifest["synthetic"], bool):
        raise DataError("manifest.synthetic must be true or false")
    for name in ("underlying.csv", "options.csv"):
        want = manifest["files"].get(name)
        path = os.path.join(folder, name)
        if not want:
            raise DataError("manifest has no hash for %s" % name)
        if not os.path.exists(path):
            raise DataError("dataset file missing: %s" % path)
        got = sha256_file(path)
        if got != want:
            raise DataError("%s hash %s does not match the manifest (%s): file changed or corrupt"
                            % (name, got[:12], want[:12]))
    minutes = int(manifest["interval_minutes"])
    rejected = defaultdict(int)

    underlying = defaultdict(lambda: defaultdict(list))
    seen = set()
    with open(os.path.join(folder, "underlying.csv"), encoding="utf-8") as f:
        r = csv.DictReader(f)
        if any(c not in (r.fieldnames or []) for c in U_COLS):
            raise DataError("underlying.csv needs columns %s" % ", ".join(U_COLS))
        for row in r:
            try:
                t = _ts(row["ts"])
                o, h, l, c = (_f(row[k]) for k in ("open", "high", "low", "close"))
            except ValueError:
                rejected["underlying: unparseable"] += 1
                continue
            key = (row["underlying"], t)
            if None in (o, h, l, c) or not (0 < l <= min(o, c) and h >= max(o, c)):
                rejected["underlying: missing or inconsistent price"] += 1
            elif key in seen:
                rejected["underlying: duplicate bar"] += 1
            elif not _in_session(t, minutes):
                rejected["underlying: outside session"] += 1
            else:
                seen.add(key)
                underlying[row["underlying"]][t.date()].append((t, o, h, l, c))
    for u in underlying:
        for d in underlying[u]:
            underlying[u][d].sort()

    options = defaultdict(lambda: defaultdict(dict))
    with open(os.path.join(folder, "options.csv"), encoding="utf-8") as f:
        r = csv.DictReader(f)
        if any(c not in (r.fieldnames or []) for c in O_COLS):
            raise DataError("options.csv needs columns %s" % ", ".join(O_COLS))
        for row in r:
            try:
                t = _ts(row["ts"])
                rec = {
                    "ts": t, "expiry": dt.date.fromisoformat(row["expiry"]),
                    "strike": _f(row["strike"]), "type": row["option_type"],
                    "open": _f(row["open"]), "high": _f(row["high"]), "low": _f(row["low"]),
                    "close": _f(row["close"]), "volume": _i(row["volume"]), "oi": _i(row["oi"]),
                    "bid": _f(row["bid"]), "ask": _f(row["ask"]),
                    "bid_qty": _i(row["bid_qty"]), "ask_qty": _i(row["ask_qty"]),
                    "lot_size": _i(row["lot_size"]), "tick_size": _f(row["tick_size"]),
                }
            except ValueError:
                rejected["options: unparseable"] += 1
                continue
            if rec["type"] not in ("CE", "PE") or not rec["strike"] or not rec["lot_size"] or not rec["tick_size"]:
                rejected["options: bad contract fields"] += 1
                continue
            if rec["expiry"] < t.date():
                rejected["options: bar after expiry"] += 1
                continue
            if not _in_session(t, minutes):
                rejected["options: outside session"] += 1
                continue
            bucket = options[(row["underlying"], t.date())][(rec["expiry"], rec["strike"], rec["type"])]
            if t in bucket:
                rejected["options: duplicate bar"] += 1
                continue
            bucket[t] = rec
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return Dataset(manifest, {u: dict(v) for u, v in underlying.items()},
                   {k: dict(v) for k, v in options.items()}, dict(rejected), digest)


def write_manifest(folder, name, source, synthetic, interval_minutes):
    """Hash the two CSVs and write manifest.json (used by importers and test fixtures)."""
    files = {n: sha256_file(os.path.join(folder, n)) for n in ("underlying.csv", "options.csv")}
    doc = {"name": name, "source": source, "synthetic": synthetic,
           "interval_minutes": interval_minutes, "files": files}
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    return doc


# ---------------------------------------------------------------------------
# Stocks and ETFs
#
# Folder with manifest.json {"name", "source", "synthetic", "interval_minutes",
# "kind": "equity", "fill_model": "quotes" | "estimated_spread",
# "estimated_spread_pct" (only for estimated_spread), "files": {"equities.csv": sha}}
# equities.csv: ts,symbol,kind,open,high,low,close,volume,bid,ask,bid_qty,ask_qty,tick_size
# With fill_model "quotes" bid/ask are the snapshot at the bar close (empty = unknown).
# With "estimated_spread" bid/ask are absent and fills use close -/+ half the spread,
# which the gate accepts only when the config allows it.

E_COLS = ("ts", "symbol", "kind", "open", "high", "low", "close", "volume", "bid", "ask",
          "bid_qty", "ask_qty", "tick_size")
FILL_MODELS = ("quotes", "estimated_spread")
MIN_ESTIMATED_SPREAD_PCT = 0.05


class EquityDataset(object):
    def __init__(self, manifest, bars, kinds, rejected, digest):
        self.manifest = manifest
        self.synthetic = bool(manifest.get("synthetic"))
        self.interval = int(manifest["interval_minutes"])
        self.fill_model = manifest["fill_model"]
        self.spread_pct = manifest.get("estimated_spread_pct")
        self.bars = bars            # {symbol: {day: [row dicts sorted by ts]}}
        self.kinds = kinds          # {symbol: "equity" | "etf"}
        self.rejected = rejected
        self.digest = digest

    def symbols(self):
        return sorted(self.bars)

    def candle_count(self):
        return sum(len(b) for d in self.bars.values() for b in d.values())


def load_equity(folder):
    mpath = os.path.join(folder, "manifest.json")
    try:
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise DataError("dataset %s has no readable manifest.json: %s" % (folder, e))
    for k in ("name", "source", "synthetic", "interval_minutes", "files", "fill_model"):
        if k not in manifest:
            raise DataError("manifest is missing %s" % k)
    if manifest.get("kind") != "equity":
        raise DataError("manifest kind must be 'equity' for a stock/ETF dataset")
    if manifest["fill_model"] not in FILL_MODELS:
        raise DataError("fill_model must be one of %s" % ", ".join(FILL_MODELS))
    if manifest["fill_model"] == "estimated_spread":
        sp = manifest.get("estimated_spread_pct")
        if not isinstance(sp, (int, float)) or sp < MIN_ESTIMATED_SPREAD_PCT:
            raise DataError("estimated_spread_pct must be at least %.2f%%" % MIN_ESTIMATED_SPREAD_PCT)
    path = os.path.join(folder, "equities.csv")
    want = manifest["files"].get("equities.csv")
    if not want or not os.path.exists(path):
        raise DataError("dataset needs equities.csv listed in the manifest")
    if sha256_file(path) != want:
        raise DataError("equities.csv does not match its manifest hash: file changed or corrupt")
    minutes = int(manifest["interval_minutes"])
    rejected = defaultdict(int)
    bars = defaultdict(lambda: defaultdict(dict))
    kinds = {}
    with open(path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        if any(c not in (r.fieldnames or []) for c in E_COLS):
            raise DataError("equities.csv needs columns %s" % ", ".join(E_COLS))
        for row in r:
            try:
                t = _ts(row["ts"])
                rec = {"ts": t, "open": _f(row["open"]), "high": _f(row["high"]), "low": _f(row["low"]),
                       "close": _f(row["close"]), "volume": _i(row["volume"]), "bid": _f(row["bid"]),
                       "ask": _f(row["ask"]), "bid_qty": _i(row["bid_qty"]), "ask_qty": _i(row["ask_qty"]),
                       "tick_size": _f(row["tick_size"])}
            except ValueError:
                rejected["unparseable"] += 1
                continue
            o, h, l, c = rec["open"], rec["high"], rec["low"], rec["close"]
            if row["kind"] not in ("equity", "etf") or not row["symbol"] or not rec["tick_size"]:
                rejected["bad symbol, kind or tick size"] += 1
            elif None in (o, h, l, c) or not (0 < l <= min(o, c) and h >= max(o, c)):
                rejected["missing or inconsistent price"] += 1
            elif not _in_session(t, minutes):
                rejected["outside session"] += 1
            elif t in bars[row["symbol"]][t.date()]:
                rejected["duplicate bar"] += 1
            else:
                bars[row["symbol"]][t.date()][t] = rec
                kinds[row["symbol"]] = row["kind"]
    out = {s: {d: [v[t] for t in sorted(v)] for d, v in days.items()} for s, days in bars.items()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return EquityDataset(manifest, out, kinds, dict(rejected), digest)


def write_equity_manifest(folder, name, source, synthetic, interval_minutes, fill_model, spread_pct=None):
    doc = {"name": name, "source": source, "synthetic": synthetic, "kind": "equity",
           "interval_minutes": interval_minutes, "fill_model": fill_model,
           "files": {"equities.csv": sha256_file(os.path.join(folder, "equities.csv"))}}
    if spread_pct is not None:
        doc["estimated_spread_pct"] = spread_pct
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    return doc
