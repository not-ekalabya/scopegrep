"""Early-exit, null-calibrated ScopeGrep scorer.

Same two-pass pipeline as `two_pass_core` (landmark gists -> stage-2 re-rank
under `native_budget`), with two changes to how a chunk is scored, both from
the layer study in decoding/experiments/attention_layers_20260929:

1. The model stops after decoder layer EXIT_LAYER (default 20, 1-indexed; the
   layers after it (21-32) are never built or loaded, nor are the vision
   tower and LM head, which a scorer that never decodes does not use). Layer
   20 is where question->chunk attention localizes gold best; the layers
   after it spread attention back out.
2. The score is layer EXIT_LAYER's head-mean question->chunk attention minus
   the same readout under a content-free question ("N/A", ICR-style), from a
   second question-only pass over the same cached context.

Because the transformer is causal in depth, the truncated model's layer-20
attention is bit-for-bit what the full model computes at layer 20 (checked in
decoding/experiments/exit20_benches_20260929/check_equivalence.py).

`two_pass_core` is untouched: it is a verbatim extraction and must stay that
way. This module reuses its helpers and mirrors landmark_score /
two_pass_score with dense_scores swapped for `exit_scores`.
"""
import torch

from two_pass_core import (
    DEFAULT_GIST_TOKENS, AttentionCapture, _chunk_token_lengths, _fill_budget, _QA_LABELS,
    _restore_linear_state, _snapshot_linear_state, _tok_ids, _chat_wrap, _truncate_full_attn_cache,
    build_prompt, force_attn_implementation, full_attention_layer_indices, gist_chunks,
    linear_attention_layer_indices)

EXIT_LAYER = 20  # 1-indexed; the last decoder layer that is built
NULL_QUESTION = "N/A"


def load_exit_model(model_id="Qwen/Qwen3.5-9B", exit_layer=EXIT_LAYER, dtype=torch.bfloat16, device="cuda"):
    """Text-only Qwen3.5 decoder truncated to its first `exit_layer` layers.
    Weights for later layers, the vision tower and lm_head are never loaded."""
    from transformers import AutoConfig
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5TextModel
    cfg = AutoConfig.from_pretrained(model_id).get_text_config()
    assert cfg.layer_types[exit_layer - 1] == "full_attention", "exit must land on a softmax layer"
    cfg.num_hidden_layers = exit_layer
    cfg.layer_types = list(cfg.layer_types[:exit_layer])
    model = Qwen3_5TextModel.from_pretrained(model_id, config=cfg, dtype=dtype, attn_implementation="sdpa",
                                             key_mapping={r"^model\.language_model\.": ""})
    return model.to(device).eval()


def _q_tail(tokenizer, question, mode):
    _, tail = _chat_wrap(tokenizer)
    q_label, a_label = _QA_LABELS[mode]
    return _tok_ids(tokenizer, f"{q_label}: {question}\n{a_label}:"), _tok_ids(tokenizer, tail)


def _question_pass(model, cache, ids, q_len, spans, q_block):
    """Question rows of the last softmax layer, head-mean, row-mean; blocked."""
    acc, total = {}, 0
    for start in range(0, ids.shape[1], q_block):
        blk = ids[:, start:start + q_block]
        lo, hi = 0, min(blk.shape[1], max(0, q_len - start))
        if hi > lo:
            with force_attn_implementation(model, "eager"), \
                 AttentionCapture(model, spans, q_slice=(lo, hi)) as cap:
                out = model(input_ids=blk, past_key_values=cache, use_cache=True)
            last = cap.get_per_layer_scores()[-1]  # deepest hooked layer == the exit layer
            w = hi - lo
            for k, v in last.items():
                acc[k] = acc.get(k, 0.0) + v * w
            total += w
        else:
            out = model(input_ids=blk, past_key_values=cache, use_cache=True)
        cache = out.past_key_values
    return {k: v / total for k, v in acc.items()} if total else {}, cache


