"""Dashboard controls for the PAPER bot: pause new entries, resume them, emergency halt.

The dashboard writes control.json to the `bot-control` branch; the paper loop reads it once a cycle
(GitHub contents API with the job's own GITHUB_TOKEN, or a local file). Controls can only ever
BLOCK new entries. Nothing here opens, closes or sizes a trade, and open positions keep being
managed to their stop, target or time exit whatever the controls say.

  entries_paused   true -> every new entry is refused ("new entries paused from the dashboard")
                   until a resume. Reversible.
  halt_day         "YYYY-MM-DD" -> EMERGENCY HALT: the shared risk book is locked for that day,
                   the same lockout as the loss limit. Not reversible from the dashboard.

Fail closed: if the control file cannot be read MAX_FAILURES cycles in a row, new entries are
paused until it can be read again. A missing file (never written) means no controls are set.
"""

import json
import os
import urllib.error
import urllib.request

from . import clock, state

FILE = "control.json"
BRANCH = "bot-control"
MAX_FAILURES = 3
DEFAULT = {"entries_paused": False, "halt_day": None, "updated_at": None, "note": None}


def normalise(doc):
    if not isinstance(doc, dict):
        raise ValueError("control document is not an object")
    out = dict(DEFAULT)
    out["entries_paused"] = bool(doc.get("entries_paused", False))
    out["halt_day"] = doc.get("halt_day") or None
    out["updated_at"] = doc.get("updated_at")
    out["note"] = doc.get("note")
    return out


def read_local(path):
    return normalise(state.read_json(path, missing=DEFAULT) or DEFAULT)


def github_fetch(repo, token, branch=BRANCH, timeout=10):
    """control.json from the bot-control branch (None if the branch or file does not exist yet)."""
    url = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (repo, FILE, branch)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw+json",
                                               "Authorization": "Bearer %s" % token,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


class Poller(object):
    """Reads the controls once a cycle and applies them to the shared risk book."""

    def __init__(self, fetch):
        self.fetch = fetch
        self.failures = 0
        self.last = dict(DEFAULT)

    @classmethod
    def from_env(cls, env=os.environ):
        path = env.get("BOT_CONTROL_FILE")
        if path:
            return cls(lambda: state.read_json(path, missing=None))
        repo, token = env.get("BOT_CONTROL_REPO"), env.get("GITHUB_TOKEN")
        if repo and token:
            return cls(lambda: github_fetch(repo, token))
        return None

    def get(self):
        try:
            doc = self.fetch()
            self.last = normalise(doc) if doc is not None else dict(DEFAULT)
            self.failures = 0
            return dict(self.last, read_ok=True)
        except Exception as e:                      # noqa: BLE001  (fail closed below, never crash the loop)
            self.failures += 1
            out = dict(self.last, read_ok=False, error=str(e)[:200])
            if self.failures >= MAX_FAILURES:
                out["entries_paused"] = True
                out["note"] = "controls unreadable %d cycles in a row: new entries paused (fail closed)" % self.failures
            return out

    def apply(self, book, now):
        """Lock the day on an emergency halt; pause entries in memory. Returns the controls used."""
        c = self.get()
        today = clock.to_ist(now).date().isoformat()
        if c.get("halt_day") == today:
            book.lock("EMERGENCY HALT from the dashboard (%s): no new entries today; open positions are "
                      "still managed" % (c.get("updated_at") or "time unknown"), now)
        book.paused = ("new entries paused from the dashboard%s" % (": " + c["note"] if c.get("note") else "")) \
            if c.get("entries_paused") else None
        return c
