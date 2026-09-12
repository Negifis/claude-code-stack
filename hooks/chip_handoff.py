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
is done without a handoff is the failure this exists to prevent. In the parent, and only in the
session that opened the chip, it repeats without blocking until the chip is closed, because a
report nobody acted on is the other half of the same failure. Fail-open everywhere: a session
must never fail to end because its bookkeeping did.

Two things this file is careful about. A session has two unrelated ids — the `local_…` one the
session tools address and the transcript one every hook payload carries — so both are recorded,
and only the first is ever offered to `archive_session`. And a card is written by three parties
at once, so every mutation of a card or an index happens under `chip_lock`; a writer that
cannot take it drops its bookkeeping rather than bury somebody else's verdict.
"""
import argparse
import contextlib
import glob
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
BY_SPAWN = os.path.join(CHIP_DIR, "by-spawn")
LOCK_PATH = os.path.join(CHIP_DIR, ".lock")
LOCK_TIMEOUT = 3.0
LOCK_STALE = 60.0
MAX_BLOCKS = 3
SESSION_REGISTRY = os.path.join(os.environ.get("APPDATA") or
                                os.path.expanduser("~/AppData/Roaming"),
                                "Claude", "claude-code-sessions")
# Outside CHIP_DIR on purpose: `status` reads that directory as cards.
SESSION_MAP_CACHE = os.path.join(hc.STATE_DIR, "session-map.json")
SESSION_MAP_TTL = 300.0
SPAWN_TOOL = "mcp__ccd_session__spawn_task"
PENDING_GRACE = 600.0
# The footer carries this so a re-spawn of the same prompt is recognised by the chip it
# names, not by prose that merely looks like a handoff block.
CHIP_TOKEN_RE = re.compile(r"<!-- chip:([0-9a-f]{8}) -->")
# The session ids the session-management tools address. Anything else — a transcript id, a
# typo, a paraphrase — must never reach `archive_session`, so the shape is checked, not just
# the prefix.
CCD_SESSION_RE = re.compile(
    r"^local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
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


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def save_record(record):
    return save_json(record_path(record["chip_id"]), record)


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


def hook_session_id():
    """This session's transcript id — the only id a hook payload ever carries.

    A hook never sees the `local_…` session id the session-management tool uses, and the two
    are different uuids, not one with a prefix. So a chip is indexed under both, and
    `session_map` converts between them.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def session_map(force=False):
    """{transcript id: `local_…` id} and back, from the app's own session registry.

    The registry is the only place the two id spaces meet: each `local_<id>.json` states its
    own `sessionId` and the `cliSessionId` of the transcript it runs. Reading all of it costs
    a few hundred small files, far too much for a hook that runs every turn, so the pairs are
    cached and rebuilt on a timer — an id pairing never changes once written, only new ones
    appear.
    """
    cached = read_json(SESSION_MAP_CACHE)
    if not force and cached and time.time() - (cached.get("built_ts") or 0) < SESSION_MAP_TTL:
        return cached.get("pairs") or {}
    known_misses = set((cached or {}).get("misses") or ())
    claims, pairs = {}, {}
    try:
        for entry in glob.glob(os.path.join(SESSION_REGISTRY, "**", "local_*.json"),
                               recursive=True):
            data = read_json(entry) or {}
            ccd, transcript = data.get("sessionId"), data.get("cliSessionId")
            if ccd and transcript and is_ccd_session_id(ccd):
                claims.setdefault(transcript, set()).add(ccd)
    except Exception:
        return (cached or {}).get("pairs") or {}
    # A transcript claimed by two sessions identifies neither: publishing either one would send
    # a report, or an archive command, to a session that never opened the chip.
    for transcript, owners in claims.items():
        if len(owners) == 1:
            ccd = owners.pop()
            pairs[transcript] = ccd
            pairs[ccd] = transcript
    try:
        save_json(SESSION_MAP_CACHE, {"built_ts": time.time(), "pairs": pairs,
                                      "misses": sorted(known_misses)})
    except Exception:
        pass
    return pairs


