# Changelog

User-visible changes to the scopegrep plugin. Dates are Asia/Kolkata (IST).

## 0.3.8 — 2026-09-18

- Added `.scopegrepignore` support: same gitignore-pattern syntax as
  `.gitignore`/`.ignore`, honoured alongside them on every call against a
  root, no `exclude=` needed per call. Found the gap the hard way: an
  `include=["**/*.py"]` scope against a repo with baked-in task
  environments and run-output directories walked several hundred thousand
  files before hitting the size check, because those directories were
  neither gitignored nor caught by the generic default excludes (which
  only match directories literally named `env`, not project-specific
  names). `exclude=` fixes one call; a root-level ignore file now fixes
  every call against it. Documented in the skill under "Repos with noisy
  generated directories."

## 0.3.7 — 2026-09-17

- README "Results so far" corrected: the accuracy-delta claim (96% vs 85% of
  test cases, 63 vs 27 across runs) is withdrawn. Re-pooling the full set of
  real agent episodes now on disk — more than existed when that number was
  first measured — shows no significant resolution-rate effect in either
  direction; the sample sizes affordable for repeated real agentic bug-fix
  runs aren't enough to tell a real effect from noise. Replaced with the
  completeness benchmark result as the lead capability claim, and reworded
  the cost section so it no longer leans on the withdrawn accuracy number to
  justify its range.
- Completeness benchmark expanded same day, n=20 (one repo) → n=50 (two
  unrelated repos), and given a real significance test for the first time:
  def-file recall 92%/100% at k=10/k=20 vs a dense-embedding baseline's
  60%/74%, 95% CI on the k=20 gap [+14, +40] points, excludes zero. Reported
  the baseline's k=1 win honestly too (36% vs 16%) rather than only the
  favorable cuts.

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
