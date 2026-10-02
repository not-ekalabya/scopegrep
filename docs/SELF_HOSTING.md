# Self-hosting the scoring service

scopegrep has two halves. The **client** (`src/scopegrep/`, the Claude Code
plugin / MCP server) walks your repository, cuts it into chunks, and renders
results. The **service** (`backend/`) holds the model and does the scoring.
There is no hosted service: you run the backend yourself, on your own GPU or
on Modal, and point the client at it.

| | Local GPU | Modal |
|---|---|---|
| you need | one CUDA GPU (see sizes below) | a Modal account |
| cost | your hardware | per-second GPU billing, scales to zero when idle |
| first request | after the model loads at startup | ~1–2 min if the container was idle |
| auth | optional on `127.0.0.1`, required otherwise | always (a Modal secret) |

Either way, the service downloads `Qwen/Qwen3.5-9B` from Hugging Face on
first start (~19 GB on disk; only the first 20 decoder layers, 10.7 GB in
bf16, are loaded onto the GPU).

## What the service runs

The method is the one in the paper (`backend/service.py` has the full
description):

- **Model:** Qwen3.5-9B, truncated after decoder layer 20. Layers 21–32, the
  LM head and the vision tower are never loaded (5.34B of 9.41B parameters).
- **Score:** each chunk's layer-20 question-to-chunk attention, averaged over
  heads, question tokens and the chunk's tokens, minus the same readout for
  the content-free question "N/A".
- **Scope Attention:** the whole scope is prefilled once, at full length, into
  a key–value / DeltaNet cache. Each query runs only its own tokens over that
  cache and the cache is rolled back exactly afterwards. Scopes over 250,000
  tokens are split into equal shards, scored separately and interleaved by
  rank.
- **Fine Attention:** the top of that ranking, up to 8,000 tokens, is
  re-scored in a fresh prompt where the candidates compete only with each
  other.
- **Edits:** a changed scope is re-encoded from the last block boundary
  before the first changed token, not from scratch.

`backend/two_pass_core.py`, `layer_exit_core.py` and `scope_cache_core.py`
are the research code the paper's numbers were measured with, unmodified.

## GPU sizing

Memory is the 10.7 GB of weights plus 20 KB per cached scope token, plus
one 4,096-token block's activations while encoding. Measured on an
A100-80GB (paper, Appendix A):

| scope | chunks | encode (once) | per query | held | peak |
|---|---|---|---|---|---|
| 16k tokens | 100 | 2.2 s | 1.3 s | 11.1 GB | 12.1 GB |
| 52k | 300 | 8.1 s | 1.4 s | 11.8 GB | 14.2 GB |
| 101k | 588 | 20.1 s | 1.8 s | 12.8 GB | 17.3 GB |
| 238k | 1,380 | 84.7 s | 2.2 s | 15.6 GB | 27.2 GB |

So a 24 GB card handles scopes up to roughly 150k tokens (several hundred
files of code); a full-size scope (~240k tokens, ~1,400 chunks) peaks at
27 GB and needs a 32 GB card or larger. The service keeps
several scopes warm at once, up to `SCOPEGREP_MAX_CACHED_TOKENS` scope tokens
in total (default 600,000 ≈ 12 GB of cache); lower it on a smaller card.
The DeltaNet layers run without their optimised kernels, so these times are
upper bounds.

## Option A: your own GPU

```bash
cd scopegrep    # the unzipped repository folder (see README, Install)
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt    # torch 2.12 / transformers 5.8, as in the paper
python backend/serve.py                    # listens on http://127.0.0.1:8000
```

The first start downloads the model; startup then takes under a minute.
`serve.py` options: `--host`, `--port`, `--device`, `--model`,
`--exit-layer`. Changing the model or exit layer leaves the configuration
the paper measured.

The client's default `SCOPEGREP_URL` is `http://127.0.0.1:8000`, so with the
plugin installed on the same machine nothing else needs configuring.

**Serving another machine** (e.g. a GPU box on your network): set a token,
and bind a non-loopback address. `serve.py` refuses to bind one without a
token, because anyone who can reach the port could send it code.

```bash
export SCOPEGREP_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
python backend/serve.py --host 0.0.0.0 --port 8000
```

