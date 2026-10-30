"""The validation gate: APPROVED, REJECTED or INSUFFICIENT DATA for one strategy on one dataset.

Order of checks (the first that fails decides):
  1. synthetic dataset                         -> REJECTED (unless a test passes allow_synthetic)
  2. valid independent examples < gate_min_examples (>= 10,000)  -> INSUFFICIENT DATA
  3. unresolved trades > 1% of valid examples  -> REJECTED (data quality)
  4. out-of-sample executed trades < gate_min_oos_trades -> INSUFFICIENT DATA
  5. validation expectancy <= 0                -> REJECTED
  6. sensitivity: < 70% of neighbouring parameter sets keep a positive train expectancy -> REJECTED
  7. out-of-sample expectancy CI lower bound <= 0 -> REJECTED
  8. out-of-sample expectancy <= coin-flip baseline expectancy -> REJECTED
  9. the untouched test period has already been evaluated for more than
     MAX_TEST_STRATEGIES different strategies on this dataset -> REJECTED
Otherwise APPROVED. Re-running the same strategy on the same dataset adds no
examples: counts come from one pass over distinct bars.

APPROVED means only "passed these historical checks". It is not evidence of
future profit.
"""

import hashlib
import json
import os

from . import state
from .backtest import engine, metrics

MAX_TEST_STRATEGIES = 3
MAX_UNRESOLVED_FRACTION = 0.01
SENSITIVITY_KEEP = 0.7
DISCLAIMER = ("SIMULATED results on historical data. A large sample and a passing gate are not "
              "evidence of future profitability. Live fills, slippage and gaps can be worse.")


def strategy_hash(strategy):
    return hashlib.sha256(json.dumps(strategy, sort_keys=True).encode()).hexdigest()[:16]


def neighbours(strategy):
    """Each tunable parameter moved down and up by about 20%, one at a time."""
    out = []
    step = strategy["interval_minutes"]
    for key in ("stop_pct", "target_r"):
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
            if key.endswith("_minutes"):
                nv = max(step, int(round(nv / step)) * step)
                if nv == v:
                    nv = v + (step if f > 1 else -step)
                    if nv < step:
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
           "synthetic_data": ds.synthetic, "disclaimer": DISCLAIMER, "reasons": []}

    def done(status, reason=None):
        doc["status"] = status
        if reason:
            doc["reasons"].append(reason)
        state.write_json(os.path.join(state_dir, "gate", "%s.json" % shash), doc)
        return doc

    if ds.synthetic and not allow_synthetic:
        return done("REJECTED", "dataset is marked synthetic; synthetic data can never approve a signal")

    days = engine.all_days(ds, cfg["underlyings"])
    full = engine.run(ds, strategy, cfg, costs, cal)
    c = full["counts"]
    doc["counts"] = {"candles": c.get("candles", 0), "raw_overlapping_occurrences": c.get("raw_occurrences", 0),
                     "independent_examples": c.get("independent", 0), "valid_examples": c.get("valid", 0),
                     "executed_trades": c.get("executed", 0), "unresolved_trades": full["unresolved"]}
    doc["invalid_reasons"] = full["invalid"]
    doc["rejected_rows"] = ds.rejected
    need = cfg["gate_min_examples"]
    if c.get("valid", 0) < need:
        return done("INSUFFICIENT DATA", "%d valid independent examples; %d required"
                    % (c.get("valid", 0), need))
    if full["unresolved"] > MAX_UNRESOLVED_FRACTION * c["valid"]:
        return done("REJECTED", "%d trades could not be closed in the data (> %.0f%%)"
                    % (full["unresolved"], MAX_UNRESOLVED_FRACTION * 100))

    split = engine.split_days(days)
    doc["periods"] = {k: [str(v[0]), str(v[-1])] if v else None for k, v in split.items()}
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

    # Only now touch the untouched test period, and log that we did.
    reuse = _log_test_access(state_dir, ds.digest, shash)
    test = engine.run(ds, strategy, cfg, costs, cal, days=set(split["test"]))
    base = engine.run(ds, strategy, cfg, costs, cal, days=set(split["test"]), direction="coin")
    doc["test"] = metrics.compute(test["trades"])
    doc["test_by_regime"] = metrics.by(test["trades"], "regime")
    doc["test_by_underlying"] = metrics.by(test["trades"], "underlying")
    doc["baseline_test"] = metrics.compute(base["trades"])
    doc["baseline"] = "same signal times and strike rule, direction by a seeded coin flip"
    doc["test_period_strategies_evaluated"] = reuse

    if test["counts"].get("executed", 0) < cfg["gate_min_oos_trades"]:
        return done("INSUFFICIENT DATA", "only %d out-of-sample trades; %d required"
                    % (test["counts"].get("executed", 0), cfg["gate_min_oos_trades"]))
    if doc["validation"].get("expectancy", 0) <= 0:
        return done("REJECTED", "validation expectancy after costs is not positive")
    pos = sum(1 for s in sens if s["expectancy"] is not None and s["expectancy"] > 0)
    if not sens or pos / len(sens) < SENSITIVITY_KEEP:
        return done("REJECTED", "fragile: only %d of %d neighbouring parameter sets are profitable in training"
                    % (pos, len(sens)))
    lo = doc["test"]["expectancy_ci95"][0]
    if lo is None or lo <= 0:
        return done("REJECTED", "out-of-sample expectancy 95%% CI lower bound %s is not above zero" % lo)
    if doc["test"]["expectancy"] <= doc["baseline_test"].get("expectancy", float("-inf")):
        return done("REJECTED", "out-of-sample expectancy does not beat the coin-flip baseline")
    if reuse > MAX_TEST_STRATEGIES:
        return done("REJECTED", "the test period has been used to evaluate %d strategies; it is no longer "
                    "untouched. Collect a fresh out-of-sample period." % reuse)
    return done("APPROVED")


def status_for(state_dir, strategy, dataset_digest=None):
    """The stored decision for this exact strategy, or 'NOT EVALUATED'. Used by the risk check."""
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
    return doc.get("status", "NOT EVALUATED")
