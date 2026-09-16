# Changelog

User-visible changes to the scopegrep plugin. Dates are Asia/Kolkata (IST).

## 0.3.6 — 2026-09-16

- Skill and `scopegrep_retrieve` docstring both gained a "complex tasks, not
  just point lookups" section: an agent this session, working on the research
  repo, defaulted to manual grep across a config/selector/precedent question
  spanning several files that shared no vocabulary — despite the skill
  already saying "call this first." One `scopegrep_retrieve` call on the same
  question, tried afterward, surfaced a sibling config pattern and a second
  selector function the manual search had missed entirely, unprompted, via
  the existing outbound-call block. The gap was that the skill described
  *when* to call it but not what a genuinely complex, multi-file, no-shared-
  vocabulary win looks like — so there was nothing concrete to recognize the
  moment against. Added a `include=[...]` row to the lookup table for
  wiring/config-spanning questions specifically, and led the retrieve
  docstring's own opening with the same point, since that's what an agent
  reads at the moment it's deciding whether to reach for this over grep.

## 0.3.5 — 2026-09-14

- Added a `PreToolUse` hook (`hooks/scopegrep-gate.sh`) that blocks the
  first `Grep` call, or grep-shaped `Bash` command, in a session until
  `scopegrep_retrieve`/`scopegrep_multi_retrieve` has been tried at least
  once — the per-prompt reminder alone wasn't enough to stop the agent
  reaching for grep first. Lifted by a `PostToolUse` hook
  (`hooks/scopegrep-mark-used.sh`) that marks the session as soon as a
  retrieval is attempted, success or failure, so a down service doesn't
  lock the session out of search.

## 0.3.4 — 2026-09-13

- The `UserPromptSubmit` hook now fires on every prompt instead of only ones
  shaped like a symptom/behavior question — the agent gets a reminder that
  scopegrep and its skill exist on every turn, not just when the hook's own
  heuristic recognized the prompt as a fit.
- README's accuracy figure restated as test-case pass rate (96% vs 85%,
  63 vs 27 test cases), matching how it's tracked internally.

## 0.3.3 — 2026-09-13

- README: added a "Results so far" section — the headline cost and accuracy
  numbers, stated with real sample sizes, plus an explanation of why input
  tokens dominate an agent's cost. Still no retrieval method, model, or
  infrastructure details, and no full result set.
- Added `docs/PYTHON_MODULE.md`: package layout, console scripts, env vars,
  and how to run the server or pre-warm helper outside a Claude Code session.

## 0.3.2 — 2026-09-13

- README rewritten as a plain product page: what this does, how to get
  access, how to install, and real usage documentation (tool reference,
  a worked example, an FAQ) — without describing the retrieval method or
  publishing performance figures.
- Removed the internal benchmark and experiment logs from this repository.
- Trimmed comments and messages throughout that named specific measurements,
  models, or infrastructure choices, without changing any behavior.

## 0.3.1 — 2026-09-13

- Installable directly from GitHub: `claude plugin marketplace add` and
  `pip install git+...` both work without a local clone.
- Pilot access is now requested by email rather than assumed; see the
  README's "Get access" section.

## 0.3.0 — 2026-09-13

- Renamed from an earlier internal name (`gistgrep`) to `scopegrep`.
- Split into a public plugin/client repo (this one) and a private hosted
  service, so a pilot tester never needs their own infrastructure account.
- The tool that reports "what else calls this" now works across more
  languages than before.
- Added a one-command way to warm up the service ahead of a demo, so the
  first request of a session doesn't have to wait for it.

## 0.2.2 and earlier

Pre-public-pilot development. Not itemized here.