def remember_miss(any_id):
    """Record an id the registry does not pair, so the next turn does not rescan for it.

    A session that is not in the registry at all — and the Stop hook runs in every one of them —
    would otherwise force a full rescan on every single turn.
    """
    cached = read_json(SESSION_MAP_CACHE) or {}
    misses = set(cached.get("misses") or ())
    if any_id in misses:
        return
    misses.add(any_id)
    cached["misses"] = sorted(misses)
    try:
        save_json(SESSION_MAP_CACHE, cached)
    except Exception:
        pass


def registry_misses():
    return set((read_json(SESSION_MAP_CACHE) or {}).get("misses") or ())


def ccd_for_transcript(transcript_id):
    """The `local_…` id of the session running this transcript, or None."""
    if not transcript_id:
        return None
    found = session_map().get(transcript_id)
    if not is_ccd_session_id(found) and transcript_id not in registry_misses():
        found = session_map(force=True).get(transcript_id)
        if not is_ccd_session_id(found):
            remember_miss(transcript_id)
    return found if is_ccd_session_id(found) else None


def session_ids(any_id):
    """Both ids of one session, in the order a lookup should try them.

    A resumed parent arrives with a transcript id minted after the cache was built, so an
    unknown id is worth one rebuild: without it the parent hears nothing about its chips until
    the cache expires, which is the whole failure this pairing exists to prevent.
    """
    if not any_id:
        return []
    other = session_map().get(any_id)
    if not other and any_id not in registry_misses():
        other = session_map(force=True).get(any_id)
        if not other:
            remember_miss(any_id)
    return [any_id] + ([other] if other and other != any_id else [])


def is_ccd_session_id(value):
    """Whether this is an id the session tools address, rather than a transcript id."""
    return bool(value) and bool(CCD_SESSION_RE.match(str(value)))


@contextlib.contextmanager
def chip_lock(timeout=LOCK_TIMEOUT):
    """Serialize every write to a card and its indexes; yields whether the lock was taken.

    A card is read, changed and written by three parties — the child's `finish`, the send
    hook, the parent's `close` — and each also moves index entries. Unserialized, a hook that
    read a card before the parent closed it writes the verdict away and leaves an accepted chip
    looking unaccepted. Callers must not write when the lock was refused; a mutation dropped is
    recoverable, a lost verdict is not.
    """
    os.makedirs(CHIP_DIR, exist_ok=True)
    deadline = time.time() + timeout
    held = False
    while True:
        try:
            os.mkdir(LOCK_PATH)
            held = True
            break
        except FileExistsError:
            try:
                # A process killed mid-write would otherwise wedge every later chip operation.
                if time.time() - os.path.getmtime(LOCK_PATH) > LOCK_STALE:
                    os.rmdir(LOCK_PATH)
                    continue
            except Exception:
                pass
            if time.time() >= deadline:
                break
            time.sleep(0.02)
        except Exception:
            break
    try:
        yield held
    finally:
        if held:
            try:
                os.rmdir(LOCK_PATH)
            except Exception:
                pass


def index_remove(directory, key, chip_id):
    """Drop one id from an index. A closed chip must stop costing the Stop hook a file read."""
    path = os.path.join(directory, safe_key(key))
    remaining = [c for c in index_read(directory, key) if c != chip_id]
    try:
        if remaining:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(remaining) + "\n")
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def record_for_tree(cwd):
    ids = index_read(BY_TREE, hc.tree_key(cwd))
    return read_json(record_path(ids[-1])) if ids else None


def records_from_index(directory, key):
    found = []
    for chip_id in index_read(directory, key):
        record = read_json(record_path(chip_id))
        if record:
            found.append(record)
    return found


