"""
Chip handoff — a spawned task returns its work to the session that spawned it.

`spawn_task` hands the child a prompt and a directory and nothing else: no parent branch, no
parent session, no way back. So the child finishes somewhere nobody looks, and the work is
found later by accident or not at all.

This binds the two ends. `open` records who spawned the chip and, for work that touches code,
cuts a worktree and a branch off the parent's HEAD so neither session edits the other's tree.
`finish` merges into the parent branch when that branch is checked out nowhere, and when it is
not — the ordinary case, because the parent is sitting on it — leaves the branch and a bundle
and says exactly how to pull them in. Operational work has nothing to pull and reports its
effect instead. Either way `finish` prints the message the child sends to the parent, and
`close` records the parent's verdict once the parent has checked the result: accepted, and the
child session can be archived, or sent back with what is missing.

Two hooks keep it honest without nagging. In a chip's own worktree the Stop hook speaks only
when the child closes out work — a `[gate]` receipt in the final message — because a chip that
is done without a handoff is the failure this exists to prevent. In the parent it reminds once,
without blocking, that a reported chip is still unverified. Fail-open everywhere: a session must
never fail to end because its bookkeeping did.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hygiene_common as hc  # noqa: E402

hc.configure_utf8_streams()

CHIP_DIR = os.path.join(hc.STATE_DIR, "chips")
TREE_ROOT = os.path.join(CHIP_DIR, "trees")
BY_TREE = os.path.join(CHIP_DIR, "by-tree")
BY_PARENT = os.path.join(CHIP_DIR, "by-parent")
MAX_BLOCKS = 3
NOTIFY_TOOL = "mcp__ccd_session_mgmt__send_message"
# The kinds are `code_work_gate_stop.TERMINAL_RE`'s, deliberately copied rather than imported:
# each hook family here stays importable on its own. This one only asks whether the turn looks
# like a closing one, so it stays a loose search where the gate's is a strict parse.
GATE_RECEIPT = re.compile(
    r"^\[gate\]\s*(?:verified|operational|no-change|pr-ready|draft-blocked)\s*:",
    re.IGNORECASE | re.MULTILINE)

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def git(cwd, *args, timeout=60):
    """Run git in `cwd`. Returns (ok, stdout, stderr).

    `hygiene_common.git` drops stderr, and here the whole point of a failed merge is what git
    said about it.
    """
    try:
        proc = subprocess.run(
            ("git",) + args, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except Exception as exc:
        return False, "", str(exc)
    return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def slugify(title):
    out = []
    for char in (title or "").lower():
        if char in TRANSLIT:
            out.append(TRANSLIT[char])
        elif char.isascii() and char.isalnum():
            out.append(char)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug[:40].strip("-") or "chip"


def safe_key(value):
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(value or ""))[-120:]


def record_path(chip_id):
    return os.path.join(CHIP_DIR, "{}.json".format(safe_key(chip_id)))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_record(record):
    path = record_path(record["chip_id"])
    os.makedirs(CHIP_DIR, exist_ok=True)
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def index_write(directory, key, chip_id, append=False):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, safe_key(key)), "a" if append else "w",
              encoding="utf-8") as handle:
        handle.write(chip_id + "\n")


def index_read(directory, key):
    try:
        with open(os.path.join(directory, safe_key(key)), encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]
    except Exception:
        return []


def record_for_tree(cwd):
    ids = index_read(BY_TREE, hc.tree_key(cwd))
    return read_json(record_path(ids[-1])) if ids else None


def records_for_parent(session_id):
    found = []
    for chip_id in index_read(BY_PARENT, session_id):
        record = read_json(record_path(chip_id))
        if record:
            found.append(record)
    return found


def checked_out_branches(repo_root):
    """Branch -> worktree that holds it. A branch checked out anywhere cannot be merged into."""
    ok, out, _ = git(repo_root, "worktree", "list", "--porcelain")
    held = {}
    if not ok:
        return held
    tree = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            tree = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/"):
            held[line[len("branch refs/heads/"):].strip()] = tree
    return held


def cmd_open(args):
    cwd = os.path.abspath(args.cwd or os.getcwd())
    chip_id = uuid.uuid4().hex[:8]
    record = {
        "chip_id": chip_id,
        "title": args.title,
        "mode": "operational" if args.operational else "code",
        "created_ts": time.time(),
        "parent_session_id": args.session,
        "parent_cwd": cwd,
        "status": "open",
        "outcome": None,
        "notified": False,
        "child_session_id": None,
        "blocks": 0,
    }

    if not args.operational:
        ok, repo_root, err = git(cwd, "rev-parse", "--show-toplevel")
        if not ok:
            return fail("не git-репозиторий: {}\nдля работы без кода добавь --operational\n{}"
                        .format(cwd, err))
        repo_root = os.path.abspath(repo_root)
        ok, head, err = git(repo_root, "rev-parse", "--verify", "HEAD")
        if not ok:
            return fail("в репозитории нет коммитов, не от чего ветвиться\n{}".format(err))
        _, branch, _ = git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        parent_branch = None if branch in ("", "HEAD") else branch
        slug = slugify(args.title)
        chip_branch = "chip/{}-{}".format(slug, chip_id)
        worktree = os.path.join(
            TREE_ROOT, "{}-{}-{}".format(os.path.basename(repo_root), slug, chip_id))
        os.makedirs(TREE_ROOT, exist_ok=True)
        ok, _, err = git(repo_root, "worktree", "add", "-b", chip_branch, worktree,
                         parent_branch or head)
        if not ok:
            return fail("не удалось создать worktree {}\n{}".format(worktree, err))
        record.update({
            "repo_root": repo_root, "parent_branch": parent_branch, "worktree": worktree,
            "chip_branch": chip_branch, "base_sha": head,
        })
        try:
            save_record(record)
            index_write(BY_TREE, hc.tree_key(worktree), chip_id)
        except Exception as exc:
            git(repo_root, "worktree", "remove", "--force", worktree)
            git(repo_root, "branch", "-D", chip_branch)
            return fail("не удалось записать карточку чипа, worktree убран\n{}".format(exc))
    else:
        save_record(record)

    if args.session:
        index_write(BY_PARENT, args.session, chip_id, append=True)

    print("chip:     {}".format(chip_id))
    if record["mode"] == "code":
        print("worktree: {}".format(record["worktree"]))
        print("branch:   {}".format(record["chip_branch"]))
        print("base:     {}".format(record["parent_branch"] or record["base_sha"]))
        print("")
        print("Передай в spawn_task cwd={}".format(record["worktree"]))
    else:
        print("")
        print("Операционный чип: worktree не нужен, cwd для spawn_task выбирай по задаче.")
    print("и добавь в конец prompt блок:")
    print("")
    print(handoff_footer(record))
    return 0


def finish_command(record):
    target = "" if record["mode"] == "code" else " --chip {}".format(record["chip_id"])
    return '"{}" "{}" finish{} --message "<что сделано, одной строкой>"'.format(
        sys.executable, os.path.abspath(__file__), target)


def handoff_footer(record):
    lines = ["## Возврат работы родителю", ""]
    if record["mode"] == "code":
        lines += [
            "Ты работаешь в отдельном worktree на ветке `{}`, отведённой от `{}`.".format(
                record["chip_branch"], record.get("parent_branch") or record["base_sha"][:12]),
            "Родительская сессия ждёт результат и сама его не увидит.",
            "",
            "Закончив работу и пройдя `development-verification`:",
            "",
            "1. Закоммить всё в этом worktree.",
            "2. Выполни:",
        ]
    else:
        lines += [
            "Это операционный чип «{}»: подтягивать родителю нечего, нужен отчёт о "
            "проделанном.".format(record["title"]),
            "Родительская сессия ждёт результат и сама его не увидит.",
            "",
            "Закончив работу и пройдя `development-verification`:",
            "",
            "1. Убедись, что эффект проверен по самой системе, а не по ожиданию.",
            "2. Выполни:",
        ]
    lines += ["", "```bash", finish_command(record), "```", ""]
    lines.append("3. Отправь напечатанный им текст в родительскую сессию через "
                 "`{}`".format(NOTIFY_TOOL))
    if record.get("parent_session_id"):
        lines.append("   с `session_id: {}`.".format(record["parent_session_id"]))
    else:
        lines.append("   (идентификатор родительской сессии не записан — найди его через "
                     "`list_sessions`).")
    lines += ["", "Родитель проверит результат и либо примет чип, либо пришлёт правки в эту же "
                  "сессию — не закрывай её до его ответа."]
    return "\n".join(lines)


def base_ref(record):
    """(name to merge into, commit it points at) — the parent branch while it still exists,
    else the commit the chip was cut from."""
    parent = record.get("parent_branch")
    if parent:
        ok, sha, _ = git(record["repo_root"], "rev-parse", "--verify", "refs/heads/" + parent)
        if ok:
            return parent, sha
    return record["base_sha"], record["base_sha"]


def try_merge(record, base):
    """Merge the chip branch into the parent branch, in a worktree of its own.

    Never touches the parent's own checkout: a branch checked out anywhere is left alone, and
    the merge that does happen runs in a temporary worktree that is removed either way. Moving
    the parent's ref under a live session would leave its index describing a tree it no longer
    has.

    Returns (outcome, detail, conflicts); `detail` carries whatever that outcome needs — a
    commit, a worktree path or git's own complaint — and is only ever read per outcome.
    """
    parent = record.get("parent_branch")
    if not parent or parent != base:
        return "no-parent-branch", None, []
    held = checked_out_branches(record["repo_root"])
    if parent in held:
        return "branch-busy", held[parent], []

    tmp = os.path.join(CHIP_DIR, "merge-" + record["chip_id"])
    ok, _, err = git(record["repo_root"], "worktree", "add", tmp, parent)
    if not ok:
        return "merge-unavailable", err, []
    try:
        ok, _, err = git(tmp, "merge", "--no-ff", "--no-edit", record["chip_branch"])
        if ok:
            _, sha, _ = git(tmp, "rev-parse", "HEAD")
            return "merged", sha, []
        _, conflicted, _ = git(tmp, "diff", "--name-only", "--diff-filter=U")
        git(tmp, "merge", "--abort")
        return "conflict", err, [f for f in conflicted.splitlines() if f]
    finally:
        git(record["repo_root"], "worktree", "remove", "--force", tmp)


def cmd_finish(args):
    cwd = os.path.abspath(args.cwd or os.getcwd())
    record = read_json(record_path(args.chip)) if args.chip else record_for_tree(cwd)
    if not record:
        return fail("не найдена карточка чипа: {} не worktree чипа, --chip не задан"
                    .format(cwd))
    record["finished_ts"] = time.time()
    record["summary"] = args.message
    record["blocks"] = 0

    if record["mode"] == "operational":
        record["status"] = "handed-off"
        record["outcome"] = "reported"
        save_record(record)
        print(notification(record))
        return 0

    worktree = record.get("worktree") or cwd
    ok, dirty, err = git(worktree, "status", "--porcelain")
    if not ok:
        return fail("git status не отработал\n{}".format(err))
    if dirty:
        return fail("рабочее дерево не чистое — сначала коммит:\n{}".format(dirty))

    base, base_commit = base_ref(record)
    ok, count, err = git(worktree, "rev-list", "--count", "{}..HEAD".format(base))
    if not ok:
        return fail("не удалось сравнить с {}\n{}".format(base, err))
    record["base_commit"] = base_commit
    record["commits"] = int(count or "0")
    record["status"] = "handed-off"

    if record["commits"] == 0:
        record["outcome"] = "no-changes"
        save_record(record)
        print(notification(record))
        return 0

    outcome, detail, conflicts = try_merge(record, base)
    record["outcome"] = outcome
    record["outcome_detail"] = detail
    record["conflicts"] = conflicts
    if outcome != "merged":
        bundle = os.path.join(CHIP_DIR, "{}.bundle".format(record["chip_id"]))
        ok, _, err = git(worktree, "bundle", "create", bundle, record["chip_branch"],
                         "--not", base)
        record["bundle"] = bundle if ok else None
        record["bundle_error"] = None if ok else err
    save_record(record)
    print(notification(record))
    return 0


def parent_actions(record):
    close = '"{}" "{}" close --chip {}'.format(sys.executable, os.path.abspath(__file__),
                                               record["chip_id"])
    return [
        "",
        "Проверь результат сам, потом закрой чип:",
        "",
        "```bash",
        "{} --accept".format(close),
        '{} --rework "<что доделать>"'.format(close),
        "```",
    ]


def notification(record):
    """The message the child sends to the parent. Says how to get the work, always."""
    outcome = record["outcome"]
    lines = ["Чип «{}» завершён ({}).".format(record.get("title") or record["chip_id"],
                                              record["chip_id"]), ""]
    if record.get("summary"):
        lines += [record["summary"], ""]

    if outcome == "reported":
        lines.append("Операционная работа: подтягивать нечего, эффект проверяется по самой "
                     "системе.")
        return "\n".join(lines + parent_actions(record))
    if outcome == "no-changes":
        lines.append("Изменений в коде нет — забирать нечего, ветка `{}` пустая.".format(
            record["chip_branch"]))
        return "\n".join(lines + parent_actions(record))

    chip = record["chip_branch"]
    lines.append("Ветка `{}`, коммитов: {}.".format(chip, record.get("commits")))
    if outcome == "merged":
        lines += [
            "Влито в `{}` (коммит {}).".format(record["parent_branch"],
                                               (record.get("outcome_detail") or "")[:12]),
            "Чтобы увидеть: `git switch {}` в {}.".format(record["parent_branch"],
                                                          record["repo_root"]),
        ]
        return "\n".join(lines + parent_actions(record))

    reasons = {
        "branch-busy": "ветка `{}` занята рабочим деревом {} — автомерж в занятую ветку "
                       "перезаписал бы дерево живой сессии".format(
                           record.get("parent_branch"), record.get("outcome_detail")),
        "conflict": "мерж дал конфликт и был отменён; конфликтуют: {}".format(
            ", ".join(record.get("conflicts") or []) or "см. git"),
        "no-parent-branch": "родительская ветка не найдена (detached HEAD или ветка удалена)",
        "merge-unavailable": "не удалось подготовить временное дерево для мержа: {}".format(
            record.get("outcome_detail")),
    }
    lines += [
        "Автомерж не выполнен: {}.".format(reasons.get(outcome, outcome)),
        "",
        "Забрать так — из {}:".format(record["repo_root"]),
        "",
        "```bash",
        "git merge --no-ff {}".format(chip),
        "```",
    ]
    if record.get("bundle"):
        lines += [
            "",
            "Если ветки уже нет (worktree удалён, ветка подчищена) — те же коммиты лежат в "
            "bundle, поверх `{}`:".format((record.get("base_commit") or "")[:12]),
            "",
            "```bash",
            'git fetch "{}" {}:{}'.format(record["bundle"].replace("\\", "/"), chip, chip),
            "```",
        ]
    elif record.get("bundle_error"):
        lines += ["", "Запасной bundle создать не удалось ({}) — ветка `{}` единственный "
                      "носитель работы, не удаляй её до мержа.".format(
                          record["bundle_error"], chip)]
    return "\n".join(lines + parent_actions(record))


def cmd_close(args):
    record = read_json(record_path(args.chip))
    if not record:
        return fail("нет карточки чипа {}".format(args.chip))
    if record["status"] == "open":
        return fail("чип {} ещё не отчитался — принимать нечего".format(args.chip))
    record["status"] = "accepted" if args.accept else "rework"
    record["closed_ts"] = time.time()
    record["verdict"] = args.rework if args.rework else None
    save_record(record)

    child = record.get("child_session_id")
    if args.accept:
        print("Чип {} принят.".format(args.chip))
        if child:
            print("Заархивируй дочернюю сессию: archive_session session_id={}".format(child))
        else:
            print("Идентификатор дочерней сессии не записан — найди её через list_sessions.")
        return 0
    print("Чип {} отправлен на доработку. Пошли в дочернюю сессию{}:".format(
        args.chip, " {}".format(child) if child else " (найди её через list_sessions)"))
    print("")
    print("Чип «{}» не принят. {}".format(record.get("title") or args.chip, args.rework))
    return 0


def cmd_status(args):
    if args.session:
        records = records_for_parent(args.session)
    elif os.path.isdir(CHIP_DIR):
        records = [read_json(os.path.join(CHIP_DIR, name))
                   for name in sorted(os.listdir(CHIP_DIR)) if name.endswith(".json")]
    else:
        records = []
    rows = [r for r in records if r and r.get("chip_id")
            and (args.all or r["status"] != "accepted")]
    if not rows:
        print("открытых чипов нет")
        return 0
    for record in sorted(rows, key=lambda r: r.get("created_ts") or 0):
        print("{}  {:<12} {:<10} {}".format(
            record["chip_id"], record["status"], record["mode"],
            record.get("title") or record.get("chip_branch") or ""))
    return 0


def hook_stop():
    payload = hc.read_payload()
    if payload is None:
        return 0
    closing = bool(GATE_RECEIPT.search(payload.get("last_assistant_message") or ""))
    if child_reminder(payload.get("cwd") or os.getcwd(), closing):
        return 0
    parent_reminder(payload.get("session_id"))
    return 0


def child_reminder(cwd, closing):
    """Block a chip session that is closing out work without having handed it back."""
    record = record_for_tree(cwd)
    if not record or not closing or record.get("blocks", 0) >= MAX_BLOCKS:
        return False
    if record["status"] != "open" and record.get("notified"):
        return False

    where = " с session_id {}".format(record["parent_session_id"]) if record.get(
        "parent_session_id") else ""
    if record["status"] == "open":
        reason = ("Это worktree чипа «{}». Работа закрывается, но родительская сессия её не "
                  "получит.\nЗакоммить всё и выполни:\n  {}\nЗатем отправь напечатанный текст "
                  "в родительскую сессию через {}{}.").format(
                      record.get("title"), finish_command(record), NOTIFY_TOOL, where)
    else:
        reason = ("Чип «{}» подготовлен к передаче, но родительская сессия не уведомлена.\n"
                  "Отправь итог через {}{}.").format(record.get("title"), NOTIFY_TOOL, where)

    record["blocks"] = record.get("blocks", 0) + 1
    try:
        save_record(record)
    except Exception:
        return False
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return True


def parent_reminder(session_id):
    """Tell the parent once that a chip reported and is still unverified. Never blocks."""
    if not session_id:
        return
    pending = [r for r in records_for_parent(session_id)
               if r.get("notified") and r["status"] == "handed-off" and not r.get("reminded")]
    if not pending:
        return
    listed = ", ".join("{} «{}»".format(r["chip_id"], r.get("title")) for r in pending)
    print(json.dumps({"systemMessage": (
        "Чипы отчитались и ждут приёмки: {}. Проверь результат и закрой их — "
        "chip_handoff.py close --chip <id> --accept | --rework \"...\"".format(listed))},
        ensure_ascii=False))
    # Marked only after the reminder is out: a write that fails costs a repeat, not silence.
    for record in pending:
        record["reminded"] = True
        try:
            save_record(record)
        except Exception:
            return


def hook_notified():
    payload = hc.read_payload()
    if payload is None or payload.get("tool_name") != NOTIFY_TOOL:
        return 0
    target = (payload.get("tool_input") or {}).get("session_id")
    sender = payload.get("session_id")
    record = record_for_tree(payload.get("cwd") or os.getcwd())
    if record and record.get("parent_session_id") and target != record["parent_session_id"]:
        return 0
    if not record:
        # A chip with no worktree of its own is only recognised by who it writes to, so it is
        # claimed solely when the parent has exactly one report outstanding. Any other message
        # to that parent — a peer session, a second chip — would otherwise mark the wrong one
        # notified and disarm its reminder.
        waiting = [r for r in records_for_parent(target)
                   if r["status"] == "handed-off" and not r.get("notified")
                   and r.get("child_session_id") in (None, sender)]
        if len(waiting) != 1:
            return 0
        record = waiting[0]
    if record.get("child_session_id") in (None, sender):
        record["child_session_id"] = sender or record.get("child_session_id")
    # Only the report itself counts. A chip that writes to its parent mid-work would otherwise
    # mark itself delivered and silence the block that makes it hand the work back.
    if record["status"] == "handed-off":
        record["notified"] = True
        record["notified_ts"] = time.time()
    try:
        save_record(record)
    except Exception:
        pass
    return 0


def fail(message):
    sys.stderr.write(message.rstrip() + "\n")
    return 2


def main():
    parser = argparse.ArgumentParser(prog="chip_handoff")
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open", help="record a chip and, for code work, cut its worktree")
    opener.add_argument("--title", required=True)
    opener.add_argument("--session", help="parent sessionId, from get_session self")
    opener.add_argument("--operational", action="store_true",
                        help="work with no code to hand back: no worktree, report only")
    opener.add_argument("--cwd")

    finisher = sub.add_parser("finish", help="merge or hand back the chip's work")
    finisher.add_argument("--message", help="one-line summary for the parent")
    finisher.add_argument("--chip", help="chip id, for a chip with no worktree of its own")
    finisher.add_argument("--cwd")

    closer = sub.add_parser("close", help="record the parent's verdict on a reported chip")
    closer.add_argument("--chip", required=True)
    verdict = closer.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true")
    verdict.add_argument("--rework", metavar="TEXT")

    lister = sub.add_parser("status", help="chips still waiting on somebody")
    lister.add_argument("--session", help="limit to chips spawned by this sessionId")
    lister.add_argument("--all", action="store_true")

    sub.add_parser("hook-stop")
    sub.add_parser("hook-notified")

    args = parser.parse_args()
    if args.command == "open":
        return cmd_open(args)
    if args.command == "finish":
        return cmd_finish(args)
    if args.command == "close":
        return cmd_close(args)
    if args.command == "status":
        return cmd_status(args)
    try:
        return hook_stop() if args.command == "hook-stop" else hook_notified()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
