# scopegrep

Find code by describing what it does, not by guessing what it's called.

`scopegrep` is a Claude Code plugin for coding agents. Instead of matching
keywords, it answers a question in plain language — "where is retry backoff
configured", "what handles session invalidation" — against a codebase, and
returns the parts of the code most relevant to that question, along with a
short note on where else in the codebase those same parts are used.

This project is under active development as part of a research project.
The pilot program below is how it's being validated on real
codebases; the retrieval method itself, the model behind it, and the full
result set are part of the submitted research materials rather than this
public repository — what follows is the headline numbers, stated with their
real sample sizes, not the methodology behind them.

## Results so far

Two separate measurements, on two different kinds of evidence: a designed
retrieval-quality benchmark (deterministic, scored directly), and real agent
runs on previously-unseen open-source bug fixes (stochastic, agent-driven).

**Completeness — the actual capability claim, and the one with a real
significance test behind it.** On a same-repo, multi-gold benchmark built
around the exact failure this tool targets (50 questions across two
unrelated real codebases, 2-7 real call sites each — a fix that needs
several call sites of the same function updated together, not just the one
a keyword search would find): scored on whether retrieval surfaces the
*definition* — the step that decides whether the rest of the call sites can
even be found — `scopegrep` reaches 92% by the top 10 results and 100% by
the top 20. A standard dense-embedding baseline, given the same budget,
reaches 60% and 74% at the same two points, and still hasn't reached 100%
even given the top 50. The gap is real, not sampling luck: a 95% confidence
interval on the difference at the top-20 mark is +14 to +40 percentage
points, computed by resampling the 50 questions 2,000 times — it does not
cross zero. (At the very top result only, the dense baseline actually wins,
36% to 16% — reporting that honestly too; `scopegrep`'s advantage shows up
once it's allowed a handful of results, not right at rank one.)

**Cost: 21-43% fewer input tokens billed per task** on real agent runs
(n=3 repetitions per task), depending on the task. This is the reproducible
number — a later, independent rerun landed within a point of the original
measurement.

**What we're not claiming.** An earlier version of this page reported an
accuracy delta — the agent resolves more bugs with the tool wired in than
without. That number does not survive a fuller pool of the same repeated
task: real agent-driven bug-fix runs vary enough, run to run, that the
repetition counts affordable for a project this size (single digits per
arm) aren't enough to tell a real effect from noise, in either direction.
We'd rather say that plainly than repeat a number that looked good on a
first small sample. The completeness result above doesn't have this
problem — it's a deterministic score over a designed benchmark, not an
agent's stochastic path through a task, so a much smaller sample size
actually means something there.

On harder, more varied real-world tasks the per-task cost number moves
around and isn't always a win by itself. That range is a real property of
running an LLM agent, not noise specific to this tool: the identical task,
run twice with the identical setup, can bill substantially different token
totals from one rollout to the next, since a coding agent's path through a
task (how many turns it takes, what it decides to re-read) isn't fixed.

### Why input tokens matter this much

A coding agent's turn cost is dominated by *input* tokens, not output: every
turn re-sends the entire prior conversation — every file read, every tool
result, every previous turn's output — because the model has no memory
between calls except what's in that resent context. In our measurements, a
single turn re-sends a median of ~28,500 tokens of prior context. That means
a tool call that *avoids one extra round trip* — by returning a fact the
agent would otherwise have had to go ask for separately — is worth far more
than the tokens that tool call itself returns. A 40-token fact fetched via
an extra turn costs roughly 700x its own size in re-sent context; the same
fact folded into a response the agent already needed costs nothing extra.
This is also why `scopegrep`'s own payload size barely matters: across every
measured task it's under 0.07% of that task's total billed input — any cost
difference you see above comes from turns avoided, not payload size.

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

Running it outside a Claude Code session — another MCP host, or the raw
console scripts — is documented separately: [docs/PYTHON_MODULE.md](docs/PYTHON_MODULE.md).

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

This repository states what the tool does and the headline numbers behind
that claim, honestly and with real sample sizes — but it intentionally does
not describe the retrieval method itself, the underlying model, or hosting
details, and it doesn't publish the full result set behind "Results so far."
Those are part of the research submission this project supports, not public
documentation. What's here is what a pilot tester needs to install, configure,
and use the tool, and enough evidence to decide whether it's worth trying.
If you're a judge or reviewer with a reason to see the underlying
methodology, please reach out to the email above directly.
