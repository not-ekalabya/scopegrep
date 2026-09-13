#!/usr/bin/env bash
# Force the scoring service to load its model before anybody is waiting on it.
#
# The service scales to zero. /health is served from inside the GPU class, so
# calling it starts a container and runs the 9B model load -- measured at
# 71-117s in the benchmark transcripts. That cost is paid by whoever queries
# first. On your own machine that is an annoyance; in front of a pilot partner
# it is the first thing they see, and it is not what the tool costs in steady
# state.
#
# Run this at the start of a session, or on a timer during a pilot window.
#
#   usage: ./tools/prewarm.sh [--watch SECONDS]
#
# Needs SCOPEGREP_URL and SCOPEGREP_TOKEN, the same two the MCP server reads.
# SCOPEGREP_TOKEN falls back to ~/.config/scopegrep/token.
set -uo pipefail

URL="${SCOPEGREP_URL:-}"
TOKEN="${SCOPEGREP_TOKEN:-}"
[ -z "$TOKEN" ] && [ -r "$HOME/.config/scopegrep/token" ] &&
  TOKEN=$(tr -d '\n' < "$HOME/.config/scopegrep/token")

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "prewarm: set SCOPEGREP_URL and SCOPEGREP_TOKEN (or write the token to" \
       "~/.config/scopegrep/token)" >&2
  exit 2
fi

warm_once() {
  local t0 t1
  t0=$(date +%s)
  # --max-time must exceed a cold model load or this reports a failure for a
  # service that is working exactly as designed.
  body=$(curl -sS -L --max-time 900 -H "X-Scopegrep-Token: $TOKEN" \
               -w '\n%{http_code}' "$URL/health" 2>&1)
  t1=$(date +%s)
  code=$(printf '%s' "$body" | tail -1)
  if [ "$code" != "200" ]; then
    echo "$(date '+%F %T')  prewarm FAILED after $((t1 - t0))s: HTTP ${code:-none}" >&2
    printf '%s\n' "$body" | head -3 >&2
    return 1
  fi
  # Report whether this call paid the load or found it already up, because the
  # two look identical from the exit code and only one of them is the point.
  up=$(printf '%s' "$body" | head -n -1 |
       python3 -c 'import json,sys;h=json.load(sys.stdin);print(
         f"{h[\"container_uptime_seconds\"]:.0f}s up, load {h[\"model_load_seconds\"]}s, "
         f"{len(h[\"cached_scopes\"])} warm scope(s), min_containers="
         f"{h.get(\"min_containers\",\"?\")}")' 2>/dev/null || echo "ok")
  echo "$(date '+%F %T')  warm in $((t1 - t0))s  [$up]"
}

if [ "${1:-}" = "--watch" ]; then
  every="${2:?--watch needs a number of seconds}"
  # Keep it warm without holding a container 24/7: re-touch it inside the
  # scaledown window. Cheaper than min_containers for a fixed demo window,
  # and it goes away when you stop the loop.
  echo "prewarm: touching $URL/health every ${every}s (ctrl-c to stop)"
  while :; do warm_once || true; sleep "$every"; done
else
  warm_once
fi
