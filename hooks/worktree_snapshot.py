"""
Session hygiene — WorktreeRemove hook: never let a removal take work with it.

Claude Code removes a worktree and its branch when a session exits, when a subagent
finishes, and when a background session is deleted. Its own periodic sweep spares a worktree
that still holds work, but an explicit removal does not: the directory and the branch go,
and with them every uncommitted file and every unpushed commit.

So before the removal, this hook parks whatever is there on a branch of its own —
`wip/<name>-<date>` — which survives because it is not the branch being deleted. Committing
is the only form of preservation that outlives the directory; a stash entry would live in
the shared object store but hang off no ref the user would ever find.

The commit is built with plumbing — `write-tree`, `commit-tree`, `branch` — and never checks
anything out. `git switch -c` refuses outright in a worktree with a merge or rebase in
progress ("cannot switch branch while merging"), which is exactly the state an abandoned
worktree tends to be in, and a refusal there meant the removal took the work. Plumbing has no
such precondition and leaves HEAD, the branch and the working tree untouched.

Fail-open by design — WorktreeRemove cannot be blocked and the removal proceeds either way —
but never silent about it: a failure is printed and recorded in the snapshot log, because the
one thing worse than losing the work is losing it without anyone noticing.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hygiene_common as hc  # noqa: E402

hc.configure_utf8_streams()

# The shared default is sized for the 10s session hooks. This hook has 120s and its probes
# decide whether any rescue happens at all, so on a cold or scanned filesystem they get room
# to answer rather than a budget that turns slowness into "nothing to save".
SNAPSHOT_PROBE_TIMEOUT = 60

def unique_branch(path, base):
    ok, _ = hc.git(path, "rev-parse", "--verify", "--quiet", base)
    if not ok:
        return base
    for suffix in range(2, 50):
        candidate = "{}-{}".format(base, suffix)
        ok, _ = hc.git(path, "rev-parse", "--verify", "--quiet", candidate)
        if not ok:
            return candidate
    return None


def pending_files(path):
    """Paths git reports as changed or untracked, or None when git could not answer.

    The distinction is the whole point: an empty list means "nothing to rescue" and ends the
    hook, so a failed or timed-out `status` returning `[]` would impersonate a clean tree and
    the removal would take the work in silence — the exact outcome this hook exists to
    prevent, arrived at through a probe instead of a commit.
    """
    ok, out = hc.git(path, "status", "--porcelain", timeout=SNAPSHOT_PROBE_TIMEOUT)
    if not ok:
        return None
    return [line for line in out.splitlines() if line.strip()]


def unreachable_commits(path, branch):
    """Commits that would disappear with this branch.

    Mirrored as `unreachableCommits` in `~/.claude/tools/worktree-audit.mjs`, which answers
    the same question from Node for the audit; change both or neither.

    An upstream answers it directly when there is one. Without an upstream the question is
    not "is this pushed" but "does anything else reach it", because Claude Code deletes the
    branch along with the worktree — so the fallback counts commits reachable from HEAD and
    from no other local or remote ref.
    """
    ok, out = hc.git(path, "rev-list", "--count", "@{upstream}..HEAD",
                     timeout=SNAPSHOT_PROBE_TIMEOUT)
    if ok and out.isdigit():
        return int(out)
    ok, refs = hc.git(path, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes",
                      timeout=SNAPSHOT_PROBE_TIMEOUT)
    if not ok:
        return None
    own = "refs/heads/{}".format(branch) if branch else None
    others = [ref for ref in refs.splitlines() if ref.strip() and ref.strip() != own]
    # `--exclude=<glob> --branches` looks equivalent but counts zero here: the exclusion does
    # not survive the `--not` that has to sit between them. Naming the refs avoids the trap.
    ok, out = hc.git(path, "rev-list", "--count", "HEAD", "--not", *others,
                     timeout=SNAPSHOT_PROBE_TIMEOUT)
    # None, not 0: on the ref-only path a zero means "nothing to rescue" and ends the hook
    # silently, which is the same trap `pending_files` was pulled out of.
    return int(out) if ok and out.isdigit() else None


def snapshot(path, name, branch):
    if not os.path.isdir(path):
        return None
    if not hc.git(path, "rev-parse", "--git-dir", timeout=SNAPSHOT_PROBE_TIMEOUT)[0]:
        return None

    pending = pending_files(path)
    if pending is None:
        return {"error": "git status did not answer, so a clean tree cannot be told from a "
                         "busy one"}
    ok, current = hc.git(path, "rev-parse", "--abbrev-ref", "HEAD",
                         timeout=SNAPSHOT_PROBE_TIMEOUT)
    current = current if ok and current and current != "HEAD" else branch
    ahead = unreachable_commits(path, current)
    if ahead is None:
        if not pending:
            return {"error": "git could not say whether this branch holds commits nothing "
                             "else reaches"}
        ahead = 0
    if not pending and ahead == 0:
        return None

    stamp = time.strftime("%Y%m%d")
    label = (name or os.path.basename(path.rstrip("/\\")) or "worktree").strip()
    label = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in label)[:60]
    target = unique_branch(path, "wip/{}-{}".format(label, stamp))
    if target is None:
        return {"error": "no free wip/ branch name for {}".format(label)}

    # An unborn HEAD is not a failure: the first commit simply has no parent.
    ok, head = hc.git(path, "rev-parse", "HEAD", timeout=SNAPSHOT_PROBE_TIMEOUT)
    head = head if ok and head else None
    if head is None and not pending:
        return None

    if not pending:
        # Nothing to commit, so the commits are the work: a plain ref keeps them reachable
        # after the session's own branch is deleted, and leaves the worktree untouched.
        created, _ = hc.git(path, "branch", target, head)
        if not created:
            return {"error": "could not create branch {}".format(target)}
        return {"branch": target, "mode": "ref", "files": 0, "ahead": ahead, "head": head}

    # Staged into a scratch index, never the real one. A removal can fail after this hook has
    # run, and a tree that goes on living must not find its staged/unstaged split flattened —
    # mid-merge that also collapses the unmerged entries, so the next ordinary commit would
    # quietly carry conflict markers.
    scratch = os.path.join(tempfile.gettempdir(), "cwg-snapshot-{}.index".format(os.getpid()))
    env = {"GIT_INDEX_FILE": scratch}
    try:
        # Seed from HEAD first. An empty index makes `add -A` re-apply the ignore rules to
        # files that are tracked *and* ignored — this repository has fourteen, `.codex/`
        # among them — so it would skip their edits and record the files as deleted, while
        # the hook still reported success. Seeded, they are already known and stay.
        if head:
            seeded, _ = hc.git(path, "read-tree", head, timeout=30, env=env)
            if not seeded:
                return {"error": "git read-tree failed, so the snapshot would drop "
                                 "tracked-but-ignored files"}
        staged, _ = hc.git(path, "add", "-A", timeout=60, env=env)
        if not staged:
            return {"error": "git add failed (locked index, or a path git refuses)"}
        ok, tree = hc.git(path, "write-tree", timeout=30, env=env)
        if not ok or not tree:
            return {"error": "git write-tree failed"}
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    message = "snapshot: unfinished work from worktree {}".format(label)
    parents = ["-p", head] if head else []
    ok, commit = hc.git(path, "commit-tree", tree, *parents, "-m", message, timeout=20)
    if not ok or not commit:
        return {"error": "git commit-tree failed"}
    created, _ = hc.git(path, "branch", target, commit)
    if not created:
        return {"error": "could not create branch {}".format(target)}
    return {"branch": target, "mode": "commit", "files": len(pending), "ahead": ahead,
            "head": commit}


def main():
    payload = hc.read_payload()
    if payload is None:
        return 0
    path = payload.get("path") or payload.get("cwd")
    if not path:
        return 0
    try:
        result = snapshot(str(path), payload.get("name"), payload.get("branch"))
    except Exception as error:
        result = {"error": "{}: {}".format(type(error).__name__, error)}
    if not result:
        return 0
    if result.get("error"):
        # The removal happens regardless, so an unreported failure is work that vanishes with
        # nobody the wiser. Say it, and leave a record the audit can pick up.
        hc.append_jsonl(hc.SNAPSHOT_LOG, {
            "ts": time.time(),
            "session_id": payload.get("session_id"),
            "worktree": str(path),
            "removed_branch": payload.get("branch"),
            "failed": result["error"],
        })
        print("Could NOT save unfinished work from {}: {}. The worktree is being removed "
              "anyway — recover from git objects if the work mattered."
              .format(path, result["error"]))
        return 0
    hc.append_jsonl(hc.SNAPSHOT_LOG, {
        "ts": time.time(),
        "session_id": payload.get("session_id"),
        "worktree": str(path),
        "removed_branch": payload.get("branch"),
        "saved_to": result["branch"],
        "mode": result["mode"],
        "files": result["files"],
        "ahead": result["ahead"],
        "head": result["head"],
    })
    print("Saved unfinished work from {} to branch {}".format(path, result["branch"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
