"""Full-text ScopeGrep over a cached scope (no excerpts).

`layer_exit_core` scores a scope in two passes because it prefills every
prompt in one forward call, whose activations run out of memory at a few
hundred thousand tokens; the Coarse pass shrinks each chunk to 32 tokens to
fit. Measured on 300-chunk code pools (decoding/experiments/
excerpt_validity_20260930), the excerpts lose ~9% of gold chunks and rely on
names, while one full-text pass does neither.

This module keeps full text and fixes the memory instead:

1. The scope (chat head, instruction, every chunk at full length) does not
   depend on the question, so it is prefilled ONCE, in blocks of `block`
   tokens, into a KV/DeltaNet cache. Peak memory is the cache (5 softmax
   layers x 4 KV heads x 256 dims x K,V x bf16 = 20 KB/token) plus one
   block's activations, not the whole prompt's.
2. Each query runs only its question tokens over that cache, reads layer
   20's question->chunk attention, then rolls the cache back (truncate the
   softmax KV, restore the DeltaNet state) so the next query sees the
   scope unchanged.
3. The "N/A" readout is query-independent, so it is computed once per
   scope and subtracted from every query's readout.

Scores equal layer_exit_core.exit_scores on the full-text prompt up to bf16
numerics of blocked vs one-shot prefill.
"""
import time

import torch

from layer_exit_core import NULL_QUESTION, _q_tail, _question_pass
from two_pass_core import (
    AttentionCapture, _chunk_token_lengths, _fill_budget, _restore_linear_state, _snapshot_linear_state,
    _truncate_full_attn_cache, build_prompt, force_attn_implementation, full_attention_layer_indices,
    linear_attention_layer_indices)

MAX_POSITIONS = 262_144  # Qwen3.5 max_position_embeddings
SHARD_TOKENS = 250_000   # scopes larger than this are split into contiguous shards


def _question_pass_all_layers(model, cache, ids, q_len, spans, q_block):
    """_question_pass, but reading the mean over EVERY hooked softmax layer
    (the uniform-depth readout) instead of only the deepest one."""
    acc, total = {}, 0
    for start in range(0, ids.shape[1], q_block):
        blk = ids[:, start:start + q_block]
        lo, hi = 0, min(blk.shape[1], max(0, q_len - start))
        if hi > lo:
            with force_attn_implementation(model, "eager"), \
                 AttentionCapture(model, spans, q_slice=(lo, hi)) as cap:
                out = model(input_ids=blk, past_key_values=cache, use_cache=True)
            w = hi - lo
            for k, v in cap.get_scores().items():
                acc[k] = acc.get(k, 0.0) + v * w
            total += w
        else:
            out = model(input_ids=blk, past_key_values=cache, use_cache=True)
        cache = out.past_key_values
    return {k: v / total for k, v in acc.items()} if total else {}, cache


