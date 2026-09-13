"""scopegrep MCP server -- semantic context retrieval over a declared scope
of the working repository.

The agent declares a SCOPE (glob patterns), a QUERY, and a token budget. This
process reads the matching files, cuts them into path-headed chunks, ships
them to a hosted scoring service, and returns as many whole ranked chunks as
the budget allows.

Everything expensive is on the other side of the network; everything here is
file walking, chunking, and caching. Chunks are cached in-process keyed by
(scope spec + chunker version + a content hash of every in-scope file), so a
second query against an UNCHANGED scope sends a short scope_key instead of
the whole source and the service answers it from its own warm cache -- while
an edit that preserves a file's size and mtime still invalidates the entry.

The response is governed by `budget_tokens`, not by k: evidence items are
included whole or not at all, and anything dropped is reported by location.

Configuration:
  SCOPEGREP_URL    base URL of the hosted scoring service. Defaults to the
                  endpoint deployed for this plugin.
  SCOPEGREP_TOKEN  shared secret, sent as X-Scopegrep-Token. Falls back to
                  ~/.config/scopegrep/token, then to .scopegrep_token in the
                  plugin root -- so the secret never has to live in a shell
                  profile or in the MCP config.
  SCOPEGREP_ROOT   repository root to search (default: cwd)
"""
import hashlib
import json
import os
import subprocess
import re
import time

import httpx

try:                                    # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:             # mcp 1.x, where it was called FastMCP
    from mcp.server.fastmcp import FastMCP as _Server

DEFAULT_URL = "https://gekalabya2010--scopegrep-scopegrep-web.modal.run"
TOKEN_FILES = [os.path.expanduser("~/.config/scopegrep/token"),
               # __file__ is src/scopegrep/server.py -- three levels up is the
               # repo root, where .scopegrep_token lives. The rename from
               # server/gistgrep_mcp.py (two levels deep) to this src layout
               # made the old two-dirname computation land in src/ instead.
               os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                   os.path.abspath(__file__)))), ".scopegrep_token")]


def _read_token():
    tok = os.environ.get("SCOPEGREP_TOKEN", "").strip()
    if tok:
        return tok
    for path in TOKEN_FILES:
        try:
            with open(path) as fh:
                tok = fh.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return ""


URL = (os.environ.get("SCOPEGREP_URL") or DEFAULT_URL).rstrip("/")
TOKEN = _read_token()
ROOT = os.path.abspath(os.environ.get("SCOPEGREP_ROOT") or os.getcwd())

# The chunk shape: "# File: <relpath>\n" + the file's first 1500 characters,
# one chunk per file. The skill's guidance is calibrated to chunks of this size.
CHUNK_CHARS = 1500
CHARS_PER_TOKEN = 3.75      # rough estimate for code + JSON payloads
MAX_CHUNKS = 2000           # service-side hard cap
SAFE_CHUNKS = 1200          # largest scope this has been exercised against; refuse above this
HTTP_TIMEOUT = 900.0        # a cold service instance can take a while to become ready
# A request that outruns the service's synchronous response window gets a
# redirect to a polling URL instead of an immediate answer, so every call
# here follows redirects. Without it, the first request of a cold session
# returns an unhelpful early response instead of the result.

DEFAULT_EXCLUDES = [
    "**/.git/**", "**/.hg/**", "**/.svn/**", "**/node_modules/**",
    "**/__pycache__/**", "**/.venv/**", "**/venv/**", "**/env/**",
    "**/.mypy_cache/**", "**/.pytest_cache/**", "**/.ruff_cache/**",
    "**/dist/**", "**/build/**", "**/target/**", "**/vendor/**",
    "**/.next/**", "**/.nuxt/**", "**/coverage/**", "**/.tox/**",
    "**/*.min.js", "**/*.min.css", "**/*.map", "**/*.lock",
    "**/package-lock.json", "**/yarn.lock", "**/poetry.lock",
    "**/Cargo.lock", "**/pnpm-lock.yaml", "**/*.ipynb_checkpoints/**",
]
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".so", ".dylib", ".dll", ".a", ".o", ".pyc", ".pyo", ".class",
    ".jar", ".wasm", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov",
    ".avi", ".wav", ".flac", ".parquet", ".npy", ".npz", ".pt", ".pth",
    ".onnx", ".safetensors", ".h5", ".pkl",
}
MAX_FILE_BYTES = 2_000_000

# split points preferred when cutting a large file into windows: a line that
# starts a top-level definition in any of the common languages
BOUNDARY = re.compile(
    r"^(?:def |class |async def |function |const |let |var |export |import "
    r"|from |type |struct |impl |enum |trait |interface |public |private "
    r"|protected |func |fn |package |module |@|#\[|///|/\*\*)"
)

mcp = _Server("scopegrep", version="0.3.4")
_scope_cache = {}          # local_key -> {"chunks","meta","scope_key","built_at"}


# ------------------------------------------------------------------- chunking

def _norm_globs(v, default=None):
    if v is None:
        return list(default or [])
    if isinstance(v, str):
        v = [p.strip() for p in v.split(",") if p.strip()]
    return list(v)


