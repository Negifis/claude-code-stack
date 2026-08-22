"""
Continuity — PostToolUse progress marker.

A landed edit is real progress: it opens a new generation, so the loop guard stops counting
the reads that led up to it, and it feeds the checkpoint nudge in the prompt hook.
Restricting this to the edit tools is the PostToolUse matcher's job.

Fail-open: any error still lets the tool call through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()


def main():
    data = cc.read_stdin_json()
    try:
        def apply(state):
            cc.bump_generation(state)
            state["edits"] = int(state.get("edits") or 0) + 1

        cc.update_state(cc.session_key(data.get("session_id")), apply)
    except Exception:
        pass
    cc.quiet()


if __name__ == "__main__":
    main()
