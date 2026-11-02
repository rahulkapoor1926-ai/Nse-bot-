"""The validation gate for one strategy on one HISTORICAL dataset: APPROVED, REJECTED or
INSUFFICIENT EVIDENCE (evidence framework decided by jojo on 2026-11-02; it replaces the old
10,000-example requirement).

Order of checks (the first that fails decides):
  1. synthetic dataset -> REJECTED: synthetic data can never approve anything
  2. no genuine option bid/ask history (options), or a stock/ETF dataset without bid/ask unless
     gate_allow_estimated_spread                                    -> INSUFFICIENT EVIDENCE
  3. unresolved trades > 1% of valid examples                      -> REJECTED (data quality)
  4. fewer than 30 out-of-sample trades to be expected (checked BEFORE the test period is touched)
                                                                   -> INSUFFICIENT EVIDENCE
  5. validation expectancy <= 0                                    -> REJECTED
  6. sensitivity: < 70% of neighbouring parameter sets keep a positive train expectancy -> REJECTED
  7. out-of-sample (untouched test period) executed trades < 30    -> INSUFFICIENT EVIDENCE
  8. out-of-sample expectancy CI lower bound <= 0                  -> REJECTED
  9. out-of-sample expectancy <= coin-flip baseline                -> REJECTED
 10. the test period already used for more than MAX_TEST_STRATEGIES strategies -> REJECTED
 11. out-of-sample executed trades < gate_min_oos_trades (100)     -> INSUFFICIENT EVIDENCE
     (INITIAL EVIDENCE: reported, never approved)
Otherwise APPROVED (STRONGER EVIDENCE: 100+ out-of-sample trades).

Every record also reports results per market regime (trending / sideways, high / normal
volatility; regime.py), each with its own evidence level, and the limitations of the
simulation (the live order-flow filter cannot be replayed: no futures depth history exists).
Each record stores the code version, the configuration hash and the cost-schedule hash; a
record from other code, config or costs is STALE (status_for).

APPROVED means only "passed these historical checks". It is not evidence of future profit.
"""

import glob
import hashlib
import json
import os

from . import forward, state
from .backtest import engine as option_engine, equity_engine, metrics, sweep_engine

INSUFFICIENT = "INSUFFICIENT EVIDENCE"
MIN_OOS_INITIAL = forward.MIN_SAMPLE           # 30
ORDERFLOW_LIMITATION = ("order-flow filter NOT SIMULATED: no historical futures depth (bid/ask book) exists, so "
                        "the live order-flow confirmation cannot be replayed. Live trades are a filtered subset of "
                        "these backtested trades; this result does not validate the order-flow filter.")
# configuration keys that never change a simulated trade
_CFG_IGNORE = ("state_dir", "forward_validation", "data_source", "events_file", "holidays_file", "costs_file",
               "equity_universe_file", "notify_label")

MAX_TEST_STRATEGIES = 3
MAX_UNRESOLVED_FRACTION = 0.01
SENSITIVITY_KEEP = 0.7
DISCLAIMER = ("SIMULATED results on historical data. A large sample and a passing gate are not "
              "evidence of future profitability. Live fills, slippage and gaps can be worse.")


def strategy_hash(strategy):
    return hashlib.sha256(json.dumps(strategy, sort_keys=True).encode()).hexdigest()[:16]


def code_version():
    """sha256 of every nse_bot source file: any code change makes an old gate record STALE."""
    root = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for path in sorted(glob.glob(os.path.join(root, "**", "*.py"), recursive=True)):
        h.update(os.path.relpath(path, root).encode())
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def config_hash(cfg):
    return hashlib.sha256(json.dumps({k: v for k, v in sorted(cfg.items()) if k not in _CFG_IGNORE},
                                     sort_keys=True, default=str).encode()).hexdigest()[:16]


