"""
Chip handoff — a spawned task returns its work to the session that spawned it.

`spawn_task` hands the child a prompt and a directory and nothing else: no parent branch, no
parent session, no way back. So the child finishes somewhere nobody looks, and the work is
found later by accident or not at all.

This binds the two ends. `open` records who spawned the chip and, for work that touches code,
cuts a worktree and a branch off the parent's HEAD so neither session edits the other's tree.
`finish` merges into the parent branch when that branch is checked out nowhere, and when it is
not — the ordinary case, because the parent is sitting on it — leaves the branch and a bundle
and says exactly how to pull them in. It does not merge either when the parent's own tree is
gone, as archiving the parent removes it: nobody would check that merge, so the report names
the first working tree of the repository that still stands. Operational work has nothing to
pull and reports its effect instead. Either way `finish` prints the message the child sends to
the parent, and `close` records the parent's verdict once the parent has checked the result:
accepted, and the child session can be archived, or sent back with what is missing.

`start_session`, which replaces `spawn_task` in the desktop app, starts the child itself in a
worktree the app cuts, so for it the spawn hook cuts nothing: it names the child's branch after
the chip, cuts it from the parent's branch, and `finish` finds the tree when the child reports.

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

import hygiene_common as hc

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
# The desktop app's successor to `spawn_task` (read from app 2.42304): it starts a sibling
# session in a worktree it cuts itself, from `source_branch`, on a branch named after `branch_name`.
START_TOOL = "mcp__ccd_session__start_session"
SPAWN_TOOLS = (SPAWN_TOOL, START_TOOL)
# What `start_session` answers when a session started, or when it is waiting as a card: each kind
# by its whole first line, so an id is taken only from the answer of its own kind. The title is
# matched greedily, so the id is the one after it, whatever ids the title itself spells.
STARTED_RE = re.compile(
    r'^Started session ".*" \(session_id: (local_[0-9a-f-]{36})(?:, name: [^()\n]*)?\)', re.M)
PROPOSED_RE = re.compile(r'^Proposed ".*" \(task_id: (task_[0-9a-f]{8})\)', re.M)
PENDING_GRACE = 600.0
# The footer carries this so a re-spawn of the same prompt is recognised by the chip it
# names, not by prose that merely looks like a handoff block.
CHIP_TOKEN_RE = re.compile(r"<!-- chip:([0-9a-f]{8}) -->")
# The slug the spawn hook passes as `branch_name`; the app adds the user's prefix and a suffix.
CHIP_BRANCH_RE = re.compile(r"(?:^|[/-])chip-([0-9a-f]{8})(?=$|[-_/])")
# A session's first message, where a spawned session's handoff block sits, is near the top of
# its transcript; the Stop hook reads no further than this.
TRANSCRIPT_HEAD_BYTES = 256 * 1024
# The session ids the session-management tools address. Anything else — a transcript id, a
# typo, a paraphrase — must never reach `archive_session`, so the shape is checked, not just
# the prefix.
CCD_SESSION_RE = re.compile(
    r"^local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
NOTIFY_TOOL = "mcp__ccd_session_mgmt__send_message"
# The same delivery through the messaging tool, which names the session in `to`.
PEER_NOTIFY_TOOL = "SendMessage"
# The kinds are `code_work_gate_stop.TERMINAL_RE`'s, deliberately copied rather than imported:
# each hook family here stays importable on its own. This one only asks whether the turn looks
# like a closing one, so it stays a loose search where the gate's is a strict parse: it takes a
# receipt behind a leading backtick, as the gate's `receipt_line` does, without asking for the
# closing one.
GATE_RECEIPT = re.compile(
    r"^(?:`\s*)?\[gate\]\s*(?:verified|operational|no-change|pr-ready|draft-blocked)\s*:",
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


def chip_of_branch(branch):
    """The chip id a branch is named after, or None."""
    match = CHIP_BRANCH_RE.search(branch or "")
    return match.group(1) if match else None


def head_branch(cwd):
    """The branch checked out in the working tree that holds `cwd`, or None.

    Read from the tree's own files rather than by starting git: the Stop hook asks this in every
    session, and two small reads cost nothing beside a process.
    """
    path = os.path.abspath(cwd or os.getcwd())
    while True:
        dot_git = os.path.join(path, ".git")
        if os.path.isdir(dot_git):
            git_dir = dot_git
            break
        if os.path.isfile(dot_git):
            try:
                with open(dot_git, encoding="utf-8") as handle:
                    pointer = handle.readline().strip()
            except OSError:
                return None
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = os.path.join(path, pointer[len("gitdir:"):].strip())
            break
        above = os.path.dirname(path)
        if above == path:
            return None
        path = above
    try:
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as handle:
            head = handle.readline().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def chip_of_transcript(path):
    """The chip whose handoff block ends a session's first message, or None.

    A spawned session's first message is its task, and the block the spawn hook appended to it
    ends with the chip's token. Only that message counts: a later one quoting a block is talk
    about a chip, not the task of one.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            budget = TRANSCRIPT_HEAD_BYTES
            while budget > 0:
                line = handle.readline(budget)
                if not line:
                    return None
                budget -= len(line)
                content = first_message_text(line)
                if content is not None:
                    tokens = CHIP_TOKEN_RE.findall(content)
                    return tokens[-1] if tokens else None
    except OSError:
        return None
    return None


def first_message_text(line):
    """The text of a transcript line that is a user's message, or None for any other line."""
    if '"user"' not in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        # A line cut off at the read budget; nothing past it is the first message.
        return None
    message = entry.get("message") if entry.get("type") == "user" else None
    if not isinstance(message, dict) or entry.get("isMeta"):
        return None
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(block.get("text") or "" for block in content
                           if isinstance(block, dict) and block.get("type") == "text")
    return content if isinstance(content, str) and content.strip() else None


