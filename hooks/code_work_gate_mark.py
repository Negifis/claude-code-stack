"""
Code Work Gate - PostToolUse marker.

Marks a session when code, configuration, infrastructure, or executable agent instructions are
edited. The Stop hook can then enforce a finite development-verification protocol if the final
answer has no valid terminal receipt.

The marker keeps first_ts, last_ts and last_durable_ts. Stop enforcement is keyed to last_ts and
has a hard retry cap, while a later edit becomes a new candidate. last_durable_ts records the
last change to a lasting artifact and is what review-freshness checks compare against, so
rerunning a throwaway script cannot expire a verdict. first_ts bounds every finite budget the
Stop hook counts, so it also restarts when the candidate identity changes or the marker goes
stale — a candidate abandoned without a terminal receipt must not spend the next candidate's
budgets.
Fail-open: any error returns continue=true.
"""
import json
import hashlib
import glob
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_work_gate_common as cwg  # noqa: E402

cwg.configure_utf8_streams()

SHELL_WRITE_RE = re.compile(
    r"(?im)(\bapply_patch\b|\bgit\s+(?:apply|mv)\b|"
    r"(?:^|[;&|]\s*)(?:rm|mv)\b|\bsed\b[^\r\n]*\s-i\b|"
    r"\bperl\b[^\r\n]*\s-pi\b|\b(Set-Content|Add-Content|Out-File)\b|"
    r"\b(Copy-Item|Move-Item|Remove-Item|Rename-Item|New-Item)\b|"
    r"\b(prettier|eslint|ruff)\b[^\r\n]*"
    r"(--write|--fix)\b|\b(cat|echo|printf)\b[^\r\n]*((?<![0-9])>>?(?!&)|\btee\b))"
)
READ_ONLY_SHELL_RE = re.compile(
    r"(?i)^\s*(?:"
    r"git\s+(?:status|diff|log|show|rev-parse|ls-files)\b|"
    r"rg\b|grep\b|pwd\b|ls\b|dir\b|where\b|which\b|"
    r"Get-Content\b|Select-String\b|Test-Path\b|Get-Item\b|Get-ChildItem\b|"
    r"echo\b|(?:python|python3|node|claude)\s+--version\b"
    r")[^;&|>\r\n]*$"
)
VALIDATION_SHELL_RE = re.compile(
    r"(?i)^\s*(?:"
    r"(?:npm(?:\.cmd)?|pnpm(?:\.cmd)?|bun)\s+(?:test\b|run\s+"
    r"(?:test|typecheck|lint|build|check|validate|verify|ux:check)(?::[\w.-]+)?\b)|"
    r"yarn(?:\.cmd)?\s+(?:run\s+)?"
    r"(?:test|typecheck|lint|build|check|validate|verify)(?::[\w.-]+)?\b|"
    r"(?:python|python3|py)\s+-m\s+pytest\b|pytest\b|go\s+test\b|"
    r"cargo\s+(?:test|check|clippy|build)\b|dotnet\s+(?:test|build)\b|"
    r"(?:\.?[/\\])?gradlew(?:\.bat)?\s+(?:test|check|build)\b|"
    r"gradle\s+(?:test|check|build)\b|mvn(?:\.cmd)?\s+(?:test|verify)\b|"
    r"make\s+(?:test|check|lint|build|validate|verify)\b|"
    r"(?:npx(?:\.cmd)?|pnpm(?:\.cmd)?\s+exec|yarn(?:\.cmd)?\s+exec|bunx)\s+"
    r"(?:tsc|eslint|prettier|ruff)\b|"
    r"node(?:\.exe)?\s+(?:--test\b|(?:\.?[/\\])?(?:scripts?|tools?)[/\\]"
    r"[^\s;&|><`\r\n]*(?:test|check|lint|typecheck|validate|verify|build)"
    r"[^\s;&|><`\r\n]*\.(?:[cm]?js|ts)\b)"
    r")[^;&|><`\r\n]*$"
)

