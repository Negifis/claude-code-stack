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

Agent frontmatter is the source of truth for that agent's default `tools`, `model`, `effort` and
`maxTurns`. Read `model-routing.md` when a bounded lane needs an explicit override, and its Claude
Fable 5.1 section before writing the prompt of an agent that runs on `fable`; model routing is
guidance, not a mandatory gate. Do not maintain a second roster here.

Claude Code's documentation (https://code.claude.com/docs/en/sub-agents) says the next delegation
picks up an edited agent file. The 2.1.258 build did not do that on 2026-09-13: two reviewers
launched after their profile was edited described the old text as their instructions. On such a
build, treat an agent edit as taking effect only from a new session, and a lane launched in the
editing session as no evidence that the new prompt works.

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

## Hook registration

Every hook in `~/.claude/settings.json` is registered in exec form: `command` is the executable
itself (the Python below, `powershell.exe`, `node`) and `args` the argument vector, so Claude Code
spawns it directly instead of through Git Bash (2.1.139 and later; every build on this machine is
newer). In shell form a timeout killed `bash.exe` alone and could leave the Python it had started
suspended on the hook's pipe, the session waiting on it until someone killed it (21 minutes on
2026-09-18). In exec form a hook that outlived its 3 s timeout was gone, together with a native
child holding its stdout, and the tool call went on after 3.4 s (2.1.271, 2026-09-19). A new
registration keeps the form, and `test_gate.py` asserts it. A `.cmd` or `.bat` shim cannot be
spawned without a shell: register the script under its interpreter. The codex plugin's three
hooks stay in shell form: the plugin owns them, and its update would overwrite an edit.

