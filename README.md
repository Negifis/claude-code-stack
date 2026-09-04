# Claude Code Stack

A working Claude Code configuration built around one idea: **prompt rules decay, hooks don't.**

Instructions in `CLAUDE.md` are advisory — the model follows them until context gets long,
the task gets interesting, or a summary drops the paragraph that mattered. So the parts of the
workflow that must not drift are enforced by deterministic hooks that read the session
transcript and block the `Stop` event, while the parts that need judgment stay in skills.

This repo is the enforcement half plus the skills that pair with it. It is not a starter
template — it is a stack in daily use on Windows, published as-is.

---

## What's in here

| Directory | What it holds |
|---|---|
| `hooks/` | The Code Work Gate and its anomaly inbox, the continuity system, the comment-density guard, chip handoff and session hygiene — plus their regression suites (1155 gate assertions, 70 continuity checks, 48 guard cases, chip and hygiene suites) |
| `skills/` | 16 skills for engineering workflow, verification, writing, delegation, chips and task start |
| `agents/` | The adversarial reviewer, the single `simplify-reviewer` lane, and an `Explore` profile that overrides the built-in one |
| `commands/` | `/adversarial-review`, `/adversarial-review-internal`, `/checkpoint`, `/rebuild` |
| `tools/` | `worktree-audit.mjs` — parks unsaved work and prunes stale worktrees (the `Setup` maintenance hook) |
| `reference/` | On-demand docs the model reads only when relevant — model routing, Codex routing, config layout, and the September 2026 usage optimization with its measured baseline |
| `output-styles/` | `dense` — the terse output style the whole setup assumes |
| `rules/` | Path-scoped rules (UI/UX rules that load only for front-end files) |
| `CLAUDE.md` | The global instructions the hooks enforce |
| `install.py` | The installer, with its own suite in `test_install.py` (80 assertions) |

---

## The hook systems

### 1. Code Work Gate

A `Stop` hook that refuses to let a session finish claiming code work is done when the
protocol it claims to have followed was never observably run.

It does **not** perform judgment. It checks a small set of facts that either appear in the
transcript or don't:

- the `development-verification` skill was invoked once for this session;
- for a high-risk candidate, one foreground `simplify-reviewer` result exists for the
  candidate (the three legacy lens names are still accepted);
- for a high-risk candidate, an independent adversarial review produced a verdict, and that
  verdict is *newer* than the last change to a lasting artifact;
- the review verdict came from a session that was actually briefed with the reviewer role —
  printing the string `VERDICT: APPROVED` without running a review does not pass;
- the candidate reached a terminal state instead of trailing off.

Two review engines are accepted: external **Codex** (default) and the bundled native
`adversarial-reviewer` subagent (fallback). One lane per round, never both.

The Codex lane is verified against the Codex CLI's own rollout logs, not against the text of
the command that was typed. The log is read with a binary search over an append-only,
chronological file, so a review round buried in the middle of a multi-megabyte resumed
session is found without scanning the whole thing.

The gate has a **hard three-block cap per unchanged candidate**. It makes disobedience
explicit; it cannot create an unbounded loop.

The gate also speaks *before* the Stop event. `code_work_gate_mark.py` announces a candidate
once when it opens and once when its risk floor rises, and `code_work_gate_prompt.py` names
the open candidate and its receipt on every prompt, so the receipt shape is known long before
the model tries to finish. `codex_lane.py` is a circuit breaker for the Codex lane: a
usage-limit or capacity refusal printed by the Codex CLI is recorded once, with the retry time
the CLI named, and `/adversarial-review` skips the lane until then instead of paying several
full-context turns to rediscover the outage. The breaker chooses the engine; it is never read
as review evidence.

```bash
python hooks/test_gate.py
```

### 2. Continuity

Compaction is where long tasks quietly lose their requirements. Six hooks keep a task
alive across compaction, restarts and delegation:

- `continuity_session_start.py` — injects this directory's checkpoint on start/resume/clear/compact
- `continuity_prompt.py` — updates intent on every user turn, resets the loop counters
- `continuity_progress.py` — treats a landed edit as real progress
- `continuity_loop_guard.py` — counts identical tool calls inside one generation and stops the spiral
- `continuity_stop.py` — lints the final answer for correction residue ("actually", "as I said earlier")
- `continuity_subagent.py` — hands a delegated lane the current requirement, not the one it replaced

