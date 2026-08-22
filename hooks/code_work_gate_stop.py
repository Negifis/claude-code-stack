"""
Code Work Gate - strict but finite Stop hook.

The development-verification skill owns engineering judgment. This hook verifies a small set of
observable protocol facts in the Claude Code transcript, under the contract that matches what
the candidate produced — a lasting artifact, or an effect on a live system:

* development-verification was actually invoked;
* an operational candidate ends with a receipt naming its pre-execution check and its effect;
* non-trivial/high-risk simplify used its three named foreground lenses;
* HIGH completion has an adversarial APPROVED result — from either review engine — newer than
  the final edit;
* post-ESCALATE publication has a bounded closure-validation result.

It never re-runs review itself. A candidate can be blocked at most three times; after that the
cycle is retired as unverified, preventing an infinite Stop loop.
"""
import datetime
import fnmatch
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_work_gate_common as cwg  # noqa: E402

cwg.configure_utf8_streams()

MAX_BLOCKS_PER_CANDIDATE = 3
MAX_REVIEW_ROUNDS = 3
MAX_CLOSURE_PASSES = 2
MAX_SIMPLIFY_PASSES = 2
RISK_ORDER = cwg.RISK_ORDER
SIMPLIFY_REVIEWERS = {
    "simplify-reuse-reviewer",
    "simplify-quality-reviewer",
    "simplify-efficiency-reviewer",
}
TERMINAL_RE = re.compile(
    r"^\[gate\]\s*(verified|operational|no-change|pr-ready|draft-blocked)\s*:\s*(\S.*)$",
    re.IGNORECASE,
)
VERIFIED_REASON_RE = re.compile(r"^(LOW|STANDARD|HIGH)\s*;\s*\S", re.IGNORECASE)
OPERATIONAL_RECEIPTS = {"operational", "no-change"}
VERDICT_LINE_RE = re.compile(
    r"^VERDICT:\s*(APPROVED|REVISE|ESCALATE)$", re.IGNORECASE
)
CLOSURE_LINE_RE = re.compile(
    r"^CLOSURE_VALIDATION:\s*(READY|BLOCKED)$", re.IGNORECASE
)
CONTROL_PREFIX_RE = re.compile(
    r"^(VERDICT|CLOSURE_VALIDATION):", re.IGNORECASE
)
CONTROL_TOKEN_RE = re.compile(r"VERDICT:|CLOSURE_VALIDATION:", re.IGNORECASE)
# SECURITY: a shell call's command line is text the agent wrote, so no reading of it — substring
# or full shell grammar — can prove a reviewer ran; heredoc bodies, escapes and quoting defeat
# each attempt in turn. The Codex CLI writes its own rollout log for every run, so that log,
# stamped inside the call's own execution window, is what makes a shell result a review round.
CODEX_SESSION_GLOB = "rollout-*.jsonl"
CODEX_RUN_SLACK = 5.0        # the CLI keeps logging briefly after the command returns
CODEX_RUN_HORIZON = 7 * 86400.0  # older sessions reviewed some earlier candidate, not this one
# Read budget across every log this run inspects, newest first: one session store here holds
# 300 MB logs, and the hook answers inside a ten-second Stop timeout.
CODEX_SCAN_BUDGET = 128 * 1024 * 1024
CODEX_EXCERPT = 400
CODEX_MIN_BINDING = 200
_CODEX_ROLE = {}
# A session's brief sits at its start and the verdict of the call that just ended sits at its
# end, so both windows are read and the middle of a long-running session is not.
CODEX_HEAD_BYTES = 4 * 1024 * 1024
CODEX_TAIL_BYTES = 8 * 1024 * 1024
_CODEX_RUNS = {"since": None, "files": [], "budget": CODEX_SCAN_BUDGET}
_CODEX_SAID = {}
REQUIRED_EXTERNAL_TOKEN = "CODE_WORK_GATE_REQUIRED"
# Declared intent, never proof, and it governs one case only: whether a result that cannot be
# attributed is heard as failed review activity. A verdict itself is heard because a briefed
# Codex session produced it, marker or not, so this cannot buy acceptance.
REVIEW_INTENT_TOKEN = "CODE_WORK_GATE_REVIEW"


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


def allow(system_message=None):
    payload = {"continue": True}
    if system_message:
        payload["systemMessage"] = system_message
    emit(payload)


def parse_ts(raw):
    if isinstance(raw, (int, float)):
        return float(raw)
    if not raw:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return 0.0


def content_blocks(entry):
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def skill_name(block):
    payload = block.get("input") or {}
    if block.get("name") == "Skill":
        return str(payload.get("skill") or "").lower()
    if block.get("name") == "SlashCommand":
        return str(payload.get("command") or "").lstrip("/").split()[0].lower()
    return ""


def codex_sessions_root():
    home = os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex"
    )
    return os.path.join(home, "sessions")


