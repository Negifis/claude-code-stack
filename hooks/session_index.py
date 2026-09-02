"""
Session hygiene — SessionEnd hook: keep a register of sessions that actually ended.

Nothing on disk says which sessions exist, what branch each worked on, or whether its work
reached a merge request: transcripts are an internal format, and the session-management tool
that knows the answer needs a running app and a confirmation prompt for every call. So the
weekly audit needs a register somebody writes, and the cheapest honest moment to write it is
when a session ends.

Also releases the tree lock the SessionStart guard took, so the next session in that
directory does not inherit a warning about a session that is gone.

Fail-open: SessionEnd cannot block, and a session must never fail to end because its
bookkeeping did.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hygiene_common as hc  # noqa: E402

hc.configure_utf8_streams()


def main():
    payload = hc.read_payload()
    if payload is None:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id")
    branch = None
    try:
        _, branch = hc.git_branch_of(cwd)
    except Exception:
        branch = None
    hc.append_jsonl(hc.SESSION_INDEX, {
        "ts": time.time(),
        "session_id": session_id,
        "cwd": os.path.abspath(str(cwd)),
        "branch": branch,
        "reason": payload.get("reason"),
        "transcript_path": payload.get("transcript_path"),
    })
    hc.clear_tree_lock(cwd, session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
