"""Trade statistics after costs, with confidence intervals.

The bootstrap here only measures uncertainty of the mean of REAL trades. Its
resamples are never counted as examples anywhere.
"""

import math
import random
from collections import defaultdict


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - m) / d, 4), round((c + m) / d, 4))


def bootstrap_mean_ci(xs, n=2000, seed=7, alpha=0.05):
    if len(xs) < 2:
        return (None, None)
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return (round(means[int(n * alpha / 2)], 2), round(means[int(n * (1 - alpha / 2)) - 1], 2))


def compute(trades, ci=True):
    # time order (exit time) for the drawdown and losing streak, whatever order the engines listed them in
    trades = sorted(trades, key=lambda t: t.get("exit_time") or "")
    nets = [t["net"] for t in trades]
    n = len(nets)
    if n == 0:
        return {"trades": 0}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    streak = longest = 0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        streak = streak + 1 if x <= 0 else 0
        longest = max(longest, streak)
    gl = -sum(losses)
    out = {
        "trades": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(nets) / n, 2),
        "profit_factor": round(sum(wins) / gl, 3) if gl > 0 else None,
        "net_pnl": round(sum(nets), 2),
        "gross_pnl": round(sum(t["gross"] for t in trades), 2),
        "charges": round(sum(t["charges"] for t in trades), 2),
        "max_drawdown": round(mdd, 2),
        "longest_losing_streak": longest,
    }
    if ci:
        out["win_rate_ci95"] = wilson(len(wins), n)
        out["expectancy_ci95"] = bootstrap_mean_ci(nets)
    return out


def by(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[t.get(key, "unknown")].append(t)
    return {k: compute(v, ci=False) for k, v in sorted(groups.items())}