def codex_run_files(since):
    """(path, mtime) for each Codex rollout log the CLI may have written for this candidate.

    Cached per hook run: the answer is the same for every call being judged, and the scan reads
    a dated tree the CLI owns. A Codex home the CLI does not write — a different CODEX_HOME, a
    disabled log — yields nothing, which costs the Codex lane its evidence and falls back to the
    native reviewer rather than accepting an unproven one.

    `since` is a floor on how far back to look, not the binding: which call a session vouches
    for is decided per record in `session_said`. The current caller opens the window at the
    session's own start, so the horizon is what normally governs here.
    """
    if _CODEX_RUNS.get("since") == since:
        return _CODEX_RUNS["files"]
    root = codex_sessions_root()
    now = time.time()
    # The tree is YYYY/MM/DD, so the days are addressable by name. Walking it instead would
    # have to trust the mtime of the year and month folders, which only moves when a child
    # folder is created — a stale parent hides every log written under it today.
    day = datetime.datetime.fromtimestamp(max(since, now - CODEX_RUN_HORIZON) - 86400.0)
    last = datetime.datetime.fromtimestamp(now)
    files = []
    while day.date() <= last.date():
        folder = os.path.join(root, *day.strftime("%Y %m %d").split())
        day += datetime.timedelta(days=1)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not fnmatch.fnmatch(name, CODEX_SESSION_GLOB):
                continue
            path = os.path.join(folder, name)
            try:
                files.append((path, os.path.getmtime(path)))
            except OSError:
                continue
    files.sort(key=lambda item: item[1], reverse=True)
    _CODEX_RUNS.update(since=since, files=files)
    return files


def normalized(text):
    return " ".join(str(text or "").split()).lower()


def reviewer_role():
    """The opening of the reviewer role definition this machine owns, normalized.

    SECURITY: this is what makes the Codex lane's reviewer identity as attested as the native
    lane's. There, `subagent_type` makes the harness deliver `agents/adversarial-reviewer.md` as
    the system prompt; here, the whole of that text has to appear in what the session was given,
    before it says anything the gate will hear. An excerpt would let a session quoting the
    opening lines pass as briefed. Neither lane can attest that the reviewer obeyed its role —
    only that the role was the one on disk.
    """
    # Cached for the process, which is one Stop event: an unreadable role file therefore fails
    # every session in that run rather than being retried, and the lane falls back to native.
    if "role" not in _CODEX_ROLE:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "agents", "adversarial-reviewer.md",
        )
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                body = stream.read().split("---", 2)[-1]
        except OSError:
            body = ""
        _CODEX_ROLE["role"] = normalized(body)
    return _CODEX_ROLE["role"]


def message_text(payload):
    return " ".join(
        str(part.get("text") or "")
        for part in payload.get("content") or []
        if isinstance(part, dict)
    )


def logged_input(record):
    """What the session was given in one rollout record, or empty for anything else."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") not in ("developer", "user"):
        return ""
    return message_text(payload)


def logged_output(record):
    """What the model itself said in one rollout record, or empty for anything else.

    SECURITY: only the model's own output can vouch for a verdict. The same log also holds the
    prompt that was piped in and the tool output it read, so matching the file as a whole would
    let a call bind to text it supplied itself.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") == "agent_message":
        return str(payload.get("message") or "")
    if payload.get("type") == "message" and payload.get("role") == "assistant":
        return message_text(payload)
    return ""


def session_records(path, mtime_ns, size, started, finished):
    """(when, what) for everything one session said, read once per state of the log.

    A session that was never given the reviewer role says nothing this gate will hear: it may
    have been a rescue, an errand, or a request to produce text. That check happens here so a
    verdict can only come from a session briefed as the reviewer.

    A candidate can spend several review rounds, and each asks the same logs the same question;
    a log here reaches hundreds of megabytes. The cache key carries the log's size and mtime, so
    a session that grows between rounds is re-read rather than answered from a stale parse.
    Exhausting the shared read budget truncates the list, so a session whose logs cannot be
    afforded proves nothing — the safe direction. An unreadable line only costs its own record:
    unlike the transcript, this log cannot hide a stale approval, only fail to support a claim.
    """
    key = (path, mtime_ns, size, started, finished)
    if key in _CODEX_SAID:
        return _CODEX_SAID[key]
    role = reviewer_role()
    briefed_at = None
    said = []
    try:
        with open(path, "rb") as raw:
            for line in read_span(raw, 0, min(size, CODEX_HEAD_BYTES)):
                if role and role in normalized(logged_input(json_record(line))):
                    briefed_at = record_time(line)
                    break
            # One byte back so the record starting exactly at that offset is not mistaken for
            # the partial line `read_span` discards.
            for line in read_span(raw, max(0, seek_time(raw, size, started) - 1), size):
                record = json_record(line)
                stamp = parse_ts(record.get("timestamp"))
                if stamp > finished + CODEX_RUN_SLACK:
                    break
                spoken = logged_output(record)
                if spoken:
                    said.append((stamp, spoken))
                elif role and role in normalized(logged_input(record)):
                    # A resumed round is briefed again, right before it answers.
                    briefed_at = stamp if briefed_at is None else min(briefed_at, stamp)
    except OSError:
        pass
    # Only what the session said after it was briefed counts, and normalizing is deferred until
    # then: an errand's output is discarded whole, which on these logs is the common case.
    _CODEX_SAID[key] = [] if briefed_at is None else [
        (at, normalized(text)) for at, text in said if at >= briefed_at
    ]
    return _CODEX_SAID[key]