# The gate grades executable agent configuration HIGH, and on this machine that configuration
# lives outside any repository — exactly what a Git snapshot cannot see. A shell command that
# rewrote a hook therefore left the candidate marked <shell-mutation>, and a session whose whole
# product was a new hook graded as operational work needing neither simplify nor review. These
# trees are small and hand-edited, which is what makes snapshotting them affordable on every
# shell call. `plugins/` is deliberately excluded: it is machine-managed, an order of magnitude
# larger, and rewritten by the autoupdate hook, so watching it would mark sessions that touched
# nothing.
# Deliberately not the same set as the directories `AGENT_EXECUTABLE_PATH_RE` grades HIGH:
# `rules` and `reference` are watched but graded as prose. A gated tree left unwatched is not
# merely unseen: `outside_snapshot` then finds a recorded path no snapshot can vouch for, which
# expires the review verdict on every later shell call and leaves the candidate unable to close.
# `plugins` stays out because it is machine-managed and an order of magnitude larger.
AGENT_CONFIG_DIRS = (
    "hooks", "agents", "commands", "skills", "output-styles", "rules", "reference",
)
AGENT_CONFIG_FILES = (
    "settings.json", "settings.local.json", ".mcp.json", "claude.md", "agents.md",
)
# These trees are shared: a second session, an editor, or a background hook writing here while
# a command runs is attributed to that command, the same way a dirty worktree is attributed to
# the command that ran over it. The cost is a review round, never a missed one.
# Bookkeeping and vendored trees a configuration directory may still contain. Dot-directories
# are skipped wholesale for the same reason `plugins/` is: every one of them here is machine
# managed — a vendor namespace the CLI re-syncs (`.codex/skills/.system`), a virtualenv, a cache,
# a repository. Their churn arrives on whatever command happens to be running, which marked
# sessions that touched nothing. An edit made through Edit/Write is still gated by path.
AGENT_CONFIG_SKIP = {"__pycache__", "node_modules", "backups", "state", "plans"}
# Across all the homes together: a configuration this large is not the hand-edited tree this
# scan assumes, and proving nothing is safer than spending a Stop budget walking it.
AGENT_CONFIG_LIMIT = 8192
# Agent tooling copies its skill trees into every worktree, so one sync rewrites them on every
# branch at once and lands on whichever command was running. Inside a repository they are that
# copy, not the session's work; the sources they are copied from are watched in the homes above.
# Attribution only, like the skip list: an edit made through Edit/Write is still gated by path.
SYNCED_AGENT_TREE_RE = re.compile(r"/\.(?:agents|codex|claude)/skills/", re.IGNORECASE)
# Whether a shell command could have rewritten a file the snapshot did not see decides whether
# an unresolved mutation expires the review verdict. The rule is an allowlist of commands proven
# read-only, judged per pipeline segment: anything unknown, any redirect other than a discarded
# or merged stderr, a command substitution, a heredoc or a script block is write-capable.
# `git status --porcelain | wc -l` seven seconds after a Codex approval expired that approval on
# 2026-09-03 and cost the session a native review round it did not need; nothing that command
# can do touches a lasting artifact, while `tee`, `truncate` or `1>` plainly can.
READ_ONLY_COMMANDS = frozenset("""
    cd ls dir cat head tail grep egrep fgrep rg find fd wc sort uniq cut tr diff comm
    stat file du df pwd echo printf date true false test [ type which where whoami
    printenv jq column nl tac basename dirname realpath readlink md5sum sha1sum sha256sum tree
    ps tasklist nproc uname sleep
    get-childitem get-content get-item get-command select-string select-object measure-object
    format-table format-list out-string write-output write-host test-path resolve-path
    get-process get-location get-date sort-object gci gc gi sls findstr
""".split())
GIT_READ_SUBCOMMANDS = frozenset("""
    status log diff show rev-parse ls-files ls-tree blame describe rev-list cat-file shortlog
    for-each-ref name-rev merge-base grep check-ignore diff-tree diff-index count-objects reflog
    var version help
""".split())
# The listing forms of git subcommands that also write: `git branch` alone lists, `git branch x`
# creates, and `git stash` alone stashes.
GIT_LISTING_RE = re.compile(
    r"^(?:(?:branch|tag|remote|worktree|config)\b(?:\s+(?:list|-l|--list|-a|-v|-vv|--all|"
    r"--show-current|--merged|--no-merged|--contains\s+\S+|--get(?:-all|-regexp)?\s+\S+))*"
    r"|stash\s+(?:list|show)\b[^\r\n]*)\s*$"
)
# Arguments that turn a listed command into a writer or a command runner. A command absent
# here can only write through a redirect, which is caught separately.
MUTATING_ARGS = {
    "find": re.compile(r"(?:^|\s)-(?:delete|exec(?:dir)?|ok(?:dir)?|fprint0?|fprintf|fls)\b"),
    "fd": re.compile(r"(?:^|\s)(?:-x|-X|--exec(?:-batch)?)\b"),
    "rg": re.compile(r"(?:^|\s)--pre\b"),
    # `-o` also inside a single-dash cluster or attached to its operand: `-uo f f`, `-ofile`.
    "sort": re.compile(r"(?:^|\s)(?:-(?!-)\w*o|--output\b)"),
    "tree": re.compile(r"(?:^|\s)-(?!-)\w*o"),
    "date": re.compile(r"(?:^|\s)(?:-s\b|--set\b)"),
    "git": re.compile(r"(?:^|\s)(?:-o\b|--output\b)"),
}
# gh talks to GitHub; only these forms are known not to touch the working tree or the local
# clone. Anything else — `pr checkout`, `repo clone`, an alias, an extension — is write-capable.
GH_READ_RE = re.compile(
    r"^(?:(?:-R|--repo)(?:=\S+|\s+\S+)\s+)*(?:"
    r"pr\s+(?:view|list|status|diff|checks|comment|edit|create|close|reopen|review|ready)|"
    r"issue\s+(?:view|list|status|create|comment|edit|close|reopen)|repo\s+(?:view|list)|"
    r"api|run\s+(?:view|list|watch)|release\s+(?:view|list)|search\s+\S+|label\s+list|"
    r"auth\s+status|browse)\b"
)
# What a Codex launch feeds on stdin, read before the command runs: the Stop hook binds the
# verdict to the session that was given exactly this text, whatever the file holds later.
STDIN_REDIRECT_RE = re.compile(r"(?<![<>])<(?!<)\s*\"?([^\s\"<>|;&]+)\"?")
PACKET_KEEP_BYTES = 256 * 1024
# A capture older than this belongs to a launch whose notification never came.
PACKET_CAPTURE_TTL = 24 * 3600.0
# `-c key=value` is not skipped: it can point a reading subcommand at an external program
# (`diff.external`, `core.fsmonitor`), and so falls through as an unknown subcommand.
GIT_GLOBAL_OPTIONS_RE = re.compile(
    r"^(?:(?:-C\s+\S+|--no-pager|--git-dir=\S+|--work-tree=\S+)\s+)+"
)
# Argument shapes that execute something whatever the command: a sub-expression or type
# accessor in PowerShell, a bracket expression or grouping in either shell.
EXECUTING_ARGUMENT_RE = re.compile(r"[(\[]|::")
# Only a discarded or merged stderr is not a file the command may have written into.
HARMLESS_REDIRECT_RE = re.compile(r"2>\s*(?:/dev/null|\$null|nul\b)|2>&1|1>&2|>&2")
SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;&\r\n]")
ENV_ASSIGNMENT_RE = re.compile(r"^\w+=(?:\"[^\"]*\"|'[^']*'|\S*)\s*")
WRAPPER_RE = re.compile(r"^(?:timeout\s+(?:-\S+\s+)*\S+|time|nohup|command|builtin)\s+")
# A verdict covers content, not edit events: the marker keeps a fingerprint of its lasting
# paths at every durable change, so an edit that was reverted leaves the approved content — and
# the approval — in place. Bounded so a wide candidate costs nothing: past these limits the
# fingerprint is unknown and freshness falls back to the timestamp rule.
FINGERPRINT_MAX_FILES = 64
FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024
CONTENT_MARKS_KEPT = 32
SHELL_READ_ONLY = "READ_ONLY"
SHELL_VALIDATION = "VALIDATION"
SHELL_UNKNOWN = "UNKNOWN_OR_MUTATING"
# Backstop for a session resumed long after its candidate was left open: the identity check
# below cannot see a candidate that was abandoned on the branch the session is still sitting on,
# and outside a repository it is the only mechanism there is.
CANDIDATE_IDLE_LIMIT = 8 * 3600


def shell_write(data):
    """Recognize common shell-based file mutation without persisting the command text."""
    if str(data.get("tool_name") or "") not in cwg.SHELL_TOOLS:
        return False
    command = str((data.get("tool_input") or {}).get("command") or "")
    return bool(SHELL_WRITE_RE.search(command))


def command_head(segment):
    """The executable a pipeline segment starts with (lower-case basename, no `.exe`), and its
    arguments. Environment assignments and timing wrappers in front of it are skipped."""
    segment = segment.strip().lstrip("({!").strip()
    while True:
        stripped = WRAPPER_RE.sub("", ENV_ASSIGNMENT_RE.sub("", segment, count=1), count=1)
        if stripped == segment:
            break
        segment = stripped
    words = segment.split(None, 1)
    if not words:
        return "", ""
    token = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if token.endswith(".exe"):
        token = token[:-4]
    return token, (words[1] if len(words) > 1 else "")


SAFE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,39}$")


