---
name: root-cause-engineering
description: Before editing on a bug, a failing test, lint or type check, a flaky test, a regression, an unclear failure, a performance problem or a production incident.
---

# Root-Cause Engineering Protocol

For bugs, flaky behavior, broken tests, regressions, unclear failures, or performance issues:

1. Reproduce or characterize the failure.
   - Capture the exact command, error, observed behavior, and expected behavior.
   - Recall first: `nlm-memory recall "<error text or symptom>"`. A recorded GOTCHA may already name the cause, and the lookup takes a second.
   - Do not edit before understanding the failure boundary unless the cause is obvious.

2. Identify ownership.
   - Find the source of truth: config, schema, API contract, type definition, lifecycle, state model, build step, migration, dependency boundary, or runtime environment.
   - Use `codebase-memory-mcp` graph tools to locate it — `search_graph` for the symbol, `trace_path` (direction=both) for callers/callees and the propagation path, `get_architecture` for boundaries, `detect_changes` to map the current diff to affected symbols — before falling back to `Grep`/`Read`.
   - Determine where the invariant should be enforced.

3. Search existing patterns.
   - Look for related code paths, tests, previous fixes, and project conventions — via `codebase-memory-mcp` (`search_graph`, `trace_path`, `get_code_snippet`) first; use `Grep`/`Read` for text/config content or when the graph is unavailable/stale.

4. Form the cause before editing.
   - What invariant was violated?
   - Why did it happen here?
   - Why did existing tests/checks miss it?
   - What is the smallest responsible layer that should own the fix?
   - For a failing test, lint or type check: which side is wrong, the code or the check? Decide from the requirement, the contract, the history of both sides (`git log`, `git blame`, the change that turned it red) and the code's observed behavior, never from which side is easier to change. Suspect the code first: a check that passed before and fails now has most likely caught a regression.

5. Fix the cause.
   - Prefer contract, validation, data flow, lifecycle, type, schema, or state ownership fixes over local masking.
   - Fix a regression in the code. Fix a lint or type error in the code too, unless the check's configuration is shown wrong (a wrong path, resolver, stub or version); then correct that configuration without disabling, excluding or suppressing anything. Change or remove a test only when the behavior it asserts is shown wrong, or was changed or removed on purpose, and say which in the change. Never edit an assertion, expected value, snapshot, fixture or tolerance to match what the code now produces, and never skip or suppress the check.

6. Verify.
   - Add or update tests when behavior changes or the bug could regress. A regression test fails on the unfixed code and passes on the fix: run it both ways.
   - Run narrow checks first, then broader checks when warranted.
   - Record the confirmed root cause as a GOTCHA, or an open hazard as a RISK, with `nlm-memory remember` (see `project-memory`), so the next session finds it in a second.

If you cannot reproduce, characterize with logs, code paths, version/config boundaries, and plausible failure invariants. Be explicit about what is confirmed versus assumed.