`/checkpoint` shows, writes or refreshes the checkpoint by hand.

### 3. Comment density guard

A `PreToolUse` guard on `Edit|Write` that rejects an edit which buries code under commentary,
or leaks the conversation into a comment (`# the user asked for this`, `# first attempt
failed`, `# before this change…`). Deterministic, local, no model call — a few milliseconds
on top of interpreter startup. Handles Russian text too.

Disable per-machine with `CLAUDE_COMMENT_GUARD=off` in the `env` block of `settings.json`.

```bash
python hooks/comment_density_guard_test.py
```

### 4. Chips and session hygiene

`spawn_task` hands a child session a prompt and a directory and no way back.
`chip_handoff.py` gives a chip its route home: a worktree and branch off the parent's HEAD
for code, a report for operational work, a message to the parent session, and the parent's
own verification before the child is archived or sent back. The `chip-handoff` skill is the
procedure; the `Stop` hook blocks a chip session that closes work without handing it back,
and reminds the parent once that a reported chip is unverified.

Three more hooks keep worktrees and sessions from silting up: `session_guard.py` warns when
another live session already holds the working tree, `session_index.py` keeps a register of
sessions that ended, and `worktree_snapshot.py` commits unsaved work to a `wip/` branch
before a worktree is removed. `tools/worktree-audit.mjs` runs the sweep on the `Setup`
maintenance hook or by hand.

```bash
python hooks/chip_handoff_test.py
```

```bash
python hooks/hygiene_hooks_test.py
```

---

## Skills

| Skill | When it fires |
|---|---|
| `engineering-workflow` | Before non-trivial code, config, schema, migration or API work |
| `development-verification` | Final checks and finite risk-based review before finishing implementation |
| `root-cause-engineering` | A bug, flaky test, regression, perf problem or production incident |
| `simplify` | A changed scope with a concrete readability, reuse, control-flow or efficiency concern — a local pass, or one `simplify-reviewer` lane carrying all three lenses |
| `chip-handoff` | Spawning a chip, finishing inside one, or accepting one that reported back |
| `task-start` | Starting work on a tracked issue in its own worktree, branch and named session |
| `current-docs` | Anything version-sensitive about a library, API, framework or CLI |
| `codebase-memory` | Structural queries — symbols, callers, call chains, change impact |
| `subagent-delegation` | Deciding whether a bounded lane is worth a subagent |
| `canonical-state` | A requirement changed, a handover is coming, or the same fix failed twice |
| `compact-instructions` | Writing or reviewing compaction and handoff summaries |
| `local-windows-tooling` | Shell commands on Windows |
| `text-quality` | Before finalizing any prose, including commit messages and code comments |
| `human-writing` | Prose that should read like a person wrote it |
| `claude-code-uiux-design` | Anything the end user sees |
| `codex-imagegen` | Image and illustration generation routed through Codex |

---

## Install

