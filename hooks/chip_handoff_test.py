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
from chip_handoff import safe_key  # noqa: E402

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


def cli(cwd, *args, hook_session=None):
    env = {**os.environ, **HOME_OVERRIDE,
           "CLAUDE_CODE_SESSION_ID": hook_session or "transcript-" + os.path.basename(cwd)}
    proc = subprocess.run(
        [PYTHON, SCRIPT] + list(args), cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=120,
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


def write_session_registry(transcript_id, ccd_id):
    """Fake one entry of the app's session registry — the only place the two ids meet."""
    root = os.path.join(HOME_OVERRIDE["APPDATA"], "Claude", "claude-code-sessions", "ws")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ccd_id + ".json"), "w", encoding="utf-8") as handle:
        json.dump({"sessionId": ccd_id, "cliSessionId": transcript_id}, handle)
    try:
        os.remove(os.path.join(os.path.dirname(chips_dir()), "session-map.json"))
    except OSError:
        pass


def open_chip(cwd, title="Тестовый чип", operational=False, session=None, hook_session=None):
    args = ["open", "--title", title]
    if session is not None or hook_session is None:
        args += ["--session", session or parent_of(cwd)]
    if operational:
        args.append("--operational")
    code, out, err = cli(cwd, *args, hook_session=hook_session)
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
    check("no archive command is built from a transcript id",
          "archive_session" not in out, out)
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
    check("child transcript is recorded",
          record.get("child_hook_session") == "transcript-" + os.path.basename(worktree))
    code, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("handed-off chip stops freely", code == 0 and out == "", out)