class ScopeCache:
    @torch.no_grad()
    def __init__(self, model, tokenizer, chunks, mode, block=4096, q_block=1024, kept=None, readout="last"):
        """kept: chunk indices to include (default all), in scope order.
        readout: "last" = the deepest built softmax layer (ScopeGrep: layer 20
        of the truncated model); "all" = mean over every softmax layer
        (uniform depth, for the ablation)."""
        self.model, self.tok, self.mode, self.q_block = model, tokenizer, mode, q_block
        self._qpass = _question_pass if readout == "last" else _question_pass_all_layers
        device = next(model.parameters()).device
        kept = list(range(len(chunks))) if kept is None else sorted(kept)
        ids, self.spans, q_span = build_prompt(tokenizer, chunks, kept, "", mode)
        self.ctx_len = q_span[0]
        if self.ctx_len >= MAX_POSITIONS:
            raise ValueError(f"scope is {self.ctx_len} tokens, over the model's {MAX_POSITIONS} positions")
        ctx = ids[:, :self.ctx_len].to(device)
        self.full_idx = full_attention_layer_indices(model)
        self.lin_idx = linear_attention_layer_indices(model)
        t = time.time()
        cache = None
        for start in range(0, self.ctx_len, block):
            out = model(input_ids=ctx[:, start:start + block], past_key_values=cache, use_cache=True)
            cache = out.past_key_values
            del out
        torch.cuda.synchronize()
        self.encode_seconds = time.time() - t
        self.cache = cache
        self.snap = _snapshot_linear_state(cache, self.lin_idx)
        self.null = self._readout(NULL_QUESTION)

    def _readout(self, question):
        q, tail = _q_tail(self.tok, question, self.mode)
        device = next(self.model.parameters()).device
        ids = torch.tensor([q + tail], device=device)
        scores, cache = self._qpass(self.model, self.cache, ids, len(q), self.spans, self.q_block)
        _truncate_full_attn_cache(cache, self.full_idx, self.ctx_len)
        # clones: the snapshot must survive in-place state updates by later queries
        _restore_linear_state(cache, self.lin_idx, {i: (c.clone(), r.clone()) for i, (c, r) in self.snap.items()})
        self.cache = cache
        return scores

    @torch.no_grad()
    def score(self, question):
        """Layer-20 question->chunk attention minus the scope's N/A readout."""
        real = self._readout(question)
        return {k: real[k] - self.null.get(k, 0.0) for k in real}

    @torch.no_grad()
    def score_both(self, question):
        """(calibrated, raw) from one question pass."""
        real = self._readout(question)
        return {k: real[k] - self.null.get(k, 0.0) for k in real}, real

    def rank(self, question):
        s = self.score(question)
        return sorted(s, key=lambda i: s[i], reverse=True)


def shard_scope(tokenizer, chunks, mode, max_tokens=SHARD_TOKENS):
    """Contiguous shards of chunk indices of near-equal token size, as few as
    keep each shard's prompt within max_tokens (one shard when the scope fits).
    Equal sizes keep rank-interleaved merging from favouring a small shard."""
    import math
    ids, spans, q = build_prompt(tokenizer, chunks, list(range(len(chunks))), "", mode)
    if q[0] <= max_tokens:
        return [list(range(len(chunks)))]
    overhead = q[0] - sum(e - s for s, e in spans.values())
    body = q[0] - overhead
    k = math.ceil(body / (max_tokens - overhead))
    while True:
        target = body / k
        shards, cur, used = [], [], 0
        for i in range(len(chunks)):
            n = spans[i][1] - spans[i][0]
            if cur and used + n > target and len(shards) < k - 1:
                shards.append(cur); cur, used = [], 0
            cur.append(i); used += n
        shards.append(cur)
        sizes = [sum(spans[i][1] - spans[i][0] for i in sh) + overhead for sh in shards]
        if max(sizes) <= max_tokens:
            return shards
        k += 1


def merge_shards(rankings):
    """Interleave per-shard rankings by rank (rank 1 of every shard, then rank 2, ...)."""
    out, k = [], 0
    while any(k < len(r) for r in rankings):
        out += [r[k] for r in rankings if k < len(r)]
        k += 1
    return out


def fine_rank(model, tokenizer, chunks, question, mode, theta, budget=8000, calibrate=True, readout="last"):
    """Fine Attention: admit chunks along theta while their full length fits the
    budget, re-score the admitted set in a fresh prompt, then the rest in theta order."""
    lens, over = _chunk_token_lengths(tokenizer, chunks, question, mode)
    cand, _ = _fill_budget(theta, lens, max(1, budget - over))
    fc = ScopeCache(model, tokenizer, chunks, mode, kept=cand, readout=readout)
    s = fc.score_both(question)[0 if calibrate else 1]
    del fc
    cset = set(cand)
    return sorted(cand, key=lambda i: s.get(i, float("-inf")), reverse=True) + [i for i in theta if i not in cset]