The gate's own hooks get more time than their work takes: the marker 20 s before a shell command,
10 s before an edit and 15 s after either (a failed command included), the Stop hook 20 s. Their
internal budgets stay at a few seconds, so the headroom only absorbs a slow start of Python and git
on a loaded machine: with 10 s before a shell command, 15 shell commands in 11 sessions had no
pre-command snapshot on 2026-09-18, and a hook cut off at its timeout leaves its command unmeasured.

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
| `hooks/code_work_gate_mark.py` | `PreToolUse`/`PostToolUse`/`PostToolUseFailure` on edits and shell | Marks the candidate (paths, class, risk floor); on `PostToolUse` injects one line when a candidate opens or its floor rises, and records a Codex outage from a finished foreground `codex exec` call (the Stop hook records it for a background lane, from the stderr capture the command named, when its notification brings no verdict). A verdict expires through an unresolved pre-command snapshot only for a write-capable command: anything not proven read-only (an allowlist per pipeline segment, a separator inside quotes not counting as one — `ls`, `cat`, `grep`, `wc`, git's reading subcommands, the `gh` and `glab` forms that only talk to the server…; a redirect counts as writing unless it lands in a discarded stream, a merged stderr or a throwaway file — an absolute throwaway path with no `.` or `..` step, spelled literally or through a bash variable the same command gave one, not a tool's variable and not inside a pipeline; a heredoc's body is data unless an unquoted delimiter lets `$(` or a backtick in it run, `$(cat <one file>)` only reads, and any other substitution, a here-string, bash's `$'…'`, a heredoc operator behind a `#` or next to a line continuation, or a script block counts as writing; a `>`, brace or parenthesis inside quotes is text, and a quoted option is still that option (report dc30d302)). A command that is exactly the Codex review lane's launch (`review_launch_only`: `REVIEW_ID=<literal>`, at most a `cd`, and one bare `codex exec [resume <id>]` with only the template's options, its packet on stdin and its stderr in a throwaway file — Git Bash `/c/…` paths read as their drive — joined by `;`, `&&` or a newline, carrying `CODE_WORK_GATE_REVIEW`) counts as not writing, wherever it starts, so the lane's own launch no longer expires its verdict for a candidate outside any repository (report a5767180), a resumed round naming its session before or after the options or asking for `--last` included (report 27c9dcd0); a pipe, a background `&`, a substitution, another program or option, or a `CODEX_*`/`OPENAI_*` variable (tool variables everywhere now) makes it an ordinary command. Bookkeeping tools that write only their own state count as not writing: `nlm-memory recall|remember|stats|doctor|status|maintain` (also as `nlm-memory.cmd`, a path, a bash variable the same command assigned that literal path, not inside a pipeline, or the bridge it runs, `notebooklm-sync/bin/nlm_sync.py`, under an interpreter — the direct call the `project-memory` skill gives for text cmd.exe would parse, report c2a8dfe0; `rollback`, `init`, `migrate`, `sync` and the rest stay write-capable), a PowerShell variable given a literal (of the `$env:` names only `PYTHONUTF8` and `PYTHONIOENCODING`), and `gate_inbox.py`/`codex_lane.py` — and `chip_handoff.py finish|status|close`, which merges only in a scratch worktree under `state/chips` and moves a branch no checkout holds (report 44855de6); `open` stays write-capable — under an interpreter, with free text in quotes (report e5533b32). The marker also keeps a fingerprint of its lasting paths at every durable change (`content_marks`), so a verdict given before an edit that was reverted byte for byte still covers the candidate; an intent-to-add entry (`git add -N`, the empty blob) is no staged content, so staging and committing the reviewed file over it changes nothing. At a receipt that vouches for the bytes (`verified`, `operational`, `no-change`, `pr-ready`) the Stop hook keeps the closed candidate's lasting files and their content fingerprint (`closed_content` in its state; an UNVERIFIED close clears it); while no candidate is open, a resolved command that names only those files and leaves every byte of them as they closed — a commit or a clean rebase, a push in the same command — opens nothing and leaves a `settled` event in the ledger (report 9dbbfe70). A bare push names no file and still opens an operational candidate. A merge is judged by content: when a merge was in progress before the command or is after it, a path the index and the disk hold exactly as `git merge-tree` computes it from the HEAD the pre-command snapshot recorded and a commit reachable from a remote's default branch (`refs/remotes/<remote>/HEAD`) is neither recorded nor graded, in the merge, in the commit that concludes it, and in an abort that returns to HEAD what the merge had staged (a path staged and nothing else before the abort, clean after it, never recorded by the candidate); a clean merge that changes the candidate's own files leaves a content mark naming the content it replaced (`merge`), and a verdict that covered that content covers the merge. A rebase, or a merge committed by the same command, rewrites the candidate's committed files on a clean tree, which neither snapshot lists (report c4c78b99). Before a command that names `rebase`, `merge` or `pull`, the snapshot takes the fingerprint of the open candidate's content from the files alone, without git (within 8 s of the hook's start; for clean files it is the full fingerprint); after it, the candidate's files are carried with the same mark when that fingerprint equals the last mark's, every lasting file lies in the repository the command started in and is clean before and after it (a file the candidate deleted earlier counts as unchanged while neither the merged tree nor the index holds it; a sparse checkout's skip-worktree entry is held), HEAD moved by a merge commit on the HEAD before the command or by a rebase of it run start to finish (read from the worktree's own HEAD reflog, `pull --rebase` included) onto an upstream commit, `git merge-tree` merges the two without a conflict, HEAD's whole tree is that merge, and the index and the disk hold it for each lasting file. Any other HEAD move — a checkout, a reset, a rebase onto a feature branch, a hand resolution anywhere in the tree, an alias the words miss — is left to the Stop hook's catch-up. The trust is in the ref, not the author: the session's own push to the default branch, a local-clone remote's checked-out branch and a remote-tracking ref moved by hand all read as upstream. A merge of any other branch, an octopus and a path resolved by hand are recorded as before, and only the repository the command starts in is judged, within 3.5 s of the hook's start (on a loaded machine the judge runs out and the merge's files are recorded as before); a merge that commits in one step leaves no listing to judge, so a candidate file it changed shows at the next measurement. A mark whose pre-command snapshot never arrived (its PreToolUse hook was cancelled or failed) says so (`no_snapshot`). A shell command run inside a read-only subagent (the hook's `agent_type`: `adversarial-reviewer`, `Explore`, `Plan`) is recorded only for what the snapshot measured: a write there counts as anyone's, while a command the snapshot could not resolve neither opens a candidate nor expires the verdict the lane is producing (report a1c7b71b). A file written with Edit/Write into a subdirectory of a drive-root temp directory (`C:/tmp/<dir>/…`) that is no repository is a throwaway and is not recorded; a clone there is a repository and stays gated. The snapshot covers the repository the command starts in, the configuration homes, up to two other repositories holding the candidate's lasting paths, each lasting file outside any repository, and up to two repositories the command itself changes into by a literal path (`cd`, `pushd`, `Set-Location`; a bash variable counts when the same command assigned it a literal value and nothing since could have reassigned it; no glob, pipeline, subshell or heredoc); a command whose first executing segment follows only assignments, comments and such a change starts in that directory when it is a repository (`REVIEW_ID=r; cd <repo> && codex exec …` run from C:/tmp), and the repository of the directory the hook was given stays covered as well. Trees beyond the hook's time budget (4 s of PreToolUse, 2.5 s of PostToolUse) are left out and count as unmeasured; the mark records how many (`skipped`). A command that ends in a repository none of them covers stays unresolved, and its content mark and ledger line keep the command's label and where it ended (`landed`). A change under a configuration home the command neither ran in nor names (another session's edit seen through the shared home) is neither recorded nor graded; for a home change another session announced or was writing beside, the directory holding the home — the user's home directory — is not inside it, so a command run there takes no floor from it unless it names the home, its own name (`.claude/…`, `./.claude/…`) included, while a change nobody else accounts for still counts as the command's — the cost being that a session's own unspelled write takes no floor when it lands on the very file another session announced, beside another session's shell in that home, or while the claims registry overflowed (report b26e3641); a chip worktree is not its home's ground, and the hooks' own bookkeeping scripts do not name the home when an interpreter starting the segment runs them (past its options: `py -3`, `python -X utf8`, PowerShell's `& '…python.exe' '…gate_inbox.py'`). A settings file whose content, read without the keys the app writes from its own interface (`outputStyle`, `model`, `effortLevel`, `theme` and the like: `APP_SETTINGS_KEYS`), is the same before and after a command was rewritten by the app, not by the command, and is neither recorded nor graded (reports aa7603a1, 551e104e: an output-style switch landed in two sessions' candidates at HIGH); what stays open is a command that itself changes only those keys through the shell. A cycle the same branch resumes after the idle limit keeps its rounds and its spent block and wait budgets alike — the accepted trade for not losing review evidence overnight; a cycle that replaces an open one records it in `displaced` (when, which identities, which files), and the candidate note and the Stop hook's block text say so. |
| `hooks/code_work_gate_prompt.py` | `UserPromptSubmit` | One line naming the open candidate's class, floor and receipt shape; silent when nothing is open. |
| `hooks/code_work_gate_stop.py` | `Stop` | The finite validator: skill invoked, simplify lanes by risk (one `simplify-reviewer` for STANDARD, the three lenses for HIGH), legal review transitions, fresh approval for HIGH, receipt; three blocks per unchanged candidate. A backgrounded Codex lane is bound at its `<task-notification>` to the verdict one briefed Codex session stated in the rollout log between launch and notification (the output file is not evidence; without the marker's packet capture the packet file itself binds when it was last written before the launch); a backgrounded native reviewer is judged from the result its notification carries, filed at the launch, and whatever the lane does after stating its verdict — stopped, killed, resumed without one — is activity after it; a round that `SendMessage` resumed on a reviewer (its id from the launch acknowledgement or the foreground result's `agentId:` trailer) is a lane of its own, keyed by the `<tool-use-id>` its notification carries and filed at the `SendMessage`; a closure result with no round-3 ESCALATE before it is review activity, never a closure; an ESCALATE before round 3, counted from the last APPROVED, is read as that round's REVISE, so the review continues instead of standing without a legal move (report 6d8e2c4c); a READY that a later lasting change or barrier made stale retires with the closure passes before it, as a stale APPROVED retires its rounds, so the next pass starts a fresh budget; a READY past the pass cap retires nothing, and nothing retires through a READY that still covers the candidate (report 27c9dcd0); an operational candidate also closes as `verified` at the risk it declares; a turn may end while the session's own background task is in flight — an agent it resumed with `SendMessage` included, reviewer or not, until that agent's notification (report ac2ee4da) — (eight waits per candidate, two hours per task, `TaskStop`/`failed` recorded as failed activity). An `operational` or `no-change` receipt closes a candidate that changed lasting files once git shows them undone: HEAD on the opening commit, every ref but the branches where it was — tags, the stash, notes (branches are left out: a repository's worktrees share one ref store and other sessions move theirs, report 2b8bbfb1; a fetch moves only remote-tracking refs), no commit made here since the opening command started — a commit, merge, cherry-pick or rebase pick in this worktree's own HEAD reflog, not already under the opening commit — on a local branch or a remote-tracking ref (left on another branch, or pushed and then reset away or left on a deleted branch, keeps it open, while a checkout, a rebase probe or a fast-forward onto fetched commits does not; with HEAD's reflog off nothing is checked), none of the candidate's paths gitignored (asked of `git check-ignore --stdin -z` in one call; a code other than 0 or 1 is no answer, report f82c87c7) or differing from HEAD; files it never touched do not keep it open, unless it ran a command the snapshots could not resolve, which asks for the whole tree clean (report fb6a9be6). A block carries what the hook read — when evidence counts from and which candidate that replaced, what follows the last approval, the rounds read, what keeps a repository from reading as restored — and the ledger's `review` lines keep a background verdict's filing time as `at` and its notification as `notified`. A content mark a clean upstream merge left (`merge`) keeps current every verdict that covered the content it replaced. The marker keeps the size and modification time of its lasting paths from each measurement (`content_stats`) and when they last matched (`content_stats_at`, refreshed by every stop that finds them unchanged); when a Stop finds them changed and the content differs from the last mark — an edit made while a marker hook was cancelled — it adds a mark and moves the freshness anchor there: at the latest modification time of the changed files when all of them were modified after that last match, otherwise (a file gone, a copy that kept an older time) at the moment it looks. A verdict from before the mark is then stale and one from after it covers, and the block says no marker hook measured it (report 8db8b3d2), naming what it cannot tell apart: a hook cancelled or timed out, a checkout, reset, merge or rebase that was no clean integration of upstream, a write from outside the session's tools. The measurement gets 3 s of the hook, each git call only what is left; the marker is written back only if no marker hook wrote in between. What stays open: a kept time between the last match and a verdict, a lasting file outside the recorded paths, a change undone before any stop. Past the path cap nothing is compared. Closing retires the marker before the block state and sweeps last, so a hook killed at its timeout mid-close leaves either nothing changed, and the same receipt closes next time, or a closed candidate. |
| `state/gate-events.jsonl` | written by the gate hooks | Append-only ledger of gate decisions (`close`, `wait`, `block`, `review`, `durable`), one JSON line each, rotated once at 1 MB. Read it to see why a session was blocked without replaying the transcript. |
| `hooks/codex_lane.py` | CLI + used by the marker | Circuit breaker for the Codex lane: `check` prints `CODEX_LANE: available` or the recorded outage; `record`/`clear` by hand. State in `state/codex-lane.json`. |
| `hooks/gate_inbox.py` | CLI + `SessionStart` (digest) | Anomaly inbox `state/gate-anomalies.jsonl`: `report` files an agent's disagreement with a block (with the marker, the state, the session's ledger tail and the Stop hook's own view of the transcript); `scan` derives anomalies from the ledger by fixed rules; `list`/`show`/`ack` triage; `register` makes the calling session the gate-ops session (`state/gate-ops-session.json`) that `report` tells the agent to message with `mcp__ccd_session_mgmt__send_message`; `digest` injects the unresolved ones into a session started from `~/.claude` as the fallback. |
| `hooks/test_gate.py` | by hand | Regression suite for all of the above. |

