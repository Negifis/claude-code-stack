---
name: canonical-state
description: When a requirement changes or is corrected, before delegating or handing over, on long tasks, after compaction, or when the same fix fails twice.
---

# Canonical State

One rule underneath everything here: **the current state of the task is the only state.**
Corrections replace, they do not accumulate. What the user last said outranks every earlier
reading of it, including your own.

## The principle

- The last explicit correction replaces the prior state; it is not layered on top of it.
- The current wording of the task outranks earlier readings, drafts, plans, and your own
  assumptions.
- Rejected options, fixed mistakes, deleted entities, and the history of corrections stay out
  of the deliverable unless the user asked for them.
- After a correction the result must read as if the corrected requirement had been there from
  the start.
- Do not write "not X, but Y" when Y alone is the answer.
- Do not name a forbidden or deleted thing in order to point out that it is gone.
- Turn a negative requirement into a positive description of what the thing now is: "no
  external deps" becomes "runs on the standard library".
- Once the user has replaced your interpretation, stop defending, explaining, or refining it.

## The state you carry

Hold exactly this, and rebuild it after every change of requirements:

goal · authoritative requirements · confirmed facts · reasonable assumptions · constraints ·
done **and verified** · current state · **one** next action · acceptance criteria · stop condition

Keep out: cancelled requirements, rejected options, discussion history, superseded
assumptions, already-fixed mistakes that no longer affect verification, and any explanation of
why the old approach was wrong.

When a correction lands: rewrite the state first, then work — only from it.

## Reading a correction

| Type | Signal | Effect |
|------|--------|--------|
| Replacement | "нет, не так", "я же просил", "переделай", "redo" | Old decision is deleted; rebuild the affected part |
| Addition | "ещё нужно", "плюс добавь", "also" | New requirement; everything else stands |
| Constraint | "только без X", "не трогай Y", "must not" | Solution space narrows; existing results are re-checked against it |
| Fact fix | "нет, порт 8443", "файл лежит не там" | Old fact is invalid everywhere it was used, including in conclusions drawn from it |

Ambiguous? Pick the most likely reading from the wording and the last few turns and proceed.
Ask only when every reading would be unsafe or would waste the work.

Precedence, highest first: safety and system constraints → the user's latest explicit
instruction → earlier instructions at the same level → your plan → an earlier draft → a
subagent's conclusion → the stored checkpoint.

## Phases

1. **Intent normalization** — what is being asked, right now.
2. **Evidence collection** — only the facts the next action needs.
3. **Execution** — real changes to real files.
4. **Verification** — against the acceptance criteria.
5. **Finalization** — the clean result, no internal history.

Do not fall back from Execution into full re-exploration without a concrete reason: a failed
check, a contradiction, or a file that changed. Planning is not progress when the user asked
for implementation and you already have enough to start.

## No-progress guard

Two consecutive near-identical actions that move none of these — files, working hypothesis,
confirmed facts, checkpoint, test results, blocker list, next action — mean the loop is
closed. Then:

1. Do not run it a third time. (A `PreToolUse` hook denies the third identical file read and
   warns on the third identical shell command — shell output can legitimately change, file
   reads within one generation cannot. Treat either as the signal, not as an obstacle to
   route around. If you are genuinely waiting on something external, say what you are waiting
   for and how you will know it changed.)
2. Name what produced nothing.
3. Switch source, tool, or method of verification.
4. Move to the next practical action.
5. If nothing can move, stop and state the blocker and what would unblock it.

Never loop on: re-reading the same file, re-searching the same symbol, re-running an unchanged
check, re-planning instead of editing, re-reviewing an area already accepted, or promising an
implementation without touching a file.

## Sub-procedures

- Long tasks, compaction, and restarts → `checkpoint.md`
- The same fix failed twice → `clean-room-rebuild.md`
- Handing the result to the user → `output-lint.md`
- Handing work to a subagent → `subagent-packet.md`

## What runs automatically

| Hook | Event | Effect |
|------|-------|--------|
| `continuity_session_start.py` | SessionStart | Restores the checkpoint — authoritative after compaction or resume, an unverified lead on a fresh start, not at all after `clear` |
| `continuity_prompt.py` | UserPromptSubmit | Detects corrections, injects this contract, escalates to clean-room after two in a row, nudges the checkpoint on long tasks |
| `continuity_loop_guard.py` | PreToolUse | Denies a third identical file read; warns on a third identical shell command |
| `continuity_progress.py` | PostToolUse | An edit ends the generation, clears the loop counters, and counts toward the checkpoint nudge |
| `continuity_stop.py` | Stop | Lints the final answer for edit-history residue in the few turns after a correction |
| `continuity_subagent.py` | SubagentStart | States the delegated-lane contract inside the subagent |

Commands: `/checkpoint`, `/rebuild`.
