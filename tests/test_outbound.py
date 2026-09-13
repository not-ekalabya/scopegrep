#!/usr/bin/env python3
"""Tests for the outbound completeness block.

Run: python3 tests/test_outbound.py      (no pytest, no network, no GPU)

Why these exist at all: `_repo_callers` shells out to `git grep`, so its output
is shaped by whatever repository it is pointed at. Nothing else in this server
has that property, and nothing catches a regression in it -- a filter that
silently stops matching returns "no calls outside that file", which is also the
correct answer for a symbol that genuinely has none. The two cases are
indistinguishable from the outside, so they have to be distinguished here.

Each test builds a real git repository in a temp directory and asserts on the
rendered block, not on internals. That is the contract a caller depends on.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "scopegrep"))
os.environ.setdefault("SCOPEGREP_URL", "http://127.0.0.1:9")
import server as g                                               # noqa: E402


def make_repo(files):
    """A real git repository. `_repo_callers` runs `git grep`, so a fixture
    that is only a directory exercises the fallback path instead of the one
    under test, and would pass while the real path was broken."""
    d = tempfile.mkdtemp(prefix="scopegrep-test-")
    for rel, text in files.items():
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "t"], cwd=d, check=True, env=env)
    return d


def chunk_of(root, rel, symbol=None):
    text = (Path(root) / rel).read_text()
    return [text], [{"path": rel, "start_line": 1, "header_lines": 0,
                     "symbol": symbol}]


def block(root, rel, symbol=None, repo_wide=True):
    """The rendered block for a chunk, as the caller would receive it."""
    chunks, meta = chunk_of(root, rel, symbol)
    old = g._OUTBOUND_REPO_WIDE
    g._OUTBOUND_REPO_WIDE = repo_wide
    try:
        return "\n".join(g._resolve_outbound([0], chunks, meta, root=root))
    finally:
        g._OUTBOUND_REPO_WIDE = old


class TestCallSiteCounting(unittest.TestCase):
    def test_enumerates_every_call_site_and_says_all_of_them(self):
        root = make_repo({
            "lib.py": "def compute_total(x):\n    return x\n",
            "a.py": "from lib import compute_total\ncompute_total(1)\n",
            "b.py": "from lib import compute_total\nv = compute_total(2)\n",
        })
        out = block(root, "lib.py")
        self.assertIn("compute_total", out)
        self.assertIn("2 call site(s)", out)
        self.assertIn("ALL of them", out)
        self.assertIn("a.py:2", out)
        self.assertIn("b.py:2", out)

    def test_import_lines_are_not_call_sites(self):
        """An import names the symbol and decides nothing about changing it.
        Four of one symbol's eight reported 'call sites' were once imports."""
        root = make_repo({
            "lib.py": "def compute_total(x):\n    return x\n",
            "a.py": "from lib import (\n    compute_total,\n)\n",
        })
        out = block(root, "lib.py")
        self.assertIn("no calls outside", out)
        self.assertNotIn("a.py:2", out)

    def test_comments_and_the_definition_itself_are_not_call_sites(self):
        root = make_repo({
            "lib.py": "def compute_total(x):\n    return x\n",
            "a.py": "# compute_total is the one to use\nimport lib\n",
        })
        self.assertIn("no calls outside", block(root, "lib.py"))

    def test_tests_are_counted_but_not_enumerated(self):
        root = make_repo({
            "lib.py": "def compute_total(x):\n    return x\n",
            "tests/test_lib.py": "from lib import compute_total\n"
                                 "compute_total(1)\n",
        })
        out = block(root, "lib.py")
        self.assertIn("in tests", out)
        self.assertNotIn("tests/test_lib.py:2", out)

    def test_flood_reports_exact_count_without_enumerating(self):
        """Above the cap the count stays exact; only the listing is withheld."""
        n = g._OUTBOUND_MAX_SITES + 5
        files = {"lib.py": "def compute_total(x):\n    return x\n"}
        for i in range(n):
            files[f"m{i}.py"] = "import lib\ncompute_total(1)\n"
        out = block(make_repo(files), "lib.py")
        self.assertIn(f"{n} call sites", out)
        self.assertIn("too many to list", out)