Simplify lanes, all Sonnet, medium, `maxTurns: 40`: `agents/simplify-reviewer.md` covers reuse,
quality and efficiency in one report and is the STANDARD pass; `simplify-reuse-reviewer.md`,
`simplify-quality-reviewer.md` and `simplify-efficiency-reviewer.md` are the three lenses a HIGH
candidate runs as separate lanes, and the complete trio also satisfies STANDARD.
`agents/Explore.md` overrides the built-in Explore agent with a Sonnet profile. The measured
cost baseline behind the lane calibration: `reference/usage-optimization-2026-09.md`, data in
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
| `hooks/worktree_snapshot.py` | `WorktreeRemove` | Unsaved work is committed to `wip/<name>-<date>` before the worktree and its branch are deleted. Built with `write-tree`/`commit-tree`, never a checkout, so it also works in a worktree stopped mid-merge — where `git switch -c` refuses and the earlier version lost the work. A failure is printed and logged, never silent. Logged in `state/worktree-snapshots.jsonl`. Dormant for now: Claude Code 2.1.236 fires the event (with the tree in `worktree_path`) only for a worktree a `WorktreeCreate` hook built, none is configured, and its own worktrees it removes itself after unlinking their reparse points. |
| `hooks/session_index.py` | `SessionEnd` | Appends the session, its cwd and branch to `state/session-index.jsonl` — the register the audits read, since session metadata is not otherwise on disk. |
| `hooks/session_guard.py` | `SessionStart` | Warns when another live session already holds this working tree, and asks for `/rename #<issue> …` when the branch carries an issue number. Holders are tracked per tree in `state/tree-locks/`. |
| `tools/worktree-audit.mjs` | `Setup` (`maintenance`), or by hand | Lists worktrees holding unsaved work, stale clean ones, and sessions with no issue. `--fix` only snapshots and prunes, skips any tree a live session still holds, and parks work without moving HEAD. |