def child_record(payload):
    """(card, proven) for the chip this session works for, or (None, False).

    A chip that cut its own worktree is found by the directory, and a child the app gave a
    worktree (`start_session`) by its branch, named after the chip when the spawn hook could name
    it: both are where only the child works, short of the parent stepping in to check the result,
    which `child_standing` rules out on either path. Failing those, the handoff block its first
    message ends with points at the chip, but proves the session its child only when the
    registry pairs it with the child the app named — a session can quote a block too.
    """
    cwd = payload.get("cwd") or os.getcwd()
    session = payload.get("session_id")
    record = record_for_tree(cwd)
    if record and child_standing(record, session) is not None:
        return record, True
    chip_id = chip_of_branch(head_branch(cwd))
    record = read_json(record_path(chip_id)) if chip_id else None
    if record and child_standing(record, session) is not None:
        return record, True
    chip_id = chip_of_transcript(payload.get("transcript_path"))
    record = read_json(record_path(chip_id)) if chip_id else None
    standing = child_standing(record, session) if record else None
    return (record, standing) if standing is not None else (None, False)


def child_standing(record, session):
    """True when the ids prove this session the child the app named, False when they prove
    nothing, None when they rule it out.

    The session that opened the chip is never its child — it can sit on the chip's branch or
    quote the block — and once the app named the child, a session the registry pairs with
    another `local_…` id is not it: a fork of the child inherits its first message, block and
    all.
    """
    own = ccd_for_transcript(session) if session else None
    if session and (session == record.get("parent_hook_session")
                    or (own and own == record.get("parent_session_id"))):
        return None
    expected = record.get("child_session_id")
    if expected and own:
        return True if own == expected else None
    return False


def locate_child_tree(record, cwd, repo):
    """(worktree, branch) the app cut for this chip's child, or None.

    The branch named after the chip is the child's wherever it is checked out. A child whose
    branch could not be named — a proposal the user started, a fork — is the tree `finish` runs
    in, as long as that is a worktree of the same repository and neither the parent's own
    checkout nor on the parent's branch: either would hand the parent its own work back.
    """
    repo_root = record.get("repo_root")
    if not repo_root:
        return None
    def childs(path, branch):
        return not same_path(path, repo_root) and not (
            branch and branch == record.get("parent_branch"))

    trees = worktrees_of(repo)
    named = [(path, branch) for path, branch in trees
             if chip_of_branch(branch) == record["chip_id"] and childs(path, branch)]
    if named:
        return next((row for row in named if under(cwd, row[0])), named[0])
    ok, top, _ = git(cwd, "rev-parse", "--show-toplevel", timeout=10)
    if not ok:
        return None
    return next(((path, branch) for path, branch in trees
                 if same_path(top, path) and childs(path, branch)), None)


def common_dir(tree):
    """The git common directory of the working tree whose top level is `tree`, or None when no
    working tree stands there.

    Only the tree's own top level answers: a directory left behind after its worktree was
    removed would otherwise answer for whatever checkout encloses it.
    """
    if not tree:
        return None
    ok, out, _ = git(tree, "rev-parse", "--path-format=absolute", "--show-toplevel",
                     "--git-common-dir", timeout=10)
    lines = out.splitlines()
    if not ok or len(lines) != 2 or not same_path(lines[0], tree):
        return None
    return os.path.abspath(lines[1])


def stands_in(tree, repo):
    """Whether a working tree of the repository whose common directory is `repo` stands at
    `tree` — not a bare repository, an empty directory left where a tree was, or a clone."""
    found = common_dir(tree)
    return bool(found and same_path(found, repo))


def repository(record):
    """The chip repository's git common directory, where questions about the repository as a
    whole are asked, or None.

    `repo_root` is the parent's working tree as it was when the chip was cut, and archiving the
    parent's session removes that tree — often before the child reports, and git cannot run in
    a directory that is gone. Every worktree of a repository shares one common directory, so the
    question goes there: the one recorded when the chip was cut, which names the repository
    whatever stands at the chip's path now, and nothing else once it has moved. A card written
    before it was recorded has only the parent's tree to say which repository the chip's tree
    must belong to; once that is gone too, nothing does — whatever stands at the chip's path
    would vouch for itself — so there is no answer.
    """
    recorded = record.get("git_common_dir")
    if recorded:
        return recorded if os.path.isdir(recorded) else None
    return common_dir(record.get("repo_root"))


def worktrees_of(repo):
    """[(path, branch)] for every worktree of this repository, the main one first; branch is
    None when detached. Raises ChipError when git cannot list them: an empty list would read as
    a repository with no chip trees, and finish would then take a nested worktree for
    uncommitted work."""
    ok, out, err = git(repo, "worktree", "list", "--porcelain")
    if not ok:
        raise ChipError("git worktree list не отработал в {}\n{}".format(repo, err))
    found, path = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            found.append([path, None])
        elif line.startswith("branch refs/heads/") and found:
            found[-1][1] = line[len("branch refs/heads/"):].strip()
    return [(os.path.realpath(pth), branch) for pth, branch in found if pth]


def same_path(one, other):
    """Whether two spellings name the same directory, as the filesystem resolves them."""
    return under(one, other) and under(other, one)


def under(path, root):
    """Whether `path` is `root` or inside it, comparing what the filesystem resolves to.

    git prints resolved paths; a card stores the path as it was built. Behind a junction or a
    short name the two spell the same directory differently, and a lexical comparison then
    fails to recognise the chip's own worktree.
    """
    root = os.path.normcase(os.path.realpath(root))
    path = os.path.normcase(os.path.realpath(path))
    return path == root or path.startswith(root + os.sep)


def dirty_outside(tree, nested):
    """What is uncommitted in `tree`, ignoring the worktrees the child cut inside it.

    A worktree nested in another shows up in its parent as an untracked directory, so a chip
    whose child cut its own tree would otherwise never look clean and could never hand
    anything back.
    """
    # `-uall` so an untracked directory does not collapse into one line and hide a file next
    # to a nested worktree; `core.quotePath=false` so a non-ASCII name arrives as itself
    # instead of octal escapes that match nothing.
    ok, out, err = git(tree, "-c", "core.quotePath=false", "status", "--porcelain", "-uall",
                       timeout=20)
    if not ok:
        return None, err
    ignored = {os.path.normcase(os.path.abspath(path)) for path in nested
               if not under(tree, path)}
    kept = []
    for line in out.splitlines():
        entry = line[3:].strip().strip('"').rstrip("/")
        if not entry:
            continue
        # Only the nested worktree itself is ignored, never a directory that contains one:
        # a file sitting beside it is somebody's unsaved work.
        full = os.path.normcase(os.path.abspath(os.path.join(tree, entry)))
        if full in ignored:
            continue
        kept.append(line)
    return "\n".join(kept), ""