def records_for_parent(session_id):
    return records_from_index(BY_PARENT, session_id)


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


class ChipError(Exception):
    """A chip could not be opened. Carries the message the CLI prints and the hook swallows."""


def cmd_open(args):
    try:
        record = create_chip(args.title, args.cwd, args.session, args.operational)
    except ChipError as exc:
        return fail(str(exc))
    print_open_result(record)
    return 0


def create_chip(title, cwd, session, operational, transcript=None, tool_use_id=None):
    """Register a chip and, for code work, cut its worktree. Raises ChipError."""
    cwd = os.path.abspath(cwd or os.getcwd())
    transcript = transcript or hook_session_id()
    # The parent almost never knows its own `local_…` id, and asking for it was one more thing
    # to forget; the app's own session registry converts the transcript id the hook does see.
    session = session or ccd_for_transcript(transcript)
    chip_id = uuid.uuid4().hex[:8]
    record = {
        "chip_id": chip_id,
        "spawn_tool_use_id": tool_use_id,
        "title": title,
        "mode": "operational" if operational else "code",
        "created_ts": time.time(),
        # Two id spaces that never convert: a `local_…` id addresses a session for
        # `send_message` and `archive_session`, a transcript id is all a hook payload carries.
        # Each side records both so the reminder can find a chip and the parent can reach it.
        "parent_session_id": session,
        "parent_hook_session": transcript,
        "parent_cwd": cwd,
        # A chip cut by the spawn hook is not real until the spawn lands: the tool can
        # still be denied or cancelled, and a card left behind would outlive its session.
        "status": "pending" if tool_use_id else "open",
        "outcome": None,
        "notified": False,
        "delivery_attempted": False,
        "child_session_id": None,
        "child_hook_session": None,
        "blocks": 0,
    }

    if not operational:
        ok, repo_root, err = git(cwd, "rev-parse", "--show-toplevel", timeout=10)
        if not ok:
            raise ChipError("не git-репозиторий: {}\nдля работы без кода добавь --operational\n{}"
                        .format(cwd, err))
        repo_root = os.path.abspath(repo_root)
        ok, head, err = git(repo_root, "rev-parse", "--verify", "HEAD", timeout=10)
        if not ok:
            raise ChipError("в репозитории нет коммитов, не от чего ветвиться\n{}".format(err))
        _, branch, _ = git(repo_root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
        parent_branch = None if branch in ("", "HEAD") else branch
        slug = slugify(title)
        chip_branch = "chip/{}-{}".format(slug, chip_id)
        worktree = os.path.join(
            TREE_ROOT, "{}-{}-{}".format(os.path.basename(repo_root), slug, chip_id))
        os.makedirs(TREE_ROOT, exist_ok=True)
        ok, _, err = git(repo_root, "worktree", "add", "-b", chip_branch, worktree,
                         parent_branch or head, timeout=20)
        if not ok:
            raise ChipError("не удалось создать worktree {}\n{}".format(worktree, err))
        record.update({
            "repo_root": repo_root, "parent_branch": parent_branch, "worktree": worktree,
            "chip_branch": chip_branch, "base_sha": head,
        })
    with chip_lock() as held:
        if not held:
            discard_chip(record)
            raise ChipError("карточки чипов заняты другой операцией — чип не заведён")
        try:
            save_record(record)
            if not operational:
                index_write(BY_TREE, hc.tree_key(worktree), chip_id)
            if tool_use_id:
                index_write(BY_SPAWN, tool_use_id, chip_id)
            for key in (session, transcript):
                if key:
                    index_write(BY_PARENT, key, chip_id, append=True)
        except Exception as exc:
            # Through the same cleanup as an abandoned chip: a half-written card left behind
            # would later be finalized as a chip whose worktree no longer exists.
            discard_chip(record)
            raise ChipError("не удалось записать карточку чипа, worktree убран\n{}".format(exc))
    return record


def print_open_result(record):
    print("chip:     {}".format(record["chip_id"]))
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
    return ('"{}" "{}" finish{} --child-session <свой sessionId> '
            '--message "<что сделано, одной строкой>"').format(
        sys.executable, os.path.abspath(__file__), target)


def handoff_footer(record):
    lines = ["<!-- chip:{} -->".format(record["chip_id"]),
             "## Возврат работы родителю", ""]
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
    lines += [
        "   `--child-session` возьми из `mcp__ccd_session_mgmt__get_session` с `\"self\"`, "
        "поле `sessionId` — без него родитель не сможет тебя ни заархивировать, ни вернуть "
        "на доработку.",
        "",
        "```bash", finish_command(record), "```", "",
    ]
    lines.append("3. Отправь напечатанный им текст в родительскую сессию через "
                 "`{}`".format(NOTIFY_TOOL))
    if record.get("parent_session_id"):
        lines.append("   с `session_id: {}`.".format(record["parent_session_id"]))
    else:
        lines.append("   (идентификатор родительской сессии не записан — найди его через "
                     "`list_sessions`).")
    lines += [
        "   Если отправка не проходит — родитель запущен по расписанию и недоступен для "
        "сообщений, — это не твоя ошибка: отчёт уже лежит в карточке чипа, родитель заберёт "
        "его оттуда. Повторять и обходить не нужно.",
        "",
        "Родитель проверит результат и либо примет чип, либо пришлёт правки в эту же "
        "сессию — не закрывай её до его ответа.",
    ]
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
    resolved = args.child_session or ccd_for_transcript(hook_session_id())
    # `finish` re-run by the parent while it checks the work would otherwise rebind the chip to
    # the parent, and `close --accept` would then offer to archive the session doing the
    # accepting. A binding already made is kept.
    if resolved and resolved == record.get("parent_session_id"):
        resolved = None
    if record.get("child_session_id") and not args.child_session:
        resolved = record["child_session_id"]
    args.child_session = resolved
    if args.child_session and not is_ccd_session_id(args.child_session):
        return fail("--child-session {} — это не идентификатор сессии: нужен вид "
                    "local_<uuid>, поле sessionId из get_session self"
                    .format(args.child_session))
    record["finished_ts"] = time.time()
    record["summary"] = args.message
    record["blocks"] = 0
    record["child_hook_session"] = hook_session_id() or record.get("child_hook_session")
    # A new report is a new delivery: whatever was sent for the previous one says nothing about
    # this one, and leaving the flags set would release the chip without it being sent again.
    record["notified"] = False
    record["notified_ts"] = None
    record["delivery_attempted"] = False
    if args.child_session:
        record["child_session_id"] = args.child_session

    if record["mode"] == "operational":
        record["status"] = "handed-off"
        record["outcome"] = "reported"
        return publish_report(record)

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
        return publish_report(record)

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
    return publish_report(record)


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


def publish_report(record):
    """Write a finished report and print it, refusing to bury a verdict recorded meanwhile."""
    with chip_lock() as held:
        if not held:
            return fail("карточки чипов заняты другой операцией — повтори через несколько "
                        "секунд; отчёт не записан")
        fresh = read_json(record_path(record["chip_id"]))
        if fresh and fresh.get("status") == "accepted":
            return fail("чип {} уже принят родителем — отчёт не записан"
                        .format(record["chip_id"]))
        save_record(record)
    print(notification(record))
    return 0


def cmd_close(args):
    with chip_lock() as held:
        if not held:
            return fail("карточки чипов заняты другой операцией — повтори через несколько "
                        "секунд; вердикт не записан")
        return close_locked(args)


def close_locked(args):
    record = read_json(record_path(args.chip))
    if not record:
        return fail("нет карточки чипа {}".format(args.chip))
    if record["status"] == "open":
        return fail("чип {} ещё не отчитался — принимать нечего".format(args.chip))
    record["status"] = "accepted" if args.accept else "rework"
    record["closed_ts"] = time.time()
    record["verdict"] = args.rework if args.rework else None
    if args.rework:
        # Sending it back asks for another report, and that one has to be delivered on its own
        # merits — carrying this delivery over would let the next handoff skip the message.
        record["notified"] = False
        record["notified_ts"] = None
        record["delivery_attempted"] = False
    save_record(record)
    if args.accept:
        # Only acceptance is terminal. A chip sent back reports again, so it keeps its place in
        # the indexes; an accepted one must stop being read on every Stop hook, forever.
        for key in (record.get("parent_session_id"), record.get("parent_hook_session")):
            if key:
                index_remove(BY_PARENT, key, record["chip_id"])

    # Only the `local_…` id the child read from `get_session self` addresses a session; the
    # transcript id a hook records names a log file and archive_session rejects it.
    child = record.get("child_session_id")
    if not is_ccd_session_id(child):
        child = None
    hint = ("" if child else
            " Найди её в list_sessions по названию «{}»{}.".format(
                record.get("title") or args.chip,
                " (её транскрипт — {})".format(record["child_hook_session"])
                if record.get("child_hook_session") else ""))
    if args.accept:
        print("Чип {} принят.".format(args.chip))
        if child:
            print("Заархивируй дочернюю сессию: archive_session session_id={}".format(child))
        else:
            print("Идентификатор дочерней сессии не записан." + hint)
        return 0
    print("Чип {} отправлен на доработку. Пошли в дочернюю сессию{}:".format(
        args.chip, " {}".format(child) if child else "." + hint))
    print("")
    print("Чип «{}» не принят. {}".format(record.get("title") or args.chip, args.rework))
    return 0


def cmd_status(args):
    # Always the cards themselves, never the index: the index carries only what is still
    # pending, so reading it here would make `--all` mean different things with and without
    # `--session`. This runs by hand, not in a hook, so a small directory listing is free.
    records = []
    if os.path.isdir(CHIP_DIR):
        records = [read_json(os.path.join(CHIP_DIR, name))
                   for name in sorted(os.listdir(CHIP_DIR)) if name.endswith(".json")]
    rows = [r for r in records if r and r.get("chip_id")
            and (args.all or r["status"] != "accepted")
            and (not args.session or args.session in (r.get("parent_session_id"),
                                                      r.get("parent_hook_session")))]
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
    cwd = payload.get("cwd") or os.getcwd()
    closing = bool(GATE_RECEIPT.search(payload.get("last_assistant_message") or ""))
    if child_reminder(cwd, closing):
        return 0
    parent_reminder(payload.get("session_id"))
    return 0


def child_reminder(cwd, closing):
    """Block a chip session that is closing out work without having handed it back."""
    record = record_for_tree(cwd)
    if not record or not closing or record.get("blocks", 0) >= MAX_BLOCKS:
        return False
    # A chip the parent has already ruled on is finished with its child, whether or not a
    # message was ever delivered; nagging it then asks for something nobody is waiting for.
    if record["status"] in ("accepted", "rework", "pending"):
        return False
    # An attempt is enough once the report is written. A parent that runs unattended cannot be
    # messaged at all, so demanding a delivered notification would hold such a chip hostage to
    # something it can never achieve — the card is the delivery in that case.
    if record["status"] != "open" and (record.get("notified")
                                       or record.get("delivery_attempted")):
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

    with chip_lock() as held:
        # Without the lock the count is written from a copy read before the parent may have
        # closed the chip, and the verdict is lost. A block skipped costs one reminder.
        if not held:
            return False
        fresh = read_json(record_path(record["chip_id"]))
        if not fresh or fresh.get("status") not in ("open", "handed-off", "rework"):
            return False
        fresh["blocks"] = fresh.get("blocks", 0) + 1
        try:
            save_record(fresh)
        except Exception:
            return False
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return True


def parent_reminder(session_id):
    """Tell the parent a chip is reported and still unverified. Never blocks.

    Deliberately repeats until the chip is closed: a report the parent never acted on is
    exactly the state this exists to surface, and a nudge that fires once is a nudge that gets
    scrolled past.
    """
    if not session_id:
        return
    seen, pending = set(), []
    # Both ids of the session, because a resumed parent keeps its `local_…` id while its
    # transcript id changes: keyed only by the transcript, it would never hear about the chips
    # it opened before the resume. Identity is the session and nothing else — a directory is
    # where a chip was opened, not who owns it, and two sessions share a checkout routinely.
    for key in session_ids(session_id):
        for record in records_for_parent(key):
            chip_id = record.get("chip_id")
            if chip_id in seen:
                continue
            seen.add(chip_id)
            # `notified` is deliberately not required: an undelivered report — the parent runs
            # unattended, the send failed — most needs the parent's attention.
            if record.get("status") == "handed-off":
                pending.append(record)
    if not pending:
        return
    listed = ", ".join("{} «{}»".format(r["chip_id"], r.get("title")) for r in pending)
    print(json.dumps({"systemMessage": (
        "Чипы отчитались и ждут приёмки: {}. Проверь результат сам и закрой каждый — "
        "chip_handoff.py close --chip <id> --accept | --rework \"...\"; "
        "после --accept заархивируй дочернюю сессию, после --rework отправь ей правки."
        .format(listed))}, ensure_ascii=False))


def spawn_mode(cwd):
    """(operational, repo_root) for a directory a chip is about to be spawned into.

    Operational unless there is a commit to branch from. A repository without one reads as code
    work by its top level alone, and cutting a worktree there fails — which used to leave the
    chip with no card and no way back at all, so an unbranchable repository reports instead.
    """
    ok, repo_root, _ = git(cwd, "rev-parse", "--show-toplevel", timeout=10)
    if not ok:
        return True, None
    has_head, _, _ = git(repo_root, "rev-parse", "--verify", "HEAD", timeout=10)
    return (not has_head), (os.path.abspath(repo_root) if has_head else None)


def sweep_pending(now=None):
    """Settle chips whose spawn was never confirmed, by adopting them — never by deleting.

    A chip is cut before the tool it belongs to runs, because the child needs its directory to
    exist the moment it starts. Whether that spawn then happened is knowable only from the
    confirmation hook, and a session started before that hook existed sends none. Nothing else
    tells the two apart: a child can read for ten minutes before its first edit, and an
    operational chip has no worktree to inspect at all. So an unconfirmed chip is treated as
    real. A chip that truly never became a session leaves behind a clean worktree, which
    `tools/worktree-audit.mjs` already owns; losing a live child's work would be unrecoverable,
    and that asymmetry decides this.
    """
    now = now or time.time()
    for name in (os.listdir(BY_SPAWN) if os.path.isdir(BY_SPAWN) else []):
        for chip_id in index_read(BY_SPAWN, name):
            record = read_json(record_path(chip_id))
            if not record or record.get("status") != "pending":
                # A card that is gone or has moved on leaves nothing to settle; its entry here
                # would otherwise be read by every later spawn forever.
                index_remove(BY_SPAWN, name, chip_id)
                continue
            if now - (record.get("created_ts") or 0) < PENDING_GRACE:
                continue
            adopt_pending(chip_id)


def adopt_pending(chip_id):
    """Promote one unconfirmed chip to a real one, under the lock and on a fresh read."""
    with chip_lock() as held:
        if not held:
            return
        fresh = read_json(record_path(chip_id))
        if not fresh or fresh.get("status") != "pending":
            return
        fresh["status"] = "open"
        try:
            save_record(fresh)
            if fresh.get("spawn_tool_use_id"):
                index_remove(BY_SPAWN, fresh["spawn_tool_use_id"], chip_id)
        except Exception:
            pass


def discard_chip(record):
    """Remove a chip whose spawn was explicitly refused. Returns whether it went.

    Never forces anything. A worktree that holds work refuses to be removed, and the branch is
    only deleted while it still points at the commit it was cut from — so a child that started
    working between any check and this call keeps everything, and the bookkeeping stays with it
    rather than being deleted out from under a directory that survived.
    """
    repo_root, worktree = record.get("repo_root"), record.get("worktree")
    if worktree and repo_root:
        ok, _, _ = git(repo_root, "worktree", "remove", worktree, timeout=15)
        if not ok:
            return False
        branch, base = record.get("chip_branch"), record.get("base_sha")
        if branch and base:
            ok, tip, _ = git(repo_root, "rev-parse", "--verify", "refs/heads/" + branch,
                             timeout=10)
            if ok and tip == base:
                git(repo_root, "branch", "-D", branch, timeout=10)
        index_remove(BY_TREE, hc.tree_key(worktree), record["chip_id"])
    for key in (record.get("parent_session_id"), record.get("parent_hook_session")):
        if key:
            index_remove(BY_PARENT, key, record["chip_id"])
    if record.get("spawn_tool_use_id"):
        index_remove(BY_SPAWN, record["spawn_tool_use_id"], record["chip_id"])
    try:
        os.remove(record_path(record["chip_id"]))
    except OSError:
        pass
    return True


def record_for_spawn(tool_use_id):
    if not tool_use_id:
        return None
    ids = index_read(BY_SPAWN, tool_use_id)
    return read_json(record_path(ids[-1])) if ids else None


def already_registered(prompt):
    """Whether this prompt already carries a handoff for a chip that exists.

    Keyed on the chip id the footer embeds rather than on the visible heading: a task about
    this tooling quotes that heading, and treating the quote as proof of registration left the
    chip with no card at all.
    """
    for chip_id in CHIP_TOKEN_RE.findall(prompt or ""):
        if read_json(record_path(chip_id)):
            return True
    return False


def chip_cwd(worktree, parent_cwd, repo_root):
    """Where inside the chip worktree the child should start.

    The caller may have pointed it at one package of a monorepo, and the same place in the
    chip worktree is what that meant. Anything that cannot be expressed there — another drive,
    a path outside the repository, a directory that did not exist at the base commit — falls
    back to the worktree root rather than failing the spawn.
    """
    if not repo_root:
        return worktree
    try:
        inside = os.path.relpath(parent_cwd, repo_root)
    except ValueError:
        return worktree
    target = os.path.normpath(os.path.join(worktree, inside))
    if not os.path.isdir(target):
        return worktree
    root = os.path.normcase(os.path.abspath(worktree))
    if not os.path.normcase(os.path.abspath(target)).startswith(root):
        return worktree
    return target


def hook_spawn():
    """Register the chip `spawn_task` is about to create, and hand it its way back.

    Everything else here assumed somebody ran `open` first. Nobody did: across every transcript
    on this machine `spawn_task` was called hundreds of times and `open` once. A procedure the
    model has to remember at the exact moment it is delegating is a procedure that does not
    run, so the registration moves to the only place that cannot be skipped — the spawn itself.

    The chip is cut here and stays `pending` until the spawn actually lands, and the tool input
    is rewritten to carry the handoff block and, for code work, the chip worktree as the child
    directory. The permission decision is left alone: this hook exists to add a route home, not
    to approve delegating. Fail-open in every branch — a chip that spawns without a card is the
    old behaviour, while a hook that raises would stop the user delegating at all.
    """
    payload = hc.read_payload()
    if payload is None or payload.get("tool_name") != SPAWN_TOOL:
        return 0
    spawn = payload.get("tool_input")
    if not isinstance(spawn, dict):
        return 0
    if already_registered(spawn.get("prompt") or ""):
        return 0
    try:
        sweep_pending()
    except Exception:
        pass

    parent_cwd = os.path.abspath(spawn.get("cwd") or payload.get("cwd") or os.getcwd())
    operational, repo_root = spawn_mode(parent_cwd)
    try:
        record = create_chip(spawn.get("title") or "Чип", parent_cwd, None, operational,
                             transcript=payload.get("session_id") or hook_session_id(),
                             tool_use_id=payload.get("tool_use_id"))
    except Exception:
        return 0

    updated = dict(spawn)
    updated["prompt"] = (spawn.get("prompt") or "").rstrip() + "\n\n" + handoff_footer(record)
    if record["mode"] == "code":
        updated["cwd"] = chip_cwd(record["worktree"], parent_cwd, repo_root)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
    }}, ensure_ascii=False))
    return 0