Then on the client machine set `SCOPEGREP_URL=http://<gpu-host>:8000` and the
same `SCOPEGREP_TOKEN`. The service speaks plain HTTP; put it behind a TLS
reverse proxy or an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 gpu-host`) if the
network is not yours.

## Option B: Modal

```bash
cd scopegrep    # the unzipped repository folder (see README, Install)
pip install modal
modal setup                                   # one-time login

# the shared secret clients send; keep it out of your shell history if you can
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
modal secret create scopegrep-auth SCOPEGREP_TOKEN="$TOKEN"

modal deploy backend/modal_app.py
```

The deploy prints the endpoint URL (`https://<workspace>--scopegrep-scopegrep-web.modal.run`).
Give the client that URL and `$TOKEN`. The first build takes a few minutes
(CUDA image plus torch); later code-only redeploys take seconds. The model
weights are cached in a Modal volume (`scopegrep-hf-cache`) after the first
cold start.

Settings are read from your environment at deploy time:

| variable | default | meaning |
|---|---|---|
| `SCOPEGREP_GPU` | `A100-80GB` | Modal GPU spec. `A100-40GB` or `L40S` (48 GB) also fit a full-size scope; set `SCOPEGREP_MAX_CACHED_TOKENS` to ~300000 on them. |
| `SCOPEGREP_SCALEDOWN` | `300` | seconds idle before the container is torn down |
| `SCOPEGREP_MIN_CONTAINERS` | `0` | containers held warm regardless of traffic. Bills continuously when > 0. |
| `SCOPEGREP_MAX_CACHED_TOKENS` | `600000` | scope tokens kept warm across scopes |
| `SCOPEGREP_MODEL_ID` | `Qwen/Qwen3.5-9B` | scoring model |

```bash
SCOPEGREP_GPU=A100-40GB SCOPEGREP_MAX_CACHED_TOKENS=300000 modal deploy backend/modal_app.py
```

`max_containers=1` is deliberate: the warm scope caches live in one
container's GPU memory, so one container means the next query always finds
them, and it bounds the bill.

**Cold starts.** With `min_containers=0` an idle deployment costs nothing, and
the first request after it scales down waits for the model to load (about
1–2 minutes). Run `scopegrep-prewarm` (or `tools/prewarm.sh`) a couple of
minutes before you need it, or `scopegrep-prewarm --watch 240` to keep it
warm for a session without holding a container permanently.

**Revoking access** means rotating the secret and redeploying:
`modal secret create scopegrep-auth SCOPEGREP_TOKEN=<new> --force`, then
`modal deploy backend/modal_app.py`. There is one shared token, no per-user
accounts.

**Stopping it:** `modal app stop scopegrep`.

## Point the client at it

```bash
export SCOPEGREP_URL='http://127.0.0.1:8000'     # or your Modal URL
export SCOPEGREP_TOKEN='...'                      # only if the service has one
```

The token can also live in `~/.config/scopegrep/token`, so it is never in a
file you commit. Then, in Claude Code, run `/scopegrep-check`, or call the
`scopegrep_status` tool.

## Verify it

```bash
python3 backend/smoke.py               # health, cache self-test, one retrieval
python3 backend/smoke.py --selftest    # just the cache checks
python3 backend/smoke.py --scope 'src/**/*.py' --query 'where is the scope cache keyed'
```

The self-test checks the two properties the cache depends on: the same query
scores identically before and after other queries (the rollback is exact),
and a scope re-encoded after an edit ranks like one encoded from scratch.

## HTTP API

All endpoints take and return JSON, with the token (if any) in the
`X-Scopegrep-Token` header.

| endpoint | body | returns |
|---|---|---|
| `GET /health` | — | model, exit layer, uptime, warm scopes |
| `POST /index` | `chunks`, `mode` | `scope_key`, per-chunk token counts, shard count |
| `POST /retrieve` | `query`, `k`, `mode`, and `chunks` or a warm `scope_key` | ranking, `top_k`, timings, warnings |
| `POST /selftest` | optional `chunks`, `query` | rollback and refresh exactness |

`mode` is `"codegen"` for source code and `"short"` for prose. A
`/retrieve` with a `scope_key` the service no longer holds answers `409`;
resend with `chunks` (the client does this automatically).

## Privacy

The service sees exactly the chunks the client sends: the files inside the
scope you declare with `include=[...]`, minus gitignored paths and
credential-shaped files. It keeps them in GPU memory while the scope is warm
and writes nothing to disk except the model weights.