def test_parent_is_reminded_until_the_chip_is_closed(root):
    repo = make_repo(root, "reminder-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    notify(worktree, parent_of(repo))
    code, out = stop(repo, "Любой ответ родителя.", session_id="transcript-reminder-repo")
    check("parent hears about the waiting chip", code == 0 and chip_id in out, out)
    check("the reminder does not block", '"block"' not in out, out)
    _, again = stop(repo, "Следующий ответ.", session_id="transcript-reminder-repo")
    check("the reminder repeats while the chip waits", chip_id in again, again)
    cli(repo, "close", "--chip", chip_id, "--accept")
    _, after = stop(repo, "Третий ответ.", session_id="transcript-reminder-repo")
    check("closing the chip silences the reminder", after == "", after)


def test_parent_is_reminded_without_any_delivery(root):
    """The case the mechanism was blind to: an unattended parent cannot be messaged at all."""
    repo = make_repo(root, "undelivered-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    record = record_of(chip_id)
    check("nothing was delivered", not record.get("notified"), record.get("notified"))
    _, out = stop(repo, "Ответ родителя.", session_id="transcript-undelivered-repo")
    check("the parent is reminded anyway", chip_id in out, out)


def test_reminder_is_bound_to_the_session_that_opened_the_chip(root):
    repo = make_repo(root, "identity-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    _, mine = stop(repo, "Ответ родителя.", session_id="transcript-identity-repo")
    check("the session that opened it is reminded", chip_id in mine, mine)
    _, theirs = stop(repo, "Чужой ответ.", session_id="another-session-same-checkout")
    check("no other session in that checkout is", theirs == "", theirs)


def test_malformed_ccd_id_is_refused(root):
    repo = make_repo(root, "malformed-id-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    code, _, err = cli(worktree, "finish", "--child-session", "local_garbage",
                       "--message", "готово")
    check("a local_-prefixed non-id is refused", code == 2, code)
    check("the refusal names the shape", "local_<uuid>" in err, err)
    check("nothing was recorded", not record_of(chip_id).get("child_session_id"))


def test_a_locked_store_drops_bookkeeping_instead_of_burying_a_verdict(root):
    repo = make_repo(root, "locked-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    cli(repo, "close", "--chip", chip_id, "--accept")
    lock = os.path.join(chips_dir(), ".lock")
    os.makedirs(lock)
    try:
        notify(worktree, parent_of(repo))
        record = record_of(chip_id)
        check("the verdict survives a notify that could not lock",
              record["status"] == "accepted", record["status"])
        code, _, err = cli(repo, "close", "--chip", chip_id, "--rework", "ещё раз")
        check("a blocked close refuses loudly", code == 2, code)
        check("and says nothing was written", "не записан" in err, err)
    finally:
        os.rmdir(lock)


def test_accepted_chip_is_still_listed_by_status_all(root):
    repo = make_repo(root, "status-all-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    cli(repo, "close", "--chip", chip_id, "--accept")
    _, plain, _ = cli(repo, "status", "--session", parent_of(repo))
    check("an accepted chip is not pending", chip_id not in plain, plain)
    _, everything, _ = cli(repo, "status", "--session", parent_of(repo), "--all")
    check("but --all still finds it", chip_id in everything, everything)


def test_failed_delivery_frees_the_child(root):
    repo = make_repo(root, "failed-delivery-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    hook("hook-notify-failed", {
        "cwd": worktree, "session_id": "local_CHILD",
        "tool_name": "mcp__ccd_session_mgmt__send_message",
        "tool_input": {"session_id": parent_of(repo)},
    })
    record = record_of(chip_id)
    check("the attempt is recorded", record.get("delivery_attempted") is True, record)
    check("but nothing is claimed delivered", not record.get("notified"), record)
    _, out = stop(worktree, "Готово.\n\n" + RECEIPT)
    check("the child is not blocked for an impossible send", out == "", out)


def test_close_names_a_session_archive_can_take(root):
    repo = make_repo(root, "archive-id-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--child-session", "local_11111111-2222-3333-4444-555555555555", "--message", "готово")
    check("the CCD id is recorded", record_of(chip_id)["child_session_id"] == "local_11111111-2222-3333-4444-555555555555")
    _, out, _ = cli(repo, "close", "--chip", chip_id, "--accept")
    check("close prints an archivable id",
          "archive_session session_id=local_11111111-2222-3333-4444-555555555555" in out,
          out)


def test_close_without_a_ccd_id_says_how_to_find_the_child(root):
    repo = make_repo(root, "no-archive-id-repo")
    chip_id, worktree = open_chip(repo, title="Ищи меня по названию")
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    notify(worktree, parent_of(repo))
    _, out, _ = cli(repo, "close", "--chip", chip_id, "--accept")
    check("no bogus archive command is offered", "archive_session" not in out, out)
    check("the parent is told how to find the child", "Ищи меня по названию" in out, out)


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
    notify(os.path.join(root, "somewhere-else"), parent_of(repo),
           session_id="transcript-ops-notify-repo")
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
    check("the sender is still remembered", record.get("child_hook_session") == "local_CHILD")
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


def test_finish_refuses_a_transcript_id_as_the_child_session(root):
    repo = make_repo(root, "bad-id-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    code, _, err = cli(worktree, "finish", "--child-session",
                       "873a5532-1f0f-4434-98d5-80f6f05e0462", "--message", "готово")
    check("a transcript id is refused", code == 2, code)
    check("the refusal names the right source", "get_session self" in err, err)
    check("nothing was recorded", not record_of(chip_id).get("child_session_id"))


def test_reminder_never_asks_the_child_to_accept_itself(root):
    """An operational chip runs in its parent's own directory; only the parent is the acceptor."""
    repo = make_repo(root, "self-accept-repo")
    chip_id, _ = open_chip(repo, title="Операционка рядом", operational=True)
    cli(repo, "finish", "--chip", chip_id, "--message", "сделано",
        hook_session="transcript-the-child")
    _, child = stop(repo, "Ответ ребёнка.", session_id="transcript-the-child")
    check("the child is not asked to accept its own chip", child == "", child)
    _, parent = stop(repo, "Ответ родителя.", session_id="transcript-self-accept-repo")
    check("the parent still is", chip_id in parent, parent)


def test_rework_demands_a_fresh_delivery(root):
    repo = make_repo(root, "rework-cycle-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "первый заход")
    notify(worktree, parent_of(repo))
    cli(repo, "close", "--chip", chip_id, "--rework", "доделай")
    check("the old delivery is forgotten", not record_of(chip_id).get("delivery_attempted"))
    commit_work(worktree, name="more.txt", message="rework")
    cli(worktree, "finish", "--message", "второй заход")
    _, out = stop(worktree, "Готово." + chr(10) * 2 + RECEIPT)
    check("the second report must be sent too", '"block"' in out, out)


def test_a_late_notification_cannot_reopen_a_closed_chip(root):
    repo = make_repo(root, "late-notify-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    cli(repo, "close", "--chip", chip_id, "--accept")
    notify(worktree, parent_of(repo))
    record = record_of(chip_id)
    check("the verdict survives", record["status"] == "accepted", record["status"])
    check("it is not marked waiting again", not record.get("notified"), record)


def test_accepting_a_chip_stops_costing_the_stop_hook(root):
    repo = make_repo(root, "compaction-repo")
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    cli(repo, "close", "--chip", chip_id, "--accept")
    for key in (parent_of(repo), "transcript-compaction-repo"):
        index = os.path.join(chips_dir(), "by-parent", safe_key(key))
        left = open(index, encoding="utf-8").read() if os.path.exists(index) else ""
        check("the accepted chip leaves the {} index".format(key), chip_id not in left, left)


def spawn(cwd, title="Разобрать падение", prompt="Почини тест.", session="transcript-x",
          spawn_cwd=None, tool_use_id="toolu_test", land=True):
    """The spawn hook, then the PostToolUse that confirms the spawn actually happened."""
    payload = {"cwd": cwd, "session_id": session, "tool_use_id": tool_use_id,
               "tool_name": "mcp__ccd_session__spawn_task",
               "tool_input": {"title": title, "tldr": "коротко", "prompt": prompt,
                              **({"cwd": spawn_cwd} if spawn_cwd else {})}}
    code, out = hook("hook-spawn", payload)
    if land and out:
        hook("hook-spawned", payload)
    return code, (json.loads(out) if out else None)


def spawn_failed(cwd, tool_use_id, session="transcript-x"):
    return hook("hook-spawn-failed",
                {"cwd": cwd, "session_id": session, "tool_use_id": tool_use_id,
                 "tool_name": "mcp__ccd_session__spawn_task", "tool_input": {}})


def cards():
    return [json.load(open(os.path.join(chips_dir(), f), encoding="utf-8"))
            for f in os.listdir(chips_dir()) if f.endswith(".json")]


def test_spawning_a_chip_registers_it_without_being_asked(root):
    repo = make_repo(root, "spawn-repo")
    code, out = spawn(repo, session="transcript-spawn-repo")
    if not check("the spawn hook answers", code == 0 and out, out):
        return
    updated = out["hookSpecificOutput"]["updatedInput"]
    check("the permission decision is left alone",
          "permissionDecision" not in out["hookSpecificOutput"], out["hookSpecificOutput"])
    check("the child is told how to report", "## Возврат работы родителю" in updated["prompt"])
    check("the original task survives", "Почини тест." in updated["prompt"])
    check("the child is sent to its own worktree",
          "state" in updated["cwd"] and "chips" in updated["cwd"], updated["cwd"])
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("a card exists for it", len(mine) == 1, [c.get("chip_id") for c in mine])
    check("the card knows the parent transcript",
          mine and mine[0]["parent_hook_session"] == "transcript-spawn-repo")
    check("a landed spawn leaves the chip open", mine and mine[0]["status"] == "open",
          mine and mine[0]["status"])
    _, out2 = stop(repo, "Ответ.", session_id="transcript-spawn-repo")
    check("nothing is pending before the chip reports", out2 == "", out2)


def test_spawn_hook_does_not_register_twice(root):
    repo = make_repo(root, "spawn-twice-repo")
    _, first = spawn(repo, session="transcript-twice", tool_use_id="toolu_twice_1")
    prompt = first["hookSpecificOutput"]["updatedInput"]["prompt"]
    code, again = spawn(repo, prompt=prompt, session="transcript-twice",
                        tool_use_id="toolu_twice_2")
    check("an already-registered prompt is left alone", code == 0 and again is None, again)


def test_a_prompt_quoting_the_heading_is_still_registered(root):
    """A task about this tooling quotes the handoff heading; that is not a registration."""
    repo = make_repo(root, "quoting-repo")
    quoted = "Поправь текст блока «## Возврат работы родителю» в скилле."
    _, out = spawn(repo, prompt=quoted, session="transcript-quoting",
                   tool_use_id="toolu_quoting")
    if not check("a quoting prompt is still registered", out is not None):
        return
    check("and keeps its own text",
          quoted in out["hookSpecificOutput"]["updatedInput"]["prompt"])


def test_a_spawn_that_never_lands_is_cleaned_up(root):
    repo = make_repo(root, "cancelled-repo")
    _, out = spawn(repo, session="transcript-cancelled", tool_use_id="toolu_cancelled",
                   land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    check("its worktree exists while the spawn is pending", os.path.isdir(worktree))
    spawn_failed(repo, "toolu_cancelled", session="transcript-cancelled")
    check("a refused spawn takes its worktree with it", not os.path.isdir(worktree), worktree)
    left = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("and leaves no card", left == [], [c.get("chip_id") for c in left])
    branches = git(repo, "branch", "--list", "chip/*").stdout.strip()
    check("and no branch", branches == "", branches)


def test_a_repository_with_no_commits_still_gets_a_way_back(root):
    fresh = os.path.join(root, "unborn-repo")
    os.makedirs(fresh)
    git(fresh, "init", "-q", "-b", "main")
    _, out = spawn(fresh, session="transcript-unborn", tool_use_id="toolu_unborn")
    if not check("an unbranchable repository is still registered", out is not None):
        return
    updated = out["hookSpecificOutput"]["updatedInput"]
    check("it reports instead of branching", "cwd" not in updated, updated.get("cwd"))
    check("and still carries the handoff", "## Возврат работы родителю" in updated["prompt"])


def test_an_explicit_subdirectory_is_preserved(root):
    repo = make_repo(root, "monorepo")
    package = os.path.join(repo, "packages", "api")
    os.makedirs(package)
    write(package, "manifest.txt", "api\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "package")
    _, out = spawn(repo, spawn_cwd=package, session="transcript-mono",
                   tool_use_id="toolu_mono")
    if not check("the spawn is registered", out is not None):
        return
    cwd = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    check("the child lands in the same package of its own worktree",
          cwd.endswith(os.path.join("packages", "api")), cwd)
    check("which really exists", os.path.isdir(cwd), cwd)


def test_spawn_outside_a_repository_still_gets_a_way_back(root):
    plain = os.path.join(root, "not-a-repo-spawn")
    os.makedirs(plain)
    _, out = spawn(plain, session="transcript-plain")
    if not check("the spawn hook answers", out is not None):
        return
    updated = out["hookSpecificOutput"]["updatedInput"]
    check("an operational chip keeps the chosen directory", "cwd" not in updated, updated)
    check("but still carries the handoff", "## Возврат работы родителю" in updated["prompt"])


def test_spawn_hook_is_silent_for_other_tools(root):
    code, out = hook("hook-spawn", {"cwd": root, "tool_name": "Bash",
                                    "tool_input": {"command": "ls"}})
    check("another tool is untouched", code == 0 and out == "", out)


def test_a_resumed_parent_still_hears_about_its_chips(root):
    """A resumed session keeps its `local_…` id and gets a new transcript id."""
    repo = make_repo(root, "resumed-repo")
    old_transcript, ccd = "transcript-before-resume", "local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_session_registry(old_transcript, ccd)
    chip_id, worktree = open_chip(repo, session=ccd, hook_session=old_transcript)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово")
    new_transcript = "transcript-after-resume"
    write_session_registry(new_transcript, ccd)
    _, out = stop(repo, "Ответ после резюма.", session_id=new_transcript)
    check("the resumed parent is still reminded", chip_id in out, out)


def test_finish_finds_the_child_session_id_by_itself(root):
    repo = make_repo(root, "self-id-repo")
    child_transcript = "transcript-the-finishing-child"
    child_ccd = "local_12345678-1234-1234-1234-123456789abc"
    write_session_registry(child_transcript, child_ccd)
    chip_id, worktree = open_chip(repo)
    commit_work(worktree)
    code, _, _ = cli(worktree, "finish", "--message", "готово", hook_session=child_transcript)
    check("finish succeeds", code == 0)
    check("the child id was resolved without being passed",
          record_of(chip_id).get("child_session_id") == child_ccd, record_of(chip_id))
    _, out, _ = cli(repo, "close", "--chip", chip_id, "--accept")
    check("close can name it for archiving", child_ccd in out, out)


def test_finish_never_binds_the_chip_to_its_own_parent(root):
    repo = make_repo(root, "parent-rebind-repo")
    parent_transcript, parent_ccd = "transcript-parent-rebind", parent_of(repo)
    write_session_registry(parent_transcript, parent_ccd)
    chip_id, worktree = open_chip(repo, session=parent_ccd, hook_session=parent_transcript)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово", hook_session=parent_transcript)
    record = record_of(chip_id)
    check("the parent is not recorded as its own child",
          record.get("child_session_id") != parent_ccd, record.get("child_session_id"))
    _, out, _ = cli(repo, "close", "--chip", chip_id, "--accept")
    check("close does not offer to archive the parent", parent_ccd not in out, out)


def test_an_ambiguous_registry_pairing_is_refused(root):
    repo = make_repo(root, "ambiguous-registry-repo")
    shared = "transcript-claimed-twice"
    write_session_registry(shared, "local_aaaaaaaa-0000-0000-0000-000000000001")
    write_session_registry(shared, "local_bbbbbbbb-0000-0000-0000-000000000002")
    chip_id, worktree = open_chip(repo, session=None, hook_session=shared)
    check("no local id is guessed from an ambiguous pairing",
          record_of(chip_id).get("parent_session_id") is None,
          record_of(chip_id).get("parent_session_id"))


def test_a_provisional_chip_does_not_block_its_child(root):
    """Between the spawn hook cutting the worktree and the spawn landing, the chip is not real."""
    repo = make_repo(root, "provisional-repo")
    _, out = spawn(repo, session="transcript-provisional", tool_use_id="toolu_provisional",
                   land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    code, blocked = stop(worktree, "Готово." + chr(10) * 2 + RECEIPT)
    check("a pending chip never blocks", code == 0 and blocked == "", blocked)


def sweep_now(extra=1):
    return subprocess.run(
        [PYTHON, "-c",
         "import sys, time; sys.path.insert(0, r'{}');"
         " import chip_handoff as ch;"
         " ch.sweep_pending(now=time.time() + ch.PENDING_GRACE + {})".format(HERE, extra)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **HOME_OVERRIDE}, timeout=120)


def test_an_unconfirmed_spawn_is_adopted_never_deleted(root):
    """A session on an older hook set sends no confirmation; its child is real all the same."""
    repo = make_repo(root, "unconfirmed-repo")
    _, out = spawn(repo, session="transcript-unconfirmed", tool_use_id="toolu_unconfirmed",
                   land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    write(worktree, "child-was-here.txt", "работа ребёнка\n")
    swept = sweep_now()
    check("the sweep runs", swept.returncode == 0, swept.stderr[-300:])
    check("the worktree survives", os.path.isdir(worktree), worktree)
    check("the child's file survives",
          os.path.exists(os.path.join(worktree, "child-was-here.txt")))
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("and the chip is adopted", len(mine) == 1 and mine[0]["status"] == "open", mine)


def test_an_untouched_unconfirmed_spawn_is_also_adopted(root):
    """Silence is not proof the spawn failed: a child may read for a long time before editing."""
    repo = make_repo(root, "silent-child-repo")
    _, out = spawn(repo, session="transcript-silent", tool_use_id="toolu_silent", land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    swept = sweep_now()
    check("the sweep runs", swept.returncode == 0, swept.stderr[-300:])
    check("an untouched worktree is kept too", os.path.isdir(worktree), worktree)
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("its chip is adopted rather than dropped",
          len(mine) == 1 and mine[0]["status"] == "open", mine)
    left = os.path.join(chips_dir(), "by-spawn", safe_key("toolu_silent"))
    check("and it leaves the provisional index",
          not os.path.exists(left) or "toolu" not in open(left, encoding="utf-8").read(),
          left)


def test_an_operational_chip_keeps_its_card_when_unconfirmed(root):
    """It has no worktree to judge, so nothing may infer that its spawn failed."""
    plain = os.path.join(root, "ops-unconfirmed")
    os.makedirs(plain)
    _, out = spawn(plain, session="transcript-ops-unconfirmed", tool_use_id="toolu_ops",
                   land=False)
    if not check("the chip was registered", out is not None):
        return
    sweep_now()
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(plain)]
    check("its card survives so it can still report",
          len(mine) == 1 and mine[0]["status"] == "open", mine)


def test_an_explicit_refusal_will_not_take_work_with_it(root):
    """Even the confirmed-failure path refuses a worktree somebody has written in."""
    repo = make_repo(root, "refused-but-busy-repo")
    _, out = spawn(repo, session="transcript-busy", tool_use_id="toolu_busy", land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    write(worktree, "started.txt", "уже работаю\n")
    spawn_failed(repo, "toolu_busy", session="transcript-busy")
    check("the worktree with work is kept", os.path.isdir(worktree), worktree)
    check("the work itself is kept", os.path.exists(os.path.join(worktree, "started.txt")))
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("and its card is kept with it", len(mine) == 1, mine)


def test_a_stale_index_entry_is_collected(root):
    repo = make_repo(root, "stale-index-repo")
    _, out = spawn(repo, session="transcript-stale", tool_use_id="toolu_stale")
    if not check("the chip was cut and landed", out is not None):
        return
    sweep_now()
    entry = os.path.join(chips_dir(), "by-spawn", safe_key("toolu_stale"))
    check("a settled spawn leaves no provisional entry", not os.path.exists(entry), entry)


def test_finish_never_binds_the_chip_to_its_own_parent(root):
    repo = make_repo(root, "parent-rebind-repo")
    parent_transcript, parent_ccd = "transcript-parent-rebind", parent_of(repo)
    write_session_registry(parent_transcript, parent_ccd)
    chip_id, worktree = open_chip(repo, session=parent_ccd, hook_session=parent_transcript)
    commit_work(worktree)
    cli(worktree, "finish", "--message", "готово", hook_session=parent_transcript)
    record = record_of(chip_id)
    check("the parent is not recorded as its own child",
          record.get("child_session_id") != parent_ccd, record.get("child_session_id"))
    _, out, _ = cli(repo, "close", "--chip", chip_id, "--accept")
    check("close does not offer to archive the parent", parent_ccd not in out, out)


def test_an_ambiguous_registry_pairing_is_refused(root):
    repo = make_repo(root, "ambiguous-registry-repo")
    shared = "transcript-claimed-twice"
    write_session_registry(shared, "local_aaaaaaaa-0000-0000-0000-000000000001")
    write_session_registry(shared, "local_bbbbbbbb-0000-0000-0000-000000000002")
    chip_id, worktree = open_chip(repo, session=None, hook_session=shared)
    check("no local id is guessed from an ambiguous pairing",
          record_of(chip_id).get("parent_session_id") is None,
          record_of(chip_id).get("parent_session_id"))


def test_a_provisional_chip_does_not_block_its_child(root):
    """Between the spawn hook cutting the worktree and the spawn landing, the chip is not real."""
    repo = make_repo(root, "provisional-repo")
    _, out = spawn(repo, session="transcript-provisional", tool_use_id="toolu_provisional",
                   land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    code, blocked = stop(worktree, "Готово." + chr(10) * 2 + RECEIPT)
    check("a pending chip never blocks", code == 0 and blocked == "", blocked)


def sweep_now(extra=1):
    return subprocess.run(
        [PYTHON, "-c",
         "import sys, time; sys.path.insert(0, r'{}');"
         " import chip_handoff as ch;"
         " ch.sweep_pending(now=time.time() + ch.PENDING_GRACE + {})".format(HERE, extra)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **HOME_OVERRIDE}, timeout=120)


def test_the_sweep_never_deletes_a_chip_that_holds_work(root):
    """A session on an older hook set never confirms its spawn; its child is alive regardless."""
    repo = make_repo(root, "unconfirmed-repo")
    _, out = spawn(repo, session="transcript-unconfirmed", tool_use_id="toolu_unconfirmed",
                   land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    write(worktree, "child-was-here.txt", "работа ребёнка\n")
    swept = sweep_now()
    check("the sweep runs", swept.returncode == 0, swept.stderr[-300:])
    check("the worktree survives", os.path.isdir(worktree), worktree)
    check("the child's file survives",
          os.path.exists(os.path.join(worktree, "child-was-here.txt")))
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("and the chip is adopted rather than dropped",
          len(mine) == 1 and mine[0]["status"] == "open", mine)


def test_the_sweep_keeps_a_chip_that_committed(root):
    repo = make_repo(root, "committed-repo")
    _, out = spawn(repo, session="transcript-committed", tool_use_id="toolu_committed",
                   land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    commit_work(worktree)
    swept = sweep_now()
    check("the sweep runs", swept.returncode == 0, swept.stderr[-300:])
    check("a committed worktree is never removed", os.path.isdir(worktree), worktree)
    log = git(worktree, "log", "--oneline", "-1").stdout
    check("its commit is intact", "chip work" in log, log)


def test_an_abandoned_spawn_is_swept_after_the_grace_period(root):
    repo = make_repo(root, "swept-repo")
    _, out = spawn(repo, session="transcript-swept", tool_use_id="toolu_swept", land=False)
    if not check("the chip was cut", out is not None):
        return
    worktree = out["hookSpecificOutput"]["updatedInput"]["cwd"]
    mine = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("it is pending", len(mine) == 1 and mine[0]["status"] == "pending", mine)
    # A spawn that was neither confirmed nor refused: nothing signals it, only time does.
    swept = sweep_now()
    check("the sweep runs", swept.returncode == 0, swept.stderr[-300:])
    check("the abandoned worktree is gone", not os.path.isdir(worktree), worktree)
    left = [c for c in cards() if c.get("parent_cwd") == os.path.abspath(repo)]
    check("and its card with it", left == [], [c.get("chip_id") for c in left])


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
    HOME_OVERRIDE.update({"HOME": fake_home, "USERPROFILE": fake_home,
                          "APPDATA": os.path.join(fake_home, "AppData", "Roaming")})
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
            test_parent_is_reminded_until_the_chip_is_closed,
            test_parent_is_reminded_without_any_delivery,
            test_reminder_is_bound_to_the_session_that_opened_the_chip,
            test_malformed_ccd_id_is_refused,
            test_a_locked_store_drops_bookkeeping_instead_of_burying_a_verdict,
            test_accepted_chip_is_still_listed_by_status_all,
            test_failed_delivery_frees_the_child,
            test_close_names_a_session_archive_can_take,
            test_close_without_a_ccd_id_says_how_to_find_the_child,
            test_notified_hook_ignores_another_target,
            test_operational_notification_is_matched_by_parent,
            test_midwork_message_does_not_count_as_the_handoff,
            test_ambiguous_parent_notification_is_ignored,
            test_finish_refuses_a_transcript_id_as_the_child_session,
            test_reminder_never_asks_the_child_to_accept_itself,
            test_rework_demands_a_fresh_delivery,
            test_a_late_notification_cannot_reopen_a_closed_chip,
            test_accepting_a_chip_stops_costing_the_stop_hook,
            test_spawning_a_chip_registers_it_without_being_asked,
            test_spawn_hook_does_not_register_twice,
            test_a_prompt_quoting_the_heading_is_still_registered,
            test_a_spawn_that_never_lands_is_cleaned_up,
            test_a_repository_with_no_commits_still_gets_a_way_back,
            test_an_explicit_subdirectory_is_preserved,
            test_spawn_outside_a_repository_still_gets_a_way_back,
            test_spawn_hook_is_silent_for_other_tools,
            test_a_resumed_parent_still_hears_about_its_chips,
            test_finish_finds_the_child_session_id_by_itself,
            test_a_provisional_chip_does_not_block_its_child,
            test_an_unconfirmed_spawn_is_adopted_never_deleted,
            test_an_untouched_unconfirmed_spawn_is_also_adopted,
            test_an_operational_chip_keeps_its_card_when_unconfirmed,
            test_an_explicit_refusal_will_not_take_work_with_it,
            test_a_stale_index_entry_is_collected,
            test_finish_never_binds_the_chip_to_its_own_parent,
            test_an_ambiguous_registry_pairing_is_refused,
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