Requires Claude Code ≥ 2.1.246 (the floor for the `Explore` override, `maxTurns` and
`autoCompactWindow`; the template's `minimumVersion` says the same) and Python 3.11+;
`tools/worktree-audit.mjs` needs Node 18+.

```bash
git clone https://github.com/Negifis/claude-code-stack.git && cd claude-code-stack
```

Look before you leap — the dry run reports every file it would add or replace:

```bash
python install.py --dry-run
```

Then install. Files that already exist and differ are copied into
`~/.claude/backups/stack-install-<timestamp>/` before being replaced:

```bash
python install.py
```

That writes `~/.claude/settings.stack.json` with the hook paths resolved against your actual
interpreter and config directory, and leaves your `settings.json` alone. To merge the hooks
in automatically instead — your current settings are backed up first, unrelated keys are
preserved, and re-running never double-registers a hook:

```bash
python install.py --merge-settings
```

Restart Claude Code, then confirm everything runs:

```bash
python hooks/test_gate.py
```

```bash
python test_install.py
```

### Optional

- **Codex review lane.** The default adversarial-review engine. Needs Node 18.18+, the Codex
  CLI (`npm install -g @openai/codex`), and the `codex@openai-codex` plugin. Setup is in
  `reference/codex-routing.md`. Without it the native `adversarial-reviewer` subagent takes
  the lane — the gate accepts either.
- **`codebase-memory` MCP.** The `codebase-memory` skill assumes a graph-index MCP server,
  Skip the skill if you don't run one.

---

## Configuration

`settings.example.json` is a template, not a file to edit. Its `__CLAUDE_DIR__` and
`__PYTHON__` placeholders are deliberately unquoted: the installer splits each command into
arguments and adds the quoting the launching shell actually needs, which is more than
quoting spaces — on Windows a path containing `&`, `(`, `^` or a dozen other legal filename
characters is shell syntax too.

So don't substitute by hand. Run `install.py` without `--merge-settings` and read
`~/.claude/settings.stack.json`: it holds the finished, correctly quoted hooks block, ready
to copy into your own `settings.json` if you would rather merge it yourself.

Worth knowing before you turn it on:

- The `dense` output style must keep `keep-coding-instructions: true` in its frontmatter.
  Without that flag Claude Code drops its built-in "Doing tasks" section — and that section
  carries the no-comments policy the whole density setup rests on.
- `CLAUDE.md` is installed only if you don't already have one. Merge it yourself otherwise.
- The permission allowlist is deliberately **not** shipped. Allowlists are personal and a
  borrowed one is a security hole.
- The plugin-updater hook is PowerShell, so it is registered on Windows only. Everything
  else in the payload is Python and runs anywhere, except the worktree audit, which runs
  under Node.
- `autoCompactWindow: "300k"` and `skillListingMaxDescChars: 320` ship in the template on
  purpose; the measurements behind them are in `reference/usage-optimization-2026-09.md`.
  Drop them if your sessions are short.
- The installer refuses a config path it cannot quote safely for the launching shell. On
  Windows that means no `"`, no `%` and no `!` anywhere in the path: quoting does not stop
  `cmd.exe` expanding `%NAME%`, delayed expansion eats `!NAME!`, and a literal quote cannot
  be represented at all.

---

## September 2026: usage optimization

The stack was measured against 47 days of its own transcripts (134 sessions, 1,170 subagent
runs, 59k API requests) before the current shape was chosen. The findings and the changes
are in `reference/usage-optimization-2026-09.md`; the headline ones:

- context size, not subagents, was the cost — 90% of main requests ran at 150k–1M tokens,
  subagents were 6% of context tokens;
- the mandatory three-lens simplify wave cost three Sonnet contexts and nine minutes per
  candidate for findings that overlapped by 6%; it is now one lane, required for HIGH only;
- the Codex review lane failed to deliver a verdict in 60% of its launches, almost always on
  quota or capacity, and the native lane then ran anyway; hence the breaker;
- 29% of review delta rounds had no changed candidate between them, including 62
  "confirmation" reviews after an approval; the skill now forbids them;
- the built-in `Explore` inherited Opus; the override runs it on Sonnet with a turn cap.

---

## Not included

Third-party skills that live alongside this stack were left out — they belong to their
authors, and you should install them from the source:

- [avoid-ai-writing](https://github.com/conorbronsdon/avoid-ai-writing) — MIT
- [anti-ai-rhetoric](https://github.com/matteoroversi/anti-ai-rhetoric)
- [drawio-skill](https://github.com/Agents365-ai/drawio-skill) — MIT
- [geo-seo-claude](https://github.com/zubair-trabzada/geo-seo-claude) — the GEO/SEO skill family
- [claude-skill-tilda](https://github.com/JHamidun/claude-skill-tilda)
- [notebooklm-cli](https://github.com/jacob-bd/notebooklm-cli) — MIT, ships the `nlm-skill` NotebookLM skill
- the `superamped` marketing pack (ads, competitor research, content strategy)
- Anthropic's own bundled skills — `playwright`, `webapp-testing`, `theme-factory`
- Figma's `figma`, `figma-implement-design`, `figma-create-design-system-rules`

Also left out: the NotebookLM memory bridge. `CLAUDE.md` still carries its one paragraph
(pointing at `reference/notebooklm-memory.md`) and `reference/environment.md` still lists the
bridge, because both files are published exactly as they run locally; neither the reference
file nor the bridge is here, since they depend on tooling that isn't published. Delete that
paragraph, or ignore it, if you don't run NotebookLM.

---

## License

MIT — see [LICENSE](LICENSE).

[Русская версия](README.ru.md)
