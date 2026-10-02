"""VERBATIM extraction of the scoring path benchmarked in
`decoding/longrange_score.py` (arm `two_pass_g32`), plus the pipeline helpers
it depends on. Every function body below is copied byte-for-byte from the
research repo by an AST extractor -- not retyped, not "cleaned up" -- because
the recall numbers this service advertises were measured on exactly this code
and a paraphrase would silently invalidate them.

Provenance:
  decoding/pipeline/core.py         force_attn_implementation,
                                    find_full_attention_targets,
                                    _span_pool_matrix, AttentionCapture,
                                    _full_attn_cache_len,
                                    _truncate_full_attn_cache,
                                    _snapshot_linear_state,
                                    _restore_linear_state,
                                    full_attention_layer_indices,
                                    linear_attention_layer_indices
  decoding/pipeline/prompt.py       whole module
  decoding/pipeline/scaled_score.py _chunk_token_lengths
  decoding/longrange_score.py       dense_scores, gist_chunks, landmark_score,
                                    _fill_budget, two_pass_score

Do not edit the extracted region by hand. Re-run tools/extract_scorer.py
against the research repo instead.
"""
import contextlib

import torch

DEFAULT_GIST_TOKENS = 32

# ---------------------------------------------------------------------- pipeline/prompt.py
SHORT_INSTRUCTION = (
    "Answer the question using only the information in the numbered passages "
    "below. Respond with just the final answer phrase, nothing else.\n\nPassages:\n"
)

LONG_INSTRUCTION = (
    "Answer the question using only the information in the numbered passages "
    "below. Think through the relevant passages and your reasoning step by "
    "step, citing passage numbers where relevant, then end your response "
    "with a final line in the exact form 'Final answer: <answer>'.\n\nPassages:\n"
)

SUMMARY_INSTRUCTION = (
    "Write a concise, coherent summary synthesizing the key information "
    "across the numbered source documents below.\n\nPassages:\n"
)

CODEGEN_INSTRUCTION = (
    "The numbered passages below are source files from a code library. "
    "Using the APIs and patterns defined in these files, complete the "
    "task below.\n\nPassages:\n"
)

MULTI_INSTRUCTION = (
    "Answer each of the following numbered questions using only the "
    "information in the numbered passages below. For each question, briefly "
    "cite the relevant passage number(s) and your reasoning, then give the "
    "answer in the exact form 'N) Answer: <answer>' on its own line, one "
    "such line per question.\n\nPassages:\n"
)

_INSTRUCTIONS = {
    "short": SHORT_INSTRUCTION,
    "long_form": LONG_INSTRUCTION,
    "summary": SUMMARY_INSTRUCTION,
    "codegen": CODEGEN_INSTRUCTION,
    "multi": MULTI_INSTRUCTION,
}

_QA_LABELS = {
    "short": ("Question", "Answer"),
    "long_form": ("Question", "Answer"),
    "summary": ("Task", "Summary"),
    "codegen": ("Task", "Code"),
    "multi": ("Questions", "Response"),
}


def _tok_ids(tokenizer, text):
    return tokenizer(text, add_special_tokens=False)["input_ids"]


_CHAT_HEAD = None
_CHAT_TAIL = None