@torch.no_grad()
def exit_scores(model, tokenizer, input_ids, chunk_spans, question_span, mode, calibrate=True, q_block=1024,
                return_raw=False):
    """Drop-in for two_pass_core.dense_scores on a truncated model: the exit
    layer's question->chunk attention, minus its null-question counterpart.
    return_raw=True also returns the uncalibrated readout (free: same pass)."""
    context_len = question_span[0]
    q_len = question_span[1] - question_span[0]
    full_idx = full_attention_layer_indices(model)
    lin_idx = linear_attention_layer_indices(model)
    out_a = model(input_ids=input_ids[:, :context_len], use_cache=True)
    cache = out_a.past_key_values
    del out_a
    snap = _snapshot_linear_state(cache, lin_idx) if calibrate else None
    real, cache = _question_pass(model, cache, input_ids[:, context_len:], q_len, chunk_spans, q_block)
    if not calibrate:
        return (real, real) if return_raw else real
    _truncate_full_attn_cache(cache, full_idx, context_len)
    _restore_linear_state(cache, lin_idx, snap)
    nq, ntail = _q_tail(tokenizer, NULL_QUESTION, mode)
    null_ids = torch.tensor([nq + ntail], device=input_ids.device)
    null, _ = _question_pass(model, cache, null_ids, len(nq), chunk_spans, q_block)
    cal = {k: real[k] - null.get(k, 0.0) for k in real}
    return (cal, real) if return_raw else cal


@torch.no_grad()
def landmark_score(model, tokenizer, chunks, question, mode,
                   gist_tokens=DEFAULT_GIST_TOKENS, gist_mode="head_tail", calibrate=True):
    """two_pass_core.landmark_score with exit_scores. Also returns the
    uncalibrated stage-1 scores ("scores_raw") from the same pass."""
    device = next(model.parameters()).device
    all_idx = list(range(len(chunks)))
    gists, n_truncated = gist_chunks(tokenizer, chunks, gist_tokens, gist_mode)
    ids, spans, q_span = build_prompt(tokenizer, gists, all_idx, question, mode)
    ids = ids.to(device)
    scores, raw = exit_scores(model, tokenizer, ids, spans, q_span, mode, calibrate=calibrate, return_raw=True)
    return {"scores": scores, "scores_raw": raw, "n_tokens": int(ids.shape[1]), "n_truncated": n_truncated,
            "gist_tokens": gist_tokens, "gist_mode": gist_mode}


@torch.no_grad()
def two_pass_score(model, tokenizer, chunks, question, mode, native_budget,
                   gist_tokens=DEFAULT_GIST_TOKENS, gist_mode="head_tail", stage1=None, calibrate=True):
    """two_pass_core.two_pass_score with exit_scores; identical candidate
    selection, budget fill and final ordering."""
    device = next(model.parameters()).device
    all_idx = list(range(len(chunks)))
    chunk_lens, overhead = _chunk_token_lengths(tokenizer, chunks, question, mode)
    cap_total = max(1, native_budget - overhead)
    if stage1 is None:
        stage1 = landmark_score(model, tokenizer, chunks, question, mode, gist_tokens=gist_tokens,
                                gist_mode=gist_mode, calibrate=calibrate)
    s1 = stage1["scores"]
    s1_ranking = sorted(all_idx, key=lambda i: s1.get(i, float("-inf")), reverse=True)
    candidates, used = _fill_budget(s1_ranking, chunk_lens, cap_total)
    cand_sorted = sorted(candidates)
    ids, spans, q_span = build_prompt(tokenizer, chunks, cand_sorted, question, mode)
    ids = ids.to(device)
    s2 = exit_scores(model, tokenizer, ids, spans, q_span, mode, calibrate=calibrate)
    s2_ranking = sorted(cand_sorted, key=lambda i: s2.get(i, float("-inf")), reverse=True)
    cand_set = set(cand_sorted)
    tail = [i for i in s1_ranking if i not in cand_set]
    return {"scores_stage1": s1, "scores_stage2": s2, "ranking": s2_ranking + tail,
            "candidates": cand_sorted, "n_candidates": len(cand_sorted), "candidate_tokens": used,
            "n_tokens": int(ids.shape[1]), "stage1_n_tokens": stage1["n_tokens"]}
