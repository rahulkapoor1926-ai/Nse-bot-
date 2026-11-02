"""What the live paper bot may trade: NIFTY index options only (approved 2 Nov 2026).

Enforced at three points, so no single change can re-open equity or ETF trading:
  config      instruments_enabled must be ["index_options"], underlyings ["NIFTY"] (config.py)
  signals     a strategy whose instrument is not an index option never scans (bot.enabled) and the
              `paper` command refuses to load it (cli.py)
  risk book   an entry whose kind is not index_options or whose underlying is not NIFTY is blocked,
              and an order that does not say what it is is blocked too (risk.py)
Equity/ETF code stays in the repository for research and backtests only.
"""

UNDERLYINGS = ("NIFTY",)
KIND = "index_options"
STRATEGY_INSTRUMENT = "index_option"


def strategy_allowed(strategy):
    return strategy.get("instrument") == STRATEGY_INSTRUMENT


def order_kind(c):
    """kind of a contract: option contracts carry CE/PE, stocks/ETFs carry kind equity/etf."""
    k = getattr(c, "kind", None)
    if k:
        return k
    return KIND if getattr(c, "option_type", None) in ("CE", "PE") else None


def order_problem(order):
    if order.get("kind") != KIND:
        return "only NIFTY index options are enabled: %s entries are disabled" % (order.get("kind") or "unknown-kind")
    if order.get("underlying") not in UNDERLYINGS:
        return "only NIFTY index options are enabled: underlying %s is not allowed" % order.get("underlying")
    return None
