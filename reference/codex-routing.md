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
  attributed from vanishing instead of standing as an unresolved lane.
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
    /codex:adversarial-review --wait CODE_WORK_GATE_REVIEW <specific risk focus>

## Codex model / effort routing

These govern the flags passed to **Codex**, not Claude's own model. Prefer relying on the
existing Codex config defaults in `~/.codex/config.toml` — do NOT pass `--model`/`--effort`
unless there is a specific reason to override. Current global default there: `gpt-5.6-sol`
at `medium`, with `plan_mode_reasoning_effort = "high"`. Native multi-agent is on
(`[features] multi_agent`, `[agents] max_threads = 6`, `max_depth = 1`); profiles `deep`
(Sol/high) and `max` (Sol/max) are the escalation presets.

GPT-5.6 is a three-tier family — pick the weakest tier that fits, a stronger one only for
judgment, not by habit:

- `gpt-5.6-sol` — architecture, ambiguous or high-risk changes, security, final review.
  Effort `medium`/`high`, rarely `xhigh`/`max`. (`gpt-5.6` with no suffix aliases this.)
- `gpt-5.6-terra` — the everyday worker: normal implementation, repo exploration, moderate
  refactors, docs. Effort `low`/`medium`.
- `gpt-5.6-luna` — repeatable/mechanical work: test runs, extraction, classification,
  formatting, bulk passes. Effort `none`/`low`, occasionally `medium`.
- `gpt-5.4-mini` — cheaper fallback for throwaway secondary passes when even Luna is overkill.

Codex's own native subagents (defined in `~/.codex/agents/`, routed by `~/.codex/AGENTS.md`)
already encode this split: `explorer` = Terra/low/read-only, `implementer` = Terra/medium/
write, `test_runner` = Luna/low, `reviewer` = Sol/high/read-only. When you delegate a broad
build to Codex, the parent Sol session fans these out — you don't address them directly.

Effort ladder, lowest to highest: `none | minimal | low | medium | high | xhigh | max |
ultra` (Codex CLI >= 0.143; older CLIs stop at `xhigh` and refuse a config that sets
`max`/`ultra`). The official Config Reference still lists only `…xhigh`, so keep portable
role files at `xhigh` or below. `max` gives one model more time on one task; `ultra` turns
the turn into a multi-agent workflow and costs far more tokens because each sub-agent reasons
independently. Neither is the default.

Reach for `ultra` only when the task splits into 2–3 genuinely independent lanes — a
security+tests+maintainability review, several unrelated services, code+docs+logs research,
comparing architectures, a bulk audit, a migration with separate schema/app/rollback agents.
Not for a one-method edit, a local bug with a clear repro, a rename, formatting, or anything
where every agent would touch the same files. Rule of thumb: read in parallel, write in
sequence; the parent coordinates rather than repeating its children. The bundled
`codex-plugin-cc` also caps its own `--effort` at `xhigh`, so `/codex:*` can't reach
`max`/`ultra` by flag regardless — that needs the config default.

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
    Expected output:
    - diff summary, files changed, commands run + results, remaining risks / skipped checks

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