def chip_trees(record, repo):
    """Every worktree the chip owns: its own and anything cut inside it.

    Distinct from `work_sources`, which ranks only the trees that carry work. Cleanliness is
    judged across all of them, and each of them has to be invisible to the others — a worktree
    nested in another shows up there as an untracked directory.
    """
    chip_tree = record.get("worktree")
    if not chip_tree:
        return []
    found = [path for path, _ in worktrees_of(repo)
             if under(path, chip_tree) and os.path.isdir(path)]
    return found or [chip_tree]


def work_sources(record, repo, parent_tip=None):
    """[(worktree, branch, commits)] carrying this chip's work, most commits first.

    The chip worktree is where the child starts, and it does not always stay on the chip
    branch: it cuts a worktree of its own inside the chip's and works there, leaving the chip
    branch empty. Reporting that branch would tell the parent there is nothing to pull while
    the work sits one branch away.

    Only worktrees inside the chip's own directory count. Where a session says it is working is
    not evidence of authorship — the app hands sessions worktrees of its own and reuses them
    between children — whereas a worktree under the chip's directory was cut by this chip's
    child and nobody else. A candidate must also descend from the commit the chip was cut from,
    so a directory that merely happens to sit there cannot pass its own history off as the
    chip's work.
    """
    chip_tree, origin = record.get("worktree"), record.get("base_sha")
    if not chip_tree or not origin:
        return []
    # Counted against the chip's own base and the parent's tip, and nothing else: subtracting
    # every ref in the repository turned a delivery the child had merely pushed, backed up or
    # branched from into nothing at all.
    excludes = ["--not", origin] + ([parent_tip] if parent_tip and parent_tip != origin else [])
    # Where the chip tree itself stands. Useful only while the chip branch has moved: before
    # the child's first commit it equals the base, and then it tells nothing apart.
    _, chip_head, _ = git(chip_tree, "rev-parse", "HEAD", timeout=20)
    chip_branch = record.get("chip_branch")

    found, seen = [], set()
    for path, branch in worktrees_of(repo):
        # Everything under the chip's directory was cut there by this chip's child, so nothing
        # is dropped: a tree is ranked, never silenced. Only the ranking decides what the
        # parent is told to take.
        if not under(path, chip_tree) or not os.path.isdir(path):
            continue
        own_tree = under(chip_tree, path)
        ok, head, _ = git(path, "rev-parse", "HEAD", timeout=20)
        if ok and head in seen:
            # Two worktrees at the same commit are one delivery; naming the copy as a second
            # source would ask the parent to chase work it has already been given.
            continue
        counted, count, _ = git(path, "rev-list", "--count", "HEAD", *excludes, timeout=20)
        # A count that could not be taken is unknown, never zero: reporting it as zero told the
        # parent there was nothing to pull while the branch held the delivery.
        unique = int(count) if counted and count.isdigit() else None
        if unique is None or unique or own_tree:
            found.append((unique, path, branch,
                          False if own_tree else borrowed_history(path, branch, chip_branch),
                          derived_from(path, chip_head, origin, own_tree)))
            if ok and head:
                seen.add(head)
    if not found:
        # The chip's own worktree is missing from the repository's list — nothing here can be
        # counted, and saying "nothing to pull" would be a guess.
        return [(chip_tree, record.get("chip_branch"), None, False)]
    # Unknown first, because it has to be looked at before anything is reported. Then history
    # that belongs to somebody else goes last, whatever its size. Then what grew out of the
    # chip's current branch, then the larger contribution, and the chip's own tree breaks a tie.
    found.sort(key=lambda row: (row[0] is not None, row[3], not row[4], -(row[0] or 0),
                                not under(chip_tree, row[1])))
    return [(path, branch, commits, borrowed)
            for commits, path, branch, borrowed, _ in found]


def borrowed_history(path, branch, chip_branch):
    """Whether this worktree stands on commits some other ref already owns.

    A tree the child cut to compare against — from the trunk, a remote-tracking branch, a
    release tag — sits on that history: the ref contains its HEAD. Work the child did itself is
    contained by nothing but its own branch and the copies of it the child pushed. Size cannot
    tell the two apart, since a trunk ahead of the parent always has more commits than the
    child wrote, so containment does.

    Asked only of trees the child cut inside the chip's: the chip's own worktree is the
    delivery by definition, and the parent branch contains its base, which would make every
    untouched chip look borrowed.
    """
    ok, out, _ = git(path, "for-each-ref", "--format=%(refname:short)", "--contains", "HEAD",
                     "refs/heads/", "refs/remotes/", "refs/tags/", timeout=20)
    if not ok:
        # Unknown ownership is treated as borrowed: promoting a tree that might be somebody
        # else's over the chip's own work is the failure this test exists to prevent.
        return True
    mine = {name for name in (branch, chip_branch) if name}
    # Every remote this repository knows, not a guessed pair: a child that pushed its work to a
    # fork, or under another name, still pushed its own work.
    ok_remotes, remotes, _ = git(path, "remote", timeout=10)
    known = [line.strip() for line in remotes.splitlines() if line.strip()] if ok_remotes else []
    mine |= {"{}/{}".format(remote, name) for name in list(mine) for remote in known}
    ok_up, upstream, _ = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                             timeout=10)
    if ok_up and upstream.strip():
        mine.add(upstream.strip())
    return any(name.strip() and name.strip() not in mine for name in out.splitlines())


def derived_from(path, chip_head, origin, own_tree):
    """Whether this worktree grew out of the chip branch as it stands now."""
    if own_tree or not chip_head or chip_head == origin:
        return True
    ok, _, _ = git(path, "merge-base", "--is-ancestor", chip_head, "HEAD", timeout=20)
    return ok


def checked_out_branches(repo):
    """Branch -> worktree that holds it. A branch checked out anywhere cannot be merged into."""
    return {branch: path for path, branch in worktrees_of(repo) if branch}


