# What actually happened when an agent used scopegrep

`BENCHMARKS.md` is the offline GPU numbers: recall at fixed k, measured once,
against a static haystack. This file is the other half — what happened when
real Claude Code agents (Haiku 4.5 and Sonnet) were turned loose with the
plugin installed, on real tasks, with token/time/turn counts pulled from the
actual session transcripts. Several of BENCHMARKS.md's numbers turned out not
to predict agentic behavior at all, for reasons documented below.

Every number here is from a specific session transcript
(`~/.claude/projects/.../*.jsonl`), a `claude -p --output-format json` run, or
a direct unit test against `src/scopegrep/server.py`. Nothing is estimated
unless labeled as a projection, and every projection states the constants it's
built from.

Test target throughout: a shallow clone of `django/django`, `django/template/`
subsystem (183 chunks / 56,860 tokens after the glob fix below), question:
*"Something is silently swallowing an exception during template rendering and
I just get an empty string back instead of an error. What is responsible for
that?"* — a genuine symptom question with no literal symbol given. Gold
answer: `django/template/base.py`, `Variable._resolve_lookup`, the
`silent_variable_failure` / `string_if_invalid` mechanism.

---

## 1. The tool was never being used at all (adoption)

First finding, before any of the bugs below: across three independent test
agents (2x Sonnet, 1x Haiku) on tasks explicitly matching the skill's own
stated trigger ("a description of behavior... which part of this codebase
handles X"), **zero calls to any scopegrep tool.** All three defaulted straight
to Bash/grep/Read and reached correct answers anyway.

Root causes found by inspecting the actual transcripts, not by guessing:

- **Task-tool subagents inherit a frozen plugin/hook snapshot** from whenever
  the parent session started. Toggling `claude plugin enable/disable`
  mid-session has *zero effect* on already-running or newly-spawned Task-tool
  subagents — confirmed by checking that `scopegrep` appeared identically (as
  an inert name) in transcripts from a run where the plugin was supposedly
  disabled. This invalidated an early "controlled experiment": both arms had
  identical tool availability and neither used scopegrep, so a reported
  "+35% tokens / -33% time" delta was measurement noise, not a scopegrep
  effect. Retracted.
- **A `UserPromptSubmit` hook was added** (`hooks/scopegrep-nudge.sh`) to
  detect symptom-shaped prompts and inject a routing reminder — but the first
  version's regex was broken. Piping the exact failing prompt into the script
  directly proved it: `sometimes.{1,40}sometimes` couldn't span the ~75 chars
  real writing puts between two "sometimes", and `find the (part|code) that`
  required the words adjacent, when real text was "find the part **of the
  codebase that**". Every alternative in the pattern missed a textbook
  qualifying prompt. Fixed; regression-tested to 12/12 on a matrix of
  literal-bearing (must stay silent) and symptom-shaped (must fire) prompts.
- **The only proven way to test a mid-session plugin/hook change is a fresh
  `claude -p` subprocess**, not a Task-tool `Agent` call. Task-tool subagents
  share the parent's stale config; `claude -p` cold-loads current disk state.
  This became the standard test harness for everything below.

---

## 2. The glob bug — the real story of this whole investigation

Every "scopegrep missed the answer" result before this fix was not a recall
problem. It was this:

```python
>>> import fnmatch
>>> fnmatch.fnmatch("django/template/base.py", "django/template/**/*.py")
False
>>> fnmatch.fnmatch("django/template/backends/dummy.py", "django/template/**/*.py")
True
```

`fnmatch` has no globstar semantics: `**/` requires *at least one*
intermediate directory. `subsystem/**/*.py` — the exact pattern the skill
recommends for scoping to a subsystem — silently drops every file directly
inside that subsystem, keeping only files in subdirectories. This is the
worst possible failure mode: it punishes *correct* usage (the more faithfully
you follow "scope to a subsystem," the more of that subsystem's core you
lose), and it fails silently — `scopegrep_scope` reports a plausible-looking
chunk count either way, so nothing looks wrong.

Measured effect, same `include`, before/after the fix:

| | chunks | tokens | `base.py` in scope? |
|---|---|---|---|
| before | 23 | 5,492 | **no** |
| after | 183 | 56,860 | yes, confirmed at the exact gold line range |

Every earlier retrieval had been ranking a corpus with the answer surgically
removed, while reporting a healthy chunk count. This explains prior confusing
results cleanly: one run confidently answered `dummy.py:51` (a real file, just
the *only* plausible-looking one left after `base.py` vanished); another
"missed, fell back to grep" — there was nothing to find.

**Fix:** `_expand_globstar()` in `src/scopegrep/server.py` — every `**/`-bearing
pattern is also tried with that segment collapsed or removed, matching bash
globstar / gitignore / ripgrep semantics. Regression-tested against 6 cases
including the exact failing pattern; all pass.

This is the single highest-leverage fix in the whole session. Nothing else
mattered while it was broken.

---

## 3. `output="chunks"` default — the docstring encoded the exact wrong model

Original docstring justification for defaulting to `output="chunks"`: *"the
evidence is bounded and already in context."* That sentence is the bug.

In an agentic loop a tool result is not paid for once. It lands in context
and is **re-sent on every later turn of the task.** Measured on one real run:
two `output="chunks"` retrievals totaling ~7.5k tokens of payload grew
cache-read by **283,000 tokens** — the payload re-transmitted across the ~13
turns that followed it, because the retrieval didn't end the search (the
model grepped and read afterward anyway).

