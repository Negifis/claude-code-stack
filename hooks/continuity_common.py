"""
Continuity — shared state for the canonical-state hooks.

One runtime file per session in the temp dir, `cont_<sid>.json`, holds everything the
hooks need to agree on:

    generation        bumped on every user turn and every file edit; resets the loop guard
    calls             per-generation call fingerprints -> repeat count (the loop guard)
    turns             user turns seen this session
    edits             file edits seen this session; reported in the checkpoint nudge
    corrections       strong user corrections seen this session
    streak            consecutive strong corrections (2 -> clean-room rebuild)
    episode           id of the current correction episode; a new correction starts a new one
    lint_until_turn   last turn the output lint stays armed for
    lint_blocks       blocks spent in this episode
    lint_blocked_gen  generation the lint already blocked on (at most one block per generation)
    history_turn      the turn on which the user asked for history, so the lint stands down

Hooks on one event run in parallel, and the loop guard writes on every tool call, so every
mutation goes through `update_state`, which holds a per-session lock across the whole
load -> mutate -> write and reports whether the result actually reached disk. Without the
lock it does not mutate at all, and a caller that blocks on the outcome must stand down when
persistence failed — an unrecorded block would repeat forever.

Deliberately self-contained — no imports from other hook families. These hooks must keep
working (and stay silent) when a neighbouring hook module is renamed or broken.

Checkpoints are durable and live outside the session: a project file when the repo owns
one, otherwise `~/.claude/state/checkpoints/<project-key>.md`.
"""
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time

try:                      # Windows
    import msvcrt
except ImportError:
    msvcrt = None
try:                      # POSIX
    import fcntl
except ImportError:
    fcntl = None

CHECKPOINT_DIR = os.path.join(os.path.expanduser("~"), ".claude", "state", "checkpoints")
PROJECT_CHECKPOINT = os.path.join(".claude", "CHECKPOINT.md")
# Older than this and the checkpoint is a lead to verify, never current state.
STALE_AFTER_DAYS = 30
# Enough for the whole template plus a working task; a runaway file is truncated and said so.
MAX_INJECT_CHARS = 8000
LOCK_ATTEMPTS = 60
LOCK_DELAY = 0.01

# Checkpoint headings, matched exactly. A line is a heading only if it is one of these, so
# body text can never be mistaken for the start of the next section.
SECTIONS = ("GOAL", "STATUS", "AUTHORITATIVE STATE", "COMPLETED", "CURRENT STATE",
            "NEXT ACTION", "ACCEPTANCE", "BLOCKERS", "STOP CONDITION")
HEADING_RE = re.compile(r"^[ \t]*([A-Z][A-Z0-9 _-]*)[ \t]*(?::[ \t]*(.*))?[ \t]*$")
DONE_RE = re.compile(r"^(done|completed|finished|closed|abandoned)\b", re.I)
# The template's own angle-bracket prompts are not content.
PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")


def configure_utf8_streams():
    """Pin the streams to UTF-8: paths, prompts and answers are arbitrary user text."""
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def emit(payload):
    """Every hook answers with one JSON object on stdout."""
    print(json.dumps(payload, ensure_ascii=False))


def quiet():
    emit({"continue": True, "suppressOutput": True})


def emit_context(event, text):
    """Inject context and pass silently — the shape four of the hooks answer with."""
    emit({
        "continue": True,
        "suppressOutput": True,
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
    })


def normalize(text):
    """Fold ё to е so one spelling of a pattern matches both."""
    return str(text or "").replace("ё", "е").replace("Ё", "Е")


def read_stdin_json():
    try:
        return json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    except Exception:
        return {}


def normalize_path(path):
    """Forward-slash, lowercased form used for every path comparison here."""
    return str(path).replace("\\", "/").lower() if path else ""


def session_key(raw):
    """Filesystem-safe session id."""
    key = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(raw or "unknown"))
    return key or "unknown"


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, dict) else default


