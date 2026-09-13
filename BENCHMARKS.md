# Where scopegrep's numbers come from

Every figure the plugin quotes was produced by two GPU runs on an
A100-40GB against real corpora, not estimated and not carried over from a
different experiment. This file is the provenance so the guidance can be
checked rather than believed.

Source repository: `decoding/` (the research repo this plugin was extracted
from). Scoring code: `longrange_score.py`, arm `two_pass_g32`. Harness:
`longrange_bench.py`. The plugin's `two_pass_core.py` (in the separate `scopegrep-server` repo) is an
AST-verbatim copy of that code — regenerate and diff it with
`tools/extract_scorer.py`.

## The two corpora

Both are **one coherent haystack** with many questions against it, not one
small document per question. This matters: earlier runs in the source repo
scored each SWE instance against only its own 25-30 files, which is not a
long-context test at all.

**Corpus A — SWE-bench_Lite, long-context.**
20 SWE-bench_Lite instances. Each instance's real repository files (cloned at
the instance's `base_commit`) were concatenated into ONE shared context, so
every instance's files are distractors for all 19 other questions.

- 589 chunks, 198,165 tokens
- chunk = `# File: <relpath>\n` + the file's first 1500 characters
- mean chunk 1266 chars / 337 tokens
- query = the real GitHub issue body (up to 3000 chars)
- gold = the file the accepted patch edited; exactly 1 gold per question
- `prompt_mode="codegen"`
- results: `results/swe_longrange_perquestion_results.jsonl`, Modal run `b285jpcq0`, $0.237

**Corpus B — ToolBench, long-context.**
Same construction over 20 ToolBench queries and their real tool schemas.

- 500 chunks, 69,772 tokens, mean 140 tokens/chunk
- query = the real natural-language user request
- gold = the tools the qrels mark relevant; 2-5 per question
- `prompt_mode="short"`
- results: `results/toolbench_longrange_perquestion_results.jsonl`, Modal run `bbcjlditz`, $0.587

## Gold-chunk recall

**Corpus A (code, n=20, 1 gold each).** `dense_ceiling` could not run: at
198k tokens it needs more than 40GB before any scoring happens, which is the
whole reason a two-pass scorer exists.

| k | landmark_g16 | landmark_g32 | two_pass_g16 | **two_pass_g32** | grep (path-aware) | grep (bag-of-words) |
|---|---|---|---|---|---|---|
| 1 | 0.050 | 0.050 | 0.250 | 0.200 | **0.350** | 0.200 |
| 12 | 0.300 | 0.650 | 0.650 | **0.750** | 0.550 | 0.500 |
| 20 | 0.550 | 0.800 | 0.650 | **0.800** | 0.700 | 0.650 |
| 32 | 0.650 | 0.800 | 0.650 | **0.800** | 0.750 | 0.700 |

**Corpus B (tool schemas, n=20, 2-5 gold each).**

| k | landmark_g16 | landmark_g32 | two_pass_g16 | **two_pass_g32** | dense_ceiling | grep (bag-of-words) |
|---|---|---|---|---|---|---|
| k=n_gold | 0.423 / 0.500 | 0.654 / 0.778 | 0.654 / 0.667 | **0.731 / 0.778** | 0.731 / 0.778 | 0.550 |
| 12 | 0.592 | 0.890 | 0.712 | **0.933** | 0.892 | 0.850 |
| 20 | 0.625 | 0.933 | 0.712 | **0.950** | 0.950 | 0.850 |
| 32 | 0.667 | 0.950 | 0.712 | **0.950** | 0.950 | 0.850 |

(`k=n_gold` shows the k=2 mean over the 13 two-gold questions and the k=3
mean over the 6 three-gold questions.)

The grep columns are a from-scratch lexical baseline built for this
comparison, not a published number: bag-of-words term-overlap ranking over
the same chunks, plus, for corpus A, a tier that prioritises chunks matching
a full multi-segment relative path quoted in the issue text (the only kind of
path an agent would actually copy out of a traceback). 7 of the 20 issues
quote such a path; 3 of those 7 resolve to the gold file.

## Cost, latency, memory

| arm | wall | peak VRAM | stage-1 tokens | candidates |
|---|---|---|---|---|
| two_pass_g32, corpus A | 8.4s (6.2 stage 1 + 2.2 stage 2) | 23.7 GB | 24,360 | 26.5 |
| two_pass_g32, corpus B | 9.1s (6.5 + 2.6) | 23.0 GB | 21,176 | 63.9 |
| dense_ceiling, corpus B | 23.9s | 35.7 GB | 74,264 | — |

A100-40GB at $0.000583/s. Both runs together: **$0.824**, ~13 minutes GPU.

`candidates` is the number the plugin reports as `ranking_valid_to_k`: stage
2 re-scores only what fits its 8000-token budget, and that count is why
recall goes flat past k≈26 on 337-token code chunks and past k≈64 on
140-token schema chunks.