`Setup` fires on `claude -p --maintenance`, which needs a build newer than 2.1.179 — the
hook is registered and simply never fires until then. The weekly `charon-hygiene` scheduled
task drives the same two scripts meanwhile.

Git for Windows walks into a live junction inside a tree that `git worktree remove` deletes
and deletes the target's contents: on 2026-09-19 one removal emptied a live session's tree
through a `node_modules` junction. `hygiene_common.unlink_directory_links` removes every
directory reparse point under a tree (junctions, directory symlinks) without entering it, read
from attributes any Python 3 reports; `chip_handoff.py` runs it
before each of its removals, and by hand it is
`python hooks/hygiene_common.py unlink-links <tree> && git worktree remove <tree>`, which the
audit prints for stale trees. The command refuses a main checkout and exits 1 when a link will
not go.

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
`by-tree/<tree-key>` for the child's own worktree and `by-parent/<id>` for the parent's
reminder, so each hook path costs a keyed file open and never a directory scan. `by-parent`
holds only what is still pending — acceptance removes the entry — so `status` reads the cards
themselves instead. A directory is deliberately not an index: two sessions share a checkout
routinely, and an operational chip runs in its parent's own directory, so treating possession
of a directory as parent authority would let a stranger, or the chip's own child, accept it.
Every write to a card or an index happens under `state/chips/.lock` (`chip_lock`, a mkdir lock
with a 3s wait and a 60s staleness break); a writer that cannot take it drops its bookkeeping
rather than bury another party's verdict.

