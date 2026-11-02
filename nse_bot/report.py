"""Read-only performance report: paper ledger, risk state and gate decisions.

Opens files for reading only. Every figure is labelled SIMULATED.
"""

import glob
import json
import os

from . import state
from .broker.paper import Ledger
from .errors import StateCorrupt

BANNER = "SIMULATED RESULTS (paper trading / historical backtest). No real orders. Not evidence of future profit."


def build(state_dir, risk_dir=None):
    out = {"label": BANNER, "ledger": {}, "risk_days": [], "gate": []}
    led = Ledger(state_dir)
    try:
        out["ledger"]["records"] = led.verify()
        out["ledger"]["integrity"] = "ok"
        recs = led.records()
        fills = [r for r in recs if r["event"] == "fill"]
        out["ledger"]["fills"] = len(fills)
        out["ledger"]["failed_orders"] = sum(1 for r in recs if r["event"] == "order_failed")
        out["ledger"]["charges_inr"] = round(sum(r["charges"]["total"] for r in fills), 2)
        pos = led.positions()
        out["ledger"]["realized_pnl_inr"] = round(sum(p["realized"] for p in pos.values()), 2)
        out["ledger"]["open_positions"] = {k: p["qty"] for k, p in pos.items() if p["qty"]}
        from . import dashboard
        out["ledger"]["by_strategy"] = dashboard.by_strategy(dashboard._trades(fills, {})[0])
    except StateCorrupt as e:
        out["ledger"]["integrity"] = "CORRUPT: %s" % e
    for path in sorted(glob.glob(os.path.join(risk_dir or state_dir, "risk", "*.json"))):
        try:
            d = state.read_json(path)
            out["risk_days"].append({"date": d["date"], "realized_pnl": d["realized_pnl"], "trades": d["trades"],
                                     "locked": d["lockout"]["locked"], "lock_reason": d["lockout"]["reason"]})
        except (StateCorrupt, KeyError, TypeError) as e:
            out["risk_days"].append({"file": path, "error": str(e)})
    for path in sorted(glob.glob(os.path.join(state_dir, "gate", "*.json"))):
        if path.endswith("test_access.json"):
            continue
        try:
            d = state.read_json(path)
            out["gate"].append({k: d.get(k) for k in ("strategy", "status", "reasons", "counts", "test",
                                                         "baseline_test", "synthetic_data", "dataset")})
        except StateCorrupt as e:
            out["gate"].append({"file": path, "error": str(e)})
    return out


def render(doc):
    lines = ["=" * len(BANNER), BANNER, "=" * len(BANNER), "", "Paper ledger:"]
    for k, v in doc["ledger"].items():
        lines.append("  %-18s %s" % (k, v))
    lines.append("")
    lines.append("Daily risk state:")
    for d in doc["risk_days"] or [{"none": "no trading days recorded"}]:
        lines.append("  " + json.dumps(d, sort_keys=True))
    lines.append("")
    lines.append("Validation gate decisions:")
    for g in doc["gate"] or [{"none": "no strategy evaluated"}]:
        lines.append("  " + json.dumps(g, sort_keys=True, default=str))
    lines.append("")
    lines.append(BANNER)
    return "\n".join(lines)
