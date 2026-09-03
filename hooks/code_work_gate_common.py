"""
Code Work Gate — shared state helpers.

Both gate hooks address the same two per-session files in the temp dir:
  cwg_<sid>.mark   — written by the PostToolUse marker, one candidate worth of edits
  cwg_state_<sid>.json — written by the Stop hook, finite enforcement state + terminal receipt
Every helper fails soft: callers treat a None/False result as "no state" and stay open.
"""
import json
import os
import re
import sys
import tempfile
import time

SHELL_MUTATION_PATH = "<shell-mutation>"
SHELL_TOOLS = ("Bash", "PowerShell")
RISK_ORDER = {"LOW": 0, "STANDARD": 1, "HIGH": 2}
# What a candidate produced, which decides which completion contract applies. PERSISTENT work
# leaves an artifact that later runs read and re-execute, so its cost of being wrong recurs and
# code-quality review pays for itself. OPERATIONAL work acts on a live system through commands
# and throwaway scripts: its cost lands once, at execution time, so the useful check happens
# before the command runs, not after the effect is already irreversible.
WORK_PERSISTENT = "PERSISTENT"
WORK_OPERATIONAL = "OPERATIONAL"
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|specs?|fixtures?)(/|$)|"
    r"(^|[._-])(test|spec)([._-]|$)",
    re.IGNORECASE,
)
SENSITIVE_PATH_RE = re.compile(
    r"(^|[/_.-])("
    r"auth|authentication|authorization|authn|authz|oauth|rbac|permissions?|"
    r"security|securitypolicy|crypto|cryptography|secrets?|credentials?|tokens?|"
    r"migrations?|schemas?|billing|payments?|paymentservice|checkouts?|deploy|"
    r"deployments?|releases?|production"
    r")([/_.-]|$)",
    re.IGNORECASE,
)
# Files whose whole purpose is to hold a secret. The word-boundary pattern above cannot carry
# them: `env` as a path word also names ordinary environment plumbing like src/env.ts.
SECRET_BASENAME_RE = re.compile(
    r"^\.env(\.|$)|^id_(rsa|ed25519|ecdsa)$|\.(pem|p12|pfx|jks|keystore)$",
    re.IGNORECASE,
)
# The subset of agent configuration that actually executes or grants authority: hooks, agent and
# command definitions, skills, settings and MCP wiring. Getting these wrong changes what the
# agent is allowed to do in every later session, which is why they stay HIGH. The rest of an
# agent-config directory — rules, decisions, runbooks, notes — is prose an operator reads; a
# wrong sentence there misleads, it does not widen authority, and grading it HIGH bought a full
# review panel for single-paragraph documentation edits.
AGENT_EXECUTABLE_PATH_RE = re.compile(
    r"(^|/)(claude|agents)\.md$|"
    r"(^|/)\.mcp\.json$|"
    r"(^|/)(\.claude|\.codex|\.agents)/"
    r"(hooks|agents|commands|skills|output-styles|plugins)(/|$)|"
    r"(^|/)(\.claude|\.codex|\.agents)/settings[^/]*\.json$",
    re.IGNORECASE,
)
# Throwaway artifacts: a session scratchpad, a loose file dropped straight into a temp root, and
# the agent's own bookkeeping. They are written to be executed once and abandoned, so no future
# run reads them and reviewing them for reuse, naming or hot-path cost polishes something nobody
# will open again. They never open a gate cycle on their own; the risk of the work that produced
# them lives in executing them, which the shell mark records as an operational candidate.
#
# A temp root alone is deliberately not enough: real working clones live in directories like
# C:/tmp/<project>, and treating those as throwaway would silently drop the gate on ordinary
# source work. Only a scratchpad segment or a file sitting directly in the temp root qualifies.
TEMP_ROOT = (
    r"(?:^(?:[a-z]:)?/(?:tmp|temp)/|^/var/tmp/|"
    r"(?:^|/)(?:users|home)/[^/]+/appdata/local/temp/)"
)
# The agent's own bookkeeping, scoped to the home configuration. A repository that commits
# .claude/plans or .claude/state is publishing files people read later, so those stay gated.
HOME_BOOKKEEPING = (
    r"(?:^|/)(?:users|home)/[^/]+/(?:\.claude|\.codex|\.agents)/"
    r"(?:state|plans)(?:/|$)"
)
EPHEMERAL_PATH_RE = re.compile(
    TEMP_ROOT + r"[^/]+$|"
    + TEMP_ROOT + r"(?:[^/]+/)*scratchpad(?:/|$)|"
    + HOME_BOOKKEEPING,
    re.IGNORECASE,
)


