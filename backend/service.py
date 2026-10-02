"""The scopegrep scoring service: ScopeGrep as described in the paper, behind
a small HTTP API. Host-agnostic -- `serve.py` runs it on your own GPU,
`modal_app.py` runs it on Modal. Both import `ScopeGrepService` and
`make_api` from here, so the two deployments cannot drift apart.

The method, in the order a request meets it:

  model         Qwen3.5-9B truncated after decoder layer 20
                (`layer_exit_core.load_exit_model`): layers 21-32, the LM head
                and the vision tower are never loaded. 5.34B of 9.41B params.
  score         layer-20 head-mean question->chunk attention, minus the same
                readout for the content-free question "N/A".
  Scope         the scope (every chunk at full length) is prefilled ONCE into
  Attention     a KV/DeltaNet cache, in blocks. Each query runs only its own
                tokens over the cache, reads every chunk's score, and the
                cache is rolled back exactly (softmax KV truncated, DeltaNet
                state restored). "N/A" is read once per scope.
  shards        a scope over 250,000 tokens is split into the fewest
                contiguous shards of near-equal size; each is cached and
                scored on its own, and the rankings are interleaved by rank.
  Fine          walk that ranking, admit each chunk whose full length still
  Attention     fits an 8,000-token budget, re-score the admitted set in a
                fresh prompt with its own "N/A" readout; final ranking is the
                admitted set by that score, then the rest in Scope order.
  edits         a changed scope is not re-encoded from scratch: the cached
                scope sharing the longest token prefix with it is rolled back
                to the last block boundary before the first changed token and
                re-encoded from there.

`two_pass_core`, `layer_exit_core` and `scope_cache_core` are the research
code the paper's numbers were measured with, copied unmodified. This module
only composes them; the composition is the one the final evaluation used
(per-shard `ScopeCache.score`, `merge_shards`, `fine_rank`).

Endpoints (POST unless noted):
  GET  /health    model, warm scopes, uptime
  POST /index     encode a scope's chunks -> scope_key + per-chunk token counts
  POST /retrieve  scope_key (or chunks) + query + k -> ranking
  POST /selftest  rollback and edit-refresh exactness on a synthetic scope
"""
import hashlib
import hmac
import os
import threading
import time

MODEL_ID = os.environ.get("SCOPEGREP_MODEL_ID", "Qwen/Qwen3.5-9B")
EXIT_LAYER = int(os.environ.get("SCOPEGREP_EXIT_LAYER", "20"))
# Scope tokens held across all warm scopes. The cache is 20 KB/token for the
# layer-20 Qwen3.5-9B (5 softmax layers x 4 KV heads x 256 dims x K,V x bf16),
# so the default holds ~12 GB of cache next to 10.7 GB of weights. On a 24 GB
# card set it to ~150000.
MAX_CACHED_TOKENS = int(os.environ.get("SCOPEGREP_MAX_CACHED_TOKENS", "600000"))
MAX_CHUNKS = int(os.environ.get("SCOPEGREP_MAX_CHUNKS", "20000"))
FINE_BUDGET_DEFAULT = 8000      # the benchmarked Fine Attention budget
BLOCK_TOKENS = 4096             # prefill block; bounds peak activation memory
MAX_K = 200


