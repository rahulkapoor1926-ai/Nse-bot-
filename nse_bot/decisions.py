"""Trade Decision Reports: evidence check and storage.

Every sweep_reversal evaluation produces a report (sweep.evaluate). Before it is
stored, verify() checks that every candle the report cites exists, with the
same prices, in the data the evaluation was given. 5m/15m/future-5m candles are
recomputed here from the raw 1m bars, not taken from the report. Any mismatch
turns the report into SKIP ("evidence mismatch"); a report can never cite a
candle that was not in the data.

Storage:
  <state>/decisions/<date>.jsonl            full reports (contain SAHI market data;
                                            kept in the private cache, not the public branch)
  <state>/dashboard/decisions-<date>.jsonl  one-line public summaries (no quotes or Greeks)
A report is written only when its outcome changes, so a SKIP repeated every
30 seconds is stored once.
"""

import os

from . import state, structure as st

PUBLIC_KEYS = ("strategy", "underlying", "evaluated_at", "decision", "skip_reason", "bias", "why")


def series_from(u1, u1h, u1d, f1):
    return {"1m": u1, "5m": st.resample(u1, 5), "15m": st.resample(u1, 15), "1h": u1h, "1d": u1d,
            "fut1m": f1, "fut5m": st.resample(f1, 5)}


def verify(report, sources):
    """List of problems; empty means every cited candle is in the data exactly."""
    idx = {name: {b[0].isoformat(): b for b in bars} for name, bars in sources.items()}
    bad = []
    for ev in report.get("evidence", []):
        bars = idx.get(ev.get("series"))
        if bars is None:
            bad.append("unknown series %r" % ev.get("series"))
            continue
        b = bars.get(ev.get("t"))
        if b is None:
            bad.append("%s candle %s is not in the data" % (ev["series"], ev.get("t")))
            continue
        got = (b[1], b[2], b[3], b[4])
        if tuple(round(x, 6) for x in got) != tuple(round(ev[k], 6) for k in ("o", "h", "l", "c")):
            bad.append("%s candle %s prices differ from the data" % (ev["series"], ev["t"]))
    return bad


def enforce(report, sources):
    bad = verify(report, sources)
    report["evidence_check"] = "ok" if not bad else bad
    if bad:
        report["decision"] = "SKIP"
        report["skip_reason"] = "evidence mismatch: " + "; ".join(bad[:3])
        report.pop("_selected", None)
    return report


def _key(r):
    return (r["strategy"], r["underlying"], r["decision"], r.get("skip_reason", "") and r["skip_reason"].split(":")[0],
            (r.get("liquidity") or {}).get("sweep_bar"), r.get("signal_known_at"))


def record(state_dir, report, day):
    clean = {k: v for k, v in report.items() if not k.startswith("_")}
    path = os.path.join(state_dir, "decisions", "%s.jsonl" % day.isoformat())
    last = state.read_jsonl(path)
    same = [r for r in last if r["strategy"] == clean["strategy"] and r["underlying"] == clean["underlying"]]
    if same and _key(same[-1]) == _key(clean):
        return False
    state.append_jsonl(path, clean)
    state.append_jsonl(os.path.join(state_dir, "dashboard", "decisions-%s.jsonl" % day.isoformat()),
                       {k: clean.get(k) for k in PUBLIC_KEYS})
    return True