def read_payload():
    """Read one hook payload. None means malformed input; an empty object is valid."""
    try:
        data = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def configure_utf8_streams():
    """
    Pin the streams to UTF-8 rather than trusting the caller to export PYTHONIOENCODING.
    Both hooks need it: edited paths and waiver reasons are arbitrary user text.
    """
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def edited_path(payload):
    """The path an active edit/write/notebook tool call touches, however it names it."""
    payload = payload or {}
    return payload.get("file_path") or payload.get("notebook_path") or payload.get("path")


def valid_ts(value):
    """Whether a stored timestamp is usable. Both hooks must agree on this."""
    return isinstance(value, (int, float)) and value > 0


def normalize_path(path):
    """Forward-slash, lowercased form used for every gate path comparison."""
    return str(path).replace("\\", "/").lower() if path else ""


def basename(path):
    """Last segment of an already normalized path."""
    return path.rsplit("/", 1)[-1]


def is_ephemeral(path):
    """Whether this path is a throwaway artifact rather than something a later run reads."""
    return bool(EPHEMERAL_PATH_RE.search(normalize_path(path)))


def durable_paths(paths):
    """The recorded paths that name a lasting artifact, which is what risk is graded on."""
    return [
        normalized
        for normalized in (normalize_path(path) for path in paths if path)
        if normalized != SHELL_MUTATION_PATH and not is_ephemeral(normalized)
    ]


def work_class(paths):
    """PERSISTENT as soon as one lasting artifact changed, OPERATIONAL otherwise."""
    return WORK_PERSISTENT if durable_paths(paths) else WORK_OPERATIONAL


def minimum_risk(paths):
    """Conservative path-based lower bound shared by marker and Stop verifier.

    Only lasting artifacts are graded. An unresolvable shell mutation used to force HIGH on its
    own, which made every operational session — ssh to a device, a maintenance command on a
    server, a probe script run once — indistinguishable from a production code change and cost
    it the full review panel. Such a candidate is bounded by its own operational contract in the
    Stop hook instead, so the path grade for it is LOW rather than a fabricated HIGH.
    """
    lasting = durable_paths(paths)
    if not lasting:
        return "LOW"
    graded = [path for path in lasting if not TEST_PATH_RE.search(path)]
    if not graded:
        return "LOW"
    if any(
        (
            SENSITIVE_PATH_RE.search(path)
            or AGENT_EXECUTABLE_PATH_RE.search(path)
            or SECRET_BASENAME_RE.search(basename(path))
        )
        for path in graded
    ):
        return "HIGH"
    return "STANDARD"


def max_risk(*risks):
    valid = [risk for risk in risks if risk in RISK_ORDER]
    return max(valid or ["STANDARD"], key=RISK_ORDER.get)


def marker_paths(entry):
    """Every path a marker records, normalized, with the legacy `last_path` folded in.

    The one reading of a marker's paths: the Stop hook grades the candidate on it and the
    reminders describe the candidate from it, so a marker written before `paths` existed is
    read the same way by both.
    """
    paths = [
        normalize_path(path)
        for path in ((entry or {}).get("paths") or [])
        if path
    ]
    fallback = normalize_path((entry or {}).get("last_path"))
    if fallback and fallback not in paths:
        paths.append(fallback)
    return paths


def candidate_shape(entry):
    """What an open marker says about its candidate, for the notes the hooks inject.

    Returns None when there is no open candidate; otherwise a dict with `persistent`, the
    path-based `floor` (persistent candidates only), the number of lasting `files`, and
    `first_ts`. It reads the marker through `marker_paths`, exactly as the Stop hook's
    `candidate_class` and receipt preflight do; the equivalence is pinned by the gate suite.
    """
    if not entry or entry.get("closed") or not valid_ts(entry.get("first_ts")):
        return None
    paths = marker_paths(entry)
    lasting = durable_paths(paths)
    seen = entry.get("minimum_risk_seen")
    persistent = bool(lasting) or bool(entry.get("path_overflow")) or bool(
        entry.get("unattributed_durable")
    ) or (seen in RISK_ORDER and RISK_ORDER[seen] > RISK_ORDER["LOW"])
    return {
        "persistent": persistent,
        "floor": max_risk(minimum_risk(paths), seen) if persistent else None,
        "files": len(set(lasting)),
        "first_ts": float(entry.get("first_ts")),
    }


# One line per gate decision, so a verdict that "expired" or a block that surprised the parent
# can be read back instead of reconstructed from a transcript. Bounded: rotated once at this size.
EVENT_LOG_LIMIT = 1024 * 1024


