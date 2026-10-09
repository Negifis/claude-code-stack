---
name: chip-handoff
description: For work outside the current task's scope (in-scope follow-ups go to a subagent). Give a spawn_task chip a way back — own worktree and branch for code, a report for operational work, a message to the parent, and the parent's own verification, then merge, accept and archive without asking the user, or send back. Use when spawning a chip ("вынеси в чип", "отдельной задачей"), when finishing inside one, or when one reports back.
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

A chip is for work outside the current task's scope. Follow-up work inside the scope goes to a
subagent (`development-verification` §4 defines the scope); neither kind is dropped as "not
mine".

## Spawning one

Nothing to do. A `PreToolUse` hook registers the chip while `spawn_task` is being called and
rewrites that call, so the child starts in its own worktree with the handoff block already in
its prompt. Spawn the way you always would.

It cuts a branch and worktree whenever the directory is a repository with a commit to branch
from, and otherwise registers an operational chip that reports instead. The chip stays
provisional until the spawn actually lands; a call that is denied or cancelled takes its
worktree and branch with it.

`open` remains for a chip you are setting up by hand:

```bash
python3 ~/.claude/hooks/chip_handoff.py open --title "<заголовок>"
python3 ~/.claude/hooks/chip_handoff.py open --title "<заголовок>" --operational
```

It prints the worktree to pass as `cwd` and the block to append to `prompt`. Session ids are
resolved on their own; pass `--session` only to override that.

## Finishing one

After the work is done and `development-verification` has closed it:

1. A code chip commits everything first — `finish` refuses a dirty tree rather than guessing
   what belongs to the chip. An operational chip verifies its effect against the system, as
   that skill's operational track requires.

2. Run the `finish` command from the handoff block. In a chip worktree it needs no arguments
   beyond the summary; an operational chip passes its `--chip <id>`. Nothing else: `finish`
   resolves your own `sessionId` through the app's session registry, so the parent can archive
   you or send you back without being told who you are. It names the chip by id, so run it from
   wherever the shell already is. Commits made in a worktree outside the chip's directory count
   once they are on the chip branch; `git -C <chip tree> merge --ff-only <branch>` puts them
   there without a `cd`.

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

4. Do not archive the child session yourself. The parent archives it after accepting, or sends
   it back.

## Accepting one

A report is a claim, not evidence. Chips reach you two ways: as a message, and — when the send
could not be delivered — as a line in your own Stop hook naming the chips still waiting. Both
oblige you equally; `status` lists them at any time, and a resumed or scheduled session should
drain it before picking the goal back up.

The user set this up so as not to supervise chips one by one: this skill is their standing
agreement to merge, accept and archive every chip that passes your verification, in the same
turn, without asking them before or after. Only a chip that fails verification goes back with
`--rework`.

When a chip is waiting:

1. **Verify it yourself.** For code: read `git log --oneline <parent>..<chip-branch>` and the
   diff, and run the checks the changed boundary deserves — the child's own gate receipt is
   not a substitute. Bringing the chip's commits into your own tree — a checkout, a merge —
   opens your own candidate at its floor, and the child's approval does not close it; a merge
   you resolved by hand is a delta candidate (`development-verification` §6). For operational
   work: check the effect on the system, not the child's description of it.

2. **Merge it**, by what the report says:
   - already merged («Влито в …», «… уже есть в …»), nothing to take, or an operational chip —
     nothing to do;
   - «Автомерж не выполнен» — run the `git merge --no-ff <branch>` it prints, in the tree it
     names once that tree is on the parent branch; on a conflict resolve it by hand, which
     makes the merge a delta candidate. When the reason is
     a rewritten parent branch, carry the commits over with `git cherry-pick -x` instead;
   - commits found off the chip branch or on a detached HEAD — merge or cherry-pick them when
     the diff you verified is the result, otherwise send the chip back.

   The merged result is your own candidate and closes under your own `development-verification`;
   once its checks pass, accept and archive in the same turn.

3. **Accept it and archive the child session:**

   ```bash
   python3 ~/.claude/hooks/chip_handoff.py close --chip <id> --accept
   ```

   `--accept` prints the child's `sessionId` when one was recorded; archive that session with
   `mcp__ccd_session_mgmt__archive_session` right away; an approval the app shows for the call
   is its own, not a question to repeat in chat. When it prints no id, find the session by the
   chip's title in `list_sessions` — an id that is not of the `local_<uuid>` form is refused
   rather than offered, because `archive_session` does not take it.

   A chip that fails verification is sent back instead, and keeps its session:

   ```bash
   python3 ~/.claude/hooks/chip_handoff.py close --chip <id> --rework "<что доделать>"
   ```

   `--rework` prints the message to send back into the child session with `send_message` — the
   child is waiting for exactly that.

   Closing is not optional bookkeeping: until a chip is closed its parent is reminded again on
   every turn, because a report nobody acted on is the failure this exists to catch.

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
- The script archives nothing itself: the parent archives an accepted chip's session with
  `archive_session` right after `--accept`, and a chip sent back for rework keeps its session.
- It does not clean up worktrees beyond its own merge tree and a refused chip's clean tree, and
  it unlinks a tree's junctions and directory symlinks before removing either: on Windows git
  follows a junction and deletes its target. `tools/worktree-audit.mjs` owns the rest and parks
  unmerged work on `wip/` branches; the `WorktreeRemove` snapshot is dormant (see
  `reference/environment.md`).
