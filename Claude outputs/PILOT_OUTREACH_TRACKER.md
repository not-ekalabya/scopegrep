# scopegrep pilot outreach — candidate list & 7-day plan

Written 2026-09-13. Companion to `PRODUCT_OVERVIEW.md`. Not for the public
repo — this is the internal-doc slot that `PRODUCT_OVERVIEW.md` §4 already
pointed at.

## 0. Why this list is shaped the way it is

Your token-ratio table says where scopegrep should and shouldn't be pitched:

| Task type | In:out tokens | In:out cost | Dominant |
|---|---|---|---|
| Chat/assistant, short replies | 4:1 | ~1:1 | Even |
| RAG / document Q&A | 20:1 | ~4:1 | Input |
| **Coding agents (multi-step, growing context)** | **25–50:1** | **~6–10:1** | **Input, heavily** |
| Classification/extraction | 50:1+ | ~10:1+ | Input, heavily |
| Long-form generation | 1:3 | ~1:15 | Output |
| Reasoning/planning models | 1:5–1:10 | ~1:25–1:50 | Output, heavily |

scopegrep's entire mechanism — answer the "who else calls this" question
inline, in the same response, instead of costing the agent a follow-up turn
that re-sends the whole prior transcript — only pays off where a saved round
trip is worth more than the tokens it would've returned. That's exactly the
"coding agents" row: your own README math is a median ~28,500 tokens of
re-sent context per turn in the benchmark suite, against a call-site block
that's 228–958 tokens. RAG/document Q&A is directionally similar (input
dominates) but the re-send penalty per turn is smaller, so it's a weaker
pitch, not a wrong one. Everything on the bottom two rows (long-form
writing, reasoning/planning) is output-dominated — an agent that's mostly
generating tokens, not re-reading them, won't feel this at all. Don't spend
outreach cycles there.

So the filter for this list was: **is this company's product a multi-step
coding agent operating on a real, growing repo context** — either because
they build one, or because their own engineering team runs one daily against
a large codebase (dogfooding case). Companies whose LLM spend is
output-heavy (pure writing/summarization tools, most chatbot products) were
excluded even if "AI startup" and "YC" both applied.

## 1. The 7-day constraint shaped channel choice, not just target choice

Cold email to a general inbox is the slowest, most bounce-prone channel you
have — for a 7-day approach-to-feedback window, every Tier 1 target below is
picked partly because there's a *faster* channel available: a founder active
on X, a public Discord/GitHub repo you can open an issue or PR against, or a
YC batch-mate connection. Use the fastest channel listed per company, not
email-by-default.

The ask itself needs to fit in 7 days too. Frame it as: *install the plugin
in your existing Claude Code/Codex setup, run it for 2–3 days on real
tickets/PRs against your own repo, tell us what changed.* That's a
same-day integration (`claude plugin marketplace add
not-ekalabya/scopegrep`), which is what makes a 7-day loop realistic — don't
ask for anything that requires them to change their agent architecture
first.

## 2. What to actually say (from PRODUCT_OVERVIEW.md §2 — don't overclaim)

- **Lead with cost, not accuracy.** "0.57x billed input on MEDIUM-difficulty
  tasks (43% cheaper), independently reproduced on a second port" is the one
  claim that has survived every re-check — say it plainly and first.
- **Never quote the MEDIUM 3/3-resolved number as a settled result.** The
  2026-09-12/13 reproduction attempt scored 0/3 against it; pooling all 7
  MEDIUM episodes on disk gives foldp 1/7 vs control 2/3 on the specific
  failure mode, Fisher's exact p=0.183 — directionally favorable, not
  significant. Say "small-n, directionally favorable, we're mid-rerun" —
  that's true and it's still a real pitch.
- **Never restate it at the test-case level** ("63 vs 27 resolved") — both
  failing tests in a pair always co-occur, so that framing is the same 10
  episodes multiplied to look like more evidence than they are.
- **Zero regressions on previously-passing tests, every arm, every cell** —
  this one has no caveat, use it freely, it's a good risk-reducer line for a
  pilot pitch specifically ("this doesn't touch what already worked").
- **Don't pitch it against HARD-bin-shaped problems** (fix-selection
  failures, not retrieval-completeness failures) — say so upfront if a
  target's use case looks like that, it'll save both sides time in the pilot.

## 3. Tier 1 — best fit, fastest movers (approach Day 0)

These are small, technical, YC-adjacent teams whose entire public output is
about this exact problem (context cost, codebase-aware retrieval, coding-agent
completeness). High chance of a real technical read within days, not weeks.

1. **The Token Company** (YC W26) — "intelligent compression for LLM context
   bloat," per their own Launch YC post. This is the single most
   on-the-nose audience possible: they sell cost reduction on the input
   side of the exact ratio your table describes, so no framing work is
   needed. Channel: X, @thetokenco (active), or thetokencompany.com.
   Angle: not a competitor pitch — call-site fan-out is a different lever
   (completeness, not compression) and could be pitched as complementary/stackable.

2. **OpenSpec** (YC W26, github.com/Fission-AI/openspec) — spec-driven
   development framework for AI coding assistants, real GitHub traction
   (front-paged on HN, 20K+ stars claimed). Their whole premise is "make
   the agent follow a spec instead of guessing," which is a sibling problem
   to "make the agent see all the callers instead of guessing." Channel:
   GitHub issue/discussion on Fission-AI/openspec, or the HN thread
   (news.ycombinator.com/item?id=45663874) — both faster than email for an
   OSS-first team.