class ChipError(Exception):
    """A chip could not be opened. Carries the message the CLI prints and the hook swallows."""


def cmd_open(args):
    try:
        record = create_chip(args.title, args.cwd, args.session, args.operational)
    except ChipError as exc:
        return fail(str(exc))
    print_open_result(record)
    return 0


def create_chip(title, cwd, session, operational, transcript=None, tool_use_id=None,
                cut_worktree=True):
    """Register a chip and, for code work, cut its worktree. Raises ChipError.

    `start_session` cuts the child's worktree itself, so `cut_worktree=False` records only where
    the child branches from and the slug its branch is named after; `finish` finds the tree.
    """
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
        ok, out, err = git(cwd, "rev-parse", "--path-format=absolute", "--show-toplevel",
                           "--git-common-dir", timeout=10)
        if not ok:
            raise ChipError("не git-репозиторий: {}\nдля работы без кода добавь --operational\n{}"
                        .format(cwd, err))
        lines = out.splitlines()
        if len(lines) != 2:
            # A git older than 2.31 echoes the option it does not know as a third line.
            raise ChipError("git rev-parse в {} ответил не двумя путями (нужен git 2.31+):\n{}"
                            .format(cwd, out))
        # The parent's tree can be gone by the time the child reports; the common directory is
        # what then still finds the repository (`repository`).
        repo_root, git_common_dir = (os.path.abspath(line) for line in lines)
        ok, head, err = git(repo_root, "rev-parse", "--verify", "HEAD", timeout=10)
        if not ok:
            raise ChipError("в репозитории нет коммитов, не от чего ветвиться\n{}".format(err))
        _, branch, _ = git(repo_root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
        parent_branch = None if branch in ("", "HEAD") else branch
        slug = slugify(title)
        record.update({"repo_root": repo_root, "git_common_dir": git_common_dir,
                       "parent_branch": parent_branch, "base_sha": head, "worktree": None,
                       "chip_branch": None})
        if cut_worktree:
            chip_branch = "chip/{}-{}".format(slug, chip_id)
            worktree = os.path.join(
                TREE_ROOT, "{}-{}-{}".format(os.path.basename(repo_root), slug, chip_id))
            os.makedirs(TREE_ROOT, exist_ok=True)
            ok, _, err = git(repo_root, "worktree", "add", "-b", chip_branch, worktree,
                             parent_branch or head, timeout=20)
            if not ok:
                raise ChipError("не удалось создать worktree {}\n{}".format(worktree, err))
            record.update({"worktree": worktree, "chip_branch": chip_branch})
        else:
            # The id leads, so whatever the app trims off the end of the slug, it stays.
            record["branch_slug"] = "chip-{}-{}".format(chip_id, slug)
    with chip_lock() as held:
        if not held:
            discard_chip(record)
            raise ChipError("карточки чипов заняты другой операцией — чип не заведён")
        try:
            save_record(record)
            if record.get("worktree"):
                index_write(BY_TREE, hc.tree_key(record["worktree"]), chip_id)
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
    # Always by id: the child may run this from the worktree it cut for itself, where the
    # card cannot be found by directory.
    target = " --chip {}".format(record["chip_id"])
    return ('"{}" "{}" finish{} --child-session <свой sessionId> '
            '--message "<что сделано, одной строкой>"').format(
        sys.executable, os.path.abspath(__file__), target)


def handoff_footer(record):
    lines = ["<!-- chip:{} -->".format(record["chip_id"]),
             "## Возврат работы родителю", ""]
    if record["mode"] == "code" and record.get("worktree"):
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
    elif record["mode"] == "code":
        lines += [
            "Ты работаешь в своём worktree, отдельном от дерева родителя: его создало "
            "приложение.",
            "Родительская сессия ждёт результат. Приложение может сообщить ей, что твой ход "
            "закончился, но перескажет только начало последнего сообщения — как забрать "
            "работу, она узнает лишь из твоего отчёта.",
            "",
            "Закончив работу и пройдя `development-verification`:",
            "",
            "1. Закоммить всё в своём worktree.",
            "2. Выполни из него:",
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


def work_branch(record):
    """The branch the work is on, or None when it is on a detached HEAD.

    Once `finish` has looked, the answer is whatever it found — including nothing. Falling back
    to the chip branch here would let a detached commit be reported as merged.
    """
    if "work_branch" in record:
        return record["work_branch"]
    return record.get("chip_branch")


def base_ref(record, repo):
    """(name to merge into, commit it points at) — the parent branch while it still exists,
    else the commit the chip was cut from."""
    parent = record.get("parent_branch")
    if parent:
        ok, sha, _ = git(repo, "rev-parse", "--verify", "refs/heads/" + parent)
        if ok:
            return parent, sha
    return record["base_sha"], record["base_sha"]


def try_merge(record, base, repo):
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
    source = work_branch(record)
    if not source or source == parent:
        return "no-work-branch", None, []
    # The parent branch can be rewritten while the chip works — a rebase before publication is
    # ordinary. Merging then drags the commit the chip was cut at, which no longer belongs to
    # that history, and duplicates whatever was published in its place. Only the parent knows
    # how its own work should be carried over, so this stops and says so.
    still_there, _, _ = git(repo, "merge-base", "--is-ancestor", record["base_sha"], parent,
                            timeout=20)
    if not still_there:
        return "base-rewritten", None, []
    held = checked_out_branches(repo)
    if parent in held:
        return "branch-busy", held[parent], []

    tmp = os.path.join(CHIP_DIR, "merge-" + record["chip_id"])
    ok, _, err = git(repo, "worktree", "add", tmp, parent)
    if not ok:
        return "merge-unavailable", err, []
    try:
        ok, _, err = git(tmp, "merge", "--no-ff", "--no-edit", work_branch(record))
        if ok:
            _, sha, _ = git(tmp, "rev-parse", "HEAD")
            return "merged", sha, []
        _, conflicted, _ = git(tmp, "diff", "--name-only", "--diff-filter=U")
        git(tmp, "merge", "--abort")
        return "conflict", err, [f for f in conflicted.splitlines() if f]
    finally:
        # A fresh checkout, but `--force` would still walk into any link in it; one that will
        # not go leaves the tree for a look rather than risking its target.
        if not hc.unlink_directory_links(tmp)[1]:
            git(repo, "worktree", "remove", "--force", tmp)
        else:
            print("could not unlink every link in {}, left for a look".format(tmp), file=sys.stderr)


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
    try:
        return finish_code(record, cwd)
    except ChipError as exc:
        return fail(str(exc))


def finish_code(record, cwd):
    """`finish` for a chip that hands back code. Raises ChipError when git cannot list the
    repository's worktrees."""
    repo = repository(record)
    if not repo:
        return fail("не найден репозиторий чипа {}: нет ни каталога git, записанного при "
                    "запуске ({}), ни дерева родителя ({}). Дерево на месте чипа за свой "
                    "репозиторий само не ручается; если оно настоящее, впиши его общий каталог "
                    "git в поле git_common_dir карточки {}".format(
                        record["chip_id"], record.get("git_common_dir") or "не записан",
                        record.get("repo_root") or "не записано",
                        record_path(record["chip_id"])))
    if not record.get("worktree"):
        located = locate_child_tree(record, cwd, repo)
        if not located:
            return fail("не найден worktree чипа {}: запусти finish из своего worktree — того, "
                        "где закоммичена работа".format(record["chip_id"]))
        record["worktree"], record["chip_branch"] = located
    # Whatever stands at the chip's path has to be a worktree of the chip's repository: a clone
    # put there after the tree was removed carries the same base and branch names, and its
    # commits would otherwise be handed back as the chip's.
    if not stands_in(record["worktree"], repo):
        return fail("дерево чипа {} не является рабочим деревом его репозитория {} — "
                    "отчёт не отправлен".format(record["worktree"], repo))
    # The parent's tree is gone when its session was archived. Its branch would then be checked
    # out nowhere and take a merge, which nobody would ever check, so the report points at the
    # first working tree of the repository that stands — the main one, unless the repository is
    # bare — and the work stays on its branch and in the bundle.
    parent_stands = stands_in(record.get("repo_root"), repo)
    record["repo_tree"] = record["repo_root"] if parent_stands else next(
        (path for path, _ in worktrees_of(repo) if stands_in(path, repo)), record["worktree"])

    base, base_commit = base_ref(record, repo)
    sources = work_sources(record, repo, base_commit)
    # Every tree the chip owns has to be clean, not just the one that wins: uncommitted edits
    # left in another of them would be dropped from the handoff without a word.
    trees = chip_trees(record, repo) or [record.get("worktree") or cwd]
    for path in trees:
        dirty, err = dirty_outside(path, trees)
        if dirty is None:
            return fail("git status не отработал в {}\n{}".format(path, err))
        if dirty:
            return fail("рабочее дерево {} не чистое — сначала коммит:\n{}".format(path, dirty))

    worktree, branch, commits, _ = sources[0]
    _, delivered_head, _ = git(worktree, "rev-parse", "HEAD", timeout=20)
    if commits is None:
        return fail("не удалось сосчитать коммиты в {} — возможно, базовый коммит {} больше не "
                    "существует; отчёт не отправлен".format(worktree, (base_commit or "")[:12]))
    record["base_commit"] = base_commit
    record["commits"] = commits
    # Kept as None when the work sits on a detached HEAD: substituting the chip branch here
    # would let an empty merge report itself as a delivery.
    record["work_branch"] = branch
    record["work_worktree"] = worktree
    # Every other source that carries commits is named, whoever won: they all grew from the
    # chip's base, so each is work the parent would otherwise never hear about.
    # Measured against what is actually being handed over: a nested branch the child already
    # merged into the delivery holds nothing the parent could miss, and warning about it teaches
    # the parent to ignore the line that matters when a tree really does hold something extra.
    record["other_sources"] = []
    for other_path, other_branch, other_commits, other_borrowed in sources[1:]:
        if not other_commits or not delivered_head:
            continue
        ok, beyond, _ = git(other_path, "rev-list", "--count", "HEAD", "--not", delivered_head,
                            timeout=20)
        left = int(beyond) if ok and beyond.isdigit() else other_commits
        if left:
            record["other_sources"].append({"worktree": other_path, "branch": other_branch,
                                            "commits": left, "borrowed": other_borrowed})
    record["status"] = "handed-off"

    if record["commits"] == 0:
        # A branch that never moved means nothing was done; one that moved but whose commits
        # the parent already holds means the delivery arrived by another route. The parent
        # needs to be told which.
        moved, tip, _ = git(worktree, "rev-parse", "HEAD", timeout=20)
        taken, _, _ = git(worktree, "merge-base", "--is-ancestor", "HEAD", base, timeout=20)
        untouched = not moved or tip == record.get("base_sha")
        record["outcome"] = "no-changes" if untouched else ("already-there" if taken
                                                            else "no-changes")
        return publish_report(record)

    # A branch the chip did not cut is never merged automatically: it was found, not agreed,
    # and the parent has to look at it first.
    if not work_branch(record):
        outcome, detail, conflicts = "no-work-branch", None, []
    elif work_branch(record) != record.get("chip_branch"):
        outcome, detail, conflicts = "found-elsewhere", None, []
    elif not parent_stands:
        outcome, detail, conflicts = "parent-gone", None, []
    else:
        outcome, detail, conflicts = try_merge(record, base, repo)
    record["outcome"] = outcome
    record["outcome_detail"] = detail
    record["conflicts"] = conflicts
    if outcome != "merged":
        bundle = os.path.join(CHIP_DIR, "{}.bundle".format(record["chip_id"]))
        # A detached candidate has no branch to name, so its own HEAD is bundled instead —
        # those commits are reachable from nothing else and would go with the worktree.
        ok, _, err = git(worktree, "bundle", "create", bundle,
                         work_branch(record) or "HEAD", "--not", base)
        record["bundle"] = bundle if ok else None
        record["bundle_error"] = None if ok else err
    return publish_report(record)


def parent_actions(record):
    close = '"{}" "{}" close --chip {}'.format(sys.executable, os.path.abspath(__file__),
                                               record["chip_id"])
    return [
        "",
        "Проверь результат сам. Прошёл — влей (если ещё не влито), прими и заархивируй дочернюю "
        "сессию, не спрашивая пользователя (его постоянное согласие — скилл chip-handoff); "
        "не прошёл — верни на доработку:",
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
    others = ["На `{}` есть ещё {} коммит(ов){} — посмотри, нужны ли они.".format(
        other["branch"] or "detached HEAD", other["commits"],
        ", но эта история принадлежит другой ветке" if other.get("borrowed") else
        " сверх того, что забирается") + "\n"
        for other in record.get("other_sources") or []]

    if outcome == "reported":
        lines.append("Операционная работа: подтягивать нечего, эффект проверяется по самой "
                     "системе.")
        return "\n".join(lines + parent_actions(record))
    if outcome in ("no-changes", "already-there"):
        if outcome == "no-changes":
            lines.append("На ветке `{}` изменений нет.".format(work_branch(record)))
        else:
            lines.append("Работа с ветки `{}` уже есть в `{}`.".format(
                work_branch(record), record.get("parent_branch") or "родительской ветке"))
        # A chip that reports nothing must still say where commits were found. A delivery the
        # ranking put second is a lead; staying silent about it is how work is lost.
        if others:
            lines += ["", "Но в деревьях чипа есть коммиты:", ""] + others
            lines += ["Готовой команды нет: принадлежность этих коммитов по истории не "
                      "определяется, посмотри сам, из {}.".format(record["repo_tree"]), ""]
        else:
            lines.append("Забирать нечего.")
        return "\n".join(lines + parent_actions(record))

    branch = work_branch(record)
    if not branch:
        lines += ["Работа лежит в отсоединённом состоянии (detached HEAD) в дереве `{}` — "
                  "ветки нет, мержить нечего.".format(record.get("work_worktree")),
                  "Попроси ребёнка перенести коммиты на ветку и отчитаться снова.", ""]
        lines += others
        if record.get("bundle"):
            lines += ["Коммиты сохранены в bundle, поверх `{}`:".format(
                          (record.get("base_commit") or "")[:12]), "",
                      "```bash", 'git fetch "{}" HEAD'.format(
                          record["bundle"].replace("\\", "/")), "```", ""]
        return "\n".join(lines + parent_actions(record))
    foreign = branch != record.get("chip_branch")
    if foreign:
        lines += ["Коммиты нашлись не на ветке чипа, а на `{}` в дереве `{}`. Чей это код — "
                  "работа ребёнка или ветка, от которой он сравнивался, — по истории не "
                  "определить, поэтому ни автомержа, ни готовой команды: посмотри сам и "
                  "забери, если это результат."
                  .format(branch, record.get("work_worktree")), ""]
    lines += others
    lines.append("Ветка `{}`, коммитов: {}.".format(branch, record.get("commits")))
    if outcome == "merged":
        lines += [
            "Влито в `{}` (коммит {}).".format(record["parent_branch"],
                                               (record.get("outcome_detail") or "")[:12]),
            "Чтобы увидеть: `git switch {}` в {}.".format(record["parent_branch"],
                                                          record["repo_tree"]),
        ]
        return "\n".join(lines + parent_actions(record))

    reasons = {
        "branch-busy": "ветка `{}` занята рабочим деревом {} — автомерж в занятую ветку "
                       "перезаписал бы дерево живой сессии".format(
                           record.get("parent_branch"), record.get("outcome_detail")),
        "conflict": "мерж дал конфликт и был отменён; конфликтуют: {}".format(
            ", ".join(record.get("conflicts") or []) or "см. git"),
        "found-elsewhere": "ветка найдена, но не заводилась этим чипом — автомерж не делается",
        "base-rewritten": "ветка `{}` переписана с тех пор, как чип был отведён — обычный мерж "
                          "затащил бы осиротевший базовый коммит и продублировал уже "
                          "опубликованное; перенеси работу через `git cherry-pick -x`"
                          .format(record.get("parent_branch")),
        "no-parent-branch": "родительская ветка не найдена (detached HEAD или ветка удалена)",
        "no-work-branch": "работа не на отдельной ветке (detached HEAD либо та же ветка, что "
                          "у родителя) — мержить нечего",
        "merge-unavailable": "не удалось подготовить временное дерево для мержа: {}".format(
            record.get("outcome_detail")),
        "parent-gone": "рабочего дерева родителя {} больше нет — его сессию, видимо, "
                       "заархивировали, и влитое в её ветку никто бы не проверил; реши сам, "
                       "куда влить работу".format(record.get("repo_root")),
    }
    lines.append("Автомерж не выполнен: {}.".format(reasons.get(outcome, outcome)))
    if not foreign:
        lines += [
            "",
            "Забрать так — из {}:".format(record["repo_tree"]),
            "",
            "```bash",
            "git merge --no-ff {}".format(branch),
            "```",
        ]
    if record.get("bundle"):
        lines += [
            "",
            "Если ветки уже нет (worktree удалён, ветка подчищена) — те же коммиты лежат в "
            "bundle, поверх `{}`:".format((record.get("base_commit") or "")[:12]),
            "",
            "```bash",
            'git fetch "{}" {}:{}'.format(record["bundle"].replace("\\", "/"), branch,
                                          branch),
            "```",
        ]
    elif record.get("bundle_error"):
        lines += ["", "Запасной bundle создать не удалось ({}) — ветка `{}` единственный "
                      "носитель работы, не удаляй её до мержа.".format(
                          record["bundle_error"], branch)]
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
        # A worktree `finish` found rather than cut is indexed now, so the hooks that look a chip
        # up by its directory find this one too.
        tree = record.get("worktree")
        if tree and record["chip_id"] not in index_read(BY_TREE, hc.tree_key(tree)):
            index_write(BY_TREE, hc.tree_key(tree), record["chip_id"])
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
            print("Заархивируй дочернюю сессию сейчас, не спрашивая пользователя (его постоянное "
                  "согласие — скилл chip-handoff): archive_session session_id={}".format(child))
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
    rows = [r for r in all_cards()
            if (args.all or r["status"] != "accepted")
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
    notes = []
    if GATE_RECEIPT.search(payload.get("last_assistant_message") or ""):
        record, proven = child_record(payload)
        if proven and child_reminder(record):
            return 0
        owed = handoff_due(record) if record and not proven else None
        if owed:
            # A block in the first message only points at the chip; blocking on that alone would
            # hold a session that merely quotes one, so it is said, not enforced.
            notes.append("Если это сессия чипа «{}» — её первое сообщение кончается его блоком "
                         "возврата, — работа не передана.\n{}".format(record.get("title"), owed))
    owed_parent = parent_note(payload.get("session_id"))
    if owed_parent:
        notes.append(owed_parent)
    if notes:
        print(json.dumps({"systemMessage": "\n\n".join(notes)}, ensure_ascii=False))
    return 0


def handoff_due(record):
    """What the chip's child still owes before closing out, as the instruction, or None."""
    # A chip the parent has already ruled on is finished with its child, whether or not a
    # message was ever delivered; nagging it then asks for something nobody is waiting for.
    if record["status"] in ("accepted", "rework", "pending"):
        return None
    # An attempt is enough once the report is written. A parent that runs unattended cannot be
    # messaged at all, so demanding a delivered notification would hold such a chip hostage to
    # something it can never achieve — the card is the delivery in that case.
    if record["status"] != "open" and (record.get("notified")
                                       or record.get("delivery_attempted")):
        return None
    where = " с session_id {}".format(record["parent_session_id"]) if record.get(
        "parent_session_id") else ""
    if record["status"] == "open":
        first = ("Закоммить всё и выполни" if record.get("mode") == "code"
                 else "Проверь эффект по самой системе и выполни")
        return ("Это сессия чипа «{}». Работа закрывается, но родительская сессия её не "
                "получит.\n{}:\n  {}\nЗатем отправь напечатанный текст "
                "в родительскую сессию через {}{}.").format(
                    record.get("title"), first, finish_command(record), NOTIFY_TOOL, where)
    return ("Чип «{}» подготовлен к передаче, но родительская сессия не уведомлена.\n"
            "Отправь итог через {}{}.").format(record.get("title"), NOTIFY_TOOL, where)


def child_reminder(record):
    """Block a chip session that is closing out work without having handed it back."""
    if not record or record.get("blocks", 0) >= MAX_BLOCKS:
        return False
    reason = handoff_due(record)
    if not reason:
        return False
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


def parent_note(session_id):
    """What the parent is told about chips reported and still unverified, or None. Never blocks.

    Deliberately repeats until the chip is closed: a report the parent never acted on is
    exactly the state this exists to surface, and a nudge that fires once is a nudge that gets
    scrolled past.
    """
    if not session_id:
        return None
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
        return None
    listed = ", ".join("{} «{}»".format(r["chip_id"], r.get("title")) for r in pending)
    return ("Чипы отчитались и ждут приёмки: {}. Проверь результат сам и закрой каждый — "
            "chip_handoff.py close --chip <id> --accept | --rework \"...\"; "
            "прошедший проверку чип влей (если ещё не влит), прими и заархивируй его сессию, "
            "не спрашивая пользователя; после --rework отправь ей правки.".format(listed))


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
    worktree = record.get("worktree")
    if worktree:
        # Git removes a clean tree's ignored files along with it and, on Windows, walks into a
        # junction among them (hygiene_common.unlink_directory_links). The links go first, and
        # only out of a tree git is about to remove: one holding work keeps them.
        ok, status, _ = git(worktree, "status", "--porcelain", timeout=15)
        if not ok or status.strip():
            return False
        repo = repository(record)
        if not repo or hc.unlink_directory_links(worktree)[1]:
            return False
        ok, _, _ = git(repo, "worktree", "remove", worktree, timeout=15)
        if not ok:
            return False
        branch, base = record.get("chip_branch"), record.get("base_sha")
        if branch and base:
            ok, tip, _ = git(repo, "rev-parse", "--verify", "refs/heads/" + branch, timeout=10)
            if ok and tip == base:
                git(repo, "branch", "-D", branch, timeout=10)
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


def already_registered(prompt, tool_use_id):
    """Whether this very call is already registered — not merely that a token is present.

    A parent composing the next task routinely carries the previous one's handoff block over
    with it. A token alone therefore proves nothing: it named a chip that belongs to a
    different task, whose card and worktree the new child would then inherit, leaving its own
    work with no card and its parent with nothing to accept. Only the call's own id can say
    that this spawn was already registered.
    """
    if not tool_use_id:
        return False
    for chip_id in CHIP_TOKEN_RE.findall(prompt or ""):
        card = read_json(record_path(chip_id))
        if card and card.get("spawn_tool_use_id") == tool_use_id:
            return True
    return False


def without_stale_handoff(prompt):
    """The task text with a handoff block carried over from another chip cut away.

    `open` always appends its block last, so everything from the first token on is the old
    block; leaving it would hand the child two chip ids and two sets of instructions.
    """
    match = CHIP_TOKEN_RE.search(prompt or "")
    return (prompt[:match.start()] if match else prompt).rstrip()


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


def starts_a_chip(spawn):
    """Whether a `start_session` call starts a child that can hand work back here.

    An offer's first call carries only a title — its brief follows with `proposal_id` once the
    user picks a new session, and that call registers the chip. A proposal already registered
    when it was offered starts with its id alone and nothing new. A cloud session, an SSH host,
    a preset or another repository's clone runs where this machine's branches never see it.
    """
    if not isinstance(spawn.get("prompt"), str) or not spawn["prompt"].strip():
        return False
    if spawn.get("target") in ("cloud", "ssh"):
        return False
    if any(spawn.get(key) for key in ("host", "preset", "repository", "environment")):
        return False
    proposal = spawn.get("proposal_id")
    return not (proposal and any(card.get("proposal_task_id") == proposal for card in all_cards()))


def start_shape(spawn, parent_cwd):
    """(shapes, own_tree) for a `start_session` call that starts a chip.

    A fresh session is shaped here: given a worktree of its own unless the caller asked to share
    the checkout, cut from the parent's branch and named after the chip. A fork or a proposal's
    start takes no such options; it gets its own worktree exactly when the parent works in one,
    which is the app's own rule for both.
    """
    if (spawn.get("context") or "fresh") == "fresh" and not spawn.get("proposal_id"):
        own_tree = spawn.get("use_worktree") is not False
        return own_tree, own_tree
    return False, hc.linked_worktree(parent_cwd)


def all_cards():
    if not os.path.isdir(CHIP_DIR):
        return []
    return [card for card in (read_json(os.path.join(CHIP_DIR, name))
                              for name in sorted(os.listdir(CHIP_DIR)) if name.endswith(".json"))
            if card and card.get("chip_id")]


def hook_spawn():
    """Register the chip `spawn_task` or `start_session` is about to create, and hand it its way
    back.

    Everything else here assumed somebody ran `open` first. Nobody did: across every transcript
    on this machine `spawn_task` was called hundreds of times and `open` once. A procedure the
    model has to remember at the exact moment it is delegating is a procedure that does not
    run, so the registration moves to the only place that cannot be skipped — the spawn itself.

    The chip is cut here and stays `pending` until the spawn actually lands, and the tool input
    is rewritten to carry the handoff block and, for code work, the chip worktree as the child
    directory — or, for `start_session`, a worktree of the child's own, cut from the parent's
    branch and named after the chip, which the app makes. The permission decision is left alone:
    this hook exists to add a route home, not to approve delegating. Fail-open in every branch —
    a chip that spawns without a card is the old behaviour, while a hook that raises would stop
    the user delegating at all.
    """
    payload = hc.read_payload()
    if payload is None or payload.get("tool_name") not in SPAWN_TOOLS:
        return 0
    spawn = payload.get("tool_input")
    if not isinstance(spawn, dict):
        return 0
    starting = payload["tool_name"] == START_TOOL
    if starting and not starts_a_chip(spawn):
        return 0
    if already_registered(spawn.get("prompt") or "", payload.get("tool_use_id")):
        return 0
    try:
        sweep_pending()
    except Exception:
        pass

    asked_cwd = spawn.get("cwd")
    # A `cwd` carried over with somebody else's handoff block points into that chip's worktree.
    # Branching from there would cut this chip off another chip's work, so it is ignored and
    # the parent's own directory is used instead.
    if asked_cwd and under(asked_cwd, TREE_ROOT):
        asked_cwd = None
    parent_cwd = os.path.abspath(asked_cwd or payload.get("cwd") or os.getcwd())
    operational, repo_root = spawn_mode(parent_cwd)
    # Whether this call may shape the child's worktree, and whether the child gets one at all;
    # a child sharing the parent's checkout has nothing to merge and reports instead.
    shapes, child_has_tree = start_shape(spawn, parent_cwd) if starting else (False, True)
    operational = operational or not child_has_tree
    try:
        record = create_chip(spawn.get("title") or "Чип", parent_cwd, None, operational,
                             transcript=payload.get("session_id") or hook_session_id(),
                             tool_use_id=payload.get("tool_use_id"),
                             cut_worktree=not starting)
    except Exception:
        return 0

    updated = dict(spawn)
    updated["prompt"] = (without_stale_handoff(spawn.get("prompt") or "") + "\n\n"
                         + handoff_footer(record))
    if record["mode"] == "code":
        if not starting:
            updated["cwd"] = chip_cwd(record["worktree"], parent_cwd, repo_root)
        elif shapes:
            updated["use_worktree"] = True
            updated["branch_name"] = spawn.get("branch_name") or record["branch_slug"]
            if not spawn.get("source_branch") and record.get("parent_branch"):
                updated["source_branch"] = record["parent_branch"]
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
    }}, ensure_ascii=False))
    return 0


