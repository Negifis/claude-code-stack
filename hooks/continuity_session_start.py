"""
Continuity — SessionStart hook (compaction / restart recovery).

Injects the checkpoint for the current directory so a compacted or resumed session
continues from NEXT ACTION instead of re-deriving what was already settled.

How far it is trusted depends on how the session started:

- `compact` / `resume` — the task is genuinely continuing, so a live checkpoint with a next
  action is authoritative.
- `startup` / `fork` — this may be a completely different task in the same directory. The
  checkpoint goes in as an unverified lead.
- `clear` — the user asked for a clean slate. Only the path is mentioned; nothing is restored.

A checkpoint marked done, one with no next action, one past its staleness horizon, or one
that is not valid UTF-8 is never authoritative.

Cannot block; anything unexpected degrades to a silent pass-through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()

AUTHORITATIVE = (
    "[Continuity] Task checkpoint restored from {path} (updated {age}).\n\n"
    "{body}\n\n"
    "Resume from NEXT ACTION. AUTHORITATIVE STATE is current: re-verify only what the checkpoint "
    "marks unverified, what the working tree contradicts, or what a failed check exposes — do not "
    "re-explore the project or re-derive settled decisions. Any explicit instruction from the user "
    "outranks this file; if the user's request is clearly a different task, say so and leave the "
    "checkpoint alone."
)
COMPACTED = (
    "The context was just compacted, so this file — not the surviving summary — is the "
    "authoritative state.\n"
)
LEAD = (
    "[Continuity] There is a checkpoint for this directory at {path} (updated {age}{why}). Treat it "
    "as an unverified lead, not as current state: check it against the working tree and against "
    "what the user is actually asking for before resuming from it.\n\n{body}"
)
COMPLETED = (
    "[Continuity] The checkpoint at {path} is marked done (updated {age}). It is history, not "
    "current state — start from the user's request. If this task turns out to continue, rewrite the "
    "file with a live STATUS and a real NEXT ACTION."
)
UNREADABLE = (
    "[Continuity] A checkpoint exists at {path} but it is not valid UTF-8, so nothing was restored. "
    "Inspect it yourself before relying on it, or rewrite it."
)
CLEARED = (
    "[Continuity] Context cleared, so the stored checkpoint for this directory was not restored. It "
    "is at {path} if the new task turns out to be the same one."
)
MISSING = (
    "[Continuity] No task checkpoint for this directory. If this task runs beyond a few steps, write "
    "one to {path} using the canonical-state skill format — it is what survives compaction."
)
MISSING_AFTER_COMPACT = (
    "[Continuity] The context was just compacted and there is no checkpoint for this directory. "
    "Rebuild the canonical state from the surviving summary and the working tree before acting: goal, "
    "authoritative requirements, confirmed facts, what is already done and verified, the single next "
    "action, acceptance criteria. Then write it to {path} so the next compaction is cheap."
)


def describe_age(days):
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    return "{} days ago".format(int(days))


def alternate_note(found):
    """Name the checkpoint that was not used, so a wrong pick is visible rather than silent."""
    other = found.get("alternate")
    return ("\n\nAnother checkpoint for this directory exists at {} and was not used."
            .format(other)) if other else ""


def build(source, cwd):
    """The context to inject for this session start, or an empty string for none."""
    found = cc.find_checkpoint(cwd)
    if source == "clear":
        path = found["path"] if found else cc.global_checkpoint_path(cwd)
        return CLEARED.format(path=path) if found else MISSING.format(path=path)
    if not found:
        template = MISSING_AFTER_COMPACT if source == "compact" else MISSING
        return template.format(path=cc.global_checkpoint_path(cwd))

    age = describe_age(found["age_days"])
    if not found["readable"]:
        return UNREADABLE.format(path=found["path"])
    if found["done"]:
        return COMPLETED.format(path=found["path"], age=age)

    doubts = []
    if source not in ("compact", "resume"):
        doubts.append("this session did not resume that task")
    if not found["has_next_action"]:
        doubts.append("no NEXT ACTION recorded")
    if found["age_days"] > cc.STALE_AFTER_DAYS:
        doubts.append("past the staleness horizon")
    if found["truncated"]:
        doubts.append("too long to inject whole")
    if doubts:
        context = LEAD.format(path=found["path"], age=age, body=found["text"],
                              why=", " + "; ".join(doubts))
    else:
        context = AUTHORITATIVE.format(path=found["path"], age=age, body=found["text"])
        if source == "compact":
            context = COMPACTED + context
    return context + alternate_note(found)


def main():
    data = cc.read_stdin_json()
    try:
        context = build(str(data.get("source") or ""), data.get("cwd") or os.getcwd())
        if not context:
            cc.quiet()
            return
        cc.emit_context("SessionStart", context)
    except Exception:
        cc.quiet()


if __name__ == "__main__":
    main()
