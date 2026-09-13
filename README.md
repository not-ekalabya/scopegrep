# scopegrep

Find code by describing what it does, not by guessing what it's called.

`scopegrep` is a Claude Code plugin for coding agents. Instead of matching
keywords, it answers a question in plain language — "where is retry backoff
configured", "what handles session invalidation" — against a codebase, and
returns the parts of the code most relevant to that question, along with a
short note on where else in the codebase those same parts are used.

This project is under active development as part of an ISEF research
submission. The pilot program below is how it's being validated on real
codebases; detailed methodology and results are part of the submitted
research materials rather than this public repository.

## Get access

Pilot testing is invite-only right now. Email
[ekalabya2010@gmail.com](mailto:ekalabya2010@gmail.com) to request an access
code — include a line about the codebase(s) you'd try it on.

## Install

```bash
claude plugin marketplace add not-ekalabya/scopegrep
claude plugin install scopegrep@scopegrep
```

or, for the command-line tools only:

```bash
pip install git+https://github.com/not-ekalabya/scopegrep.git
```

Either way installs directly from this repository — no separate download
step.

## Set up your access code

```bash
export SCOPEGREP_URL='<the URL you were given>'
export SCOPEGREP_TOKEN='<the code you were given>'
```

The plugin reads both from your environment, so your code is never written
into any file you'd commit. It also works as a persistent local file
(`~/.config/scopegrep/token` or `.scopegrep_token` in this repo's root) if
you'd rather not export an environment variable every session.

## Documentation

### Quick start

Once installed and configured, an agent session with the plugin active gains
four tools. You don't call these by hand in normal use — the agent decides
when to use them, guided by [the bundled skill](skills/scopegrep/SKILL.md) —
but this is what they do:

| tool | what it's for |
|---|---|
| `scopegrep_status` | Check whether the service is reachable and ready. |
| `scopegrep_scope` | Preview how large a set of files is before searching it — how many pieces it breaks into, roughly what that will cost to search. |
| `scopegrep_retrieve` | The main tool: ask a question in prose about a declared set of files, get back the most relevant pieces of code — and, alongside them, a note on every other place in the codebase that uses the same functions or classes, so a change doesn't miss a caller it should have updated too. |
| `scopegrep_multi_retrieve` | Ask several related questions against the same declared scope in one call. |

### A typical exchange

```
scopegrep_scope(include=["src/**/*.py"])
  -> previews the scope: how many files, how many pieces, and whether
     it's small enough to search directly or should be narrowed first

scopegrep_retrieve(query="<a description of the bug, or the failing test output>",
                    include=["src/**/*.py"])
  -> the most relevant pieces of code, plus a note on every other place
     that calls the same functions -- so a fix doesn't miss a sibling
     call site it should also have touched
```

### When to reach for it

- You can describe a *behavior* but don't know which file or function
  implements it.
- A grep for the obvious keyword comes back empty, or comes back with too
  much to read through.
- You're about to change a function and want to know everywhere else in the
  codebase that calls it, before you decide the change is complete.

### When not to

- You already know the exact name, path, or error string — grep is faster
  and exact.
- You've already found and opened the file — just read it.

### First request may be slow

The underlying service can go idle between uses and take up to a couple of
minutes to become ready again on the next request. If you're about to give a
live demo, run `scopegrep-prewarm` (or `./tools/prewarm.sh` if you installed
as a plugin only) a couple of minutes ahead of time, so that wait happens
before anyone's watching rather than during.

### FAQ

**Does it see my code?** Only the files inside whatever scope you declare
with `include=[...]`. Nothing outside that scope is read or sent anywhere.

**Does it modify anything?** No — every tool here is read-only. It never
edits, writes, or deletes files.

**Why do I need an access code?** The service behind this plugin is hosted,
not run on your machine, so it needs a way to know which requests are part
of the pilot. See "Get access" above.

**What if it's slow or wrong?** Please report it — that feedback is exactly
what the pilot exists to collect. Email the address above with what you
asked, what came back, and what you expected instead.

## A note on scope

This repository intentionally does not describe the retrieval method, the
underlying model, hosting details, or measured performance figures — those
are part of the research submission this project supports, not public
documentation. What's here is what a pilot tester needs to install, configure,
and use the tool. If you're a judge or reviewer with a reason to see the
underlying methodology, please reach out to the email above directly.
