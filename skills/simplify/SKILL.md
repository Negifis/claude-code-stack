---
name: simplify
description: Simplify a non-trivial changed scope when a concrete readability, reuse, control-flow, type/error, resource, or efficiency concern exists.
---

# Simplify

A bounded, behavior-preserving polish pass over the changed scope: clearer, more maintainable
code that fits the repository. Not fewer lines for their own sake, and not a ceremony.

## When

- The diff is non-trivial and has a concrete clarity or maintenance concern, repeated logic,
  an existing helper that would shrink it, or control flow, types, errors, resources or hot-path
  work that got harder to reason about.
- The user asks to simplify or refactor.
- `development-verification` requires it: a HIGH persistent candidate needs one foreground
  `simplify-reviewer` result. STANDARD and LOW work never require it.

Skip it for routine low-risk work, small obvious fixes, generated output and docs churn.
Never run it on operational work: a scratchpad or one-shot script that already executed, or a
command against a live system — nothing reads that code again. A script being promoted into a
repository is no longer throwaway and enters the ordinary rules.

## One pass, one lane

The main agent owns the pass and the edits.

- **Local pass** — for a small or narrowly bounded diff, the main agent reviews reuse,
  readability, control flow, type/error clarity, resource handling and avoidable cost itself,
  from the diff and nearby patterns. No subagent.
- **One lane** — for a non-trivial scope, or whenever the gate requires simplify evidence,
  launch exactly one `simplify-reviewer` agent with `run_in_background: false` set
  explicitly. It carries the reuse, quality and efficiency lenses in one report; the result is
  the gate's evidence. Give it the bounded changed scope (diff or commit range, files, nearby
  helpers worth knowing about), the invariants that must hold, and ask for file:line findings
  with behavior-preservation reasoning. Do not pass the conversation.

There is no three-lane wave. The August 2026 transcripts showed the three-lens trio costing
three Sonnet contexts and about nine minutes per wave, plus forced single-lens re-runs, while
the lenses' findings overlapped by 6% and were acted on in about half of the waves; one lane
covering all three concerns keeps the coverage at a third of the cost.

Useful moves: reduce needless nesting and redundant state; reuse an established local helper;
remove a wrapper that only adds indirection; consolidate genuinely duplicated logic without
widening ownership; fix misleading names; make error and resource lifecycles explicit; remove
repeated parsing, I/O, allocation, queries or no-op updates.

Reject aesthetic churn, speculative abstraction, dependency additions, semantic changes, test
weakening, and any suggestion whose equivalence cannot be established.

## Finite workflow

1. One pass over the changed scope: local, or the single lane.
2. Apply only concrete behavior-preserving improvements; rerun affected checks after edits.
3. For broad or high-risk work, one confirmation pass is allowed after accepted edits — reuse
   the same lane with only the delta. Maximum two runs of the lane per candidate, no exception.
4. Stop after a clean pass or after the second run. A new finding is recorded for the owning
   gate; it never opens a third pass.
5. A lane result that already exists for this candidate is the completed pass, whether or not
   this skill was invoked before it. Never re-run it to make the record look tidier, and never
   run it after review approval as a ritual.

If the foreground lane fails, retry it once. If the pass was optional, report the failure and
continue; if it was required, record `SIMPLIFY_UNAVAILABLE` and finish `DRAFT_BLOCKED`. Never
open a replacement lane, a background copy, or another wait loop.
