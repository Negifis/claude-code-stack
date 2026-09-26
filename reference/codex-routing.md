# Codex Routing — Reference (on-demand)

Reference for delegating a bounded, distinct result to OpenAI Codex through the
`codex-plugin-cc` plugin. Read it when routing an implementation lane here, or when running the
adversarial review, for which Codex is the default engine.

Codex is not the default worker for all multi-file changes. Keep one owner per lane, never run
Claude and Codex over the same lane in the same round, and return to the Claude parent for
integration and the final decision.

## When to delegate (recap)

Delegate only when Codex can own one bounded, distinct result and that isolation materially
helps: a substantial non-overlapping implementation/debug lane, a requested cross-engine
opinion, recovery after a concrete failed approach, or an independent diagnosis/review that
adds evidence for a named risk. Mechanical work, a multi-file diff, or the need to run tests is
not sufficient by itself; keep it in the Claude parent when it remains one ordinary sequential
lane.

## Commands

- `/codex:rescue` — implementation, debugging, test fixing, root-cause work, or
  continuing substantial coding. Proactive equivalent: the `codex:codex-rescue` subagent.
- `/codex:review` — read-only review of current changes or a branch diff.
- `/codex:adversarial-review` — review that challenges design, assumptions, tradeoffs,
  security, reliability, rollback, data-loss, race-condition, and auth risks.
- `/codex:transfer` — create a persistent Codex thread from the current Claude session.
- `/codex:status` — check running/recent Codex jobs.
- `/codex:result` — retrieve a finished Codex job.
- `/codex:cancel` — cancel a running Codex job.
- `/codex:setup` — one-time setup (auth + config check).

## Flags

- `--background` (async) / `--wait` (sync) — always pass one for review/rescue so the
  user is not asked to choose an execution mode. A review that has to satisfy the gate takes
  `--wait` plus the literal `CODE_WORK_GATE_REVIEW` in its arguments: a detached run returns only
  a launch acknowledgement, and the marker is what keeps a review that ran but could not be
  attributed from vanishing instead of standing as an unresolved lane. This plugin route loads
  the full local package — MCP servers, plugins, hooks — so for a gate review prefer the lean
  `codex exec` invocation in `/adversarial-review`, which turns off the MCP servers, plugins,
  hooks, memories and the browser, image and app tools while keeping the shell and web search.
  Skills and the global `AGENTS.md` still load either way — that command lists the residuals.
- `--fresh` (new task) / `--resume` (continue prior task) — pass one for rescue when the
  correct choice is clear.
- `--base <ref>` — base ref for branch review (e.g. `--base main`).
- `--model <id>` / `--effort <level>` — override Codex model/effort (prefer config
  defaults; see below).
- `--source <path>` — session file location for `/codex:transfer`.
- `--enable-review-gate` / `--disable-review-gate` — see "Review gate".

Typical invocations:

    /codex:rescue --fresh --background <self-contained task>
    /codex:rescue --fresh --wait <small bounded task>
    /codex:rescue --resume --background <follow-up>
    /codex:review --background                       # ad hoc, not gate evidence
    /codex:review --base main --background           # ad hoc, not gate evidence
    /codex:adversarial-review --wait CODE_WORK_GATE_REVIEW <risk focus>  # heavy; gate reviews
                                                                        # use the lean exec

## Codex model / effort routing

These settings govern the Codex child, not the Claude parent or its provider. The source of
truth is the active `~/.codex/config.toml`, its `*.config.toml` profiles and native role files
under `~/.codex/agents/`. Read the relevant settings instead of maintaining another model
catalog in this reference.

The user selects Sol for native Codex work and keeps Luna where it was chosen: the `explorer`
role and the `low` and `research` profiles run on `gpt-6-luna`. The root baseline is `gpt-6-sol`
with `medium` effort. Discovery uses the configured `explorer` role, deterministic checks use
`test_runner` at `low`, bounded implementation uses `implementer` at `medium`, and independent
review uses `reviewer`/`adversarial-reviewer` at `high`. Higher effort is justified by a concrete
unresolved question after collecting evidence; return to the sufficient lower effort afterward.
Do not silently fall back to another model when Sol is unavailable.

Use the companion's supported `--effort high` for an independent review and its baseline for
ordinary implementation. Verify the installed parser before using another level. The installed
codex-plugin-cc 1.0.6 accepts flags only through `xhigh`; do not claim its flag reaches `max`
or `ultra`. Native Codex 0.156.1 supports `low`, `medium`, `high`, `xhigh`, `max`, `ultra` for
`gpt-6-sol`. Its existing `deep` profile selects high and `max` selects max; the `low` profile
selects low. Do not use `none` or `minimal` for Sol, enable Fast mode, or change billing
automatically.