**Two id spaces, and they do not convert.** A hook payload carries only the transcript session
id (`state/session-index.jsonl`, `~/.claude/projects/**/<id>.jsonl`); the session-management
tools use a `local_…` id that is a different uuid entirely. So `by-parent` is written under
both — the `local_…` id the parent passes to `open`, and the transcript id `open` reads from
`CLAUDE_CODE_SESSION_ID` — and `archive_session` only ever accepts the `local_…` form, which
the child must supply itself via `finish --child-session`. A transcript id recorded by a hook
is kept separately as `child_hook_session` and is never offered as an archive target.

`open` is not something anybody has to remember: `hook-spawn` runs as a `PreToolUse` hook on
`mcp__ccd_session__spawn_task`, cuts the chip there and returns `updatedInput` carrying the
handoff block and the chip's worktree as the child's `cwd`. It picks code mode whenever the
parent's directory is a repository. Both ids of a session are resolved through the app's own
registry (`%APPDATA%/Claude/claude-code-sessions/**/local_<id>.json`, which pairs `sessionId`
with `cliSessionId`), cached in `state/session-map.json` for five minutes; that is also what
lets a resumed parent — new transcript id, same `local_…` id — still be reminded about chips it
opened earlier.

The chip is cut before the tool runs, because the child needs its directory to exist the moment it starts, so it stays `pending` until `hook-spawned` (PostToolUse) confirms the spawn landed; `hook-spawn-failed` (PostToolUseFailure) and a 10-minute sweep on the next spawn remove a chip whose tool was denied or cancelled, worktree and branch included. Idempotency is keyed on a `<!-- chip:<id> -->` token in the footer, not on the visible heading, so a task that merely quotes the heading is still registered.