def read_span(raw, start, end):
    """Whole lines between two byte offsets, charged against the shared read budget."""
    raw.seek(start)
    if start:
        raw.readline()  # the offset lands mid-record
    position = raw.tell()
    while position < end:
        line = raw.readline()
        if not line:
            return
        position += len(line)
        _CODEX_RUNS["budget"] -= len(line)
        if _CODEX_RUNS["budget"] < 0:
            return
        yield line.decode("utf-8", "replace")


def json_record(line):
    if '"message"' not in line and '"agent_message"' not in line:
        return {}
    try:
        return json.loads(line)
    except Exception:
        return {}


def record_time(line):
    try:
        return parse_ts(json.loads(line).get("timestamp"))
    except Exception:
        return 0.0


def seek_time(raw, size, target):
    """The offset of the first record at or after `target`, by bisection.

    A rollout log is append-only and in order, so the span a call asks about can be located
    without reading what precedes it — which is how a session that has been resumed for hours
    stays affordable while keeping every round it recorded readable.
    """
    low, high = 0, size
    while low < high:
        middle = (low + high) // 2
        raw.seek(middle)
        if middle:
            raw.readline()  # the offset lands mid-record
        start = raw.tell()
        line = raw.readline()
        if not line:
            high = middle
            continue
        stamp = record_time(line)
        if stamp and stamp >= target:
            # The answer is at or before this record, which began at or after `middle`.
            high = middle
        else:
            # Everything through this record predates the target, and `start + len(line)`
            # exceeds `middle`, so the search always advances.
            low = start + len(line)
    return low


def session_said(path, mtime_ns, size, started, finished, excerpt):
    """Whether this session's own output, written while the call was open, carried this text.

    Each record is judged by its own timestamp rather than the file's: a resumed session's log
    still holds every earlier review, and the file is touched again the moment it resumes.
    Tolerance is on the upper bound only, because the CLI finishes writing after the command
    returns while a record predating the call belongs to an earlier review, however narrowly.
    """
    return any(
        started <= stamp <= finished + CODEX_RUN_SLACK and excerpt in spoken
        for stamp, spoken in session_records(path, mtime_ns, size, started, finished)
    )


def codex_produced(text, started, finished, since):
    """Whether a Codex run overlapping this call logged the very output the call returned.

    SECURITY: a run overlapping in time says only that some Codex process was busy nearby — a
    rescue, another session, a detached run — which any command printing a verdict could borrow.
    The binding is the content: the tail of the result, ending at its verdict line, has to be
    something that session said while this call was open. Forging it means writing a rollout log,
    which is a gated edit and no longer a shortcut. Nothing here defends against an agent that
    rewrites this hook; the gate exists to keep honest work honest.
    """
    whole = normalized(text)
    # A result too short to be distinctive would bind to any session that happened to end the
    # same way — and "VERDICT: APPROVED" alone is every approval ever written. A review that
    # states nothing loses the lane its evidence, which is the safe direction.
    if len(whole) < CODEX_MIN_BINDING:
        return False
    excerpt = whole[-CODEX_EXCERPT:]
    candidates = []
    for path, _ in codex_run_files(since):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        # A log last written before the call opened cannot hold a record from inside it. The
        # stat is taken per call rather than reused from the listing: a session written to
        # while this hook runs would otherwise be judged on a snapshot taken before its last
        # record landed.
        if stat.st_mtime < started:
            continue
        candidates.append((abs(stat.st_mtime - finished), path, stat.st_mtime_ns, stat.st_size))
    # Closest to the call first, so the shared read budget is spent on the likely session
    # rather than on whatever happens to be newest in the store.
    candidates.sort()
    return any(
        session_said(path, mtime_ns, size, started, finished, excerpt)
        for _, path, mtime_ns, size in candidates
    )


def record_control(evidence, stamp, control, malformed=False):
    """File one reviewer result under the verdict it carries.

    `malformed` says whether a result that states no usable verdict is still review activity: it
    is, for the dedicated reviewer subagent that has no other purpose, and for a Codex call that
    declared itself the review lane but could not be attributed. A plain CLI errand is neither,
    and recording it would let an unrelated run reopen a closed gate.
    """
    control_kind, control_value = control
    if control_kind == "ordinary":
        evidence["ordinary_reviews"].append((stamp, control_value))
    elif control_kind == "closure":
        evidence["closure_reviews"].append((stamp, control_value))
    elif not malformed:
        return
    evidence["review_events"].append((stamp, control_kind, control_value))