def command_label(command):
    """What the ledger keeps of a command: its first executable's name, or nothing recognizable.

    A first token that is not a plain executable name — a PowerShell assignment such as
    `$token='…'`, a quoted path with spaces — is not copied at all, so no value it carries can
    reach the ledger.
    """
    if not command.strip():
        return ""
    first = SEGMENT_SPLIT_RE.split(command, maxsplit=1)[0]
    head = command_head(first)[0]
    return head if SAFE_LABEL_RE.match(head) else "(unrecognized)"


def git_read_only(arguments):
    """Whether a git invocation only reads: a reading subcommand, a listing form, no output file."""
    arguments = GIT_GLOBAL_OPTIONS_RE.sub("", arguments.strip())
    if MUTATING_ARGS["git"].search(arguments):
        return False
    words = arguments.split()
    subcommand = words[0].lower() if words else ""
    if subcommand == "reflog":
        return len(words) == 1 or words[1] == "show"
    return subcommand in GIT_READ_SUBCOMMANDS or bool(GIT_LISTING_RE.match(arguments))


def read_only_pipeline(command):
    """Whether every segment of the command is a command proven not to write, arguments included."""
    if any(token in command for token in ("$(", "<(", ">(", "`", "<<", "{")):
        return False
    # A merged stderr (`2>&1`) is dropped before splitting, or its `&` would cut the pipeline.
    cleaned = HARMLESS_REDIRECT_RE.sub(" ", command)
    if ">" in cleaned:
        return False
    for segment in SEGMENT_SPLIT_RE.split(cleaned):
        if not segment.strip():
            continue
        # An environment assignment can redirect a reading command to an external program
        # (`GIT_EXTERNAL_DIFF`, `RIPGREP_CONFIG_PATH`), so a prefixed segment is not proven.
        if ENV_ASSIGNMENT_RE.match(segment.strip().lstrip("({!").strip()):
            return False
        head, rest = command_head(segment)
        if EXECUTING_ARGUMENT_RE.search(rest):
            return False
        if head == "git":
            if git_read_only(rest):
                continue
            return False
        if head == "gh":
            if GH_READ_RE.match(rest.strip()):
                continue
            return False
        if head not in READ_ONLY_COMMANDS:
            return False
        guard = MUTATING_ARGS.get(head)
        if guard and guard.search(rest):
            return False
        # `uniq input output` writes its second operand.
        if head == "uniq" and len([w for w in rest.split() if not w.startswith("-")]) > 1:
            return False
    return True


def write_capable(data):
    """Whether this shell command could have rewritten a file the snapshot did not see.

    Broader than `shell_write`, which names the shapes that definitely write and therefore keep
    their whole delta in `own_delta`; this one only decides whether an unresolved mutation may
    expire a review verdict. Only a pipeline of commands proven read-only is outside it, plus a
    validation command: reruns of the checks the skill asks for never edit source.
    """
    if str(data.get("tool_name") or "") not in cwg.SHELL_TOOLS:
        return False
    command = str((data.get("tool_input") or {}).get("command") or "")
    if shell_write(data):
        return True
    if not command.strip():
        return False
    if VALIDATION_SHELL_RE.match(command) and "$(" not in command:
        return False
    return not read_only_pipeline(command)


def shell_policy(data):
    """Classify shell calls without treating successful validation as a write."""
    if str(data.get("tool_name") or "") not in cwg.SHELL_TOOLS:
        return SHELL_UNKNOWN
    command = str((data.get("tool_input") or {}).get("command") or "")
    if shell_write(data):
        return SHELL_UNKNOWN
    if READ_ONLY_SHELL_RE.match(command):
        return SHELL_READ_ONLY
    if VALIDATION_SHELL_RE.match(command) and "$(" not in command:
        return SHELL_VALIDATION
    return SHELL_UNKNOWN


def shell_read_only(data):
    return shell_policy(data) == SHELL_READ_ONLY


def shell_snapshot_path(data):
    identity = "{}_{}".format(
        data.get("session_id") or "unknown",
        data.get("tool_use_id") or "pending",
    )
    return os.path.join(
        tempfile.gettempdir(),
        "cwg_shell_{}.json".format(cwg.session_key(identity)),
    )


def run_git(cwd, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            check=False,
            timeout=4,
        )
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def nul_paths(raw):
    if raw is None:
        return None
    return [
        os.fsdecode(item)
        for item in raw.split(b"\0")
        if item
    ]


def file_token(path):
    try:
        stat = os.stat(path, follow_symlinks=False)
        digest = hashlib.sha256()
        if stat.st_size <= 10 * 1024 * 1024 and os.path.isfile(path):
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            content = digest.hexdigest()
        else:
            content = "metadata"
        return "{}:{}:{}".format(stat.st_size, stat.st_mtime_ns, content)
    except FileNotFoundError:
        return "missing"
    except Exception:
        return "unreadable"


def git_snapshot(cwd):
    root_raw = run_git(cwd, "rev-parse", "--show-toplevel")
    if root_raw is None:
        return None
    root = os.fsdecode(root_raw).strip()
    sources = (
        ("worktree", ("diff", "--name-only", "-z", "--")),
        ("index", ("diff", "--cached", "--name-only", "-z", "--")),
        ("untracked", ("ls-files", "--others", "--exclude-standard", "-z")),
    )
    # PERF: ignored files stay unlisted. `ls-files --ignored` measured 19s here (3.8s even with
    # --directory) against 0.37s for the untracked listing, and a timeout fails the whole
    # snapshot open. A shell write onto a gitignored .env therefore grades as operational work;
    # the same edit through Edit/Write still grades HIGH.
    flags = {}
    for label, args in sources:
        names = nul_paths(run_git(root, *args))
        if names is None:
            return None
        for name in names:
            normalized = cwg.normalize_path(name)
            flags.setdefault(normalized, []).append(label)
    if len(flags) > 4096:
        return {"root": root, "overflow": True, "files": {}}
    files = {}
    for relative, labels in flags.items():
        absolute = os.path.join(root, *relative.split("/"))
        files[relative] = {
            "flags": sorted(labels),
            "token": file_token(absolute),
        }
    return {"root": root, "overflow": False, "files": files}


def agent_config_roots():
    """The agent-configuration homes, honouring the environment overrides the tools read."""
    return (
        cwg.config_home(),
        cwg.codex_home(),
        os.path.join(os.path.expanduser("~"), ".agents"),
    )


def file_metadata(stat):
    """What the configuration snapshot stores per file: enough to see that it was written."""
    return "{}:{}".format(stat.st_size, stat.st_mtime_ns)


