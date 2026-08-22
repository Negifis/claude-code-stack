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
import os
import re
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
    r"(--write|--fix)\b|\b(cat|echo|printf)\b[^\r\n]*(>>?|\btee\b))"
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


def changed_snapshot_paths(before, after):
    if (
        not before
        or not after
        or before.get("root") != after.get("root")
        or before.get("overflow")
        or after.get("overflow")
    ):
        return None
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    changed = [
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    ]
    root = after["root"]
    return [
        os.path.join(root, *path.split("/"))
        for path in changed
        if cwg.is_gated(path)
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
    last_ts = existing.get("last_ts")
    if cwg.valid_ts(last_ts) and now - float(last_ts) > CANDIDATE_IDLE_LIMIT:
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
    named = [path for path in incoming if path != cwg.SHELL_MUTATION_PATH]
    if not named:
        return True
    recorded = set(existing.get("paths") or [])
    return any(path in recorded for path in named)


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
        }
    if os.path.exists(marker):
        try:
            fresh["first_ts"] = os.path.getmtime(marker)
        except Exception:
            pass
    return fresh


def outside_snapshot(paths, root):
    """Durable recorded paths a snapshot of `root` says nothing about."""
    covered = cwg.normalize_path(root)
    if not covered:
        return list(cwg.durable_paths(paths))
    return [
        path
        for path in cwg.durable_paths(paths)
        if path != covered and not path.startswith(covered + "/")
    ]


def record_paths(data, candidate_paths, unresolved=False, snapshot_root=None):
    """Append diagnostic paths while preserving monotonic risk beyond the 128-path cap.

    `unresolved` means a mutation was observed but the snapshot could not name what it touched,
    so it may have been a source edit made through the shell. `snapshot_root` bounds what an
    empty delta actually proves: it vouches for its own repository and for nothing else, and
    this machine keeps its highest-risk artifacts — the agent configuration itself — outside
    any repository.
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
    minimum_risk_seen = cwg.max_risk(cycle["minimum_risk_seen"], observed_risk)
    overflow = cycle["path_overflow"] or len(paths) > 128
    # Freshness of a review verdict is measured against the last change to a lasting artifact,
    # not against any mark at all. Rewriting a throwaway script or re-running a maintenance
    # command after an approval does not touch what the reviewer read, and treating it as a new
    # edit forced a fresh review round for work the verdict already covered. An unresolved
    # mutation is the exception: it could have edited source through the shell, so once the
    # cycle holds durable paths it must expire the verdict the same way a named edit would.
    expired = unresolved or outside_snapshot(paths, snapshot_root)
    last_durable_ts = (
        now
        if cwg.durable_paths(incoming) or (expired and cwg.durable_paths(paths))
        else cycle["last_durable_ts"]
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
    })


def main():
    data = cwg.read_payload() or {}
    try:
        path = cwg.edited_path(data.get("tool_input"))
        tool = str(data.get("tool_name") or "")
        event = str(data.get("hook_event_name") or "")
        is_shell = tool in cwg.SHELL_TOOLS
        policy = shell_policy(data) if is_shell else None

        if is_shell and event == "PreToolUse":
            if policy != SHELL_READ_ONLY:
                snapshot = git_snapshot(str(data.get("cwd") or os.getcwd()))
                if snapshot is not None:
                    cwg.write_json(shell_snapshot_path(data), snapshot)
            print(json.dumps({"continue": True}))
            return

        shell_paths = None
        snapshot_root = None
        if is_shell and policy != SHELL_READ_ONLY:
            snapshot_file = shell_snapshot_path(data)
            before = cwg.read_json(snapshot_file)
            cwg.remove(snapshot_file)
            after = git_snapshot(str(data.get("cwd") or os.getcwd()))
            shell_paths = changed_snapshot_paths(before, after)
            if shell_paths is not None:
                snapshot_root = (after or {}).get("root")

        if cwg.is_gated(path):
            record_paths(data, [path or cwg.SHELL_MUTATION_PATH])
        elif is_shell and policy != SHELL_READ_ONLY:
            if shell_paths:
                record_paths(data, shell_paths, snapshot_root=snapshot_root)
            elif shell_paths is None or policy == SHELL_UNKNOWN:
                # An empty Git delta is not proof of no write: ignored files and paths outside
                # the repository are invisible to the snapshot. Unknown or mutating commands
                # therefore open a conservative operational candidate, and a snapshot that could
                # not be built proves nothing even for an allowlisted validation command.
                record_paths(
                    data,
                    [cwg.SHELL_MUTATION_PATH],
                    unresolved=shell_paths is None,
                    snapshot_root=snapshot_root,
                )
    except Exception:
        pass
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
