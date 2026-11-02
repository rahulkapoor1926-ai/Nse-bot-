"""Forward (live-market) paper validation: a log of every strategy signal, accepted or rejected with
its reasons, and separate performance statistics for ORB and Sweep from the paper ledger.

Nothing here creates, changes or blocks a trade. Statistics use only closed round trips in the
hash-chained paper ledger, which the paper broker writes only for real strategy signals filled at
the live ask (entry) and bid (exit) with slippage and every charge. No trade is ever synthesised.

Files (inside the book's state_dir):
  forward/signals.jsonl      one row per signal per change of outcome (re-checks of the same signal
                             with the same outcome are not repeated)
  forward/report.md / .json  written by `python -m nse_bot forward-report`
"""

import datetime as dt
import json
import os
import re

from . import clock, regime, state

MIN_SAMPLE = 30          # below this: INSUFFICIENT EVIDENCE, no conclusion of any kind
GOOD_SAMPLE = 100        # 30-99: INITIAL EVIDENCE; 100+: STRONGER EVIDENCE. Nothing here is ever a PASS.


def evidence(n):
    """The evidence level of n completed trades (decision of 2026-11-02). Never PASS."""
    if n < MIN_SAMPLE:
        return "INSUFFICIENT EVIDENCE"
    return "INITIAL EVIDENCE" if n < GOOD_SAMPLE else "STRONGER EVIDENCE"


def _family(name):
    return "Sweep" if "sweep" in (name or "").lower() else "ORB"


def _seen_path(state_dir, day):
    return os.path.join(state_dir, "forward", "seen-%s.json" % day.isoformat())


def record_signal(state_dir, now, strategy, underlying, contract, signal_time, decision, reasons, extra=None):
    """Append a signal outcome once per (signal, outcome). Reporting only; never raises."""
    try:
        now = clock.to_ist(now)
        key = "%s|%s|%s|%s" % (strategy, underlying, contract, signal_time)
        outcome = "%s|%s" % (decision, "; ".join(reasons or []))
        path = _seen_path(state_dir, now.date())
        seen = state.read_json(path, missing={}) or {}
        if seen.get(key) == outcome:
            return None
        seen[key] = outcome
        state.write_json(path, seen)
        row = {"time": now.isoformat(), "family": _family(strategy), "strategy": strategy, "underlying": underlying,
               "contract": contract, "signal_time": signal_time, "decision": decision, "reasons": list(reasons or [])}
        row.update(extra or {})
        state.append_jsonl(os.path.join(state_dir, "forward", "signals.jsonl"), row)
        return row
    except Exception:                           # noqa: BLE001  (the log must never affect trading)
        return None


def _verdict(x):
    return (x or {}).get("verdict") or (x or {}).get("status")


def orb_signal(ctx, now, strategy, cand, row):
    c = row.get("contract")
    risk = cand.get("risk") or {}
    decision = "ACCEPTED" if risk.get("verdict") == "PASS" else "REJECTED"
    reasons = risk.get("reasons") if risk else (cand.get("reasons") or [cand.get("status", "")])
    extra = {"net_reward_risk": ((cand.get("plan") or {}).get("net") or {}).get("net_reward_risk"),
             "orderflow": _verdict(cand.get("orderflow")), "auction_context": _verdict(cand.get("auction_context")),
             "options_desk": _verdict(cand.get("options_desk")), "data_source": ctx.cfg.get("data_source")}
    extra.update(cand.get("regime") or regime.fields(None))
    return record_signal(ctx.cfg["state_dir"], now, strategy["name"], row.get("underlying"),
                         getattr(c, "symbol", None), str((row.get("signal") or {}).get("known_at")), decision,
                         reasons, extra)


def sweep_signal(ctx, now, strategy, u, rep):
    if rep.get("decision") not in ("SETUP", "TAKE", "BLOCKED"):
        return None
    ex = rep.get("execution") or {}
    decision = "ACCEPTED" if rep["decision"] == "TAKE" else "REJECTED"
    reasons = ex.get("risk_reasons") or ([ex["result"]] if ex.get("result") else []) or \
        (ex.get("entry_check") or {}).get("reasons") or []
    extra = {"net_reward_risk": ((ex.get("plan") or {}).get("net") or {}).get("net_reward_risk"),
             "orderflow": _verdict(ex.get("orderflow")), "auction_context": _verdict(ex.get("auction_context")),
             "options_desk": _verdict(ex.get("options_desk")), "data_source": ctx.cfg.get("data_source")}
    extra.update(ex.get("regime") or regime.fields(None))
    return record_signal(ctx.cfg["state_dir"], now, strategy["name"], u, (rep.get("contract") or {}).get("symbol"),
                         rep.get("signal_known_at"), decision, reasons, extra)