def costs_hash(costs):
    return hashlib.sha256(json.dumps(getattr(costs, "by_segment", None), sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def fingerprint(cfg, costs):
    return {"code_version": code_version(), "config_hash": config_hash(cfg), "costs_hash": costs_hash(costs)}


def _genuine_option_quotes(ds):
    """(bars with a recorded bid and ask, all option bars) in an index-option dataset."""
    n = with_q = 0
    for contracts in ds.options.values():
        for bars in contracts.values():
            for rec in bars.values():
                n += 1
                if rec.get("bid") is not None and rec.get("ask") is not None:
                    with_q += 1
    return with_q, n


def _by(trades, key):
    out = metrics.by(trades, key)
    for v in out.values():
        v["evidence"] = forward.evidence(v.get("trades", 0))
    return out


def neighbours(strategy):
    """Each tunable parameter moved down and up by about 20%, one at a time."""
    out = []
    step = strategy["interval_minutes"]
    for key in ("stop_pct", "target_r"):
        if key not in strategy:
            continue
        for f in (0.8, 1.2):
            s = json.loads(json.dumps(strategy))
            s[key] = round(strategy[key] * f, 4)
            out.append(("%s x%.1f" % (key, f), s))
    for key, v in strategy["params"].items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v == 0:
            continue
        for f in (0.8, 1.2):
            s = json.loads(json.dumps(strategy))
            nv = v * f
            if key.endswith("_minutes") and strategy["pattern"] != "sweep_reversal":
                nv = max(step, int(round(nv / step)) * step)          # bar-aligned windows
                if nv == v:
                    nv = v + (step if f > 1 else -step)
                    if nv < step:
                        continue
            elif isinstance(v, int):
                nv = int(round(nv))                                   # counts stay whole numbers
                if nv == v:
                    nv = v + (1 if f > 1 else -1)
                if nv < 1:
                    continue
            s["params"][key] = round(nv, 4)
            out.append(("params.%s x%.1f" % (key, f), s))
    return out


def _log_test_access(state_dir, ds_digest, shash):
    path = os.path.join(state_dir, "gate", "test_access.json")
    doc = state.read_json(path, missing={})
    seen = doc.setdefault(ds_digest, [])
    if shash not in seen:
        seen.append(shash)
    state.write_json(path, doc)
    return len(seen)


def evaluate(ds, strategy, cfg, costs, cal, state_dir, allow_synthetic=False):
    shash = strategy_hash(strategy)
    doc = {"strategy": strategy["name"], "strategy_hash": shash, "dataset": ds.manifest["name"],
           "dataset_digest": ds.digest, "dataset_source": ds.manifest["source"],
           "synthetic_data": ds.synthetic, "disclaimer": DISCLAIMER, "reasons": [], "limitations": []}
    doc.update(fingerprint(cfg, costs))
    if cfg.get("orderflow_confirmation") == "required":
        doc["limitations"].append(ORDERFLOW_LIMITATION)

    def done(status, reason=None):
        doc["status"] = status
        if reason:
            doc["reasons"].append(reason)
        n = (doc.get("test") or {}).get("trades", 0)
        doc["evidence"] = forward.evidence(n) if status != "REJECTED" else "REJECTED"
        state.write_json(os.path.join(state_dir, "gate", "%s.json" % shash), doc)
        return doc

    if ds.synthetic and not allow_synthetic:
        return done("REJECTED", "dataset is marked synthetic; synthetic data can never approve a signal")
    if strategy.get("pattern") == "sweep_reversal" or getattr(ds, "kind", None) == "sweep":
        if strategy.get("pattern") != "sweep_reversal" or getattr(ds, "kind", None) != "sweep":
            return done("REJECTED", "sweep_reversal needs a sweep dataset and a sweep dataset needs sweep_reversal")
        engine = sweep_engine
    else:
        engine = equity_engine if strategy["instrument"] == "equity" else option_engine
    is_equity_ds = hasattr(ds, "fill_model")
    if engine is not sweep_engine and is_equity_ds != (engine is equity_engine):
        return done("REJECTED", "strategy instrument %s does not match the dataset type" % strategy["instrument"])
    if is_equity_ds:
        doc["fill_model"] = ds.fill_model
        if ds.fill_model == "estimated_spread":
            doc["fill_model"] += " (%.2f%% assumed spread, no recorded bid/ask)" % ds.spread_pct
            if not cfg["gate_allow_estimated_spread"]:
                return done(INSUFFICIENT, "no historical bid/ask in this dataset and "
                            "gate_allow_estimated_spread is false")
    if engine is option_engine:
        with_q, n = _genuine_option_quotes(ds)
        doc["option_bars_with_bid_ask"] = [with_q, n]
        if with_q == 0:
            return done(INSUFFICIENT, "no genuine historical option bid/ask in this dataset; index candles or "
                        "option OHLC are never used as execution prices")

    days = engine.all_days(ds, cfg["underlyings"])
    full = engine.run(ds, strategy, cfg, costs, cal)
    c = full["counts"]
    doc["counts"] = {"candles": c.get("candles", 0), "raw_overlapping_occurrences": c.get("raw_occurrences", 0),
                     "independent_examples": c.get("independent", 0), "valid_examples": c.get("valid", 0),
                     "executed_trades": c.get("executed", 0), "unresolved_trades": full["unresolved"],
                     "no_trade_net_reward_risk": c.get("net_rr_no_trade", 0),
                     "no_trade_risk_above_limit": c.get("sizing_no_trade", 0)}
    doc["invalid_reasons"] = full["invalid"]
    doc["rejected_rows"] = ds.rejected
    if c.get("valid", 0) and full["unresolved"] > MAX_UNRESOLVED_FRACTION * c["valid"]:
        return done("REJECTED", "%d trades could not be closed in the data (> %.0f%%)"
                    % (full["unresolved"], MAX_UNRESOLVED_FRACTION * 100))

    split = engine.split_days(days)
    doc["periods"] = {k: [str(v[0]), str(v[-1])] if v else None for k, v in split.items()}
    expected_oos = c.get("executed", 0) * len(split["test"]) / float(len(days) or 1)
    if expected_oos < MIN_OOS_INITIAL:
        return done(INSUFFICIENT, "about %.0f out-of-sample trades to be expected from %d executed trades over %d "
                    "sessions; at least %d are needed (the test period was not touched)"
                    % (expected_oos, c.get("executed", 0), len(days), MIN_OOS_INITIAL))
    train = engine.run(ds, strategy, cfg, costs, cal, days=set(split["train"]))
    val = engine.run(ds, strategy, cfg, costs, cal, days=set(split["validation"]))
    doc["train"] = metrics.compute(train["trades"])
    doc["validation"] = metrics.compute(val["trades"])

    sens = []
    for label, s in neighbours(strategy):
        r = engine.run(ds, s, cfg, costs, cal, days=set(split["train"]))
        m = metrics.compute(r["trades"], ci=False)
        sens.append({"variant": label, "trades": m.get("trades", 0), "expectancy": m.get("expectancy")})
    doc["sensitivity"] = sens
    doc["configurations_tested"] = 1 + len(sens)
    if doc["validation"].get("expectancy", 0) <= 0:
        return done("REJECTED", "validation expectancy after costs is not positive")
    pos = sum(1 for s in sens if s["expectancy"] is not None and s["expectancy"] > 0)
    if not sens or pos / len(sens) < SENSITIVITY_KEEP:
        return done("REJECTED", "fragile: only %d of %d neighbouring parameter sets are profitable in training"
                    % (pos, len(sens)))

    # Only now touch the untouched test period, and log that we did.
    reuse = _log_test_access(state_dir, ds.digest, shash)
    test = engine.run(ds, strategy, cfg, costs, cal, days=set(split["test"]))
    base = engine.run(ds, strategy, cfg, costs, cal, days=set(split["test"]), direction="coin")
    doc["test"] = metrics.compute(test["trades"])
    doc["test_by_regime"] = _by(test["trades"], "regime")
    doc["test_by_trend"] = _by(test["trades"], "trend")
    doc["test_by_volatility"] = _by(test["trades"], "volatility")
    doc["test_by_underlying"] = metrics.by(test["trades"], "underlying")
    doc["baseline_test"] = metrics.compute(base["trades"])
    doc["baseline"] = getattr(engine, "BASELINE", "same signal times and strike rule, direction by a seeded coin flip")
    doc["test_period_strategies_evaluated"] = reuse

    n_oos = test["counts"].get("executed", 0)
    if n_oos < MIN_OOS_INITIAL:
        return done(INSUFFICIENT, "only %d out-of-sample trades; at least %d are needed for an initial evaluation"
                    % (n_oos, MIN_OOS_INITIAL))
    lo = doc["test"]["expectancy_ci95"][0]
    if lo is None or lo <= 0:
        return done("REJECTED", "out-of-sample expectancy 95%% CI lower bound %s is not above zero" % lo)
    if doc["test"]["expectancy"] <= doc["baseline_test"].get("expectancy", float("-inf")):
        return done("REJECTED", "out-of-sample expectancy does not beat the baseline")
    if reuse > MAX_TEST_STRATEGIES:
        return done("REJECTED", "the test period has been used to evaluate %d strategies; it is no longer "
                    "untouched. Collect a fresh out-of-sample period." % reuse)
    if n_oos < cfg["gate_min_oos_trades"]:
        return done(INSUFFICIENT, "INITIAL EVIDENCE only: %d out-of-sample trades pass every check; %d are "
                    "required before approval" % (n_oos, cfg["gate_min_oos_trades"]))
    return done("APPROVED")


def status_for(state_dir, strategy, dataset_digest=None, cfg=None, costs=None):
    """The stored decision for this exact strategy, or 'NOT EVALUATED'. Used by the risk check.
    With cfg (and costs), a record made by other code, config or costs is not trusted: a record that
    approved or rejected becomes STALE; one that had insufficient evidence counts as NOT EVALUATED."""
    try:
        doc = state.read_json(os.path.join(state_dir, "gate", "%s.json" % strategy_hash(strategy)))
    except Exception:
        return "NOT EVALUATED (gate record unreadable)"
    if not doc:
        return "NOT EVALUATED"
    if doc.get("synthetic_data"):
        return "REJECTED (synthetic data)"
    if dataset_digest and doc.get("dataset_digest") != dataset_digest:
        return "STALE (dataset changed since evaluation)"
    status = doc.get("status", "NOT EVALUATED")
    if cfg is not None:
        now = {"code_version": code_version(), "config_hash": config_hash(cfg)}
        if costs is not None:
            now["costs_hash"] = costs_hash(costs)
        changed = [k for k, v in now.items() if doc.get(k) != v]
        if changed:
            if status.startswith(INSUFFICIENT) or status.startswith("INSUFFICIENT"):
                return "NOT EVALUATED (gate record is from other %s)" % ", ".join(changed)
            return "STALE (%s changed since evaluation; was %s)" % (", ".join(changed), status)
    return status
