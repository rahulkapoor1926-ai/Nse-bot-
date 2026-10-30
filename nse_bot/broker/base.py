"""Broker adapter interface.

This phase supports read-only market data and paper fills only. There is no
live order path: LiveOrders.place() raises LiveTradingDisabled whatever it is
given, and no adapter in this package implements an order endpoint.
"""

from abc import ABC, abstractmethod

from ..errors import LiveTradingDisabled


class MarketData(ABC):
    """Read-only market data. Prices returned to callers are rupees (floats), never 0 for 'missing'."""

    @abstractmethod
    def quotes(self, instrument_ids):
        """{instrument_id: nse_bot.quotes.Quote}. Unknown ids are absent, not zero."""

    @abstractmethod
    def candles(self, instrument_id, size, start, end):
        """[(start_ist, open, high, low, close, volume, oi_or_None)] ascending."""

    @abstractmethod
    def expiries(self, underlying_id):
        """[datetime.date] nearest first."""

    @abstractmethod
    def option_chain(self, underlying_id, expiry):
        """The raw chain document, prices converted to rupees."""


class LiveOrders(object):
    """Placeholder that documents the boundary. Live order placement needs a separate review and approval."""

    def place(self, *args, **kwargs):
        raise LiveTradingDisabled("live order placement is disabled in this phase; use the paper broker")

    modify = cancel = place
