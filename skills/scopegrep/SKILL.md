---
name: scopegrep
description: Semantic repository retrieval for behaviour-to-code gaps - a symptom, an issue body, "which part decides X", where the question shares no vocabulary with the code. Not for literals you already hold (symbol, path, error string - grep those) and not for a file you have already identified (read it). Tools may be deferred; load with ToolSearch if scopegrep_retrieve is not live.
---

# Scopegrep: semantic repository retrieval

Scopegrep ranks repository chunks against a natural-language question using the
attention a 9B model pays to each chunk while reading the question. It finds
code that does not contain your words. It costs a GPU call and a few seconds.

## When it is the right tool

| you have | use | why |
|---|---|---|
| a symbol, error string, file path, or literal | **grep** | exact, free, instant. Measured on the same corpus: a path-aware grep found the right file at rank 1 **35%** of the time; scopegrep **20%**. |
| a traceback quoting a path | **grep that path** | the path is the answer; do not pay a GPU to rediscover it |
| a behaviour or symptom with no literal | **`scopegrep_retrieve`** | grep has no query to run. On tool-schema retrieval keyword ranking plateaus at 0.850 recall; scopegrep reaches 0.950. |
| grep returned 400 hits | **`scopegrep_retrieve`** with a narrow `include` | ranking is the problem, not matching |
| grep returned nothing and you are out of guesses | **`scopegrep_retrieve`** | its best case |
| you already know the file | **read it** | scoping to one known file is a `Read` in a GPU costume |

This is advisory routing, not a gate. Nothing here requires a retrieval before
a grep or a read, and a call that was not going to change what you do next is
pure cost.

### The two measured failure modes

- **Forcing it where grep already wins.** A run that scoped to
  `["django/db/models/query.py"]` — a file it had already named — and retrieved
  inside it spent a 73s cold start, a 28,988-character result and two extra
  round trips to locate a range one `Read` produced in 1,127 characters.
- **Retrieval that does not end the search.** Two `output="chunks"` retrievals
  returned ~7.5k tokens of payload, but cache-read grew by **283k tokens**,
  because a tool result is re-sent on every later turn. What you pay is
  *payload x turns*. A cheap result that fails to end the search is the
  expensive one — a locations-only run returned 1.3k characters and then spent
  11.7k re-reading the files it had just ranked.

Both are the same error: retrieving when you already know where to look, or
retrieving and then searching anyway.

## Calling it

One call is meant to be enough. Scope is resolved and cached inside the
plugin, so `scopegrep_scope` is optional — use it when you want to see a chunk
count before spending, not as a required first step.

```
scopegrep_retrieve(
    query="<the whole issue body, traceback, or failing test — not keywords>",
    include=["django/template/**/*.py"],   # a subsystem, not the repo
    budget_tokens=3000,
)
```

**Write the query as prose.** The recall figures below were measured with full
GitHub issue bodies — paragraphs, stack traces, reproduction steps — as the
query. It is scored at full token resolution against every candidate, so
detail is signal. `retry logic` throws that away.

**Scope to a subsystem.** A 200-file scope ranks better and costs less than a
2,000-file one; the service refuses above ~1,200 chunks.

## Budget, not k

The response is governed by `budget_tokens` — the size of the whole reply,
headers included. Evidence items are included whole or not at all, and
anything dropped is named by location so you can ask for it.

Measured by re-scoring the stored 2026-09-05 rankings at matched serialized
budgets (20 SWE-bench issues, gold = the file the accepted patch edited):

| budget | mean gold recall | note |
|---|---|---|
| 500 | 0.350 | too tight; roughly half the recall of 4k |
| 1,000 | 0.550 | |
| 2,000 | 0.750 | |
| **3,000** | **0.800** | default |
| 4,000 | 0.800 | |
| 12,000 | 0.850 | +8k tokens buys +0.05 recall |

The knee is between 2k and 4k. Below 1k you are paying for a GPU call and
throwing away most of what it ranked. Scale down for a small scope: a run
against a 90-chunk / 29k-token scope returned 6,214 tokens — 21.3% of the
whole corpus — for a question one `Read` answered in 1,127 characters.

`ranking_valid_to_k` in the response is the depth stage 2 re-scored at full
resolution; items past it are stage-1 gist order and are weaker evidence.

## Reading a result

