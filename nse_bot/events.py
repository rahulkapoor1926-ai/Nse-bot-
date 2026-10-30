"""Scheduled market events (RBI policy, Union Budget, data releases) with sources.

Events come from a user-maintained file (config "events_file"). Each must carry
an aware ISO time, an impact level and a source URL; an event without a source
is rejected rather than trusted.
"""

import datetime as dt
import json

from .clock import to_ist
from .errors import DataError

IMPACTS = ("high", "medium", "low")


def validate(ev):
    for k in ("time", "title", "impact", "source_url"):
        if not ev.get(k):
            raise DataError("event %r is missing %s" % (ev.get("title"), k))
    if ev["impact"] not in IMPACTS:
        raise DataError("event %r impact must be one of %s" % (ev["title"], IMPACTS))
    if not str(ev["source_url"]).startswith("https://"):
        raise DataError("event %r source_url must be an https link" % ev["title"])
    try:
        t = dt.datetime.fromisoformat(ev["time"])
    except ValueError:
        raise DataError("event %r time %r is not ISO 8601" % (ev["title"], ev["time"]))
    if t.tzinfo is None:
        raise DataError("event %r time has no timezone (use +05:30)" % ev["title"])
    return dict(ev, time=to_ist(t))


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise DataError("cannot read events file %s: %s" % (path, e))
    return [validate(e) for e in doc.get("events", [])]


def blackout(events, now, minutes):
    """The first high-impact event within +-minutes of now, else None."""
    if minutes <= 0:
        return None
    w = dt.timedelta(minutes=minutes)
    for ev in events:
        if ev["impact"] == "high" and abs(ev["time"] - now) <= w:
            return ev
    return None


def upcoming(events, now, hours=24):
    end = now + dt.timedelta(hours=hours)
    return sorted((e for e in events if now <= e["time"] <= end), key=lambda e: e["time"])
