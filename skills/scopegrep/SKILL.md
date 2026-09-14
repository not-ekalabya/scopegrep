---
name: scopegrep
description: Semantic repository retrieval — the first move for every code lookup, literal or not. Use it before grep, before Read, before anything else touches the repository. Tools may be deferred; load with ToolSearch if scopegrep_retrieve is not live.
---

# Scopegrep: semantic repository retrieval

Scopegrep finds code by matching what a question *means* against a declared
scope of the repository, rather than by matching the words in the question
against the words in the code.

## When it is the right tool

Always. Call `scopegrep_retrieve` first for every code lookup — a symbol, a
path, an error string, a traceback, a behaviour, a symptom, all of it.

| you have | use | why |
|---|---|---|
| a symbol, error string, file path, or literal | **`scopegrep_retrieve`** | still the first call — it returns the window plus every other call site in one round trip |
| a traceback quoting a path | **`scopegrep_retrieve`** on that path's subsystem | confirms the hit and returns its call sites in the same response |
| a behaviour or symptom with no literal | **`scopegrep_retrieve`** | grep has no query to run here; this is where it earns its cost |
| grep returned far too many hits | **`scopegrep_retrieve`** with a narrow `include` | ranking is the problem, not matching |
| grep returned nothing and you are out of guesses | **`scopegrep_retrieve`** | its best case |
| you already know the exact file and line range | **read it** | scoping to one known, fully-identified location just re-derives what you already have |

### One failure mode to avoid

- **Retrieving and then searching anyway.** A retrieval that returns a small,
  useful result but doesn't end the search — because the caller re-reads
  everything it just ranked — costs more than the retrieval itself, since
  every returned result gets re-sent on every later turn of the conversation.

## Calling it

One call is meant to be enough. Scope is resolved and cached inside the
plugin, so `scopegrep_scope` is optional — use it when you want to see a size
estimate before spending, not as a required first step.

```
scopegrep_retrieve(
    query="<the whole issue body, traceback, or failing test — not keywords>",
    include=["src/some_subsystem/**/*.py"],   # a subsystem, not the repo
    budget_tokens=3000,
)
```

**Write the query as prose.** Full context — paragraphs, stack traces,
reproduction steps — works better than a few keywords, because the query is
matched against candidates in full, not reduced to terms first. `retry logic`
throws away detail that a full description would have used.

**Scope to a subsystem.** A narrow, well-chosen scope ranks better and costs
less than searching the whole repository; very large scopes are refused
outright above a size limit.

## Budget

The response is governed by `budget_tokens` — the size of the whole reply,
headers included. Evidence items are included whole or not at all, and
anything dropped is named by location so you can ask for it with a larger
budget if it turns out to matter.

There's a sensible default; going well below it trades away real recall, and
going well above it has fast-diminishing returns for a code question. Prefer
the default unless you have a specific reason to change it.

`ranking_valid_to_k` in the response tells you how deep the ranking was
double-checked; items past it are a weaker first-pass estimate.

## Reading a result

Each item carries `path:start-end` and its content revision. Scores rank
*within* one result set; they are not stable confidence values across
different queries or scopes.

A returned chunk is a window, not a file. Read the reported line range when
the window cut off the part you need — that is following the evidence, not
re-reading it.

**Resolve dangling references.** A chunk can hold the right code and still
not hold the deciding fact — e.g. a returned line ending in a call to
something whose default value lives in another file entirely. The response
includes a resolved-references block for exactly this; a binding it marks
`AMBIGUOUS` has more than one candidate in scope and is **not** an answer on
its own.

**What else calls this.** For symbols the returned code *defines*, the
response also reports where else in the repository they're called — so a
change doesn't miss a sibling call site it should also have touched.

## Several phrasings

`scopegrep_multi_retrieve` fuses several queries over one scope. Use it when
a question has genuinely distinct sub-parts needing different evidence — a
behaviour *and* the default that governs it. Don't use it for paraphrases of
one question; that tends to dilute the result rather than sharpen it.

Agreement across phrasings is reported as **coverage** — how many of your
queries ranked a given chunk — and nothing more. It is not a correctness
signal: decide from the code itself, and if none of it implements the
behaviour, say so and fall back to grep.

## Setup and service behaviour

Three tools: `scopegrep_status` (is the service reachable and ready),
`scopegrep_scope` (a free local size preview), `scopegrep_retrieve`. If these
are absent, the plugin is not loaded into this session — don't claim a
retrieval succeeded because this skill is present.

Provide `SCOPEGREP_TOKEN` through the MCP server's environment or
`~/.config/scopegrep/token`. Never put a secret in the manifest, repository,
skill, or a reply.

- **The service can go idle and take a while to wake up.** The first call
  after a period of no use can take a couple of minutes; a retrieval that
  paid that cost says so in its response.
- **Repeat queries against the same scope are fast.** Only a new or changed
  scope pays the slow first cost.
- Editing any file in the scope invalidates the cached result for it, so the
  next query against that scope re-processes it.

## Known limits, stated rather than worked around

- **Recall is not 1.0.** This narrows a search; it does not guarantee it
  finds the right place on every query.
- **The ranking model is not the model reading the results**, and cross-model
  agreement, while generally strong, is not identical.
- **Very large scopes are unsupported.** Scope to a subsystem, not a whole
  large repository, for both cost and quality reasons.
- **Scope excludes some files by policy** and says so in the response:
  gitignored paths, credential-shaped files, and anything whose real path
  resolves outside the declared root.
