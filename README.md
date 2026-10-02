# scopegrep

Find code by describing what it does, not by guessing what it's called.

`scopegrep` is a Claude Code plugin for coding agents. Instead of matching
keywords, it answers a question in plain language — "where is retry backoff
configured", "what handles session invalidation" — against a declared part of
a codebase, and returns the code most relevant to that question, along with
every other place in the codebase that calls what it returned.

It is open source and self-hosted: the scoring service lives in
[`backend/`](backend/) and runs on your own GPU or on Modal
([guide](docs/SELF_HOSTING.md)). There is no hosted service and no access
code.

## How it works

scopegrep is the retriever from the research paper *Attention as Search:
Where Language Models Decide What Matters in Their Context, and a Retriever
for Coding Agents Built on It* (Ekalabya Ghosh, 2026). The paper asks how a language model decides what in its
context matters, and finds that attention works in stages: early layers favour
chunks that resemble the question, one middle layer is where the question
shifts attention onto what is actually relevant, and later layers spread
attention out again. scopegrep reads that middle layer directly. It needs no
training and no index.

- **Score.** The declared files are split into chunks and placed, at full
  length, in one prompt followed by the question. A chunk's score is the
  attention the question pays it at layer 20 of Qwen3.5-9B (averaged over
  heads and tokens), minus the attention a content-free question ("N/A")
  pays it. Every chunk is scored under one softmax, so a chunk can raise
  another's score — a function's caller helps its undescribed helper rank.
- **Early exit.** Nothing after layer 20 affects the score, so only the first
  20 decoder layers are loaded: 5.34B of 9.41B parameters, 10.7 GB.
- **Scope Attention.** The scope does not depend on the question, so it is
  encoded once into a cache. Each query then runs only its own tokens over
  the cache, ranks every chunk, and the cache is rolled back exactly. A
  238,000-token scope (1,380 chunks) encodes in 85 s on one A100 and each
  query then takes about 2 s. After an edit, the cache is re-encoded from the
  first changed chunk onward.
- **Fine Attention.** The top of that ranking, up to 8,000 tokens, is
  re-scored in a fresh prompt where the candidates compete only with each
  other.
- **What else the change touches.** For the symbols the returned chunks
  define, one `git grep` lists their call sites elsewhere in the repository,
  so the agent gets the relevant code and what depends on it in one call.

## Results

From the paper. Every retriever ranked the same chunks for the same
question; the held-out benchmarks were built from repositories and questions
used nowhere in development, under a preregistration. Intervals are 95%
paired bootstrap.

| benchmark (held-out) | scopegrep | best 7–8B embedder |
|---|---|---|
| code call chains, both hops in top 2 (n=150) | **0.77** | 0.51 (Qwen3-Embedding-8B) |
| 3–4-hop code chains, every hop in top 10 (n=100) | **0.86** | 0.46 (Qwen3-Embedding-8B) |
| SWE-bench issue → file, MRR (n=117) | **0.880** | 0.801 (gte-Qwen2-7B) |
| same issues without identifiers, MRR (n=117) | 0.814 | 0.800 (Qwen3-Embedding-8B), level |
| HotpotQA bridge, both hops in top 2 (n=200) | 0.71 | 0.65 (e5-mistral-7b), level |

- **Where it wins:** questions whose answer lies in code connected to, but
  unlike, the question — the helper a described function calls (+0.26
  [+0.17, +0.36] at top 2), and more so as chains get longer (+0.40 at
  3–4 hops).
- **Where it is level:** when the answer resembles the question (HotpotQA,
  identifier-free issues), it matches the best embedders. On the 25 hardest
  development issues it trailed gte-Qwen2-7B (0.77 vs 0.88 MRR).
- **Agents.** On 34 SWE-bench Verified instances, agents using scopegrep spent
  0.78× the tokens per episode of the same agent without it (95% CI
  0.68–0.90) and 0.69× per resolved bug. They resolved 40 of 84 paired
  episodes against 34 — directionally better, not significant (p = 0.21).
  They spent less on the same tasks; they did not detectably fix more.
- **Literal lookups.** When the question names the symbol, `grep` is as good
  (definition file in the top 10: 98% vs 100% on 1–2 million-token
  repositories). Send literal lookups to grep.