Each item carries `path:start-end`, its content revision, and whether stage 2
re-ranked it. Scores rank *within* one result set; they are not stable
confidence values across queries or scopes, and the returned percentage is
context consumed, not accuracy.

A returned chunk is a window, not a file. Read the reported line range when
the window cut off the part you need — that is following the evidence, not
re-reading it.

**Resolve dangling references.** A chunk can hold the right code and still not
hold the deciding fact. Measured: asked why a template renders an empty
string, retrieval returned the correct chunk at rank 1, ending in
`current = context.template.engine.string_if_invalid`. That is the answer only
if you know `string_if_invalid` defaults to `""` — which lives in another
file. A rival chunk literally contained `return ""`, and a model told to
answer from the chunks alone picked it. The response includes a resolved
references block for exactly this; a binding it marks `AMBIGUOUS` has more
than one candidate in scope and is **not** an answer.

## Several phrasings

`scopegrep_multi_retrieve` fuses several queries over one scope. Use it when a
question has genuinely distinct sub-parts needing different evidence — a
behaviour *and* the default that governs it. Do not use it for paraphrases of
one question: measured on two documents, naive packaged-query fusion fell from
1.0 coverage to 0.233, and a union merely recovered the single-query baseline.

Agreement counts are reported as **coverage** — how many of your queries
ranked a chunk — and nothing more. They are not a correctness signal: a failed
query shrinks the denominator, and paraphrases are correlated evidence, so a
chunk every phrasing returned can still be the wrong chunk. Decide from the
code. If none of it implements the behaviour, say so and fall back to grep.

## Who it helps

The tool's value scales inversely with how well the calling model already
searches, because what it sells is a shortcut past flailing.

| model | grep-only | with scopegrep | verdict |
|---|---|---|---|
| Haiku 4.5 | 40.5s, 12 turns, 316,813 tok, partly wrong | 43.0s, 4 turns, 106,346 tok, correct | ~2.98x fewer tokens, better answer |
| Sonnet | 16.0s, 3 turns, 103,851 tok, correct | 39.6s, 4 turns, 147,011 tok, **wrong** | slower, costlier, worse |

Sonnet greps in three turns and leaves nothing to recover; forced onto the
retrieval path it answered worse. Offered the tool and left to judge, it
declined and grepped — the right call. Two observations, not a general result.

## Setup and service behaviour

Three tools: `scopegrep_status` (health, warm containers, cached scopes),
`scopegrep_scope` (free local preview — no GPU), `scopegrep_retrieve`. If they
are absent the plugin is not loaded into this session; do not claim retrieval
succeeded because the skill is present.

Provide `SCOPEGREP_TOKEN` through the MCP server's environment or
`~/.config/scopegrep/token`. `SCOPEGREP_URL` only overrides the deployed
endpoint. Never put a secret in the manifest, repository, skill, or a reply.

- Cold service (scaled to zero): **75-150s** for the first call, almost all of
  it loading the model. A retrieval that paid a cold start says so.
- First query on a new scope, warm: **~5-10s**. 589 chunks / 198k tokens
  indexed in 9.7s; 139 chunks in 2.3s.
- Repeat queries on an unchanged scope: **~2.0-2.5s**, question-only through
  the warm KV cache — bit-identical scores, verified by
  `tools/smoke.py --selftest`.
- The container scales to zero after 3 minutes idle.

Editing any file in the scope invalidates the local cache — identity is a
content hash, so a same-size edit that preserves the timestamp still
invalidates — and the next query re-uploads.

## Known limits, to be stated rather than worked around

- **Recall is not 1.0.** At a 3k budget one code query in five did not surface
  its gold file. Scopegrep narrows the search; it does not close it.
- **The scorer is not the model reading the results.** Ranking comes from
  Qwen3.5-9B. A 0.8B model on the same documents scored Spearman 0.799 and
  keep-set IoU 0.662 — correlated, not identical.
- **Hybrid ranking is not established as a token-efficiency win.** Its extra
  recall at equal k came with ~32% more returned tokens, and at matched
  budgets on 20 queries no budget showed an advantage whose 95% confidence
  interval excluded zero.
- **Scopes above ~1,200 chunks are unmeasured.** The largest benchmarked
  haystack was 589 chunks / 198k tokens.
- **`split="window"` is unmeasured.** The published recall came from one
  head-truncated chunk per file.
- **Scope excludes some files by policy** and says so in the response:
  gitignored paths, credential-shaped files, and anything whose real path
  resolves outside the declared root.
