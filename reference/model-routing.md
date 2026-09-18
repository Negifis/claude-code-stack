# Model Routing Guidance

On-demand guidance, not a gate. The primary agent starts with the configured session model and
effort. Override them only when a bounded lane has a clear cost, latency, or reasoning
requirement.

## Choose by lane

| Lane | Profile / tier | Effort |
|---|---|---|
| Deterministic lookup, evidence collection, one named command | `haiku` (pass explicitly) | low |
| Repository exploration | `Explore` profile: Sonnet, `maxTurns: 50` | medium |
| Bounded simplify pass | `simplify-reviewer` for STANDARD, the three `simplify-*-reviewer` lenses for HIGH: Sonnet, `maxTurns: 40` | medium |
| Routine implementation, focused QA, web research | `general-purpose` with `model: "sonnet"` passed explicitly | medium/high |
| Architecture, security, root cause, adversarial review | `adversarial-reviewer` profile: Fable, `maxTurns: 60`; or Opus/Fable by explicit choice | high |
| Orchestration, integration, final decision, user answer | primary agent | session default |

Built-in `Explore` and `Plan` inherit the main session's model (capped at Opus); the custom
`agents/Explore.md` overrides the built-in with the Sonnet profile above, which is the
documented override mechanism for Claude Code 2.1.25x. `Plan` is left built-in: two uses in
seven weeks, both in plan mode where the strong model is the point. `CLAUDE_CODE_SUBAGENT_MODEL`
is deliberately not set globally (the wa-tg-tun-new project sets it locally to
`claude-sonnet-5`; see usage-optimization-2026-09.md): before 2.1.251 it overrode every agent's
own `model`, including the reviewer's, and the `minimumVersion` floor of 2.1.246 is below that.

The aliases are pinned in `settings.json` (`env`): `opus` -> `claude-opus-5`, `sonnet` ->
`claude-sonnet-5`, `fable` -> `claude-fable-5-1`. Raise the pins when a new generation ships;
every agent profile follows them. The exact aliases available to the current Claude Code
runtime are authoritative. Do not encode a machine-wide assumption that one named alias always
exists or that the most expensive tier is required for every review.

## Rules

- Start with the primary agent. Do not create a lane merely to route to another model.
- Agent definitions carry the default model, effort and turn budget. Use them unless the task
  packet names a concrete reason to override; `effort: max` is never a default for a lane.
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
verdict from another model is worth more than a second opinion from this one — when it is
available. When `hooks/codex_lane.py check` reports a recorded outage, the round goes to the
native reviewer instead, without a Codex launch. For implementation it stays
selective: broad or mechanical multi-file work, patch/test/debug loops, an independent
implementation pass — not every multi-file change. Never run Claude and Codex over the same
lane in the same round.

There is no third engine, and driving the ChatGPT web app would not make one. Among the things
OpenAI's Terms of Use say you may not do is «Осуществлять автоматическое или программное
извлечение данных или Выходных данных»; they separately forbid circumventing rate limits or
protective measures, and the termination clause reserves suspending or deleting the account for
breaching them (https://openai.com/policies/terms-of-use/, effective 1 January 2026). That is the
account Codex authenticates through, and this machine holds no OpenAI API key to fall back on.
The engineering objection points the same way: evidence produced and judged inside one session is
not independent — the same distinction a browser draws when it marks script-dispatched events
untrusted. When Codex cannot review, the fallback is the native reviewer, and the fix for a CLI
too old for its model is upgrading the CLI.

See codex-routing.md for the routing details.