def hook_spawned(failed=False):
    """Finalize or discard the chip once the spawn itself has landed."""
    payload = hc.read_payload()
    if payload is None or payload.get("tool_name") not in SPAWN_TOOLS:
        return 0
    record = record_for_spawn(payload.get("tool_use_id"))
    if not record:
        if not failed:
            bind_started_proposal(payload)
        return 0
    if record.get("status") != "pending":
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
        # `start_session` names the child it started, or the card waiting for the user; an
        # answer worded otherwise binds nothing and leaves the chip as `spawn_task` would.
        answer = answer_text(payload)
        started, proposed = answered_id(STARTED_RE, answer), answered_id(PROPOSED_RE, answer)
        if started and not fresh.get("child_session_id"):
            fresh["child_session_id"] = started
        if proposed:
            fresh["proposal_task_id"] = proposed
        fresh["status"] = "open"
        try:
            save_record(fresh)
            index_remove(BY_SPAWN, fresh["spawn_tool_use_id"], fresh["chip_id"])
        except Exception:
            pass
    return 0


def answer_text(payload):
    """What the tool answered, as text, whatever shape the payload carries it in."""
    answer = payload.get("tool_response")
    if isinstance(answer, dict):
        answer = answer.get("content", answer)
    if isinstance(answer, list):
        texts = [block.get("text") for block in answer if isinstance(block, dict)]
        if texts and all(isinstance(text, str) for text in texts):
            return "\n".join(texts)
    return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