def transcript_evidence(path, since, skill_since=None):
    """Collect protocol evidence for a candidate.

    Everything is bound to the candidate window, except the fact that the protocol skill was
    read: the operational track requires that judgment to happen *before* the command runs, so
    a window opening at the first mutation would reject exactly the order the skill prescribes.
    Only the skills' own timestamps reach back; review, lens and external results never do, so
    an earlier candidate's verdict cannot be inherited.
    """
    skill_since = since if skill_since is None else min(skill_since, since)
    evidence = {
        "skills": {},
        "simplify_successes": {},
        "simplify_failures": {},
        "ordinary_reviews": [],
        "closure_reviews": [],
        "review_failures": [],
        "review_events": [],
        "external_calls": [],
        "external_results": [],
        "scan_failed": False,
    }
    if not path or not os.path.isfile(path):
        return evidence

    calls = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                if (
                    '"tool_use"' not in raw
                    and '"tool_result"' not in raw
                    and '"name"' not in raw
                ):
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    # This line claimed a call or a result and could not be read, so the record
                    # is incomplete exactly where verdicts live: a truncated review result must
                    # not leave the approval that preceded it standing as the last word.
                    evidence["scan_failed"] = True
                    continue
                stamp = parse_ts(entry.get("timestamp"))
                if stamp and stamp + 1 < skill_since:
                    continue
                before_candidate = bool(stamp) and stamp + 1 < since
                # A pre-candidate entry can only contribute a skill timestamp, and a skill call
                # cannot be present without its tool name appearing verbatim in the raw line.
                if before_candidate and '"Skill"' not in raw and '"SlashCommand"' not in raw:
                    continue

                for block in content_blocks(entry):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        called_skill = skill_name(block)
                        if called_skill:
                            evidence["skills"][called_skill] = max(
                                stamp, evidence["skills"].get(called_skill, 0.0)
                            )
                        if before_candidate:
                            continue

                        tool_name = block.get("name")
                        payload = block.get("input")
                        if not isinstance(payload, dict):
                            continue
                        call_id = block.get("id")
                        if tool_name in cwg.SHELL_TOOLS and call_id:
                            command = str(payload.get("command") or "")
                            required = REQUIRED_EXTERNAL_TOKEN in command
                            # COMPAT: a shell call is foreground unless it is explicitly
                            # detached — the harness omits the field entirely for the ordinary
                            # case, so demanding an explicit false here made every real Codex
                            # result invisible and left the native lane as the only one that
                            # could satisfy a HIGH candidate.
                            foreground = payload.get("run_in_background") is not True
                            calls[call_id] = {
                                "kind": "external",
                                "required": required,
                                "foreground": foreground,
                                "started": stamp,
                                "marked": required or REVIEW_INTENT_TOKEN in command,
                            }
                            evidence["external_calls"].append(
                                (stamp, call_id, required, foreground)
                            )
                            continue

                        if tool_name not in ("Agent", "Task"):
                            continue
                        subtype = str(payload.get("subagent_type") or "").lower()
                        if not call_id:
                            continue
                        # Claude Code 2.1.198+ defaults subagents to background. Require an
                        # explicit false so an upgrade cannot turn a verdict-bearing call into
                        # an async launch that looks complete in the transcript. The opposite
                        # polarity of the shell check above is deliberate: the two tools carry
                        # opposite defaults, and unifying them would blind one lane.
                        foreground = payload.get("run_in_background") is False
                        if subtype in SIMPLIFY_REVIEWERS:
                            calls[call_id] = {
                                "kind": "simplify",
                                "subtype": subtype,
                                "foreground": foreground,
                            }
                        elif "adversarial-reviewer" in subtype:
                            calls[call_id] = {
                                "kind": "review",
                                "subtype": subtype,
                                "foreground": foreground,
                            }

                    if before_candidate or block.get("type") != "tool_result":
                        continue
                    call = calls.get(block.get("tool_use_id"))
                    if not call or not call["foreground"]:
                        continue
                    failed = bool(block.get("is_error"))
                    text = result_text(block)
                    if call["kind"] == "external":
                        # A review round is a terminal control line the session log shows a Codex
                        # run producing while the call was open — text alone can be printed by
                        # anything, and `codex` also runs errands. The exit status decides only
                        # whether the verdict can be trusted, not whether it is read: a review
                        # that printed its verdict and then tripped over a pipeline still stated
                        # an opinion. `--required` is held to the same bar, so a status dump is
                        # an unavailable reviewer rather than a satisfied requirement.
                        control = reviewer_control(text)
                        stated = control[0] in ("ordinary", "closure")
                        bound = stated and codex_produced(
                            text, call["started"], stamp, skill_since
                        )
                        judged = bound and not failed
                        evidence["external_results"].append(
                            (
                                stamp,
                                block.get("tool_use_id"),
                                call["required"],
                                "success" if judged else "failure",
                            )
                        )
                        if judged:
                            record_control(evidence, stamp, control)
                        elif bound or (stated and call["marked"]):
                            # An unattributable verdict is never filed as one, but dropping it
                            # would leave an earlier approval as the last word — so a call that
                            # declared itself the review lane is heard as failed activity, which
                            # reopens that approval. The declaration is needed only here: a
                            # result the session log vouches for has proved what it is.
                            evidence["review_failures"].append(stamp)
                            record_control(evidence, stamp, ("unbound", None), malformed=True)
                        continue
                    if call["kind"] == "simplify":
                        subtype = call["subtype"]
                        if failed:
                            evidence["simplify_failures"].setdefault(
                                subtype, []
                            ).append(stamp)
                        elif text.strip():
                            evidence["simplify_successes"].setdefault(
                                subtype, []
                            ).append(stamp)
                        continue

                    if failed:
                        evidence["review_failures"].append(stamp)
                        evidence["review_events"].append(
                            (stamp, "failure", None)
                        )
                        continue
                    record_control(evidence, stamp, reviewer_control(text), malformed=True)
    except Exception:
        # Everything after the failure is unread, so what was collected is a prefix, not the
        # record: an approval early in the cycle would otherwise outlive the REVISE that
        # followed it. A lasting artifact cannot be signed off on a partial scan.
        evidence["scan_failed"] = True
    return evidence