def config_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def event_log_path():
    return os.path.join(config_home(), "state", "gate-events.jsonl")


def log_event(kind, **fields):
    """Append one gate event; never raises, never blocks a hook on its own bookkeeping."""
    try:
        path = event_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > EVENT_LOG_LIMIT:
                os.replace(path, path + ".1")
        except OSError:
            pass
        record = {"ts": time.time(), "kind": kind}
        record.update(fields)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def receipt_requirements(floor):
    """The evidence a persistent candidate at this floor must carry, in one clause."""
    return {
        "LOW": "the relevant deterministic check",
        "STANDARD": "affected checks",
        "HIGH": "affected checks, one foreground simplify-reviewer result and one adversarial "
                "APPROVED newer than the last edit to a lasting artifact",
    }.get(floor, "affected checks")


# Source code the gate cares about.
CODE_EXT = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs",
    ".java", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
    ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".ps1", ".psm1", ".sql", ".proto", ".gradle", ".m", ".mm", ".dart",
    ".lua", ".ex", ".exs", ".clj", ".vue", ".svelte",
}
# Config / infrastructure the gate cares about.
CONFIG_INFRA_EXT = {
    ".yml", ".yaml", ".toml", ".tf", ".tfvars", ".ini", ".conf", ".cfg",
    ".env", ".dockerfile", ".json",
}
SPECIAL_NAMES = {
    "dockerfile", "makefile", "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml", "vagrantfile", "procfile",
}
# Markdown is prose everywhere except here, where it is executable agent configuration:
# CLAUDE.md, AGENTS.md, skills, rules and agent definitions change how the agent behaves.
AGENT_CONFIG_SEGMENTS = {".claude", ".codex", ".agents"}
ROOT_AGENT_FILES = {"claude.md", "agents.md"}


def in_agent_config_dir(norm):
    """Whether a normalized relative or absolute path crosses an agent-config directory."""
    return any(part in AGENT_CONFIG_SEGMENTS for part in norm.split("/"))


def is_config_basename(base):
    """Config names whose meaningful suffix is not returned by splitext."""
    return (
        base == ".env"
        or base.startswith(".env.")
        or base == "dockerfile"
        or base.startswith("dockerfile.")
    )


def is_gated(path):
    """Whether editing this path opens a gate cycle. Both hooks must agree on this."""
    if not path:
        return False
    norm = normalize_path(path)
    if is_ephemeral(norm):
        return False
    base = basename(norm)
    if base in SPECIAL_NAMES or is_config_basename(base):
        return True
    _, ext = os.path.splitext(base)
    if ext in CODE_EXT or ext in CONFIG_INFRA_EXT:
        return True
    return ext == ".md" and (
        base in ROOT_AGENT_FILES or in_agent_config_dir(norm)
    )


def session_key(raw):
    """Filesystem-safe session id. Both hooks must derive the same key."""
    key = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(raw or "unknown"))
    return key or "unknown"


def marker_path(session_key_):
    return os.path.join(tempfile.gettempdir(), "cwg_{}.mark".format(session_key_))


def state_path(session_key_):
    return os.path.join(tempfile.gettempdir(), "cwg_state_{}.json".format(session_key_))


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
        remove(tmp)
        return False


