"""Configuration loading and validation. Fails closed.

Every key is required, unknown keys are rejected, and a few safety floors
cannot be lowered from the file at all (the 10,000-example gate, paper mode).
There are no silent defaults: a missing field is a ConfigError, not a guess.
"""

import json
import re

from .errors import ConfigError

UNDERLYINGS = ("NIFTY", "BANKNIFTY")
MIN_GATE_EXAMPLES = 10000            # the user's floor; the file may raise it, never lower it
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _num(lo=None, hi=None, integer=False):
    def check(key, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError("%s must be a number, got %r" % (key, v))
        if integer and int(v) != v:
            raise ConfigError("%s must be a whole number, got %r" % (key, v))
        if v != v:
            raise ConfigError("%s is NaN" % key)
        if lo is not None and v < lo:
            raise ConfigError("%s must be >= %s, got %s" % (key, lo, v))
        if hi is not None and v > hi:
            raise ConfigError("%s must be <= %s, got %s" % (key, hi, v))
    return check


def _const(value):
    def check(key, v):
        if v != value:
            raise ConfigError("%s must be %r in this phase, got %r" % (key, value, v))
    return check


def _choice(*allowed):
    def check(key, v):
        if v not in allowed:
            raise ConfigError("%s must be one of %s, got %r" % (key, ", ".join(allowed), v))
    return check


def _hhmm(key, v):
    if not isinstance(v, str) or not HHMM.match(v):
        raise ConfigError("%s must be HH:MM (24-hour IST), got %r" % (key, v))


def _str(key, v):
    if not isinstance(v, str) or not v.strip():
        raise ConfigError("%s must be a non-empty string" % key)


def _bool(key, v):
    if not isinstance(v, bool):
        raise ConfigError("%s must be true or false, got %r" % (key, v))


def _underlyings(key, v):
    if not isinstance(v, list) or not v:
        raise ConfigError("%s must be a non-empty list" % key)
    for u in v:
        if u not in UNDERLYINGS:
            raise ConfigError("%s: %r is not supported (only %s)" % (key, u, ", ".join(UNDERLYINGS)))
    if len(set(v)) != len(v):
        raise ConfigError("%s has duplicates" % key)


KINDS = ("equity", "etf", "index_options")


def _kinds(key, v):
    if not isinstance(v, list) or not v or any(k not in KINDS for k in v) or len(set(v)) != len(v):
        raise ConfigError("%s must be a non-empty list drawn from %s" % (key, ", ".join(KINDS)))


def _index_names(key, v):
    if not isinstance(v, dict):
        raise ConfigError("%s must be an object" % key)
    for u, name in v.items():
        if u not in UNDERLYINGS or not isinstance(name, str) or not name:
            raise ConfigError("%s: bad entry %r: %r" % (key, u, name))


SCHEMA = {
    "mode": _const("paper"),
    "timezone": _const("Asia/Kolkata"),
    "currency": _const("INR"),
    "underlyings": _underlyings,
    "index_names": _index_names,
    "capital_inr": _num(lo=1),
    "max_daily_loss_inr": _num(lo=1),
    "daily_profit_stop_inr": _num(lo=1),
    "max_risk_per_trade_inr": _num(lo=1),
    "max_trades_per_day": _num(lo=1, hi=50, integer=True),
    "max_open_positions": _num(lo=1, hi=5, integer=True),
    "max_lots_per_trade": _num(lo=1, integer=True),
    "max_premium_outlay_pct": _num(lo=1, hi=100),
    "min_reward_risk": _num(lo=2.0),     # 1:2 minimum, measured net of all costs (rr.py)
    "no_entries_before": _hhmm,
    "no_entries_after": _hhmm,
    "flat_by": _hhmm,
    "expiry_day_flat_by": _hhmm,
    "min_days_to_expiry": _num(lo=0, integer=True),
    "max_quote_age_seconds": _num(lo=1, hi=300),
    "max_spread_pct": _num(lo=0.1, hi=50),
    "min_top_qty_lots": _num(lo=1, integer=True),
    "min_volume_today": _num(lo=0, integer=True),
    "min_open_interest": _num(lo=0, integer=True),
    "slippage_ticks": _num(lo=0, integer=True),
    "max_consecutive_errors": _num(lo=1, hi=20, integer=True),
    "event_blackout_minutes": _num(lo=0, integer=True),
    "gate_min_examples": _num(lo=MIN_GATE_EXAMPLES, integer=True),
    "gate_min_oos_trades": _num(lo=30, integer=True),
    "state_dir": _str,
    "holidays_file": _str,
    "costs_file": _str,
    "events_file": _str,
    "instruments_enabled": _kinds,
    "equity_universe_file": _str,
    "equity_product": _const("intraday"),
    "equity_min_price_inr": _num(lo=1),
    "equity_min_top_value_inr": _num(lo=0),
    "equity_min_volume_today": _num(lo=0, integer=True),
    "gate_allow_estimated_spread": _bool,
    "live_orders_enabled": _const(False),
    "data_source": _choice("sahi", "fyers"),
    # order-flow confirmation of ORB / sweep entries (orderflow.py); "required" blocks when not confirmed
    "orderflow_confirmation": _choice("required", "off"),
    # forward validation book (forward.py, approved by jojo 2026-11-02): paper entries may go ahead while
    # the backtest gate has INSUFFICIENT DATA / NOT EVALUATED; REJECTED or STALE still block. Own state_dir.
    "forward_validation": _bool,
}


def validate(cfg):
    """Return cfg unchanged if valid, else raise ConfigError listing every problem."""
    if not isinstance(cfg, dict):
        raise ConfigError("configuration must be a JSON object")
    problems = []
    for key in sorted(set(cfg) - set(SCHEMA)):
        problems.append("unknown key %r (refusing to guess what it means)" % key)
    for key in sorted(set(SCHEMA) - set(cfg)):
        problems.append("missing required key %r" % key)
    for key, check in SCHEMA.items():
        if key in cfg:
            try:
                check(key, cfg[key])
            except ConfigError as e:
                problems.append(str(e))
    if not problems:
        problems.extend(_cross_checks(cfg))
    if problems:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return cfg


def _cross_checks(c):
    out = []
    if c["max_risk_per_trade_inr"] > c["max_daily_loss_inr"]:
        out.append("max_risk_per_trade_inr exceeds max_daily_loss_inr")
    if c["max_daily_loss_inr"] >= c["capital_inr"]:
        out.append("max_daily_loss_inr must be below capital_inr")
    if not c["no_entries_before"] < c["no_entries_after"] < c["flat_by"]:
        out.append("need no_entries_before < no_entries_after < flat_by")
    if not ("09:15" <= c["no_entries_before"] and c["flat_by"] <= "15:30"):
        out.append("entry window and flat_by must sit inside the NSE session 09:15-15:30 IST")
    if c["expiry_day_flat_by"] > c["flat_by"]:
        out.append("expiry_day_flat_by must not be later than flat_by")
    if c["forward_validation"] and "forward" not in c["state_dir"].replace("\\", "/").split("/"):
        out.append("forward_validation needs its own state_dir with a 'forward' folder (separate ledger)")
    for u in c["underlyings"]:
        if u not in c["index_names"]:
            out.append("index_names has no entry for %s" % u)
    return out


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except ValueError as e:
        raise ConfigError("config file %s is not valid JSON: %s" % (path, e))
    return validate(raw)