class TestLanguageIndependence(unittest.TestCase):
    """The block must not be a Python feature wearing a general name."""

    def test_go_definition_and_call_sites(self):
        root = make_repo({
            "lib.go": "package lib\n\nfunc ComputeTotal(x int) int {\n"
                      "\treturn x\n}\n",
            "main.go": "package main\n\nfunc main() {\n"
                       "\tlib.ComputeTotal(1)\n}\n",
        })
        out = block(root, "lib.go")
        self.assertIn("ComputeTotal", out)
        self.assertIn("main.go:4", out)

    def test_rust_definition_and_call_sites(self):
        root = make_repo({
            "lib.rs": "pub fn compute_total(x: i32) -> i32 {\n    x\n}\n",
            "main.rs": "fn main() {\n    compute_total(1);\n}\n",
        })
        out = block(root, "lib.rs")
        self.assertIn("compute_total", out)
        self.assertIn("main.rs:2", out)

    def test_typescript_definition_and_call_sites(self):
        root = make_repo({
            "lib.ts": "export function computeTotal(x: number) {\n"
                      "  return x;\n}\n",
            "app.ts": "const v = computeTotal(1);\n",
        })
        out = block(root, "lib.ts")
        self.assertIn("computeTotal", out)
        self.assertIn("app.ts:1", out)

    def test_call_sites_are_found_in_a_language_with_no_definition_rule(self):
        """A C++ definition is not keyword-led, so it yields no anchor -- but a
        symbol anchored from a language that IS matched must still report its
        C++ call sites. The caller scan is language-independent even where the
        anchor side is not."""
        root = make_repo({
            "lib.pyx": "cdef int compute_total(int x):\n    return x\n",
            "solver.cpp": "void solve() {\n    compute_total(1);\n}\n",
        })
        out = block(root, "lib.pyx")
        self.assertIn("compute_total", out)
        self.assertIn("solver.cpp:2", out)

    def test_unknown_extension_is_treated_as_source(self):
        """The deny-list exists so a language this was never taught produces
        call sites rather than a silent, authoritative-looking zero."""
        self.assertTrue(g._is_source("app/models/user.rb"))
        self.assertTrue(g._is_source("src/Program.cs"))
        self.assertTrue(g._is_source("lib/thing.kt"))
        self.assertTrue(g._is_source("pkg/mod.swift"))
        self.assertTrue(g._is_source("pandas/_libs/tslibs/base.pyi"))
        self.assertTrue(g._is_source("build_tools/run.sh"))

    def test_prose_and_data_are_not_source(self):
        for p in ("doc/whatsnew.rst", "README.md", "data/fixture.csv",
                  "conf/settings.yaml", "web/index.html", "go.sum",
                  "tests/data/sheet.xlsx", "vendor/lib/pkg.go",
                  "static/app.min.js", "docs/guide.py"):
            self.assertFalse(g._is_source(p), p)