def scan_config_tree(directory, files):
    """Record every gated file under one configuration directory.

    False means the tree could not be read, and the snapshot must then prove nothing rather
    than report it clean: an absent directory is ordinary — most homes have only some of these
    — but one that exists and cannot be listed would otherwise read as "nothing changed here",
    which is the one answer that must never be guessed. The Git snapshot fails the same way.
    """
    stack = [directory]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except (FileNotFoundError, NotADirectoryError):
            # Absent, or a plain file standing where a tree would be: either way it holds no
            # gated file. Refusing every command until someone finds it would be worse.
            continue
        except OSError:
            return False
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    # Dot-prefixed and named-vendor trees alike: see AGENT_CONFIG_SKIP.
                    if not (entry.name.startswith(".")
                            or entry.name.lower() in AGENT_CONFIG_SKIP):
                        stack.append(entry.path)
                    continue
                normalized = cwg.normalize_path(entry.path)
                if not cwg.is_gated(normalized):
                    continue
                # On Windows the directory listing already carries size and timestamps, so this
                # costs no further syscall — which is what keeps the scan affordable per call.
                stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                # Written and removed inside the command: the other snapshot will not hold it
                # either, so the pair still agrees.
                continue
            except OSError:
                return False
            files[normalized] = file_metadata(stat)
            if len(files) > AGENT_CONFIG_LIMIT:
                return False
    return True


def config_snapshot():
    """Size and mtime of the agent-configuration files a shell command could rewrite.

    Metadata rather than content, unlike the Git snapshot: that one hashes the handful of files
    Git already reports as changed, while this one looks at every file in the trees on every
    shell call. The question here is only whether this command wrote here, which is what size
    and mtime answer.
    """
    files = {}
    roots = []
    for root in agent_config_roots():
        if not os.path.isdir(root):
            continue
        for name in AGENT_CONFIG_DIRS:
            tree = os.path.join(root, name)
            if not scan_config_tree(tree, files):
                return {"overflow": True, "roots": [], "files": {}}
            # Each watched tree vouches for itself. Claiming the home instead would vouch for
            # `plugins/` and every other pocket this scan never opens, and an empty delta would
            # then read as proof that a hook nobody looked at is unchanged.
            roots.append(cwg.normalize_path(tree))
        for name in AGENT_CONFIG_FILES:
            path = os.path.join(root, name)
            # Watched whether or not it exists right now: creating or deleting a settings file
            # is the event to catch, and letting its existence decide what the snapshot covers
            # would make exactly that write incomparable instead of naming it.
            roots.append(cwg.normalize_path(path))
            try:
                stat = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            files[cwg.normalize_path(path)] = file_metadata(stat)
    return {"overflow": False, "roots": roots, "files": files}


def shell_snapshot(cwd):
    """Everything a shell command could change that the marker is able to name."""
    return {"git": git_snapshot(cwd), "config": config_snapshot()}


def vouching_tree(path, roots):
    """The snapshotted tree a changed path belongs to, with the skip list that vouched for it.

    The skip list travels with the tree because it differs by kind: a repository passes none —
    Git reports every file beneath its root whatever the directory is called — while a
    configuration home walks around its bookkeeping and vendored pockets. Asking the same
    question of a repository with the configuration skip list would read a subdirectory named
    `state`, `plans` or `node_modules` as outside the tree it plainly sits in.
    """
    for root, skip in roots:
        if covers(root, path, skip):
            return root, skip
    return None, ()


def own_delta(session, cwd, candidates, window_start, write_shaped, roots):
    """Split a snapshot delta into what this command answers for and what nobody can name.

    A before/after diff of the working repository and the configuration homes sees every write
    those trees received while the command ran, not only this command's. Another session editing
    the same checkout therefore landed in this candidate, and a session that changed nothing of
    its own could be left holding paths it must then review, simplify and close on — which no
    receipt it can honestly write covers.

    A write-shaped command keeps its whole delta. Its own text says it wrote something, which is
    direct evidence about this session; another session announcing the same path only says that
    session wrote it too. Subtracting on the weaker evidence would let a real in-place edit of a
    sensitive file leave no candidate at all, and losing a durable write out of the gate is worse
    than one extra review round.

    For every other command, two subtractions, in order of how much they prove. A path another
    session announced inside this window is that session's, recorded in its own marker. What is
    left over is charged here unless another session had a shell command in flight *in the same
    tree*: then the change is real, nobody can attribute it, and the only thing tying it to this
    command is that this session happened to look at that tree at that moment. Shared ground is
    the whole test: a command running in an unrelated repository proves nothing about this one,
    and treating every concurrent command anywhere on the machine as a competing writer would
    drop attribution far more often than it would correct it.

    The subtractions differ in what the caller owes afterwards, which is why they are reported
    separately. A settled claim is positively someone else's, recorded in that session's marker,
    and carries no obligation here. Everything else dropped - a write another session announced
    but has not confirmed, or a command it had in flight in this tree - is owned by nobody the
    registry can name: it may well be this command's, so the caller keeps a conservative floor
    for it even though it must not record another session's file. Ambiguity narrows what a
    session is asked about; it must never reduce what a session owes.

    An unconfirmed announcement is deliberately on the ambiguous side. Treating it as ownership
    would mean an edit that was declared and then denied could silently absolve another session
    of a durable write it really made, for as long as the announcement stood.

    A registry too large to read in one scan puts everything left over on the ambiguous side for
    the same reason: what was not read cannot be evidence that nobody else owns it.
    """
    if write_shaped:
        return list(candidates), []
    claimed, announced, busy_dirs, overflow = cwg.foreign_activity(session, window_start)
    mine = []
    ambiguous = []
    for path in candidates:
        normalized = cwg.absolute_path(path, cwd)
        if normalized in claimed:
            continue
        if normalized in announced:
            ambiguous.append(normalized)
            continue
        tree, skip = vouching_tree(normalized, roots)
        if tree and any(covers(tree, busy, skip) for busy in busy_dirs):
            ambiguous.append(normalized)
            continue
        if overflow:
            # More sessions than one scan may read. A path missing from what was read is not
            # thereby this command's: unread is not silent, so it goes where everything else
            # nobody can be shown to own goes.
            ambiguous.append(normalized)
            continue
        # The normalized form, not the raw one: what this command is judged to have written and
        # what it then announces must be the same string, or the two disagree on one path.
        mine.append(normalized)
    return mine, ambiguous


def stored_snapshot(data):
    """One stored pre-command snapshot, including the Git-only shape written before the
    configuration homes were watched: a session upgraded mid-flight must not read as a command
    whose effect could not be resolved."""
    # A Git snapshot always names its root at the top level; the current shape never does.
    if isinstance(data, dict) and "root" in data:
        return {"git": data, "config": None}
    return data if isinstance(data, dict) else {}


def changed_config_paths(before, after):
    """Absolute configuration paths this command rewrote, or None when nothing is provable."""
    if (
        not before
        or not after
        or before.get("overflow")
        or after.get("overflow")
        or before.get("roots") != after.get("roots")
    ):
        return None
    return rewritten(before, after)


def rewritten(before, after):
    """Keys whose recorded state differs between two snapshots of the same tree."""
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    return [
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    ]


