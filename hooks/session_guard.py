"""
Session hygiene — SessionStart hook: one working tree, one session.

Two sessions sharing a working tree is the single failure that produced the rest. Each one
sees the other's edits as its own: the audit of 2026-08-28 found a session whose Stop gate
demanded review of a diff it never wrote, and a user who reopened a session, found somebody
else's changes in it, and started over. The tree is shared state; nothing warned anybody.

So the guard takes a lock per working tree and, when the tree is already held by a live
session, says so in the one place the model will read it — SessionStart stdout becomes
context. It also names the branch and, when the branch carries an issue number the session
name does not, asks for a rename. It never blocks: SessionStart cannot, and a second session
in one tree is sometimes exactly what the user wants.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hygiene_common as hc  # noqa: E402

hc.configure_utf8_streams()

ISSUE_IN_BRANCH = re.compile(r"(?:^|[-_/])(\d{2,6})(?:$|[-_/])")


def issue_from_branch(branch):
    """The issue number in a branch name, if it carries one.

    Dates are stripped first: `claude/weekly-whats-new-2026-08-28` otherwise reads as issue
    #28 and the guard asks for a rename that would mispair the session in the audit. The same
    stripping lives in `scripts/agent/session-audit.mjs`, which pairs by this number.
    """
    if not branch:
        return None
    without_dates = re.sub(r"\d{4}-\d{2}-\d{2}", "", branch)
    without_dates = re.sub(r"\d{8}", "", without_dates)
    matches = ISSUE_IN_BRANCH.findall(without_dates)
    return matches[-1] if matches else None


def main():
    payload = hc.read_payload()
    if payload is None:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id")

    branch = None
    held = None
    try:
        is_git, branch = hc.git_branch_of(cwd)
        if is_git:
            held = hc.claim_tree(cwd, session_id, branch)
    except Exception:
        pass

    lines = []
    if held and held.get("session_id"):
        lines.append(
            "Another live session already works in this directory "
            "(session {}, branch {}).".format(
                str(held.get("session_id"))[:16], held.get("branch") or "unknown"
            )
        )
        lines.append(
            "Edits made here land in that session's tree too, and each session's Stop gate "
            "will attribute the other's changes to itself. Start isolated work with "
            "`claude --worktree <name>` instead, or continue only if you mean to share."
        )

    issue = issue_from_branch(branch)
    if issue:
        lines.append(
            "Branch {} looks like issue #{}. If this session is not named for it, "
            "run `/rename #{} <short title>` so the audit can pair them.".format(
                branch, issue, issue
            )
        )

    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