Six registrations in `settings.json`: `PreToolUse` on the spawn tool (`hook-spawn`), `PostToolUse` and `PostToolUseFailure` on it (`hook-spawned`, `hook-spawn-failed`); `Stop`
(`hook-stop`), which blocks a chip session at
most three times when its final message carries a `[gate]` receipt but the work was neither
handed back nor even attempted, and otherwise lists — without blocking, and repeating until
each is closed — the chips waiting for the acceptance of the session that opened them; and
`mcp__ccd_session_mgmt__send_message` on both `PostToolUse` (`hook-notified`) and
`PostToolUseFailure` (`hook-notify-failed`), because a parent that runs unattended refuses
delivery outright, and a chip must not be held hostage to a send that cannot succeed.

## NotebookLM memory

`~/.codex/notebooklm-sync/bin/nlm_sync.py` is the bridge every agent's memory goes through; the
machine reference is `reference/notebooklm-memory.md`, the working guidance the `project-memory`
skill. Six registrations in `settings.json` run it, none of them touching the network:
`SessionStart` (`hook-session-start`, the memory card), `UserPromptSubmit` (`hook-user-prompt`),
`PreToolUse` on `^(?:WebSearch|WebFetch|Agent|Task|mcp__plugin_context7_context7__.*)$`
(`hook-pre-tool`), `PostToolUseFailure` on `Bash|PowerShell` (`hook-tool-failure`), `Stop`
(`hook-stop`) and `CwdChanged` (`hook-cwd-changed`); a seventh, `notebooklm_mcp_guard.py`, guards
the MCP server. The network work runs in the Task Scheduler task `\NotebookLM\Memory maintenance`
(`bin/install-maintenance-task.ps1`). The regression suites are
`~/.codex/notebooklm-sync/tests/` — run with `python -m pytest -q` from that directory.

## Windows tooling

Raw ripgrep when plain `rg` is unavailable:
the absolute path to `rg.exe`.
Python for hooks and scripts:
the absolute path to the interpreter the hooks were installed with.
Route shell specifics through the `local-windows-tooling` skill.

`tools/file-holders.ps1 -Path <file>…` names the processes holding a file open, through the
Restart Manager API: `handle.exe` is not installed, and `Get-CimInstance` cannot see file handles.

## codebase-memory-mcp indexing

Two builds are installed: 0.10.8 in `~/.local/bin`, which every current MCP registration and
the git reindex hooks use, and 0.6.0 in `%LOCALAPPDATA%\Programs\codebase-memory-mcp`, still on
`PATH` after `.local\bin`. A project's index is `~/.cache/codebase-memory-mcp/<project>.db`.

`cli index_repository` exits 1 with `"status":"error"` and the hint "Pipeline failed. Check
repo_path exists…" whenever another process holds that `.db` open. The hint is misleading, the
mode makes no difference, the old index stays untouched, and the failed run leaves no log. The
CLI has no wait, timeout or lock-coordination option. Established on 2026-09-19 on
one project: the same repository indexed in 9 s under a fresh `--name`, and that fresh index
failed the same way as soon as a plain file handle was held on its `.db`, with or without
`FILE_SHARE_DELETE`.

Who holds the file decides what a failure means:

- A 0.10.8 server (a stdio front per session, one shared `--cbm-daemon-internal` daemon) opens a
  project's `.db` only for the length of a query. A collision with one is transient: retry on
  the next trigger.
- A 0.6.0 stdio server keeps every project `.db` it has queried open until its session ends. On
  2026-09-19, one such server pinned a project's `.db`: its session started before the user
  removed that project's 0.6.0 override from `~/.claude.json` (a backup of that file still shows it). Every reindex fails until that
  session ends. The index had not refreshed since 2026-08-29. Killing the process removes the
  session's codebase-memory tools, so ending or restarting the session is the user's call.

Automation that triggers a reindex treats this failure as "not refreshed, try on the next
trigger": it is not a hook error and does not warrant a retry loop, since a 0.6.0 holder lasts
for hours. Before calling it a bug, run `file-holders.ps1` on the `.db`: a failure with no
holder is a real pipeline fault.

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