def scope_key(chunks, mode):
    h = hashlib.sha256()
    h.update(f"{MODEL_ID}|{EXIT_LAYER}|{mode}|{len(chunks)}\x00".encode())
    for c in chunks:
        h.update(c.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _common_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _shard_index_class():
    """ShardIndex subclasses scope_cache_core.ScopeCache, which imports torch;
    built lazily so this module imports without torch (Modal's local side)."""
    import torch
    from layer_exit_core import NULL_QUESTION, _question_pass
    from scope_cache_core import MAX_POSITIONS, ScopeCache
    from two_pass_core import (
        _restore_linear_state, _snapshot_linear_state, _truncate_full_attn_cache, build_prompt,
        full_attention_layer_indices, linear_attention_layer_indices)

    class ShardIndex(ScopeCache):
        """scope_cache_core.ScopeCache with re-encoding after edits.

        Same cache, same readout, same N/A subtraction (`score`, `_readout`
        are ScopeCache's). Two differences in how the cache is built:
        prefill blocks end on chunk boundaries where possible, and the
        DeltaNet state at the start of every block is kept (on the CPU) so
        that the cache can later be rolled back to any block boundary.
        Blocked prefill changes nothing but memory (paper, Appendix A)."""

        @torch.no_grad()
        def __init__(self, model, tokenizer, chunks, mode, kept, block=BLOCK_TOKENS, q_block=1024,
                     prompt=None):
            self.model, self.tok, self.mode, self.q_block, self.block = model, tokenizer, mode, q_block, block
            self._qpass = _question_pass
            self.full_idx = full_attention_layer_indices(model)
            self.lin_idx = linear_attention_layer_indices(model)
            self.kept = list(kept)
            self.n_probes = 0
            self.cache, self.ids, self.bounds, self.block_snaps = None, [], [0], [None]
            ids, spans = prompt if prompt is not None else self.build(tokenizer, chunks, mode, self.kept)
            self.reencoded_tokens = self._encode(ids, spans, 0)

        @staticmethod
        def build(tokenizer, chunks, mode, kept):
            """(context token ids, chunk spans) -- the prompt up to the question."""
            ids, spans, q_span = build_prompt(tokenizer, chunks, kept, "", mode)
            ctx_len = q_span[0]
            if ctx_len >= MAX_POSITIONS:
                raise ValueError(f"shard is {ctx_len} tokens, over the model's {MAX_POSITIONS} positions")
            return ids[0, :ctx_len].tolist(), spans

        def _plan(self, n, spans, bounds):
            bounds = list(bounds)
            for s in sorted(st for st, _ in spans.values()) + [n]:
                if s <= bounds[-1]:
                    continue
                while s - bounds[-1] > 2 * self.block:   # one very long chunk: cut inside it
                    bounds.append(bounds[-1] + self.block)
                if s - bounds[-1] >= self.block and s < n:
                    bounds.append(s)
            return bounds

        def _cpu_snapshot(self, cache):
            return {i: (cache.layers[i].conv_states.to("cpu", copy=True),
                        cache.layers[i].recurrent_states.to("cpu", copy=True)) for i in self.lin_idx}

        def _encode(self, ids, spans, b):
            """Prefill from block boundary b (0 = from scratch). Returns tokens encoded."""
            device = next(self.model.parameters()).device
            t = time.time()
            if b == 0:
                cache, bounds, snaps = None, self._plan(len(ids), spans, [0]), [None]
            else:
                cache = self.cache
                _truncate_full_attn_cache(cache, self.full_idx, self.bounds[b])
                # copies: the stored snapshot must survive in-place state updates
                _restore_linear_state(cache, self.lin_idx, {i: (c.to(device, copy=True), r.to(device, copy=True))
                                                            for i, (c, r) in self.block_snaps[b].items()})
                bounds, snaps = self._plan(len(ids), spans, self.bounds[:b + 1]), self.block_snaps[:b + 1]
            ctx = torch.tensor([ids], device=device)
            for j in range(b, len(bounds)):
                if j > b:
                    snaps.append(self._cpu_snapshot(cache))
                hi = bounds[j + 1] if j + 1 < len(bounds) else len(ids)
                out = self.model(input_ids=ctx[:, bounds[j]:hi], past_key_values=cache, use_cache=True)
                cache = out.past_key_values
                del out
            if device.type == "cuda":
                torch.cuda.synchronize()
            self.encode_seconds = time.time() - t
            self.cache, self.ids, self.spans, self.ctx_len = cache, ids, spans, len(ids)
            self.bounds, self.block_snaps = bounds, snaps
            self.snap = _snapshot_linear_state(cache, self.lin_idx)
            self.null = self._readout(NULL_QUESTION)
            return len(ids) - bounds[b]

        def reusable_from(self, ids):
            """Index of the last block boundary whose whole prefix `ids` shares,
            or 0 if nothing past the chat head would be reused."""
            # at least the last token is re-encoded, so a block is never empty
            p = min(_common_prefix(self.ids, ids), len(ids) - 1)
            return max(j for j, s in enumerate(self.bounds) if s <= p)

        @torch.no_grad()
        def refresh(self, chunks, kept, prompt):
            """Re-encode after an edit, from the last block before the first changed token."""
            ids, spans = prompt
            b = self.reusable_from(ids)
            self.kept = list(kept)
            if b == 0:
                self.cache = None
            self.reencoded_tokens = self._encode(ids, spans, b)
            return self.reencoded_tokens

        def score(self, question):
            self.n_probes += 1
            return super().score(question)

    return ShardIndex


class Scope:
    def __init__(self, key, chunks, mode, shards, lens):
        self.key, self.chunks, self.mode, self.shards, self.lens = key, chunks, mode, shards, lens
        self.n_probes = 0

    @property
    def n_tokens(self):
        return sum(s.ctx_len for s in self.shards)


class ScopeGrepService:
    def __init__(self, model_id=MODEL_ID, exit_layer=EXIT_LAYER, device=None, dtype=None):
        import torch
        from transformers import AutoTokenizer

        from layer_exit_core import load_exit_model

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = dtype or (torch.bfloat16 if self.device == "cuda" else torch.float32)
        t0 = time.time()
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = load_exit_model(model_id, exit_layer=exit_layer, dtype=dtype, device=self.device)
        self.load_seconds = time.time() - t0
        self.booted_at = time.time()
        self.model_id, self.exit_layer = model_id, exit_layer
        self.ShardIndex = _shard_index_class()
        self.scopes = {}        # scope_key -> Scope, LRU order (oldest first)
        self.lock = threading.Lock()
        n = sum(p.numel() for p in self.model.parameters())
        print(f"[scopegrep] loaded {model_id} up to layer {exit_layer}: {n/1e9:.2f}B params "
              f"on {self.device} in {self.load_seconds:.1f}s", flush=True)

    # ------------------------------------------------------------- scope cache

    def _touch(self, key):
        self.scopes[key] = self.scopes.pop(key)

    def _free(self):
        import torch
        if self.device == "cuda":
            torch.cuda.empty_cache()

    def get_scope(self, chunks, mode):
        """Return (scope, how): how is "warm", "refreshed" or "built"."""
        from scope_cache_core import shard_scope

        key = scope_key(chunks, mode)
        if key in self.scopes:
            self._touch(key)
            return self.scopes[key], "warm"

        plan = shard_scope(self.tok, chunks, mode)
        prompts = [self.ShardIndex.build(self.tok, chunks, mode, kept) for kept in plan]
        lens = {}
        for _, spans in prompts:
            lens.update({i: e - s for i, (s, e) in spans.items()})

        # After an edit, reuse the warm shard sharing the longest token prefix
        # with each new shard. Its old scope is dropped: a refreshed shard no
        # longer belongs to it.
        pool = [(sk, sh) for sk, sc in self.scopes.items() if sc.mode == mode for sh in sc.shards]
        reuse, claimed = {}, set()
        for j, (ids, _) in enumerate(prompts):
            best, best_b = None, 0
            for sk, sh in pool:
                if id(sh) in claimed:
                    continue
                b = sh.reusable_from(ids)
                if b > best_b:
                    best, best_b = (sk, sh), b
            if best is not None:
                reuse[j] = best[1]
                claimed.add(id(best[1]))
        for sk in {sk for sk, sh in pool if id(sh) in claimed}:
            self.scopes.pop(sk, None)

        need = sum(len(ids) for j, (ids, _) in enumerate(prompts) if j not in reuse)
        held = sum(sh.ctx_len for sh in reuse.values())
        while self.scopes and sum(s.n_tokens for s in self.scopes.values()) + need + held > MAX_CACHED_TOKENS:
            old = next(iter(self.scopes))
            self.scopes.pop(old)
            print(f"[scopegrep] evicted scope {old}", flush=True)
        self._free()

        shards = []
        for j, kept in enumerate(plan):
            if j in reuse:
                sh = reuse[j]
                sh.refresh(chunks, kept, prompts[j])
            else:
                sh = self.ShardIndex(self.model, self.tok, chunks, mode, kept, prompt=prompts[j])
            shards.append(sh)
        scope = Scope(key, chunks, mode, shards, lens)
        self.scopes[key] = scope
        return scope, ("refreshed" if reuse else "built")

    # ---------------------------------------------------------------- retrieve

    def retrieve(self, body):
        import torch
        from scope_cache_core import ScopeCache, merge_shards
        from two_pass_core import _chunk_token_lengths, _fill_budget

        query = body["query"]
        mode = body.get("mode", "codegen")
        budget = int(body.get("fine_budget", FINE_BUDGET_DEFAULT))
        k = min(int(body.get("k", 20)), MAX_K)
        key, chunks = body.get("scope_key"), body.get("chunks")

        t0 = time.time()
        if chunks is None:
            if not key or key not in self.scopes:
                return {"error": "scope_not_cached", "scope_key": key,
                        "detail": "no warm cache for this scope_key (the service restarted, scaled "
                                  "to zero, or evicted it); resend the request with `chunks`",
                        "cached_scopes": list(self.scopes)}, 409
            scope, how = self.scopes[key], "warm"
            self._touch(key)
        else:
            if len(chunks) > MAX_CHUNKS:
                return {"error": "too_many_chunks", "n_chunks": len(chunks), "max_chunks": MAX_CHUNKS}, 400
            scope, how = self.get_scope(chunks, mode)
        encode_seconds = time.time() - t0
        chunks = scope.chunks

        # Scope Attention: every chunk, at full length, from the cache.
        t1 = time.time()
        per_shard, s1 = [], {}
        for sh in scope.shards:
            s = sh.score(query)
            s1.update(s)
            per_shard.append(sorted(s, key=lambda i: s[i], reverse=True))
        theta = merge_shards(per_shard)
        scope.n_probes += 1
        t_scope = time.time() - t1

        # Fine Attention: scope_cache_core.fine_rank, with the chunk lengths
        # held on the scope instead of re-tokenizing the scope every query.
        t2 = time.time()
        _, overhead = _chunk_token_lengths(self.tok, [], query, mode)
        cand, used = _fill_budget(theta, scope.lens, max(1, budget - overhead))
        fc = ScopeCache(self.model, self.tok, chunks, mode, kept=cand)
        s2 = fc.score(query)
        fine_tokens = fc.ctx_len
        del fc
        cset = set(cand)
        ranking = sorted(cand, key=lambda i: s2.get(i, float("-inf")), reverse=True) + \
            [i for i in theta if i not in cset]
        t_fine = time.time() - t2

        top = [{"index": int(i), "rank": r, "n_tokens": int(scope.lens.get(i, 0)), "reranked": i in cset,
                "score": float(s2[i] if i in s2 else s1.get(i, 0.0))} for r, i in enumerate(ranking[:k])]
        warnings = []
        if len(scope.shards) > 1:
            warnings.append(f"scope is {scope.n_tokens:,} tokens, split into {len(scope.shards)} shards; "
                            "chunks in different shards cannot attend to each other, so a connection "
                            "between them is not read. Narrow the scope if that matters.")
        if budget != FINE_BUDGET_DEFAULT:
            warnings.append(f"fine_budget={budget} is not the benchmarked {FINE_BUDGET_DEFAULT}.")
        if k > len(cand):
            warnings.append(f"k={k} exceeds ranking_valid_to_k={len(cand)}: results past rank {len(cand)} "
                            "are in Scope Attention order, not re-scored by Fine Attention.")
        vram = torch.cuda.max_memory_allocated() / 2**20 if self.device == "cuda" else None
        return {
            "scope_key": scope.key,
            "n_chunks": len(chunks),
            "k": k,
            "top_k": top,
            "ranking": [int(i) for i in ranking],
            "ranking_valid_to_k": len(cand),
            "n_candidates": len(cand),
            "returned_tokens": sum(t["n_tokens"] for t in top),
            "scope_tokens": int(scope.n_tokens),
            "scope_attention": {
                "cache": how,
                "encode_seconds": round(encode_seconds, 3),
                "reencoded_tokens": int(sum(sh.reencoded_tokens for sh in scope.shards)) if how != "warm" else 0,
                "n_shards": len(scope.shards),
                "seconds": round(t_scope, 3),
            },
            "fine_attention": {"n_tokens": int(fine_tokens), "candidate_tokens": int(used),
                               "seconds": round(t_fine, 3)},
            "fine_budget": budget,
            "mode": mode,
            "exit_layer": self.exit_layer,
            "vram_peak_mb": round(vram, 1) if vram is not None else None,
            "warnings": warnings,
        }, 200

    def index(self, chunks, mode):
        t0 = time.time()
        scope, how = self.get_scope(chunks, mode)
        return {
            "scope_key": scope.key,
            "cache": how,
            "n_chunks": len(chunks),
            "n_shards": len(scope.shards),
            "scope_tokens": int(scope.n_tokens),
            "chunk_tokens": {str(i): int(v) for i, v in scope.lens.items()},
            "seconds": round(time.time() - t0, 3),
        }

    def health(self):
        return {
            "ok": True,
            "model": self.model_id,
            "exit_layer": self.exit_layer,
            "device": self.device,
            "model_load_seconds": round(self.load_seconds, 1),
            "container_uptime_seconds": round(time.time() - self.booted_at, 1),
            "max_cached_tokens": MAX_CACHED_TOKENS,
            "cached_scopes": [
                {"scope_key": s.key, "n_chunks": len(s.chunks), "mode": s.mode, "n_shards": len(s.shards),
                 "prefix_tokens": int(s.n_tokens), "n_probes": s.n_probes}
                for s in self.scopes.values()],
            "benchmarked_defaults": {"fine_budget": FINE_BUDGET_DEFAULT, "exit_layer": 20},
        }

    def selftest(self, body):
        """Two exactness checks the cache design depends on, on a synthetic
        scope encoded in small blocks so several block boundaries exist.

        rollback  the same query asked before and after other queries must
                  score identically (the paper: largest difference 0.0). Asked
                  three times because an aliased DeltaNet snapshot is invisible
                  on the first probe and corrupts every one after it.
        refresh   editing one late chunk and re-encoding from the last block
                  boundary must match encoding the edited scope from scratch,
                  up to floating-point differences of the block layout."""
        query = body.get("query", "which passage defines the retry policy?")
        mode = body.get("mode", "short")
        chunks = body.get("chunks") or [
            f"Passage body number {i}. " + ("filler " * 40) +
            ("The retry policy is exponential backoff capped at 30s." if i == 7 else "Nothing notable here.")
            for i in range(40)]
        kept = list(range(len(chunks)))
        SI, block = self.ShardIndex, 256

        ix = SI(self.model, self.tok, chunks, mode, kept, block=block)
        a = ix.score(query)
        ix.score("an unrelated question about logging")
        b = ix.score(query)
        c = ix.score(query)
        keys = sorted(a)
        rollback = max(max(abs(a[i] - b[i]), abs(a[i] - c[i])) for i in keys)

        edited = list(chunks)
        edited[-3] = edited[-3] + " Edited: the retry cap is now 60s."
        prompt = SI.build(self.tok, edited, mode, kept)
        reenc = ix.refresh(edited, kept, prompt)
        warm = ix.score(query)
        cold = SI(self.model, self.tok, edited, mode, kept, block=block).score(query)
        refresh = max(abs(warm[i] - cold[i]) for i in keys)
        rank = lambda s: sorted(keys, key=lambda i: s[i], reverse=True)
        self._free()
        return {
            "n_chunks": len(chunks),
            "scope_tokens": ix.ctx_len,
            "rollback_max_abs_delta": rollback,
            "rollback_exact": rollback == 0.0,
            "refresh_reencoded_tokens": reenc,
            "refresh_max_abs_delta_vs_cold": refresh,
            "refresh_ranking_identical": rank(warm) == rank(cold),
            "top5": rank(a)[:5],
        }


# ------------------------------------------------------------------- HTTP API

def make_api(service, token="", extra_health=None):
    """FastAPI app over `service`. An empty `token` disables authentication
    (local use on a loopback address only; serve.py enforces that)."""
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse

    api = FastAPI(title="scopegrep", docs_url=None, redoc_url=None)

    def check(supplied):
        if not token:
            return
        # constant-time compare so a wrong token leaks nothing by timing
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="bad or missing X-Scopegrep-Token header")

    @api.get("/health")
    def health(x_scopegrep_token: str = Header(default="")):
        check(x_scopegrep_token)
        return {**service.health(), **(extra_health or {})}

    @api.post("/index")
    def index(body: dict, x_scopegrep_token: str = Header(default="")):
        check(x_scopegrep_token)
        chunks = body.get("chunks")
        if not chunks:
            raise HTTPException(status_code=400, detail="`chunks` required")
        if len(chunks) > MAX_CHUNKS:
            raise HTTPException(status_code=400, detail=f"{len(chunks)} chunks > max {MAX_CHUNKS}")
        with service.lock:
            return service.index(chunks, body.get("mode", "codegen"))

    @api.post("/retrieve")
    def retrieve(body: dict, x_scopegrep_token: str = Header(default="")):
        check(x_scopegrep_token)
        if not body.get("query"):
            raise HTTPException(status_code=400, detail="`query` required")
        with service.lock:
            payload, status = service.retrieve(body)
        return payload if status == 200 else JSONResponse(payload, status_code=status)

    @api.post("/selftest")
    def selftest(body: dict, x_scopegrep_token: str = Header(default="")):
        check(x_scopegrep_token)
        with service.lock:
            return service.selftest(body)

    return api