def marker_paths(entry):
    paths = [
        cwg.normalize_path(path)
        for path in (entry.get("paths") or [])
        if path
    ]
    fallback = cwg.normalize_path(entry.get("last_path"))
    if fallback and fallback not in paths:
        paths.append(fallback)
    return paths


minimum_risk = cwg.minimum_risk


def unfenced_nonempty_lines(message):
    result = []
    fence = None
    backticks = chr(96) * 3
    tildes = "~" * 3
    for line in str(message or "").splitlines():
        stripped = line.lstrip()
        token = backticks if stripped.startswith(backticks) else (
            tildes if stripped.startswith(tildes) else None
        )
        if token is not None:
            fence = None if fence == token else token
            continue
        if fence is None and line.strip():
            result.append(line.strip())
    return result, fence is None


HARNESS_TRAILERS = (
    # Tempered: an untempered block would start at the leftmost quoted `<usage>` and swallow
    # the terminal control line of any result that quotes a trailer before its own.
    re.compile(r"(?:^|\n)<usage>(?:(?!</?usage>).)*</usage>[ \t]*$", re.DOTALL),
    re.compile(
        r"(?:^|\n)agentId: \S+ \(use SendMessage[^\n]*\)[ \t]*$"
    ),
)


def strip_harness_trailer(message):
    """Drop the `agentId: ...` line and the `<usage>...</usage>` block that Claude Code
    appends to every Agent tool result — a shell result from the Codex lane carries neither and
    passes through untouched. They are harness decoration, not reviewer output,
    and would otherwise always displace the control line that has to terminate the
    result — making a foreground verdict unparseable no matter what the reviewer wrote."""
    text = str(message or "").rstrip()
    changed = True
    while changed:
        changed = False
        for pattern in HARNESS_TRAILERS:
            stripped = pattern.sub("", text).rstrip()
            if stripped != text:
                text = stripped
                changed = True
    return text


def final_line_outside_fence(message):
    lines, closed = unfenced_nonempty_lines(message)
    return lines[-1] if lines and closed else ""


def reviewer_control(message):
    """Exactly one unfenced control line, and it must terminate the reviewer result."""
    # No control line can exist without the word appearing somewhere, and the Codex lane routes
    # CLI output of any size through here on every Stop invocation — so rule that out in one
    # scan before the trailer loop and the line split copy a multi-megabyte payload.
    if not CONTROL_TOKEN_RE.search(str(message or "")):
        return "malformed", None
    lines, closed = unfenced_nonempty_lines(strip_harness_trailer(message))
    if not closed or not lines:
        return "malformed", None
    controls = [line for line in lines if CONTROL_PREFIX_RE.match(line)]
    if len(controls) != 1 or controls[0] != lines[-1]:
        return "malformed", None
    verdict = VERDICT_LINE_RE.match(controls[0])
    if verdict:
        return "ordinary", verdict.group(1).upper()
    closure = CLOSURE_LINE_RE.match(controls[0])
    if closure:
        return "closure", closure.group(1).upper()
    return "malformed", None


def receipt_of(message):
    match = TERMINAL_RE.match(final_line_outside_fence(message))
    if not match:
        return None
    kind = match.group(1).lower()
    reason = match.group(2).strip()
    if "<" in reason or ">" in reason:
        return None
    risk = None
    if kind == "verified":
        risk_match = VERIFIED_REASON_RE.match(reason)
        if not risk_match:
            return None
        risk = risk_match.group(1).upper()
    elif kind == "operational":
        # Both halves are required: what was established before the command ran, and what the
        # system looked like afterwards.
        precheck, separator, effect = reason.partition(";")
        if not (separator and precheck.strip() and effect.strip()):
            return None
    return kind, reason, risk


def candidate_class(entry):
    """Which completion contract this candidate falls under.

    Overflow and the monotonic risk high-water mark both mean a lasting artifact was seen
    earlier in the cycle even if the current path list no longer shows it, so either one keeps
    the candidate persistent. Downgrading on a truncated path list would let a long cycle end
    under the operational contract it never qualified for.
    """
    if entry.get("path_overflow"):
        return cwg.WORK_PERSISTENT
    seen = entry.get("minimum_risk_seen")
    if seen in RISK_ORDER and RISK_ORDER[seen] > RISK_ORDER["LOW"]:
        return cwg.WORK_PERSISTENT
    return cwg.work_class(marker_paths(entry))


