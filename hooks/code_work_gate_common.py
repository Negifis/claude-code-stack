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