def changed_snapshot_paths(before, after):
    if (
        not before
        or not after
        or before.get("root") != after.get("root")
        or before.get("overflow")
        or after.get("overflow")
    ):
        return None
    changed = rewritten(before, after)
    root = after["root"]
    return [
        os.path.join(root, *path.split("/"))
        for path in changed
        if cwg.is_gated(path) and not SYNCED_AGENT_TREE_RE.search("/" + path)
    ]


def head_pointer(directory):
    """Path of the HEAD file governing `directory`, following a linked worktree pointer."""
    entry = os.path.join(directory, ".git")
    if os.path.isdir(entry):
        return os.path.join(entry, "HEAD")
    if not os.path.isfile(entry):
        return None
    try:
        with open(entry, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except Exception:
        return None
    for line in text.splitlines():
        if line.lower().startswith("gitdir:"):
            return os.path.join(directory, line.split(":", 1)[1].strip(), "HEAD")
    return None


def candidate_identity(cwd):
    """Repository root and branch of the working directory, or None when either is unknown.

    The branch is what distinguishes one candidate from the next: the marker is otherwise
    retired only by a terminal receipt, so a candidate abandoned without one keeps its window
    open and the next candidate inherits its already spent simplify, review and closure budgets.

    HEAD itself is deliberately not part of the identity, because committing is a normal step
    inside one candidate, and a detached HEAD reads as unknown so a rebase cannot flap the
    identity mid-candidate. Read from disk rather than through run_git: this runs on every
    gated edit, and an unreadable HEAD is simply an unknown identity.
    """
    try:
        current = os.path.abspath(cwd or os.getcwd())
    except Exception:
        return None
    pointer = None
    while pointer is None:
        pointer = head_pointer(current)
        if pointer is None:
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
    try:
        with open(pointer, encoding="utf-8", errors="replace") as stream:
            head = stream.read().strip()
    except Exception:
        return None
    if not head.startswith("ref:"):
        return None
    ref = head.split(":", 1)[1].strip()
    return "{}#{}".format(cwg.normalize_path(current), ref) if ref else None


def identity_mismatch(stored, identity):
    """Whether a recorded identity and the current one are both known and differ."""
    return bool(stored and identity and stored != identity)


def continues_cycle(existing, now, identity, incoming):
    """Whether an open marker still describes the candidate being edited now."""
    if not cwg.valid_ts(existing.get("first_ts")):
        return False
    named = [path for path in incoming if path != cwg.SHELL_MUTATION_PATH]
    recorded = set(existing.get("paths") or [])
    touches_recorded = any(path in recorded for path in named)
    # An opaque shell mark on the same branch resumes too: the morning's first `git push`
    # names no file, and restarting on it would discard the rounds the same way.
    resumed = identity and existing.get("identity") == identity and (touches_recorded or not named)
    last_ts = existing.get("last_ts")
    # The idle limit is a backstop for a candidate abandoned on its branch, not a clock on
    # honest work: the same branch touching a file the cycle already holds is the same
    # candidate the next morning, and restarting it would discard the review rounds it had —
    # on 2026-09-04 that turned a REVISE, REVISE, ESCALATE sequence into an illegal lone
    # ESCALATE.
    if not resumed and cwg.valid_ts(last_ts) and now - float(last_ts) > CANDIDATE_IDLE_LIMIT:
        return False
    if not identity_mismatch(existing.get("identity"), identity):
        # An unknown identity is not evidence of a new candidate. Treating it as one would hand
        # a fresh budget to work that merely ran one edit outside a repository.
        return True
    # A branch change alone is not a new candidate either: branching mid-candidate and then
    # publishing the same scope is routine, and the escalation procedure requires it. What marks
    # a different candidate is a different branch touching none of the recorded files. Past the
    # diagnostic path cap that comparison is no longer sound, so the cycle is kept.
    if existing.get("path_overflow"):
        return True
    # An opaque shell mark names no file, so it can neither confirm nor deny a different
    # candidate. Counting it as disjoint would make the branch switch itself — recorded exactly
    # this way — restart the window and drop the evidence of the work being published.
    if not named:
        return True
    return touches_recorded


def cycle_start(marker, now, identity, incoming):
    """Preserve the current cycle, open a new one for a new candidate, and tolerate the
    legacy marker format."""
    fresh = {
        "first_ts": now,
        "edits": 0,
        "paths": [],
        "minimum_risk_seen": None,
        "path_overflow": False,
        "identity": identity,
        "last_durable_ts": 0.0,
        "unattributed_durable": False,
        "content_marks": [],
        "head_at_start": None,
    }
    existing = cwg.read_json(marker)
    if existing:
        if existing.get("closed") or not continues_cycle(existing, now, identity, incoming):
            return fresh
        stored = existing.get("identity")
        # A cycle kept across an identity mismatch keeps the identity it was opened with:
        # the first mark after a branch switch is usually the switch itself, recorded as
        # <shell-mutation> and overlapping every candidate that ever ran one. Adopting the new
        # identity there would settle the comparison before the edits that actually reveal a
        # different candidate. Where there is no mismatch, the current identity is adopted, so
        # a transient unreadable HEAD cannot erase the discriminator for the rest of the cycle.
        mismatch = identity_mismatch(stored, identity)
        carried = existing.get("last_durable_ts")
        return {
            "first_ts": float(existing["first_ts"]),
            "edits": int(existing.get("edits") or 0),
            "paths": list(existing.get("paths") or []),
            "minimum_risk_seen": existing.get("minimum_risk_seen"),
            "path_overflow": bool(existing.get("path_overflow")),
            "identity": stored if mismatch else (identity or stored),
            "last_durable_ts": float(carried) if cwg.valid_ts(carried) else 0.0,
            "unattributed_durable": bool(existing.get("unattributed_durable")),
            "content_marks": list(existing.get("content_marks") or []),
            "head_at_start": existing.get("head_at_start"),
        }
    if os.path.exists(marker):
        try:
            fresh["first_ts"] = os.path.getmtime(marker)
        except Exception:
            pass
    return fresh


def covers(root, path, skipped=()):
    """Whether a snapshot of `root` looked at `path`.

    `skipped` names the subdirectories that snapshot walks around, together with the dot-prefixed
    ones it always does; anything below either was never read, so nothing about it was proved. A
    repository passes no skip list: Git reports on every tracked and untracked file beneath its
    root whatever the directory is called, and `plans` or `state` there is ordinary source, not
    the bookkeeping those names mean in a configuration home.
    """
    if path == root:
        return True
    if not root or not path.startswith(root + "/"):
        return False
    parts = path[len(root) + 1:].split("/")
    if skipped and any(part.startswith(".") for part in parts[:-1]):
        return False
    return not any(part in skipped for part in parts)


def outside_snapshot(paths, roots, watched=()):
    """Durable recorded paths no snapshot taken for this command says anything about.

    `roots` are repositories and `watched` the configuration trees — the same question, asked of
    two snapshots that see their own trees differently.
    """
    repositories = [cwg.normalize_path(root) for root in roots or () if root]
    trees = [cwg.normalize_path(root) for root in watched or () if root]
    return [
        path
        for path in cwg.durable_paths(paths)
        if not any(covers(root, path) for root in repositories)
        and not any(covers(root, path, AGENT_CONFIG_SKIP) for root in trees)
    ]


def record_paths(data, candidate_paths, unresolved=False, snapshot_roots=(),
                 watched_roots=(), unattributed_risk=None, write_capable_command=True):
    """Append diagnostic paths while preserving monotonic risk beyond the 128-path cap.

    `unattributed_risk` is the grade of a lasting change seen during this command that no
    session can be shown to own. Its path stays out of the marker - it may be another session's
    file, and recording it would put that session's work into this candidate - but the grade
    does not, because it may equally be this command's own write, and letting ambiguity close
    the cycle under the operational contract would be a way out of the gate rather than a
    narrower question. Sticky for the cycle: a later resolvable command does not make an earlier
    unattributable one go away.

    `unresolved` means a mutation was observed but the snapshot could not name what it touched,
    so it may have been a source edit made through the shell. `snapshot_roots` bounds what an
    empty delta actually proves: each snapshot vouches for its own tree and for nothing else —
    the working repository, and the agent-configuration homes this machine keeps outside any
    repository.
    """
    marker = cwg.marker_path(cwg.session_key(data.get("session_id")))
    now = time.time()
    incoming = [
        normalized
        for normalized in (cwg.normalize_path(path) for path in candidate_paths)
        if normalized
    ]
    cycle = cycle_start(marker, now, candidate_identity(data.get("cwd")), incoming)
    paths = cycle["paths"]
    for normalized in incoming:
        if normalized not in paths:
            paths.append(normalized)
    observed_risk = cwg.minimum_risk(paths)
    minimum_risk_seen = cwg.max_risk(
        cycle["minimum_risk_seen"], observed_risk, unattributed_risk
    )
    unattributed_durable = cycle["unattributed_durable"] or bool(unattributed_risk)
    overflow = cycle["path_overflow"] or len(paths) > 128
    # Freshness of a review verdict is measured against the last change to a lasting artifact,
    # not against any mark at all. Rewriting a throwaway script or re-running a maintenance
    # command after an approval does not touch what the reviewer read, and treating it as a new
    # edit forced a fresh review round for work the verdict already covered. An unresolved
    # mutation is the exception: it could have edited source through the shell, so once the
    # cycle holds durable paths it must expire the verdict the same way a named edit would.
    # Only a command that could actually write expires a verdict on the strength of what the
    # snapshot could not see; the snapshot's own evidence (a durable path in `incoming`) always
    # does. A read-only pipeline the policy regex does not recognise, or a git failure under
    # load, must not cost a review round.
    expired = write_capable_command and (
        unresolved or bool(outside_snapshot(paths, snapshot_roots, watched_roots))
    )
    # An unattributable durable change anchors freshness too, even though its path is not
    # recorded: without an anchor such a candidate keeps last_durable_ts at zero, the Stop hook
    # falls back to the whole-cycle timestamp, and every later command then expires the very
    # approval the floor made it go and get.
    last_durable_ts = (
        now
        if cwg.durable_paths(incoming)
        or unattributed_risk
        or (expired and cwg.durable_paths(paths))
        else cycle["last_durable_ts"]
    )
    content_marks = cycle.get("content_marks") or []
    if not cycle.get("edits") and cycle.get("head_at_start") is None:
        # The commit the cycle opened on: a tree that is back on it, clean, has changed nothing
        # lasting, whatever happened in between (a rebase probe that was aborted, an edit undone).
        cycle["head_at_start"] = head_commit(str(data.get("cwd") or os.getcwd()))
    if last_durable_ts == now:
        # A change the snapshot could not attribute may have touched anything, so the content
        # after it is unknown; a named edit leaves the lasting paths' bytes to be measured.
        fingerprint = (
            None if (unattributed_risk or not cwg.durable_paths(incoming))
            else content_fingerprint(paths)
        )
        content_marks = content_marks_after(content_marks, now, fingerprint)
        cwg.log_event(
            "durable", session=cwg.session_key(data.get("session_id")),
            reason=("edit" if cwg.durable_paths(incoming) else
                    "unattributed" if unattributed_risk else "unresolved-write-capable"),
            paths=[p for p in cwg.durable_paths(incoming)][:5],
            tool=str(data.get("tool_name") or ""),
            command=command_label(str((data.get("tool_input") or {}).get("command") or "")),
            fp=(fingerprint or "")[:12],
        )
    return cwg.write_json(marker, {
        "first_ts": cycle["first_ts"],
        "last_ts": now,
        "last_durable_ts": last_durable_ts,
        "last_path": str(candidate_paths[-1]),
        "edits": cycle["edits"] + 1,
        "paths": paths[-128:],
        "minimum_risk_seen": minimum_risk_seen,
        "path_overflow": overflow,
        "identity": cycle["identity"],
        "unattributed_durable": unattributed_durable,
        "content_marks": content_marks,
        "head_at_start": cycle.get("head_at_start"),
    })


def codex_launch(command):
    """How a shell command launches Codex: whether anything is fed on stdin, and the packet file.

    `fed` is any stdin redirect anywhere in the command; `path` is the file read by the one
    segment whose executable is codex — any spelling, `.exe` or not, behind `timeout` or an
    environment assignment — running `exec`. It is empty when that segment cannot be named: no
    codex segment, several, or a `||` that may skip it. The Stop hook treats a launch that fed
    something it cannot bind by as binding nothing.
    """
    command = str(command or "")
    fed = bool(STDIN_REDIRECT_RE.search(command))
    if "||" in command:
        return {"fed": fed, "path": ""}
    fed_by = []
    for segment in SEGMENT_SPLIT_RE.split(command):
        head, rest = command_head(segment)
        words = rest.split()
        if head == "codex" and words and words[0].lower() == "exec":
            matches = STDIN_REDIRECT_RE.findall(segment)
            fed_by.append(matches[-1] if matches else "")
    if len(fed_by) != 1 or not fed_by[0]:
        return {"fed": fed, "path": ""}
    import codex_lane
    # The command arrives as the agent wrote it, `${REVIEW_ID}` and all; the value comes from
    # an assignment in the same command. A variable nothing assigns leaves no file to capture.
    path = codex_lane.resolve_variables(command, fed_by[0])
    return {"fed": fed, "path": "" if "$" in path else codex_lane.windows_path(path)}


def forget_stale_captures(session, now=None):
    """Drop this session's packet captures older than a day: their launches never reported back."""
    now = time.time() if now is None else now
    for path in glob.glob(cwg.packet_capture_path(session, "*")):
        try:
            if now - os.path.getmtime(path) > PACKET_CAPTURE_TTL:
                os.remove(path)
        except OSError:
            pass


def capture_packet(data, session):
    """Keep what this launch is about to feed Codex, so a later rewrite of the file changes nothing."""
    command = str((data.get("tool_input") or {}).get("command") or "")
    path = codex_launch(command)["path"]
    if not path or not data.get("tool_use_id"):
        return
    forget_stale_captures(session)
    try:
        with open(path, "rb") as stream:
            raw = stream.read(PACKET_KEEP_BYTES + 1)
    except OSError:
        raw = b""
    text = cwg.normalized(raw[:PACKET_KEEP_BYTES].decode("utf-8", "replace"))
    # `text` and `truncated` are what the Stop hook reads; `path` and `ts` are for a person
    # opening the capture to see which launch it belonged to.
    cwg.write_json(cwg.packet_capture_path(session, str(data.get("tool_use_id"))), {
        "path": path, "ts": time.time(), "text": text,
        "truncated": len(raw) > PACKET_KEEP_BYTES,
    })


# How a command names a configuration home it might write to: the home's own path, its
# shell spellings, or the environment variable that points at it.
HOME_REFERENCE_RE = re.compile(r"(?i)(?:\.(?:claude|codex|agents)(?=$|[\s\"'/\\;&|)])|claude_config_dir|codex_home)")


def on_home_ground(path, cwd, snapshot_roots, data):
    """Whether an unattributed change at this path can be this command's own.

    It can when the path lies under the repository the command ran in, under the command's
    working directory or the configuration home holding it, or under a configuration home the
    command names — by its path in any spelling (Windows, Git Bash), by `~/.claude`-style
    shorthand, or by the environment variable that points at it. A change elsewhere was seen
    only because the homes are shared between sessions, and grading it here would hand this
    candidate another session's floor. A path a command builds at run time without spelling
    the home is out of reach by design.
    """
    path = cwg.normalize_path(path)
    cwd = cwg.normalize_path(cwd).rstrip("/")
    homes = [cwg.normalize_path(root).rstrip("/") for root in agent_config_roots()]
    grounds = [cwg.normalize_path(root).rstrip("/") for root in snapshot_roots if root]
    grounds.append(cwd)
    grounds.extend(home for home in homes if home and cwd and covers(home, cwd))
    if any(covers(ground, path) for ground in grounds if ground):
        return True
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return False
    spelled = cwg.normalize_path(command)
    for home in homes:
        if not home:
            continue
        spellings = [home]
        if re.match(r"^[a-z]:/", home):
            spellings.append("/" + home[0] + home[2:])
        if any(spelling in spelled for spelling in spellings):
            return True
    return bool(HOME_REFERENCE_RE.search(command))


def head_commit(cwd):
    """The commit HEAD points at in this working directory, or None outside a repository."""
    head = (cwg.git_text(cwd, ["rev-parse", "HEAD"], timeout=5) or "").strip()
    return head if re.fullmatch(r"[0-9a-f]{40}", head) else None


def content_fingerprint(paths):
    """A digest of the lasting paths as they are now, or None when it cannot be known cheaply.

    Every record is domain-separated and length-prefixed — kind, path, mode, bytes or link
    target — so no content can imitate another record. A path that no longer exists contributes
    nothing: the candidate is what is on disk, so an add that was deleted again is no change,
    while a file the approval covered going missing is one. Inside a repository the index entry
    (mode and blob) is part of the record, because the commit is made from the index.
    """
    durable = sorted(set(cwg.durable_paths(paths)))
    if not durable or len(durable) > FINGERPRINT_MAX_FILES:
        return None
    digest = hashlib.sha256()

    def add(kind, *fields):
        digest.update(kind)
        for field in fields:
            data = field if isinstance(field, bytes) else str(field).encode("utf-8", "replace")
            digest.update(struct.pack(">Q", len(data)))
            digest.update(data)

    by_dir = {}
    for path in durable:
        try:
            if os.path.islink(path):
                add(b"L", path, os.readlink(path))
                continue
            if not os.path.exists(path):
                continue
            if os.path.isdir(path):
                add(b"D", path)
                continue
            if os.path.getsize(path) > FINGERPRINT_MAX_BYTES:
                return None
            with open(path, "rb") as stream:
                content = stream.read()
            add(b"F", path, os.stat(path).st_mode & 0o777, content)
            by_dir.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
        except OSError:
            return None
    for directory, names in sorted(by_dir.items()):
        add(b"I", directory, index_entries(directory, names))
    return digest.hexdigest()


def index_entries(directory, names):
    """`git ls-files -s` for these files: empty outside a repository or on any failure."""
    return cwg.git_text(
        directory, ["ls-files", "-s", "--"] + [":(icase)" + name for name in sorted(names)], timeout=10
    ) or ""


def content_marks_after(marks, now, fingerprint):
    """The marks with this change appended: a new one only when the content actually differs."""
    kept = [mark for mark in (marks or []) if isinstance(mark, dict)]
    if not kept or fingerprint is None or kept[-1].get("fp") != fingerprint:
        kept.append({"ts": now, "fp": fingerprint})
    return kept[-CONTENT_MARKS_KEPT:]


def candidate_note(before, after):
    """One line for the model when the candidate it is editing opened, or its floor rose.

    The Stop hook can only teach after the model has tried to finish, at the price of one more
    full-context turn: 114 of the 391 blocks recorded in August 2026 were a missing receipt on
    a candidate the session had opened long before. This states the contract at the moment it
    starts to apply and again only when it tightens; every other edit stays silent.
    """
    old = cwg.candidate_shape(before)
    new = cwg.candidate_shape(after)
    if new is None:
        return None
    if old is not None and old["first_ts"] == new["first_ts"] and (
        old["persistent"], old["floor"]
    ) == (new["persistent"], new["floor"]):
        return None
    if not new["persistent"]:
        return (
            "[gate] Candidate opened: OPERATIONAL (a shell mutation, no lasting artifact yet). "
            "Close it with `[gate] operational: <pre-execution check>; <verified effect>` or "
            "`[gate] no-change: <reason>` as the last line of the final message."
        )
    opened = old is None or old["first_ts"] != new["first_ts"] or not old["persistent"]
    files = new["files"]
    return (
        "[gate] Candidate {}: PERSISTENT, path floor {} ({} lasting file{}). Requires {}. Close "
        "it with `[gate] verified: {}; <candidate and decisive checks>` as the last line "
        "(pr-ready/draft-blocked only after autonomous closure)."
    ).format(
        "opened" if opened else "floor raised",
        new["floor"], files, "" if files == 1 else "s",
        cwg.receipt_requirements(new["floor"]), new["floor"],
    )


def tool_output_text(data):
    """The finished tool's output as text, one stream per line, whatever shape the harness
    delivered it in — the breaker matches the CLI's error lines at line start."""
    response = data.get("tool_response")
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return "\n".join(str(value) for value in response.values() if isinstance(value, str))
    try:
        return json.dumps(response, ensure_ascii=False)
    except Exception:
        return str(response)


def main():
    data = cwg.read_payload() or {}
    output = {"continue": True}
    try:
        path = cwg.edited_path(data.get("tool_input"))
        tool = str(data.get("tool_name") or "")
        event = str(data.get("hook_event_name") or "")
        is_shell = tool in cwg.SHELL_TOOLS
        policy = shell_policy(data) if is_shell else None

        session = cwg.session_key(data.get("session_id"))
        cwd = str(data.get("cwd") or os.getcwd())
        marker_before = (
            cwg.read_json(cwg.marker_path(session)) if event == "PostToolUse" else None
        )

        if event == "PreToolUse":
            if is_shell:
                capture_packet(data, session)
                if policy != SHELL_READ_ONLY:
                    started = time.time()
                    # The window is published before the command runs, so a session resolving a
                    # diff that overlaps it can see that someone else was writing.
                    cwg.publish_claims(
                        session, shell_start_ts=started, cwd=cwd, now=started
                    )
                    cwg.write_json(
                        shell_snapshot_path(data),
                        dict(
                            shell_snapshot(cwd),
                            ts=started,
                        ),
                    )
            elif cwg.is_gated(path):
                # Announced before the write lands, not after it: a claim published only once
                # the edit is done can arrive after a concurrent command has already resolved
                # its diff, which is the race this whole registry exists to close. Pending until
                # the edit actually completes - an announcement is not yet a write, and must not
                # excuse another session from one.
                cwg.publish_claims(session, paths=[path], pending=True, cwd=cwd)
            print(json.dumps({"continue": True}))
            return

        shell_paths = None
        observed = False
        floor = None
        snapshot_roots = []
        watched_roots = []
        # Whether anything watched the directory the command ran in. A command writes there by
        # default, so an empty delta from a snapshot of some other tree says nothing about it.
        home_ground = False
        resolving = is_shell and policy != SHELL_READ_ONLY
        shell_started = None
        if resolving:
            snapshot_file = shell_snapshot_path(data)
            before = stored_snapshot(cwg.read_json(snapshot_file))
            shell_started = before.get("ts")
            cwg.remove(snapshot_file)
            after = shell_snapshot(cwd)
            repo_paths = changed_snapshot_paths(before.get("git"), after["git"])
            config_paths = changed_config_paths(before.get("config"), after["config"])
            # Each source answers for its own tree, so one of them proving nothing narrows what
            # the command is known not to have touched instead of discarding the other's answer.
            if repo_paths is not None:
                shell_paths = list(repo_paths)
                snapshot_roots.append((after["git"] or {}).get("root"))
                home_ground = True
            if config_paths is not None:
                shell_paths = (shell_paths or []) + config_paths
                config_roots = after["config"].get("roots") or []
                watched_roots.extend(config_roots)
                home_ground = home_ground or any(
                    covers(root, cwg.normalize_path(cwd), AGENT_CONFIG_SKIP)
                    for root in config_roots
                )
            if shell_paths:
                observed = True
                shell_paths, ambiguous = own_delta(
                    session,
                    cwd,
                    shell_paths,
                    before.get("ts"),
                    shell_write(data),
                    [(cwg.normalize_path(root), ()) for root in snapshot_roots if root]
                    + [(root, AGENT_CONFIG_SKIP) for root in watched_roots],
                )
                # A change under a root the command neither ran in nor names is another
                # session's work seen through a shared home, not this command's: on
                # 2026-09-04 a `glab api` loop run in a worktree inherited a HIGH floor from
                # the gate-ops session editing hooks under ~/.claude at that moment.
                unattributed = [
                    path for path in cwg.durable_paths(ambiguous)
                    if on_home_ground(path, cwd, snapshot_roots, data)
                ]
                floor = cwg.minimum_risk(unattributed) if unattributed else None

        try:
            if cwg.is_gated(path):
                cwg.publish_claims(session, paths=[path], cwd=cwd)
                record_paths(data, [path or cwg.SHELL_MUTATION_PATH])
            elif resolving:
                if shell_paths:
                    cwg.publish_claims(session, paths=shell_paths, cwd=cwd)
                    record_paths(data, shell_paths, snapshot_roots=snapshot_roots,
                                 watched_roots=watched_roots, unattributed_risk=floor,
                                 write_capable_command=write_capable(data))
                elif observed or not home_ground or policy == SHELL_UNKNOWN:
                    # An empty delta is not proof of no write: ignored files, and paths
                    # outside both the repository and the configuration homes, are invisible
                    # to either snapshot. Unknown or mutating commands therefore open a
                    # conservative operational candidate, and `None` — neither snapshot could
                    # be compared, as opposed to an empty list from one that could — proves
                    # nothing even for a validation command.
                    # `observed` carries a third case: the tree really did change and
                    # attribution gave every path away. That must still leave a candidate this
                    # session can be asked about rather than nothing at all.
                    record_paths(
                        data,
                        [cwg.SHELL_MUTATION_PATH],
                        unresolved=not home_ground,
                        snapshot_roots=snapshot_roots,
                        watched_roots=watched_roots,
                        unattributed_risk=floor,
                        write_capable_command=write_capable(data),
                    )
        finally:
            # Closed on every path out of this block, and deliberately not before it: closing
            # it ahead of the after-snapshot left an interval as wide as a full Git snapshot
            # in which this command had neither an open window nor its resolved claims, and a
            # session resolving inside that interval would take its writes for its own. An
            # exception during the resolution above this block leaves the window open instead,
            # which SHELL_WINDOW_LIMIT absorbs the same way it absorbs a killed command.
            if resolving:
                cwg.publish_claims(session, shell_start_ts=0)

        if event == "PostToolUse":
            if is_shell:
                # A Codex launch that the CLI itself refused (usage limit, model at capacity)
                # is recorded once, so the next candidate skips the lane instead of paying for
                # the same refusal again. Reading the command text here is attribution of an
                # outage, never proof that a review ran: that stays with the Stop hook.
                try:
                    # Imported here, not at the top: the breaker is optional, and a marker
                    # that cannot import it must still mark.
                    import codex_lane
                    codex_lane.record_from_command(
                        str((data.get("tool_input") or {}).get("command") or ""),
                        tool_output_text(data),
                        started=shell_started,
                    )
                except Exception:
                    pass
            # `marker_before`, not `before`: the shell branch above reuses `before` for its
            # snapshot, and comparing a snapshot to the marker announced the candidate on every
            # shell call.
            note = candidate_note(marker_before, cwg.read_json(cwg.marker_path(session)))
            if note:
                output["hookSpecificOutput"] = {
                    "hookEventName": "PostToolUse",
                    "additionalContext": note,
                }
    except Exception:
        pass
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
