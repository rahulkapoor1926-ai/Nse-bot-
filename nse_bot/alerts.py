"""Alerts: price, entry-candidate, stop, target, stale-data and system-error.

Each alert has a key; an alert rings once per key per day (state in
<state_dir>/alerts/<day>.json, written atomically) so a restart does not ring
everything again. Every alert is appended to <state_dir>/alerts/alerts.jsonl
and passed to the sink (stdout by default).
"""

import os
import sys

from . import clock, state
from .errors import StateCorrupt

KINDS = ("price", "candidate", "stop", "target", "stale_data", "system_error")


class Alerts(object):
    def __init__(self, state_dir, day, sink=None):
        self.dir = os.path.join(state_dir, "alerts")
        self.path = os.path.join(self.dir, "%s.json" % day.isoformat())
        try:
            self.rung = set(state.read_json(self.path, missing={"rung": []})["rung"])
        except (StateCorrupt, KeyError, TypeError):
            self.rung = set()          # ringing twice is safer than staying silent
        self.sink = sink or (lambda a: print("[ALERT %s] %s" % (a["kind"], a["message"]), file=sys.stdout))

    def emit(self, kind, key, message, now, once=True, data=None):
        if kind not in KINDS:
            raise ValueError("unknown alert kind %r" % kind)
        k = "%s:%s" % (kind, key)
        if once and k in self.rung:
            return None
        a = {"kind": kind, "key": key, "message": message, "time": clock.to_ist(now).isoformat(),
             "data": data or {}}
        state.append_jsonl(os.path.join(self.dir, "alerts.jsonl"), a)
        self.sink(a)
        if once:
            self.rung.add(k)
            state.write_json(self.path, {"rung": sorted(self.rung)})
        return a

    def check_price_rules(self, rules, prices, now):
        """rules: [{"id", "instrument_id", "above"|"below"}]; prices: {id: float or None}."""
        out = []
        for r in rules:
            p = prices.get(r["instrument_id"])
            if p is None:
                a = self.emit("stale_data", "price-%s" % r["id"], "no price for rule %s" % r["id"], now)
            elif "above" in r and p >= r["above"]:
                a = self.emit("price", r["id"], "%s at %.2f >= %.2f" % (r["instrument_id"], p, r["above"]), now)
            elif "below" in r and p <= r["below"]:
                a = self.emit("price", r["id"], "%s at %.2f <= %.2f" % (r["instrument_id"], p, r["below"]), now)
            else:
                a = None
            if a:
                out.append(a)
        return out