# ---- statistics ----------------------------------------------------------------------------
def closed_trades(records):
    """Closed round trips from ledger fills, with the strategy from the entry fill's meta."""
    pos, out = {}, []
    for r in records:
        if r.get("event") != "fill":
            continue
        iid = str(r["instrument_id"])
        p = pos.setdefault(iid, {"qty": 0, "cost": 0.0, "charges": 0.0, "strategy": None, "entry_time": None})
        if r["side"] == "BUY":
            if p["qty"] == 0:
                p["strategy"], p["entry_time"] = (r.get("meta") or {}).get("strategy"), r["time"]
                p["regime"] = regime.fields((r.get("meta") or {}).get("regime"))
            p["qty"] += r["qty"]
            p["cost"] += r["price"] * r["qty"]
            p["charges"] += r["charges"]["total"]
            continue
        avg = p["cost"] / p["qty"] if p["qty"] else 0.0
        share = r["qty"] / p["qty"] if p["qty"] else 0.0
        entry_ch = p["charges"] * share
        gross = (r["price"] - avg) * r["qty"]
        costs = entry_ch + r["charges"]["total"]
        out.append({"strategy": p["strategy"], "family": _family(p["strategy"]), "instrument_id": iid,
                    "entry_time": p["entry_time"], "exit_time": r["time"], "entry": round(avg, 2), "exit": r["price"],
                    "qty": r["qty"], "gross": round(gross, 2), "costs": round(costs, 2),
                    "net": round(gross - costs, 2), "reason": (r.get("meta") or {}).get("reason"),
                    **p.get("regime", regime.fields(None))})
        p["cost"] -= avg * r["qty"]
        p["charges"] -= entry_ch
        p["qty"] -= r["qty"]
    return out


