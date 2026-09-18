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
| a wiring/config question spanning files with no shared vocabulary ("how does X actually get configured", "what overrides Y", "is there already a pattern for this") | **`scopegrep_retrieve`** scoped to the subsystem | ranks scattered config, code, and precedent together by meaning; grep needs a shared word across every file to connect them |
| grep returned far too many hits | **`scopegrep_retrieve`** with a narrow `include` | ranking is the problem, not matching |
| grep returned nothing and you are out of guesses | **`scopegrep_retrieve`** | its best case |
| you already know the exact file and line range | **read it** | scoping to one known, fully-identified location just re-derives what you already have |

### Complex tasks, not just point lookups

This is not keyword search with better ranking. Two things a point lookup
does not do, that a single call here does:

- **Trace something across files that share no vocabulary.** How a behavior
  is actually configured often crosses a JSON/YAML config, a selector
  function picking between variants, and a comment explaining why — three
  files that never use the same word for the same idea, so no grep query
  connects them. One `scopegrep_retrieve` call ranks the whole declared scope
  by meaning at once, and routinely surfaces a sibling config or an existing
  precedent for the exact thing you were about to hand-roll, because it read
  files you did not think to search, not just the ones you named.
- **Answer the completeness half of the question, unprompted.** Every result
  that returns a chunk defining a symbol also reports every other call site
  of it in scope ("What else calls this," below) and every unresolved
  binding the returned code depends on ("Resolve dangling references,"
  below) — without a second call. A plain search returns matches; this also
  returns what breaks if you change what it found.

Reach for it on architecture-shaped questions, not only "where is X":
*how does this actually get configured end-to-end*, *what else has to change
if I touch this*, *is there already a pattern for what I'm about to add*. On
these, one well-scoped call tends to beat several rounds of manual grep —
not because it's faster (it isn't; see below), but because it doesn't
require already knowing which files to look in.

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
  gitignored paths (`.gitignore`, `.ignore`, and `.scopegrepignore` — same
  syntax, all honoured together), credential-shaped files, and anything
  whose real path resolves outside the declared root.

### Repos with noisy generated directories

A broad `include` (`["**/*.py"]`, or no `include` at all) walks everything
under root before scope size is even checked — a repo with baked-in task
environments, run/output directories, or vendored checkouts sitting
alongside real source can turn a scope that should be a few hundred files
into hundreds of thousands, most of it installed-library internals nobody
meant to rank. `exclude=[...]` fixes one call; a `.scopegrepignore` file at
the repo root fixes every call against it, the same way a `.gitignore` you
already maintain would, without depending on that file's own scope
(git-tracked vs generated is a different question from search-worthy vs
noise). Add one wherever a project keeps generated environments or run
output under its root.