def receipt_preflight(receipt, entry):
    """Reject malformed, misclassified, or path-risk-downgraded receipts before scanning."""
    if receipt is None:
        return False, "terminal receipt is missing or malformed"
    kind, _, risk = receipt
    if candidate_class(entry) == cwg.WORK_OPERATIONAL:
        if kind not in OPERATIONAL_RECEIPTS:
            return False, (
                "this candidate changed no lasting artifact and ends with "
                "[gate] operational: <pre-execution check>; <verified effect> "
                "or [gate] no-change: <reason>"
            )
        return True, "preflight"
    if kind in OPERATIONAL_RECEIPTS:
        return False, "{} cannot close a candidate that changed a lasting artifact".format(kind)
    required_risk = cwg.max_risk(
        minimum_risk(marker_paths(entry)),
        entry.get("minimum_risk_seen"),
    )
    effective_risk = risk or "HIGH"
    if RISK_ORDER[effective_risk] < RISK_ORDER[required_risk]:
        return False, "declared risk {} is below path-based minimum {}".format(
            effective_risk, required_risk
        )
    return True, "preflight"


def latest(events):
    return max(events, default=(0.0, None), key=lambda item: item[0])


def active_review_start(evidence, last_ts):
    """Timestamp after which the ordinary review record still describes this candidate.

    An APPROVED ends the gate, so a marked edit after one retires it together with the round
    sequence that produced it: the reviews that follow judge different code and are a fresh
    round 1, not the illegal continuation the terminal-verdict guards exist to catch. Without
    this, the freshness rule for HIGH would demand an approval those guards forbid obtaining.

    Only APPROVED retires. REVISE leaves an unresolved objection that outlives the edit meant
    to answer it, and ESCALATE hands over to a closure phase whose remediation edits are
    expected — retiring either would erase evidence the later guards depend on.
    """
    start = -1.0
    for stamp, verdict in evidence["ordinary_reviews"]:
        if verdict == "APPROVED" and stamp < last_ts:
            start = max(start, stamp)
    return start


