---
description: "Run one finite native Claude adversarial review lane; development-verification owns remediation and autonomous closure."
argument-hint: "[plan|code|file-path] [optional named risk]"
---

# Native Adversarial Review

This is the fallback review lane. `/adversarial-review` runs Codex and is the default; use this
one when Codex is unavailable, died before a verdict on its one allowed resume, or the user
declined it — and say so in the report, since the reviewer then shares the model that wrote the
code. A round the Codex lane started continues here on the same ledger and budget.

This command is a thin entry point. Read and follow the development-verification skill first.
That skill—not this command—owns risk classification, round limits, blocker policy, recovery,
publication, and the terminal receipt.

Raw arguments: $ARGUMENTS

## 1. Establish the candidate

- Respect an explicit plan/code/file argument.
- Otherwise review the current unstaged, staged, and branch diff against the repository's base
  branch. If there is no candidate, report that fact and stop.
- For operational work the candidate is the plan and its rollback path, reviewed before the
  command runs. A throwaway script that has already executed is not a review candidate.
- Freeze the exact candidate revision/diff, acceptance criteria, relevant project rules, and
  deterministic check results.
- Keep pre-existing or unrelated defects out of the verdict.

## 2. Launch one read-only reviewer

Use one adversarial-reviewer agent with `run_in_background: false` set explicitly. Do not launch
a panel. Every verdict-bearing call must return as a foreground tool result so the finite Stop
gate can bind it to the candidate across Claude Code versions.

The task packet must contain:

- review round number (1 to 3);
- exact candidate and base;
- acceptance criteria and risk classification;
- changed files or commands that reproduce the diff;
- decisive test/build/lint/runtime evidence;
- named attack surface;
- stable finding ledger and remediation delta for round 2 or 3;
- explicit read-only scope and the required final verdict line.

The reviewer may return APPROVED, REVISE, or round-3 ESCALATE. Only CRITICAL, HIGH, and explicit
acceptance-criterion violations block unless the user set a lower threshold.

## 3. Return every result to the canonical state machine

After each foreground result, return control to `development-verification`; do not reproduce or
reinterpret its severity threshold, round cap, transitions, autonomous closure, authority
boundary, or terminal states here.

The command owns only reviewer runtime continuity:

- reuse the same reviewer when foreground continuation is supported;
- otherwise invoke the same profile with only the remediation delta, open ledger, affected
  interfaces, and new evidence;
- never send an unchanged candidate twice;
- when the skill reaches ESCALATE, continue immediately with its autonomous-closure section
  using this reviewer for closure validation.

## 4. Memory and reporting

NotebookLM context is optional evidence, never a prerequisite. Use injected context first; make
at most one focused query when a prior decision could change the verdict. Persist only reusable
terminal findings after the gate completes, best effort.

Do not dump every reviewer message into the conversation. Summarize blocking findings, fixes,
ledger dispositions, exact checks, and the terminal state. End with the receipt required by
development-verification.
