"""Quotes and quote-quality checks.

A missing price is None, never 0. A quote is usable only if check() returns no
problems; callers refuse to trade (or to mark a position) on anything else.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from .clock import from_epoch_ms, to_ist
from .errors import DataError

FUTURE_SKEW_SECONDS = 5


@dataclass
class Quote:
    instrument_id: int
    ltp: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    bid_qty: Optional[int]
    ask_qty: Optional[int]
    ts: Optional[dt.datetime]           # exchange time of the last trade/update, aware
    volume: Optional[int] = None
    oi: Optional[int] = None
    dpr_low: Optional[float] = None
    dpr_high: Optional[float] = None
    source: str = ""
    extra: dict = field(default_factory=dict)

    def mid(self):
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    def spread_pct(self):
        m = self.mid()
        if m is None or m <= 0:
            return None
        return (self.ask - self.bid) / m * 100.0


def _paise(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise DataError("price %r is not integer paise" % (v,))
    return v / 100.0


def from_sahi(iid, d, source="sahi"):
    """One entry of GET /v1/marketdata/quotes `data` (integer paise, epoch ms)."""
    if d is None:
        raise DataError("SAHI returned null for instrument %s (unknown instrument)" % iid)
    buys, sells = d.get("best_buys") or [], d.get("best_sells") or []
    ts = d.get("last_trade_time")
    return Quote(
        instrument_id=int(iid),
        ltp=_paise(d.get("last_trade_price")),
        bid=_paise(buys[0]["price"]) if buys else None,
        ask=_paise(sells[0]["price"]) if sells else None,
        bid_qty=int(buys[0]["qty"]) if buys else None,
        ask_qty=int(sells[0]["qty"]) if sells else None,
        ts=from_epoch_ms(ts) if ts is not None else None,
        volume=d.get("volume_today"),
        oi=d.get("open_interest"),
        dpr_low=_paise(d.get("dpr_lower")),
        dpr_high=_paise(d.get("dpr_upper")),
        source=source,
    )


def check(q, now, cfg, lot_size, received_at=None):
    """Return a list of problems; empty means usable for entry decisions.

    received_at: when our process received the quote. Staleness uses the later of the
    exchange timestamp and receipt only if the exchange timestamp is present; a quote
    with no timestamp at all is stale.
    """
    now = to_ist(now)
    p = []
    if q is None:
        return ["no quote"]
    for name in ("bid", "ask"):
        v = getattr(q, name)
        if v is None:
            p.append("missing %s" % name)
        elif v <= 0:
            p.append("non-positive %s %.2f" % (name, v))
    if q.ts is None:
        p.append("no timestamp: treated as stale")
    else:
        age = (now - q.ts).total_seconds()
        if age > cfg["max_quote_age_seconds"]:
            p.append("stale: %.0fs old (max %ss)" % (age, cfg["max_quote_age_seconds"]))
        elif age < -FUTURE_SKEW_SECONDS:
            p.append("timestamp %.0fs in the future: clock or feed problem" % -age)
    if p:
        return p
    if q.bid > q.ask:
        p.append("crossed quote: bid %.2f > ask %.2f" % (q.bid, q.ask))
        return p
    sp = q.spread_pct()
    if sp is not None and sp > cfg["max_spread_pct"]:
        p.append("spread %.2f%% wider than %.2f%%" % (sp, cfg["max_spread_pct"]))
    need = cfg["min_top_qty_lots"] * lot_size
    if q.bid_qty is None or q.bid_qty < need:
        p.append("thin bid: %s units < %d" % (q.bid_qty, need))
    if q.ask_qty is None or q.ask_qty < need:
        p.append("thin ask: %s units < %d" % (q.ask_qty, need))
    if q.volume is None or q.volume < cfg["min_volume_today"]:
        p.append("volume today %s < %d" % (q.volume, cfg["min_volume_today"]))
    if q.oi is None or q.oi < cfg["min_open_interest"]:
        p.append("open interest %s < %d" % (q.oi, cfg["min_open_interest"]))
    if q.ltp is not None and q.dpr_low is not None and q.dpr_high is not None:
        if not q.dpr_low <= q.ltp <= q.dpr_high:
            p.append("last price %.2f outside the daily price band %.2f-%.2f" % (q.ltp, q.dpr_low, q.dpr_high))
    return p


def usable_for_exit(q, now, cfg):
    """Exit decisions need a fresh bid only. Returns (bid or None, problems)."""
    if q is None:
        return None, ["no quote"]
    p = []
    if q.bid is None or q.bid <= 0:
        p.append("no valid bid")
    if q.ts is None:
        p.append("no timestamp")
    else:
        age = (to_ist(now) - q.ts).total_seconds()
        if age > cfg["max_quote_age_seconds"]:
            p.append("stale: %.0fs old" % age)
    return (None if p else q.bid), p