def _chat_wrap(tokenizer):
    global _CHAT_HEAD, _CHAT_TAIL
    if _CHAT_HEAD is None:
        sentinel = "<<<CONTENT>>>"
        templated = tokenizer.apply_chat_template(
            [{"role": "user", "content": sentinel}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        head, tail = templated.split(sentinel)
        _CHAT_HEAD, _CHAT_TAIL = head, tail
    return _CHAT_HEAD, _CHAT_TAIL


def build_prompt(tokenizer, chunks, kept_indices, question, mode="short"):
    """Build tokenized prompt containing only `kept_indices` chunks (in original
    order), returning input_ids plus token spans for each kept chunk and for
    the question, measured in the final concatenated sequence."""
    head, tail = _chat_wrap(tokenizer)
    instruction = _INSTRUCTIONS[mode]

    ids = list(_tok_ids(tokenizer, head))
    ids += _tok_ids(tokenizer, instruction)

    chunk_spans = {}
    for idx in kept_indices:
        piece = f"[Passage {idx}] {chunks[idx]}\n\n"
        piece_ids = _tok_ids(tokenizer, piece)
        start = len(ids)
        ids += piece_ids
        chunk_spans[idx] = (start, len(ids))

    q_label, a_label = _QA_LABELS[mode]
    q_piece = f"{q_label}: {question}\n{a_label}:"
    q_ids = _tok_ids(tokenizer, q_piece)
    q_start = len(ids)
    ids += q_ids
    question_span = (q_start, len(ids))

    ids += _tok_ids(tokenizer, tail)

    input_ids = torch.tensor([ids], dtype=torch.long)
    return input_ids, chunk_spans, question_span


# ---------------------------------------------------------------------- pipeline/core.py

@contextlib.contextmanager
def force_attn_implementation(model, implementation):
    """Temporarily override the model's attention kernel for one forward
    call. AttentionCapture needs materialized attn_weights, which fast
    kernels (sdpa/flash) never return -- eager is the only implementation
    that hands them back. PretrainedConfig's `_attn_implementation` setter
    cascades the value into every decoder layer's (shared) sub-config, so
    flipping it on `model.config` affects all layers immediately and
    restoring it afterward is exact."""
    prev = model.config._attn_implementation
    model.config._attn_implementation = implementation
    try:
        yield
    finally:
        model.config._attn_implementation = prev


def find_full_attention_targets(model):
    """Locate decoder-layer self-attention submodules that compute standard
    softmax attention (i.e. `forward` returns (attn_output, attn_weights)).
    Hybrid architectures (e.g. Qwen3.5's linear/full-attention mix) only
    expose a `self_attn` submodule on their full-attention layers; dense
    transformers expose it on every layer. Either way, hooking it directly
    sidesteps needing `output_attentions` plumbing, which some newer hybrid
    modeling code doesn't wire through at all."""
    targets = []
    for name, module in model.named_modules():
        if hasattr(module, "self_attn") and hasattr(module.self_attn, "forward"):
            layer_type = getattr(module, "layer_type", "full_attention")
            if layer_type == "full_attention":
                targets.append((name, module.self_attn))
    if not targets:
        raise RuntimeError("no full-attention self_attn submodules found on model")
    return targets


def _span_pool_matrix(items, length, device):
    """(length, n_items) matrix whose column j is a uniform-average indicator
    over [start_j, end_j) -- (row_vec/matrix) @ this produces every span's
    exact mean in one matmul. Replaces AttentionCapture's old per-span
    slice+mean().item() Python loop (one CPU/GPU op-dispatch per chunk, or
    per (row_chunk, col_chunk) pair for the affinity branch) with a single
    vectorized reduction plus one sync at the very end."""
    starts = torch.tensor([s for _, s, _ in items], device=device, dtype=torch.long)
    ends = torch.tensor([e for _, _, e in items], device=device, dtype=torch.long)
    positions = torch.arange(length, device=device).unsqueeze(1)  # (length, 1)
    mask = (positions >= starts.unsqueeze(0)) & (positions < ends.unsqueeze(0))  # (length, n_items)
    lengths = (ends - starts).float().unsqueeze(0)
    return mask.float() / lengths


class AttentionCapture:
    """Registers forward hooks on the given attention submodules and, for each
    call, reduces the returned attn_weights (batch=1, heads, q_len, kv_len)
    down to a per-key-position importance vector averaged over heads, the
    requested query row slice, and all hooked layers. Chunk-level scores are
    computed immediately inside the hook so the O(seq^2) attention tensors
    are never retained past the single layer that produced them.

    `row_chunk_spans` is a second, independent reduction over the SAME
    already-materialized attn_weights tensor: instead of pooling one query
    row-range down to a single per-key-chunk score vector (what q_slice
    does -- query-to-chunk relevance), it pools MULTIPLE row-ranges (one per
    chunk_idx, in the current forward call's own row coordinates) each down
    to their own per-key-chunk vector, producing a chunk-to-chunk affinity
    matrix. This is not extra compute -- attn_weights already covers every
    row for whatever q_len the current forward call has; q_slice-only usage
    just discards every row outside the question. Only meaningful when the
    forward call's own input tokens ARE real chunk content (e.g. a segment's
    own prefill), not just a question-tail probe -- see
    pipeline/residual_score.py."""

    def __init__(self, model, chunk_spans, q_slice=None, row_chunk_spans=None):
        self.targets = find_full_attention_targets(model)
        self.chunk_spans = chunk_spans
        self.q_slice = q_slice  # (start, end) in the *current forward call's* input, or None = all rows
        self.row_chunk_spans = row_chunk_spans  # {chunk_idx: (start, end)} in the current forward's own rows, or None
        self._handles = []
        self._layer_scores = []  # list of {chunk_idx: score} per layer
        self._layer_affinity = []  # list of {row_chunk_idx: {col_chunk_idx: score}} per layer

    def _hook(self, module, inputs, output):
        if not (isinstance(output, tuple) and len(output) >= 2 and output[1] is not None):
            return
        attn_weights = output[1]  # (1, heads, q_len, kv_len)
        with torch.no_grad():
            w = attn_weights.detach()
            device = w.device
            if self.row_chunk_spans is not None:
                pooled = w.mean(dim=(0, 1)).float()  # (q_len, kv_len), heads/batch averaged, rows kept
                q_len, kv_len = pooled.shape
                row_items = [(r, rs, re) for r, (rs, re) in self.row_chunk_spans.items() if re <= q_len]
                col_items = [(c, cs, ce) for c, (cs, ce) in self.chunk_spans.items() if ce <= kv_len]
                if row_items and col_items:
                    row_idx = [r for r, _, _ in row_items]
                    col_idx = [c for c, _, _ in col_items]
                    row_pool = _span_pool_matrix(row_items, q_len, device)   # (q_len, n_row)
                    col_pool = _span_pool_matrix(col_items, kv_len, device)  # (kv_len, n_col)
                    # two matmuls (row-mean then col-mean, same as the original
                    # per-pair block.mean()) replace what used to be one
                    # slice+mean().item() call per (row_chunk, col_chunk) pair --
                    # single .cpu() sync for the whole layer instead of one per pair
                    block = (row_pool.t() @ pooled @ col_pool).cpu().tolist()
                    affinity = {row_idx[i]: dict(zip(col_idx, block[i])) for i in range(len(row_idx))}
                else:
                    affinity = {r: {} for r, _, _ in row_items}
                self._layer_affinity.append(affinity)
            if self.q_slice is not None:
                s, e = self.q_slice
                key_importance = w[:, :, s:e, :].mean(dim=(0, 1, 2)).float()  # (kv_len,)
                kv_len = key_importance.shape[0]
                col_items = [(c, cs, ce) for c, (cs, ce) in self.chunk_spans.items() if ce <= kv_len]
                if col_items:
                    col_idx = [c for c, _, _ in col_items]
                    col_pool = _span_pool_matrix(col_items, kv_len, device)  # (kv_len, n_col)
                    vals = (key_importance @ col_pool).cpu().tolist()
                    scores = dict(zip(col_idx, vals))
                else:
                    scores = {}
                self._layer_scores.append(scores)

    def __enter__(self):
        for _, module in self.targets:
            self._handles.append(module.register_forward_hook(self._hook))
        return self

    def __exit__(self, *exc):
        for h in self._handles:
            h.remove()

    def get_scores(self):
        """Average per-chunk score across all hooked layers that fired."""
        if not self._layer_scores:
            return {}
        keys = self._layer_scores[0].keys()
        return {
            idx: sum(layer[idx] for layer in self._layer_scores) / len(self._layer_scores)
            for idx in keys
        }

    def get_affinity(self):
        """Average per (row_chunk, col_chunk) affinity across all hooked
        layers that fired -- same layer-averaging convention as get_scores().
        Only populated when row_chunk_spans was given to __init__."""
        if not self._layer_affinity:
            return {}
        row_keys = self._layer_affinity[0].keys()
        out = {}
        for r in row_keys:
            col_keys = self._layer_affinity[0][r].keys()
            out[r] = {
                c: sum(layer[r][c] for layer in self._layer_affinity) / len(self._layer_affinity)
                for c in col_keys
            }
        return out

    def get_per_layer_scores(self):
        """Per-layer chunk scores, one dict per hooked layer that fired, in
        hook-firing order (== layer depth order, since forward hooks fire in
        execution order). Unlike get_scores(), doesn't average layers away --
        used to test whether an early full-attention layer alone predicts
        chunk importance nearly as well as the full pooled average, which
        would justify scoring from a truncated (early-exit) forward pass
        instead of the full model depth."""
        return list(self._layer_scores)


def full_attention_layer_indices(model):
    """Decoder-layer indices (matching past_key_values.layers[i]) that use
    standard softmax attention, derived from the same target list
    find_full_attention_targets already validates -- avoids a second,
    independent guess at the config path for hybrid architectures."""
    indices = []
    for name, _ in find_full_attention_targets(model):
        parts = name.split(".")
        indices.append(int(parts[parts.index("layers") + 1]))
    return indices


def linear_attention_layer_indices(model):
    """Decoder-layer indices running GatedDeltaNet recurrent state instead
    of softmax attention -- the complement of full_attention_layer_indices
    within range(num_hidden_layers). Needed to snapshot/restore recurrent
    state around speculative-decode rollback (see evicted_generate)."""
    full = set(full_attention_layer_indices(model))
    n_layers = model.config.get_text_config().num_hidden_layers
    return [i for i in range(n_layers) if i not in full]


def _full_attn_cache_len(cache, full_attn_layer_indices):
    return cache.layers[full_attn_layer_indices[0]].keys.shape[-2]


def _truncate_full_attn_cache(cache, full_attn_layer_indices, keep_len):
    """Drop trailing KV entries back to `keep_len` positions. Exact, not an
    approximation: full-attention is causal, so a kept position's K/V never
    depended on the tokens being dropped (they came after it), and this is
    identical to that forward call never having seen them."""
    for i in full_attn_layer_indices:
        layer = cache.layers[i]
        layer.keys = layer.keys[..., :keep_len, :]
        layer.values = layer.values[..., :keep_len, :]


def _snapshot_linear_state(cache, linear_layer_indices):
    return {
        i: (cache.layers[i].conv_states.clone(), cache.layers[i].recurrent_states.clone())
        for i in linear_layer_indices
    }


def _restore_linear_state(cache, linear_layer_indices, snapshot):
    for i, (conv, rec) in snapshot.items():
        cache.layers[i].conv_states = conv
        cache.layers[i].recurrent_states = rec



# ---------------------------------------------------------------------- pipeline/scaled_score.py

def _chunk_token_lengths(tokenizer, chunks, question, mode="short"):
    """Token length of every chunk plus the fixed prompt overhead (chat
    wrap + instruction + question + tail), via one cheap tokenizer-only
    `build_prompt` call -- no forward pass."""
    all_idx = list(range(len(chunks)))
    ids, spans, _ = build_prompt(tokenizer, chunks, all_idx, question, mode)
    chunk_lens = {i: e - s for i, (s, e) in spans.items()}
    overhead = ids.shape[1] - sum(chunk_lens.values())
    return chunk_lens, overhead



# ---------------------------------------------------------------------- longrange_score.py

@torch.no_grad()
def dense_scores(model, input_ids, chunk_spans, question_span, q_block=1024):
    """prefill_and_score_fast's two-phase schedule (Phase A: context on the
    fast kernel; Phase B: question + chat tail only, eager, so the attention
    matrix is (heads, q_len, N) not (heads, N, N)), with logits_to_keep=1 on
    both phases. Returns {chunk_idx: score} -- the pooled 8-layer, all-head
    question-attention score, same rule as AttentionCapture.get_scores().

    Phase B runs in blocks of `q_block` query rows. Eager attention
    materializes heads x q_len x kv_len, which is fine for a 20-token
    question and fatal for a long one: a 10k-token query over 22k of context
    OOM'd a 40GB card (6.34 GiB allocation on top of 33 GiB), and that is the
    regime a real coding-agent query lives in. Blocking caps the buffer at
    heads x q_block x kv_len regardless of query length.

    The result is the same number, not an approximation: the score is a mean
    over question rows, so accumulating each block's mean weighted by its row
    count and dividing by the total recovers the global mean exactly.
    Asserted against the unblocked path in local_validate_longrange.py.
    """
    context_len = question_span[0]
    q_len = question_span[1] - question_span[0]

    out_a = model(input_ids=input_ids[:, :context_len], use_cache=True, logits_to_keep=1)
    cache = out_a.past_key_values
    del out_a

    tail = input_ids[:, context_len:]
    acc, total = {}, 0
    for start in range(0, tail.shape[1], q_block):
        blk = tail[:, start:start + q_block]
        # rows of this block that fall inside the question span (the chat tail
        # that follows it is fed for cache correctness but never scored, same
        # as the unblocked path's q_slice=(0, q_len))
        lo, hi = 0, min(blk.shape[1], max(0, q_len - start))
        if hi > lo:
            with force_attn_implementation(model, "eager"), \
                 AttentionCapture(model, chunk_spans, q_slice=(lo, hi)) as cap:
                out_b = model(input_ids=blk, past_key_values=cache, use_cache=True,
                              logits_to_keep=1)
            w = hi - lo
            for k, v in cap.get_scores().items():
                acc[k] = acc.get(k, 0.0) + v * w
            total += w
        else:
            out_b = model(input_ids=blk, past_key_values=cache, use_cache=True,
                          logits_to_keep=1)
        cache = out_b.past_key_values
        del out_b

    return {k: v / total for k, v in acc.items()} if total else {}


def gist_chunks(tokenizer, chunks, gist_tokens=DEFAULT_GIST_TOKENS, gist_mode="head_tail"):
    """Replace each chunk with a fixed-size excerpt of its own tokens.

    `head` keeps the first `gist_tokens`; `head_tail` splits the budget
    between the opening and the closing tokens, joined by an ellipsis. The
    second exists because a bridging fact is as likely to sit at the end of
    a passage as at the start, and the head-only gist would systematically
    never see it.

    Chunks already shorter than the budget are passed through untouched, so
    on a document of short chunks this degenerates to the original text and
    landmark_score degenerates to dense_full_score -- the same "collapses to
    the existing path" property the rest of this repo's modules keep.

    Decode-then-retokenize is not exactly the original token sequence (BPE
    can merge across the cut), which is fine: the gist is a summary object,
    not a claim about token identity. What matters is that every chunk gets
    the SAME budget, so the pooled-mean score is not length-biased -- a real
    confound in the full-resolution path, where a chunk's score is a mean
    over its own length."""
    out = []
    n_truncated = 0
    for c in chunks:
        ids = tokenizer(c, add_special_tokens=False)["input_ids"]
        if len(ids) <= gist_tokens:
            out.append(c)
            continue
        n_truncated += 1
        if gist_mode == "head":
            out.append(tokenizer.decode(ids[:gist_tokens]))
        elif gist_mode == "head_tail":
            h = gist_tokens // 2
            t = gist_tokens - h
            out.append(tokenizer.decode(ids[:h]) + " ... " + tokenizer.decode(ids[-t:]))
        else:
            raise ValueError(f"unknown gist_mode {gist_mode!r}")
    return out, n_truncated


@torch.no_grad()
def landmark_score(model, tokenizer, chunks, question, mode,
                   gist_tokens=DEFAULT_GIST_TOKENS, gist_mode="head_tail"):
    """§5 arm A. One forward, one softmax, every chunk in it."""
    device = next(model.parameters()).device
    all_idx = list(range(len(chunks)))
    gists, n_truncated = gist_chunks(tokenizer, chunks, gist_tokens, gist_mode)
    ids, spans, q_span = build_prompt(tokenizer, gists, all_idx, question, mode)
    ids = ids.to(device)
    scores = dense_scores(model, ids, spans, q_span)
    return {
        "scores": scores,
        "n_tokens": int(ids.shape[1]),
        "n_truncated": n_truncated,
        "gist_tokens": gist_tokens,
        "gist_mode": gist_mode,
    }


def _fill_budget(ranking, chunk_lens, cap_total):
    """Walk `ranking` (best first) taking chunks while they fit. Always
    takes at least one, same guarantee _trim_to_budget gives."""
    out, used = [], 0
    for i in ranking:
        length = chunk_lens[i]
        if not out or used + length <= cap_total:
            out.append(i)
            used += length
    return out, used


@torch.no_grad()
def two_pass_score(model, tokenizer, chunks, question, mode, native_budget,
                   gist_tokens=DEFAULT_GIST_TOKENS, gist_mode="head_tail",
                   stage1=None):
    """§5 arm B/I. Stage 1 ranks globally (landmark, cheap); stage 2 re-ranks
    the survivors that fit `native_budget` at full token resolution under one
    shared softmax.

    Final ranking: stage-2 order first, then everything stage 2 never saw, in
    stage-1 order. So the arm can only reorder within the candidate set -- its
    recall at any k >= the candidate count is stage 1's, by construction, and
    the ceiling this arm can ever reach is stage 1's recall at the candidate
    count. That is stated rather than hidden because it is the number that
    decides whether stage 2 is worth its cost.

    `stage1` lets the caller pass an already-computed landmark_score result
    so the bench doesn't pay for the same forward twice; stage-1 wall time is
    then reported by the caller, not here."""
    device = next(model.parameters()).device
    all_idx = list(range(len(chunks)))
    chunk_lens, overhead = _chunk_token_lengths(tokenizer, chunks, question, mode)
    cap_total = max(1, native_budget - overhead)

    if stage1 is None:
        stage1 = landmark_score(model, tokenizer, chunks, question, mode,
                                gist_tokens=gist_tokens, gist_mode=gist_mode)
    s1 = stage1["scores"]
    s1_ranking = sorted(all_idx, key=lambda i: s1.get(i, 0.0), reverse=True)

    candidates, used = _fill_budget(s1_ranking, chunk_lens, cap_total)
    cand_sorted = sorted(candidates)  # original document order for the prompt

    ids, spans, q_span = build_prompt(tokenizer, chunks, cand_sorted, question, mode)
    ids = ids.to(device)
    s2 = dense_scores(model, ids, spans, q_span)

    s2_ranking = sorted(cand_sorted, key=lambda i: s2.get(i, 0.0), reverse=True)
    cand_set = set(cand_sorted)
    tail = [i for i in s1_ranking if i not in cand_set]

    return {
        "scores_stage1": s1,
        "scores_stage2": s2,
        "ranking": s2_ranking + tail,
        "candidates": cand_sorted,
        "n_candidates": len(cand_sorted),
        "candidate_tokens": used,
        "n_tokens": int(ids.shape[1]),
        "stage1_n_tokens": stage1["n_tokens"],
    }
