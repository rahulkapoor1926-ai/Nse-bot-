"""Atomic JSON persistence. A file that cannot be parsed is StateCorrupt, never 'empty'."""

import json
import os
import tempfile

from .errors import StateCorrupt


def write_json(path, doc):
    """Write doc to path atomically: temp file in the same folder, fsync, rename."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path, missing=None):
    """Return the parsed document, `missing` if the file does not exist.

    A file that exists but is empty, truncated or not JSON raises StateCorrupt.
    """
    if not os.path.exists(path):
        return missing
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise StateCorrupt("%s is not valid JSON (%s). See docs/nse/RECOVERY.md." % (path, e))


def append_jsonl(path, record):
    """Append one JSON line and fsync. Used for append-only ledgers and logs."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path):
    """Every record of a JSON-lines file. A bad line raises StateCorrupt with its number."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise StateCorrupt("%s line %d is not valid JSON (%s)" % (path, n, e))
    return out
