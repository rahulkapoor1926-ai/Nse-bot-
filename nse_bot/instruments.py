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
class Future:
    """Index future (instrument_type FUTIDX). Used only as a volume/VWAP source; never traded."""
    instrument_id: int
    symbol: str
    underlying: str
    expiry: dt.date
    lot_size: int
    tick_size: float


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

    kind = "index_options"
    cost_segment = "index_options"

    def label(self):
        return "%s (%s %s %.0f %s)" % (self.symbol, self.underlying, self.expiry, self.strike, self.option_type)

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "symbol": self.symbol,
                "underlying": self.underlying, "expiry": self.expiry.isoformat(),
                "strike": self.strike, "option_type": self.option_type,
                "lot_size": self.lot_size, "tick_size": self.tick_size,
                "freeze_quantity": self.freeze_quantity}


@dataclass(frozen=True)
class Stock:
    """An NSE cash-market share or ETF. Quantities are shares (lot_size 1)."""
    instrument_id: int
    symbol: str
    name: str
    series: str
    tick_size: float
    kind: str                  # "equity" or "etf", as declared in the universe file
    lot_size: int = 1
    freeze_quantity: int = 0
    expiry: object = None
    option_type: object = None
    strike: object = None
    cost_segment: str = "equity_intraday"

    @property
    def underlying(self):
        return self.symbol

    def label(self):
        return "%s (%s, %s)" % (self.symbol, self.kind.upper(), self.name or "NSE")

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "symbol": self.symbol, "name": self.name,
                "series": self.series, "kind": self.kind, "tick_size": self.tick_size, "lot_size": 1}


class Master(object):
    def __init__(self, contracts, indices, rejected, cash=None, futures=None):
        self.futures = futures or []       # list[Future] (index futures, for volume and VWAP only)
        self.contracts = contracts         # list[Contract]
        self.indices = indices             # {company_name: instrument_id}
        self.rejected = rejected           # [(line_no, reason)]
        self.cash = cash or {}             # {symbol: row dict} NSE cash instruments (EQ series)
        self.stocks = {}                   # {symbol: Stock} after bind_universe()

    def bind_universe(self, universe):
        """Resolve the universe file's symbols against the master. Unknown symbols raise DataError."""
        missing = [u["symbol"] for u in universe if u["symbol"] not in self.cash]
        if missing:
            raise DataError("universe symbols not found as NSE EQ instruments in the scrip master: %s"
                            % ", ".join(missing))
        self.stocks = {}
        for u in universe:
            r = self.cash[u["symbol"]]
            self.stocks[u["symbol"]] = Stock(r["id"], u["symbol"], r["name"], r["series"], r["tick"], u["kind"])
        return self.stocks

    def index_id(self, name):
        if name not in self.indices:
            raise DataError("index %r not found in the scrip master" % name)
        return self.indices[name]

    def by_id(self, iid):
        for c in self.contracts:
            if c.instrument_id == iid:
                return c
        for st in self.stocks.values():
            if st.instrument_id == iid:
                return st
        return None

    def current_future(self, underlying, day):
        """The nearest-expiry index future on or after `day`, or None (callers fail closed)."""
        live = sorted((f for f in self.futures if f.underlying == underlying and f.expiry >= day),
                      key=lambda f: f.expiry)
        return live[0] if live else None

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
    contracts, indices, rejected, seen, cash, futures = [], {}, [], set(), {}, []
    for n, row in enumerate(reader, 2):
        itype = row["instrument_type"]
        if (itype in ("STK", "ETF") and row["exchange"] == "NSE" and row["segment"] == "NSE"
                and row.get("series", "EQ") == "EQ" and row["symbol"]):
            try:
                tick = float(row["tick_size"])
                if tick <= 0:
                    raise ValueError("tick")
                cash[row["symbol"]] = {"id": int(row["id"]), "name": row["company_name"],
                                       "series": row.get("series", ""), "tick": tick}
            except ValueError:
                rejected.append((n, "cash instrument with bad id or tick size"))
            continue
        if itype == "IDX" and row["company_name"] in index_names and row["exchange"] == "NSE":
            try:
                indices[row["company_name"]] = int(row["id"])
            except ValueError:
                rejected.append((n, "index id is not an integer"))
            continue
        if itype == "FUTIDX" and row["segment"] == "NFO" and row["underlying_symbol"] in underlyings:
            try:
                futures.append(Future(int(row["id"]), row["symbol"], row["underlying_symbol"],
                                      dt.datetime.strptime(row["expiry_date"], "%d-%m-%Y").date(),
                                      int(row["lot_size"]), float(row["tick_size"])))
            except (ValueError, TypeError) as e:
                rejected.append((n, "unparseable future: %s" % e))
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
    return Master(contracts, indices, rejected, cash, futures)


def load(path, underlyings=("NIFTY", "BANKNIFTY"), index_names=()):
    try:
        with open(path, encoding="utf-8") as f:
            return parse(f.read(), underlyings, index_names)
    except OSError as e:
        raise DataError("cannot read scrip master %s: %s" % (path, e))


def load_universe(path):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            syms = json.load(f)["symbols"]
    except (OSError, ValueError, KeyError) as e:
        raise DataError("cannot read universe file %s: %s" % (path, e))
    seen = set()
    for u in syms:
        if not isinstance(u, dict) or not u.get("symbol") or u.get("kind") not in ("equity", "etf"):
            raise DataError("universe entry %r needs a symbol and kind equity or etf" % (u,))
        if u["symbol"] in seen:
            raise DataError("universe lists %s twice" % u["symbol"])
        seen.add(u["symbol"])
    return syms