3. **HumanLayer** (YC-backed) — builds an orchestration layer for AI coding
   agents and publishes the most detailed public writing anywhere on
   "advanced context engineering for coding agents" (their own GitHub repo,
   humanlayer/advanced-context-engineering-for-coding-agents). Even if they
   don't run a formal pilot, this team will give you sharp, fast, credible
   technical feedback — arguably your best Day-0 send for critique alone.
   Channel: GitHub (open an issue referencing their ACE-FCA doc directly,
   cite the specific mechanism overlap), or humanlayer.dev.

4. **Sentra** (a16z Speedrun, raised $5M Jan 2026, sentra.app) — building
   "codebase memory" for AI agents and actively publishes comparison
   articles on this exact tool category (their site has pieces comparing
   GitNexus, Codebase-Memory-MCP, and CodeGraph). A team that writes
   comparison content about your category will move fast on a look at a new
   entrant — either as a pilot or as content. Channel: sentra.app contact
   form or the author byline on their articles page.

5. **Greptile** (YC-backed, ~$30M raised) — AI code review agent whose own
   tagline is "complete context of your codebase." This is closest to a
   direct validation of your core claim: their product's entire value
   proposition is not missing a call site during review. Slightly bigger
   team than the others (post-$30M), so treat as Tier 1 for fit but budget
   for a slower reply than the four above. Channel: greptile.com contact,
   or their YC page for a warm-intro path if you have any batch overlap.

## 4. Tier 2 — strong fit, budget more follow-up time (approach Day 1–2)

Send these once Tier 1 is out the door; don't let them block Tier 1.

6. **CodeRabbit** — AI code review, same "did we see every affected call
   site" problem as Greptile, larger/more commercial org so slower cycle.
   Still worth a shot given direct product overlap; frame as a technical
   evaluation ask to an eng lead, not a sales-style pitch.

7. **Qodo** (formerly Codium) — same code-review category, same caveat on
   response speed. Good fallback if Greptile/CodeRabbit go quiet by Day 3.

8. **Archal** (YC S26) — confirmed via their own YC page as "API sandboxes,
   built for AI agents" (note: some aggregator writeups describe them
   differently — trust the company's own YC listing). Relevant if their
   sandboxed agents do multi-step code changes against real repos; verify
   this in the first message rather than assuming.

## 5. Tier 3 — stretch targets, low effort per send (Day 3, no chasing)

Send once, don't follow up — these are prestige/visibility shots, not the
companies your 7-day feedback loop should depend on.

9. **Cognition (Devin)** — flagship autonomous coding agent, $1B+ raised,
   almost certainly has in-house retrieval already and a slow intake
   process. A single cold outreach (founder or eng-lead X post reply) costs
   you nothing; don't expect or wait on a reply within the week.

10. **Cursor (Anysphere)** — same logic: huge, likely self-sufficient on
    context tooling, but a "we tried this on Cursor's own team" line would
    be a strong future case study if it ever landed. One-shot send only.

## 6. What to skip, and why

Skip anything whose product is primarily long-form writing, summarization,
or a reasoning/planning-only workload (bottom two rows of your table) — the
mechanism has nothing to save there since output tokens, not re-sent input
context, dominate their cost. Also skip humanoid-robotics and
agent-payments/identity infra startups that showed up in the same YC
batches (Inkbox, Nori, OS3, Manifold, Agentcard, etc.) — real companies, just
not doing multi-turn code retrieval against a growing repo context, so the
core pitch doesn't apply.

Note: Mendral (YC W26, "AI DevOps engineer for CI") is not on this list —
their founders were acquihired by Anthropic shortly after the batch, per The
New Stack's reporting, so there's no independent team left to pilot with.

## 7. Day-by-day plan

- **Day 0 (today):** Send all 5 Tier 1 messages, fastest channel per
  company. Each message: cost claim (0.57x, reproduced) + zero-regressions
  line + the specific 2–3 day ask + link to the sanitized public repo/README
  Results section.
- **Day 1–2:** Send Tier 2 (3 messages). Nudge any Tier 1 contact who
  engaged but hasn't scheduled anything.
- **Day 3:** Follow up once on Tier 1 non-responders. Send Tier 3 (2
  messages, no follow-up planned). If Greptile/CodeRabbit/Qodo are all
  quiet, this is also the day to widen Tier 2 with 1–2 more code-review or
  coding-agent names if capacity allows.
- **Day 4–5:** Run the actual pilot with whoever said yes — walk them
  through `scopegrep-prewarm` (cold start is 71–117s, don't let that be
  their first impression) and the shared pilot token. Keep a shared doc of
  what each pilot partner reports.
- **Day 6:** Send a short structured feedback form (3–5 questions: did it
  change a real outcome, did the cost number hold on their repo, would they
  keep using it, what broke). Structured beats open-ended for a 1-day
  turnaround.
- **Day 7:** Compile. Decide which pilot(s) become quotable case studies,
  and which the corrected small-n accuracy framing (§2 above) needs to
  travel with if quoted publicly.

## 8. One operational flag before you scale outreach

The live service currently runs on a single shared pilot token
(`sheldon-cooper`, per PRODUCT_OVERVIEW.md §4) with no per-user accounts —
fine for the Tier 1 handful, but if more than a few pilots run concurrently
you won't be able to tell whose traffic is whose, or isolate one pilot's
usage from another's cold-start/rate experience. Worth deciding before Day 4
whether that's acceptable for this round or whether it's worth a quick
per-partner token before handing the same credential to 5–8 outside teams.
