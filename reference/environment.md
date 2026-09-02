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
| Config backups and rollbacks | `~/.claude/backups/<stage>/` (`usage-optimization-20260902/` holds the pre-optimization copies) |
| Usage baseline (historical transcript statistics) | `~/.claude/state/usage-baseline/` |
| Codex counterpart (do not rely on importing it) | `~/.codex/AGENTS.md` |
| NotebookLM sync bridge | `~/.codex/notebooklm-sync/bin/nlm_sync.py` |

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

## Code Work Gate files

| Piece | Event | Role |
|---|---|---|
| `hooks/code_work_gate_mark.py` | `PreToolUse`/`PostToolUse`/`PostToolUseFailure` on edits and shell | Marks the candidate (paths, class, risk floor); on `PostToolUse` injects one line when a candidate opens or its floor rises, and records a Codex outage from a finished `codex exec` call. |
| `hooks/code_work_gate_prompt.py` | `UserPromptSubmit` | One line naming the open candidate's class, floor and receipt shape; silent when nothing is open. |
| `hooks/code_work_gate_stop.py` | `Stop` | The finite validator: skill invoked, one simplify lane for HIGH, legal review transitions, fresh approval for HIGH, receipt; three blocks per unchanged candidate. |
| `hooks/codex_lane.py` | CLI + used by the marker | Circuit breaker for the Codex lane: `check` prints `CODEX_LANE: available` or the recorded outage; `record`/`clear` by hand. State in `state/codex-lane.json`. |
| `hooks/test_gate.py` | by hand | Regression suite for all of the above. |

The simplify pass is one lane, `agents/simplify-reviewer.md` (Sonnet, medium, `maxTurns: 40`),
covering reuse, quality and efficiency in one report; the Stop hook still accepts the legacy
three lens names. `agents/Explore.md` overrides the built-in Explore agent with a Sonnet
profile. Rationale and the measured baseline: `reference/usage-optimization-2026-09.md`, data in
`state/usage-baseline/`.

## Guard

`hooks/comment_density_guard.py` runs as a `PreToolUse` hook on `Edit|Write`, with
its regression suite alongside it as `comment_density_guard_test.py`. `CLAUDE_COMMENT_GUARD=off`
in the `env` block of `settings.json` disables it; a variable set in a tool shell does not
reach it.

## Session hygiene

Three hooks and one tool keep worktrees and sessions from silting up, so that the audit of
2026-08-28 does not have to be repeated by hand. They share `hooks/hygiene_common.py`, and
their regression suite is `hooks/hygiene_hooks_test.py` (run it with the Python above).

| Piece | Event | What it guarantees |
|---|---|---|
| `hooks/worktree_snapshot.py` | `WorktreeRemove` | Unsaved work is committed to `wip/<name>-<date>` before the worktree and its branch are deleted. Built with `write-tree`/`commit-tree`, never a checkout, so it also works in a worktree stopped mid-merge — where `git switch -c` refuses and the earlier version lost the work. A failure is printed and logged, never silent. Logged in `state/worktree-snapshots.jsonl`. |
| `hooks/session_index.py` | `SessionEnd` | Appends the session, its cwd and branch to `state/session-index.jsonl` — the register the audits read, since session metadata is not otherwise on disk. |
| `hooks/session_guard.py` | `SessionStart` | Warns when another live session already holds this working tree, and asks for `/rename #<issue> …` when the branch carries an issue number. Holders are tracked per tree in `state/tree-locks/`. |
| `tools/worktree-audit.mjs` | `Setup` (`maintenance`), or by hand | Lists worktrees holding unsaved work, stale clean ones, and sessions with no issue. `--fix` only snapshots and prunes, skips any tree a live session still holds, and parks work without moving HEAD. |

`Setup` fires on `claude -p --maintenance`, which needs a build newer than 2.1.179 — the
hook is registered and simply never fires until then. The weekly `charon-hygiene` scheduled
task drives the same two scripts meanwhile.

Full archival can never be automatic: `archive_session` always asks the user, and the
built-in "auto-archive after PR merge or close" runs on GitHub PR monitoring through `gh`,
so it does nothing for a self-managed GitLab.

## Chip handoff

`hooks/chip_handoff.py` gives a `spawn_task` chip a route back to the session that spawned it,
under the `chip-handoff` skill. `open` records the parent branch and `sessionId` and, unless
`--operational`, cuts a branch and a worktree off the parent's HEAD; `finish` merges into the
parent branch when nothing holds it, writes a fallback bundle when it does not, and prints the
message the child sends with `mcp__ccd_session_mgmt__send_message`; `close --accept|--rework`
records the parent's verdict after the parent has checked the result, and `--accept` names the
child session for `archive_session`. Its regression suite is `hooks/chip_handoff_test.py`.

State lives in `state/chips/<chip-id>.json`, found through two index directories:
`by-tree/<tree-key>` for the child's own worktree and `by-parent/<sessionId>` for the parent's
reminder, so each hook path costs one keyed file open and never a directory scan.

Two registrations in `settings.json`: `Stop` (`hook-stop`), which blocks a chip session at most
three times when its final message carries a `[gate]` receipt but the work was never handed
back, and otherwise reminds the parent once — without blocking — that a reported chip is
unverified; and `PostToolUse` on `mcp__ccd_session_mgmt__send_message` (`hook-notified`), which
records that the parent was told and which session told it.

## Windows tooling

Raw ripgrep when plain `rg` is unavailable:
the absolute path to `rg.exe`.
Python for hooks and scripts:
the absolute path to the interpreter the hooks were installed with.
Route shell specifics through the `local-windows-tooling` skill.

## GitLab credentials in a Claude Code session

`glab auth login` stores the token in Windows Credential Manager, which a Claude Code
shell does not read: `glab auth status` reports the self-hosted GitLab host unauthenticated there
even after a successful interactive login, and `--insecure-storage` did not land a host
block in `%LOCALAPPDATA%\glab-cli\config.yml` either. The working route on this machine is
the environment variable — `GITLAB_TOKEN` (scope `api`), set persistently with `setx`.

`setx` writes the registry and leaves the current process untouched, so a session started
before it was set still sees nothing; the variable arrives only in sessions launched
afterwards. Check with `printenv GITLAB_TOKEN` before concluding the token is wrong.

`GITLAB_MCP_TOKEN` is a different, `mcp`-scoped PAT serving only the GitLab MCP endpoint.
That endpoint exposes ten tools and cannot update an issue, post a note, set a label or
merge — those need `glab` with `GITLAB_TOKEN`.
