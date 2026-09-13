---
description: Verify the scopegrep retrieval service is reachable, warm, and correct
---

Check the scopegrep plugin end to end and report what you find:

1. Call `scopegrep_status`. Report whether the service is reachable, whether a
   container is warm, and which scopes it already holds.
2. Call `scopegrep_scope` on this repository's main source directory. Report
   the chunk count, the token count, and the recommended k with its token cost.
3. If the scope is non-empty, run one `scopegrep_retrieve` with `k=12` and a
   real question about this codebase, and report the wall time,
   `ranking_valid_to_k`, and how many tokens came back as a fraction of the
   scope.

Then state plainly whether the plugin is usable right now, and if not, which
of the three configuration steps in the plugin README is missing.
