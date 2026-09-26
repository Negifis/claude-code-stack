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
| XHIGH simplify pass | the three `simplify-*-reviewer-xhigh` lenses: `claude-opus-5-5`, `maxTurns: 80` | max |
| Routine implementation, focused QA, web research | `general-purpose` with `model: "sonnet"` passed explicitly | medium/high |
| Architecture, security, root cause, adversarial review | `adversarial-reviewer` profile: Fable, `maxTurns: 60`; or Opus/Fable by explicit choice | high |
| XHIGH adversarial review | `adversarial-reviewer-xhigh` profile: Fable, `maxTurns: 100` | max |
| Orchestration, integration, final decision, user answer | primary agent | session default |

Built-in `Explore` and `Plan` inherit the main session's model (capped at Opus); the custom
`agents/Explore.md` overrides the built-in with the Sonnet profile above, which is the
documented override mechanism for Claude Code 2.1.25x. `Plan` is left built-in: two uses in
seven weeks, both in plan mode where the strong model is the point. `CLAUDE_CODE_SUBAGENT_MODEL`
is deliberately not set globally (the wa-tg-tun-new project sets it locally to
`claude-sonnet-5`; see usage-optimization-2026-09.md): before 2.1.251 it overrode every agent's
own `model`, including the reviewer's, and the `minimumVersion` floor of 2.1.246 is below that.

The aliases are pinned in `settings.json` (`env`, `ANTHROPIC_DEFAULT_OPUS_MODEL` and its
`SONNET`/`FABLE` siblings): `opus` -> `claude-opus-5`, `sonnet` -> `claude-sonnet-5`, `fable` ->
`claude-fable-5-1`. Raise the pins when a new generation ships;
every agent profile follows them. The exact aliases available to the current Claude Code
runtime are authoritative. Do not encode a machine-wide assumption that one named alias always
exists or that the most expensive tier is required for every review.

## Rules

- Start with the primary agent. Do not create a lane merely to route to another model.
- Agent definitions carry the default model, effort and turn budget. Use them unless the task
  packet names a concrete reason to override; `effort: max` is never a default for a lane
  outside the XHIGH profiles, which the user set to max on 2026-09-26 for the work XHIGH covers.
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

## Claude Fable 5.1

Sources: Anthropic's prompting guide for the model,
https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1,
its what's-new page, the general prompting best practices,
https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices,
and Claude Code's model-configuration, subagent and output-style pages.

Parameters:

- 1M context; $10/$50 per MTok, cache reads $0.25. The `fable` alias follows the pin above.
- Effort `low`, `medium`, `high`, `xhigh`, `max`; the model's own default is `high`. In Claude
  Code a subagent's `effort` frontmatter takes all five and overrides the session; without it
  the lane inherits the session's effort, which `effortLevel` sets to `high` on this machine.
  The model-configuration page lists `low` through `xhigh` for that setting; `max` is set
  interactively with `/effort`.
- Effort names do not buy the same thinking across models, so calibration done on Fable 5 does
  not carry over. The guide starts at `high` and notes that `medium` roughly matches Fable 5 at
  lower cost. At `xhigh` and `max` the model may draft a long deliverable in its thinking before
  writing it, which lengthens the turn, so the guide keeps such requests at `high` unless a gain
  is measured. Move a lane only on a measured difference; the reviewer stays at `high` below
  XHIGH, and `adversarial-reviewer-xhigh` runs at `max` by the user's choice of 2026-09-26, not
  on a measured gain.
- Thinking is always on: Alt+T, `alwaysThinkingEnabled` and `MAX_THINKING_TOKENS=0` do not turn
  it off. `ultrathink` in a prompt asks for deeper reasoning on that one turn without changing
  effort.
- At the API, a non-default `temperature`, `top_p` or `top_k`, a prefilled assistant turn, a
  forced `tool_choice`, and `thinking` set to anything but adaptive each return 400.

Output styles reach only the main conversation and forks. A custom subagent runs its own prompt
plus the CLAUDE.md files, which serve every model, so a Fable lane's working rules belong in its
agent file or its packet. Six behaviours need an answer there:

- It batches tool calls less and may send one per turn, which pushes a lane toward its
  `maxTurns`. Ask it to list what it needs next and request every independent item in one
  response.
- It sometimes asks permission for work it was already given, and in a subagent a question ends
  the lane without a result. Say that the lane runs autonomously, what it may do, and that it
  states assumptions instead of asking.
- It writes less between tool calls, and the parent sees only the final message. Require that
  message to stand on its own.
- Its prose is denser and it formats less. Give the output contract where structure matters —
  sections, fields, the final line — and ask for direct statements where prose is wanted.
- It reproduces retrieved passages unmarked more often. A research lane rewords its sources,
  marks short quotes and gives the URL.
- In implementation work it widens scope — unrequested fixes, optimizations and tests — and
  rewrites whole files for small changes. Say that pre-existing problems are reported as
  follow-ups, and ask for targeted edits.

Two more from the guides. Prompts written to push earlier models ("be thorough", "if in doubt,
use the tool") now make it overtrigger, so state a rule once, plainly. Safety classifiers trip
more often on compile-check phrasing, lesser-known languages and base64 in tool output, so ask
"are there bugs in this code?" rather than "does it compile?".

`agents/adversarial-reviewer.md` carries these answers for the review lane. Its body also opens
every Codex review packet, so it stays model-neutral.

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