def write_json(path, data):
    """Publish atomically: a concurrent reader never sees a truncated file."""
    tmp = "{}.{}.tmp".format(path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def state_path(key):
    return os.path.join(tempfile.gettempdir(), "cont_{}.json".format(key))


def _take(handle):
    """Try to take the OS lock on an open handle. False means someone else holds it."""
    handle.seek(0)
    try:
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _drop(handle):
    handle.seek(0)
    try:
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def session_lock(path):
    """
    Exclusive per-session lock around a read-modify-write. Yields whether it was taken; the
    caller must not mutate shared state when it was not.

    The lock is held by the operating system on an open handle, not by the existence of a
    file. That is what makes it safe here: a hook killed mid-update releases it immediately,
    with no stale-marker takeover protocol to race over. The marker file itself is allowed to
    linger — it carries no meaning.
    """
    lock = path + ".lock"
    handle = None
    held = False
    try:
        handle = open(lock, "a+b")
        if msvcrt is not None or fcntl is not None:
            for _ in range(LOCK_ATTEMPTS):
                if _take(handle):
                    held = True
                    break
                time.sleep(LOCK_DELAY)
        # No locking backend (neither msvcrt nor fcntl): `held` stays False, so nothing is
        # mutated and every decision that depends on a recorded update stands down. Reporting
        # an unsynchronized write as persisted would let the guard deny — and Stop block — on
        # state that another process may have already overwritten.
    except Exception:
        held = False
    try:
        yield held
    finally:
        if handle is not None:
            if held:
                _drop(handle)
            try:
                handle.close()
            except Exception:
                pass


def load_state(key):
    state = read_json(state_path(key)) or {}
    state.setdefault("generation", 0)
    if not isinstance(state.get("calls"), dict):
        state["calls"] = {}
    return state


def update_state(key, mutator):
    """
    Run `mutator(state)` on the session state under the lock and persist the result.

    Returns `(persisted, outcome)`. `persisted` is True only when the lock was held and the
    new state actually reached disk; a caller whose decision depends on the update having
    been recorded — anything that blocks — must stand down when it is False. Without the
    lock nothing is mutated at all: an unsynchronized write would resurrect the lost-update
    race the lock exists to close.
    """
    path = state_path(key)
    try:
        with session_lock(path) as held:
            if not held:
                return False, None
            state = load_state(key)
            outcome = mutator(state)
            return write_json(path, state), outcome
    except Exception:
        return False, None


def bump_generation(state):
    """A new user turn or a file edit means the situation changed: loop counters reset."""
    state["generation"] = int(state.get("generation") or 0) + 1
    state["calls"] = {}


def project_key(cwd):
    """Stable per-directory key: readable prefix plus a hash, so siblings never collide."""
    norm = normalize_path(cwd).rstrip("/") or "unknown"
    base = norm.rsplit("/", 1)[-1] or "root"
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in base)[:40] or "root"
    digest = hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:10]
    return "{}-{}".format(safe, digest)


def global_checkpoint_path(cwd):
    return os.path.join(CHECKPOINT_DIR, "{}.md".format(project_key(cwd)))


def _decode(raw):
    """Strict UTF-8 (BOM tolerated). A file we cannot decode is not state we can trust."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def parse_sections(text):
    """
    Split a checkpoint into `{heading: body}`. Only the known headings start a section, so a
    body line can never be read as the next section, and an empty section stays empty even
    when a populated one follows it.
    """
    found = {}
    current = None
    for line in str(text or "").splitlines():
        match = HEADING_RE.match(line)
        name = (match.group(1).strip() if match else "")
        if name in SECTIONS:
            current = name
            inline = (match.group(2) or "").strip()
            found[current] = [inline] if inline else []
        elif current is not None:
            found[current].append(line)
    return {name: "\n".join(body).strip() for name, body in found.items()}


def _has_content(body):
    """A section counts as filled only if something other than a template prompt is in it."""
    body = (body or "").strip()
    return bool(body) and not PLACEHOLDER_RE.match(body)


def _candidate(path):
    """Read one checkpoint file and describe how far it can be trusted, or None if absent."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    try:
        age = max(0.0, (time.time() - os.path.getmtime(path)) / 86400.0)
    except Exception:
        age = 0.0
    text = _decode(raw)
    if text is None:
        return {"path": path, "text": "", "age_days": age, "truncated": False,
                "readable": False, "done": False, "has_next_action": False, "usable": False}
    text = text.strip()
    if not text:
        return None
    truncated = len(text) > MAX_INJECT_CHARS
    sections = parse_sections(text)
    if truncated:
        text = text[:MAX_INJECT_CHARS] + "\n… (truncated — read the file for the rest)"
    done = bool(DONE_RE.match(sections.get("STATUS", "").strip()))
    has_next_action = _has_content(sections.get("NEXT ACTION"))
    return {
        "path": path,
        "text": text,
        "age_days": age,
        "truncated": truncated,
        "readable": True,
        "done": done,
        "has_next_action": has_next_action,
        # Truncated counts as unusable: the injected text may not even contain the action
        # that made it qualify, and a complete alternative is the better candidate.
        "usable": (not done and has_next_action and not truncated
                   and age <= STALE_AFTER_DAYS),
    }


def find_checkpoint(cwd):
    """
    The checkpoint for this directory. The project's own file is preferred, but only while it
    is usable: a finished, undecodable, stale or actionless project file must not hide a live
    global one. `alternate` names the other candidate when there is one.

    Returns the chosen candidate dict, or None when there is nothing at all.
    """
    if not cwd:
        return None
    candidates = [c for c in (_candidate(os.path.join(cwd, PROJECT_CHECKPOINT)),
                              _candidate(global_checkpoint_path(cwd))) if c]
    if not candidates:
        return None
    chosen = next((c for c in candidates if c["usable"]), candidates[0])
    chosen["alternate"] = next((c["path"] for c in candidates if c is not chosen), None)
    return chosen
