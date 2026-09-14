#!/usr/bin/env bash
# scopegrep — PreToolUse hook, gates Grep (and grep-shaped Bash calls).
#
# scopegrep-nudge.sh injects an unconditional reminder on every prompt, but
# a reminder is advisory -- the agent can read it and still reach for grep
# anyway. In practice it did, even with that hook installed. This hook makes
# the first search of a session non-optional instead of asking nicely again.
#
# Scope: only the FIRST Grep-shaped call in a session is blocked, and only
# until scopegrep_retrieve (or multi_retrieve) has been tried at least once
# -- tried, not necessarily succeeded, so a down service unblocks search
# after one failed attempt rather than locking the session out of grep for
# good. See scopegrep-mark-used.sh, the PostToolUse hook that lifts this.
set -euo pipefail

payload="$(cat)"
session_id="$(jq -r '.session_id // "unknown"' <<<"$payload")"
tool_name="$(jq -r '.tool_name // ""' <<<"$payload")"
command="$(jq -r '.tool_input.command // ""' <<<"$payload")"

marker="${TMPDIR:-/tmp}/scopegrep-used-${session_id}"
[[ -f "$marker" ]] && exit 0

is_grep_shaped=0
case "$tool_name" in
  Grep) is_grep_shaped=1 ;;
  Bash)
    printf '%s' "$command" \
      | grep -Eq '(^|[|;&(]|[[:space:]])(grep|rg|ag|ack)([[:space:]]|$)' \
      && is_grep_shaped=1 ;;
esac
[[ "$is_grep_shaped" -eq 1 ]] || exit 0

reason='scopegrep has not been tried yet this session. Call scopegrep_retrieve first for this lookup (skills/scopegrep/SKILL.md) -- it returns the ranked code plus every other call site in one round trip, which this search will not. If you already have the exact file and line open, Read it directly instead of retrying this search. This gate fires once per session: after scopegrep_retrieve has been called (or attempted), grep is unblocked for the rest of the session.'

jq -n --arg reason "$reason" \
  '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
