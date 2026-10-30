"""Acceptance 2: protected upstream files are byte-identical to the Stage A manifest.

The upstream tree lives verbatim under upstream/ (its own test_repo.py requires
that nothing else sits at its root), so manifest paths resolve there.
"""

import hashlib
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "docs", "nse", "protected-sha256.txt")
UPSTREAM = os.path.join(ROOT, "upstream")


class Protected(unittest.TestCase):
    def test_every_upstream_file_unchanged(self):
        with open(MANIFEST) as f:
            rows = [l.split(None, 1) for l in f if l.strip()]
        self.assertEqual(len(rows), 85)
        changed = []
        for digest, name in rows:
            path = os.path.join(UPSTREAM, name.strip())
            if not os.path.exists(path):
                changed.append(name.strip() + " (missing)")
                continue
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != digest:
                    changed.append(name.strip())
        self.assertEqual(changed, [])

    def test_upstream_skill_folders_untouched_by_new_files(self):
        with open(MANIFEST) as f:
            listed = {l.split(None, 1)[1].strip() for l in f if l.strip()}
        extra = []
        for folder in [d for d in os.listdir(UPSTREAM) if d.startswith("trader-")]:
            for name in os.listdir(os.path.join(UPSTREAM, folder)):
                rel = "%s/%s" % (folder, name)
                if rel not in listed and name != "__pycache__":
                    extra.append(rel)
        self.assertEqual(extra, [])

    def test_upstream_root_has_nothing_new(self):
        extra = sorted(set(os.listdir(UPSTREAM)) - {".claude-plugin", "LICENSE", "README.md", "tests"}
                       - {d for d in os.listdir(UPSTREAM) if d.startswith("trader-")} - {"__pycache__"})
        self.assertEqual(extra, [])
