# scopegrep

Attention-scored context retrieval for Claude Code, as a plugin.

Declare a scope of the repository, ask a question in prose, get back the k
chunks the model itself attends to hardest while reading that question — not
the chunks that happen to contain your keywords.

```
scopegrep_scope(include=["src/**/*.py"])
  -> 214 files -> 583 chunks, ~197,000 tokens
     k=20  ~6,700 tokens   <- recommended

scopegrep_retrieve(query="<the full failing test output>", include=["src/**/*.py"], k=20)
  -> top 20, 6,712 tokens returned (3.4% of scope), 8.4s
```

Measured gold-file recall on a 198k-token code haystack: **0.800 at k=20**.
Full tables and provenance in [BENCHMARKS.md](BENCHMARKS.md); how to choose
scope and k in [skills/scopegrep/SKILL.md](skills/scopegrep/SKILL.md).

## How it works

Two passes over the scope, one GPU call:

1. **Stage 1 — global gist ranking.** Every chunk is cut to a 32-token gist
   of its own head and tail, so all N chunks fit one prompt and are compared
   under **one softmax**. A chunk at position 580 can outrank one at position
   3 on evidence rather than on arrival order. Nothing is evicted.
2. **Stage 2 — full-resolution re-rank.** The top-ranked chunks that fit an
   8000-token budget (~26 chunks of source code) are re-scored at full token
   resolution under one shared softmax, and that order replaces stage 1's
   within the candidate set.

The scorer is Qwen3.5-9B on an A100-40GB, hosted on Modal, **scaled to
zero** when idle. A scope's stage-1 KV cache stays warm on the container
between queries, so repeat queries against unchanged files skip the prefill.

Because stage 2 only re-ranks its candidate set, the ranking is
authoritative only up to `ranking_valid_to_k` — reported on every response,
and the reason k above ~25 buys nothing on code.

## Two repositories

The scoring service and the client are split, deliberately:

| repo | visibility | what it is |
|---|---|---|
| [`scopegrep`](.) (this one) | public | the plugin + `pip`-installable client: MCP server, skill, hooks, tests. This is what a pilot partner installs. |
| `scopegrep-server` | private | the Modal app: model load, two-pass scoring, `/health` `/index` `/retrieve` `/selftest`. This is what runs on a GPU. Only the person hosting the service needs it. |

A pilot partner never touches `scopegrep-server` or needs a Modal account —
they get a URL and a token (below) and point this repo's plugin at them.

## Layout

```
.claude-plugin/plugin.json    plugin manifest
.mcp.json                     MCP server registration (stdio, run via uv)
src/scopegrep/server.py       MCP server: walks the repo, chunks it, calls the service
src/scopegrep/prewarm.py      pip-installable twin of tools/prewarm.sh
tools/prewarm.sh              zero-dependency pre-warm script for a plugin-only install
skills/scopegrep/SKILL.md     scope and k guidance (loaded by the agent)
tests/test_outbound.py        20 tests for the completeness block, no network/GPU
pyproject.toml                `pip install -e .` -- console scripts: scopegrep-server, scopegrep-prewarm
BENCHMARKS.md                 every number, and how it was measured
```

The scoring service itself (`app.py`, `two_pass_core.py`, `gist_index.py`,
`tools/extract_scorer.py`, `tools/replay_benchmark.py`, `tools/smoke.py`)
lives in the separate `scopegrep-server` repo.

## Setup

### 1. Get a pilot access code