## What the numbers say

1. **k=20 is the knee, and past ~25 nothing is bought.** 0.800 at k=20 and
   0.800 at k=32 on code, for 4,100 extra tokens.
2. **g32, not g16.** The 16-token gist lost 0.10 (code, k=12) to 0.22
   (schemas, k=12).
3. **Stage 2 earns its 2.2s inside the candidate band.** On code at k=12,
   two_pass_g32 0.750 vs landmark_g32 0.650. Outside the band the two arms
   are the same ranking by construction.
4. **two_pass_g32 tracks the true ceiling where the ceiling can be
   computed.** Corpus B: 0.950 vs dense_ceiling's 0.950 at k=20, at 38% of
   the wall time and 64% of the memory.
5. **Grep wins the top of the ranking on code and loses the middle.**
   0.350 vs 0.200 at k=1; 0.550 vs 0.750 at k=12. Grep is the right first
   move; scopegrep is the right second one.
6. **Grep does not lose gracefully on semantic retrieval.** On schemas it
   plateaus at 0.850 from k=12 onward — more results do not help, because the
   query and the gold schema share no vocabulary. scopegrep reaches 0.950.

## Caveats that belong next to the numbers

- Corpus A stacks 20 unrelated repositories into one haystack. That is a
  harder distractor set than a single real repo in some ways (many
  same-named files) and an easier one in others (the right repo's files are
  lexically distinctive). It is not a simulation of a real agent session.
- The lexical baseline is a single static ranking. A real agent greps
  several times, reads results, and greps again; that iterative version is
  stronger than the baseline measured here, which biases the comparison
  toward scopegrep.
- Recall is measured against a single gold file per code question — the file
  the patch touched. A chunk that is genuinely useful but not the patched
  file scores as a miss.
- `split="window"` — the plugin's default chunker — is NOT this shape. The
  benchmark used one head-truncated chunk per file.

## Does the deployed service still produce these numbers?

It has to be checked, not assumed: the service vendors a copy of the scorer
and adds a warm-KV-cache stage 1 the benchmark never used. Either change
could move a score silently. `tools/replay_benchmark.py` replays a whole
corpus through the live endpoint and recomputes recall from the service's own
rankings.

Run 2026-09-04 against `https://gekalabya--scopegrep-scopegrep-web.modal.run`:

```
$ python3 tools/replay_benchmark.py --docs .../swe_longrange_perquestion_documents.jsonl \
      --mode codegen --ks 1,12,20,32 --expect 1=0.200,12=0.750,20=0.800,32=0.800
20 questions over 589 shared chunks, mode=codegen
indexed: 203,077 tokens, stage-1 prefix 23,947 tokens, 9.7s
mean ranking_valid_to_k = 26.5
mean per-query GPU time  = 2.52s (warm scope; first query paid the prefill)
recall:
  k=1   0.200   benchmark 0.200  MATCH
  k=12  0.750   benchmark 0.750  MATCH
  k=20  0.800   benchmark 0.800  MATCH
  k=32  0.800   benchmark 0.800  MATCH
service reproduces the benchmark exactly
```

ToolBench, same story: 0.933 / 0.950 / 0.950 at k=12/20/32, all MATCH, mean
`ranking_valid_to_k` 63.9, 2.21s per warm query.

`tools/smoke.py --selftest` covers the cache separately and more sharply:
warm probe vs cold `landmark_score`, `max|delta| = 0.000e+00` on both the
first and the second probe, rankings identical.

### What the warm cache buys

| | benchmark (cold every query) | service (warm scope) |
|---|---|---|
| SWE, per query | 8.4s | **2.52s** |
| ToolBench, per query | 9.1s | **2.21s** |
| stage-1 tokens pushed | 24,360 | ~20 (the question only) |

Recall is unchanged, to the last decimal, in both directions.

## A worked example, including where it falls short

Query, against this research repo: *"Where in this codebase is the KV cache
actually shortened after a scoring probe, and how is the recurrent state of
the linear-attention layers put back the way it was?"* The true answer is
`pipeline/core.py`, functions `_truncate_full_attn_cache` (line 529) and
`_restore_linear_state` (line 547).

- **Scope `["*.py"]` — 1166 chunks / 447k tokens, ~2x anything benchmarked.**
  `pipeline/core.py` did not appear in the top 12 at all. The scope was too
  wide, exactly as the "unmeasured above ~1200 chunks" caveat warns.
- **Scope `["pipeline/*.py"]` — 139 chunks / 50k tokens.** `pipeline/core.py`
  took ranks 4, 6, 8, 11 and 12 of 12. Right file, five times over — but the
  windows returned were lines 561-612 and 478-499, adjacent to the answer
  rather than on it.

That is what 0.800 file-level recall feels like in practice: narrowing the
scope moved the right file from absent to dominant, and the tool still
handed back a neighbourhood rather than a line. Scope narrowly; expect a
file, not a cursor position.