def evaluate_receipt(receipt, entry, evidence):
    """Precondition: `receipt` already passed `receipt_preflight` against this marker."""
    kind, _, risk = receipt
    first_ts = float(entry.get("first_ts") or 0.0)
    last_ts = float(entry.get("last_ts") or first_ts)
    paths = marker_paths(entry)

    # Session-scoped on purpose. The call proves the protocol was read, and re-reading it for
    # each candidate adds nothing: the text is already in context, so a per-candidate rule only
    # bought a restatement of the classification the agent had already made.
    if not cwg.valid_ts(evidence["skills"].get("development-verification", 0.0)):
        return False, "development-verification was not invoked in this session"

    # A live-system action carries no diff to review and no artifact to polish, so its whole
    # observable contract is the skill, which holds the pre-execution rule, plus both halves of
    # the operational receipt. The review panel here judged a script that had already run.
    # `no-change` still needs the skill: the cycle only exists because something mutating ran,
    # and a one-line claim that it changed nothing must not be the cheapest way out.
    if kind in OPERATIONAL_RECEIPTS:
        return True, kind

    if evidence.get("scan_failed"):
        return False, "the transcript scan failed, so the evidence for this candidate is partial"

    # Every freshness rule below asks whether evidence still covers the candidate the reviewer
    # read. That is the last change to a lasting artifact: a rerun maintenance command or a
    # rewritten throwaway script leaves the reviewed diff untouched. Markers written before this
    # field existed fall back to the strict whole-cycle timestamp.
    durable_ts = float(entry.get("last_durable_ts") or 0.0)
    if not cwg.valid_ts(durable_ts):
        durable_ts = last_ts

    review_start = active_review_start(evidence, durable_ts)
    ordinary_reviews = [
        item for item in evidence["ordinary_reviews"] if item[0] > review_start
    ]
    ordinary_verdicts = [verdict for _, verdict in ordinary_reviews]
    closure_verdicts = [verdict for _, verdict in evidence["closure_reviews"]]

    if len(ordinary_verdicts) > MAX_REVIEW_ROUNDS:
        return False, "ordinary review exceeded MAX_REVIEW_ROUNDS={}".format(
            MAX_REVIEW_ROUNDS
        )
    if len(closure_verdicts) > MAX_CLOSURE_PASSES:
        return False, "closure validation exceeded MAX_CLOSURE_PASSES={}".format(
            MAX_CLOSURE_PASSES
        )
    if "APPROVED" in ordinary_verdicts[:-1]:
        return False, "ordinary review continued after terminal APPROVED"
    if "ESCALATE" in ordinary_verdicts[:-1]:
        return False, "ordinary review continued after terminal ESCALATE"
    if "READY" in closure_verdicts[:-1]:
        return False, "closure validation continued after terminal READY"

    effective_risk = risk or "HIGH"

    path_count = len(set(cwg.durable_paths(paths)))
    requires_simplify = effective_risk == "HIGH" or (
        effective_risk == "STANDARD" and path_count >= 3
    )
    simplify_unavailable = False
    # The evidence a simplify pass leaves is three named lenses returning in the foreground for
    # this candidate. Keying that on a preceding Skill call, and counting only lens results that
    # followed it, enforced an order rather than the work: a candidate whose lenses ran first
    # had to re-invoke the skill and then re-run all three over already-simplified code — a
    # second pass the skill itself forbids as ritual.
    missing = []
    exhausted = []
    for reviewer in sorted(SIMPLIFY_REVIEWERS):
        # Both lists are already candidate-bound by transcript_evidence.
        successes = evidence["simplify_successes"].get(reviewer, [])
        failures = sorted(evidence["simplify_failures"].get(reviewer, []))
        if len(successes) > MAX_SIMPLIFY_PASSES:
            return False, "simplify exceeded the absolute {}-pass cap".format(
                MAX_SIMPLIFY_PASSES
            )
        latest_success = max(successes, default=0.0)
        if latest_success and latest_success > (failures[-1] if failures else 0.0):
            continue
        missing.append(reviewer)
        retries = [stamp for stamp in failures if stamp > latest_success]
        if len(retries) >= 2 and retries[-1] >= durable_ts:
            exhausted.append(reviewer)
    if requires_simplify and missing:
        if kind == "draft-blocked" and set(exhausted) == set(missing):
            simplify_unavailable = True
        else:
            return False, "simplify lenses have no foreground result: {}".format(
                ", ".join(missing)
            )

    ordinary_ts, ordinary_verdict = latest(ordinary_reviews)
    closure_ts, closure_verdict = latest(evidence["closure_reviews"])
    failed_ts = max(evidence["review_failures"], default=0.0)
    current_failure = failed_ts >= durable_ts
    required_external_calls = [
        call
        for call in evidence["external_calls"]
        if call[2]
    ]
    required_external_success = False
    required_external_unavailable = False
    if required_external_calls:
        call_ts, call_id, _, foreground = max(
            required_external_calls, key=lambda item: item[0]
        )
        matching_results = [
            result
            for result in evidence["external_results"]
            if result[1] == call_id and result[2]
        ]
        result_ts, _, _, result_status = max(
            matching_results,
            default=(0.0, None, True, "missing"),
            key=lambda item: item[0],
        )
        current_external = (
            foreground
            and call_ts >= durable_ts
            and result_ts >= durable_ts
        )
        required_external_success = (
            current_external and result_status == "success"
        )
        required_external_unavailable = (
            current_external and result_status == "failure"
        )

    if ordinary_verdict == "APPROVED" and any(
        stamp > ordinary_ts for stamp, _, _ in evidence["review_events"]
    ):
        return False, "review activity continued after terminal APPROVED"
    if closure_verdict == "READY" and any(
        stamp > closure_ts for stamp, _, _ in evidence["review_events"]
    ):
        return False, "review activity continued after terminal READY"
    if evidence["closure_reviews"]:
        if ordinary_verdict != "ESCALATE":
            return False, "closure validation requires round-3 ESCALATE"
        if any(stamp <= ordinary_ts for stamp, _ in evidence["closure_reviews"]):
            return False, "closure validation must occur after round-3 ESCALATE"

    expected_escalation = (
        ["REVISE"] * (MAX_REVIEW_ROUNDS - 1) + ["ESCALATE"]
    )
    if ordinary_verdict == "ESCALATE" and ordinary_verdicts != expected_escalation:
        return False, "ESCALATE requires exactly {}".format(
            ", ".join(expected_escalation)
        )

    if required_external_calls:
        if kind in ("verified", "pr-ready") and not required_external_success:
            return False, "required external Codex evidence is missing, stale, or failed"
        if kind == "draft-blocked" and not (
            required_external_success or required_external_unavailable
        ):
            return False, "required external Codex result is missing or stale"

    if kind == "verified":
        if closure_verdicts:
            return False, "verified cannot follow autonomous closure validation"
        if any(verdict == "ESCALATE" for _, verdict in evidence["ordinary_reviews"]):
            return False, "ESCALATE requires autonomous closure, not verified"
        if ordinary_verdicts and ordinary_verdict != "APPROVED":
            return False, "an invoked review has no terminal APPROVED verdict"
        if risk == "HIGH" and not (
            ordinary_verdict == "APPROVED" and ordinary_ts >= durable_ts
        ):
            return False, "HIGH candidate lacks a current APPROVED verdict"
        return True, "verified"

    if ordinary_verdict != "ESCALATE":
        if not (
            kind == "draft-blocked"
            and (
                current_failure
                or simplify_unavailable
                or required_external_unavailable
            )
        ):
            return False, (
                "{} requires round-3 ESCALATE or exhausted required evidence"
            ).format(kind)

    if kind == "pr-ready":
        if not (closure_verdict == "READY" and closure_ts >= durable_ts):
            return False, "pr-ready lacks current CLOSURE_VALIDATION: READY"
        return True, "pr-ready"

    if closure_verdict == "READY":
        return False, "draft-blocked conflicts with CLOSURE_VALIDATION: READY"
    if not (
        (closure_verdict == "BLOCKED" and closure_ts >= durable_ts)
        or (
            current_failure
            and (ordinary_verdict != "ESCALATE" or failed_ts > ordinary_ts)
        )
        or simplify_unavailable
        or required_external_unavailable
    ):
        return False, "draft-blocked lacks current BLOCKED or unavailable evidence"
    return True, "draft-blocked"


