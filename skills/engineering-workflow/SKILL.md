---
name: engineering-workflow
description: Before any non-trivial code, config, schema, migration or API change, and for architecture work.
---

# Engineering Workflow

Prefer root-cause fixes over symptom treatment.

## Engineering standards

Do not add hacks, broad fallbacks, silent error suppression, arbitrary sleeps/retries, hardcoded special cases, compatibility shims, or unsafe casts such as `as any` unless explicitly justified. Checks get no such exception: see Repository safety.

If a workaround is unavoidable:

- label it as a workaround;
- explain the root cause;
- explain the risk;
- describe the expected replacement;
- add the smallest useful guard or test so it does not become invisible technical debt.

Legitimate bounded retries are allowed only for documented transient failures, with clear limits, backoff where appropriate, and no masking of permanent errors.

## Repository safety

- Keep edits minimal in surface area but complete in responsibility.
- Fix the owning layer, not every call site separately.
- Follow existing project conventions, naming, structure, formatting, and architecture.
- Keep public APIs, generated files, schemas, migrations, and tests consistent with source definitions.
- Do not introduce new production dependencies unless clearly necessary. If a dependency is necessary, explain why the existing stack is insufficient.
- Maintain type safety with proper types, guards, validation, and normalization helpers.
- Preserve useful diagnostic context in errors.
- Never skip, weaken or suppress a test, lint rule or type check, and never delete or rewrite one to get it passing; no justification makes that acceptable. A check found disabled is re-enabled as `development-verification` §4 describes.
- Change or remove a test only when the behavior it asserts is shown wrong, or was changed or removed on purpose (`root-cause-engineering`), and say which in the change.

## Search before inventing

Before adding new logic, search for existing helpers, patterns, tests, types, schemas, configs, and similar implementations. Use `codebase-memory-mcp` graph tools first for structural search — `search_graph(name_pattern/label)` to locate symbols, `query_graph` (Cypher) to find duplicated shapes, `get_code_snippet` to read the source, `trace_path` for call context — instead of a wide `Grep`/`Glob`/`Read` scan. Reuse existing abstractions where appropriate. Extract shared code only when it reduces duplication without widening scope unnecessarily.

## Development workflow

For code changes:

1. Inspect enough context before editing: relevant files, tests, configs, docs, and similar implementations. Start structural discovery with `codebase-memory-mcp` graph tools (`get_architecture`, `search_graph`, `trace_path`, `get_code_snippet`, `detect_changes`) rather than a broad manual scan; use `Read`/`Grep`/`Glob` for exact detail, text/config content, unindexed files, or when the graph is unavailable/stale. Recall the decisions and constraints recorded for the area with `nlm-memory recall` before designing the change.
2. For complex tasks, maintain a concise plan and update it as work progresses.
3. Batch related edits logically.
4. Implement the smallest responsible fix at the owning layer.
5. Add or update tests when behavior changes or the bug could regress, as Writing tests below describes.
6. Run narrow checks first, then broader checks when warranted.
7. Record an accepted design decision or a discovered constraint with `nlm-memory remember` when it is confirmed (see `project-memory`).

## Writing tests

A test records behavior already shown to work, never whatever the code happens to do.

- Before writing it, run the code on real inputs and compare the result with the requirement or contract. Code that falls short is a failure for `root-cause-engineering`, not a test to adjust.
- Take expected values from the requirement, the contract or an independent calculation, never from the code's current output. Accept a snapshot or golden file only after checking its content against the requirement.
- A test for a bug fails on the code before the fix and passes after it: run it both ways.

## Tooling

- For code discovery (symbols, callers/callees, call chains, dependencies, architecture, change impact) use `codebase-memory-mcp` graph tools first; fall back to `Grep`/`Glob`/`Read` for text/config content, non-code or unindexed files, or a stale/unavailable graph. If the project is not indexed, run `index_repository` first. See the `codebase-memory` skill.
- Prefer dedicated Claude Code tools over raw shell when available.
- Prefer `apply_patch` or equivalent edit tools for targeted edits.
- Use raw shell for inspection, tests, builds, and commands where appropriate.
- Avoid large manual scans when specialized tools can narrow the scope.
- Prefer skill-provided scripts, references, or workflows when a selected skill includes them and they fit the task.
