---
name: subagent-delegation
description: Use Claude Code subagents for bounded independent exploration, implementation, specialist checks, or review when they materially improve the result.
---

# Subagent Delegation

Start with the primary agent. Delegate only when a bounded lane is independent enough to save
time, isolate verbose context, supply distinct expertise, or provide proportionate independent
judgment. A second reasoning context is the expensive resource; a parallel deterministic tool
call is not.

## Good lanes

- disjoint repository exploration with a named output (`Explore`, or a project explorer);
- a non-overlapping implementation scope with one file owner;
- a focused specialist check;
- one read-only adversarial review for high-risk work;
- the single `simplify-reviewer` lane when `simplify` needs one;
- independent QA whose evidence can be checked by the parent.

Keep small, linear, tightly coupled, destructive, sensitive, and ordinary sequential work local.
Do not form a reviewer panel, duplicate a scope, or delegate merely because agents are
available. One optional lane is enough; add another only for a named, non-overlapping result.

## Task packet

Give each lane only current state: goal and concrete output; authoritative requirements and
acceptance criteria; relevant files/evidence; explicit read/write scope and exclusions; the
verification expected. Do not pass the conversation. The parent owns decomposition,
requirements, integration, verification, and the final answer.

## Ownership and review

- One writer per file or tightly coupled scope.
- Review lanes are strictly read-only, do not delegate further, and do not start a code-work
  gate for their inspection.
- Check every returned claim against the diff, repository, command output, or runtime evidence.
  A subagent conclusion is not self-validating.
- Reuse the same lane for follow-up and send only the delta, open findings, and new evidence.
  Never create a replacement just because the first lane is slow.

## Model and effort

The agent profile's model and effort are the default; the profiles in `~/.claude/agents` are
routed by lane already (`Explore` and `simplify-reviewer` on Sonnet/medium,
`adversarial-reviewer` on Fable/high). Override only for a clear reason:

- a deterministic lookup or a named command: `haiku`;
- `general-purpose` and any built-in lane without a profile: pass `model: "sonnet"` unless
  the task is a genuine root-cause, architecture or security judgment — otherwise it inherits
  the main session's model and effort, which is the most expensive combination available;
- architecture, security, root cause, adversarial review: a strong reasoning tier.

Prefer a bounded lane: state the expected size of the answer and stop conditions in the packet.
If an explicit model is unavailable, retry once with the closest supported tier or keep the
work in the parent; do not walk a fallback ladder. Record which tier ran when it matters to
the evidence.

## Bounded wait

When a result is required, wait once for up to ten minutes. If it is still running, send one
focused status/follow-up request and wait once more only when no useful local work remains.

If the lane is still unavailable, do not recreate it or wait indefinitely:

- for optional work, skip it and state the limitation;
- for a required high-risk review, record REVIEW_UNAVAILABLE and enter the autonomous closure
  in development-verification.
