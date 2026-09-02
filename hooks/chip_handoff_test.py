"""
Regression suite for the chip handoff.

Runs the CLI the way a session runs it and the hooks the way Claude Code runs them — payload
on stdin — against throwaway repositories, and asserts the properties the parent depends on:
the work reaches the parent branch when that is safe, the message says how to fetch it when it
is not, operational chips report instead, the fallback bundle really restores the commits, and
neither side can quietly skip the handoff. Run it with the interpreter the hooks use:

    python chip_handoff_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hygiene_common as hc  # noqa: E402

PYTHON = sys.executable
SCRIPT = os.path.join(HERE, "chip_handoff.py")
FAILURES = []
HOME_OVERRIDE = {}
RECEIPT = "[gate] verified: STANDARD; done"
PARENT = "local_PARENT"


def check(name, condition, detail=""):
    if condition:
        print("  ok   {}".format(name))
        return True
    FAILURES.append(name)
    print("  FAIL {} {}".format(name, detail))
    return False


def git(cwd, *args):
    return subprocess.run(
        ("git",) + args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )


def cli(cwd, *args):
    proc = subprocess.run(
        [PYTHON, SCRIPT] + list(args), cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env={**os.environ, **HOME_OVERRIDE}, timeout=120,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def hook(command, payload):
    proc = subprocess.run(
        [PYTHON, SCRIPT, command], input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env={**os.environ, **HOME_OVERRIDE}, timeout=60,
    )
    return proc.returncode, (proc.stdout or "").strip()


def parent_of(cwd):
    """One parent session per test repository: the by-parent index is shared state, and chips
    left over from another test would make every lookup through it ambiguous."""
    return "local_" + os.path.basename(os.path.abspath(cwd))


def stop(cwd, message, session_id=None):
    return hook("hook-stop", {"cwd": cwd, "last_assistant_message": message,
                              "session_id": session_id})


def notify(cwd, target=PARENT, session_id="local_CHILD"):
    return hook("hook-notified", {
        "cwd": cwd, "session_id": session_id,
        "tool_name": "mcp__ccd_session_mgmt__send_message",
        "tool_input": {"session_id": target},
    })


def chips_dir():
    return os.path.join(HOME_OVERRIDE["HOME"], ".claude", "state", "chips")


def make_repo(root, name):
    repo = os.path.join(root, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Chip Test")
    write(repo, "kept.txt", "base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def write(root, name, text):
    with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def open_chip(cwd, title="Тестовый чип", operational=False, session=None):
    args = ["open", "--title", title, "--session", session or parent_of(cwd)]
    if operational:
        args.append("--operational")
    code, out, err = cli(cwd, *args)
    if code != 0:
        raise AssertionError("open failed: {}{}".format(out, err))
    fields = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    chip_id = fields["chip"].strip()
    worktree = fields["worktree"].strip() if "worktree" in fields else None
    if worktree:
        git(worktree, "config", "user.email", "test@example.invalid")
        git(worktree, "config", "user.name", "Chip Test")
    return chip_id, worktree


def record_of(chip_id):
    with open(os.path.join(chips_dir(), chip_id + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


def commit_work(worktree, name="added.txt", text="chip\n", message="chip work"):
    write(worktree, name, text)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", message)


def test_open_creates_branch_and_record(root):
    repo = make_repo(root, "open-repo")
    chip_id, worktree = open_chip(repo)
    check("worktree exists", os.path.isdir(worktree))
    record = record_of(chip_id)
    check("record names the parent branch", record["parent_branch"] == "main", record)
    check("record keeps the parent session", record["parent_session_id"] == parent_of(repo))
    check("mode is code", record["mode"] == "code", record["mode"])
    branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    check("chip branch is checked out", branch == record["chip_branch"], branch)
    index = os.path.join(chips_dir(), "by-tree", hc.tree_key(worktree))
    check("worktree index points at the chip", os.path.exists(index))


def test_open_refuses_code_chip_outside_a_repo(root):
    plain = os.path.join(root, "not-a-repo")
    os.makedirs(plain)
    code, _, err = cli(plain, "open", "--title", "Без репозитория", "--session", PARENT)
    check("code chip outside a repo is refused", code == 2, code)
    check("refusal points at --operational", "--operational" in err, err)


def test_finish_refuses_dirty_tree(root):
    repo = make_repo(root, "dirty-repo")
    _, worktree = open_chip(repo)
    write(worktree, "loose.txt", "uncommitted\n")
    code, _, err = cli(worktree, "finish")
    check("dirty tree is rejected", code == 2, code)
    check("rejection names the fix", "коммит" in err, err)


def test_busy_parent_branch_is_not_merged(root):
    repo = make_repo(root, "busy-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    before = git(repo, "rev-parse", "main").stdout.strip()
    code, out, _ = cli(worktree, "finish", "--message", "готово")
    after = git(repo, "rev-parse", "main").stdout.strip()
    check("finish succeeds", code == 0, out)
    check("busy parent branch is untouched", before == after)
    check("message offers the merge command", "git merge --no-ff" in out, out)
    check("message offers the bundle fallback", "git fetch" in out, out)
    check("message asks the parent to close the chip", "--accept" in out, out)
    check("outcome recorded", record_of(chip_id)["outcome"] == "branch-busy")


def test_free_parent_branch_is_merged(root):
    repo = make_repo(root, "free-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    git(repo, "switch", "-q", "-c", "parked")
    code, out, _ = cli(worktree, "finish", "--message", "готово")
    check("finish succeeds", code == 0, out)
    log = git(repo, "log", "--oneline", "main").stdout
    check("parent branch carries the work", "chip work" in log, log)
    trees = git(repo, "worktree", "list").stdout
    check("temporary merge worktree is gone", "merge-" not in trees, trees)
    record = record_of(chip_id)
    check("outcome recorded", record["outcome"] == "merged", record["outcome"])
    check("no bundle written for merged work", not record.get("bundle"), record.get("bundle"))


def test_conflict_leaves_parent_branch_intact(root):
    repo = make_repo(root, "conflict-repo")
    _, worktree = open_chip(repo)
    commit_work(worktree, name="kept.txt", text="from the chip\n", message="chip edit")
    write(repo, "kept.txt", "from the parent\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "parent edit")
    git(repo, "switch", "-q", "-c", "parked")
    before = git(repo, "rev-parse", "main").stdout.strip()
    code, out, _ = cli(worktree, "finish")
    after = git(repo, "rev-parse", "main").stdout.strip()
    check("finish succeeds", code == 0, out)
    check("parent branch is unchanged", before == after)
    check("message names the conflict", "kept.txt" in out, out)
    check("no merge left in progress", not os.path.exists(
        os.path.join(repo, ".git", "MERGE_HEAD")))


def test_no_commits_says_so(root):
    repo = make_repo(root, "empty-repo")
    chip_id, worktree = open_chip(repo)
    code, out, _ = cli(worktree, "finish")
    check("finish succeeds", code == 0, out)
    check("message reports no changes", "Изменений в коде нет" in out, out)
    check("outcome recorded", record_of(chip_id)["outcome"] == "no-changes")


def test_operational_chip_reports_without_a_worktree(root):
    repo = make_repo(root, "ops-repo")
    chip_id, worktree = open_chip(repo, title="Перезапуск сервиса", operational=True)
    check("no worktree is cut", worktree is None)
    code, out, _ = cli(repo, "finish", "--chip", chip_id, "--message", "Сервис перезапущен")
    check("finish succeeds", code == 0, out)
    check("report carries the summary", "Сервис перезапущен" in out, out)
    check("report says there is nothing to pull", "подтягивать нечего" in out, out)
    check("report asks the parent to close it", "--accept" in out, out)
    check("outcome recorded", record_of(chip_id)["outcome"] == "reported")


def test_bundle_restores_the_commits(root):
    repo = make_repo(root, "bundle-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish")
    record = record_of(chip_id)
    bundle = record.get("bundle")
    if not check("bundle was written", bundle and os.path.exists(bundle), record):
        return
    clone = os.path.join(root, "bundle-clone")
    git(root, "clone", "-q", repo, clone)
    fetched = git(clone, "fetch", bundle, "{0}:{0}".format(record["chip_branch"]))
    check("bundle fetches", fetched.returncode == 0, fetched.stderr)
    log = git(clone, "log", "--oneline", record["chip_branch"]).stdout
    check("bundle carries the work", "chip work" in log, log)


def test_close_accepts_and_names_the_child_session(root):
    repo = make_repo(root, "accept-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    notify(worktree, parent_of(repo))
    code, out, _ = cli(repo, "close", "--chip", chip_id, "--accept")
    check("close succeeds", code == 0, out)
    check("close names the session to archive", "local_CHILD" in out, out)
    check("record is accepted", record_of(chip_id)["status"] == "accepted")


def test_close_rework_prints_the_message(root):
    repo = make_repo(root, "rework-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    notify(worktree, parent_of(repo))
    code, out, _ = cli(repo, "close", "--chip", chip_id, "--rework", "нет тестов")
    check("close succeeds", code == 0, out)
    check("rework text is printed for sending", "нет тестов" in out, out)
    check("record is marked for rework", record_of(chip_id)["status"] == "rework")


def test_close_refuses_a_chip_that_never_reported(root):
    repo = make_repo(root, "premature-repo")
    chip_id, _ = open_chip(repo)
    code, _, err = cli(repo, "close", "--chip", chip_id, "--accept")
    check("closing an unreported chip is refused", code == 2, code)
    check("refusal explains why", "не отчитался" in err, err)


def test_stop_hook_is_silent_without_a_receipt(root):
    repo = make_repo(root, "silent-repo")
    _, worktree = open_chip(repo)
    code, out = stop(worktree, "Ещё работаю, ничего не закрываю.")
    check("ordinary turn is not blocked", code == 0 and out == "", out)


def test_stop_hook_blocks_a_closing_chip(root):
    repo = make_repo(root, "blocking-repo")
    _, worktree = open_chip(repo)
    code, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("closing without a handoff is blocked", code == 0 and '"block"' in out, out)
    check("block names the finish command", "finish" in out, out)
    for _ in range(4):
        _, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("blocking is finite", out == "", out)


def test_stop_hook_blocks_an_unsent_report(root):
    repo = make_repo(root, "unsent-repo")
    _, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    code, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("a prepared but unsent handoff is blocked", code == 0 and '"block"' in out, out)
    check("block asks for the message", "не уведомлена" in out, out)


def test_stop_hook_accepts_a_completed_handoff(root):
    repo = make_repo(root, "handed-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    notify(worktree, parent_of(repo))
    record = record_of(chip_id)
    check("notification is recorded", record.get("notified") is True, record)
    check("child session is recorded", record.get("child_session_id") == "local_CHILD")
    code, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("handed-off chip stops freely", code == 0 and out == "", out)


def test_parent_is_reminded_once(root):
    repo = make_repo(root, "reminder-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    notify(worktree, parent_of(repo))
    code, out = stop(repo, "Любой ответ родителя.", session_id=parent_of(repo))
    check("parent hears about the waiting chip", code == 0 and chip_id in out, out)
    check("the reminder does not block", '"block"' not in out, out)
    _, again = stop(repo, "Следующий ответ.", session_id=parent_of(repo))
    check("the reminder is not repeated", again == "", again)


def test_notified_hook_ignores_another_target(root):
    repo = make_repo(root, "wrong-target-repo")
    chip_id, worktree = open_chip(repo)
    notify(worktree, target="local_SOMEONE_ELSE")
    check("a message to another session does not count",
          record_of(chip_id).get("notified") is not True)


def test_operational_notification_is_matched_by_parent(root):
    repo = make_repo(root, "ops-notify-repo")
    chip_id, _ = open_chip(repo, title="Операционка", operational=True)
    cli(repo, "finish", "--chip", chip_id, "--message", "сделано")
    notify(os.path.join(root, "somewhere-else"), parent_of(repo))
    record = record_of(chip_id)
    check("a chip with no worktree is matched through its parent",
          record.get("notified") is True, record)


def test_midwork_message_does_not_count_as_the_handoff(root):
    repo = make_repo(root, "midwork-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    notify(worktree, parent_of(repo))
    record = record_of(chip_id)
    check("a message sent mid-work is not the report", record.get("notified") is not True)
    check("the sender is still remembered", record.get("child_session_id") == "local_CHILD")
    cli(worktree, "finish", "--message", "готово")
    _, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("the real handoff is still enforced", '"block"' in out and "не уведомлена" in out, out)


def test_ambiguous_parent_notification_is_ignored(root):
    repo = make_repo(root, "ambiguous-repo")
    first, _ = open_chip(repo, title="Первая операционка", operational=True)
    second, _ = open_chip(repo, title="Вторая операционка", operational=True)
    cli(repo, "finish", "--chip", first, "--message", "раз")
    cli(repo, "finish", "--chip", second, "--message", "два")
    notify(os.path.join(root, "nowhere"), parent_of(repo))
    check("neither of two waiting chips is claimed",
          not record_of(first).get("notified") and not record_of(second).get("notified"))


def test_hooks_survive_bad_input(root):
    for command in ("hook-stop", "hook-notified"):
        proc = subprocess.run(
            [PYTHON, SCRIPT, command], input="not json", capture_output=True, text=True,
            encoding="utf-8", errors="replace", env={**os.environ, **HOME_OVERRIDE},
            timeout=60,
        )
        check("{} survives malformed input".format(command),
              proc.returncode == 0 and not proc.stdout.strip(), proc.stdout)


def test_stop_hook_ignores_a_plain_session(root):
    repo = make_repo(root, "plain-repo")
    code, out = stop(repo, "Готово.\n\n" + RECEIPT, session_id="local_UNRELATED")
    check("a session with no chips is untouched", code == 0 and out == "", out)


def main():
    root = tempfile.mkdtemp(prefix="chip-handoff-test-")
    fake_home = os.path.join(root, "home")
    os.makedirs(fake_home)
    HOME_OVERRIDE.update({"HOME": fake_home, "USERPROFILE": fake_home})
    try:
        for test in (
            test_open_creates_branch_and_record,
            test_open_refuses_code_chip_outside_a_repo,
            test_finish_refuses_dirty_tree,
            test_busy_parent_branch_is_not_merged,
            test_free_parent_branch_is_merged,
            test_conflict_leaves_parent_branch_intact,
            test_no_commits_says_so,
            test_operational_chip_reports_without_a_worktree,
            test_bundle_restores_the_commits,
            test_close_accepts_and_names_the_child_session,
            test_close_rework_prints_the_message,
            test_close_refuses_a_chip_that_never_reported,
            test_stop_hook_is_silent_without_a_receipt,
            test_stop_hook_blocks_a_closing_chip,
            test_stop_hook_blocks_an_unsent_report,
            test_stop_hook_accepts_a_completed_handoff,
            test_parent_is_reminded_once,
            test_notified_hook_ignores_another_target,
            test_operational_notification_is_matched_by_parent,
            test_midwork_message_does_not_count_as_the_handoff,
            test_ambiguous_parent_notification_is_ignored,
            test_hooks_survive_bad_input,
            test_stop_hook_ignores_a_plain_session,
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
