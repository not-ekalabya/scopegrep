#!/usr/bin/env bash
# scopegrep — UserPromptSubmit hook.
#
# Fires only when the prompt is a behavior/symptom description with NO code
# literal in it. A literal (a call, a dotted path, a filename, a constant, an
# exception name) means grep is the correct tool and this hook must stay out
# of the way — that is the plugin's own decision table, and an earlier version
# of this hook implemented only half of it, forcing retrieval onto questions
# grep answers for free.
#
# When it does fire it also warms the scale-to-zero GPU container in the
# background, so the ~73s cold start overlaps the model's own thinking instead
# of landing on the critical path at retrieve time.
set -euo pipefail

input="$(cat)"
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null || true)"

[ -z "$prompt" ] && exit 0

# --- guard: a concrete code literal means grep, not retrieval -----------------
# Deliberately conservative: only unambiguous code shapes, so plain prose like
# "why does the WebSocket drop" still qualifies as a symptom question.
literal='(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*\(|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|[A-Za-z0-9_/.-]+\.(py|ts|tsx|js|jsx|go|rs|java|rb|php|c|cc|cpp|h|hpp|cs|kt|swift|sql|json|ya?ml|toml|ini|cfg)\b|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|[A-Za-z_][A-Za-z0-9_]*(Error|Exception)\b)'

if printf '%s' "$prompt" | grep -Eq "$literal"; then
  exit 0
fi

# --- trigger: behavior/symptom shaped, no literal to anchor on ----------------
pattern='(why (does|is|do|did|are|would)|which (part|file|module|component|piece)|what part of|how does .*(decide|choose|determine|govern|know|track|detect|tell)|how is .*(decided|chosen|determined|governed|tracked|detected)|what (causes|triggers|decides|governs|controls|sets)|who decides|sometimes.*sometimes|which .*(handles|owns|controls|decides)|find the (part|code).*that|condition (governs|controls|decides)|decides which|what.{0,30}responsible for|where (is|are|does) .{0,40}(configured|set|defined|handled|implemented|stored|come from)|what .{0,20}(cleans|handles|manages|owns|resets|clears|removes|deletes|closes|frees|releases|rolls back))'

if ! printf '%s' "$prompt" | grep -Eiq "$pattern"; then
  exit 0
fi

# --- routing note; no prewarm on the prompt path ----------------------------
# The old hook warmed the GPU here on every symptom-shaped prompt, including
# the many that never retrieve. Warming is a cost decision that belongs to the
# call that needs it, not to prompt submission, so routing happens first and
# the container stays cold until something actually queries it.

read -r -d '' context <<'EOF' || true
This prompt describes a behaviour and names no code literal, so grep has no query to run. scopegrep may help. It is advisory:

- Already know the file? Read it. Scoping a retrieval to a file you have named costs more than reading it.
- A literal turns up as you work (symbol, path, error string)? grep it.
- Location genuinely unknown -- grep found nothing, or too much to rank? Retrieve: scopegrep_retrieve(query=<the full symptom/issue text>, include=[<one subsystem>], budget_tokens=3000). Tools may be deferred; load with ToolSearch({query: "select:mcp__plugin_scopegrep_scopegrep__scopegrep_retrieve,mcp__plugin_scopegrep_scopegrep__scopegrep_scope"}).
- Read the returned chunks and pick the one whose code implements the behaviour. Rank order separates weakly; the set is the evidence. If a value the answer depends on is not shown, the response's resolved-references block usually has it -- a binding marked AMBIGUOUS is not an answer.
- If nothing returned implements the behaviour, say so and fall back to grep. Re-querying buys the same recall twice.
EOF

jq -n --arg ctx "$context" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
