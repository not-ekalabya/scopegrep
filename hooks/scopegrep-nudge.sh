#!/usr/bin/env bash
# scopegrep — UserPromptSubmit hook.
#
# Fires on every prompt, unconditionally, and injects a reminder that
# scopegrep exists plus the skill's own decision rule. An earlier version
# only fired on symptom/behavior-shaped prompts with no code literal in
# them -- correct in principle, but it meant the model had to notice the
# skill and reach for the tool on its own the rest of the time, and mostly
# didn't. Unconditional injection costs a few hundred tokens of context per
# prompt; it does not call the service, so it costs nothing in latency or
# billed retrieval on prompts that never end up calling scopegrep_retrieve.
set -euo pipefail

cat >/dev/null   # drain the hook payload on stdin; nothing in it is used

read -r -d '' context <<'EOF' || true
scopegrep is available this session (skill: skills/scopegrep/SKILL.md). It
finds code by describing what it does, not by matching keywords, and its
response includes every other call site of what it returns.

- Have a literal to grep (symbol, path, error string, filename)? Use
  grep/Read directly -- faster and exact, don't reach for scopegrep first.
- Can describe a behavior or symptom but not its location, or grep came back
  empty or too broad to read through? scopegrep_retrieve(query=<the full
  question or symptom text>, include=[<one subsystem>], budget_tokens=3000).
- About to change a function? Its outbound-call block lists every other call
  site in scope -- a symbol with call sites you haven't read is a change you
  haven't finished deciding.
- Tools may be deferred; load with ToolSearch({query: "select:mcp__plugin_scopegrep_scopegrep__scopegrep_retrieve,mcp__plugin_scopegrep_scopegrep__scopegrep_scope,mcp__plugin_scopegrep_scopegrep__scopegrep_status,mcp__plugin_scopegrep_scopegrep__scopegrep_multi_retrieve"}).
EOF

jq -n --arg ctx "$context" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
