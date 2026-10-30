"""Typed errors. Every refusal in the package is one of these, never a silent default."""


class NseBotError(Exception):
    """Base class for every error raised by nse_bot."""


class ConfigError(NseBotError):
    """The configuration is missing, malformed, or asks for something not allowed."""


class DataError(NseBotError):
    """Input data is missing, malformed or inconsistent."""


class StateCorrupt(NseBotError):
    """A persisted state file cannot be trusted. Callers must fail closed."""


class CalendarError(NseBotError):
    """The trading calendar cannot answer (for example, no holiday list for that year)."""


class LiveTradingDisabled(NseBotError):
    """Raised by every code path that would send a real order. This phase has none."""
