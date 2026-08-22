---
name: simplify
description: Simplify a non-trivial changed scope when a concrete readability, reuse, control-flow, type/error, resource, or efficiency concern exists.
---

# Simplify

Perform a bounded behavior-preserving polish pass over the changed scope. The goal is clearer,
more maintainable code that fits the repository—not fewer lines and not a mandatory ceremony.

## When to use it

Use this skill when:

- the diff is non-trivial and has a concrete clarity or maintenance concern;
- repeated logic or an existing helper may materially reduce the change;
- control flow, types, errors, resource handling, or hot-path work became harder to reason about;
- the user explicitly asks to simplify or refactor.

Skip it for routine low-risk work, small obvious fixes, generated output, and static/docs churn
unless a specific concern makes a pass useful.

Never run it on operational work: a scratchpad or one-shot script that has already executed, or
a command issued against a live system. Nothing will read that code again, so there is no
maintenance to buy, and the outcome it produced is already fixed. If such a script is being
promoted into a repository, it is no longer throwaway and the ordinary rules apply.

## Scope and evidence

- Start from the current diff and recently modified files.
- Read applicable project instructions and nearby patterns.
- Use structural graph tools only when callers, ownership, reuse, or impact are unknown.
  Focused diff/search/file reads are enough for bounded work.
- Preserve public APIs, data formats, side effects, error semantics, and timing-sensitive
  behavior unless the user explicitly requested a behavior change.
- Expand only to a nearby helper, type, or test needed to complete a local simplification.

## One composite pass

The main agent owns the pass. It evaluates reuse, readability, control flow, type/error clarity,
resource handling, and avoidable cost together.

For a non-trivial changed scope, launch the three existing read-only reviewer profiles in the
same assistant turn and set `run_in_background: false` explicitly on every call. They execute as
one foreground wave and their completed results are observable by the finite Stop gate even
after Claude Code versions where omitted subagent mode defaults to background:

1. `simplify-reuse-reviewer` — missed local helpers, duplicated logic, repeated state/config/
   schema shapes, and abstraction fit;
2. `simplify-quality-reviewer` — naming, readability, control flow, type/error clarity, tests,
   comments, dead code, and local style;
3. `simplify-efficiency-reviewer` — redundant I/O, queries, loops, parsing, allocation,
   async/concurrency overhead, hot-path cost, and cleanup.

Together these lanes are one composite simplify pass. Give all three the same bounded changed
scope and require concrete file/line evidence plus behavior-preservation reasoning. Keep them
read-only and use their configured model/effort defaults. The main agent reconciles the output
and applies only accepted findings.

For tiny or narrowly bounded work, run the same lenses locally only when simplification is
optional. When `development-verification` requires observable simplify evidence — HIGH, or
STANDARD touching at least three gated files, both counted over lasting artifacts only — launch
the trio even for a narrow candidate.

Useful moves include:

- reduce unnecessary nesting and redundant state;
- reuse an established local helper;
- remove an abstraction or wrapper that adds only indirection;
- consolidate genuinely duplicated logic without widening ownership;
- improve misleading names;
- make error and resource lifecycles explicit;
- remove avoidable repeated parsing, I/O, allocation, queries, or no-op updates.

Reject aesthetic churn, speculative abstraction, dependency additions, semantic changes, test
weakening, and suggestions whose equivalence cannot be established.

## Finite workflow

1. Run one composite pass over the changed scope; for non-trivial work this is the parallel
   three-lens pass above.
2. Apply only concrete behavior-preserving improvements.
3. Rerun affected checks after actual edits.
4. For broad or high-risk work, one confirmation pass is allowed after accepted edits. Reuse
   only the lanes whose concerns the edits touched; do not relaunch the full trio by default.
5. Stop after a clean pass or after two runs of any one lens. A new finding is recorded for the
   owning gate; it never opens a third pass.
6. Lenses that already returned for this candidate are the completed pass, whether or not this
   skill was invoked before them. Never re-run them to make the record look tidier.

Do not convert this required wave to background tasks. If a foreground lens fails, retry that
same lane once. If the pass was optional, report a second failure and continue with the
candidate-bound gate; if the pass was required, record `SIMPLIFY_UNAVAILABLE` and finish
`DRAFT_BLOCKED`. Never open a replacement lane or another wait loop.

Do not run simplify after review approval as a ritual. If review remediation introduces a
specific complexity concern before the second invocation has been used, the parent may spend
that one remaining affected-lens confirmation. Once the absolute two-invocation cap is spent,
record the concern in the finite review/closure ledger and do not invoke simplify again.