def close_cycle(marker, state_file, state, candidate_ts, receipt):
    now = time.time()
    state.update({
        "candidate_ts": candidate_ts,
        "blocks": 0,
        "closed_at": now,
        "receipt": "{}: {}".format(receipt[0], receipt[1]),
    })
    if not cwg.write_json(state_file, state):
        return False

    current = cwg.read_json(marker)
    if current is None or current.get("last_ts") == candidate_ts:
        if cwg.remove(marker):
            return True
        current = dict(current or {}, closed=True)
    else:
        current["first_ts"] = current.get("last_ts") or now
    return cwg.write_json(marker, current)


def reminder(reason, block_number, operational):
    if operational:
        contract = (
            "This candidate changed no lasting artifact: it ran commands or a throwaway "
            "script. Invoke development-verification and end with "
            "`[gate] operational: <what was established before executing>; <verified effect "
            "on the system>`, or, when nothing was modified at all, with "
            "`[gate] no-change: <reason>`, which needs no effect half. No simplify pass and no "
            "adversarial review are required for operational work."
        )
    else:
        contract = (
            "Satisfy the observable evidence contract. development-verification must have been "
            "invoked once in this session, plus honest candidate-bound checks. A HIGH or "
            "three-plus-file STANDARD candidate also needs the three named simplify lenses to "
            "have returned foreground results for this candidate — the lenses are the evidence, "
            "in whatever order they ran, so do not re-run a completed pass to satisfy this; two "
            "failed attempts for a required lens end draft-blocked. HIGH completion requires one "
            "adversarial APPROVED result newer than the final edit to a lasting artifact, from "
            "the foreground Codex lane (/adversarial-review) or, when Codex is unavailable, the "
            "native reviewer (/adversarial-review-internal). "
            "ESCALATE is not terminal: continue through at most two closure validations to READY "
            "or BLOCKED."
        )
    return (
        "[Code Work Gate] Cannot finalize this candidate: {}.\n{}\n"
        "This is finite enforcement block {}/{} for the unchanged candidate."
    ).format(reason, contract, block_number, MAX_BLOCKS_PER_CANDIDATE)


def main():
    data = cwg.read_payload()
    if data is None:
        allow()
        return

    try:
        key = cwg.session_key(data.get("session_id"))
        marker = cwg.marker_path(key)
        state_file = cwg.state_path(key)
        if not os.path.exists(marker):
            allow()
            return

        entry = cwg.read_json(marker) or {}
        if entry.get("closed"):
            allow()
            return
        candidate_ts = entry.get("last_ts")
        first_ts = float(entry.get("first_ts") or candidate_ts or 0.0)
        state = cwg.read_json(state_file) or {}
        if state.get("candidate_ts") != candidate_ts:
            state["candidate_ts"] = candidate_ts
            state["blocks"] = 0

        receipt = receipt_of(data.get("last_assistant_message"))
        preflight_ok, reason = receipt_preflight(receipt, entry)
        if preflight_ok:
            evidence = transcript_evidence(
                data.get("transcript_path"), first_ts, skill_since=0.0
            )
            valid, reason = evaluate_receipt(receipt, entry, evidence)
        else:
            valid = False
        if valid:
            if close_cycle(marker, state_file, state, candidate_ts, receipt):
                allow("Code Work Gate recorded terminal state: {}".format(receipt[0]))
            else:
                allow("Code Work Gate could not retire its state and is failing open.")
            return

        blocks = int(state.get("blocks") or 0)
        if blocks >= MAX_BLOCKS_PER_CANDIDATE:
            exhausted = ("enforcement-exhausted", reason, None)
            note = (
                "Code Work Gate exhausted its finite block budget for this unchanged candidate. "
                "The task is ending UNVERIFIED: {}."
            ).format(reason)
            if not close_cycle(marker, state_file, state, candidate_ts, exhausted):
                note += " Gate state could not be retired."
            allow(note)
            return

        state["blocks"] = blocks + 1
        state["last_block_ts"] = time.time()
        if not cwg.write_json(state_file, state):
            allow("Code Work Gate state is unavailable and enforcement is failing open.")
            return
        emit({
            "decision": "block",
            "reason": reminder(
                reason,
                blocks + 1,
                candidate_class(entry) == cwg.WORK_OPERATIONAL,
            ),
        })
    except Exception:
        allow()


if __name__ == "__main__":
    main()
