# Changelog

User-visible changes to the scopegrep plugin. Dates are Asia/Kolkata (IST).

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