Fix path (with a reversal, documented so the reasoning is checkable):

1. First attempt: default `output` to `"files"` (locations only, no chunk
   text). Wrong call — instructing the model to pass `output="files"` on a
   *different* tool call worked 1 of 3 times when tried as a prompt-only
   change (models don't reliably follow that kind of instruction), and
   locations-only meant the model had to `Read` the file anyway: measured
   1.3k retrieved followed by **11.7k re-reading files across 6 extra turns**
   — worse than doing nothing.
2. Correct diagnosis: the win isn't a smaller payload, it's **a payload that
   ends the search.** Reverted `output` default back to `"chunks"` (bounded
   ~1500-char windows, never whole files — verified separately, see §7) and
   added an explicit stop rule instead (§5).

---

## 4. Opaque `HTTP 500` on an over-wide scope

`build_chunks()` truncates locally to `MAX_CHUNKS=2000` and sends the result
to the service anyway. A prompt that scoped `include=["**/*.py"]` (the whole
Django checkout, ~2,800 files) got back a bare `HTTP 500: Internal Server
Error`, and the calling model — with no way to learn *why* — silently
abandoned the tool entirely for the rest of that task. A tool that fails
unhelpfully gets dropped, not diagnosed.

**Fix:** a pre-flight `SAFE_CHUNKS = 1200` guard (the largest chunk count
BENCHMARKS.md actually measured) that returns actionable text instead of
calling the service:

```
scope too wide: 2000 chunks from include=['**/*.py'] under ...
Only scopes up to ~1200 chunks are benchmarked, and the service rejects more than 2000.
Narrow `include` to one subsystem ... and call scopegrep_scope first to see
the chunk count before you retrieve.
```

Verified directly against the server function — confirms the message fires
at exactly the over-cap boundary.

---

## 5. Chunks can't identify themselves — the actual quality bug

Window splitting (`split="window"`, the default) cuts a large file into
~1500-char windows without regard for function boundaries. This routinely
returns a chunk that opens mid-body:

```
--- #1  django/template/base.py:1017-1043  (217 tok, score 0.0003)
                            current = current()
                        except TypeError:
```

— the gold chunk, containing the real mechanism, fourteen lines below its own
`def`. A rival, wrong chunk in the same result set opened cleanly:

```
--- #8  django/template/base.py:1126-1173  (369 tok, score 0.0001)
    def render(self, context):
```

Given a set of candidates and told to pick the one whose code implements the
behavior, a Haiku agent picked the chunk it could actually *identify* — #8,
wrong — over #1, correct, headless. This reproduced across multiple runs and
is not a ranking problem: the path and line range were both present in the
header; what was missing was *which definition this snippet is part of*.

**Fix:** `_enclosing_symbol()` scans backward from a window's start for the
nearest definition at a shallower indent, then for an enclosing class above
that, and stamps it into the chunk header:

```
# File: django/template/base.py (lines 1017-1043) -- inside Variable._resolve_lookup
```

Verified end to end: the same question, same repo, same rank-#1 chunk,
correctly labeled — a Haiku run then answered correctly from it without any
`Read` calls (§6). This is the single change that most affected answer
*quality*; the token-reduction changes affected answer *cost*.

Side effect flagged, not yet re-measured: this changes chunk text, and
stage-1 sees the symbol name inside its 32-token gist. BENCHMARKS.md's
recall figures were measured on the old (unlabeled) chunk shape and should be
re-run before being quoted as still current.

---

## 6. Bulk cross-validated retrieval + a stop rule that initially over-trusted rank

Added `scopegrep_multi_retrieve`: takes 2-5 phrasings of the same question,
scores each against a scope built **once**, fuses rankings with reciprocal
rank fusion (`RRF_K=60`), and returns one deduplicated result. Rationale:
single-query recall is 0.800 at k=20 (BENCHMARKS.md) — independent
phrasings that disagree elsewhere tend to agree on the right chunk, and the
scope-prefill means extra phrasings cost ~3s each against a warm KV cache
rather than a second full index build.

First version tagged a chunk `CONFIRMED` when *every* phrasing ranked it and
stage-2 re-ranked it. Measured failure: on 3 near-synonym phrasings over a
183-chunk scope, **7 of 12** returned chunks got tagged confirmed — including
a module docstring at rank #1 — because near-synonyms overlap almost
completely on a small scope, and everything returned was already inside
`ranking_valid_to_k`. Agreement wasn't discriminating anything; it was
universal.

Tightened to require mean rank ≤3 across queries (`SHORTLIST`) — better
(7→2 candidates) but the shortlist **excluded the gold chunk** in favor of a
docstring and an exceptions stub, on a score difference of 0.003
(0.0487 vs 0.0484 vs 0.0472 — the scorer has real recall on this corpus but
almost no rank separation at the top).

**Final framing, after two corrections:** stopped asserting any subset as
"confirmed." The tool now returns the full candidate set with agreement/rank
metadata visible, and says explicitly: *trust the set, not the order* — read
the returned chunk text, pick whichever one's code actually implements the
behavior, and do not re-open the source files. This is deliberately weaker
than the original claim, because the original claim was measured to be false.

Measured, 3 phrasings, `django/template/**/*.py`:

```
fused top 6, ~1,469-1,684 tokens returned (2.6-2.9% of scope), gold chunk
consistently in the top 2, agreement 3/3
```

---

## 7. Are these ever whole files? No — verified directly

```
chunks: 75  max: 1649 chars  mean: 1368  CHUNK_CHARS=1500
chunks over 2x CHUNK_CHARS: 0
src/scopegrep/server.py: 35325 chars -> 26 chunks   (never sent as one chunk)
chunks flagged whole_file: 0   (in this scope)
```

`split="window"` bounds every chunk near the 1500-char target regardless of
source file size; a file only becomes a single "whole_file" chunk if it's
already smaller than one window. The leak in earlier runs was never file
size — it was the `Read` tool re-opening files the model had already been
handed chunks from (§3), which the stop rule in §3/§6 targets directly.

---

## 8. Cross-tier result: the plugin's value is inversely proportional to how good the model already is at grep

Same question, same repo, warm service, both models tested with all fixes
through §7 live.

| | grep-only (model's own choice) | forced onto scopegrep |
|---|---|---|
| **Haiku 4.5** | 40.5s, 12 turns, 316,813 tok, partly wrong | 43.0s, 4 turns, **106,346 tok, correct** |
| **Sonnet** | **16.0s, 3 turns, 103,851 tok, correct** | 39.6s, 4 turns, 147,011 tok, **wrong** |

Offered the tool and left to judge (hook fired, PROTOCOL text present in
context), Sonnet **declined it** and grepped instead — one `grep -n
"silent_variable_failure\|string_if_invalid\|except Exception" base.py`, one
targeted `Read`, correct answer, fewer tokens than either scopegrep run. That
was the right call, not evasion: see §10 for why.

Forcing scopegrep on Sonnet was strictly worse on every axis. Root cause of
the wrong answer traced exactly (§9): the gold chunk contained the right
*code* but not the deciding *fact* (`string_if_invalid` is a bare identifier;
its value `""` is defined in a different file), and the stop rule from §3/§6
forbade the one lookup that would have supplied it. A rival chunk contained a
literal `return ""` and won on the evidence as given.

**Standing recommendation:** the hook stays advisory. A capable model
declining the tool is a feature of this design, not a failure of it.

---

## 9. Dangling references — the stop rule needed one carve-out

Full trace of the Sonnet failure in §8, from the actual retrieved text:

```python
# File: django/template/base.py (lines 1017-1043) -- inside Variable._resolve_lookup
        except Exception as e:
            ...
            if getattr(e, "silent_variable_failure", False):
                current = context.template.engine.string_if_invalid
            else:
                raise
```

This *is* the answer, contingent on knowing `string_if_invalid` defaults to
`""` — set in `django/template/engine.py`, not shown here. Rival chunk #4,
`inside TextNode`, contained `except UnicodeDecodeError: return ""` — a
worse mechanism, but a *complete* one as far as the returned evidence went.
Told to answer from the chunks and not read further, Sonnet reasoned
correctly from an evidence set I had made artificially incomplete, and named
chunk #4.

**Fix, in two parts:**

- Split the rule: *re-reading a file you were handed* stays forbidden (that's
  the waste in §3); *resolving a symbol whose value was never shown* is
  necessary and now explicitly carved out in the hook and skill text.
- Rather than rely on an instruction (three separate results this session
  showed models don't reliably follow secondary instructions — see §8's
  "declined the tool," and the `output="files"` compliance failure in §3),
  `scopegrep_multi_retrieve` now resolves it automatically:
  `_resolve_dangling()` scans returned chunks for referenced symbols they
  never assign a value to, and greps the **rest of the already-declared
  scope** (no GPU call, no extra turn) for a literal assignment. Verified
  output on this exact question:

  ```
  RESOLVED REFERENCES -- symbols the chunks below use but do not define:
    string_if_invalid = ""   (django/template/engine.py:28)
  ```

Re-ran the forced-Sonnet case with the fix live: correct primary answer
(`_resolve_lookup`, 1031-1042), correct secondary answer (`TextNode`'s
`UnicodeDecodeError` path), and the response explicitly cited `(default
"")` — the fact the resolver supplied. Quality fixed.

Cost of the fix, same question, forced-Sonnet, before vs after:

| | time | turns | tokens | answer |
|---|---|---|---|---|
| before | 39.6s | 4 | 147,011 | wrong |
| after | 53.9s | 5 | 193,479 | correct, most complete of any run this session |

Correctness recovered at +31% time / +32% tokens. Still 3.4x the time and
1.9x the tokens of Sonnet's own grep-only run for the same correctness —
confirms §8 rather than undoing it: the fix makes forced scopegrep *right*,
not *cheap*, on a model that didn't need it.

---

## 10. Waste decomposition — most of the payload is unused, and it barely matters

Measured retrieval precision (chunks actually cited in the final answer,
by path:line overlap, vs chunks returned):

| run | chunks used | retrieved (tok) | used (tok) | wasted |
|---|---|---|---|---|
| Haiku, `output="files"` | 2/12 | 236 | 35 | 85% |
| Haiku, `output="chunks"` k=12 | 2/12 | 3,461 | 816 | 76% |
| Haiku, multi k=8 | 1/8 | 2,258 | 400 | 82% |
| Sonnet forced, pre-fix | 2/6 | 1,861 | 847 | 54% |
| Sonnet forced, post-fix | 2/12 | 3,761 | 847 | 77% |

54-85% of every retrieval payload is never cited. But normalizing each run's
total cost by its turn count shows this doesn't actually drive the token
bill:

| | turns | total tokens | tokens/turn |
|---|---|---|---|
| Haiku control (grep) | 12 | 316,813 | 26,401 |
| Haiku, scopegrep (v8/v9) | 4 | ~106,000 | ~26,500 |
| Sonnet control (grep) | 3 | 103,851 | 34,617 |
| Sonnet forced, post-fix | 5 | 193,479 | 38,696 |

Per-turn cost is constant within a model to within ~1%. **Total cost ≈
turns x fixed-per-turn-overhead**, and the wasted chunk payload — at most a
few thousand tokens — is a fraction of a single turn. The chunks the model
never cites cost far less than the turns spent obtaining and reading them.

Direct measurement of that fixed per-turn floor, one-turn task, output
capped at 4 tokens:

| config | total tokens for 4 output tokens |
|---|---|
| `--bare` (no hooks, no plugins) | 0 |
| Haiku, plugins on | 22,401 |
| Sonnet, plugin off | 29,832 |
| Sonnet, plugin on | 32,022 |

~87-100% of every turn is fixed overhead (system prompt, tool schemas, skill
listings) independent of task content. This is the dominant cost term, not
retrieval precision.

---

## 11. "Preamble" — two setup turns that produce zero evidence

Real tool sequence, forced-Sonnet run: `ToolSearch` (269 chars, loads
schemas) -> `scopegrep_scope` (749 chars, chunk counts) -> `scopegrep_retrieve`
(16,179 chars, the actual evidence) -> answer. The first two calls return
**nothing about the question** and cost a full re-sent turn each: ~26k
(Haiku) / ~35k (Sonnet).

- `ToolSearch` exists because the scopegrep MCP tools are harness-deferred
  (name-only until fetched). Not yet confirmed whether this is
  plugin-controllable or a harness-level default; flagged as untested rather
  than asserted.
- `scopegrep_scope` as a *mandatory* first call is self-inflicted: added
  after the §4 `HTTP 500` incident, but `scopegrep_retrieve` already calls
  the same `get_scope()` internally and already carries the `SAFE_CHUNKS`
  guard. The separate call re-derives information retrieve computes anyway,
  to guard against a failure retrieve already guards against. Identified,
  not yet removed — see Open Items.

Grep, by contrast, has no preamble: the search *is* the first call.

---

## 12. Why grep keeps winning against a capable model

Sonnet's winning run against the same question, verbatim from the
transcript:

```
Bash: grep -n "silent_variable_failure\|string_if_invalid\|except Exception" django/template/base.py | head -50
Read: django/template/base.py offset=990 limit=55
```

Two calls. The model hypothesized the *actual identifier names*
(`silent_variable_failure`, `string_if_invalid`) from prior knowledge of
Django before ever looking at the repo, and grepped for them directly.

grep itself is mechanical — pattern compiled to a DFA with fast paths
(memchr/SIMD) for literal substrings, ripgrep parallelizes across
memory-mapped files respecting `.gitignore`, throughput is GB/s, a
Django-sized repo scans in well under a second. None of that is where the win
comes from, though. **The win is that a capable model can turn a semantic
question into a lexical one by guessing vocabulary correctly.** That
guessing ability, not the search primitive, is what scopegrep is actually
competing with — and it's a moving target that gets stronger with the
model, while scopegrep's fixed 2-3-turn preamble does not.

Where guessing genuinely fails and the gap for scopegrep is real: the code
contains none of the question's words and they're unguessable (idiosyncratic
naming, unfamiliar domain), a plausible grep returns hundreds of hits with no
way to rank them, or the concept is spread across files with no shared
vocabulary. Narrower than "semantic beats lexical," but real.

---

## 13. What a 3x token reduction requires, per model

Modeled from the constants measured in §10-11
(tokens_per_turn x turns + payload x persistence + P(miss) x fallback_cost),
cross-checked against real runs.

| | grep-only baseline | 3x target | turns that fits in | status |
|---|---|---|---|---|
| Haiku | 316,813 | 105,604 | 4.0 turns @ 26.4k/turn | **met** (v9: 106,346) |
| Sonnet | 103,851 | 34,617 | 1.15 turns @ 30k/turn | **impossible** — minimum is 2 turns (call + answer) |

Sonnet's 3x is not reachable by improving retrieval at all — 2 turns alone
floors at ~62k (1.63x). Projected value of each remaining lever, in
descending order:

1. **Fixed per-turn floor, 30k -> ~17k (Sonnet).** Only path to 3x on this
   model. Recoverable pieces identified: scopegrep's own docstrings (~2,190
   tok/turn — see §14), unused-plugin overhead from ARS/caveman SessionStart
   injections and ~50 unrelated MCP connector names present every turn.
   Estimated recoverable 4-7k; remainder is Claude Code's own system prompt,
   outside this plugin's control. Realistic floor ~24k -> 2 turns ~= 50k =
   **2.1x**, not 3x.
2. **Preamble 3 turns -> 1** (§11). Worth ~52.8k (Haiku) / ~69k (Sonnet).
   Certain, cheap, zero quality cost. Highest-value remaining change.
3. **Recall 0.80 -> 0.95 at k=5.** Worth ~31.7k (Haiku) / ~10.5k (Sonnet) —
   value is almost entirely in avoiding the grep-fallback branch on a miss,
   not in the smaller payload.
4. **k=12 -> k=5 payload alone.** Worth 9.3k tokens ~= 0.3 turns. Noise by
   comparison to 1-3.

Honest read: Haiku already clears 3x at 4 turns. Sonnet's 3x is a
harness-configuration target (per-turn floor), not a retrieval-quality one —
better ranking alone cannot get there, confirmed by the model in row 3
showing Sonnet still 1.4x *worse* than grep-only even at projected 0.95
recall, until the preamble and floor are also fixed.

---

## 14. scopegrep's own documentation now costs more than its results

Measured directly against the source: four tool docstrings plus the
`SKILL.md` description-in-listing plus the hook's injected PROTOCOL text,
summed per turn they're billed on:

| component | tokens | billed |
|---|---|---|
| `scopegrep_retrieve` docstring | 671 | every turn |
| `scopegrep_multi_retrieve` docstring | 549 | every turn |
| `scopegrep_scope` docstring | 345 | every turn |
| `scopegrep_status` docstring | 118 | every turn |
| `SKILL.md` description (skill listing) | 153 | every turn |
| hook PROTOCOL text | 900 | per prompt, persists across turns |

Over a 4-turn task: metadata overhead ~= 11,460 tok vs retrieval payload
(§10) ~= 7,522 tok. The tool's documentation currently outweighs its output
by ~1.5x — and every measured finding written into these docstrings and the
hook this session made this worse, since it's billed on every subsequent
turn forever. Flagged, not yet trimmed.

---

## Open items (identified, not yet done)

1. **Remove the `scopegrep_scope`-before-`scopegrep_retrieve` mandate** from
   the hook (§11, §13-#2) — `retrieve` already guards the failure `scope`
   was added to prevent. Highest remaining value-per-effort change in this
   file.
2. **Test whether the scopegrep MCP tools can be made non-deferred**, to
   remove the `ToolSearch` preamble turn (§11). Unconfirmed whether this is
   plugin-controllable.
3. **Trim the four tool docstrings and the hook PROTOCOL text** (§14) —
   currently costs more than the payload they describe.
4. **Re-run the Haiku pair (v8/v9-equivalent) with the §9 dangling-reference
   resolver live** — v9's correct answer partly matched on only half the
   question ("silently swallowing," not "empty string"); the resolver
   should make that robust rather than lucky, and this has not been
   verified.
5. **Re-run BENCHMARKS.md's recall figures on the new chunk shape** (§5's
   symbol-header stamping changes what stage-1 sees) before quoting 0.800 /
   0.950 as still current.
6. **codex-side hook is installed and pipe-test-verified but not verified
   live** — blocked by an unrelated `codex exec` bug (`unknown variant
   "max"` in its models-manager, codex-cli 0.114.0), not by anything in this
   plugin.
7. All cross-tier and cross-fix comparisons in this file are **n=1 per
   cell**. Haiku run-to-run variance was observed directly in this session
   (4-turn and 20-turn runs on identical inputs) — treat every delta here as
   a direction, not a precise ratio, until repeated.
