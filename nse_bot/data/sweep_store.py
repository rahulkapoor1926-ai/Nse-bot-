"""Dataset for sweep_reversal replays: 1-minute index and future bars plus option-chain
snapshots with bid/ask and Greeks, recorded at each minute's close.

Folder:
  manifest.json  {"name", "source", "synthetic": bool, "kind": "sweep", "underlying",
                  "files": {"index_1m.csv": sha256, "future_1m.csv": sha256, "chain_1m.csv": sha256}}
  index_1m.csv   ts,open,high,low,close
  future_1m.csv  ts,symbol,expiry,open,high,low,close,volume
  chain_1m.csv   ts,instrument_id,symbol,expiry,strike,option_type,lot_size,tick_size,
                 bid,ask,bid_qty,ask_qty,volume,oi,iv,delta,gamma,theta,vega

`ts` is the bar START (ISO with offset); a chain row with ts T is the snapshot
taken when the 1m bar starting at T closed. Prices in rupees. Empty = unknown,
never zero. Contracts come from the snapshots themselves (expired ones
included), so a replay never depends on today's scrip master: no
survivorship bias. Hash mismatches refuse the whole dataset.
"""

import csv
import datetime as dt
import hashlib
import json
import os
from collections import defaultdict

from ..errors import DataError
from ..instruments import Contract
from .store import _f, _i, _in_session, _ts, sha256_file

FILES = {
    "index_1m.csv": ("ts", "open", "high", "low", "close"),
    "future_1m.csv": ("ts", "symbol", "expiry", "open", "high", "low", "close", "volume"),
    "chain_1m.csv": ("ts", "instrument_id", "symbol", "expiry", "strike", "option_type", "lot_size", "tick_size",
                     "bid", "ask", "bid_qty", "ask_qty", "volume", "oi", "iv", "delta", "gamma", "theta", "vega"),
}


class SweepDataset(object):
    kind = "sweep"

    def __init__(self, manifest, u1, f1, chains, contracts, rejected, digest):
        self.manifest = manifest
        self.synthetic = bool(manifest.get("synthetic"))
        self.interval = 1
        self.underlying_name = manifest["underlying"]
        self.u1 = u1                  # sorted bars (t, o, h, l, c, None)
        self.f1 = f1                  # sorted bars (t, o, h, l, c, volume)
        self.chains = chains          # {snapshot time (bar end): [leg dict]}
        self.contracts = contracts    # {instrument_id: Contract}
        self.rejected = rejected
        self.digest = digest

    def days(self):
        return sorted({b[0].date() for b in self.u1})

    def by_id(self, iid):
        return self.contracts.get(int(iid))


def _check_files(folder, manifest):
    for name in FILES:
        want = manifest["files"].get(name)
        path = os.path.join(folder, name)
        if not want or not os.path.exists(path):
            raise DataError("sweep dataset needs %s with a hash in the manifest" % name)
        if sha256_file(path) != want:
            raise DataError("%s does not match its manifest hash: file changed or corrupt" % name)


def _rows(folder, name):
    with open(os.path.join(folder, name), encoding="utf-8") as f:
        r = csv.DictReader(f)
        if any(c not in (r.fieldnames or []) for c in FILES[name]):
            raise DataError("%s needs columns %s" % (name, ", ".join(FILES[name])))
        for row in r:
            yield row


def load(folder):
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise DataError("sweep dataset %s has no readable manifest: %s" % (folder, e))
    for k in ("name", "source", "synthetic", "kind", "underlying", "files"):
        if k not in manifest:
            raise DataError("manifest is missing %s" % k)
    if manifest["kind"] != "sweep" or not isinstance(manifest["synthetic"], bool):
        raise DataError("manifest kind must be 'sweep' and synthetic a boolean")
    _check_files(folder, manifest)
    rejected = defaultdict(int)

    def bars(name, with_volume):
        out, seen = [], set()
        for row in _rows(folder, name):
            try:
                t = _ts(row["ts"])
                o, h, l, c = (_f(row[k]) for k in ("open", "high", "low", "close"))
                v = _i(row["volume"]) if with_volume else None
            except ValueError:
                rejected[name + ": unparseable"] += 1
                continue
            if None in (o, h, l, c) or not (0 < l <= min(o, c) and h >= max(o, c)):
                rejected[name + ": missing or inconsistent price"] += 1
            elif t in seen:
                rejected[name + ": duplicate bar"] += 1
            elif not _in_session(t, 1):
                rejected[name + ": outside session"] += 1
            else:
                seen.add(t)
                out.append((t, o, h, l, c, v))
        return sorted(out)

    u1, f1 = bars("index_1m.csv", False), bars("future_1m.csv", True)
    chains, contracts = defaultdict(list), {}
    for row in _rows(folder, "chain_1m.csv"):
        try:
            t = _ts(row["ts"])
            leg = {"instrument_id": int(row["instrument_id"]), "option_type": row["option_type"],
                   "strike": _f(row["strike"])}
            for k in ("bid", "ask", "iv", "delta", "gamma", "theta", "vega"):
                leg[k] = _f(row[k])
            for k in ("bid_qty", "ask_qty", "volume", "oi"):
                leg[k] = _i(row[k])
            c = Contract(leg["instrument_id"], row["symbol"], manifest["underlying"],
                         dt.date.fromisoformat(row["expiry"]), leg["strike"], row["option_type"],
                         int(row["lot_size"]), float(row["tick_size"]), 0)
        except (ValueError, TypeError):
            rejected["chain_1m.csv: unparseable"] += 1
            continue
        if c.option_type not in ("CE", "PE") or c.expiry < t.date() or not _in_session(t, 1):
            rejected["chain_1m.csv: bad contract or outside session"] += 1
            continue
        contracts[c.instrument_id] = c
        chains[t + dt.timedelta(minutes=1)].append(leg)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return SweepDataset(manifest, u1, f1, dict(chains), contracts, dict(rejected), digest)


def write_manifest(folder, name, source, synthetic, underlying):
    doc = {"name": name, "source": source, "synthetic": synthetic, "kind": "sweep", "underlying": underlying,
           "files": {n: sha256_file(os.path.join(folder, n)) for n in FILES}}
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    return doc


def is_sweep_dataset(folder):
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            return json.load(f).get("kind") == "sweep"
    except (OSError, ValueError):
        return False
