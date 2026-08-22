# Checkpoint

The checkpoint is the canonical state written down, so that a compaction, a restart, or a new
session costs one file read instead of a fresh investigation.

## Where it lives

- Default, and correct for almost everything:
  `~/.claude/state/checkpoints/<dir-name>-<hash>.md` — outside the repository, one per working
  directory.
- Project file `.claude/CHECKPOINT.md` — only when the task is long, the repo is the natural
  home for it, and the team benefits. Check `.gitignore` first and do not commit working state
  without a real reason.
- Exact path for the current directory:
  `python ~/.claude/hooks/continuity_checkpoint.py path`
  (`show` prints the contents, `template` prints the empty form). The SessionStart hook also
  states the path every time a session opens.

When both exist, the project file wins only while it is usable — active, with a real NEXT
ACTION, not stale, not too long to inject. Otherwise the global one is used, and the file that
lost is named in the restored context.

## Format

```text
GOAL
The finished result.

STATUS
active — or done, once the task is finished.

AUTHORITATIVE STATE
Current requirements and confirmed facts only.

COMPLETED
What is actually done and verified.

CURRENT STATE
Files, tests, implementation as they stand.

NEXT ACTION
Exactly one concrete step.

ACCEPTANCE
How to tell it is finished.

BLOCKERS
Real current blockers only.

STOP CONDITION
When this work ends.
```

Superseded requirements are deleted from the file, not annotated. A checkpoint that carries
its own history is the drag it was meant to remove.

`STATUS` and `NEXT ACTION` decide whether the file can be resumed from at all: a checkpoint
marked done, or one with an empty next action, is only ever offered as background. Mark
STATUS done when the task ends, so the next session in that directory does not inherit it.

## When to update it

- A logical stage finished.
- Context is about to be compacted (a long session, a large diff, a big tool dump).
- Requirements changed materially.
- Before handing work to a subagent, and again after integrating what it returned.
- After tests ran or the diagnosis changed.
- Before the session ends with the task unfinished.

Small linear tasks do not need one. A checkpoint nobody will read is noise.

## Recovery

How far a restored checkpoint is trusted depends on how the session started. After a
compaction or a resume the task genuinely continued, so a live checkpoint is current state.
On a fresh start or a fork it may belong to a different task in the same directory, so it
arrives as an unverified lead — check it against the working tree and against what the user
is actually asking before resuming from it. After `clear` nothing is restored at all.

After a compaction, a restart, or picking up an old task:

1. Read the checkpoint (the SessionStart hook injects it; otherwise `continuity_checkpoint.py show`).
2. Check the working directory and any uncommitted changes.
3. Reconcile the checkpoint against the actual files and the last test results.
4. Continue from NEXT ACTION.
5. Do not re-survey the project when the checkpoint and the working tree already answer the
   question.
6. Re-investigate a settled area only on a concrete trigger: a contradiction, a changed file,
   or a failed check.

A goal without a current phase and a next action is not a restored state. When the checkpoint
is that thin, rebuild it — but from the working tree and the surviving context, not by
restarting the whole investigation.
