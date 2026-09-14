#!/usr/bin/env bash
# scopegrep — PostToolUse hook, lifts the grep gate (scopegrep-gate.sh) once
# scopegrep_retrieve or scopegrep_multi_retrieve has been tried this session.
#
# Fires whenever the tool call completes, success or error -- a failed call
# (service down, bad token, cold-start timeout) still counts as "tried," so
# a scopegrep outage degrades to plain grep instead of locking the session
# out of search entirely.
set -euo pipefail

payload="$(cat)"
session_id="$(jq -r '.session_id // "unknown"' <<<"$payload")"

mkdir -p "${TMPDIR:-/tmp}"
touch "${TMPDIR:-/tmp}/scopegrep-used-${session_id}"
exit 0
