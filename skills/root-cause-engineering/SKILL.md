---
name: root-cause-engineering
description: Before editing on a bug, flaky test, regression, unclear failure, performance problem or production incident.
---

# Root-Cause Engineering Protocol

For bugs, flaky behavior, broken tests, regressions, unclear failures, or performance issues:

1. Reproduce or characterize the failure.
   - Capture the exact command, error, observed behavior, and expected behavior.
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

5. Fix the cause.
   - Prefer contract, validation, data flow, lifecycle, type, schema, or state ownership fixes over local masking.

6. Verify.
   - Add or update tests when behavior changes or the bug could regress.
   - Run narrow checks first, then broader checks when warranted.

If you cannot reproduce, characterize with logs, code paths, version/config boundaries, and plausible failure invariants. Be explicit about what is confirmed versus assumed.