For native invocations, `codex --profile <name>` layers the matching
`$CODEX_HOME/<name>.config.toml`. A `-c model_reasoning_effort=<level>` override applies to that
invocation. A running task keeps its own settings until changed through a supported task or
turn interface; editing a file alone does not prove adoption. Explicit child role settings
prevent accidental inheritance of a costly parent mode. Check runtime metadata, not model
self-identification, when proving the applied route.

## Codex task template

Give Codex a self-contained task:

    Goal:
    - ...
    Context:
    - current behavior / desired behavior
    Relevant files/directories:
    - ...
    Implementation instructions:
    - ...
    Constraints:
    - must preserve / must not change / compatibility / security / performance
    Acceptance criteria:
    - ...
    Validation:
    - commands to run
    Authorization:
    - what the task may do without asking, and the one point where it must stop
    Expected output:
    - diff summary, files changed, commands run + results, remaining risks / skipped checks

## Prompting GPT-6 (OpenAI's Astra guidance)

Source: OpenAI's model guidance for GPT-6 Astra,
https://developers.openai.com/api/docs/guides/latest-model. It names five behaviours in which
Astra differs from GPT-5.6 Sol. Codex lanes run on `gpt-6-sol`, and packets still follow these
answers: they cost nothing there and keep a packet valid if a lane runs on Astra.

- **It stops to ask more often.** Where more input could change the result it asks instead of
  assuming, and in a non-interactive `codex exec` run that ends the turn without the result.
  State the authorization in the task: a change is carried to completion, with the authorized
  work done first so that approval is only ever needed for the final external step; a review is
  read-only, fully authorized, and ends with its verdict.
- **It follows files more closely.** Skills and `AGENTS.md` weigh more, and unclear or
  conflicting guidance in them can pause it early. `~/.codex/AGENTS.md` states that the task
  outranks skill guidance and that a pause is reported with the file and the quoted instruction;
  keep that file free of contradictions and audit it first when Codex behaves unexpectedly.
- **It formats heavily.** Lists, tables and Markdown by default. Ask for plain paragraphs where
  prose is wanted, and give the exact output contract where structure is wanted.
- **It delegates less.** Say when subagents are expected; a review lane says never.
- **It tests broadly.** Name the checks a change needs; a reversible, low-impact change gets no
  tests that mirror its implementation.

Parameters: model `gpt-6-sol`; effort `low` to `max` (plus `ultra` in Codex), never `none` —
keep a lane's current effort when moving it to another model and raise it only on a measured
failure; the user config sets verbosity `medium`; the migration guide removes `temperature`,
`top_p` and `top_logprobs` from requests. Codex 0.156.1 lists `gpt-6-sol` and runs it on the
ChatGPT account; 0.154.0 refused it, so verify the actual executable used by the wrapper. The
plugin's bundled `gpt-5-4-prompting` skill predates GPT-6; for Codex tasks this section wins
where they differ.

## When neither engine can run

Codex is down or declined AND no Claude lane can take the work: emit a self-contained
`DELEGATE_TO_CODEX` text packet — goal, context, relevant files, constraints, acceptance
criteria, validation commands — and say plainly that the lane was not executed. An unexecuted
lane is never reported as a completed one.

## After Codex returns

Treat output as evidence, not truth. Check: diff matches the goal; design stays coherent;
edge cases and failure modes handled; tests are meaningful; security/migration/rollback/
compatibility covered; scope not broader than necessary. For non-trivial diffs, group
findings by severity: Blocker / High / Medium / Low / Nits. Accept and summarize, fix
small issues directly in Claude, or send a targeted `/codex:rescue --resume` follow-up.

## Review gate

Keep it OFF by default (`/codex:setup --disable-review-gate`): it can create a
long-running Claude/Codex loop and drain usage quickly. Instead trigger review explicitly
for important changes, e.g. `/codex:adversarial-review --wait CODE_WORK_GATE_REVIEW <risk
focus>`. Both parts matter to the Code Work Gate: `--wait` puts the verdict in the transcript,
and the marker — which reaches the companion's actual shell command through `$ARGUMENTS` — keeps
an unattributable review visible as an unresolved lane. The verdict itself is heard because the
Codex session was briefed with `agents/adversarial-reviewer.md` and produced that text.

## Setup (run once, by the user — these are interactive `/plugin` commands)

    /plugin marketplace add openai/codex-plugin-cc
    /plugin install codex@openai-codex
    /reload-plugins
    /codex:setup --disable-review-gate

Prereqs: Node.js 18.18+, ChatGPT subscription (incl. Free) or OpenAI API key, Codex CLI
(`npm install -g @openai/codex`), authenticated via `codex login`. Note: this plugin
installs into Claude Code (not Codex's own plugin system); it shells out to the local
`codex` binary and reuses the existing `~/.codex/config.toml` and auth.
