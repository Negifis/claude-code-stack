# Environment — Reference (on-demand)

Paths and facts that only matter when you are changing the agent configuration itself. Never
auto-loaded; the resident pointer is `CLAUDE.md` → Configuration Invariants.

## Layout

| What | Where |
|---|---|
| Personal skills | `~/.claude/skills/<name>/SKILL.md` |
| Path-scoped rules (always-loaded when they carry no `paths:`) | `~/.claude/rules/*.md` |
| On-demand reference (never auto-loaded) | `~/.claude/reference/*.md` |
| Personal subagents | `~/.claude/agents/*.md` |
| Output styles | `~/.claude/output-styles/*.md` |
| Hooks | `~/.claude/hooks/` |
| Task checkpoints | `~/.claude/state/checkpoints/<project-key>.md`, project override `.claude/CHECKPOINT.md` |
| Config backups and rollbacks | `~/.claude/backups/<stage>/` |
| Codex counterpart (do not rely on importing it) | `~/.codex/AGENTS.md` |

`~/.claude/projects/<project>/memory/` holds archived pre-migration files only. Native
auto-memory is disabled (`autoMemoryEnabled: false`) — do not read or write it.

## Subagent definitions

Agent frontmatter is the source of truth for that agent's default tools and model. Read
`model-routing.md` only when a bounded lane needs an explicit override; model routing is
guidance, not a mandatory gate. Do not maintain a second roster here.

## Output style

The global style is `output-styles/dense.md`, selected by `outputStyle` in
`~/.claude/settings.json`. It must keep `keep-coding-instructions: true` in its frontmatter:
without that flag Claude Code drops its built-in "Doing tasks" section from the system prompt,
and that section carries the default no-comments policy the whole density setup rests on.

## Settings scopes

Resolved from the 2.1.179 binary, not from convention:

| Source | File | Scope |
|---|---|---|
| `userSettings` | `~/.claude/settings.json` | every session — the only true global |
| `projectSettings` | `<cwd>/.claude/settings.json` | shared project |
| `localSettings` | `<cwd>/.claude/settings.local.json` | project, gitignored |
| `policySettings` | managed settings | enterprise, wins over everything |

`~/.claude/settings.local.json` is **not** a global file. It is the project-local file of the
`C:\Users\you` directory and applies only to sessions started there — which is why the
permissions Claude Code auto-saved from prompts in the home directory do not carry to other
projects. Every genuinely global parameter belongs in `~/.claude/settings.json`.

## Skill visibility

`skillOverrides` in `~/.claude/settings.json` controls what reaches the model's skill listing,
keyed by skill name:

| Value | Effect |
|---|---|
| `on` (default) | name and description in the listing |
| `name-only` | name in the listing, description withheld — model can still route by name |
| `user-invocable-only` | absent from the listing; `/name` and `Skill(name)` still work |
| `off` | absent from both |

Hiding a skill never deletes it. A router skill can still invoke a hidden one by name. The
inventory script that measures the listing cost lives with the stage-2 backup.

## Guard

`hooks/comment_density_guard.py` runs as a `PreToolUse` hook on `Edit|Write`, with
its regression suite alongside it as `comment_density_guard_test.py`. `CLAUDE_COMMENT_GUARD=off`
in the `env` block of `settings.json` disables it; a variable set in a tool shell does not
reach it.

## Windows tooling

Raw ripgrep when plain `rg` is unavailable:
the absolute path to `rg.exe`.
Python for hooks and scripts:
the absolute path to the interpreter the hooks were installed with.
Route shell specifics through the `local-windows-tooling` skill.