def remove(path):
    """True when the path is gone afterwards — a failed delete must not read as success."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return not os.path.exists(path)


# --- Cross-session attribution -------------------------------------------------------------
# The marker file is per session, but what a shell command is judged to have written is read
# out of shared trees: the working repository and the agent-configuration homes. A before/after
# diff around one command therefore also contains whatever a second session, an editor, or a
# background job wrote in the same seconds, and that other work landed in this session's
# candidate — a session that edited nothing could end up unable to close under any receipt,
# because the paths it was holding were someone else's.
#
# So each session publishes what it is about to touch, and reads the others' announcements when
# it resolves a diff. Each file under `cwg_claims/` is written only by the session it names,
# so no lock is needed and a torn read is impossible; every reader treats an unreadable or
# stale file as silence and keeps the conservative answer.
CLAIM_HORIZON = 6 * 3600.0
# How far before a command's own window another session's announcement still explains a change
# seen inside it. A session announces an edit just before making it, so only hook latency has
# to be absorbed here; a wide slack would start crediting another session's older edits and
# silently drop paths this command really did write.
CLAIM_SLACK = 5.0
# An in-flight shell window this old is abandoned bookkeeping — a command killed before
# its PostToolUse hook ran — not a command that is still writing. It bounds windows
# and nothing else. An announcement carries no timeout at all, because every timeout short
# enough to be useful is one a permission prompt can outlast, and retiring an announcement
# while the write it describes is still ahead of it reopens exactly the race the
# announcement exists to close. What ends one is promotion, the count cap, or the session
# closing its cycle. Withdrawing one on a failed edit looked tidier and was not safe: a
# write can fail after landing partially, and two calls announcing one path share a single
# entry, so a retraction keyed by path alone can erase the evidence of a change that really
# happened. Standing longer than its edit only ever moves a reader from ownership to
# ambiguity, and ambiguity keeps the unattributed floor, so nobody is excused by a stale
# announcement — which is what makes an unbounded one affordable.
SHELL_WINDOW_LIMIT = 30 * 60.0
# An announcement is never retired by anything outside the session that made it. No elapsed
# time proves that the prompt holding an edit was answered rather than left open across a
# suspend, and no amount of other sessions' activity proves it either - so neither a clock
# nor a cap may drop one, and a reader may not rewrite a file it does not own to shrink it.
# What ends an announcement is promotion, this session's own count cap, or the session
# closing its cycle. The price is that a session which announces an edit that never lands
# and never closes its cycle leaves one small file behind; the settled half of the registry
# is still swept, so what accumulates is the rare denied-edit case, not ordinary work.
CLAIM_LIMIT = 512
# How many registry files one attribution scan will look at. The scan runs on a hook with a
# seconds-long timeout and nothing bounds how many files sessions leave behind, so the
# budget covers the enumeration too, not only the reads: it stops walking the directory one
# entry past the limit. A scan that cannot reach everything says so, and so does one that
# fails part way: what it did not read is unread, not silent, and the caller must then treat
# every path it cannot otherwise attribute as unowned rather than as its own.
SCAN_LIMIT = 256


def claims_root():
    """Own directory rather than the temp root: reading the registry means listing it, and a
    developer's temp root holds tens of thousands of files this gate has no control over."""
    return os.path.join(tempfile.gettempdir(), "cwg_claims")


def claim_path(session_key_):
    return os.path.join(claims_root(), "{}.json".format(session_key_))


def absolute_path(path, cwd=None):
    """Normalized absolute form of a path a tool reported, for comparison across sessions."""
    if not path:
        return ""
    raw = str(path)
    if raw == SHELL_MUTATION_PATH:
        return raw
    try:
        if not os.path.isabs(raw) and cwd:
            raw = os.path.join(str(cwd), raw)
        raw = os.path.abspath(raw)
    except Exception:
        pass
    return normalize_path(raw)


def publish_claims(session_key_, paths=(), pending=False, shell_start_ts=None, cwd=None,
                   now=None):
    """Announce this session's own writes, and open or close its shell window.

    A `pending` announcement is a write that has been declared and has not landed: the edit may
    still be waiting on a permission prompt, and it may never happen at all. It is deliberately
    kept apart from a settled claim, because the two answer different questions. A settled claim
    says another session wrote this and holds it in its own marker, which is why a reader owes
    nothing for it. A pending one says only that somebody was about to - so a reader that sees
    the file change must not credit it to that announcement and walk away. Publishing the same
    path as settled promotes it and clears the pending entry.

    `shell_start_ts` is a tri-state: a number opens the window, 0 closes it, and None leaves it
    as it was; opening one also records where the command runs, because a reader can only treat
    it as competing for a change if it was working in the same tree. Both maps are pruned by age
    and count so a long session cannot grow this file without bound.
    """
    now = time.time() if now is None else now
    path = claim_path(session_key_)
    existing = read_json(path) or {}
    claims = existing.get("claims")
    claims = dict(claims) if isinstance(claims, dict) else {}
    announced = existing.get("pending")
    announced = dict(announced) if isinstance(announced, dict) else {}
    target = announced if pending else claims
    for entry in paths or ():
        absolute = absolute_path(entry, cwd)
        if absolute and absolute != SHELL_MUTATION_PATH:
            target[absolute] = now
            if not pending:
                announced.pop(absolute, None)
    fresh = [
        (key, stamp)
        for key, stamp in claims.items()
        if valid_ts(stamp) and now - float(stamp) <= CLAIM_HORIZON
    ]
    fresh.sort(key=lambda item: item[1], reverse=True)
    waiting = [(key, stamp) for key, stamp in announced.items() if valid_ts(stamp)]
    waiting.sort(key=lambda item: item[1], reverse=True)
    try:
        os.makedirs(claims_root(), exist_ok=True)
    except Exception:
        return False
    window = existing.get("shell_start_ts")
    window_cwd = existing.get("shell_cwd") or ""
    if shell_start_ts is not None:
        window = shell_start_ts
        window_cwd = absolute_path(cwd) if valid_ts(shell_start_ts) and cwd else ""
    return write_json(path, {
        "session": session_key_,
        "updated_ts": now,
        "shell_start_ts": float(window) if valid_ts(window) else 0.0,
        "shell_cwd": window_cwd,
        "claims": dict(fresh[:CLAIM_LIMIT]),
        "pending": dict(waiting[:CLAIM_LIMIT]),
    })


