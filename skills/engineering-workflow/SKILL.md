---
name: engineering-workflow
description: Before any non-trivial code, config, schema, migration or API change, and for architecture work.
---

# Engineering Workflow

Prefer root-cause fixes over symptom treatment.

## Engineering standards

Do not add hacks, broad fallbacks, silent error suppression, arbitrary sleeps/retries, test weakening, lint disabling, hardcoded special cases, compatibility shims, or unsafe casts such as `as any` unless explicitly justified.

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
- Do not delete, skip, weaken, or rewrite tests just to make checks pass.
- If a test is wrong, explain why and update it to reflect the correct contract.

## Search before inventing

Before adding new logic, search for existing helpers, patterns, tests, types, schemas, configs, and similar implementations. Use `codebase-memory-mcp` graph tools first for structural search — `search_graph(name_pattern/label)` to locate symbols, `query_graph` (Cypher) to find duplicated shapes, `get_code_snippet` to read the source, `trace_path` for call context — instead of a wide `Grep`/`Glob`/`Read` scan. Reuse existing abstractions where appropriate. Extract shared code only when it reduces duplication without widening scope unnecessarily.

## Development workflow

For code changes:

1. Inspect enough context before editing: relevant files, tests, configs, docs, and similar implementations. Start structural discovery with `codebase-memory-mcp` graph tools (`get_architecture`, `search_graph`, `trace_path`, `get_code_snippet`, `detect_changes`) rather than a broad manual scan; use `Read`/`Grep`/`Glob` for exact detail, text/config content, unindexed files, or when the graph is unavailable/stale.
2. For complex tasks, maintain a concise plan and update it as work progresses.
3. Batch related edits logically.
4. Implement the smallest responsible fix at the owning layer.
5. Add or update tests when behavior changes or the bug could regress.
6. Run narrow checks first, then broader checks when warranted.

## Tooling

- For code discovery (symbols, callers/callees, call chains, dependencies, architecture, change impact) use `codebase-memory-mcp` graph tools first; fall back to `Grep`/`Glob`/`Read` for text/config content, non-code or unindexed files, or a stale/unavailable graph. If the project is not indexed, run `index_repository` first. See the `codebase-memory` skill.
- Prefer dedicated Claude Code tools over raw shell when available.
- Prefer `apply_patch` or equivalent edit tools for targeted edits.
- Use raw shell for inspection, tests, builds, and commands where appropriate.
- Avoid large manual scans when specialized tools can narrow the scope.
- Prefer skill-provided scripts, references, or workflows when a selected skill includes them and they fit the task.
