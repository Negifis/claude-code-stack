"""
Session hygiene — shared helpers for the worktree/session bookkeeping hooks.

Three hooks share this module: the WorktreeRemove snapshot, the SessionEnd index, and the
SessionStart guard. They exist because Claude Code's own periodic sweep deliberately never
touches a worktree that still holds work, so an abandoned worktree with uncommitted changes
lives forever, and because a session's metadata is not readable from disk — the audit needs
a register somebody actually writes.

Deliberately self-contained. These hooks must keep working when a neighbouring hook family
is renamed or broken, and every one of them is fail-open: a hook that cannot do its job
must never cost the user their session or their worktree.
"""
import json
import os
import subprocess
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "state")
SESSION_INDEX = os.path.join(STATE_DIR, "session-index.jsonl")
SNAPSHOT_LOG = os.path.join(STATE_DIR, "worktree-snapshots.jsonl")
TREE_LOCK_DIR = os.path.join(STATE_DIR, "tree-locks")

# A lock older than this is from a session that died without cleaning up. Claude Code's own
# stale-lock sweep uses the same reasoning for worktree locks.
LOCK_STALE_SECONDS = 12 * 3600


def configure_utf8_streams():
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_payload():
    """One hook payload. None means malformed input; an empty object is valid."""
    try:
        data = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def git(cwd, *args, timeout=8, env=None):
    """Run git in `cwd`. Returns (ok, stdout). Never raises: every caller is fail-open.

    The default budget is deliberately well under the smallest hook timeout (SessionStart's
    10s): a git call that outlives its hook takes the hook's own error handling with it, and
    a guard killed mid-run neither warns nor records anything.
    """
    try:
        proc = subprocess.run(
            ("git",) + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, **env} if env else None,
        )
    except Exception:
        return False, ""
    return proc.returncode == 0, (proc.stdout or "").strip()


def is_git_dir(path):
    ok, _ = git(path, "rev-parse", "--git-dir")
    return ok


def current_branch(path):
    ok, out = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok or not out:
        return None
    return None if out == "HEAD" else out


def git_branch_of(path):
    """(is_git, branch) in one place, because both session hooks need exactly this pair."""
    if not is_git_dir(path):
        return False, None
    return True, current_branch(path)


def append_jsonl(path, record):
    """Append one record. A hook that cannot write its log still lets the session proceed."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_jsonl(path, limit=None):
    """The last `limit` records. These registers only ever grow, so a limited read seeks to a
    bounded tail instead of pulling the whole file in to throw most of it away."""
    try:
        if limit:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                window = min(size, max(limit * 512, 65536))
                handle.seek(size - window)
                lines = handle.read().decode("utf-8", "replace").splitlines()[-limit:]
        else:
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
    except Exception:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def tree_key(path):
    """Filesystem-safe key for a working tree, so two sessions in one tree collide by name."""
    norm = os.path.normcase(os.path.abspath(str(path or "")))
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in norm)[-120:] or "unknown"


def lock_path(path):
    return os.path.join(TREE_LOCK_DIR, tree_key(path) + ".json")


def process_alive(pid):
    """Deliberately not consulted for liveness — see `_live_holders`.

    Kept because the recorded pid is still useful when a human reads the lock file, but it
    cannot decide anything: the hook's parent process is whatever spawned python, and on this
    machine that is a venv redirector that exits immediately. Every holder then reads as dead,
    the guard warns nobody, and the one failure this whole system exists to prevent — two
    sessions in one working tree — goes unannounced again.
    """
    return True


def _load_holders(path):
    try:
        with open(lock_path(path), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    holders = data.get("sessions") if isinstance(data, dict) else None
    return holders if isinstance(holders, dict) else {}


def _live_holders(holders):
    """Holders recent enough to still be in the tree.

    A tree can hold several sessions at once, so this is a set rather than one slot: with a
    single slot the third session would be told about the second and never about the first,
    which is still there.

    Age is the only test. The pid cannot serve as one: the hook records its parent process,
    which on this machine is a transient venv redirector, so a pid check declared every
    holder dead and the guard fell silent. Erring towards a stale warning is the right
    direction — a needless warning costs a line of context, a missing one costs the session
    mix-up this system exists to prevent. The SessionEnd hook clears the entry on the way out.
    """
    now = time.time()
    live = {}
    for session_id, entry in holders.items():
        if not isinstance(entry, dict):
            continue
        stamp = entry.get("ts")
        if not isinstance(stamp, (int, float)) or (now - stamp) > LOCK_STALE_SECONDS:
            continue
        live[session_id] = entry
    return live


def _store_holders(path, holders):
    try:
        os.makedirs(TREE_LOCK_DIR, exist_ok=True)
        target = lock_path(path)
        if not holders:
            if os.path.exists(target):
                os.remove(target)
            return True
        tmp = "{}.{}.tmp".format(target, os.getpid())
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"cwd": os.path.abspath(str(path)), "sessions": holders},
                      handle, ensure_ascii=False)
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def read_tree_lock(path, exclude_session=None):
    """A live session holding this tree other than `exclude_session`, or None."""
    for session_id, entry in _live_holders(_load_holders(path)).items():
        if session_id != exclude_session:
            return {"session_id": session_id, **entry}
    return None


def claim_tree(path, session_id, branch):
    """Record this session as a holder and report who else already held the tree.

    One pass on purpose: reading and then writing would filter the holder set twice, and
    filtering costs a liveness probe per holder — a `tasklist` spawn on Windows — on the
    SessionStart path, in front of the user.
    """
    holders = _live_holders(_load_holders(path))
    other = next(
        ({"session_id": sid, **entry} for sid, entry in holders.items() if sid != str(session_id)),
        None,
    )
    holders[str(session_id)] = {"branch": branch, "pid": os.getppid(), "ts": time.time()}
    _store_holders(path, holders)
    return other


def write_tree_lock(path, session_id, branch):
    claim_tree(path, session_id, branch)
    return True


def clear_tree_lock(path, session_id):
    """Release this session's hold; other live sessions keep theirs."""
    holders = _live_holders(_load_holders(path))
    holders.pop(str(session_id), None)
    return _store_holders(path, holders)