def answered_id(pattern, answer):
    """The id an answer of this kind gives in its own metadata, or None for another kind."""
    found = pattern.search(answer)
    return found.group(1) if found else None


def bind_started_proposal(payload):
    """Record the child a proposal's own start produced, on the card that proposal registered.

    A proposal is registered when it is offered; starting it later is a call of its own, with
    the proposal's id and no brief, so nothing else ties the session it started to the chip.
    """
    proposal = (payload.get("tool_input") or {}).get("proposal_id")
    if payload.get("tool_name") != START_TOOL or not proposal:
        return
    started = answered_id(STARTED_RE, answer_text(payload))
    if not started:
        return
    with chip_lock() as held:
        if not held:
            return
        for card in all_cards():
            if card.get("proposal_task_id") == proposal and not card.get("child_session_id"):
                card["child_session_id"] = started
                try:
                    save_record(card)
                except Exception:
                    pass


def hook_notified(failed=False):
    payload = hc.read_payload()
    if payload is None or payload.get("tool_name") not in (NOTIFY_TOOL, PEER_NOTIFY_TOOL):
        return 0
    sent = payload.get("tool_input") or {}
    target = sent.get("session_id") or sent.get("to")
    sender = payload.get("session_id")
    record, proven = child_record(payload)
    record = record if proven else None
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