class TestDefinitionExtraction(unittest.TestCase):
    def test_keyword_led_definitions_across_languages(self):
        cases = {
            "def compute_total(x):": "compute_total",
            "class OrderBook:": "OrderBook",
            "async def fetch_page(u):": "fetch_page",
            "export function computeTotal(x) {": "computeTotal",
            "func ComputeTotal(x int) int {": "ComputeTotal",
            "pub fn compute_total(x: i32) {": "compute_total",
            "type OrderBook struct {": "OrderBook",
            "interface Shape extends Base {": "Shape",
            "impl Display for OrderBook {": "Display",
            "trait Summary where Self: Sized {": "Summary",
            "enum Colour {": "Colour",
        }
        for line, want in cases.items():
            self.assertIn(want, g._DEF_KW.findall(line + "\n"), line)

    def test_prose_is_not_a_definition(self):
        """`type`, `class` and `for` are English words. A bare
        keyword-then-word rule reported `of`, `objects` and `while` as defined
        symbols on real docstrings."""
        for line in ("    type of the array to return",
                     "    class objects returned by this method",
                     "    struct members are laid out in order",
                     "    interface with the scheduler is documented above",
                     "    enum values must be unique"):
            self.assertEqual([], g._DEF_KW.findall(line + "\n"), line)

    def test_cython_typed_definition_yields_the_name_not_the_type(self):
        self.assertEqual(["compute_total"],
                         g._DEF_TYPED.findall("cdef int compute_total(int x):\n"))
        self.assertEqual(["scale_all"],
                         g._DEF_TYPED.findall("cpdef float[] scale_all(x):\n"))

    def test_keywordless_definitions_are_a_known_gap(self):
        """Pinned, not aspirational. A C/C++/Java method leads with a return
        type rather than a keyword, and a memoryview return type (`double[:]`)
        carries a colon the typed pattern does not admit.

        Widening the typed pattern to accept `:` was measured against the 126
        Cython files in the benchmark corpora: it added 68 names, of which all
        but one were `__init__`/`__cinit__` dunders that `_defined_symbols`
        discards anyway, and the exception was a false positive (`if`). So the
        gap is left open deliberately. Change this test only alongside a
        measurement that says the trade moved."""
        self.assertEqual([], g._DEF_KW.findall("public static void mainLoop(\n"))
        self.assertEqual([], g._DEF_KW.findall("void Solver::Solve(double *x)\n"))
        self.assertEqual([], g._DEF_TYPED.findall("cpdef double[:] scale(x):\n"))

    def test_common_names_are_skipped(self):
        chunks = ["def run(self):\n    pass\n\ndef compute_total(x):\n    return x\n"]
        meta = [{"path": "a.py", "start_line": 1, "header_lines": 0, "symbol": None}]
        got = g._defined_symbols([0], chunks, meta)
        self.assertIn("compute_total", got)
        self.assertNotIn("run", got)


class TestDegradation(unittest.TestCase):
    def test_no_git_repository_falls_back_instead_of_reporting_nothing(self):
        d = tempfile.mkdtemp(prefix="scopegrep-nogit-")
        (Path(d) / "lib.py").write_text("def compute_total(x):\n    return x\n")
        self.assertEqual({}, g._repo_callers(["compute_total"], d))
        chunks = ["def compute_total(x):\n    return x\n",
                  "import lib\ncompute_total(1)\n"]
        meta = [{"path": "lib.py", "start_line": 1, "header_lines": 0, "symbol": None},
                {"path": "a.py", "start_line": 1, "header_lines": 0, "symbol": None}]
        out = "\n".join(g._resolve_outbound([0], chunks, meta, root=d))
        self.assertIn("a.py:2", out)

    def test_symbol_with_no_callers_says_so_explicitly(self):
        root = make_repo({"lib.py": "def compute_total(x):\n    return x\n"})
        self.assertIn("no calls outside", block(root, "lib.py"))

    def test_block_is_empty_when_the_chunk_defines_nothing(self):
        root = make_repo({"a.py": "x = 1\ny = 2\n"})
        self.assertEqual("", block(root, "a.py"))


class TestRanking(unittest.TestCase):
    def test_decidable_symbols_outrank_floods(self):
        """A symbol with 300 call sites is a fact about the word, not a change
        to make; it must not crowd out the one symbol a caller could act on."""
        files = {"lib.py": "def compute_total(x):\n    return x\n\n"
                           "def Index(x):\n    return x\n"}
        for i in range(g._OUTBOUND_MAX_SITES + 10):
            files[f"m{i}.py"] = "import lib\nIndex(1)\n"
        files["one.py"] = "import lib\ncompute_total(1)\n"
        out = block(make_repo(files), "lib.py")
        self.assertLess(out.index("compute_total"), out.index("Index"), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
