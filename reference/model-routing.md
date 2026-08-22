# Model Routing Guidance

This is on-demand guidance, not a gate. The primary agent starts with the configured session
model and effort. Override them only when a bounded lane has a clear cost, latency, or reasoning
requirement.

## Choose by lane

| Lane | Typical tier | Typical effort |
|---|---|---|
| Deterministic lookup, evidence collection, or one named command | small/fast | low |
| Repository exploration, routine implementation, focused QA | general coding | medium |
| Architecture, security, root cause, adversarial review | strong reasoning | high |
| Orchestration, integration, final decision, user answer | primary agent | session default |

The exact aliases available to the current Claude Code runtime are authoritative. Do not encode a
machine-wide assumption that one named alias always exists or that the most expensive tier is
required for every review.

## Rules

- Start with the primary agent. Do not create a lane merely to route to another model.
- The `simplify` skill's named reuse/quality/efficiency trio is one bounded pass and may use
  the reviewer profiles' configured model defaults. It is not a reason to add more reviewers.
- Agent definitions may carry a default model. Use it unless the task packet names a concrete
  reason to override.
- Mechanical work inside a hard task can use a small tier; a security decision inside a simple
  task still needs strong reasoning. Route the lane, not the parent task's label.
- The parent checks every result and owns integration. Never delegate validation of a
  subagent's conclusion to another subagent.
- If the chosen model is unavailable, retry once with the nearest supported tier or keep the
  lane in the parent. Do not climb or descend an open-ended fallback ladder.
- Increase effort only for material risk, unresolved causal chains, or representative failures.
  Decrease it for fixed-shape work. Do not globally force maximum effort.
- Name the actual tier in the final evidence only when model independence or depth materially
  affects confidence.

## Relationship to external Codex

External Codex is a distinct runtime and the default adversarial-review engine, because a
verdict from another model is worth more than a second opinion from this one. For
implementation it stays selective: broad or mechanical multi-file work, patch/test/debug loops,
an independent implementation pass — not every multi-file change. Never run Claude and Codex
over the same lane in the same round.

See codex-routing.md for the routing details.