**Email [ekalabya2010@gmail.com](mailto:ekalabya2010@gmail.com) to request
one.** Pilot testing is by invitation right now — there is no self-serve
signup and no per-user database (see `scopegrep-server`'s README for why);
every tester authenticates with the same shared code, sent by hand.

If you're deploying your own service instead of using the hosted one: see
`scopegrep-server`'s README. In short, `modal secret create scopegrep-auth
SCOPEGREP_TOKEN="$TOKEN"` then `modal deploy app.py` from that repo, on a
profile with GPU quota.

### 2. Point the plugin at it

```bash
export SCOPEGREP_URL='https://gekalabya2010--scopegrep-scopegrep-web.modal.run'
export SCOPEGREP_TOKEN='<the code you were given>'
```

`.mcp.json` reads both from the environment, so the code is never written
into the plugin. (`SCOPEGREP_TOKEN` also falls back to
`~/.config/scopegrep/token` or a `.scopegrep_token` file at this repo's root,
for a persistent local setup — never commit either; both are gitignored.)

### 3. Install, straight from GitHub — no local clone needed

Two ways to run this, same code either way, both verified against this repo
as pushed:

**As a Claude Code plugin:**

```bash
claude plugin marketplace add not-ekalabya/scopegrep
claude plugin install scopegrep@scopegrep
```

Then `/mcp` should list `scopegrep` with four tools: `scopegrep_status`,
`scopegrep_scope`, `scopegrep_retrieve`, `scopegrep_multi_retrieve`.
`.mcp.json` invokes `src/scopegrep/server.py` via `uv run --with mcp --with
httpx`, which resolves those two dependencies on the fly — no `pip install`
required for the plugin path.

(A local clone still works too: `claude plugin install /path/to/scopegrep`.)

**As a Python package**, if you want `scopegrep-server` / `scopegrep-prewarm`
on your `PATH` independent of the plugin:

```bash
pip install git+https://github.com/not-ekalabya/scopegrep.git
```

Or from a local clone: `pip install -e /path/to/scopegrep`.

Both installation methods, both ways, run the identical
`src/scopegrep/server.py` file.

### 4. Check it end to end

```bash
scopegrep-prewarm                 # forces the cold start now, not mid-demo
python3 tools/smoke.py            # health, selftest, index, retrieve (needs scopegrep-server checked out too)
```

`tools/smoke.py --selftest` is the one worth understanding: it runs the same
query through the warm KV cache twice and through a cold, uncached
`landmark_score` once, and reports the maximum score difference. The warm
path is only legitimate if that difference is zero. It probes twice because
the failure mode it guards against — an aliased recurrent-state snapshot in
the 24 linear-attention layers — is invisible on the first probe and
corrupts every one after it.

## What else calls what you just read

Every `scopegrep_retrieve` response carries a second block alongside the
ranked chunks: for each symbol the returned chunks *define*, where else that
symbol is called. It costs no extra turn, and its size is whatever the
repository already contains — one line for a symbol nobody calls, six for one
with five callers.

It exists because completeness is a different question from relevance and the
ranking cannot express it. A shared helper's other call sites share no
vocabulary with the bug report, so a relevance ranker is *right* not to return
them under a token budget — and an agent that changes the helper still has to
decide about every one of them. Asking in a second round trip is what makes it
expensive: the answer is 12–60 tokens and a turn costs tens of thousands of
billed tokens of re-sent context.

Measured on a 3-task agentic benchmark (n=3–7 per task, paired against a
no-retrieval control): **pooled test-case pass rate 96.8% vs the control's
85.2%, and 0.57–0.72x billed cost across three independent batches**, to
first patch. One disclosed, reproducible limitation: on a bug whose complete
fix requires a sibling code path with different vocabulary (measured case: a
`std`/`var`-shaped aggregation branch), this arm misses that sibling roughly
half the independent runs — patch is otherwise correct, not a wrong diagnosis,
just incomplete on 2 of 9 tests in that shape. On a task whose fix crossed
from Python into C++, nothing resolved — for this or any other arm measured,
including the control.

Honest limits, because they decide whether it helps you:

- **Anchors come from Python and Cython definitions**, generalized to the
  chunker's own vocabulary (`def`, `class`, `function`, `func`, `fn`, `impl`,
  `struct`, `interface`, `type`, `trait`, `enum`), so Go, Rust and TypeScript
  work. A definition that leads with a return type instead of a keyword — a
  C, C++ or Java method — yields no anchor. Its *call sites* are still
  reported when something else anchors the symbol; it is the definition side
  that cannot see it.
- **It is a whole-word name scan, not a resolver.** Two unrelated functions
  sharing a name are both reported. Telling them apart is yours to do.
- **It needs a git repository.** `_repo_callers` shells out to `git grep`;
  without a git root it falls back to scanning the declared scope, which is
  narrower but never wrong about what it saw.

`tests/test_outbound.py` covers all of the above, including the gaps — run it
with `python3 tests/test_outbound.py`, no network or GPU needed.

## Cold starts, and who pays for them

The service scales to zero by default, so the first query after an idle
period blocks while a 9B model loads. Measured on this deployment:

```
149.97s wall / 135.2s load    (first request after deploy, cold image cache)
117.2s wall / 11.0s scoring
116.3s wall / 13.4s scoring
 71.1s wall /  5.7s scoring
```

The scoring is the tool. The rest is the load, and it is paid by whoever
queries first. Three ways to not hand that to someone you are demoing to,
cheapest first:

1. **Pre-warm before you need it.** `/health` is served from inside the GPU
   class, so touching it starts the container and runs the load:

   ```bash
   scopegrep-prewarm                 # once, ~2 min before the session (pip-installed)
   scopegrep-prewarm --watch 120     # hold it warm for a demo window
   ./tools/prewarm.sh                # same thing, no pip install needed
   ./tools/prewarm.sh --watch 120
   ```

   `scopegrep_status` from inside a session does the same thing.

2. **Widen the idle window** for a work session that queries in bursts:
   `SCOPEGREP_SCALEDOWN=1800` (set at deploy time, in `scopegrep-server`)
   means one cold start per session rather than one every three minutes of
   thinking.

3. **Hold a container open** for a scheduled demo or a pilot week:
   `SCOPEGREP_MIN_CONTAINERS=1` (set at deploy time). This bills for an A100
   whether or not anybody queries it, so set it for the window and unset it
   afterwards.

`scopegrep_status` reports which of these is in force — a deployment that is
held warm and one that silently scaled to zero are otherwise identical from
the outside until somebody pays the load.

## Cost

An A100-40GB on Modal is $0.000583/s. Measured: **2.2-2.5s** of GPU per
query against a warm scope, 2.3-9.7s to index a new scope, and 71-150s for a
cold start (model load). Idle costs nothing — the container is torn down
`SCOPEGREP_SCALEDOWN` seconds after the last request.

Rough: a working session with a handful of retrievals is a few cents. Leaving
the service deployed and unused is free — unless `SCOPEGREP_MIN_CONTAINERS` is
set, which bills for the GPU continuously and is the one setting here that can
cost real money while nobody is using it.

## Limits

Stated plainly, because they change how you should use it:

- **Recall is 0.800 at k=20, not 1.0.** One code query in five did not surface
  its gold file. This narrows a search; it does not close one.
- **Grep beats it at the top of the ranking.** 0.350 vs 0.200 at k=1 on the
  same corpus. Grep first for anything you can spell exactly.
- **The scorer is not the agent's model.** Ranking comes from Qwen3.5-9B.
  Cross-model transfer of these rankings is plausible and unproven.
- **Read-only, non-incremental.** Editing any file in a scope invalidates the
  cache and re-uploads the whole scope on the next query.
- **Scopes above ~1200 chunks are unmeasured**; 2000 is a hard cap.
- **The completeness block anchors on Python and Cython (+ Go/Rust/TS)
  definitions.** See above for what that excludes.
- **Single shared token, no per-partner auth.** Every pilot tester
  authenticates with the same `SCOPEGREP_TOKEN`. Fine for a small, trusted
  pilot cohort; revoke by rotating the Modal secret and redeploying if that
  changes.