def _glob_regex(p):
    """Translate one glob to a regex with real globstar semantics.

    The previous implementation expanded `**/` by string substitution and
    stopped after the FIRST occurrence, so `src/**/sub/**/*.py` matched
    nothing that needed both segments elided -- a scope that silently excludes
    the files the caller asked for is worse than one that errors. Translating
    once, here, gives every pattern the semantics bash globstar, gitignore and
    ripgrep already share:

      `**/`  zero or more directories
      `/**`  everything below this directory
      `*`    any run of characters WITHIN one path segment
      `?`    one character within one segment

    fnmatch cannot express the last two: its `*` crosses `/`, which is why
    `src/*.py` used to match `src/pkg/deep.py`.
    """
    p = p.strip().replace(os.sep, "/").lstrip("/")
    out, i, n = [], 0, len(p)
    while i < n:
        c = p[i]
        if c == "*":
            if p[i:i + 2] == "**":
                j = i + 2
                if p[j:j + 1] == "/":          # `**/` -> zero or more dirs
                    out.append("(?:[^/]+/)*")
                    i = j + 1
                else:                           # trailing `**` -> anything
                    out.append(".*")
                    i = j
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = p.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = p[i + 1:j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


_GLOB_CACHE = {}


def _compile_glob(p):
    """`a/**` also covers `a/` itself, the way `git add a/**` behaves."""
    if p not in _GLOB_CACHE:
        body = _glob_regex(p)
        if body.endswith("/.*"):
            body = body[:-3] + "(?:/.*)?"
        _GLOB_CACHE[p] = re.compile(body + r"\Z")
    return _GLOB_CACHE[p]


def _matches(rel, patterns):
    rel = rel.replace(os.sep, "/").lstrip("/")
    for p in patterns:
        if _compile_glob(p).match(rel):
            return True
        # a bare "*.py" should also match "pkg/mod.py", the way a coder expects
        if "/" not in p and _compile_glob(p).match(os.path.basename(rel)):
            return True
    return False


class _Ignore:
    """The subset of gitignore semantics a scope walk actually needs.

    Not a full gitignore implementation: no `**` in the middle of a pattern,
    no re-inclusion of a file whose parent directory was excluded (git has the
    same restriction). It handles anchoring, directory-only patterns, negation
    and per-directory files, which is what decides whether `ignored.py` and a
    `buildcache/` tree land in a scope the user believes is their source.
    """

    def __init__(self):
        self.rules = []          # (dir_prefix, pattern, dir_only, negated)

    def add_file(self, path, rel_dir):
        try:
            with open(path, "r", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return
        for raw in lines:
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            line = line.lstrip("/")
            prefix = rel_dir.replace(os.sep, "/").strip("/")
            self.rules.append((prefix, line, dir_only, negated, anchored))

    def ignored(self, rel, is_dir):
        rel = rel.replace(os.sep, "/").strip("/")
        verdict = False
        for prefix, pattern, dir_only, negated, anchored in self.rules:
            if prefix and not (rel == prefix or rel.startswith(prefix + "/")):
                continue
            local = rel[len(prefix) + 1:] if prefix else rel
            if dir_only and not is_dir and "/" not in local:
                continue
            if anchored:
                hit = _matches(local, [pattern]) or _matches(local, [pattern + "/**"])
            else:
                hit = (_matches(local, [pattern])
                       or _matches(local, ["**/" + pattern])
                       or _matches(local, ["**/" + pattern + "/**"])
                       or _matches(local, [pattern + "/**"]))
            if hit:
                verdict = not negated
        return verdict


# Dotfiles that carry credentials rather than source. A retrieval tool that
# ships these off-machine, or quotes them back into a transcript, has
# exfiltrated them; being inside the declared root is not consent.
SENSITIVE_NAMES = {
    ".env", ".envrc", ".netrc", ".npmrc", ".pypirc", ".dockercfg",
    ".git-credentials", ".htpasswd", "id_rsa", "id_ed25519", "credentials",
}
SENSITIVE_GLOBS = ("**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12",
                   "**/*.pfx", "**/*_rsa", "**/*.keystore", "**/.aws/**",
                   "**/.ssh/**")


def _is_sensitive(rel):
    base = os.path.basename(rel)
    return base in SENSITIVE_NAMES or _matches(rel, SENSITIVE_GLOBS)


def _walk(root, include, exclude, use_ignore_files=True, follow_symlinks=False):
    """Walk `root` and return (files, coverage).

    Three containment rules the previous walk did not enforce, each of which
    put content in a scope the caller had not asked for:

    * `.gitignore` / `.ignore` are honoured. Generated trees and vendored
      output are not source, and a scope that silently includes them spends
      the caller's budget ranking artifacts against their question.
    * credential-shaped files are refused outright, and refusing is reported.
    * every file is resolved with `realpath` and must still be under
      `realpath(root)`. A symlink is a perfectly ordinary repository fixture,
      and one pointing at `../../secrets/prod.py` used to be read, chunked and
      shipped to the service exactly like a source file.

    `coverage` reports what was skipped and why, so the caller can see that a
    scope is incomplete instead of inferring it from a small chunk count.
    """
    root_real = os.path.realpath(root)
    out = []
    coverage = {"ignored_by_ignore_file": 0, "sensitive_refused": 0,
                "outside_root": 0, "binary": 0, "too_large": 0, "empty": 0,
                "unreadable": 0, "excluded": 0, "not_included": 0,
                "ignore_files_read": 0}
    ignore = _Ignore()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir

        if use_ignore_files:
            for name in (".gitignore", ".ignore"):
                candidate = os.path.join(dirpath, name)
                if os.path.isfile(candidate):
                    ignore.add_file(candidate, rel_dir)
                    coverage["ignore_files_read"] += 1

        kept_dirs = []
        for d in dirnames:
            rel = os.path.join(rel_dir, d) if rel_dir else d
            if d.startswith(".git") or _matches(rel + "/x", exclude):
                continue
            if use_ignore_files and ignore.ignored(rel, True):
                coverage["ignored_by_ignore_file"] += 1
                continue
            if not follow_symlinks and os.path.islink(os.path.join(dirpath, d)):
                coverage["outside_root"] += 1
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fn in filenames:
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            full = os.path.join(dirpath, fn)
            if _is_sensitive(rel):
                coverage["sensitive_refused"] += 1
                continue
            if os.path.splitext(fn)[1].lower() in BINARY_EXT:
                coverage["binary"] += 1
                continue
            if _matches(rel, exclude):
                coverage["excluded"] += 1
                continue
            if use_ignore_files and ignore.ignored(rel, False):
                coverage["ignored_by_ignore_file"] += 1
                continue
            if include and not _matches(rel, include):
                coverage["not_included"] += 1
                continue
            real = os.path.realpath(full)
            if not (real == root_real or real.startswith(root_real + os.sep)):
                coverage["outside_root"] += 1
                continue
            try:
                st = os.stat(full)
            except OSError:
                coverage["unreadable"] += 1
                continue
            if st.st_size == 0:
                coverage["empty"] += 1
                continue
            if st.st_size > MAX_FILE_BYTES:
                coverage["too_large"] += 1
                continue
            out.append((rel, full, st.st_size, st.st_mtime))
    out.sort()
    return out, coverage


def _coverage_note(coverage):
    """One line naming what the scope does NOT contain, or None."""
    interesting = {
        "ignored_by_ignore_file": "ignored by .gitignore/.ignore",
        "sensitive_refused": "refused as credential-shaped",
        "outside_root": "symlinked outside the declared root",
        "too_large": f"larger than {MAX_FILE_BYTES:,} bytes",
        "unreadable": "unreadable",
    }
    parts = [f"{coverage[key]} {text}" for key, text in interesting.items()
             if coverage.get(key)]
    if not parts:
        return None
    return "scope coverage: excluded " + ", ".join(parts)


def _read_text(path):
    try:
        with open(path, "r", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return None


def _split_windows(lines, chunk_chars):
    """Cut `lines` into windows of about `chunk_chars`, ending a window at a
    top-level definition or a blank line when one is within reach, so a chunk
    rarely starts mid-function."""
    windows, cur, cur_chars, start = [], [], 0, 0
    for i, line in enumerate(lines):
        cur.append(line)
        cur_chars += len(line)
        if cur_chars < chunk_chars * 0.75:
            continue
        at_boundary = (i + 1 < len(lines) and
                       (BOUNDARY.match(lines[i + 1]) or not lines[i + 1].strip()))
        if cur_chars >= chunk_chars or at_boundary:
            windows.append((start, i, "".join(cur)))
            cur, cur_chars, start = [], 0, i + 1
    if cur:
        windows.append((start, len(lines) - 1, "".join(cur)))
    return windows


SYMBOL = re.compile(
    r"^(\s*)(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn|impl|struct"
    r"|interface|type)\s+([A-Za-z_][\w.]*)")


def _enclosing_symbol(lines, start):
    """Name the definition a window falls inside.

    Window splitting cuts files mid-body, so a chunk routinely opens on a line
    like `current = current()` with its `def` twenty lines above the cut. The
    path alone does not tell the reader -- model or human -- which function
    they are looking at, and a chunk that cannot identify itself loses to a
    neighbour that opens on a clean `def`, even when the unlabeled chunk was
    the actually correct answer.

    Scans backwards for the nearest definition at a shallower indent than the
    window's own first line, then for an enclosing class above that."""
    depth = None
    for ln in lines[start:start + 12]:
        if ln.strip():
            depth = len(ln) - len(ln.lstrip())
            break
    if depth is None:
        return None
    inner, inner_indent = None, None
    for i in range(start - 1, -1, -1):
        m = SYMBOL.match(lines[i])
        if not m:
            continue
        indent = len(m.group(1))
        if inner is None:
            if indent < depth or (indent == depth and i == start - 1):
                inner, inner_indent = m.group(2), indent
                if indent == 0:
                    break
        elif indent < inner_indent and lines[i].lstrip().startswith("class "):
            return f"{m.group(2)}.{inner}"
    return inner


def build_chunks(include, exclude, split="window", chunk_chars=CHUNK_CHARS,
                 root=ROOT, max_chunks=MAX_CHUNKS):
    """Return (chunks, meta, truncated_note).

    `split="head"` is the best-exercised shape: one chunk per file, the
    file's first `chunk_chars` characters, nothing else. Content past that
    point is not retrievable, because the scorer never sees it.

    `split="window"` covers whole files by cutting them into windows of about
    `chunk_chars`. Strictly more of the repo becomes reachable this way, but
    it is a less-exercised code path than symbol-split scoping, so treat its
    ranking quality as guidance rather than a promise.

    Either way every chunk opens with "# File: <relpath>" (plus the line range
    in window mode). That header is not decoration: the scorer only sees a
    short summary of each chunk in its first pass, so the path is most of
    what it has to rank on, and dropping the header measurably hurts
    ranking quality."""
    files, coverage = _walk(root, include, exclude)
    chunks, meta = [], []
    for rel, full, size, mtime in files:
        text = _read_text(full)
        if text is None or not text.strip():
            continue
        revision = _content_revision(full, size)
        # `header_lines` is how many synthetic lines this chunk carries before
        # its first line of real source. Every conversion from an offset
        # WITHIN a chunk to a source line number has to subtract it. Leaving
        # it implicit is what produced the miscitation recorded in the audit
        # as `reference_line_probe`: a definition on config.py line 1 was
        # reported as config.py:2, because the "# File:" header occupied the
        # offset the arithmetic assumed was source.
        if split == "head":
            body = text[:chunk_chars]
            n_lines = body.count("\n") + 1
            chunks.append(f"# File: {rel}\n{body}")
            meta.append({"path": rel, "start_line": 1, "end_line": n_lines,
                         "header_lines": 1, "revision": revision,
                         "whole_file": len(text) <= chunk_chars})
        else:
            lines = text.splitlines(keepends=True)
            for a, b, body in _split_windows(lines, chunk_chars):
                sym = _enclosing_symbol(lines, a)
                head = f"# File: {rel} (lines {a+1}-{b+1})"
                if sym:
                    head += f" -- inside {sym}"
                chunks.append(f"{head}\n{body}")
                meta.append({"path": rel, "start_line": a + 1, "end_line": b + 1,
                             "symbol": sym, "header_lines": 1,
                             "revision": revision,
                             "whole_file": len(lines) <= (b - a + 1)})
        if len(chunks) >= max_chunks:
            note = (f"scope truncated at {max_chunks} chunks (service cap) after "
                    f"{rel}; narrow `include` or use split='head'")
            return chunks[:max_chunks], meta[:max_chunks], note, coverage
    return chunks, meta, None, coverage


# Bump when chunk shape, header text, symbol naming, or window splitting
# changes. It is part of the cache identity because a cache entry built by a
# different chunker is not the same evidence, however unchanged the source is.
CHUNKER_VERSION = "window-v2-2026-09-06"


def _content_revision(full, size):
    """SHA-256 of the file's bytes, not its (size, mtime) metadata.

    A same-length edit with the mtime restored -- what `git stash`,
    `git checkout`, `sed -i` on an equal-width replacement, and any editor
    that preserves timestamps all produce -- would keep a (size, mtime)-based
    cache entry valid and return the pre-edit body as current evidence.
    Timestamps are a fast hint; the content hash is the proof.
    """
    h = hashlib.sha256()
    try:
        with open(full, "rb") as fh:
            while True:
                block = fh.read(1 << 20)
                if not block:
                    break
                h.update(block)
    except OSError:
        return f"unreadable:{size}"
    return h.hexdigest()


def _local_key(include, exclude, split, chunk_chars, root):
    """Cache identity over CONTENT plus every setting that shapes a chunk."""
    h = hashlib.sha256()
    h.update(json.dumps([sorted(include), sorted(exclude), split, chunk_chars,
                         root, CHUNKER_VERSION, CHARS_PER_TOKEN,
                         BOUNDARY.pattern, SYMBOL.pattern]).encode())
    files, _coverage = _walk(root, include, exclude)
    for rel, full, size, _mtime in files:
        h.update(f"{rel}:{size}:{_content_revision(full, size)}\x00".encode())
    return h.hexdigest()


def get_scope(include, exclude, split, chunk_chars, root, max_chunks=MAX_CHUNKS):
    key = _local_key(include, exclude, split, chunk_chars, root)
    hit = _scope_cache.get(key)
    if hit:
        return hit, True
    chunks, meta, note, coverage = build_chunks(include, exclude, split,
                                                chunk_chars, root, max_chunks)
    entry = {"chunks": chunks, "meta": meta, "note": note, "scope_key": None,
             "built_at": time.time(), "local_key": key, "coverage": coverage,
             "chunker_version": CHUNKER_VERSION}
    _scope_cache.clear()          # one scope in flight is the usage pattern
    _scope_cache[key] = entry
    return entry, False


# ----------------------------------------------------------------------- http

def _post(path, body):
    if not URL or not TOKEN:
        raise RuntimeError(
            "scopegrep is not configured: no service token found. Set "
            "SCOPEGREP_TOKEN, or write it to ~/.config/scopegrep/token "
            "(see the plugin README, step 2).")
    r = httpx.post(f"{URL}{path}", json=body, timeout=HTTP_TIMEOUT, follow_redirects=True,
                   headers={"X-Scopegrep-Token": TOKEN})
    return r


DEFAULT_BUDGET_TOKENS = 3000
MAX_BUDGET_TOKENS = 16000


def _est_tokens(text):
    """Client-side token estimate. No tokenizer is available in this process,
    so a fixed chars-per-token ratio stands in for one. Reported as an
    estimate everywhere it is surfaced, never as a count."""
    return int(len(text) / CHARS_PER_TOKEN)


def _recommend_budget(n_chunks, mean_tokens):
    """Budget guidance, scope-aware, in tokens rather than in k.

    The old guidance answered every scope with "k=20" -- including a scope of
    six chunks, where k=20 is the whole scope and the ranking is decoration.
    It also recommended k without reference to what k costs, which is the
    quantity the caller is actually spending.

    The thresholds below reflect where result quality levels off as the
    budget grows: a small scope reaches its best achievable quality with a
    small budget, and past a few thousand tokens further budget buys very
    little on a typical code question.
    """
    scope_tokens = int(n_chunks * mean_tokens)
    if n_chunks <= 8:
        recommended = min(scope_tokens, 2000)
        why = (f"this scope is {n_chunks} chunks (~{scope_tokens:,} tokens) -- "
               "small enough that ranking buys little; a budget covering the "
               "whole scope is reasonable.")
    elif n_chunks <= 60:
        recommended = 2000
        why = (f"{n_chunks} chunks -- a scope this size reaches good quality "
               "with a modest budget; going much higher buys little more.")
    else:
        recommended = 4000
        why = (f"{n_chunks} chunks -- quality keeps improving with budget up "
               "to a few thousand tokens, then flattens out.")
    return {
        "recommended_budget_tokens": recommended,
        "why": why,
        "est_chunks_at_budget": {
            str(b): max(1, int(b / mean_tokens)) for b in (1000, 2000, 4000, 8000)
        },
        "scope_tokens": scope_tokens,
    }


HEADER_RESERVE_TOKENS = 120     # measured ceiling for the response header


# Width of the self-reported size slot. Wide enough for any response this
# tool can produce, and FIXED so that stamping the real number in does not
# change the length of the string the number describes.
_SIZE_SLOT_WIDTH = 7
_SIZE_SLOT = "\x00" * _SIZE_SLOT_WIDTH


def _stamp_size(text):
    """Replace the size placeholder with the rendered string's true size.

    A response that reports its own cost has a circularity to resolve: the
    number changes the length of the string it measures. A fixed-width slot
    settles it in one pass -- the placeholder is exactly as wide as what
    replaces it, so the measurement taken with the placeholder in place is
    still true of the stamped result.

    The alternative, counting only the evidence bodies, is what 0.2.1 shipped:
    it under-stated every response by 106-140 tokens (mean 127, 13% of a 1k
    budget) because the header, the omitted-items list, the resolved
    references and the coverage note all sit outside the sum. The string was
    genuinely in budget; the number it printed about itself was not.
    """
    total = _est_tokens(text)
    stamped = f"{total:,}".rjust(_SIZE_SLOT_WIDTH)[:_SIZE_SLOT_WIDTH]
    return text.replace(_SIZE_SLOT, stamped, 1), total


# How many omitted locations to name, in the order the fitter will try them.
# Metadata is trimmed BEFORE evidence: on a measured run the previous
# behaviour dropped a whole 375-token source chunk to shed a 2-token overrun,
# taking utilisation from 100.0% to 90.9%. Naming three omissions instead of
# six costs the caller far less than losing a chunk, and the omission is still
# reported -- just less verbosely.
_OMIT_LOC_STEPS = (6, 3, 1, 0)


def _fit_to_budget(render, items, budget_tokens, omitted):
    """Render, then shed metadata and finally evidence until the string fits.

    `_pack_response` charges a FIXED reserve for the header, but the header's
    real size is not known until packing is done: the omitted-items line, the
    resolved-references block and the coverage note are all appended
    afterwards, and a fixed reserve can under-count that by a small margin --
    inside budget most of the time, but only by luck. This turns
    "approximately within budget" into a guarantee.

    Order matters. Verbosity goes first, evidence last: a shorter omission
    list still reports the omission, whereas a dropped chunk is gone. A single
    oversized item is still returned whole and reported, because half a
    definition is not evidence.
    """
    kept = list(items)
    max_locs = _OMIT_LOC_STEPS[0]
    text = render(kept, omitted, max_locs)
    if _est_tokens(text) <= budget_tokens:
        return text, kept, omitted, max_locs

    for step in _OMIT_LOC_STEPS[1:]:
        max_locs = step
        text = render(kept, omitted, max_locs)
        if _est_tokens(text) <= budget_tokens:
            return text, kept, omitted, max_locs

    while len(kept) > 1 and _est_tokens(text) > budget_tokens:
        omitted.insert(0, kept.pop())
        text = render(kept, omitted, max_locs)
    return text, kept, omitted, max_locs


def _pack_response(items, budget_tokens, reserve_tokens=HEADER_RESERVE_TOKENS):
    """Choose whole evidence items that fit the FINAL serialized budget.

    The budget covers everything the caller receives -- header, per-item
    location lines, and bodies -- because that whole string is what lands in
    their context. The previous behaviour budgeted only bodies and then let a
    separate character cap truncate the serialized result, which could cut a
    source item in half while keeping its full span metadata: evidence that
    looks complete and is not.

    Returns (kept, omitted, used_tokens). An item is included whole or not at
    all, and the first item is always included so a single oversized chunk
    still produces evidence rather than an empty answer.
    """
    used = reserve_tokens
    kept, omitted = [], []
    for item in items:
        cost = _est_tokens(item["text"])
        if not kept or used + cost <= budget_tokens:
            kept.append(item)
            used += cost
        else:
            omitted.append(item)
    return kept, omitted, used


# ---------------------------------------------------------------------- tools

@mcp.tool()
def scopegrep_status() -> str:
    """Check the scopegrep service: is it warm, what scopes does it hold,
    how long is a cold start.

    Call this first if a retrieval is about to matter, or if you want to know
    whether the next query pays a cold start. The service goes idle after a
    period of no use, so the first request after that can take a while to
    become ready; subsequent requests against the same scope are fast."""
    try:
        r = httpx.get(f"{URL}/health", timeout=HTTP_TIMEOUT, follow_redirects=True,
                      headers={"X-Scopegrep-Token": TOKEN})
    except Exception as e:                                  # noqa: BLE001
        return f"scopegrep service unreachable at {URL or '<unset>'}: {e}"
    if r.status_code != 200:
        return f"scopegrep /health -> HTTP {r.status_code}: {r.text[:400]}"
    h = r.json()
    lines = [
        f"service:   {URL}",
        f"instance:  up {h['container_uptime_seconds']}s "
        f"(startup took {h['model_load_seconds']}s); "
        # A deployment held warm and one that scales to zero answer /health
        # identically apart from this field, and the difference is the whole
        # question of whether the next call blocks for a minute or two.
        + (f"held warm ({h['min_containers']} container(s) always on)"
           if h.get("min_containers")
           else f"scales to zero after {h['scaledown_window_seconds']}s idle"),
        f"warm scopes ({len(h['cached_scopes'])}):",
    ]
    for s in h["cached_scopes"] or []:
        lines.append(f"  {s['scope_key']}  {s['n_chunks']} chunks, "
                     f"{s['prefix_tokens']} tokens preprocessed, "
                     f"{s['n_probes']} queries served")
    if not h["cached_scopes"]:
        lines.append("  (none -- the next retrieval pays the first-time setup cost)")
    lines.append(f"local scope cache: {len(_scope_cache)} entry(ies), root={ROOT}")
    return "\n".join(lines)


@mcp.tool()
def scopegrep_scope(include: list[str] | str | None = None,
                   exclude: list[str] | str | None = None,
                   split: str = "window",
                   chunk_chars: int = CHUNK_CHARS,
                   root: str | None = None) -> str:
    """Preview a search scope WITHOUT calling the service: how many files and
    chunks it contains, roughly how many tokens, and what k to ask for.

    Free and instant -- it only walks the filesystem. Use it before a first
    retrieval on an unfamiliar repo, or whenever you are unsure whether a
    glob is too broad. A scope of more than ~2000 chunks is rejected by the
    service; a scope of a few hundred chunks is where this performs best.

    include: glob patterns to search, e.g. ["src/**/*.py", "*.md"]. Bare
        patterns like "*.py" match at any depth. Empty means every text file
        under root, which is usually too broad to be useful -- scope down to
        the subsystem the question is about.
    exclude: extra globs to skip; sensible defaults (.git, node_modules,
        build dirs, lockfiles, minified assets, binaries) always apply.
    split: "window" cuts whole files into ~chunk_chars windows (covers the
        whole file; the more general shape). "head" keeps only each file's
        first chunk_chars characters, the best-exercised shape.
    chunk_chars: target chunk size. 1500 is the default. Going much below
        ~1000 makes the path header dominate what a short summary of the
        chunk can capture, which hurts ranking quality.
    """
    inc = _norm_globs(include)
    exc = DEFAULT_EXCLUDES + _norm_globs(exclude)
    rt = os.path.abspath(root) if root else ROOT
    t0 = time.time()
    entry, cached = get_scope(inc, exc, split, chunk_chars, rt)
    chunks, meta = entry["chunks"], entry["meta"]
    if not chunks:
        return (f"scope is empty: no text files under {rt} matched "
                f"include={inc or ['<everything>']}. Check the globs.")
    chars = sum(len(c) for c in chunks)
    est_tokens = int(chars / CHARS_PER_TOKEN)
    mean_tok = est_tokens / len(chunks)
    n_files = len({m["path"] for m in meta})
    rec = _recommend_budget(len(chunks), mean_tok)
    out = [
        f"scope: {n_files} files -> {len(chunks)} chunks, ~{est_tokens:,} tokens "
        f"(~{mean_tok:.0f} tok/chunk, estimated at {CHARS_PER_TOKEN} chars/token), "
        f"split={split}, built in {time.time()-t0:.2f}s"
        f"{' (cached)' if cached else ''}",
        f"root: {rt}",
        f"chunker: {entry['chunker_version']}",
        "",
        f"suggested budget_tokens: {rec['recommended_budget_tokens']:,} "
        f"(~{max(1, int(rec['recommended_budget_tokens'] / mean_tok))} chunks)",
        rec["why"],
    ]
    note = _coverage_note(entry.get("coverage") or {})
    if note:
        out += ["", note]
    if entry["note"]:
        out += ["", "WARNING: " + entry["note"]]
    if len(chunks) > 1200:
        out.append("WARNING: this scope is larger than what this has been "
                   "well-exercised against. Result quality past this size "
                   "is less certain.")
    return "\n".join(out)


@mcp.tool()
def scopegrep_retrieve(query: str,
                      include: list[str] | str | None = None,
                      exclude: list[str] | str | None = None,
                      budget_tokens: int = DEFAULT_BUDGET_TOKENS,
                      k: int | None = None,
                      output: str = "chunks",
                      split: str = "window",
                      chunk_chars: int = CHUNK_CHARS,
                      root: str | None = None,
                      mode: str = "codegen") -> str:
    """Retrieve the k most query-relevant chunks of a declared scope, ranked
    by meaning rather than by keyword overlap.

    USE THIS FOR SEMANTIC QUERIES -- "where is retry backoff configured",
    "which module owns session invalidation", "what handles the migration
    rollback path". It reads the whole scope and ranks every chunk together
    in one pass, so it finds code that never mentions your words.

    Use it when the question is behaviour and you do not have the vocabulary:
    a symptom, an issue body, "which part decides X". If you already have a
    symbol name, an error string, or a path, Grep is faster, free, and better
    at it for that kind of query.

    One call is meant to be enough. Scope is resolved and cached internally,
    so calling scopegrep_scope first is optional -- do it when you want to see
    a chunk count before spending, not as a required step.

    query: describe what you are looking for in full sentences, and paste the
        real artifact -- the traceback, the failing test, the issue text. The
        query is scored at full resolution against every chunk, so more
        genuine detail helps. This is not a keyword box.
    budget_tokens: the size of the whole response, headers included. Evidence
        items are included whole or not at all, and anything dropped is
        reported by location so you can ask for it. 3k is a sensible default
        for most scopes; raise it for a wide scope, lower it when you only
        need a pointer.
    k: optional hard cap on the number of chunks, for when you want exactly n
        locations. Leave it unset and the budget decides.
    output: "chunks" (the default) inlines each returned chunk's text -- a
        ~1500-character window, never a whole file. That text is the evidence.
        "files" returns ranked locations only, for when you intend to read the
        files yourself.
    include/exclude/split/chunk_chars: see scopegrep_scope.
    mode: prompt framing for the scorer. "codegen" for source code, "short"
        for prose, docs, schemas, or tool descriptions.
    """
    if not isinstance(budget_tokens, int) or isinstance(budget_tokens, bool):
        return "budget_tokens must be an integer number of tokens."
    if not 100 <= budget_tokens <= MAX_BUDGET_TOKENS:
        return (f"budget_tokens must be between 100 and {MAX_BUDGET_TOKENS}; "
                f"got {budget_tokens}.")
    inc = _norm_globs(include)
    exc = DEFAULT_EXCLUDES + _norm_globs(exclude)
    rt = os.path.abspath(root) if root else ROOT
    entry, _cached = get_scope(inc, exc, split, chunk_chars, rt)
    chunks, meta = entry["chunks"], entry["meta"]
    if not chunks:
        return (f"scope is empty: nothing under {rt} matched "
                f"include={inc or ['<everything>']}.")

    # Pre-flight: an over-wide scope used to be truncated to the service cap and
    # sent anyway, where it came back as a bare HTTP 500 with nothing the caller
    # could act on -- so the tool just got abandoned. Fail here instead, with the
    # numbers and the fix.
    if len(chunks) > SAFE_CHUNKS:
        return (
            f"scope too wide: {len(chunks)} chunks from include="
            f"{inc or ['<everything>']} under {rt}.\n"
            f"Scopes work best up to ~{SAFE_CHUNKS} chunks, and the "
            f"service rejects more than {MAX_CHUNKS}.\n"
            "Narrow `include` to one subsystem -- a directory of plausible "
            "files, e.g. ['src/some_subsystem/**/*.py'] rather than ['**/*.py'] -- "
            "and call scopegrep_scope first to see the chunk count before you "
            "retrieve. Retrieval ranks within the scope you declare; it cannot "
            "search a scope it was never given.")

    # Ask the service for enough candidates to fill the budget, then decide
    # inclusion here where the serialized cost is known. `k` remains available
    # as a hard cap for callers who want exactly n locations.
    mean_chunk_tokens = max(1.0, sum(len(c) for c in chunks) /
                            CHARS_PER_TOKEN / len(chunks))
    want = int(budget_tokens / mean_chunk_tokens) + 4
    ask = max(1, min(32, want if k is None else int(k)))

    body = {"query": query, "k": ask, "mode": mode}
    if entry["scope_key"]:
        body["scope_key"] = entry["scope_key"]
    else:
        body["chunks"] = chunks

    t0 = time.time()
    try:
        r = _post("/retrieve", body)
        if r.status_code == 409:
            # container scaled to zero or evicted this scope -- resend the text
            body.pop("scope_key", None)
            body["chunks"] = chunks
            r = _post("/retrieve", body)
    except Exception as e:                                  # noqa: BLE001
        return f"scopegrep service call failed ({URL or '<unset>'}): {e}"
    if r.status_code != 200:
        return f"scopegrep /retrieve -> HTTP {r.status_code}: {r.text[:600]}"
    res = r.json()
    entry["scope_key"] = res["scope_key"]
    wall = time.time() - t0
    scoring = (res["stage1"].get("index_build_seconds", 0) +
              res["stage1"]["seconds"] + res["stage2"]["seconds"])

    items = []
    for item in res["top_k"]:
        m = meta[item["index"]]
        loc = f"{m['path']}:{m['start_line']}-{m['end_line']}"
        if m.get("symbol"):
            loc += f"  (inside {m['symbol']})"
        flag = "" if item["reranked"] else "  [preliminary rank]"
        head = f"--- #{item['rank']+1}  {loc}  rev {m.get('revision','?')[:8]}{flag}"
        text = head if output != "chunks" else head + "\n" + chunks[item["index"]] + "\n"
        items.append({"text": text, "loc": loc, "index": item["index"]})

    # Reserve room for the header before deciding what evidence fits, so the
    # serialized total -- not just the bodies -- honours the budget.
    kept, omitted, used = _pack_response(items, budget_tokens)

    def _render(kept_items, omitted_items, max_locs):
        hdr = [
            f"scopegrep: {len(kept_items)} evidence items, "
            f"~{_SIZE_SLOT} est. tokens "
            f"of a {budget_tokens:,} budget; scope {len(chunks)} chunks, "
            f"scored to rank {res['ranking_valid_to_k']} at full resolution",
            f"{wall:.1f}s wall / {scoring:.1f}s scoring"
            + ("  (the rest was a cold start; the next query is fast)"
               if wall - scoring > 15 else ""),
        ]
        if omitted_items:
            shown = [item["loc"] for item in omitted_items[:max_locs]]
            hdr.append(
                f"omitted {len(omitted_items)} lower-ranked item(s) to stay in "
                "budget"
                + ((": " + ", ".join(shown)
                    + ("..." if len(omitted_items) > max_locs else ""))
                   if shown else "")
                + " -- re-ask with a larger budget_tokens to see them.")
        # The single-call path is now the recommended one, so it has to carry
        # the dangling-reference resolver too. It used to exist only on
        # `scopegrep_multi_retrieve` -- recommending a path without it would
        # reintroduce the failure that resolver exists to prevent (see
        # `_resolve_dangling`'s docstring).
        refs = _resolve_dangling([item["index"] for item in kept_items],
                                 chunks, meta)
        if refs:
            hdr.append(
                "REFERENCES RESOLVED ELSEWHERE IN SCOPE -- symbols these chunks "
                "use but do not define. A binding shown as AMBIGUOUS has more "
                "than one candidate and is not an answer:")
            hdr.extend(refs)
        # The outward half of the same idea. `_resolve_dangling` answers what
        # this code depends on; this answers what depends on it. Both are
        # completeness questions the ranking cannot express, and both are
        # cheaper to answer here than to make the caller spend a turn on. They
        # differ in reach: a dangling reference must resolve inside the
        # declared scope to be trustworthy, while a caller can sit anywhere,
        # so this one greps the repository instead of the chunks.
        callers = _resolve_outbound([item["index"] for item in kept_items],
                                    chunks, meta, root=rt)
        if callers:
            hdr.append(
                "WHAT ELSE CALLS WHAT YOU JUST READ -- every call site in "
                "scope for the symbols these chunks define, ranked by relevance "
                "or not. Change one of these symbols and each site listed is a "
                "decision; a symbol with none is nothing to decide:")
            hdr.extend(callers)
        note = _coverage_note(entry.get("coverage") or {})
        if note:
            hdr.append(note)
        for w in res.get("warnings", []):
            hdr.append("WARNING: " + w)
        if entry["note"]:
            hdr.append("WARNING: " + entry["note"])
        return "\n".join(hdr + [""] + [item["text"] for item in kept_items])

    text, kept, omitted, _max_locs = _fit_to_budget(
        _render, kept, budget_tokens, omitted)
    text, _total = _stamp_size(text)
    return text


# --------------------------------------------------------------- bulk retrieval

RRF_K = 60          # damping constant for combining several rankings into one

# Attribute-style references worth resolving: lowercase-led (excludes Class/
# CONST-like names, which are not the literal-default lookups this resolves),
# long enough not to be a common method call, and NOT restricted to snake_case
# -- lowerCamelCase (JS/Java/Go-convention) attributes must match too, or the
# resolver silently finds nothing outside snake_case codebases. Method calls
# are excluded by requiring the name NOT be followed by "(" -- but the
# exclusion only works if the name is matched to its END. Without `\b`, `{4,}`
# backtracks: on `object.append(value)` it gave up the final "d", matched
# "appen", saw "d" rather than "(" ahead, and reported a reference to a symbol
# that does not exist. `\b` pins the match to the whole identifier, so
# `.append(` is once again just a method call.
_REF = re.compile(r"\.([a-z][a-zA-Z0-9_]{4,})\b(?!\s*\()")
# `start_line`/`end_line` are this tool's own chunk-metadata field names, so
# they are self-references, not facts about the target repo -- generic to any
# corpus this runs against.
_REF_SKIP = {"start_line", "end_line"}


def _source_line(meta_entry, offset_in_chunk):
    """Map a 0-based offset within a chunk's lines to a source line number.

    Returns None for an offset that lands inside the synthetic header, which
    has no source line to name. A caller that cannot get a real coordinate
    must say so rather than emit an approximate one.
    """
    header = meta_entry.get("header_lines", 1)
    if offset_in_chunk < header:
        return None
    return meta_entry["start_line"] + offset_in_chunk - header


def _resolve_dangling(returned_idx, chunks, meta, limit=3):
    """Find literal values for symbols a returned chunk depends on but does not
    define.

    The failure this fixes: the right chunk can end in a reference to a
    default or a constant whose actual value lives in another file entirely.
    Asked to answer from the returned chunks alone, a reader can pass over
    the genuinely correct chunk for a worse one that happens to spell out a
    literal value -- reasoning correctly from evidence that was incomplete.
    The definition is often already inside the declared scope, just not
    among the specific chunks returned, so this resolves it here for free
    rather than making the caller spend a turn on it."""
    shown = "\n".join(chunks[i] for i in returned_idx)
    names, seen = [], set()
    for m in _REF.finditer(shown):
        n = m.group(1)
        if n in seen or n in _REF_SKIP:
            continue
        seen.add(n)
        # only worth resolving if the returned chunks never assign it a value
        if re.search(rf"\b{re.escape(n)}\s*=\s*[\"'\d]", shown):
            continue
        names.append(n)
    out = []
    rest = [i for i in range(len(chunks)) if i not in set(returned_idx)]
    for n in names[:12]:
        pat = re.compile(rf"^\s*{re.escape(n)}\s*=\s*([\"'].*?[\"']|\d+|True|False|None)\s*,?\s*$")
        # Collect EVERY binding, not the first one found. A name assigned in
        # three modules has no single authoritative default, and presenting
        # one of them as the answer is how a plausible wrong default reaches
        # the agent with a citation attached.
        found = []
        for i in rest:
            for off, line in enumerate(chunks[i].splitlines()):
                m = pat.match(line)
                if not m:
                    continue
                line_no = _source_line(meta[i], off)
                if line_no is None:
                    continue
                where = f"{meta[i]['path']}:{line_no}"
                if (m.group(1), where) not in found:
                    found.append((m.group(1), where))
                break
        if not found:
            continue
        values = {value for value, _ in found}
        if len(found) == 1:
            out.append(f"  {n} = {found[0][0]}   ({found[0][1]})")
        elif len(values) == 1:
            sites = ", ".join(where for _, where in found[:3])
            out.append(f"  {n} = {found[0][0]}   (agrees at {len(found)} sites: {sites})")
        else:
            sites = "; ".join(f"{value} at {where}" for value, where in found[:3])
            out.append(f"  {n} = AMBIGUOUS -- {len(found)} distinct bindings in "
                       f"scope: {sites}. Resolve which one binds here before "
                       f"relying on it.")
        if len(out) >= limit:
            break
    return out


# Definitions inside a returned chunk. `_resolve_dangling` looks *inward* --
# what does this code use that it does not define -- and these look *outward*,
# so the pair needs both a definition matcher and a use matcher.
# Keyword-led definitions. The vocabulary is `SYMBOL`'s, deliberately: the
# chunker already names an enclosing definition in nine languages, and an
# outbound scan that only recognised `def`/`class` disagreed with its own
# chunker everywhere else -- on a Go or Rust or TypeScript repo the block fell
# back to whatever `meta['symbol']` happened to hold and silently reported
# less. Leading modifiers (`export`, `public`, `pub`, `static` ...) are skipped
# so a definition is recognised in the form the language actually writes it.
#
# `\b` after the keyword group makes alternation order irrelevant: "function"
# cannot be matched as "func" plus a stray "tion".
_DEF_MODIFIER = (r"(?:(?:export|default|public|private|protected|internal|"
                 r"static|final|abstract|open|override|inline|async|pub|"
                 r"declare)\b[ \t]+)*")
_DEF_KEYWORD = (r"(?:def|class|function|func|fn|impl|struct|interface|type|"
                r"trait|enum)")
# The name must be followed by something a definition is followed by. Several
# of these keywords are also English words -- `type of the array` and `class
# objects returned by` both sit in ordinary docstrings -- and a bare
# keyword-then-word rule reported `of`, `objects`, `while` and `kwargs` as
# defined symbols on real codebases. A definition continues with a
# delimiter (`(`, `{`, `:`, `=`, `<`, `[`, `;`), with end of line, or with
# another structural keyword (`type Foo struct`, `impl Display for Foo`);
# English prose continues with a word.
_DEF_TAIL = (r"(?=[(<{\[:=;]|$|\b(?:struct|interface|enum|for|extends|"
             r"implements|where)\b)")
_DEF_KW = re.compile(rf"^[ \t]*{_DEF_MODIFIER}{_DEF_KEYWORD}\b[ \t]+"
                     rf"([A-Za-z_]\w*)[ \t]*{_DEF_TAIL}", re.M)
# Typed definitions, where the name follows a return type rather than a
# keyword. Kept separate because it has to anchor on the opening parenthesis:
# without it, `cdef int foo(` yields "int". This covers Cython only. A
# C/C++/Java method definition is NOT matched here -- see `_defined_symbols`.
_DEF_TYPED = re.compile(r"^[ \t]*(?:cdef|cpdef)[ \t]+[\w\s\*\[\]]*?\b"
                        r"([A-Za-z_]\w*)[ \t]*\(", re.M)
# Names too common for a whole-word scan to mean anything, independent of any
# one repo's vocabulary. A name specific to one project's API (e.g. a method
# every estimator in some ML library defines) is not filtered here -- it is
# left to `rank()`'s flood bucket (below), which sorts any symbol with an
# unworkable call-site count to the bottom generically, by count, not by name.
_OUTBOUND_SKIP = {"main", "run", "get", "set", "call", "test", "setup",
                  "next", "close", "read", "write", "copy", "update", "keys",
                  "values", "items", "name", "value", "data", "self"}
# Above this many call sites the list stops being something a caller can decide
# about one at a time. The count is still exact and still reported -- what is
# withheld is the enumeration, because printing 300 locations spends the budget
# the ranked chunks need and tells the caller nothing they can act on.
_OUTBOUND_MAX_SITES = 12
_OUTBOUND_MAX_SYMBOLS = 6
# Which corpus the caller scan reads. "scope" is the default and only looks
# at the files the caller already declared; "repo" greps the whole
# repository instead, which can surface a real caller that a narrower
# declared scope missed entirely, at the cost of more noise.
_OUTBOUND_REPO_WIDE = os.environ.get("SCOPEGREP_OUTBOUND_SCOPE", "scope") == "repo"


def _defined_symbols(returned_idx, chunks, meta):
    """Symbols the returned chunks define, with the files defining them.

    Two sources, because neither alone is complete: `meta['symbol']` is the
    enclosing symbol the chunker recorded, which is present for symbol-split
    scopes and absent for window-split ones; the regexes catch definitions that
    sit inside a window chunk without being its header.

    Known gap, stated rather than hidden: a definition with no keyword in
    front of it -- a C, C++ or Java method, written `void Solver::Solve(...)`
    -- matches neither regex, and `SYMBOL` does not recognise it either, so
    such a chunk contributes no anchors at all. The caller scan below is
    language-independent and will happily report call sites in those files;
    it is the anchor side that cannot see them. Closing it means matching a
    definition by shape instead of by keyword, which is a different and much
    noisier rule, so it is left open and documented.
    """
    defined = {}
    for i in returned_idx:
        text, path = chunks[i], meta[i]["path"]
        names = set()
        sym = meta[i].get("symbol")
        if sym:
            # A chunker symbol can be qualified (`Class.method`); the source
            # token is the last component.
            names.add(sym.rsplit(".", 1)[-1])
        names.update(_DEF_KW.findall(text))
        names.update(_DEF_TYPED.findall(text))
        for n in names:
            if len(n) < 4 or n in _OUTBOUND_SKIP or n.startswith("__"):
                continue
            defined.setdefault(n, set()).add(path)
    return defined


# Paths whose call sites are not decisions about the change: the project's own
# test suite names every symbol it exercises, and on a repo-wide scan that is
# most of the hits. They are counted and reported as a total, never enumerated.
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$")
# Widening the scan to the repository brings in everything a repository holds
# that is not code -- prose and generated docs, fixtures, lockfiles -- and none
# of that is a place anybody edits to fix a bug.
#
# This used to be an allow-list of sixteen extensions, which is a language
# policy disguised as a file filter: a Ruby, C#, PHP, Swift or Kotlin
# repository matched none of them, so every symbol came back "no calls outside
# that file" -- the same answer a genuinely unreferenced symbol gets, with
# nothing to distinguish the two. A silent zero is the worst available failure
# for a completeness claim. It also dropped `.pyi` and `.sh`, which are source
# in the very language it was written for.
#
# Inverted: the scan is `git grep -I`, which has already excluded every binary,
# so what is left to exclude is text that is not code. Naming those kinds is
# finite and language-independent; naming every language is neither.
_NON_SOURCE_EXT = (
    # prose and generated documentation
    ".rst", ".md", ".markdown", ".adoc", ".txt", ".rtf", ".tex", ".pod",
    # data, fixtures and notebooks
    ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".properties", ".sql", ".dta", ".arff",
    ".ipynb", ".geojson", ".svg",
    # lockfiles and manifests nobody fixes a bug in
    ".lock", ".sum", ".mod", ".log", ".patch", ".diff", ".map",
    # markup that is content rather than program text
    ".html", ".htm", ".xhtml", ".css", ".scss", ".less",
    # binary and archived fixtures. `git grep -I` already skips these on the
    # repository scan, but `_is_source` is also called on the in-scope scan,
    # where the corpus is whatever the caller's globs matched -- so the two
    # paths must agree about them rather than one relying on grep's behaviour.
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".ods", ".sas7bdat", ".dta", ".sav",
    ".parquet", ".feather", ".orc", ".msgpack", ".odt", ".xsl", ".xslt",
    ".h5", ".hdf5", ".pickle", ".pkl", ".npy", ".npz",
    ".gz", ".bz2", ".xz", ".zip", ".tar", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf",
    ".so", ".dylib", ".dll", ".o", ".obj", ".a", ".lib", ".exe", ".class",
    ".pyc", ".pyd", ".wasm",
)
_NON_SOURCE_DIR = re.compile(r"(^|/)(doc|docs|benchmarks?|examples?|"
                             r"scripts|ci)/")
# A minified or generated bundle is text, is enormous, and is never the place a
# fix belongs. Matched on the name so it holds whatever the extension is.
_GENERATED = re.compile(r"(^|/)(vendor|node_modules|third_party|dist|build)/"
                        r"|\.min\.[a-z]+$|\.generated\.[a-z]+$|_pb2?\.[a-z]+$")


def _is_source(path):
    """Whether a call site in this file is a place somebody would fix a bug.

    Deny-list, not allow-list: an unrecognised extension is treated as source.
    A language this was never taught should produce call sites that look odd,
    not an empty block that looks authoritative.
    """
    low = path.lower()
    return (not low.endswith(_NON_SOURCE_EXT)
            and not _NON_SOURCE_DIR.search(path)
            and not _GENERATED.search(low))


def _call_site_skip(name):
    """Lines that name a symbol without being a decision about changing it.

    Three kinds: its own definition, a comment, and an import. Imports matter
    -- `SeriesGroupBy` picked up four of them across `__init__.py`, `base.py`,
    `groupby.py` and `ops.py`, which would have read as four call sites to
    decide about and are nothing of the kind. A bare name alone on a line is
    the continuation of a parenthesised `from x import (\\n    Name,\\n)` --
    four of `SeriesGroupBy`'s eight "call sites" were exactly that.

    Factored out of `_resolve_outbound` so that any second scan asking the
    same question from a different anchor cannot answer it by a different
    rule. Two scans disagreeing about what counts as a call site would make
    the pair uninterpretable, which is the whole reason to share it.
    """
    return re.compile(rf"^[ \t]*(?:#|//|\*)"
                      rf"|^[ \t]*(?:from|import)\b"
                      rf"|^[ \t]*{re.escape(name)}[ \t]*,?[ \t]*\)?[ \t]*$"
                      rf"|^[ \t]*(?:async[ \t]+)?"
                      rf"(?:def|class|cdef|cpdef)\b[^(]*\b{re.escape(name)}\b")


def _repo_callers(names, root):
    """Every reference to `names` in the whole repository, in one `git grep`.

    A scan bounded by the retrieval's own declared scope can miss the actual
    fix location entirely: if the real answer lives outside the files a
    caller happened to include, no amount of budget makes it appear, because
    it was never read in the first place.

    Retrieval has to stay scoped: it costs real time and money per file under
    a token budget. This scan is `git grep`, so it does not, and a blast
    radius does not respect the scope somebody guessed. One alternation for
    every symbol keeps it to a single subprocess.

    Returns {} when the root is not a git repository, which makes the caller
    fall back to the in-scope scan rather than reporting nothing.
    """
    if not names:
        return {}
    alt = "|".join(re.escape(n) for n in names)
    try:
        r = subprocess.run(
            ["git", "-C", root, "grep", "-n", "--word-regexp", "-I", "-E", alt],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode not in (0, 1):          # 1 is "no matches", not an error
        return {}
    by_name = {n: [] for n in names}
    for line in r.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path, lineno, text = parts
        if not lineno.isdigit():
            continue
        for n in names:
            if re.search(rf"\b{re.escape(n)}\b", text):
                by_name[n].append((path, int(lineno), text))
    return by_name


def _resolve_outbound(returned_idx, chunks, meta, root=None,
                      max_symbols=_OUTBOUND_MAX_SYMBOLS,
                      max_sites=_OUTBOUND_MAX_SITES):
    """Where else in scope the symbols these chunks *define* are called.

    The failure this fixes: an agent changes a shared helper that has several
    call sites, and patches only the one that a relevance ranking surfaced.
    The other call sites can share no vocabulary at all with the original
    question, so a relevance ranker is *right* not to return them under a
    token budget -- they carry no information about the bug being fixed. But
    the agent still needs to know they exist before it can decide the change
    is complete.

    Completeness is a different question from relevance, and asking it as a
    separate follow-up call is what makes it expensive: the answer is small,
    but a whole additional round trip re-sends everything already in context.
    The scope is already walked and chunked here, so this answers it in the
    same response instead.

    Nothing here decides whether the task is hard. The block's *size* is what
    the repository already contains -- one line when a symbol has no callers,
    more when it has several -- so the easy case stays cheap without anything
    having to classify it as easy.
    """
    defined = _defined_symbols(returned_idx, chunks, meta)
    if not defined:
        return []
    returned = set(returned_idx)
    rest = [i for i in range(len(chunks)) if i not in returned]
    repo = (_repo_callers(sorted(defined), root)
            if root and _OUTBOUND_REPO_WIDE else {})

    found = {}
    for name, def_files in defined.items():
        pat = re.compile(rf"\b{re.escape(name)}\b")
        skip = _call_site_skip(name)
        sites, outside, same_file, in_tests = [], 0, 0, 0

        def record(path, line_no, line):
            """Classify one occurrence. Shared by both scans so that widening
            the search cannot quietly change what counts as a call site."""
            nonlocal outside, same_file, in_tests
            if skip.search(line) or not _is_source(path):
                return
            # A call in the defining file is still a decision, but it is one
            # the caller is already looking at, so it is counted and not
            # enumerated. Dropping it silently would overstate how small the
            # change is.
            if path in def_files:
                same_file += 1
                return
            if _TEST_PATH.search(path):
                in_tests += 1
                return
            outside += 1
            if line_no is not None and len(sites) < max_sites:
                sites.append(f"{path}:{line_no}")

        if repo.get(name) is not None and root:
            for path, line_no, text in repo[name]:
                record(path, line_no, text)
        else:
            # No git repository, or the grep failed: fall back to the scope
            # that was chunked. Narrower, but never wrong about what it saw.
            for i in rest:
                m = meta[i]
                for off, line in enumerate(chunks[i].splitlines()):
                    if pat.search(line):
                        record(m["path"], _source_line(m, off), line)
        found[name] = (sorted(def_files), outside, sites, same_file, in_tests)

    # Order by how decidable the answer is, not by how many sites there are.
    # Sorting on the raw count puts the worst entries first: a generic name
    # used everywhere is a fact about the word rather than a change to make,
    # and would crowd out a more specific symbol with a small, actionable
    # list of call sites. So: symbols with an enumerable set first (most
    # sites first among those), then the ones with nothing outside, then
    # the floods.
    def rank(kv):
        outside = kv[1][1]
        if 0 < outside <= max_sites:
            return (0, -outside)
        return (1, 0) if outside == 0 else (2, outside)

    ranked = sorted(found.items(), key=rank)[:max_symbols]
    out = []
    for name, (def_files, outside, sites, same_file, in_tests) in ranked:
        where = ", ".join(def_files[:2]) + (" ..." if len(def_files) > 2 else "")
        extra = []
        if same_file:
            extra.append(f"+{same_file} in that file")
        if in_tests:
            extra.append(f"+{in_tests} in tests")
        also = f" ({', '.join(extra)})" if extra else ""
        if outside == 0:
            out.append(f"  {name}  (defined {where}) -- no calls outside "
                       f"that file{also}")
        elif outside <= max_sites:
            out.append(f"  {name}  (defined {where}) -- {outside} call site(s) "
                       f"outside it{also}, ALL of them: " + ", ".join(sites))
        else:
            out.append(f"  {name}  (defined {where}) -- {outside} call sites "
                       f"outside it{also}, too many to list; first "
                       f"{len(sites)}: " + ", ".join(sites))
    return out


def _retrieve_once(entry, chunks, query, k, mode):
    """One scored pass over an already-built scope. Returns (res, error)."""
    body = {"query": query, "k": int(k), "mode": mode}
    if entry["scope_key"]:
        body["scope_key"] = entry["scope_key"]
    else:
        body["chunks"] = chunks
    try:
        r = _post("/retrieve", body)
        if r.status_code == 409:
            body.pop("scope_key", None)
            body["chunks"] = chunks
            r = _post("/retrieve", body)
    except Exception as e:                                  # noqa: BLE001
        return None, f"service call failed ({URL or '<unset>'}): {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    res = r.json()
    entry["scope_key"] = res["scope_key"]
    return res, None


@mcp.tool()
def scopegrep_multi_retrieve(queries: list[str] | str,
                            include: list[str] | str | None = None,
                            exclude: list[str] | str | None = None,
                            k: int = 6,
                            per_query_k: int = 10,
                            budget_tokens: int = DEFAULT_BUDGET_TOKENS,
                            output: str = "chunks",
                            split: str = "window",
                            chunk_chars: int = CHUNK_CHARS,
                            root: str | None = None,
                            mode: str = "codegen") -> str:
    """Run SEVERAL phrasings of the same question over one scope in a single
    call, fuse the rankings, and return one cross-validated chunk set.

    NOT the default. Use scopegrep_retrieve first; reach for this only when the
    question has genuinely distinct sub-parts that need different evidence --
    a behaviour AND the default that governs it, say. Paraphrases of one
    question are correlated evidence, not independent confirmation.

    What fusion does and does not buy: it helps when the queries genuinely
    seek different things, and it can actively dilute the result -- doing
    worse than a single well-chosen query -- when they overlap too closely.

    Agreement counts are reported as COVERAGE -- how many of your queries
    ranked a chunk -- and nothing more. They are not a correctness signal: a
    query that fails shrinks the denominator, and near-synonyms over a small
    scope overlap almost completely. Judge the chunks by whether their code
    implements the behaviour you asked about. If it does not, say so and look
    elsewhere; do not treat a vote count as evidence that it does.

    queries: 2-5 different phrasings of the SAME question. Vary the vocabulary
        -- the symptom in the user's words, the mechanism in implementation
        words, the domain term. Do not send unrelated questions; fusion
        assumes they are after the same code.
    k: how many fused chunks to return (default 8). This is the number that
        lands in your context, so keep it small; cross-validation means you
        need fewer of them than a single query would.
    per_query_k: depth each individual query is scored to before fusion
        (default 12). Larger widens the candidate pool without enlarging the
        returned result.
    budget_tokens: size of the whole response, headers included, shared across
        all queries rather than multiplied by them. Items are included whole;
        anything dropped is named by location.
    output: "chunks" (default) returns the chunk text, which is what lets you
        stop. "files" returns ranked locations only.
    include/exclude/split/chunk_chars/mode: as scopegrep_retrieve.
    """
    qs = [queries] if isinstance(queries, str) else list(queries)
    qs = [q for q in (s.strip() for s in qs) if q]
    if not qs:
        return "no queries given."
    if len(qs) > 8:
        return f"too many queries ({len(qs)}); 2-5 phrasings is the useful range."

    inc = _norm_globs(include)
    exc = DEFAULT_EXCLUDES + _norm_globs(exclude)
    rt = os.path.abspath(root) if root else ROOT
    entry, _cached = get_scope(inc, exc, split, chunk_chars, rt)
    chunks, meta = entry["chunks"], entry["meta"]
    if not chunks:
        return (f"scope is empty: nothing under {rt} matched "
                f"include={inc or ['<everything>']}.")
    if len(chunks) > SAFE_CHUNKS:
        return (f"scope too wide: {len(chunks)} chunks. Narrow `include` to one "
                f"subsystem and call scopegrep_scope first to check the count.")

    t0 = time.time()
    fused, agree, best_rank, reranked_at = {}, {}, {}, {}
    per_query, errors = [], []
    scope_tokens = 0
    for q in qs:
        res, err = _retrieve_once(entry, chunks, q, per_query_k, mode)
        if err:
            errors.append(f'"{q[:60]}" -> {err}')
            continue
        scope_tokens = res["scope_tokens"]
        valid_to = res["ranking_valid_to_k"]
        per_query.append((q, len(res["top_k"])))
        for item in res["top_k"]:
            idx, rank = item["index"], item["rank"]
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
            agree[idx] = agree.get(idx, 0) + 1
            best_rank[idx] = min(best_rank.get(idx, 10**9), rank + 1)
            if item["reranked"]:
                reranked_at[idx] = True
    wall = time.time() - t0

    if not per_query:
        return "every query failed:\n" + "\n".join(errors)

    order = sorted(fused, key=lambda i: (-fused[i], best_rank[i]))[:int(k)]
    n_q = len(per_query)
    n_failed = len(errors)

    items = []
    for n, idx in enumerate(order, 1):
        m = meta[idx]
        loc = f"{m['path']}:{m['start_line']}-{m['end_line']}"
        if m.get("symbol"):
            loc += f"  (inside {m['symbol']})"
        mark = "  [preliminary rank]" if not reranked_at.get(idx) else ""
        head = (f"--- #{n}  {loc}  rev {m.get('revision','?')[:8]}  "
                f"(ranked by {agree[idx]} of {n_q} executed queries, "
                f"best rank {best_rank[idx]}){mark}")
        text = head if output != "chunks" else head + "\n" + chunks[idx] + "\n"
        items.append({"text": text, "loc": loc, "index": idx})

    kept, omitted, used = _pack_response(items, budget_tokens)

    def _render(kept_items, omitted_items, max_locs):
        lines = [
            f"scopegrep multi: {n_q} of {len(qs)} queries executed, fused "
            f"over {len(chunks)} chunks; "
            f"{len(kept_items)} evidence items, "
            f"~{_SIZE_SLOT} est. "
            f"tokens of a {budget_tokens:,} budget, {wall:.1f}s wall",
        ]
        if n_failed:
            # Coverage counts are per EXECUTED query. Letting a failure shrink
            # the denominator silently is what turns partial execution into
            # apparent consensus, so failures are named where counts are read.
            lines.append(f"{n_failed} query(ies) FAILED and are not in any "
                         f"coverage count below:")
            lines.extend("  " + e for e in errors)
        if omitted_items:
            shown = [item["loc"] for item in omitted_items[:max_locs]]
            lines.append(
                f"omitted {len(omitted_items)} lower-ranked item(s) to stay in "
                "budget"
                + ((": " + ", ".join(shown)
                    + ("..." if len(omitted_items) > max_locs else ""))
                   if shown else ""))
        lines.append(
            "Coverage counts say how many of your queries ranked a chunk. They "
            "are not a correctness signal -- paraphrases are correlated, so a "
            "chunk every query returned may still be the wrong chunk. Decide "
            "from the code below; if none of it implements the behaviour, say "
            "so.")
        refs = _resolve_dangling([item["index"] for item in kept_items],
                                 chunks, meta)
        if refs:
            lines.append(
                "REFERENCES RESOLVED ELSEWHERE IN SCOPE -- symbols these chunks "
                "use but do not define. A binding shown as AMBIGUOUS has more "
                "than one candidate and is not an answer:")
            lines.extend(refs)
        # The outward half of the same idea. `_resolve_dangling` answers what
        # this code depends on; this answers what depends on it. Both are
        # completeness questions the ranking cannot express, and both are
        # cheaper to answer here than to make the caller spend a turn on. They
        # differ in reach: a dangling reference must resolve inside the
        # declared scope to be trustworthy, while a caller can sit anywhere,
        # so this one greps the repository instead of the chunks.
        callers = _resolve_outbound([item["index"] for item in kept_items],
                                    chunks, meta, root=rt)
        if callers:
            lines.append(
                "WHAT ELSE CALLS WHAT YOU JUST READ -- every call site in "
                "scope for the symbols these chunks define, ranked by relevance "
                "or not. Change one of these symbols and each site listed is a "
                "decision; a symbol with none is nothing to decide:")
            lines.extend(callers)
        note = _coverage_note(entry.get("coverage") or {})
        if note:
            lines.append(note)
        if entry["note"]:
            lines.append("WARNING: " + entry["note"])
        return "\n".join(lines + [""] + [item["text"] for item in kept_items])

    text, kept, omitted, _max_locs = _fit_to_budget(
        _render, kept, budget_tokens, omitted)
    text, _total = _stamp_size(text)
    return text


def main():
    """Entry point for the `scopegrep-server` console script (pip install)."""
    mcp.run()


if __name__ == "__main__":
    main()
