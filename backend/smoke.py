"""End-to-end check of a running scopegrep service, without Claude Code.

    export SCOPEGREP_URL=http://127.0.0.1:8000          # or your Modal URL
    export SCOPEGREP_TOKEN=...                          # if the service has one
    python3 backend/smoke.py                 # health + selftest + index + retrieve
    python3 backend/smoke.py --selftest      # just the cache-exactness checks
    python3 backend/smoke.py --scope 'src/**/*.py' --query 'where is retry backoff configured'
"""
import argparse
import os
import sys
import time

import httpx

URL = os.environ.get("SCOPEGREP_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("SCOPEGREP_TOKEN", "")
H = {"X-Scopegrep-Token": TOKEN} if TOKEN else {}
T = 900.0   # must exceed a cold start (model load) or a working service reads as a failure


def call(method, path, body=None):
    t0 = time.time()
    if method == "GET":
        r = httpx.get(f"{URL}{path}", headers=H, timeout=T, follow_redirects=True)
    else:
        r = httpx.post(f"{URL}{path}", json=body or {}, headers=H, timeout=T, follow_redirects=True)
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"  {path} -> HTTP {r.status_code} in {dt:.1f}s\n  {r.text[:600]}")
        return None, dt
    return r.json(), dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scope", default=None, help="glob to chunk (with the client's chunker) and query")
    ap.add_argument("--query", default="which function applies the retry policy?")
    ap.add_argument("--k", type=int, default=20)
    args = ap.parse_args()

    print(f"service: {URL}")
    h, dt = call("GET", "/health")
    if h is None:
        sys.exit(1)
    print(f"  health OK in {dt:.1f}s -- {h['model']} to layer {h['exit_layer']} on "
          f"{h.get('gpu', h['device'])}, up {h['container_uptime_seconds']}s, "
          f"{len(h['cached_scopes'])} warm scope(s)")

    print("\nselftest (cache rollback and edit refresh):")
    s, dt = call("POST", "/selftest", {})
    if s is None:
        sys.exit(1)
    ok = s["rollback_exact"] and s["refresh_ranking_identical"]
    print(f"  {s['n_chunks']} chunks, {s['scope_tokens']} tokens, {dt:.1f}s")
    print(f"  rollback: max|same query, before vs after other queries| = {s['rollback_max_abs_delta']:.3e}")
    print(f"  refresh:  re-encoded {s['refresh_reencoded_tokens']} tokens after an edit; "
          f"max|refreshed - encoded from scratch| = {s['refresh_max_abs_delta_vs_cold']:.3e}, "
          f"ranking identical {s['refresh_ranking_identical']}")
    print(f"  => cache is {'EXACT' if ok else 'NOT EXACT -- do not use'}")
    if args.selftest:
        sys.exit(0 if ok else 1)

    if args.scope:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
        os.environ.setdefault("SCOPEGREP_ROOT", os.getcwd())
        from scopegrep import server as G
        chunks, meta, note, coverage = G.build_chunks(
            [args.scope], G.DEFAULT_EXCLUDES, "window", G.CHUNK_CHARS, os.getcwd())
        print(f"\nlocal scope: {len(chunks)} chunks from {args.scope}" + (f"  ({note})" if note else ""))
    else:
        chunks = [f"# File: mod/f{i}.py\ndef f{i}():\n    " +
                  ("return retry(backoff='exponential', cap_seconds=30)" if i == 7 else f"return {i} * 2")
                  for i in range(60)]
        meta = [{"path": f"mod/f{i}.py", "start_line": 1, "end_line": 2} for i in range(60)]
        print(f"\nsynthetic scope: {len(chunks)} chunks (gold is mod/f7.py)")

    ix, dt = call("POST", "/index", {"chunks": chunks, "mode": "codegen"})
    if ix is None:
        sys.exit(1)
    print(f"  indexed in {dt:.1f}s: scope_key={ix['scope_key']} {ix['scope_tokens']:,} tokens, "
          f"{ix['n_shards']} shard(s), cache {ix['cache']}")

    for label, body in [
        ("chunks sent", {"chunks": chunks, "query": args.query, "k": args.k, "mode": "codegen"}),
        ("scope_key only", {"scope_key": ix["scope_key"], "query": args.query, "k": args.k, "mode": "codegen"}),
    ]:
        res, dt = call("POST", "/retrieve", body)
        if res is None:
            continue
        sa, fa = res["scope_attention"], res["fine_attention"]
        print(f"\n{label}: {dt:.1f}s wall, cache {sa['cache']}, Scope Attention {sa['seconds']}s, "
              f"Fine Attention {fa['seconds']}s over {res['ranking_valid_to_k']} chunks")
        print(f"  returned {res['returned_tokens']:,} / {res['scope_tokens']:,} tokens, "
              f"vram peak {res['vram_peak_mb']} MB")
        for t in res["top_k"][:5]:
            m = meta[t["index"]]
            print(f"    #{t['rank']+1} {m['path']}:{m['start_line']}-{m['end_line']} "
                  f"score={t['score']:.4f} {'fine' if t['reranked'] else 'scope'}")
        for w in res["warnings"]:
            print(f"  WARNING: {w}")


if __name__ == "__main__":
    main()
