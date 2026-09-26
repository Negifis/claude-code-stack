"""
Regression suite for the session-hygiene hooks.

Runs each hook the way Claude Code does — payload on stdin — against throwaway git
repositories, and asserts the property that matters: after a WorktreeRemove, the work is
still reachable from a branch. Run it with the same interpreter the hooks use:

    python hygiene_hooks_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hygiene_common as hc  # noqa: E402

PYTHON = sys.executable
FAILURES = []


HOME_OVERRIDE = {}


def run_hook(script, payload, env=None):
    proc = subprocess.run(
        [PYTHON, os.path.join(HERE, script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **HOME_OVERRIDE, **(env or {})},
        timeout=120,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def git(cwd, *args):
    return subprocess.run(
        ("git",) + args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )


def make_repo(root, name="origin-repo"):
    repo = os.path.join(root, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Hygiene Test")
    with open(os.path.join(repo, "kept.txt"), "w", encoding="utf-8") as handle:
        handle.write("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def check(name, condition, detail=""):
    if condition:
        print("ok   - {}".format(name))
    else:
        print("FAIL - {} {}".format(name, detail))
        FAILURES.append(name)


def branch_exists(repo, branch):
    return git(repo, "rev-parse", "--verify", "--quiet", branch).returncode == 0


def wip_branches(repo, pattern):
    """Branch names only. `git branch --list` decorates a branch checked out elsewhere."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/" + pattern).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_snapshot_saves_uncommitted(root):
    repo = make_repo(root, "repo-dirty")
    wt = os.path.join(root, "wt-dirty")
    git(repo, "worktree", "add", "-q", "-b", "feature-x", wt)
    with open(os.path.join(wt, "kept.txt"), "a", encoding="utf-8") as handle:
        handle.write("edited\n")
    with open(os.path.join(wt, "brand-new.txt"), "w", encoding="utf-8") as handle:
        handle.write("untracked work\n")

    # `worktree_path` is the field Claude Code sends; `cwd` is the session's own directory.
    code, out, err = run_hook("worktree_snapshot.py", {
        "session_id": "test-1", "hook_event_name": "WorktreeRemove", "cwd": repo,
        "name": "dirty", "worktree_path": wt, "branch": "feature-x",
    })
    check("snapshot hook exits 0", code == 0, err)
    check("snapshot reports the branch", "wip/dirty-" in out, out)

    saved = wip_branches(repo, "wip/dirty-*")
    check("wip branch exists", bool(saved), saved)
    if saved:
        listing = git(repo, "ls-tree", "-r", "--name-only", saved[0]).stdout
        check("untracked file survived", "brand-new.txt" in listing, listing)
        check("edit survived", "kept.txt" in listing, listing)

    git(repo, "worktree", "remove", "--force", wt)
    check("work outlives the worktree", bool(saved) and branch_exists(repo, saved[0]))


