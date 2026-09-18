#!/usr/bin/env python3
"""Tests for directory pruning in `_walk`.

Run: python3 tests/test_walk_pruning.py      (no pytest, no network, no hosted service)

Why this exists: `include` used to be applied only to filenames, after
`os.walk` had already recursed through the entire tree. A caller who scoped
`include=["target/**"]` against a root that also held an unrelated
gigabyte-scale sibling directory still paid the cost of walking all of it,
because nothing pruned `dirnames` on `include`'s account -- on a large enough
sibling, that walk does not finish in any practical time, and the caller sees
what looks like a dead service. Directory-prefixed include patterns must
prune descent into directories no pattern can reach; bare patterns (no "/")
must keep walking everything, since they are allowed to match at any depth.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "scopegrep"))
os.environ.setdefault("SCOPEGREP_URL", "http://127.0.0.1:9")
import server as g                                               # noqa: E402


def make_tree(root, files):
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


class WalkPruningTest(unittest.TestCase):

    def test_prefixed_include_prunes_unrelated_sibling(self):
        with tempfile.TemporaryDirectory() as root:
            make_tree(root, {
                "target/a.py": "x = 1\n",
                "target/sub/b.py": "y = 2\n",
                "huge_sibling/c.py": "z = 3\n",
                "huge_sibling/deep/d.py": "w = 4\n",
            })
            files, coverage = g._walk(root, ["target/**"], [])
            rels = sorted(rel for rel, *_ in files)
            self.assertEqual(rels, ["target/a.py", "target/sub/b.py"])
            self.assertGreater(coverage["pruned_dirs"], 0)

    def test_bare_pattern_still_reaches_every_depth(self):
        with tempfile.TemporaryDirectory() as root:
            make_tree(root, {
                "target/a.py": "x = 1\n",
                "huge_sibling/deep/d.py": "w = 4\n",
            })
            files, coverage = g._walk(root, ["*.py"], [])
            rels = sorted(rel for rel, *_ in files)
            self.assertEqual(rels, ["huge_sibling/deep/d.py", "target/a.py"])
            self.assertEqual(coverage["pruned_dirs"], 0)

    def test_mixed_bare_and_prefixed_disables_pruning_for_the_whole_call(self):
        # A bare pattern anywhere in `include` can match in any directory, so
        # it must disable pruning globally -- a narrower sibling pattern in
        # the same call does not get to prune around it.
        with tempfile.TemporaryDirectory() as root:
            make_tree(root, {
                "target/a.py": "x = 1\n",
                "huge_sibling/deep/d.py": "w = 4\n",
            })
            files, coverage = g._walk(root, ["target/**", "*.py"], [])
            rels = sorted(rel for rel, *_ in files)
            self.assertEqual(rels, ["huge_sibling/deep/d.py", "target/a.py"])
            self.assertEqual(coverage["pruned_dirs"], 0)

    def test_globstar_middle_pattern_prunes_by_literal_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            make_tree(root, {
                "src/a/deep/x.py": "x = 1\n",
                "src/b/y.md": "# doc\n",
                "other/z.py": "z = 1\n",
            })
            files, coverage = g._walk(root, ["src/**/*.py"], [])
            rels = sorted(rel for rel, *_ in files)
            self.assertEqual(rels, ["src/a/deep/x.py"])
            self.assertGreater(coverage["pruned_dirs"], 0)

    def test_dir_reachable_unit(self):
        self.assertTrue(g._dir_reachable(["pipeline"], g._pattern_segments("pipeline/**")))
        self.assertFalse(g._dir_reachable(["docs"], g._pattern_segments("pipeline/**")))
        self.assertTrue(g._dir_reachable(["src", "a", "deep"],
                                          g._pattern_segments("src/**/*.py")))
        self.assertTrue(g._dir_reachable(["anything"], g._pattern_segments("*.py")))


if __name__ == "__main__":
    unittest.main()
