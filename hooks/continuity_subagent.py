"""
Continuity — SubagentStart hook.

A delegated lane gets the task packet and nothing else, so it has no way to tell a current
requirement from one the user replaced three turns ago. This states the contract on the
lane's side: the packet is the authoritative state, contradictions get reported rather than
resolved silently, and the answer comes back as evidence — not as a narrative of attempts.

Cannot block; anything unexpected degrades to a silent pass-through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()

CONTRACT = (
    "[Delegated lane contract]\n"
    "- The task packet in your prompt is the authoritative state. Work only from it. What it does "
    "not state is unknown — name the gap in your answer instead of filling it with an assumption.\n"
    "- If what you observe contradicts the packet, report the contradiction explicitly with "
    "evidence; do not quietly pick a side.\n"
    "- Return findings, evidence, and file:line references. No narrative of your attempts, no "
    "rejected alternatives, no changelog of your own edits.\n"
    "- Your output is evidence, not the final answer: the main agent owns integration, final "
    "judgment, and what reaches the user."
)
CHECKPOINT_NOTE = (
    "\n- Background only (the packet wins on any conflict): task checkpoint at {path}."
)


def main():
    data = cc.read_stdin_json()
    try:
        context = CONTRACT
        found = cc.find_checkpoint(data.get("cwd") or os.getcwd())
        if found:
            context += CHECKPOINT_NOTE.format(path=found["path"])
        cc.emit_context("SubagentStart", context)
    except Exception:
        cc.quiet()


if __name__ == "__main__":
    main()