def test_snapshot_keeps_unpushed_commits(root):
    repo = make_repo(root, "repo-commits")
    wt = os.path.join(root, "wt-commits")
    git(repo, "worktree", "add", "-q", "-b", "feature-y", wt)
    with open(os.path.join(wt, "kept.txt"), "a", encoding="utf-8") as handle:
        handle.write("committed work\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "work that was never pushed")
    head = git(wt, "rev-parse", "HEAD").stdout.strip()

    run_hook("worktree_snapshot.py", {
        "session_id": "test-2", "hook_event_name": "WorktreeRemove",
        "name": "commits", "path": wt, "branch": "feature-y",
    })
    saved = wip_branches(repo, "wip/commits-*")
    check("ref-only snapshot created", bool(saved), saved)
    if saved:
        tip = git(repo, "rev-parse", saved[0]).stdout.strip()
        check("snapshot points at the work", tip == head, "{} != {}".format(tip, head))

    git(repo, "worktree", "remove", "--force", wt)
    git(repo, "branch", "-D", "feature-y")
    check("commits outlive branch deletion", bool(saved) and branch_exists(repo, saved[0]))


def test_snapshot_skips_clean_worktree(root):
    repo = make_repo(root, "repo-clean")
    wt = os.path.join(root, "wt-clean")
    git(repo, "worktree", "add", "-q", "-b", "feature-clean", wt)
    run_hook("worktree_snapshot.py", {
        "session_id": "test-3", "hook_event_name": "WorktreeRemove",
        "name": "clean", "path": wt, "branch": "feature-clean",
    })
    listing = wip_branches(repo, "wip/clean-*")
    check("clean worktree makes no branch", listing == [], listing)
    git(repo, "worktree", "remove", "--force", wt)


def test_snapshot_is_idempotent(root):
    repo = make_repo(root, "repo-twice")
    wt = os.path.join(root, "wt-twice")
    git(repo, "worktree", "add", "-q", "-b", "feature-twice", wt)
    with open(os.path.join(wt, "again.txt"), "w", encoding="utf-8") as handle:
        handle.write("one\n")
    payload = {
        "session_id": "test-4", "hook_event_name": "WorktreeRemove",
        "name": "twice", "path": wt, "branch": "feature-twice",
    }
    run_hook("worktree_snapshot.py", payload)
    with open(os.path.join(wt, "again.txt"), "a", encoding="utf-8") as handle:
        handle.write("two\n")
    run_hook("worktree_snapshot.py", payload)
    saved = wip_branches(repo, "wip/twice-*")
    check("second run does not overwrite the first", len(saved) == 2, saved)
    git(repo, "worktree", "remove", "--force", wt)


def test_snapshot_saves_a_conflicted_worktree(root):
    """The state the whole hook exists for: an abandoned worktree stopped mid-merge.

    `git switch -c` refuses outright here ("cannot switch branch while merging"), so the
    earlier checkout-based snapshot returned empty-handed and the removal took the work with
    it. Reproduced by the round-1 review; this is the regression that keeps it fixed.
    """
    repo = make_repo(root, "repo-conflict")
    wt = os.path.join(root, "wt-conflict")
    git(repo, "worktree", "add", "-q", "-b", "feature-conflict", wt)
    with open(os.path.join(wt, "kept.txt"), "w", encoding="utf-8") as handle:
        handle.write("theirs\n")
    git(wt, "commit", "-q", "-am", "diverge on the branch")
    git(repo, "switch", "-q", "main")
    with open(os.path.join(repo, "kept.txt"), "w", encoding="utf-8") as handle:
        handle.write("ours\n")
    git(repo, "commit", "-q", "-am", "diverge on main")
    merge = git(wt, "merge", "main")
    check("the merge really conflicted", merge.returncode != 0, merge.stdout)
    with open(os.path.join(wt, "rescue-me.txt"), "w", encoding="utf-8") as handle:
        handle.write("untracked work that must survive\n")

    refused = git(wt, "switch", "-c", "probe-branch")
    check("git switch is refused mid-merge (the original failure)",
          refused.returncode != 0, refused.stderr)

    code, out, err = run_hook("worktree_snapshot.py", {
        "session_id": "test-conflict", "hook_event_name": "WorktreeRemove",
        "name": "conflict", "path": wt, "branch": "feature-conflict",
    })
    check("snapshot hook still exits 0", code == 0, err)
    saved = wip_branches(repo, "wip/conflict-*")
    check("a snapshot branch was created mid-merge", bool(saved), out)
    if saved:
        listing = git(repo, "ls-tree", "-r", "--name-only", saved[0]).stdout
        check("untracked work survived the conflict", "rescue-me.txt" in listing, listing)
    head_after = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    check("the worktree stayed on its own branch", head_after == "feature-conflict", head_after)
    git(repo, "worktree", "remove", "--force", wt)


def test_snapshot_keeps_tracked_but_ignored_files(root):
    """Files that are both tracked and ignored must survive the rescue.

    Staging into an empty scratch index re-applies the ignore rules to them, so `add -A`
    skips the edit and the snapshot records the file as deleted — while still reporting
    success. This repository has fourteen such files, `.codex/config.toml` among them, and one
    of them was modified when the round-3 review caught this.
    """
    repo = make_repo(root, "repo-ignored")
    wt = os.path.join(root, "wt-ignored")
    git(repo, "worktree", "add", "-q", "-b", "feature-ignored", wt)
    with open(os.path.join(wt, "secret.toml"), "w", encoding="utf-8") as handle:
        handle.write("committed = true\n")
    git(wt, "add", "-f", "secret.toml")
    with open(os.path.join(wt, ".gitignore"), "w", encoding="utf-8") as handle:
        handle.write("secret.toml\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "track a file that is also ignored")
    with open(os.path.join(wt, "secret.toml"), "w", encoding="utf-8") as handle:
        handle.write("committed = true\nedited = yes\n")

    run_hook("worktree_snapshot.py", {
        "session_id": "test-ignored", "hook_event_name": "WorktreeRemove",
        "name": "ignored", "path": wt, "branch": "feature-ignored",
    })
    saved = wip_branches(repo, "wip/ignored-*")
    check("snapshot branch created", bool(saved), saved)
    if saved:
        listing = git(repo, "ls-tree", "-r", "--name-only", saved[0]).stdout
        check("the tracked-but-ignored file is still in the tree",
              "secret.toml" in listing, listing)
        content = git(repo, "show", "{}:secret.toml".format(saved[0])).stdout
        check("its edit was captured, not just its last commit",
              "edited = yes" in content, content)
    git(repo, "worktree", "remove", "--force", wt)


def test_snapshot_reports_its_own_failure(root):
    """A failure that nobody hears is the same as losing the work quietly."""
    repo = make_repo(root, "repo-failure")
    wt = os.path.join(root, "wt-failure")
    git(repo, "worktree", "add", "-q", "-b", "feature-fail", wt)
    with open(os.path.join(wt, "work.txt"), "w", encoding="utf-8") as handle:
        handle.write("work\n")
    # Exhausting every wip/ name is the cheapest reachable failure: the hook then has nowhere
    # to park the work, which is exactly the case that must not pass unannounced.
    stamp = time.strftime("%Y%m%d")
    git(repo, "branch", "wip/failure-{}".format(stamp), "main")
    for suffix in range(2, 50):
        git(repo, "branch", "wip/failure-{}-{}".format(stamp, suffix), "main")
    code, out, err = run_hook("worktree_snapshot.py", {
        "session_id": "test-fail", "hook_event_name": "WorktreeRemove",
        "name": "failure", "path": wt, "branch": "feature-fail",
    })
    check("hook still exits 0 when it cannot save", code == 0, err)
    check("the failure is announced, not swallowed", "Could NOT save" in out, out)
    check("the failure is recorded in the log",
          any(r.get("failed") for r in hc.read_jsonl(hc.SNAPSHOT_LOG, 20)))


def test_snapshot_survives_bad_input(root):
    code, _, _ = run_hook("worktree_snapshot.py", {"path": os.path.join(root, "does-not-exist")})
    check("missing path exits 0", code == 0)
    proc = subprocess.run(
        [PYTHON, os.path.join(HERE, "worktree_snapshot.py")],
        input="not json at all", capture_output=True, text=True, timeout=60,
    )
    check("malformed payload exits 0", proc.returncode == 0, proc.stderr)


def test_snapshot_never_takes_cwd_for_the_tree(root):
    """`cwd` is the session's directory — for a subagent's worktree, the parent's checkout — so a
    payload naming no tree parks nothing from it."""
    repo = make_repo(root, "repo-cwd")
    with open(os.path.join(repo, "parent-work.txt"), "w", encoding="utf-8") as handle:
        handle.write("the parent's own edit\n")
    code, _, err = run_hook("worktree_snapshot.py", {
        "session_id": "test-cwd", "hook_event_name": "WorktreeRemove", "cwd": repo,
    })
    check("a payload without a tree exits 0", code == 0, err)
    check("and parks nothing from the session's directory", not wip_branches(repo, "wip/*"),
          wip_branches(repo, "wip/*"))


def make_junction(target, link):
    """A directory junction on Windows, a directory symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


def test_links_go_before_the_tree(root):
    """Git for Windows walks into a junction when it removes a tree and deletes what it points at
    (2026-09-19). Every junction and directory link goes first, as a link, and nothing behind one
    is touched: not a shared `node_modules`, not one nested deeper, not a dangling one."""
    repo = make_repo(root, "repo-links")
    wt = os.path.join(root, "wt-links")
    git(repo, "worktree", "add", "-q", "-b", "feature-links", wt)
    shared = os.path.join(root, "links-shared")
    nested = os.path.join(root, "links-nested")
    gone = os.path.join(root, "links-gone")
    for directory in (os.path.join(shared, "pkg"), nested, gone):
        os.makedirs(directory)
    for directory in (os.path.join(shared, "pkg"), nested):
        with open(os.path.join(directory, "kept.txt"), "w", encoding="utf-8") as handle:
            handle.write("must survive\n")
    make_junction(shared, os.path.join(wt, "node_modules"))
    os.makedirs(os.path.join(wt, "sub", "deeper"))
    make_junction(nested, os.path.join(wt, "sub", "deeper", "link"))
    make_junction(gone, os.path.join(wt, "dangling"))
    os.rmdir(gone)

    removed, failed = hc.unlink_directory_links(wt)
    check("every link goes", len(removed) == 3 and not failed, (removed, failed))
    check("what they led to stays",
          os.path.isfile(os.path.join(shared, "pkg", "kept.txt"))
          and os.path.isfile(os.path.join(nested, "kept.txt")))
    check("the tree's own files stay", os.path.isfile(os.path.join(wt, "kept.txt")))
    git(repo, "worktree", "remove", "--force", wt)
    check("the tree then goes", not os.path.exists(wt))
    check("and the targets outlive it",
          os.path.isfile(os.path.join(shared, "pkg", "kept.txt"))
          and os.path.isfile(os.path.join(nested, "kept.txt")))

    linked_root = os.path.join(root, "links-root")
    make_junction(shared, linked_root)
    removed, failed = hc.unlink_directory_links(linked_root)
    check("a root that is itself a link is refused, not entered",
          failed == [linked_root] and not removed
          and os.path.isfile(os.path.join(shared, "pkg", "kept.txt")), (removed, failed))


def test_unlink_command_spares_a_main_checkout(root):
    """By hand the command runs only in a linked worktree: a main checkout's links are its own."""
    repo = make_repo(root, "repo-unlink-main")
    shared = os.path.join(root, "unlink-shared")
    os.makedirs(shared)
    make_junction(shared, os.path.join(repo, "node_modules"))
    command = [PYTHON, os.path.join(HERE, "hygiene_common.py"), "unlink-links"]
    proc = subprocess.run(command + [repo], capture_output=True, text=True, timeout=60)
    check("a main checkout is refused", proc.returncode == 1
          and "not a linked worktree" in proc.stderr, proc.stderr)
    check("and keeps its junction", os.path.isdir(os.path.join(repo, "node_modules")))
    wt = os.path.join(root, "wt-unlink")
    git(repo, "worktree", "add", "-q", "-b", "feature-unlink", wt)
    make_junction(shared, os.path.join(wt, "node_modules"))
    proc = subprocess.run(command + [wt], capture_output=True, text=True, timeout=60)
    check("a linked worktree loses its links", proc.returncode == 0
          and "unlinked" in proc.stdout and not os.path.exists(os.path.join(wt, "node_modules")),
          proc.stdout + proc.stderr)
    check("the shared directory stays", os.path.isdir(shared))
    git(repo, "worktree", "remove", "--force", wt)


def older_python():
    """An interpreter on PATH older than 3.12, which has no `is_junction`, or None."""
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if not found:
            continue
        proc = subprocess.run([found, "-c", "import sys; print(sys.version_info < (3, 12))"],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and proc.stdout.strip() == "True":
            return found
    return None


def test_unlink_command_under_an_older_python(root):
    """The advice names a bare `python`, which may be older than 3.12; the links must still go."""
    older = older_python()
    if not older:
        print("skip - no interpreter older than 3.12 on PATH")
        return
    repo = make_repo(root, "repo-unlink-older")
    shared = os.path.join(root, "unlink-older-shared")
    os.makedirs(shared)
    with open(os.path.join(shared, "kept.txt"), "w", encoding="utf-8") as handle:
        handle.write("must survive\n")
    wt = os.path.join(root, "wt-unlink-older")
    git(repo, "worktree", "add", "-q", "-b", "feature-older", wt)
    make_junction(shared, os.path.join(wt, "node_modules"))
    proc = subprocess.run([older, os.path.join(HERE, "hygiene_common.py"), "unlink-links", wt],
                          capture_output=True, text=True, timeout=60)
    check("an older interpreter still unlinks the junction", proc.returncode == 0
          and not os.path.exists(os.path.join(wt, "node_modules")), proc.stdout + proc.stderr)
    git(repo, "worktree", "remove", "--force", wt)
    check("and the target outlives the removal", os.path.isfile(os.path.join(shared, "kept.txt")))


def test_guard_warns_on_shared_tree(root):
    repo = make_repo(root, "repo-guard")
    first = run_hook("session_guard.py", {
        "session_id": "sess-A", "hook_event_name": "SessionStart", "cwd": repo,
    })
    check("first session is not warned", "Another live session" not in first[1], first[1])
    second = run_hook("session_guard.py", {
        "session_id": "sess-B", "hook_event_name": "SessionStart", "cwd": repo,
    })
    check("second session in the same tree is warned",
          "Another live session" in second[1], second[1])

    for ended in ("sess-A", "sess-B"):
        run_hook("session_index.py", {
            "session_id": ended, "hook_event_name": "SessionEnd",
            "cwd": repo, "reason": "other",
        })
    check("index recorded the session",
          any(r.get("session_id") == "sess-A" for r in hc.read_jsonl(hc.SESSION_INDEX, 50)))
    third = run_hook("session_guard.py", {
        "session_id": "sess-C", "hook_event_name": "SessionStart", "cwd": repo,
    })
    check("lock is released when every holder ends",
          "Another live session" not in third[1], third[1])
    hc.clear_tree_lock(repo, "sess-C")


def test_guard_tracks_several_holders(root):
    """A tree can hold more than one session. With a single slot the third session would be
    told about the second and never about the first, which is still sitting in the tree."""
    repo = make_repo(root, "repo-holders")
    for session in ("hold-1", "hold-2"):
        run_hook("session_guard.py", {
            "session_id": session, "hook_event_name": "SessionStart", "cwd": repo,
        })
    run_hook("session_index.py", {
        "session_id": "hold-2", "hook_event_name": "SessionEnd", "cwd": repo, "reason": "other",
    })
    third = run_hook("session_guard.py", {
        "session_id": "hold-3", "hook_event_name": "SessionStart", "cwd": repo,
    })
    check("the remaining holder is still reported after one of two ends",
          "Another live session" in third[1], third[1])
    check("the session named is the one still there",
          "hold-1" in third[1], third[1])
    for session in ("hold-1", "hold-3"):
        hc.clear_tree_lock(repo, session)


def test_guard_names_the_issue(root):
    repo = make_repo(root, "repo-issue")
    git(repo, "switch", "-q", "-c", "in/wechat-per-chat-restriction-378")
    code, out, _ = run_hook("session_guard.py", {
        "session_id": "sess-issue", "hook_event_name": "SessionStart", "cwd": repo,
    })
    check("guard exits 0", code == 0)
    check("guard extracts the issue number", "#378" in out, out)
    hc.clear_tree_lock(repo, "sess-issue")


def main():
    root = tempfile.mkdtemp(prefix="hygiene-test-")
    # The hooks derive their state directory from the home directory, and the register they
    # append to is the one the real audits read. A suite that wrote there would leave records
    # of worktrees that never existed in the file people make decisions from.
    fake_home = os.path.join(root, "home")
    os.makedirs(fake_home, exist_ok=True)
    HOME_OVERRIDE.update({"HOME": fake_home, "USERPROFILE": fake_home})
    hc.STATE_DIR = os.path.join(fake_home, ".claude", "state")
    hc.SESSION_INDEX = os.path.join(hc.STATE_DIR, "session-index.jsonl")
    hc.SNAPSHOT_LOG = os.path.join(hc.STATE_DIR, "worktree-snapshots.jsonl")
    hc.TREE_LOCK_DIR = os.path.join(hc.STATE_DIR, "tree-locks")
    try:
        for test in (
            test_snapshot_saves_uncommitted,
            test_snapshot_keeps_unpushed_commits,
            test_snapshot_skips_clean_worktree,
            test_snapshot_is_idempotent,
            test_snapshot_saves_a_conflicted_worktree,
            test_snapshot_keeps_tracked_but_ignored_files,
            test_snapshot_reports_its_own_failure,
            test_snapshot_survives_bad_input,
            test_snapshot_never_takes_cwd_for_the_tree,
            test_links_go_before_the_tree,
            test_unlink_command_spares_a_main_checkout,
            test_unlink_command_under_an_older_python,
            test_guard_warns_on_shared_tree,
            test_guard_tracks_several_holders,
            test_guard_names_the_issue,
        ):
            print("--- {}".format(test.__name__))
            test(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print("{} failing check(s): {}".format(len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
