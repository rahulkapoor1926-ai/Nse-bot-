"""SAHI scrip master -> NIFTY/BANKNIFTY index option contracts.

Format per https://docs.sahi.com/api/v1/reference/scrip-master/ : RFC 4180 CSV,
columns read by header name. Strikes, lot and tick sizes are in rupees and units
exactly as the master gives them; nothing is hard-coded from memory.
"""

import csv
import datetime as dt
import io
from dataclasses import dataclass

from .errors import DataError

REQUIRED = ("id", "symbol", "company_name", "underlying_symbol", "exchange", "segment",
            "instrument_type", "option_type", "expiry_date", "strike_price", "lot_size",
            "tick_size", "freeze_quantity")


@dataclass(frozen=True)
class Contract:
    instrument_id: int
    symbol: str
    underlying: str
    expiry: dt.date
    strike: float
    option_type: str       # CE or PE
    lot_size: int
    tick_size: float
    freeze_quantity: int

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "symbol": self.symbol,
                "underlying": self.underlying, "expiry": self.expiry.isoformat(),
                "strike": self.strike, "option_type": self.option_type,
                "lot_size": self.lot_size, "tick_size": self.tick_size,
                "freeze_quantity": self.freeze_quantity}


class Master(object):
    def __init__(self, contracts, indices, rejected):
        self.contracts = contracts         # list[Contract]
        self.indices = indices             # {company_name: instrument_id}
        self.rejected = rejected           # [(line_no, reason)]

    def index_id(self, name):
        if name not in self.indices:
            raise DataError("index %r not found in the scrip master" % name)
        return self.indices[name]

    def by_id(self, iid):
        for c in self.contracts:
            if c.instrument_id == iid:
                return c
        return None

    def expiries(self, underlying, on_or_after):
        return sorted({c.expiry for c in self.contracts
                       if c.underlying == underlying and c.expiry >= on_or_after})

    def chain(self, underlying, expiry):
        return sorted((c for c in self.contracts if c.underlying == underlying and c.expiry == expiry),
                      key=lambda c: (c.strike, c.option_type))

    def strikes(self, underlying, expiry):
        return sorted({c.strike for c in self.chain(underlying, expiry)})

    def pick(self, underlying, expiry, option_type, spot, offset=0):
        """The contract `offset` strikes away from at-the-money (positive = out of the money)."""
        if spot is None or spot <= 0:
            raise DataError("cannot pick a strike without a valid underlying price")
        strikes = self.strikes(underlying, expiry)
        if not strikes:
            raise DataError("no %s strikes for expiry %s" % (underlying, expiry))
        atm = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i]))
        step = offset if option_type == "CE" else -offset
        i = atm + step
        if not 0 <= i < len(strikes):
            raise DataError("strike offset %d from ATM %.2f is outside the listed chain" % (offset, strikes[atm]))
        for c in self.chain(underlying, expiry):
            if c.strike == strikes[i] and c.option_type == option_type:
                return c
        raise DataError("no %s %s at strike %.2f" % (underlying, option_type, strikes[i]))


def parse(text, underlyings=("NIFTY", "BANKNIFTY"), index_names=()):
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise DataError("scrip master is missing columns: %s" % ", ".join(missing))
    contracts, indices, rejected, seen = [], {}, [], set()
    for n, row in enumerate(reader, 2):
        itype = row["instrument_type"]
        if itype == "IDX" and row["company_name"] in index_names and row["exchange"] == "NSE":
            try:
                indices[row["company_name"]] = int(row["id"])
            except ValueError:
                rejected.append((n, "index id is not an integer"))
            continue
        if itype != "OPTIDX" or row["segment"] != "NFO" or row["underlying_symbol"] not in underlyings:
            continue
        try:
            c = Contract(
                instrument_id=int(row["id"]),
                symbol=row["symbol"],
                underlying=row["underlying_symbol"],
                expiry=dt.datetime.strptime(row["expiry_date"], "%d-%m-%Y").date(),
                strike=float(row["strike_price"]),
                option_type=row["option_type"],
                lot_size=int(row["lot_size"]),
                tick_size=float(row["tick_size"]),
                freeze_quantity=int(row["freeze_quantity"]),
            )
        except (ValueError, TypeError) as e:
            rejected.append((n, "unparseable field: %s" % e))
            continue
        why = None
        if c.option_type not in ("CE", "PE"):
            why = "option_type %r" % c.option_type
        elif c.strike <= 0:
            why = "strike %r" % c.strike
        elif c.lot_size <= 0:
            why = "lot_size %r" % c.lot_size
        elif c.tick_size <= 0:
            why = "tick_size %r" % c.tick_size
        elif c.instrument_id in seen:
            why = "duplicate id %d" % c.instrument_id
        if why:
            rejected.append((n, why))
            continue
        seen.add(c.instrument_id)
        contracts.append(c)
    lots = {}
    for c in contracts:
        lots.setdefault((c.underlying, c.expiry), set()).add(c.lot_size)
    for key, sizes in lots.items():
        if len(sizes) > 1:
            raise DataError("%s %s has mixed lot sizes %s in one expiry" % (key[0], key[1], sorted(sizes)))
    return Master(contracts, indices, rejected)


def load(path, underlyings=("NIFTY", "BANKNIFTY"), index_names=()):
    try:
        with open(path, encoding="utf-8") as f:
            return parse(f.read(), underlyings, index_names)
    except OSError as e:
        raise DataError("cannot read scrip master %s: %s" % (path, e))
