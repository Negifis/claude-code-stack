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
| `hooks/` | The Code Work Gate, the continuity system, and the comment-density guard — plus their regression suites (580 assertions and 48 guard cases) |
| `skills/` | 14 skills for engineering workflow, verification, writing and delegation |
| `agents/` | The adversarial reviewer and the three `simplify` lenses |
| `commands/` | `/adversarial-review`, `/adversarial-review-internal`, `/checkpoint`, `/rebuild` |
| `reference/` | On-demand docs the model reads only when relevant — model routing, Codex routing, config layout |
| `output-styles/` | `dense` — the terse output style the whole setup assumes |
| `rules/` | Path-scoped rules (UI/UX rules that load only for front-end files) |
| `CLAUDE.md` | The global instructions the hooks enforce |
| `install.py` | The installer, with its own suite in `test_install.py` (30 assertions) |

---

## The three hook systems

### 1. Code Work Gate

A `Stop` hook that refuses to let a session finish claiming code work is done when the
protocol it claims to have followed was never observably run.

It does **not** perform judgment. It checks a small set of facts that either appear in the
transcript or don't:

- the `development-verification` skill was invoked once for this session;
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

---

## Skills

| Skill | When it fires |
|---|---|
| `engineering-workflow` | Before non-trivial code, config, schema, migration or API work |
| `development-verification` | Final checks and finite risk-based review before finishing implementation |
| `root-cause-engineering` | A bug, flaky test, regression, perf problem or production incident |
| `simplify` | A changed scope with a concrete readability, reuse, control-flow or efficiency concern |
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

Requires Claude Code ≥ 2.1.117 and Python 3.11+.

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

`settings.example.json` uses two placeholders: `__CLAUDE_DIR__` and `__PYTHON__`. They are
deliberately unquoted, because the installer tokenizes each command and adds the quoting
the launching shell needs. Wiring it up by hand means doing that yourself — quote any path
containing a space, and escape backslashes for JSON — so prefer `install.py` and read the
`settings.stack.json` it produces if you want to see the result before merging it.

Worth knowing before you turn it on:

- The `dense` output style must keep `keep-coding-instructions: true` in its frontmatter.
  Without that flag Claude Code drops its built-in "Doing tasks" section — and that section
  carries the no-comments policy the whole density setup rests on.
- `CLAUDE.md` is installed only if you don't already have one. Merge it yourself otherwise.
- The permission allowlist is deliberately **not** shipped. Allowlists are personal and a
  borrowed one is a security hole.
- The plugin-updater hook is PowerShell, so it is registered on Windows only. Everything
  else in the payload is Python and runs anywhere.
- The installer refuses a config path it cannot quote safely for the launching shell —
  on Windows that means no `%` and no `"` anywhere in the path.

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

Also left out: the NotebookLM memory bridge referenced by an earlier version of `CLAUDE.md`,
since it depends on tooling that isn't published here.

---

## License

MIT — see [LICENSE](LICENSE).

[Русская версия](README.ru.md)