Limitations, from the paper: all retrieval results use one model
(Qwen3.5-9B); code benchmarks come from seven Python repositories; scopes
over ~250k tokens are split into shards that cannot see each other; and the
cost of re-encoding after edits in real sessions was not measured.

## Install

```bash
claude plugin marketplace add not-ekalabya/scopegrep
claude plugin install scopegrep@scopegrep
```

or, for the command-line tools only:

```bash
pip install git+https://github.com/not-ekalabya/scopegrep.git
```

## Run the scoring service

The plugin needs a running backend. Pick one ([full guide](docs/SELF_HOSTING.md)):

**Your own GPU** (one CUDA GPU; 24 GB handles scopes up to ~150k tokens):

```bash
git clone https://github.com/not-ekalabya/scopegrep.git && cd scopegrep
pip install -r backend/requirements.txt
python backend/serve.py            # http://127.0.0.1:8000, the client's default
```

**Modal** (scales to zero when idle):

```bash
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
modal secret create scopegrep-auth SCOPEGREP_TOKEN="$TOKEN"
modal deploy backend/modal_app.py
export SCOPEGREP_URL='<the URL modal deploy printed>'
export SCOPEGREP_TOKEN="$TOKEN"
```

The token can also live in `~/.config/scopegrep/token` instead of your
environment. Check the setup with `/scopegrep-check` in Claude Code, or
`python3 backend/smoke.py`.

## Documentation

### Quick start

Once installed and pointed at a running backend, an agent session with the plugin active gains
four tools. You don't call these by hand in normal use — the agent decides
when to use them, guided by [the bundled skill](skills/scopegrep/SKILL.md) —
but this is what they do:

| tool | what it's for |
|---|---|
| `scopegrep_status` | Check whether the service is reachable and ready. |
| `scopegrep_scope` | Preview how large a set of files is before searching it — how many pieces it breaks into, roughly what that will cost to search. |
| `scopegrep_retrieve` | The main tool: ask a question in prose about a declared set of files, get back the most relevant pieces of code — and, alongside them, a note on every other place in the codebase that uses the same functions or classes, so a change doesn't miss a caller it should have updated too. |
| `scopegrep_multi_retrieve` | Ask several related questions against the same declared scope in one call. |

Running it outside a Claude Code session — another MCP host, or the raw
console scripts — is documented separately: [docs/PYTHON_MODULE.md](docs/PYTHON_MODULE.md).

### A typical exchange

```
scopegrep_scope(include=["src/**/*.py"])
  -> previews the scope: how many files, how many pieces, and whether
     it's small enough to search directly or should be narrowed first

scopegrep_retrieve(query="<a description of the bug, or the failing test output>",
                    include=["src/**/*.py"])
  -> the most relevant pieces of code, plus a note on every other place
     that calls the same functions -- so a fix doesn't miss a sibling
     call site it should also have touched
```

### When to reach for it

- You can describe a *behavior* but don't know which file or function
  implements it.
- A grep for the obvious keyword comes back empty, or comes back with too
  much to read through.
- You're about to change a function and want to know everywhere else in the
  codebase that calls it, before you decide the change is complete.

### When not to

- You already know the exact name, path, or error string — grep is faster
  and exact.
- You've already found and opened the file — just read it.

### First request may be slow

The first query on a scope encodes it (seconds for a few hundred chunks,
about a minute and a half for ~1,400); later queries on it take about two
seconds. A Modal deployment that has scaled to zero also waits for the model
to load, a minute or two. Run `scopegrep-prewarm` (or `./tools/prewarm.sh`) a
couple of minutes ahead of a demo so that wait happens before anyone's
watching.

### FAQ

**Does it see my code?** Only the files inside the scope you declare with
`include=[...]`, minus gitignored and credential-shaped files. They go only to
the backend you run.

**Does it modify anything?** No — every tool here is read-only. It never
edits, writes, or deletes files.

**Do I need a GPU?** The backend does: one CUDA GPU on your machine, or a
Modal account. The client runs anywhere.

**What if it's slow or wrong?** Open an issue with what you asked, what came
back, and what you expected.

## License

MIT.
