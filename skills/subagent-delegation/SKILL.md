---
name: subagent-delegation
description: Use Claude Code subagents for bounded independent exploration, implementation, specialist checks, or review when they materially improve the result.
---

# Subagent Delegation

Start with the primary agent. Delegate only when a bounded lane is independent enough to save
time, isolate verbose context, supply distinct expertise, or provide proportionate independent
judgment.

## Good lanes

- disjoint repository exploration with a named output;
- a non-overlapping implementation scope with one file owner;
- a focused specialist check;
- one read-only adversarial review for high-risk work;
- independent QA whose evidence can be checked by the parent.

Keep small, linear, tightly coupled, destructive, sensitive, and ordinary sequential work local.
Do not form an independent-review panel, duplicate a scope, or delegate merely because agents
are available. The named `simplify` skill may run its three specialized read-only lenses in
parallel; together they count as one bounded simplify pass.

## Task packet

Give each lane only current state:

- goal and concrete output;
- authoritative requirements and acceptance criteria;
- relevant files/evidence;
- explicit read/write scope and exclusions;
- verification expected from the lane.

Do not pass the entire conversation. The parent owns decomposition, requirements, integration,
verification, and the final answer.

## Ownership and review

- Keep one writer per file or tightly coupled scope.
- Review lanes are strictly read-only, do not delegate further, and do not start a code-work
  gate for their inspection.
- Check every returned claim against the diff, repository, command output, or runtime evidence.
  A subagent conclusion is not self-validating.
- Reuse the same lane for follow-up and send only the delta, open findings, and new evidence.
  Never create a replacement just because the first lane is slow.

## Model and effort

Use the agent profile's model by default. Override only when the lane has a clear cost or depth
requirement:

- a deterministic lookup or named command can use a small/fast tier;
- routine code exploration or bounded implementation can use a general coding tier;
- architecture, security, root cause, or adversarial review can use a stronger reasoning tier.

Start from configured effort. Increase it only for representative failures or material risk.
If an explicit model is unavailable, retry once with the closest supported tier or keep the work
in the parent; do not walk an open-ended fallback ladder. Record which tier actually ran when it
matters to the evidence.

## Bounded wait

When a result is required, wait once for up to ten minutes. If it is still running, send one
focused status/follow-up request and wait once more only when no useful local work remains.

If the lane is still unavailable, do not recreate it or wait indefinitely:

- for optional work, skip it and state the limitation;
- for a required high-risk review, record REVIEW_UNAVAILABLE and enter the autonomous closure
  in development-verification.