def foreign_activity(session_key_, since, now=None):
    """What other live sessions say about the window ending now.

    Four answers, and the first three are not interchangeable. `claimed` is what another session
    settled inside the window: it wrote that file and holds it. `announced` is what another
    session said it was about to write and has not confirmed. `busy_dirs` are the working
    directories of the shell commands another session had in flight - directories, not time
    spans, which is why they are not called windows here. The middle two both mean the same thing
    to a reader - a change nobody can be shown to own - while the first means it is positively
    someone else's. `overflow` says the registry was larger than one scan may read, so the
    absence of a path from the first three proves nothing about it.
    """
    now = time.time() if now is None else now
    floor = float(since) - CLAIM_SLACK if valid_ts(since) else now - CLAIM_SLACK
    claimed = set()
    announced = set()
    busy_dirs = set()
    root = claims_root()
    own = "{}.json".format(session_key_)
    found = []
    overflow = False
    try:
        with os.scandir(root) as listing:
            visited = 0
            for entry in listing:
                visited += 1
                if visited > SCAN_LIMIT:
                    # Counted before the name is even looked at: the budget is on the walk, and
                    # an entry this scan skips still cost the step that produced it. One past
                    # the limit is all it takes to know there are too many, and walking the rest
                    # to find out how many more is the cost being avoided.
                    overflow = True
                    break
                if entry.name.endswith(".json") and entry.name != own:
                    found.append(entry.name)
    except FileNotFoundError:
        # No registry yet, which is a complete answer rather than a failed one: nobody has
        # published anything, so there is nothing this scan could have missed.
        return claimed, announced, busy_dirs, False
    except Exception:
        # It is there and could not be listed. That is a scan with a hole in it, not an empty
        # directory, and the caller must not read the difference as nobody else being active.
        return claimed, announced, busy_dirs, True
    for name in found:
        try:
            with open(os.path.join(root, name), encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            # Retired between the listing and the read: there is nothing there to have missed.
            continue
        except Exception:
            # Present and unreadable is a session whose state this scan does not have.
            overflow = True
            continue
        if not isinstance(data, dict) or not data:
            continue
        updated = data.get("updated_ts")
        if not valid_ts(updated):
            remove(os.path.join(root, name))
            continue
        waiting = data.get("pending")
        waiting = waiting if isinstance(waiting, dict) else {}
        for path, stamp in waiting.items():
            # No age test anywhere on this value: a declared write stays ahead of its
            # announcement for as long as nobody answers the prompt holding it, and this is the
            # one thing in the registry that must not be retired on a clock.
            if valid_ts(stamp):
                announced.add(normalize_path(path))
        if now - float(updated) > CLAIM_HORIZON:
            # Past the horizon the file's window and settled claims no longer describe anything
            # current, and its session may never run again. One holding no announcement is
            # dropped here. One that still announces something is kept whatever its age: see
            # the note above the limits.
            if not waiting:
                remove(os.path.join(root, name))
            continue
        window = data.get("shell_start_ts")
        if valid_ts(window) and float(window) <= now and now - float(window) <= SHELL_WINDOW_LIMIT:
            busy_dirs.add(normalize_path(data.get("shell_cwd")))
        claims = data.get("claims")
        if not isinstance(claims, dict):
            continue
        for path, stamp in claims.items():
            if valid_ts(stamp) and float(stamp) >= floor:
                claimed.add(normalize_path(path))
    return claimed, announced, busy_dirs, overflow


def retire_claims(session_key_):
    """Drop this session's registry file once its candidate is closed.

    Kept when a shell window is still open: a background command outlives the Stop that closed
    the cycle, and removing its window would let another session charge itself for what that
    command is still writing. What stays behind is pruned by horizon on the next read.
    """
    data = read_json(claim_path(session_key_)) or {}
    window = data.get("shell_start_ts")
    if valid_ts(window) and time.time() - float(window) <= SHELL_WINDOW_LIMIT:
        return False
    return remove(claim_path(session_key_))