def stats(trades, capital):
    n = len(trades)
    wins = [x["net"] for x in trades if x["net"] > 0]
    losses = [x["net"] for x in trades if x["net"] <= 0]
    eq = peak = dd = 0.0
    for x in sorted(trades, key=lambda x: x["exit_time"]):
        eq += x["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) < 0 else None
    out = {"trades": n, "wins": len(wins), "losses": len(losses),
           "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else None,
           "gross": round(sum(x["gross"] for x in trades), 2), "costs": round(sum(x["costs"] for x in trades), 2),
           "net": round(sum(x["net"] for x in trades), 2),
           "profit_factor": round(pf, 2) if pf is not None else None,
           "profit_factor_note": None if pf is not None else ("no trades" if not n else "no losing trades yet"),
           "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
           "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
           "expectancy": round(sum(x["net"] for x in trades) / n, 2) if n else None,
           "max_drawdown_inr": round(dd, 2), "max_drawdown_pct_of_capital": round(100.0 * dd / capital, 2)}
    out["evidence"] = evidence(n)
    if n < MIN_SAMPLE:
        out["sample"] = ("INSUFFICIENT EVIDENCE: %d completed trade%s; at least %d are needed for an initial "
                         "evaluation (%d+ for stronger evidence). These numbers are not evidence."
                         % (n, "" if n == 1 else "s", MIN_SAMPLE, GOOD_SAMPLE))
    elif n < GOOD_SAMPLE:
        out["sample"] = "INITIAL EVIDENCE: %d trades; %d+ needed for stronger evidence" % (n, GOOD_SAMPLE)
    else:
        out["sample"] = "STRONGER EVIDENCE: %d trades" % n
    return out


def by_regime(trades, capital):
    """Per trend label and per volatility label, each with its own evidence level."""
    out = {}
    for key in ("trend", "volatility"):
        labels = sorted({t.get(key, regime.UNKNOWN) for t in trades})
        out[key] = {lab: stats([t for t in trades if t.get(key, regime.UNKNOWN) == lab], capital) for lab in labels}
    return out


DEFAULT_STRATEGIES = ("orb15-atm", "sweep-reversal")


def signal_counts(rows):
    """Per strategy: signals (last outcome of each), accepted, rejected, top reasons, and signals per regime."""
    out = {}
    for r in rows:
        f = out.setdefault(r["strategy"], {"family": r["family"], "signals": 0, "accepted": 0, "rejected": 0,
                                           "top_reasons": {}})
        keyset = f.setdefault("_keys", {})
        k = (r["strategy"], r["underlying"], r["contract"], r["signal_time"])
        keyset[k] = r                                   # last outcome per signal
    for f in out.values():
        last = list(f.pop("_keys").values())
        f["signals"] = len(last)
        f["accepted"] = sum(1 for r in last if r["decision"] == "ACCEPTED")
        f["rejected"] = f["signals"] - f["accepted"]
        f["by_regime"] = {}
        for r in last:
            lab = r.get("regime") or "%s/%s" % (regime.UNKNOWN, regime.UNKNOWN)
            f["by_regime"][lab] = f["by_regime"].get(lab, 0) + 1
        tally = {}
        for r in last:
            if r["decision"] == "REJECTED":
                for why in r["reasons"] or ["(no reason recorded)"]:
                    k = re.sub(r"\d+(\.\d+)?", "#", why)[:90]          # group "stale: 12s" with "stale: 40s"
                    tally[k] = tally.get(k, 0) + 1
        f["top_reasons"] = dict(sorted(tally.items(), key=lambda kv: -kv[1])[:6])
    return out


ROWS = [("Evidence", "evidence"), ("Completed trades", "trades"), ("Wins / losses", None), ("Win rate %", "win_rate_pct"),
        ("Net P&L (Rs)", "net"), ("Costs (Rs)", "costs"), ("Profit factor", "profit_factor"),
        ("Net expectancy per trade (Rs)", "expectancy"), ("Max drawdown (Rs)", "max_drawdown_inr"),
        ("Max drawdown (% of capital)", "max_drawdown_pct_of_capital")]


def _cell(st, k):
    if k is None:
        return "%d / %d" % (st["wins"], st["losses"])
    if k == "profit_factor" and st[k] is None:
        return "n/a (%s)" % st["profit_factor_note"]
    return "n/a" if st[k] is None else str(st[k])


def report(books, capital, now):
    """books: [(label, state_dir)]. Returns (dict, markdown). Each strategy is reported on its own."""
    from .broker.paper import Ledger
    trades, signals = [], []
    for label, sd in books:
        trades += [dict(t, book=label) for t in closed_trades(Ledger(sd).records())]
        try:
            signals += state.read_jsonl(os.path.join(sd, "forward", "signals.jsonl"))
        except Exception:                       # noqa: BLE001
            pass
    names = list(DEFAULT_STRATEGIES)
    for x in [t["strategy"] for t in trades] + [r["strategy"] for r in signals]:
        if x and x not in names:
            names.append(x)
    strat = {}
    for n in names:
        mine = [t for t in trades if t["strategy"] == n]
        strat[n] = {"family": _family(n), "overall": stats(mine, capital), "by_regime": by_regime(mine, capital)}
    doc = {"generated": clock.to_ist(now).isoformat(), "capital_inr": capital, "books": [b for b, _ in books],
           "strategies": strat, "signals": signal_counts(signals), "trades": trades,
           "evidence_rule": "per strategy: < %d completed trades INSUFFICIENT EVIDENCE, %d-%d INITIAL EVIDENCE, "
                            "%d+ STRONGER EVIDENCE; never a PASS" % (MIN_SAMPLE, MIN_SAMPLE, GOOD_SAMPLE - 1,
                                                                     GOOD_SAMPLE),
           "regime_basis": regime.BASIS,
           "note": "paper trades on live FYERS prices only; no synthetic or artificial trades"}
    md = ["# Forward paper validation (%s)" % clock.to_ist(now).strftime("%Y-%m-%d %H:%M IST"), "",
          "Paper trades on live FYERS prices only (entries at ask, exits at bid, slippage and all charges). "
          "No synthetic or artificial trades. Each strategy is reported on its own.", "",
          "Evidence: fewer than %d completed trades is INSUFFICIENT EVIDENCE, %d-%d is INITIAL EVIDENCE, %d+ is "
          "STRONGER EVIDENCE. Nothing here is a PASS." % (MIN_SAMPLE, MIN_SAMPLE, GOOD_SAMPLE - 1, GOOD_SAMPLE), "",
          "| | %s |" % " | ".join(names), "|---|%s" % ("---|" * len(names))]
    for label, k in ROWS:
        md.append("| %s | %s |" % (label, " | ".join(_cell(strat[n]["overall"], k) for n in names)))
    md += ["", "## By market regime", "", "Regime: %s." % regime.BASIS, ""]
    for n in names:
        rows = []
        for key in ("trend", "volatility"):
            for lab, st in strat[n]["by_regime"][key].items():
                rows.append("| %s | %d | %s | %s | %s | %s |" % (lab, st["trades"], st["evidence"], _cell(st, "net"),
                                                              _cell(st, "expectancy"), _cell(st, "profit_factor")))
        md += ["**%s**" % n, ""]
        if rows:
            md += ["| Regime | Trades | Evidence | Net (Rs) | Expectancy (Rs) | Profit factor |",
                   "|---|---|---|---|---|---|"] + rows + [""]
        else:
            md += ["No completed trades yet (INSUFFICIENT EVIDENCE in every regime).", ""]
    md += ["## Signals (accepted / rejected, commonest rejection reasons, signals per regime)", ""]
    for n, sc in sorted(doc["signals"].items()):
        md.append("- **%s**: %d signals, %d accepted, %d rejected. %s Regimes: %s" % (
            n, sc["signals"], sc["accepted"], sc["rejected"],
            "; ".join("%s (%d)" % kv for kv in sc["top_reasons"].items()),
            ", ".join("%s %d" % kv for kv in sorted(sc["by_regime"].items()))))
    if not doc["signals"]:
        md.append("- No strategy signals recorded yet.")
    return doc, "\n".join(md) + "\n"


def write_report(books, capital, now, out_dir):
    doc, md = report(books, capital, now)
    os.makedirs(out_dir, exist_ok=True)
    state.write_json(os.path.join(out_dir, "report.json"), doc)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    return doc, md


__all__ = ["record_signal", "orb_signal", "sweep_signal", "closed_trades", "stats", "by_regime", "evidence", "report",
           "write_report", "dt", "json"]
