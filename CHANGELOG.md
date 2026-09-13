# Changelog

Behaviour changes to the scopegrep plugin. Dates are Asia/Kolkata (IST).

Every "before"/"after" figure below was produced by executing both versions of
the code, not by reading the diff. The measurements and the frozen pre-fix
copies live in the research repo at
`decoding/experiments/product_fixes_20260906/`.

## 0.3.0 — 2026-09-13

Renamed `gistgrep` -> `scopegrep` (package, plugin, tool names, env vars,
Modal app/secret/volume names, token file paths) and split into two repos:
this one (plugin + `pip`-installable client) and the private `scopegrep-server`
(the Modal scoring service). Restructured the client as a proper `src/`
package with a `pyproject.toml`, so `pip install -e .` works alongside the
existing plugin-directory install -- both run the identical
`src/scopegrep/server.py`.

Ported `_resolve_outbound`, the "what else calls what you just read"
completeness block, from a research fork -- see the README's "What else calls
what you just read" section for the measured numbers and honest limits.
Along with the port: `_is_source` changed from a 16-extension allow-list to a
deny-list (an unrecognised language used to come back "no calls outside that
file", indistinguishable from a symbol that genuinely has none), and
definition extraction widened from Python/Cython keywords only to the
chunker's own nine-language vocabulary. `tests/test_outbound.py` is the first
test suite this project has had.

Added `SCOPEGREP_MIN_CONTAINERS` (server-side) and `scopegrep-prewarm` /
`tools/prewarm.sh` (client-side) to address cold starts, measured at
71-150s: `/health` runs inside the GPU class, so a deployment held warm and
one that silently scaled to zero are otherwise indistinguishable from the
outside until someone pays the load. `scopegrep_status` now reports which is
in force.

Redeployed on a fresh Modal profile with unused credit; the pilot's shared
`SCOPEGREP_TOKEN` is a single hard-coded secret, not per-tester -- see
`scopegrep-server`'s README for why that's a deliberate scope cut, not an
oversight.

## 0.2.2 — 2026-09-06

All three found by measuring 0.2.1 on live retrievals, not by review.

### Fixed

- **Both retrieve tools raised `TypeError` on every call.** 0.2.1's
  `_fit_to_budget` was given a `max_locs` argument and a fourth return value
  while both `_render` closures kept the old two-argument signature. The MCP
  layer surfaces only "Error executing tool scopegrep_retrieve" with an empty
  stderr, so it read as a service outage rather than a code fault: a headless
  benchmark on apache/airflow spent 67 turns and 3.85M input tokens fighting
  the tool against a control that took 27 turns and 1.32M, and that run was
  discarded. Probe `both_retrieve_paths_execute` now drives BOTH tools end to
  end against a stubbed service — every earlier probe exercised helpers only,
  which is exactly why this class of break got through.
- **Responses under-stated their own cost.** 0.2.1 recomputed the header's
  "est. tokens" from the evidence bodies alone, leaving out the header itself,
  the omitted-items list, the resolved references and the coverage note —
  under-reporting every response by 106-140 tokens (mean 127, 13% of a 1k
  budget), always in the same direction. 0.2.0's number had tracked reality to
  +14. The size is now stamped into a fixed-width slot after rendering, so the
  measurement is taken on the final string and the printed number is exact
  (measured error 0 at 600/1,200/4,000-token budgets).
- **Trimming spent evidence to save metadata.** The fit loop dropped whole
  source chunks first: on a measured run it discarded a 375-token chunk to
  shed a 2-token overrun, taking utilisation from 100.0% to 90.9%. It now
  shortens the omitted-items list (6 -> 3 -> 1 -> 0 named locations) before
  touching evidence. The omission is still reported either way.
- **The hook missed "where is X configured".** That is a paraphrase of
  `scopegrep_retrieve`'s own docstring example and the hook stayed silent on it,
  as it did on "how does X know". Trigger recall on the behaviour-shaped set:
  **3 of 5 -> 5 of 5**, with false fires still 0 and the literal guard still
  7 of 7 correctly silent.

## 0.2.1 — 2026-09-06

Found by the first real-repo smoke run of 0.2.0, not by review.

### Fixed

- **The response budget was approximate, not exact.** `_pack_response` charges
  a fixed 120-token reserve for the header, but the omitted-items list, the
  resolved-references block and the coverage note are appended *after*
  packing, and their combined size is data-dependent. On a real pandas
  retrieval the header under-counted the rendered response by 131 tokens
  (3.4%) -- inside the requested budget that time only because the extras
  happened to be small. Both retrieve tools now render, then drop trailing
  items until the string genuinely fits. A single oversized item is still
  returned whole and reported.

## 0.2.0 — 2026-09-06

Acts on `decoding/docs/SCOPEGREP_PRODUCT_REVIEW.md`. Sixteen before/after
defect probes went from 0 requirements met to 16.

### Breaking

- **`scopegrep_retrieve` is governed by `budget_tokens`, not `k`.** `k` is now
  an optional hard cap on item count; leaving it unset lets the budget decide.
  The budget covers the whole serialized response, headers included. Evidence
  items are included whole or not at all, and anything dropped is named by
  location so the caller can ask for it. A single oversized direct hit is
  still returned whole and reported as over budget — half a definition is not
  evidence.
- **`build_chunks` returns four values** (`chunks, meta, note, coverage`).
  `tools/smoke.py` updated; any other caller must be too.
- **`_walk` returns `(files, coverage)`.**
- **Chunk cache entries built by 0.1.x are not reused.** Cache identity now
  includes `CHUNKER_VERSION`, so the first query after upgrading rebuilds.

### Fixed

- **Scope leaked files the caller never asked for.** The walk returned `.env`,
  a `.gitignore`-ignored file, and a symlink resolving outside the declared
  root. It now honours `.gitignore`/`.ignore`, refuses credential-shaped files
  whatever the include patterns say, resolves every path with `realpath`
  against the root, and reports what it excluded and why.
- **Globs both over- and under-matched.** `**/` expansion stopped after the
  first occurrence, so `src/**/sub/**/*.py` matched nothing; `fnmatch`'s `*`
  crossed `/`, so `src/*.py` matched `src/pkg/deep.py`. Patterns are now
  translated once to a regex with real globstar semantics. `**/` still means
  zero or more directories.
- **Stale evidence survived an edit.** Cache identity was `(size, mtime)`, so
  a same-length edit with the mtime restored — what `git stash`,
  `git checkout` and an equal-width `sed -i` all produce — returned the
  pre-edit body as current evidence. Identity is now a SHA-256 of file
  content plus every setting that shapes a chunk.
- **Citations were off by one.** A definition on line 1 was reported as line 2,
  because the synthetic `# File:` header occupied the offset the arithmetic
  treated as source. Chunks carry `header_lines` and every chunk-offset to
  source-line conversion goes through `_source_line`.
- **`object.append(value)` produced a reference to `appen`.** The `{4,}` in the
  reference regex backtracked past the final `d`, so the "not followed by `(`"
  guard passed on a truncated identifier. A `\b` pins the match to the whole
  name.
- **A guessed default was presented as authoritative.** A symbol with several
  distinct bindings in scope now reports `AMBIGUOUS` with its candidate sites
  instead of resolving to the first one found.
- **The dangling-reference resolver ran only on `scopegrep_multi_retrieve`.**
  It is the fix that turned the measured forced-Sonnet run from wrong to
  correct, and making single-call retrieval the recommended path without it
  would have reintroduced that failure. It now runs on both paths.

### Changed

- **Guidance is advisory, not mandatory.** The skill's frontmatter no longer
  calls retrieval MANDATORY before grep on an unread file, and the hook no
  longer says to ALWAYS call `scopegrep_scope` first or to prefer
  `scopegrep_multi_retrieve` by default. Scope is resolved and cached inside
  `scopegrep_retrieve`, which already carried the `SAFE_CHUNKS` guard the
  separate call was added to provide. Required round trips before an answer:
  **3 → 2**.
- **Agreement counts are reported as coverage, not confirmation.** "A chunk
  with full agreement that was re-ranked by stage 2 is confirmed. Answer from
  it" is gone. Counts now state outright that they are not a correctness
  signal, that a failed query shrinks the denominator, and that paraphrases
  are correlated evidence. Failed queries are named where the counts are read.
- **`scopegrep_multi_retrieve` is no longer the default.** It is for questions
  with genuinely distinct sub-parts. Measured: on two documents naive
  packaged-query fusion fell from 1.0 coverage to 0.233, and a union merely
  recovered the single-query baseline.
- **k guidance replaced by scope-aware budget guidance.** The old advice
  answered every scope with "k=20", including a six-chunk scope where k=20 is
  the whole corpus. `scopegrep_scope` now suggests a token budget scaled to the
  scope, citing recall measured at matched budgets: 0.550 at 1k, 0.750 at 2k,
  0.800 at 4k, 0.850 at 12k on the 20-query SWE set.
- **The hook no longer warms the GPU on the prompt path.** Warming is a cost
  decision belonging to the call that needs it, not to every symptom-shaped
  prompt that may never retrieve.
- **Responses report scope coverage** — what was excluded by ignore rules, as
  credential-shaped, or as resolving outside the root — so an incomplete scope
  is visible rather than inferred from a small chunk count.
- Per-turn instruction surface (skill description in the listing, hook
  injected context, four tool descriptions): **2,466 → 1,872 tokens**, ~24%
  smaller. The tool docstrings themselves are essentially unchanged (+6); the
  saving is in the skill description and the hook.

### Known limits

- The ignore matcher is deliberately partial: no mid-pattern `**`, and no
  re-inclusion of a file under an excluded directory. Git has the same
  restriction, but a repository relying on those rules will see a scope
  difference.
- Client-side token counts are estimates. There is no tokenizer in the MCP
  process; 3.75 chars/token is the measured constant, labelled as an estimate
  everywhere it is surfaced.
- `ToolSearch` remains a required first call because the MCP tools are
  harness-deferred. Whether that is plugin-controllable is still untested.
- Hybrid ranking is **not** adopted. Its extra recall at equal k came with
  ~32% more returned tokens, and at matched budgets on 20 queries no budget
  produced an advantage whose 95% CI excluded zero.
- Cold start, tenant isolation, memory-bounded cache admission, and readiness
  without a model load are service-side and untouched by this release.