def hook_spawned(failed=False):
    """Finalize or discard the chip once the spawn itself has landed."""
    payload = hc.read_payload()
    if payload is None or payload.get("tool_name") != SPAWN_TOOL:
        return 0
    record = record_for_spawn(payload.get("tool_use_id"))
    if not record or record.get("status") != "pending":
        return 0
    with chip_lock() as held:
        if not held:
            return 0
        fresh = read_json(record_path(record["chip_id"]))
        if not fresh or fresh.get("status") != "pending":
            return 0
        if failed:
            discard_chip(fresh)
            return 0
        fresh["status"] = "open"
        try:
            save_record(fresh)
            index_remove(BY_SPAWN, fresh["spawn_tool_use_id"], fresh["chip_id"])
        except Exception:
            pass
    return 0


def hook_notified(failed=False):
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
                   and r.get("child_hook_session") in (None, sender)]
        if len(waiting) != 1:
            return 0
        record = waiting[0]
    # Re-read under the lock and write in the same critical section: the parent may close this
    # chip at any moment, and writing a copy read before that would drop the verdict and leave
    # an accepted chip looking unaccepted. A refused lock drops this bookkeeping rather than
    # risk that — the child is released by its own `finish`, not by this flag alone.
    with chip_lock() as held:
        if not held:
            return 0
        fresh = read_json(record_path(record["chip_id"]))
        if not fresh:
            return 0
        if fresh.get("child_hook_session") in (None, sender):
            fresh["child_hook_session"] = sender or fresh.get("child_hook_session")
        # Only the report itself counts. A chip that writes to its parent mid-work would
        # otherwise mark itself delivered and silence the block that hands the work back.
        if fresh.get("status") == "handed-off":
            fresh["delivery_attempted"] = True
            if not failed:
                fresh["notified"] = True
                fresh["notified_ts"] = time.time()
        try:
            save_record(fresh)
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
    finisher.add_argument("--child-session", dest="child_session",
                          help="this session's own sessionId, from get_session self — the only "
                               "form archive_session accepts")
    finisher.add_argument("--cwd")

    closer = sub.add_parser("close", help="record the parent's verdict on a reported chip")
    closer.add_argument("--chip", required=True)
    verdict = closer.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true")
    verdict.add_argument("--rework", metavar="TEXT")

    lister = sub.add_parser("status", help="chips still waiting on somebody")
    lister.add_argument("--session", help="limit to chips spawned by this sessionId")
    lister.add_argument("--all", action="store_true")

    sub.add_parser("hook-spawn")
    sub.add_parser("hook-spawned")
    sub.add_parser("hook-spawn-failed")
    sub.add_parser("hook-stop")
    sub.add_parser("hook-notified")
    sub.add_parser("hook-notify-failed")

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
        if args.command == "hook-spawn":
            return hook_spawn()
        if args.command == "hook-spawned":
            return hook_spawned()
        if args.command == "hook-spawn-failed":
            return hook_spawned(failed=True)
        if args.command == "hook-stop":
            return hook_stop()
        return hook_notified(failed=args.command == "hook-notify-failed")
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
