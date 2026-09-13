# Changelog

User-visible changes to the scopegrep plugin. Dates are Asia/Kolkata (IST).

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
