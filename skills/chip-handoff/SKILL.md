---
name: chip-handoff
description: Give a spawn_task chip a way back — own worktree and branch for code, a report for operational work, a message to the parent, and the parent's own verification before the child session is archived or sent back. Use when spawning a chip ("вынеси в чип", "отдельной задачей"), when finishing inside one, or when one reports back.
disable-model-invocation: false
---

# Give a chip a way back

`spawn_task` hands the child a prompt and a directory. Nothing carries the parent's branch,
the parent's session, or any route home, so a chip that succeeds still leaves its work in a
place the parent never looks. Worse, a code chip spawned into the parent's own directory edits
the tree a live session is using — the shared-tree failure `session_guard` exists to warn
about.

`hooks/chip_handoff.py` closes both ends. Use it for every chip, and let its own output tell
you the next command; the paths below are the shape, not something to retype from memory.

## Spawning one

Nothing to do: a `PreToolUse` hook registers the chip while `spawn_task` is being called, and
rewrites that call so the child starts in its own worktree with the handoff block already in
its prompt. Spawn the way you always would. Everything below is what happens next, and the
manual `open` remains only for a chip you are setting up by hand or one that needs
`--operational` against your judgement rather than the hook's.


1. Get the parent session id — `mcp__ccd_session_mgmt__get_session` with `"self"`, field
   `sessionId`. Without it the child cannot address its report and the parent gets no reminder.

2. Decide what the chip produces. **Code** — anything that leaves a diff — gets its own branch
   and worktree. **Operational** work — a command against a live system, a check, a
   restart — has nothing to pull, so it gets `--operational` and no worktree.

   ```bash
   python "C:\Users\in\.claude\hooks\chip_handoff.py" open --title "<заголовок>" --session <sessionId>
   python "C:\Users\in\.claude\hooks\chip_handoff.py" open --title "<заголовок>" --session <sessionId> --operational
   ```

   The code form cuts `chip/<slug>-<id>` off the current HEAD and adds a worktree for it under
   `~/.claude/state/chips/trees/`. Both forms record the parent branch and session and print a
   ready handoff block.

3. Call `spawn_task` with the printed block appended to the end of `prompt`. For a code chip
   `cwd` is the printed worktree — never the parent's own directory. For an operational chip
   pick `cwd` by the task. Everything above the block is the ordinary task description.

Outside a git repository the code form fails and says to use `--operational`.

## Finishing one

After the work is done and `development-verification` has closed it:

1. A code chip commits everything first — `finish` refuses a dirty tree rather than guessing
   what belongs to the chip. An operational chip verifies its effect against the system, as
   that skill's operational track requires.

2. Run the `finish` command from the handoff block. In a chip worktree it needs no arguments
   beyond the summary; an operational chip passes its `--chip <id>`. Always pass
   `--child-session` with your own `sessionId` from `get_session` with `"self"` — a hook can
   only see this session's transcript id, and `archive_session` does not accept that, so
   without it the parent can neither archive you nor send you back. It must be the real
   `local_<uuid>`; anything else is refused rather than stored.

   For code it merges into the parent branch when that branch is checked out nowhere, and
   otherwise leaves it alone — merging into a branch a live session holds would move the ref
   out from under that session's index. When it does not merge, it writes a bundle of the
   chip's commits as a second route. For operational work it prints the report.

3. Send the printed message to the parent with `mcp__ccd_session_mgmt__send_message` and the
   `session_id` from the handoff block. This is the step the parent usually sees; a branch
   notifies nobody.

   A parent that runs unattended — a scheduled task, a remote-dispatched session — cannot be
   messaged at all, and the send is refused by the target, not by you. That refusal is a
   finished handoff, not a failure: `finish` already wrote the report into the card, the
   parent's own Stop hook lists it as waiting, and the chip is released. Do not retry, do not
   look for another route, and do not leave the report only in your own transcript.

4. Do not archive the child session yourself. The parent decides, and may send it back.

## Accepting one

A report is a claim, not evidence. Chips reach you two ways: as a message, and — when the send
could not be delivered — as a line in your own Stop hook naming the chips still waiting. Both
oblige you equally; `status` lists them at any time, and a resumed or scheduled session should
drain it before picking the goal back up.

When a chip is waiting:

1. **Verify it yourself.** For code: read `git log --oneline <parent>..<chip-branch>` and the
   diff, and run the checks the changed boundary deserves — the child's own gate receipt is
   not a substitute. For operational work: check the effect on the system, not the child's
   description of it.

2. Then close it:

   ```bash
   python "C:\Users\in\.claude\hooks\chip_handoff.py" close --chip <id> --accept
   python "C:\Users\in\.claude\hooks\chip_handoff.py" close --chip <id> --rework "<что доделать>"
   ```

   Closing is not optional bookkeeping: until a chip is closed its parent is reminded again on
   every turn, because a report nobody acted on is the failure this exists to catch.

   `--accept` prints the child's `sessionId` when the child recorded one; archive that session
   with `mcp__ccd_session_mgmt__archive_session`, which asks the user for confirmation. When it
   prints no id — the child never passed `--child-session` — find the session by the chip's
   title in `list_sessions`; never pass the transcript id it offers as a hint, `archive_session`
   does not take that form. `--rework` prints the message to send back into the child session
   with `send_message` — the child is waiting for exactly that and should not have been closed.

`status` lists chips still waiting on somebody; pass `--session <sessionId>` for this
session's own.

## What enforces it

In a chip's worktree a `Stop` hook speaks only when the session closes out work — a `[gate]`
receipt in the final message — and blocks at most three times, naming the exact command. An
ordinary turn is never interrupted, and a chip that has reported and tried to deliver is
released even when delivery was impossible. `PostToolUse` and `PostToolUseFailure` hooks on
`send_message` record the attempt, and delivery when it succeeded. In the parent — and only in
the session that actually opened the chip — the same Stop hook lists the chips waiting for
acceptance and repeats, without ever blocking, until each is closed.

An operational chip has no worktree, so the child side has no Stop enforcement — its handoff
block and this skill are what carry it.

## What this deliberately does not do

- It does not push, open a merge request, or touch a remote. The chip's product is a local
  branch and a message; publication stays where `development-verification` puts it.
- It does not merge into a branch somebody is sitting on, and never force-merges a conflict.
  A conflicted merge is aborted and reported with the conflicting paths.
- It does not archive anything. `archive_session` always asks the user, and a chip sent back
  for rework must keep its session.
- It does not clean up worktrees. `tools/worktree-audit.mjs` and the `WorktreeRemove` snapshot
  own that, and both already protect unmerged work.
