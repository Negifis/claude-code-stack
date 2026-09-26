"""
Code Work Gate - strict but finite Stop hook.

The development-verification skill owns engineering judgment. This hook verifies a small set of
observable protocol facts in the Claude Code transcript, under the contract that matches what
the candidate produced — a lasting artifact, or an effect on a live system:

* development-verification was actually invoked;
* an operational candidate ends with a receipt naming its pre-execution check and its effect;
* a STANDARD candidate has one foreground simplify-reviewer result, a HIGH candidate one from
  each of the three simplify lenses, an XHIGH candidate one from each lens's XHIGH profile;
* HIGH completion has an adversarial APPROVED result — from either review engine — newer than
  the final edit, and XHIGH completion has one from an XHIGH lane: the native XHIGH reviewer, or
  a Codex turn its rollout log shows on the XHIGH model and effort;
* post-ESCALATE publication has a bounded closure-validation result.

It never re-runs review itself. A candidate can be blocked at most three times; after that the
cycle is retired as unverified, preventing an infinite Stop loop.
"""
import datetime
import fnmatch
import hashlib
import html
import json
import math
import os
import re
import time
import uuid

import code_work_gate_common as cwg

cwg.configure_utf8_streams()

MAX_BLOCKS_PER_CANDIDATE = 3
# A turn may end without a receipt while this session's own background work is still running:
# the harness resumes the session with the task's completion notification, and blocking here
# only made the parent poll the task in a loop (364 polling calls in one session on 2026-09-02).
# Bounded twice: a task older than this is treated as dead, and a candidate gets this many
# waiting stops before the ordinary block applies again.
BACKGROUND_WAIT_LIMIT = 2 * 3600.0
MAX_BACKGROUND_WAITS = 8
# The harness's own acknowledgement envelopes, matched whole: a result that says anything more
# — a review that opens by mentioning a background task — is not an acknowledgement.
# After the envelope the harness may add its own notes, one per line — today the one it
# writes when the command changed directory, matched through its fixed wording so nothing
# can ride on the note's line. Every note starts with exactly one line break and nothing
# else may follow, so a review that opens with these words is still a review, and a result
# that nearly matches costs the regex one pass, not a search.
ACK_TAIL = (
    r"(?: To check interim output, use Read on that file path\.)?"
    r"(?:[ \t]*\r?\nSession cwd remains [^\r\n]*?; directory changes made by the "
    r"backgrounded command do not apply to subsequent commands\.)*[ \t\r\n]*$"
)
DETACHED_ACK_RE = re.compile(
    r"^\s*Command running in background with ID: ?([A-Za-z0-9_-]+)\. "
    r"Output is being written to: [^\n]+?\.output\. You will be notified when it completes\."
    + ACK_TAIL
)
MOVED_ACK_RE = re.compile(
    r"^\s*Command did not complete within its \d+s timeout and was moved to the background "
    r"\(ID: ?([A-Za-z0-9_-]+)\)\. Output is being written to: [^\n]+?\.output\. "
    r"You will be notified when it completes\." + ACK_TAIL
)
AGENT_BG_RE = re.compile(
    r"^\s*Async agent launched successfully\.[^\n]*\n?agentId: ([A-Za-z0-9_-]+)"
)
# The Workflow tool's answer: every script runs in the background until its task notification, and
# the summary line names it. Read only as the result of a Workflow call, so no other text can
# declare work in flight.
WORKFLOW_BG_RE = re.compile(
    r"^\s*Workflow launched in background\. Task ID: ([A-Za-z0-9_-]+)[ \t]*(?:\r?\nSummary: ([^\r\n]*))?"
)
# The id a foreground agent result names for continuing that agent with SendMessage.
AGENT_TRAILER_RE = re.compile(r"(?:^|\n)agentId: ([A-Za-z0-9_-]+) \(use SendMessage")
# What SendMessage returns when it resumed a finished agent; a message to a running one says
# nothing like it and starts no round.
RESUMED_AGENT_RE = re.compile(r"\"resumedAgentId\"\s*:\s*\"([A-Za-z0-9_-]+)\"")
# Raw-line tokens without which a pre-candidate transcript line can contribute nothing: a skill
# timestamp, the background bookkeeping that spans the whole session, or the id of a reviewer a
# later round may resume.
PRE_CANDIDATE_TOKENS = (
    '"Skill"', '"SlashCommand"', '"TaskStop"', '"Agent"', '"Task"',
    "in background with ID", "moved to the background", "Async agent launched",
    "use SendMessage", '"SendMessage"', "resumedAgentId", '"Workflow"', "Workflow launched in background",
)
NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTIFICATION_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
NOTIFICATION_CALL_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
NOTIFICATION_STATUS_RE = re.compile(r"<status>([^<]+)</status>")
# An agent's notification carries its final message, HTML-escaped, as the result.
NOTIFICATION_RESULT_RE = re.compile(r"<result>(.*?)</result>", re.S)
MAX_REVIEW_ROUNDS = 3
MAX_CLOSURE_PASSES = 2
MAX_SIMPLIFY_PASSES = 2
RISK_ORDER = cwg.RISK_ORDER
# HIGH work gets a separate context per simplify concern; STANDARD gets the one lane covering
# all three, which the complete trio also satisfies. The lane names are what the transcript
# shows, so they are the whole of the evidence.
SIMPLIFY_LANE = cwg.SIMPLIFY_LANE
SIMPLIFY_LENSES = cwg.SIMPLIFY_LENSES
XHIGH_LENSES = cwg.XHIGH_LENSES
SIMPLIFY_REVIEWERS = {SIMPLIFY_LANE, *SIMPLIFY_LENSES, *XHIGH_LENSES}
# The HIGH lens an XHIGH lens run proves when its call overrode the profile's model.
XHIGH_BASE = dict(zip(XHIGH_LENSES, SIMPLIFY_LENSES))
TERMINAL_RE = re.compile(
    r"^\[gate\]\s*(verified|operational|no-change|pr-ready|draft-blocked|anomaly-reported)"
    r"\s*:\s*(\S.*)$",
    re.IGNORECASE,
)
# `[gate] anomaly-reported: <report id>; <the verifiable contradiction>`
# Wall-clock budget for the git calls that decide whether a repository is back where the
# candidate opened: the Stop hook has a twenty-second window of its own.
RESTORE_BUDGET = 4.0
ANOMALY_REASON_RE = re.compile(r"^([0-9a-f]{8})\s*;\s*\S")
VERIFIED_REASON_RE = re.compile(r"^({})\s*;\s*\S".format("|".join(RISK_ORDER)), re.IGNORECASE)
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
# 300 MB logs, and the hook answers inside a twenty-second Stop timeout.
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
# What each session was given inside the same window, from the same scan.
_CODEX_GIVEN = {}
# (model, effort) of the turn each record of `_CODEX_SAID` was said in, aligned with that list.
_CODEX_TURNS = {}
# The session being judged, for the ledger lines written from inside the transcript scan — and
# whether they are written at all: the inbox runs the same scan for inspection only.
_SESSION = {"key": "", "effects": True}


def review_note(**fields):
    # Only an XHIGH verdict carries its level into the ledger; every other line stays as it was.
    if not fields.get("tier"):
        fields.pop("tier", None)
    if _SESSION["effects"]:
        cwg.log_event("review", session=_SESSION["key"], **fields)
REQUIRED_EXTERNAL_TOKEN = "CODE_WORK_GATE_REQUIRED"
# Declared intent, never proof, and it governs one case only: whether a result that cannot be
# attributed is heard as failed review activity. A verdict itself is heard because a briefed
# Codex session produced it, marker or not, so this cannot buy acceptance.
REVIEW_INTENT_TOKEN = cwg.REVIEW_INTENT_TOKEN


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
    return os.path.join(cwg.codex_home(), "sessions")


def codex_run_files(since):
    """(path, mtime) for each Codex rollout log the CLI may have written for this candidate.

    Cached per hook run: the answer is the same for every call being judged, and the scan reads
    a dated tree the CLI owns. A Codex home the CLI does not write — a different CODEX_HOME, a
    disabled log — yields nothing, which costs the Codex lane its evidence and falls back to the
    native reviewer rather than accepting an unproven one.

    `since` is a floor on how far back to look, not the binding: which call a session vouches
    for is decided per record in `session_said_place`. The current caller opens the window at the
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


normalized = cwg.normalized


def reviewer_role():
    """The opening of the reviewer role definition this machine owns, normalized.

    SECURITY: this is what makes the Codex lane's reviewer identity as attested as the native
    lane's. There, `subagent_type` makes the harness deliver `agents/adversarial-reviewer.md` as
    the system prompt; here, the whole of that text has to appear in what the session was given,
    before it says anything the gate will hear. An excerpt would let a session quoting the
    opening lines pass as briefed. Neither lane can attest that the reviewer obeyed its role —
    only that the role was the one on disk.
    """
    return reviewer_role_texts()[1]


def reviewer_role_texts():
    """(whole_file, body_below_front_matter) of the reviewer role, normalized.

    A packet carries the role in one of two spellings: the file as it sits on disk, front matter
    and all, or only the body — which is what the harness delivers to the native lane and so what
    marks a session briefed. Both have to be known to tell a packet's own words from the role's.
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
                whole = stream.read()
        except OSError:
            whole = ""
        _CODEX_ROLE["file"] = normalized(whole)
        _CODEX_ROLE["role"] = normalized(whole.split("---", 2)[-1]) if whole else ""
    return _CODEX_ROLE["file"], _CODEX_ROLE["role"]


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


def codex_cache_key(path, mtime_ns, size, started, finished):
    """What one parse of a rollout log is keyed by: the file's state and the call's window."""
    return (path, mtime_ns, size, started, finished)


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
    key = codex_cache_key(path, mtime_ns, size, started, finished)
    if key in _CODEX_SAID:
        return _CODEX_SAID[key]
    role = reviewer_role()
    briefed_at = None
    said, given, turn = [], [], None
    try:
        with open(path, "rb") as raw:
            for line in read_span(raw, 0, min(size, CODEX_HEAD_BYTES)):
                if role and role in normalized(logged_input(json_record(line))):
                    briefed_at = record_time(line)
                    break
            # One byte back so the record starting exactly at that offset is not mistaken for
            # the partial line `read_span` discards.
            for line in read_span(raw, max(0, seek_time(raw, size, started) - 1), size):
                if '"turn_context"' in line:
                    # Output belongs to the turn opened last before it in the log's own order:
                    # records written in one millisecond share a stamp, so time cannot tell.
                    turn = turn_of(line)
                    continue
                record = json_record(line)
                stamp = parse_ts(record.get("timestamp"))
                if stamp > finished + CODEX_RUN_SLACK:
                    break
                spoken = logged_output(record)
                if spoken:
                    said.append((stamp, spoken, turn))
                    continue
                heard = logged_input(record)
                if heard:
                    heard = normalized(heard)
                    given.append(heard)
                    if role and role in heard:
                        # A resumed round is briefed again, right before it answers.
                        briefed_at = stamp if briefed_at is None else min(briefed_at, stamp)
    except OSError:
        pass
    # Only what the session said after it was briefed counts, and normalizing is deferred until
    # then: an errand's output is discarded whole, which on these logs is the common case.
    _CODEX_GIVEN[key] = given
    kept = [] if briefed_at is None else [record for record in said if record[0] >= briefed_at]
    _CODEX_TURNS[key] = [opened for _, _, opened in kept]
    _CODEX_SAID[key] = [(at, normalized(text), text) for at, text, _ in kept]
    return _CODEX_SAID[key]


def turn_of(line):
    """(model, effort) of one rollout `turn_context` record, or None.

    The CLI writes this record itself when a turn starts, naming the model and the reasoning
    effort that turn runs on; the launch command only asks for them.
    """
    try:
        record = json.loads(line)
    except Exception:
        return None
    if not isinstance(record, dict) or record.get("type") != "turn_context":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    return str(payload.get("model") or ""), str(payload.get("effort") or "")


def codex_tier(path, mtime_ns, size, started, finished, place):
    """XHIGH when the record at `place` in `session_records` was said in a turn on the XHIGH
    model and effort: the `turn_context` the log opened last before it inside the call's window.

    SECURITY: the level comes from the rollout log, like the verdict itself, never from the
    command that launched Codex — that text is the agent's own and can name any model. A record
    with no turn opened before it in the window has no level.
    """
    session_records(path, mtime_ns, size, started, finished)
    turns = _CODEX_TURNS.get(codex_cache_key(path, mtime_ns, size, started, finished), ())
    turn = turns[place] if 0 <= place < len(turns) else None
    return "XHIGH" if turn == (cwg.XHIGH_CODEX_MODEL, cwg.XHIGH_CODEX_EFFORT) else None


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
        record = json.loads(line)
    except Exception:
        return {}
    return record if isinstance(record, dict) else {}


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


def session_said_place(path, mtime_ns, size, started, finished, excerpt):
    """Where this session's own output, written while the call was open, last carried this text:
    the record's place in `session_records`, or None when it never did.

    Each record is judged by its own timestamp rather than the file's: a resumed session's log
    still holds every earlier review, and the file is touched again the moment it resumes.
    Tolerance is on the upper bound only, because the CLI finishes writing after the command
    returns while a record predating the call belongs to an earlier review, however narrowly.
    """
    found = None
    for place, (stamp, spoken, _) in enumerate(
            session_records(path, mtime_ns, size, started, finished)):
        if started <= stamp <= finished + CODEX_RUN_SLACK and excerpt in spoken:
            found = place
    return found


def codex_produced(text, started, finished, since):
    """(bound, tier): whether a Codex run overlapping this call logged the very output the call
    returned, and the level of the turn that said it (`codex_tier`).

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
        return False, None
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
    for _, path, mtime_ns, size in candidates:
        place = session_said_place(path, mtime_ns, size, started, finished, excerpt)
        if place is not None:
            return True, codex_tier(path, mtime_ns, size, started, finished, place)
    return False, None


# A packet shorter than this is not distinctive enough to name a session.
PACKET_MIN_CHARS = 200


def distinctive_of(packet):
    """The run of a packet's own words after the reviewer role: what names one session.

    Every review is given the same role, so only the rest of the packet tells one session from
    another — and it has to stay a single unbroken run of the packet's text, because that is what
    the rollout log records verbatim. Both spellings of the role are cut, the file before its
    body: a packet assembled from the whole role file, front matter included, has only its body
    recognized otherwise, and cutting that from the middle splices the front matter onto the
    brief — a string no record holds, which is how real reviews went unbound.

    The last run is the answer, not the longest. The packet contract puts the role first and
    verbatim, so the brief is what follows it. Taking the longest would hand a packet whose front
    matter did not match this machine's file — an older copy pasted in, a line edited since —
    that unmatched front matter as its identity, and front matter is common to every packet built
    the same way: it would bind the verdict of whichever session happened to answer, not of the
    one given this brief. A short run binds nothing, which the caller enforces.
    """
    parts = [packet]
    for role in reviewer_role_texts():
        if role:
            parts = [piece for part in parts for piece in part.split(role)]
    return " ".join(parts[-1].split())


def packet_of_launch(command, call_id, started=None):
    """(fed_on_stdin, distinctive_packet_text, why_unbindable) for a Codex launch.

    The packet is what the session was given, and Codex logs it verbatim, so it names the one
    session among several that this launch started. It is read from the capture the marker hook
    took before the command ran — not from the file, which the model may have rewritten since.
    Without a capture, because that hook did not run to the end, the file itself is read when
    nothing can have written it after the launch started. What names the session is the packet
    beyond the role every review is given, and it has to be long enough to be distinctive. A
    launch that fed something it cannot be bound by binds nothing, and the third value says why.
    """
    try:
        import code_work_gate_mark as mark
        launch = mark.codex_launch(command)
    except Exception:
        mark = None
        launch = {"fed": "<" in str(command or ""), "path": ""}
    if not launch["fed"]:
        return False, "", ""
    if not launch["path"]:
        return True, "", "the hooks could not name the file the launch fed"
    capture = cwg.read_json(cwg.packet_capture_path(_SESSION["key"], str(call_id or "")))
    # A missing capture reads as None, not a dict — a launch whose PreToolUse mark hook was
    # cancelled (it can run tens of seconds) leaves none. Guard the whole record before any
    # field access: dereferencing None here aborted the entire Stop scan, and everything after
    # the aborting notification went unread, so a later in-flight task earned no wait and the
    # turn was blocked with no receipt instead.
    if not isinstance(capture, dict):
        capture = packet_from_file(mark, launch["path"], started)
        if capture is None:
            return True, "", "no capture, and the file changed after the launch or cannot be read"
    if capture.get("truncated"):
        return True, "", "the packet is longer than a capture keeps"
    text = capture.get("text")
    if not isinstance(text, str):
        return True, "", "the capture holds no text"
    distinctive = distinctive_of(text)
    if len(distinctive) < PACKET_MIN_CHARS:
        return True, "", "the packet beyond the reviewer role is too short to name a session"
    return True, distinctive, ""


def packet_from_file(mark, path, started):
    """The packet file read as a capture, when its last write came before the launch (R4).

    The file is stated before and after the read, so a write landing in between binds nothing.
    """
    if mark is None or not cwg.valid_ts(started):
        return None
    try:
        before = os.stat(path)
        if before.st_mtime > float(started):
            return None
        read = mark.packet_text(path)
        after = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    if read is None or (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
        return None
    return {"text": read[0], "truncated": read[1]}


def session_given(path, mtime_ns, size, started, finished, packet):
    """Whether this session was given the packet inside the call's window.

    Read from the same scan that produced the session's records, so the shared read budget is
    charged once per log, not once more per candidate.
    """
    if not packet:
        return False
    session_records(path, mtime_ns, size, started, finished)
    key = codex_cache_key(path, mtime_ns, size, started, finished)
    return any(packet in heard for heard in _CODEX_GIVEN.get(key, ()))


def rollout_verdict(started, finished, since, command="", call_id=""):
    """The verdict one briefed Codex session stated between a background launch and its notification.

    The task's output file is the harness's copy of what Codex printed, and nothing keeps it that
    way afterwards, so it is not evidence: the verdict is read from the rollout log Codex wrote
    itself, from records stamped inside the launch-to-notification window. When the launch fed a
    packet on stdin, the session that was given that packet is the one; otherwise exactly one
    briefed session may have spoken there — two are ambiguous, and an ambiguous verdict binds
    to nothing, which is the safe direction. Returns `(verdict, why_none, tier)`, the tier being
    that of the turn which stated the verdict (`codex_tier`).
    """
    fed, packet, why = packet_of_launch(command, call_id, started)
    if fed and not packet:
        # The usual cause: the packet was written by the same shell command that launched
        # Codex, so nothing existed when the marker hook looked before the command ran.
        return None, ("packet fed on stdin but nothing to bind by at launch ({}); write the "
                      "packet in its own call before launching").format(why), None
    verdicts, given = {}, []
    for path, _ in codex_run_files(since):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime < started:
            continue
        for place, (stamp, _, spoken) in enumerate(session_records(
            path, stat.st_mtime_ns, stat.st_size, started, finished
        )):
            if not started <= stamp <= finished + CODEX_RUN_SLACK:
                continue
            control = reviewer_control(spoken)
            if control[0] in ("ordinary", "closure"):
                # The session's last stated verdict inside the window is its answer.
                verdicts[path] = (control, place, stat.st_mtime_ns, stat.st_size)
        if packet and session_given(path, stat.st_mtime_ns, stat.st_size, started, finished, packet):
            given.append(path)

    def answer(path):
        control, place, mtime_ns, size = verdicts[path]
        return control, "", codex_tier(path, mtime_ns, size, started, finished, place)

    if packet:
        # Exactly one session may have been given this packet, whether or not it answered:
        # two chats reviewing with the same words are told apart by nothing, and bind nothing.
        if len(given) != 1:
            unbound = ("no session in the window was given this launch's packet" if not given
                       else "{} sessions were given this launch's packet".format(len(given)))
            return None, unbound, None
        if given[0] not in verdicts:
            return None, "the session given the packet stated no verdict in the window", None
        return answer(given[0])
    if len(verdicts) != 1:
        unbound = ("no briefed session stated a verdict in the window" if not verdicts
                   else "{} briefed sessions stated verdicts in the window".format(len(verdicts)))
        return None, unbound, None
    return answer(next(iter(verdicts)))


def record_control(evidence, stamp, control, malformed=False, tier=None):
    """File one reviewer result under the verdict it carries.

    `malformed` says whether a result that states no usable verdict is still review activity: it
    is, for the dedicated reviewer subagent that has no other purpose, and for a Codex call that
    declared itself the review lane but could not be attributed. A plain CLI errand is neither,
    and recording it would let an unrelated run reopen a closed gate. `tier` is "XHIGH" for a
    verdict from an XHIGH lane, which is also filed where an XHIGH receipt looks for it.
    """
    control_kind, control_value = control
    if control_kind == "ordinary":
        evidence["ordinary_reviews"].append((stamp, control_value))
    elif control_kind == "closure":
        evidence["closure_reviews"].append((stamp, control_value))
    elif not malformed:
        return
    if tier == "XHIGH" and control_kind in ("ordinary", "closure"):
        evidence["xhigh_verdicts"].append((stamp, control_value))
    evidence["review_events"].append((stamp, control_kind, control_value))


def lane_tier(subtype, payload):
    """The level a native review lane runs at: its profile's, unless the call passed a model, which
    overrides the profile's pin and so cannot show that the XHIGH model ran."""
    return "XHIGH" if subtype == cwg.XHIGH_REVIEWER and not payload.get("model") else None


def review_lane(task_id, calls_named, rounds, open_rounds, launched):
    """The judged review lane a notification about this task belongs to, or None.

    The round the notification names by its SendMessage call, then the agent's round still
    waiting for one, then the agent's own background launch.
    """
    for call_id in calls_named:
        if rounds.get(call_id, {}).get("agent") == task_id:
            return call_id
    if task_id in open_rounds:
        return open_rounds[task_id]
    return task_id if task_id in launched else None


def resumed_agent(text):
    """The agent a SendMessage result says it resumed, or None."""
    try:
        payload = json.loads(text)
    except Exception:
        match = RESUMED_AGENT_RE.search(str(text or ""))
        return match.group(1) if match else None
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    agent = payload.get("resumedAgentId")
    return agent if isinstance(agent, str) else None


def judge_background_agents(evidence, launches, notices_by_task):
    """File each backgrounded native review lane once all its notifications are read.

    The verdict is the control line in the notification's result — the harness's own record of
    the agent's final message, HTML-escaped there — filed at the launch exactly as a background
    Codex verdict is: nothing edited after the launch was read by the lane, so a durable edit
    since then expires it. An agent notifies once per stop, and one that paused to wait for its
    own background suite reports first without a verdict, so the first notice that states a
    verdict is the lane's verdict. Everything the lane says after it — a resumed agent
    stopped, killed, answering without a verdict, or stating a verdict again — is activity
    after the verdict, exactly as a second foreground call would be. No verdict at all is the
    same activity a foreground result without one is; a lane whose last word was a kill or a
    stop is a failed lane.
    """
    for task_id, notices in notices_by_task.items():
        launch = launches[task_id]
        notices = sorted(notices, key=lambda item: item[0])
        first = next(
            (index for index, (_, _, control) in enumerate(notices)
             if control is not None and control[0] in ("ordinary", "closure")),
            None,
        )
        if first is not None:
            stamp, _, control = notices[first]
            tier = launch.get("tier")
            record_control(evidence, launch["started"], control, tier=tier)
            resumed = {"agent": launch["agent"]} if launch.get("agent") else {}
            review_note(at=launch["started"], notified=stamp, engine="native-background",
                        verdict=control[1], task=task_id, tier=tier, **resumed)
            notices = notices[first + 1:]
            reason = "the lane went on after stating its verdict"
        else:
            reason = "no verdict in the lane's completion notifications"
        if not notices:
            continue
        last_stamp, last_status, _ = notices[-1]
        if last_status == "completed":
            record_control(evidence, last_stamp, ("malformed", None), malformed=True)
        else:
            evidence["review_failures"].append(last_stamp)
            record_control(evidence, last_stamp, ("failure", None), malformed=True)
        review_note(at=last_stamp, engine="native-background", verdict=None, task=task_id,
                    status=last_status, reason=reason)


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
        # (place in review_events, why) for each review result filed as unbound.
        "unbound_reasons": [],
        # (when, verdict) for each ordinary or closure verdict an XHIGH lane stated.
        "xhigh_verdicts": [],
        "external_calls": [],
        "external_results": [],
        "background": {},
        "background_done": {},
        "background_judged": [],
        "scan_failed": False,
    }
    if not path or not os.path.isfile(path):
        return evidence

    calls = {}
    # Background launches keyed by task id, so the completion notification can be matched to
    # the call that started it. A marked Codex launch that went to the background is judged
    # when its notification arrives, from the output file the harness wrote for it.
    background_calls = {}
    # Native review agents launched into the background, keyed by the agent id the harness
    # gave at launch, with every notification each one sent: judged after the scan, since an
    # agent that stops to wait for its own background suite notifies without a verdict first.
    background_agents = {}
    agent_notices = {}
    # Review agents by the id the harness gave them — a background launch's acknowledgement
    # names it, and so does a foreground result's trailer — so that a SendMessage resuming one
    # is a review round (report 7435cfb4). Rounds are keyed by that SendMessage call, whose id the
    # round's notification carries, and each agent's round still waiting for it is kept for a
    # notification that names no call.
    review_agents = {}
    resume_rounds = {}
    open_rounds = {}
    latest_rounds = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                if (
                    '"tool_use"' not in raw
                    and '"tool_result"' not in raw
                    and '"name"' not in raw
                    and "task-notification" not in raw
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
                if "task-notification" in raw and entry.get("type") in NOTIFICATION_RECORDS:
                    # Only the harness's own notification record is handled here; a tool
                    # result that merely mentions the word is ordinary evidence below.
                    notices = NOTIFICATION_RE.findall(notification_text(entry))
                    for notice in notices:
                        status_match = NOTIFICATION_STATUS_RE.search(notice)
                        status = (status_match.group(1) if status_match else "?").strip().lower()
                        calls_named = NOTIFICATION_CALL_RE.findall(notice)
                        for task_id in NOTIFICATION_ID_RE.findall(notice):
                            if task_id.startswith("__orphan"):
                                continue
                            delivery = entry.get("type") in ("user", "attachment")
                            lane = review_lane(task_id, calls_named, resume_rounds, open_rounds,
                                               background_agents)
                            # A native review agent's verdict lives in its delivery record — the
                            # absorbed command (`attachment`) or the idle turn (`user`) — not in
                            # the queue bookkeeping. Marking such an agent done on a lone enqueue
                            # (the turn ended between the enqueue and the attachment) would drop
                            # it from in-flight without its verdict ever being read, letting an
                            # earlier approval stand for a lane that had moved on. So a native
                            # lane is done only when its delivery record is seen; every other
                            # task (a Codex launch, judged from its rollout) is done on any
                            # notification, as before.
                            if lane is None or delivery:
                                evidence["background_done"][task_id] = (stamp, status)
                            if lane is not None and not stamp + 1 < since and delivery:
                                # One notice per physical delivery: the absorbed command
                                # (`attachment`) or the idle turn (`user`). The enqueue and the
                                # later `absorbed_mid_turn` remove are the same text as
                                # bookkeeping and would double-count a single delivery, or —
                                # since the remove can lag — invent a second verdict; a resumed
                                # agent's identical verdict is a new attachment, counted as
                                # activity after the first. Only a completed lane's result is a
                                # result; a killed or failed one's control is never read.
                                found = NOTIFICATION_RESULT_RE.search(notice)
                                control = (
                                    reviewer_control(html.unescape(found.group(1) if found else ""))
                                    if status == "completed" else None
                                )
                                agent_notices.setdefault(lane, []).append(
                                    (stamp, status, control)
                                )
                                if open_rounds.get(task_id) == lane:
                                    del open_rounds[task_id]
                            call = background_calls.pop(task_id, None)
                            if not call or stamp + 1 < since:
                                continue
                            # A backgrounded review lane is judged from the rollout log Codex
                            # wrote between the launch and this notification; its output file
                            # is for the parent to read, not evidence.
                            if status == "completed":
                                control, unbound_why, tier = rollout_verdict(
                                    call["started"], stamp, skill_since,
                                    call.get("command", ""), call["call_id"])
                            else:
                                control, unbound_why, tier = None, "the task ended " + status, None
                            bound = control is not None
                            # The verdict covers the candidate as it was when the review was
                            # launched — nothing edited after the launch was in the packet —
                            # so a bound verdict is filed at the launch, and a durable edit
                            # since then expires it exactly as it would a foreground one. A
                            # failure is filed when it became known.
                            evidence["external_results"].append(
                                (call["started"] if bound else stamp, call["call_id"],
                                 call["required"], "success" if bound else "failure")
                            )
                            evidence["background_judged"].append(call["call_id"])
                            if bound:
                                record_control(evidence, call["started"], control, tier=tier)
                                # `at` is where the verdict is filed, which is what expiry is
                                # judged against; the notification only reports it (a269a6fc).
                                review_note(at=call["started"], notified=stamp,
                                            engine="codex-background", verdict=control[1],
                                            task=task_id, tier=tier)
                            else:
                                evidence["review_failures"].append(stamp)
                                evidence["unbound_reasons"].append((len(evidence["review_events"]), unbound_why))
                                record_control(evidence, stamp, ("unbound", None), malformed=True)
                                review_note(at=stamp, engine="codex-background",
                                              verdict=None, task=task_id, status=status,
                                              reason="no single briefed Codex verdict between "
                                                     "launch and notification: " + unbound_why)
                                # The marker hook reads a foreground call's stderr when it
                                # returns; a background lane returns at launch, so its outage
                                # (usage limit, capacity) is only readable here, from the
                                # capture the command named.
                                if _SESSION["effects"]:
                                    record_background_outage(call)
                    if notices:
                        continue
                if stamp and stamp + 1 < skill_since:
                    continue
                before_candidate = bool(stamp) and stamp + 1 < since
                # A pre-candidate entry can only contribute a skill timestamp or background
                # bookkeeping — a server or suite started before the candidate opened is still
                # this session's running work — and neither is present without its token
                # appearing verbatim in the raw line.
                if before_candidate and not any(token in raw for token in PRE_CANDIDATE_TOKENS):
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
                        tool_name = block.get("name")
                        payload = block.get("input")
                        if not isinstance(payload, dict):
                            continue
                        call_id = block.get("id")
                        if tool_name == "TaskStop":
                            stopped = str(payload.get("task_id") or "")
                            if stopped:
                                evidence["background_done"][stopped] = (stamp, "stopped")
                                # A stop lands on the agent's latest round, else its first lane:
                                # after a verdict it is activity after that verdict.
                                lane = latest_rounds.get(stopped) or (
                                    stopped if stopped in background_agents else None
                                )
                                if lane is not None:
                                    agent_notices.setdefault(lane, []).append(
                                        (stamp, "stopped", None)
                                    )
                                call = background_calls.pop(stopped, None)
                                if call:
                                    # A review lane stopped by hand is failed lane activity,
                                    # exactly like a failed notification: it reopens an
                                    # earlier approval rather than leaving it the last word.
                                    evidence["external_results"].append(
                                        (stamp, call["call_id"], call["required"], "failure")
                                    )
                                    evidence["background_judged"].append(call["call_id"])
                                    evidence["review_failures"].append(stamp)
                                    evidence["unbound_reasons"].append(
                                        (len(evidence["review_events"]), "the review task was stopped"))
                                    record_control(evidence, stamp, ("unbound", None),
                                                   malformed=True)
                                    review_note(at=stamp, engine="codex-background",
                                                  verdict=None, task=stopped,
                                                  status="stopped",
                                                  reason="the review task was stopped")
                            continue
                        if tool_name == "SendMessage":
                            target = str(payload.get("to") or "")
                            if call_id:
                                reviewer = review_agents.get(target)
                                calls[call_id] = {
                                    "kind": "resume", "agent": target, "started": stamp,
                                    "foreground": False,
                                    # A resumed round runs at the level its reviewer was launched at.
                                    "tier": reviewer.get("tier") if reviewer else None,
                                    "label": reviewer.get("label", "") if reviewer
                                    else str(payload.get("summary") or target)[:80],
                                    # A round only counts inside the window, like any other review call.
                                    "round": reviewer is not None and not before_candidate,
                                }
                            continue
                        if tool_name == "Workflow" and call_id:
                            # Running work whenever it started, like a detached command: a
                            # workflow's own lanes write files until its notification (report
                            # 874ab23b), so a stop meanwhile waits rather than asking for a receipt.
                            # Unlike a shell or agent acknowledgement, a workflow's is never read
                            # without its call: text alone must not buy waiting stops.
                            calls[call_id] = {"kind": "workflow", "started": stamp, "foreground": False,
                                              "label": str(payload.get("name") or "")}
                            continue
                        if before_candidate and tool_name not in ("Agent", "Task"):
                            continue
                        if tool_name in cwg.SHELL_TOOLS and call_id:
                            command = str(payload.get("command") or "")
                            required = REQUIRED_EXTERNAL_TOKEN in command
                            # COMPAT: a shell call is foreground unless it is explicitly
                            # detached — the harness omits the field entirely for the ordinary
                            # case, so demanding an explicit false here made every real Codex
                            # result invisible and left the native lane as the only one that
                            # could satisfy a HIGH candidate. A detached launch, or a foreground
                            # one the harness moved to the background at its timeout, is judged
                            # later from its completion notification and output file.
                            foreground = payload.get("run_in_background") is not True
                            calls[call_id] = {
                                "kind": "external",
                                "required": required,
                                "foreground": foreground,
                                "started": stamp,
                                "marked": required or REVIEW_INTENT_TOKEN in command,
                                "call_id": call_id,
                                "command": command,
                                "label": command.strip().splitlines()[0][:80] if command.strip() else "",
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
                        # A model passed with the call overrides the profile's pin, so an XHIGH
                        # lens run that way proves only its HIGH lens (and a reviewer, `lane_tier`).
                        if payload.get("model") and subtype in XHIGH_BASE:
                            subtype = XHIGH_BASE[subtype]
                        # Claude Code 2.1.198+ defaults subagents to background. Require an
                        # explicit false so an upgrade cannot turn a verdict-bearing call into
                        # an async launch that looks complete in the transcript. The opposite
                        # polarity of the shell check above is deliberate: the two tools carry
                        # opposite defaults, and unifying them would blind one lane.
                        foreground = payload.get("run_in_background") is False
                        agent_call = {
                            "kind": "agent",
                            "subtype": subtype,
                            "tier": lane_tier(subtype, payload),
                            "foreground": foreground,
                            "started": stamp,
                            "label": str(payload.get("description") or subtype)[:80],
                            # A reviewer from before the window lends it nothing but its id,
                            # which a round inside the window may resume.
                            "prior": before_candidate,
                        }
                        if subtype in SIMPLIFY_REVIEWERS:
                            agent_call["kind"] = "simplify"
                        elif "adversarial-reviewer" in subtype:
                            agent_call["kind"] = "review"
                        if before_candidate and agent_call["kind"] != "review":
                            continue
                        calls[call_id] = agent_call

                    if block.get("type") != "tool_result":
                        continue
                    call = calls.get(block.get("tool_use_id"))
                    text = result_text(block)
                    if call is not None and call["kind"] == "workflow":
                        launched = WORKFLOW_BG_RE.match(text)
                        if launched:
                            evidence["background"][launched.group(1)] = {
                                "started": call["started"], "kind": "workflow",
                                "label": (call["label"] or launched.group(2) or "")[:80], "review": False,
                            }
                        continue
                    if call is not None and call["kind"] == "resume":
                        # Only a resumed agent is back in flight; a message queued to one still
                        # running is part of its own flight. Any agent this session resumed runs
                        # until its notification, reviewer or not (report ac2ee4da), and a
                        # reviewer's resumption inside the window is a round of its own.
                        agent = None if block.get("is_error") else resumed_agent(text)
                        if agent:
                            is_round = call["round"] and agent == call["agent"]
                            if is_round:
                                round_id = block.get("tool_use_id")
                                resume_rounds[round_id] = call
                                open_rounds[agent] = round_id
                                latest_rounds[agent] = round_id
                            evidence["background"][agent] = {
                                "started": call["started"], "kind": "agent",
                                "label": call["label"], "review": is_round,
                            }
                            evidence["background_done"].pop(agent, None)
                        continue
                    # Background bookkeeping spans the whole transcript: a server started before
                    # the candidate opened is still this session's running work, so the harness's
                    # acknowledgement is read even for a call the scan did not register.
                    if call is None or call["kind"] == "external":
                        task_id = background_ack(text, None if call is None else call["foreground"])
                        if task_id:
                            evidence["background"][task_id] = {
                                "started": stamp, "kind": "shell",
                                "label": call.get("label", "") if call else "",
                                "review": bool(call and call.get("marked")),
                            }
                            if call and call.get("marked"):
                                background_calls[task_id] = call
                            continue
                    if call is None or (call["kind"] != "external" and not call["foreground"]):
                        launched = AGENT_BG_RE.match(text)
                        if launched:
                            evidence["background"][launched.group(1)] = {
                                "started": stamp, "kind": "agent",
                                "label": call.get("label", "") if call else "",
                                "review": bool(call and call["kind"] == "review"),
                            }
                            if call and call["kind"] == "review":
                                review_agents[launched.group(1)] = call
                                if not call.get("prior"):
                                    background_agents[launched.group(1)] = call
                            continue
                    if call is None:
                        continue
                    if call["kind"] == "review" and call["foreground"]:
                        trailer = AGENT_TRAILER_RE.search(text)
                        if trailer:
                            review_agents[trailer.group(1)] = call
                    if (before_candidate or call.get("prior") or call["kind"] == "agent"
                            or not call["foreground"]):
                        continue
                    failed = bool(block.get("is_error"))
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
                        bound, tier = codex_produced(
                            text, call["started"], stamp, skill_since
                        ) if stated else (False, None)
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
                            record_control(evidence, stamp, control, tier=tier)
                            review_note(at=stamp,
                                          engine="codex", verdict=control[1], tier=tier)
                        elif bound or (stated and call["marked"]):
                            # An unattributable verdict is never filed as one, but dropping it
                            # would leave an earlier approval as the last word — so a call that
                            # declared itself the review lane is heard as failed activity, which
                            # reopens that approval. The declaration is needed only here: a
                            # result the session log vouches for has proved what it is.
                            evidence["review_failures"].append(stamp)
                            evidence["unbound_reasons"].append((len(evidence["review_events"]), (
                                "the review call failed after stating its verdict" if bound
                                else "no Codex run's log shows the verdict the call printed")))
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
                    control = reviewer_control(text)
                    tier = call.get("tier")
                    record_control(evidence, stamp, control, malformed=True, tier=tier)
                    if control[0] in ("ordinary", "closure"):
                        review_note(at=stamp,
                                      engine="native", verdict=control[1], tier=tier)
        judge_background_agents(evidence, dict(background_agents, **resume_rounds), agent_notices)
    except Exception:
        # Everything after the failure is unread, so what was collected is a prefix, not the
        # record: an approval early in the cycle would otherwise outlive the REVISE that
        # followed it. A lasting artifact cannot be signed off on a partial scan.
        evidence["scan_failed"] = True
    return evidence


# Where the harness records a task notification: as a user turn when the session was idle,
# and only as the queued command it absorbed mid-turn otherwise.
NOTIFICATION_RECORDS = ("user", "queue-operation", "attachment")


def notification_text(entry):
    """The text of a record the harness wrote for a task notification, whichever shape it took.

    A notification that arrives while the turn is running never becomes a user record: it is
    enqueued (`queue-operation`, the text as `content`) and absorbed as a queued command
    (`attachment` of type `queued_command`, the text as `prompt`). Reading both shapes is what
    keeps a task that finished mid-turn from being counted as still running.
    """
    kind = entry.get("type")
    if kind == "user":
        return user_text(entry)
    if kind == "queue-operation":
        content = entry.get("content")
        return content if isinstance(content, str) else ""
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and attachment.get("commandMode") == "task-notification":
        prompt = attachment.get("prompt")
        return prompt if isinstance(prompt, str) else ""
    return ""


def user_text(entry):
    """The plain text of a user record: a string body, or its text blocks joined."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def record_background_outage(call):
    """Let the Codex lane breaker read a background launch's stderr capture, never raising."""
    try:
        import codex_lane
        codex_lane.record_from_command(call.get("command", ""), "", started=call.get("started"))
    except Exception:
        pass


def background_ack(text, foreground=None):
    """The task id when a shell result is, whole, the harness's own background envelope.

    A detached launch acknowledges as running and a foreground call only as moved at its
    timeout, so each envelope is accepted for its own polarity; `foreground=None` — a call the
    scan did not register — accepts either. The result has to be the envelope and nothing else.
    """
    if foreground is not True:
        match = DETACHED_ACK_RE.match(text)
        if match:
            return match.group(1)
    if foreground is not False:
        match = MOVED_ACK_RE.match(text)
        if match:
            return match.group(1)
    return None


def in_flight(evidence, now=None):
    """Background tasks this session started that have not reported back and are not stale."""
    now = time.time() if now is None else now
    running = []
    for task_id, task in evidence.get("background", {}).items():
        if task_id in evidence.get("background_done", {}):
            continue
        if now - task["started"] > BACKGROUND_WAIT_LIMIT:
            continue
        running.append(dict(task, id=task_id))
    running.sort(key=lambda task: task["started"])
    return running


marker_paths = cwg.marker_paths


minimum_risk = cwg.minimum_risk


def candidate_key(entry):
    """What the finite block budget is spent against: the candidate's content, not its clock.

    The budget used to be keyed to the marker's `last_ts`, which every mark moves — including
    the marks the blocked turn itself produces while running the checks the block asked for,
    and, before attribution became session-owned, marks caused by a second session writing in
    the same tree. Each of those reset the counter to zero, so `MAX_BLOCKS_PER_CANDIDATE` was
    not a cap at all and enforcement could block without end.

    The fingerprint moves only when the candidate does: a new cycle (`first_ts`), a path that
    was not in it before, a higher risk grade, the diagnostic path cap being crossed, or the
    first unattributable durable change - which is its own input because a purely test-path
    one flips the flag without moving the grade. Running
    a test, re-editing a file already in the set, or a throwaway script leaves it alone, which
    is exactly the "unchanged candidate" the cap is documented to bound.
    """
    payload = json.dumps(
        [
            float(entry.get("first_ts") or 0.0),
            sorted(set(marker_paths(entry))),
            entry.get("minimum_risk_seen"),
            bool(entry.get("path_overflow")),
            bool(entry.get("unattributed_durable")),
        ],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unfenced_nonempty_lines(message):
    """Non-empty lines outside fenced blocks, the ones inside them, and whether every fence
    closed. A caller that only wants the visible text ignores the fenced half."""
    result = []
    fenced = []
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
        if line.strip():
            (result if fence is None else fenced).append(line.strip())
    return result, fenced, fence is None


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
    lines, _, closed = unfenced_nonempty_lines(message)
    return lines[-1] if lines and closed else ""


def stated_control(line):
    """The control one line states, or malformed for a control-shaped line stating none."""
    verdict = VERDICT_LINE_RE.match(line)
    if verdict:
        return "ordinary", verdict.group(1).upper()
    closure = CLOSURE_LINE_RE.match(line)
    if closure:
        return "closure", closure.group(1).upper()
    return "malformed", None


def reviewer_control(message):
    """One unfenced control value, however often stated, ending the reviewer result."""
    # No control line can exist without the word appearing somewhere, and the Codex lane routes
    # CLI output of any size through here on every Stop invocation — so rule that out in one
    # scan before the trailer loop and the line split copy a multi-megabyte payload.
    if not CONTROL_TOKEN_RE.search(str(message or "")):
        return "malformed", None
    lines, fenced, closed = unfenced_nonempty_lines(strip_harness_trailer(message))
    if not closed or not lines:
        return "malformed", None
    # A control line inside a fence is never the reviewer stating its verdict: either it is an
    # example being quoted, or the doubling below has made fence state meaningless — an
    # unmatched fence in the message hides one copy's control line and exposes the other's,
    # which is how an unbalanced result would otherwise read as a well-formed approval.
    if any(CONTROL_PREFIX_RE.match(line) for line in fenced):
        return "malformed", None
    # COMPAT: `codex exec` prints the reviewer's final message twice — once as it streams, then
    # again as the run's last message, with the CLI's own footer between them — so one verdict
    # reaches the transcript as two identical control lines. Counting occurrences rejected every
    # real Codex verdict as malformed and left the native subagent as the only lane that could
    # satisfy a HIGH candidate. What the rule is actually for is a result that states more than
    # one thing, so it is the stated controls that must agree: a repeated verdict is still one
    # verdict, while two different control lines stay malformed, as does a result whose last
    # line is not a control line.
    controls = {stated_control(line) for line in lines if CONTROL_PREFIX_RE.match(line)}
    if len(controls) != 1 or not CONTROL_PREFIX_RE.match(lines[-1]):
        return "malformed", None
    return controls.pop()


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
    # A lasting change seen during this candidate that no session could be shown to own is
    # recorded as a grade without a path. It keeps the candidate persistent for the same reason
    # overflow does: the marker's path list is no longer the whole story, and the operational
    # contract answers for commands, not for a file that may have been written here.
    if entry.get("path_overflow") or entry.get("unattributed_durable"):
        return cwg.WORK_PERSISTENT
    seen = entry.get("minimum_risk_seen")
    if seen in RISK_ORDER and RISK_ORDER[seen] > RISK_ORDER["LOW"]:
        return cwg.WORK_PERSISTENT
    return cwg.work_class(marker_paths(entry))


def anomaly_closure(receipt, state, key):
    """Whether an anomaly receipt is backed by a report this session filed after its last block.

    The report is evidence for whoever maintains the gate, never a key that opens it: the
    candidate closes UNVERIFIED, the receipt is available only once the hook has spoken, and the
    report has to be newer than that block and quote its reason, so the two sides of the
    disagreement are on record together.
    """
    match = ANOMALY_REASON_RE.match(receipt[1])
    if not match:
        return False, "anomaly-reported needs `<report id>; <the verifiable contradiction>`"
    if int(state.get("blocks") or 0) < 1:
        return False, "anomaly-reported is only available after the gate has blocked this candidate"
    try:
        import gate_inbox
        report = gate_inbox.find_report(match.group(1), key)
    except Exception:
        report = None
    if report is None:
        return False, "anomaly-reported names no report filed by this session (gate_inbox.py report)"
    # The nonce is minted at the block and printed only in its text, so a report carrying it
    # was written after the hook spoke, whatever timestamp the record claims; the timestamp is
    # still required to be a real one.
    nonce = str(state.get("block_nonce") or "")
    if not nonce or report.get("block_nonce") != nonce:
        return False, "the anomaly report does not carry this block's nonce (copy the command from the block text)"
    filed = report.get("ts")
    if not isinstance(filed, (int, float)) or not math.isfinite(filed) or filed > time.time() + 300:
        return False, "the anomaly report carries no real timestamp"
    if filed < float(state.get("last_block_ts") or 0):
        return False, "the anomaly report predates the last block"
    last = normalized(state.get("last_block_reason"))
    if not last or last not in normalized(report.get("block_reason")):
        return False, "the anomaly report does not quote the last block's reason"
    return True, match.group(1)


def restored_to_head(entry):
    """Whether the repository shows nothing lasting changed since the candidate opened."""
    return not restoration_blocker(entry)


# One answer per marker within a Stop run, which `main` switches on: the preflight and the block
# text ask the same question, and each asking costs git calls against the hook's own timeout.
_RESTORATION = {"memo": None}


def restoration_blocker(entry):
    """Why the repository does not show the candidate undone, or "" when it does.

    Nothing is missing only when the marker remembers the commit and the refs the cycle opened
    on and names every lasting path it touched, HEAD is that commit again, every ref but the
    branches points where it did (tags, the stash, notes), no commit made here since the opening
    survives on a branch or a remote-tracking ref
    (`kept_commit`; branches other sessions move and fetches are not the candidate's), the
    repository ignores case so the lower-cased paths can be
    checked against its ignore patterns, none of them is gitignored (git could not see a change
    to it), and `git status` shows none of those paths. Files the candidate never touched — test
    output, another session's work — do not keep it open (report fb6a9be6), but once the
    candidate ran a command the snapshots could not resolve, its paths are not the whole of what
    it may have changed and the whole tree has to be clean. A lasting path outside the
    repository is not something git can vouch for. Every git call shares one small budget and
    any failure keeps the candidate open.
    """
    memo = _RESTORATION["memo"]
    if memo is None:
        return find_restoration_blocker(entry)
    key = json.dumps([entry.get(field) for field in (
        "identity", "head_at_start", "refs_at_start", "path_overflow", "paths", "last_path",
        "unattributed_durable", "first_ts", "opened_at",
    )], sort_keys=True, default=str)
    if key not in memo:
        memo[key] = find_restoration_blocker(entry)
    return memo[key]


def find_restoration_blocker(entry):
    import code_work_gate_mark as mark
    root = cwg.identity_root(entry.get("identity")).rstrip("/")
    start, refs = entry.get("head_at_start"), entry.get("refs_at_start")
    if not root or not isinstance(start, str) or not start or not isinstance(refs, str) or not refs:
        return "the marker records no repository, commit and refs the candidate opened on"
    # Past the path cap the marker no longer names every lasting path, so the ignore probe
    # below could not cover them all.
    if entry.get("path_overflow"):
        return "the candidate passed the path cap, so the marker no longer names every lasting path"
    durable = cwg.durable_paths(marker_paths(entry))
    outside = [path for path in durable if not mark.covers(root, path)]
    if outside:
        return "a lasting path lies outside {}, where git cannot vouch for it ({})".format(
            root, cwg.basename(outside[0]))
    deadline = time.monotonic() + RESTORE_BUDGET
    silent = "git did not answer inside the hook's budget"

    def call(arguments, cap, stdin=None):
        remaining = deadline - time.monotonic()
        if remaining < 0.25:
            return None
        return cwg.git_run(root, arguments, timeout=min(cap, remaining), stdin=stdin)

    head = call(["rev-parse", "HEAD"], 1.5)
    if not head or head[0] != 0:
        return silent
    if head[1].strip() != start:
        return "HEAD is {}, the candidate opened on {}".format(head[1].strip()[:12], start[:12])
    listing = call(["for-each-ref", "--format=%(refname) %(objectname)"], 1.5)
    if not listing or listing[0] != 0:
        return silent
    # A marker opened before branches were left out holds the digest of every local ref, and one
    # opened before remote-tracking refs were, of every ref.
    views = (cwg.pinned_refs, cwg.local_refs, lambda text: text)
    if not any(hashlib.sha256(view(listing[1]).encode("utf-8", "replace")).hexdigest() == refs
               for view in views):
        return "a ref other than a branch moved since the candidate opened (a tag, the stash, a note)"
    # A marker written before `opened_at` existed has only the later `first_ts`.
    kept = kept_commit(call, start, float(entry.get("opened_at") or entry.get("first_ts") or 0.0))
    if kept != "":
        return silent if kept is None else kept
    if durable:
        # Where the marker's paths are lower-cased (Windows), `check-ignore` matches them against
        # the patterns case-insensitively only while the repository ignores case, so any other
        # setting leaves the answer unknown. Elsewhere the paths keep their case.
        if cwg.CASE_FOLDED_PATHS:
            ignorecase = call(["config", "--type=bool", "core.ignorecase"], 1.0)
            if not ignorecase or ignorecase[0] != 0 or ignorecase[1].strip() != "true":
                return "the repository does not ignore case, so its ignore rules cannot be checked"
        # `-q` takes one path: with several git exits 128, which read as "ignored" kept every
        # candidate of more than one file open (report f82c87c7).
        ignored = call(["check-ignore", "--stdin", "-z"], 1.5, stdin="".join(path + "\0" for path in durable))
        # Exit 0: the paths it prints are ignored; 1: none is; anything else, or a hang: unknown.
        if not ignored:
            return silent
        if ignored[0] == 0:
            named = [name for name in ignored[1].split("\0") if name]
            return "a lasting path is gitignored, so git cannot vouch for it ({})".format(
                cwg.basename(cwg.normalize_path(named[0])) if named else "?")
        if ignored[0] != 1:
            return "git could not tell whether a lasting path is gitignored"
    status = call(["status", "--porcelain", "-z", "--untracked-files=all"], 2.5)
    if not status or status[0] != 0:
        return silent
    dirty = porcelain_paths(root, status[1])
    if not dirty:
        return ""
    if cwg.SHELL_MUTATION_PATH in marker_paths(entry) or entry.get("unattributed_durable"):
        return "the working tree is not clean, and the candidate ran a command the gate could not resolve"
    touched = sorted(dirty & set(durable))
    if touched:
        return "a path the candidate changed still differs from HEAD ({})".format(cwg.basename(touched[0]))
    return ""


REFLOG_TIME_RE = re.compile(r"@\{(\d+)\}")
# Reflog subjects of the operations that create a commit here. A checkout, a reset, a rebase's
# start or abort and a fast-forward only move HEAD onto a commit that already exists, fetched ones
# included, and counting those blocked an aborted rebase probe (G12 review, F5).
MADE_HERE_RE = re.compile(
    r"^(?:commit(?: \((?:amend|initial|merge)\))?|cherry-pick|revert|am"
    r"|rebase(?: -i)? \((?:pick|continue|reword|squash|fixup|edit)\)"
    r"|(?:merge|pull)\b[^:]*: Merge made)"
)
# Past this many commits made since the opening the check gives up and keeps the candidate open.
PUSH_CHECK_CAP = 50


def kept_commit(call, start, since):
    """Why a commit made here while the candidate was open outlives the undo, "" when none does,
    or None when git could not tell.

    HEAD back on its opening commit does not undo a commit left on another branch, or pushed and
    then reset away or left on a deleted branch: a local branch or a remote-tracking ref still
    holds it (G12 review, F1). The worktree's own HEAD reflog records each commit made
    here — a commit, a merge, a cherry-pick, a rebase's picks — and nothing another worktree of
    the repository does, so those made after `since` (when the command that opened the candidate
    started) must be on no local branch and no remote-tracking ref; a tag holding one has moved
    the digest checked before this. Branches other sessions move and fetched commits are never
    made here, which is why the digest leaves both out (report 2b8bbfb1). A repository whose
    HEAD reflog is off or expired shows nothing, and then nothing is checked.
    """
    reflog = call(["reflog", "show", "--date=unix", "--format=%H %gd %gs", "HEAD"], 1.5)
    if not reflog or reflog[0] != 0:
        return None
    made = []
    for line in reflog[1].splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        commit, selector, subject = parts
        stamp = REFLOG_TIME_RE.search(selector)
        if (stamp and int(stamp.group(1)) + 1 >= since and MADE_HERE_RE.match(subject)
                and commit != start and commit not in made):
            made.append(commit)
    if len(made) > PUSH_CHECK_CAP:
        return "more than {} commits were made since the candidate opened to check where they went".format(
            PUSH_CHECK_CAP)
    if not made:
        return ""
    # The reflog window reaches a second back, and a commit the opening already stood on is none
    # of the candidate's work, however recent: every branch holding the start holds it too.
    fresh = call(["rev-list"] + made + ["--not", start], 1.5)
    if not fresh or fresh[0] != 0:
        return None
    listed = set(fresh[1].split())
    made = [commit for commit in made if commit in listed]
    if not made:
        return ""
    holders = call(["for-each-ref", "--format=%(refname)"] + ["--contains=" + commit for commit in made]
                   + ["refs/heads/", "refs/remotes/"], 1.5)
    if not holders or holders[0] != 0:
        return None
    names = holders[1].split()
    local = [name for name in names if not name.startswith("refs/remotes/")]
    if local:
        return "a commit made here while the candidate was open is on {}".format(local[0])
    # A remote's `HEAD` only points at one of its branches, which names the push better.
    names = [name for name in names if not name.endswith("/HEAD")] or names
    return "a commit made while the candidate was open was pushed ({})".format(names[0]) if names else ""


def porcelain_paths(root, output):
    """The normalized absolute paths `git status --porcelain -z` lists, a rename's source included."""
    entries = output.split("\0")
    paths, index = set(), 0
    while index < len(entries):
        item = entries[index]
        index += 1
        if len(item) < 4:
            continue
        paths.add(cwg.normalize_path(root + "/" + item[3:]))
        # Either status column can report a rename or a copy, whose source is the next field.
        if ("R" in item[:2] or "C" in item[:2]) and index < len(entries):
            paths.add(cwg.normalize_path(root + "/" + entries[index]))
            index += 1
    return paths


def candidate_floor(entry):
    """The lowest risk this candidate may close at: its recorded paths and every floor seen."""
    return cwg.max_risk(minimum_risk(marker_paths(entry)), entry.get("minimum_risk_seen"))


def receipt_preflight(receipt, entry):
    """Reject malformed, misclassified, or path-risk-downgraded receipts before scanning.

    An operational candidate may also close as `verified`: a lasting change the snapshots cannot
    see — prose written through the shell into an unwatched file — is still one, and its receipt
    is judged at the risk it declares (report c07fc211). That is no cheaper than `no-change`.
    """
    if receipt is None:
        return False, "terminal receipt is missing or malformed"
    kind, _, risk = receipt
    if candidate_class(entry) == cwg.WORK_OPERATIONAL:
        if kind not in OPERATIONAL_RECEIPTS and kind != "verified":
            return False, (
                "this candidate changed no lasting artifact the gate could see and ends with "
                "[gate] operational: <pre-execution check>; <verified effect>, "
                "[gate] no-change: <reason>, or, for a lasting change made where the gate "
                "cannot see it, [gate] verified: <risk>; <candidate and checks>"
            )
        return True, "preflight"
    if kind in OPERATIONAL_RECEIPTS:
        if not restoration_blocker(entry):
            # A rebase probe aborted, an edit undone: the repository is back on the commit
            # the candidate opened on and clean, so nothing lasting changed after all.
            return True, "preflight"
        return False, (
            "{} cannot close a candidate that changed a lasting artifact (the repository "
            "still differs from the commit the candidate opened on)".format(kind)
        )
    required_risk = candidate_floor(entry)
    effective_risk = risk or "HIGH"
    if RISK_ORDER[effective_risk] < RISK_ORDER[required_risk]:
        return False, "declared risk {} is below path-based minimum {}".format(
            effective_risk, required_risk
        )
    return True, "preflight"


def latest(events):
    return max(events, default=(0.0, None), key=lambda item: item[0])


def unknown_mark(mark):
    """Whether a content mark is a barrier: an unattributed change, or an unmeasurable one.

    COMPAT: markers written before the flag existed encoded a barrier as a missing fingerprint,
    so a mark without a string `fp` still reads as one.
    """
    return bool(mark.get("unknown")) or not isinstance(mark.get("fp"), str)


def content_at(entry, stamp):
    """The fingerprint the candidate's lasting paths had at this moment, or None when unknown."""
    current = None
    for mark in entry.get("content_marks") or []:
        if not isinstance(mark, dict) or not cwg.valid_ts(mark.get("ts")):
            continue
        if float(mark["ts"]) <= stamp:
            current = mark.get("fp")
        else:
            break
    return current if isinstance(current, str) else None


def covered_contents(entry, stamp):
    """The contents a verdict stated at this moment covers: what the candidate held then, and each
    content a clean upstream merge made of a covered one afterwards (report f9920b99).

    The marker writes such a mark (`merge`) only when every byte the command changed is what
    `git merge-tree` computed from the HEAD before it and an upstream commit, which is the clean
    merge a verdict needs nothing for.
    """
    then = content_at(entry, stamp)
    covered = {then} if then else set()
    for mark in entry.get("content_marks") or []:
        if (
            isinstance(mark, dict)
            and cwg.valid_ts(mark.get("ts"))
            and float(mark["ts"]) > stamp
            and mark.get("merge") in covered
            and isinstance(mark.get("fp"), str)
        ):
            covered.add(mark["fp"])
    return covered


UNMEASURED_REASON = "unmeasured-change"
# Seconds of the Stop hook's ten the catch-up measurement may spend before it gives up for this stop.
CATCH_UP_BUDGET = 3.0


def unmeasured_change(entry, now):
    """`(marker brought up to date, the mark it added or None)`, or None when nothing is known.

    A marker hook cancelled at its timeout records nothing, so an edit made then left no mark: the
    last approval still read as current, and the delta round the session honestly ran after the fix
    read as review continued after a terminal APPROVED (report 8db8b3d2). A command that moves HEAD
    over committed files leaves no mark either unless it is a clean integration of upstream, which
    the marker carries itself (report c4c78b99): the snapshots list only what differs from HEAD, and
    a checkout of another branch must not open a candidate of its files. The marker keeps the size
    and modification time of its lasting paths and when they last matched (`content_stats_at`, the
    last measurement or stop that found them unchanged). When they differ now and the content does
    too, a mark is added and the freshness anchor moves to it, so a verdict from before the change is
    stale and one from after it covers. It goes at the latest modification time among the changed
    files when every one of them was modified after the stats last matched; a file that is gone, or
    one whose time claims a moment the stats still matched (a copy that kept its source's time),
    puts it at the moment noticed. What stays open: a kept time that falls between the last match
    and a verdict, a lasting file outside `content_paths`, and a change undone before any stop.
    Past the path cap the marker no longer holds the domain it measured, so nothing is compared.
    """
    stored = entry.get("content_stats")
    paths = entry.get("content_paths") or []
    marks = [item for item in entry.get("content_marks") or []
             if isinstance(item, dict) and cwg.valid_ts(item.get("ts"))]
    if not isinstance(stored, dict) or not marks:
        return None
    import code_work_gate_mark as mark
    if len(paths) >= mark.MARKER_PATH_CAP:
        return None
    current = mark.content_stats(paths)
    if current == stored:
        return dict(entry, content_stats_at=now), None
    fingerprint = mark.content_fingerprint(paths, deadline=time.monotonic() + CATCH_UP_BUDGET)
    if fingerprint is None:
        # Nothing provable now; the next stop asks again.
        return None
    updated = dict(entry, content_stats=current, content_stats_at=now)
    if fingerprint == marks[-1].get("fp"):
        # Touched, not changed.
        return updated, None
    matched = entry.get("content_stats_at")
    matched = float(matched) if cwg.valid_ts(matched) else float(marks[-1]["ts"])
    moved = [path for path in current if current[path] != stored.get(path)]
    times = [current[path][1] / 1e9 for path in moved if current[path]]
    trusted = bool(times) and len(times) == len(moved) and min(times) > matched
    anchor = min(max(max(times) if trusted else now, float(marks[-1]["ts"]) + 0.001), now)
    updated["content_marks"] = mark.content_marks_after(
        entry.get("content_marks"), anchor, fingerprint, cause={"reason": UNMEASURED_REASON}
    )
    updated["last_durable_ts"] = max(float(entry.get("last_durable_ts") or 0.0), anchor)
    return updated, updated["content_marks"][-1]


def content_covers(entry, stamp, durable_ts):
    """Whether evidence stated at this moment still describes the candidate on disk.

    Freshness is measured against content, not against edit events: a verdict given before an
    edit that was later reverted covers exactly the bytes the reviewer read. Without content
    marks — a marker written before they existed — the strict rule stands: nothing older than
    the last durable change covers it. A change the snapshot could not attribute is a barrier
    in its own right: the fingerprint measures only the recorded paths, so an equal fingerprint
    after such a change proves nothing about what it touched, and only a fresh verdict crosses
    it. Before a verdict such a change is no barrier at all — the reviewer read the state it
    left behind — so the mark keeps its measurement and still serves as the baseline. Recording
    no measurement there erased the baseline outright, and the next named edit (a `git add` of
    the very bytes the reviewer read) then retired a verdict nothing had invalidated.
    """
    if stamp >= durable_ts:
        return True
    marks = [
        mark for mark in entry.get("content_marks") or []
        if isinstance(mark, dict) and cwg.valid_ts(mark.get("ts"))
    ]
    if not marks:
        return False
    now_fp = content_at(entry, float("inf"))
    if now_fp is None or now_fp not in covered_contents(entry, stamp):
        return False
    return not any(
        float(mark["ts"]) > stamp and unknown_mark(mark) for mark in marks
    )


def active_review_start(evidence, stale):
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
        if verdict == "APPROVED" and stale(stamp):
            start = max(start, stamp)
    return start


def round_verdicts(reviews):
    """The ordinary reviews in filing order, an ESCALATE before round 3 read as that round's REVISE.

    ESCALATE ends the review only as round 3's verdict. An earlier one — a packet that numbered its
    rounds across the candidate this one replaced, a reviewer that escalated early — left no legal
    move: every closure asked for a round-3 ESCALATE, and the next round read as review after a
    terminal verdict (report 6d8e2c4c). It stays what it reports, a round with open blockers, and
    the review goes on within its budget; a closure packet sent after it is review activity, as
    before any round-3 ESCALATE (report 77226dfa). Rounds count from the last APPROVED, where a
    new sequence starts.
    """
    read, rounds = [], 0
    for stamp, verdict in sorted(reviews):
        rounds += 1
        if verdict == "ESCALATE" and rounds < MAX_REVIEW_ROUNDS:
            verdict = "REVISE"
        read.append((stamp, verdict))
        if verdict == "APPROVED":
            rounds = 0
    return read


def simplify_missing(risk, state):
    """The simplify lanes this risk still needs a current foreground result from.

    A lens's XHIGH run is the same concern on a stronger model, so it stands in for that lens at
    HIGH and in the STANDARD trio; a HIGH lens never stands in for an XHIGH one.
    """
    def current(lane):
        return state.get(lane) == "current"

    if risk == "XHIGH":
        return [lens for lens in XHIGH_LENSES if not current(lens)]
    uncovered = [lens for lens, stronger in zip(SIMPLIFY_LENSES, XHIGH_LENSES)
                 if not (current(lens) or current(stronger))]
    if risk == "HIGH":
        return uncovered
    if risk == "STANDARD":
        return [] if current(SIMPLIFY_LANE) or not uncovered else [SIMPLIFY_LANE]
    return []


def simplify_block(risk, missing, state):
    lenses = ", ".join(SIMPLIFY_LENSES)
    needed = {
        "XHIGH": "one result from each of {}".format(", ".join(XHIGH_LENSES)),
        "HIGH": "one result from each of {} (or its XHIGH run)".format(lenses),
    }.get(risk, "one {} result or the complete trio ({})".format(SIMPLIFY_LANE, lenses))
    failed = [lane for lane in missing if state.get(lane) in ("failed", "exhausted")]
    return (
        "simplify lenses have no foreground result: a {} candidate needs {} for this candidate "
        "(missing: {}){}"
    ).format(
        risk, needed, ", ".join(missing),
        " (last attempt failed: {})".format(", ".join(failed)) if failed else "",
    )


def evaluate_receipt(receipt, entry, evidence):
    """Precondition: `receipt` already passed `receipt_preflight` against this marker."""
    kind, _, risk = receipt
    first_ts = float(entry.get("first_ts") or 0.0)
    last_ts = float(entry.get("last_ts") or first_ts)

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
    # rewritten throwaway script leaves the reviewed diff untouched. A candidate with no recorded
    # lasting change — one whose change no snapshot can see, closed as `verified` — is judged by its
    # last mark that could have written (`last_write_ts`), which bookkeeping and readers after the
    # approval leave where it was (report cf223da5). A marker without it — opened before it existed,
    # or holding only such marks — falls back to the strict whole-cycle timestamp.
    durable_ts = float(entry.get("last_durable_ts") or 0.0)
    if not cwg.valid_ts(durable_ts):
        written = entry.get("last_write_ts")
        durable_ts = float(written) if cwg.valid_ts(written) else last_ts

    def current(stamp):
        return content_covers(entry, stamp, durable_ts)

    review_start = active_review_start(evidence, lambda stamp: not current(stamp))
    # Sorted by the moment each verdict is filed at: a background lane's verdict is filed at
    # its launch once its notification is read, after every result that returned in between.
    rounds = round_verdicts(evidence["ordinary_reviews"])
    ordinary_reviews = [item for item in rounds if item[0] > review_start]
    ordinary_verdicts = [verdict for _, verdict in ordinary_reviews]
    # A closure validation exists only after the round-3 ESCALATE whose recovery it checks. A
    # closure packet sent before one — round 3 ended REVISE, the packet offered the wrong
    # shape — is a reviewer result of the wrong kind: it stays review activity, which retires
    # an earlier approval, but it is neither terminal nor the start of a closure phase.
    # Counting it as one left the candidate no legal move: the block that refused the closure
    # named an ordinary APPROVED as the remedy, and the next block called that approval
    # activity after a terminal READY (report 77226dfa).
    escalated_at = max(
        (stamp for stamp, verdict in ordinary_reviews if verdict == "ESCALATE"), default=None
    )
    phase_reviews = sorted(
        item for item in evidence["closure_reviews"]
        if escalated_at is not None and item[0] > escalated_at
    )
    # A READY that a later lasting change or barrier made stale retires with the passes before it,
    # as a stale APPROVED retires its rounds: the block asks for a fresh closure validation, which
    # read as validation after a terminal READY and left no legal receipt (report 27c9dcd0). Only a
    # READY inside the pass budget retires, so a pass past the cap cannot reset the cap, and none
    # after a READY that still covers the candidate: that one stays terminal.
    retired, passes = None, 0
    for stamp, verdict in phase_reviews:
        passes += 1
        if verdict != "READY":
            continue
        if current(stamp):
            break
        if passes <= MAX_CLOSURE_PASSES:
            retired, passes = stamp, 0
    closure_reviews = [item for item in phase_reviews if retired is None or item[0] > retired]
    closure_verdicts = [verdict for _, verdict in closure_reviews]

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

    # A closure receipt states no risk; the candidate's own floor decides which lanes it owes.
    effective_risk = risk or candidate_floor(entry)

    # Keying the pass on a preceding Skill call enforced an order rather than the work; the lane
    # results themselves are the evidence, in whatever order they ran. The per-lane pass cap is
    # checked for every lane regardless of risk, so a ritual repeat is caught even where no pass
    # was required.
    simplify_state = {}
    for reviewer in sorted(SIMPLIFY_REVIEWERS):
        # Both lists are already candidate-bound by transcript_evidence.
        successes = evidence["simplify_successes"].get(reviewer, [])
        failures = sorted(evidence["simplify_failures"].get(reviewer, []))
        if len(successes) > MAX_SIMPLIFY_PASSES:
            return False, "simplify exceeded the absolute {}-pass cap for {}".format(
                MAX_SIMPLIFY_PASSES, reviewer
            )
        latest_success = max(successes, default=0.0)
        if latest_success and latest_success > (failures[-1] if failures else 0.0):
            simplify_state[reviewer] = "current"
            continue
        retries = [stamp for stamp in failures if stamp > latest_success]
        if len(retries) >= 2 and retries[-1] >= durable_ts:
            simplify_state[reviewer] = "exhausted"
        elif retries:
            simplify_state[reviewer] = "failed"
    missing_simplify = simplify_missing(effective_risk, simplify_state)
    simplify_unavailable = False
    if missing_simplify:
        if kind == "draft-blocked" and any(
            simplify_state.get(lane) == "exhausted" for lane in missing_simplify
        ):
            simplify_unavailable = True
        else:
            return False, simplify_block(effective_risk, missing_simplify, simplify_state)

    ordinary_ts, ordinary_verdict = latest(ordinary_reviews)
    closure_ts, closure_verdict = latest(closure_reviews)
    failed_ts = max(evidence["review_failures"], default=0.0)
    current_failure = current(failed_ts)
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
        # A background call is observable once its notification was judged, in either
        # direction; only a launch nothing ever reported on is not.
        current_external = (
            (foreground or call_id in evidence.get("background_judged", ()))
            and current(call_ts)
            and current(result_ts)
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
        if any(verdict == "ESCALATE" for _, verdict in rounds):
            return False, "ESCALATE requires autonomous closure, not verified"
        if ordinary_verdicts and ordinary_verdict != "APPROVED":
            return False, "an invoked review has no terminal APPROVED verdict"
        if risk in STALE_REASONS and not (
            ordinary_verdict == "APPROVED" and current(ordinary_ts)
        ):
            return False, STALE_REASONS[risk]
        if risk == "XHIGH" and (ordinary_ts, "APPROVED") not in evidence["xhigh_verdicts"]:
            return False, XHIGH_LANE_REASON
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
            return False, "{} {}".format(kind, ESCALATE_REQUIRED)

    if kind == "pr-ready":
        if not (closure_verdict == "READY" and current(closure_ts)):
            if evidence["closure_reviews"] and not phase_reviews:
                return False, (
                    "pr-ready lacks a CLOSURE_VALIDATION: READY after round-3 ESCALATE "
                    "(a closure packet sent before the ESCALATE is not a closure validation)"
                )
            return False, "pr-ready lacks current CLOSURE_VALIDATION: READY"
        return True, "pr-ready"

    if closure_verdict == "READY":
        return False, "draft-blocked conflicts with CLOSURE_VALIDATION: READY"
    if not (
        (closure_verdict == "BLOCKED" and current(closure_ts))
        or (
            current_failure
            and (ordinary_verdict != "ESCALATE" or failed_ts > ordinary_ts)
        )
        or simplify_unavailable
        or required_external_unavailable
    ):
        return False, "draft-blocked lacks current BLOCKED or unavailable evidence"
    return True, "draft-blocked"


# The receipts that close a candidate on evidence; `anomaly-reported`, `draft-blocked` and an
# exhausted block budget end it UNVERIFIED, so its bytes are not recorded as closed.
VOUCHING_RECEIPTS = frozenset(("verified", "operational", "no-change", "pr-ready"))


def close_cycle(marker, state_file, state, candidate_ts, receipt, session_key_):
    """Retire the candidate, then the block state, then sweep: the order a cancelled hook survives.

    The harness kills a hook at its timeout wherever it is. Resetting the block count first and
    retiring the marker last left, for a hook killed in between, an open candidate with no block
    on record, and the next stop refused the very anomaly receipt this one had accepted (report
    267de208, a Stop hook cancelled at 11 s under load). Killed before the marker moves, nothing
    has changed and the same receipt closes it next time; killed after, the candidate is closed,
    and a block state left behind only counts against a candidate that no longer exists.
    """
    now = time.time()
    current = cwg.read_json(marker)
    try:
        import code_work_gate_mark as mark
    except Exception:
        # Only the import is forgiven, and every way it can fail: the marker is edited in this
        # very repository, so a half-written file raises SyntaxError rather than ImportError,
        # and letting that escape would report a close that already happened as a hook failure,
        # with no `close` line in the ledger. The sweep itself stays outside: a fault in it is a
        # defect and must surface.
        mark = None
    whole = current is None or current.get("last_ts") == candidate_ts
    try:
        closed_content = closed_content_of(current, mark, receipt[0]) if whole else None
    except Exception:
        # The record is optional and the close is not: an unmeasurable candidate vouches for nothing.
        closed_content = {}
    if whole:
        retired = cwg.remove(marker) or cwg.write_json(marker, dict(current or {}, closed=True))
    else:
        current["first_ts"] = current.get("last_ts") or now
        retired = cwg.write_json(marker, current)
    if not retired:
        return False
    state.update({
        "candidate_ts": candidate_ts,
        "blocks": 0,
        "closed_at": now,
        "receipt": "{}: {}".format(receipt[0], receipt[1]),
    })
    if closed_content is not None:
        state["closed_content"] = closed_content
    cwg.write_json(state_file, state)

    # The attribution registry outlives no candidate: what this session announced is only ever
    # read by a command running at the same time, and the cycle it belonged to is over. The
    # packet captures do outlive it. A background lane is launched under one candidate and
    # notifies whenever it finishes, which in a long session is often after that candidate has
    # closed; a capture discarded with the cycle left its verdict bound to nothing, forever. So
    # a capture is dropped only by its own day-long expiry, which this closing is an occasion to
    # apply — otherwise nothing sweeps a session that launches no further review.
    cwg.retire_claims(session_key_)
    if mark is not None:
        mark.forget_stale_captures(session_key_)
    return True


def closed_content_of(entry, mark, kind):
    """What the closing candidate's lasting files held, for the marker to recognise them later:
    `{"paths", "fp"}`; None when the candidate had none, which leaves the previous record
    standing; `{}` when a receipt of `kind` vouches for nothing (an UNVERIFIED close) or the files
    cannot be measured (past the path cap, too large, too slow, the marker module unavailable),
    which clears it, since such a candidate may have moved the earlier one's files and the old
    record would then vouch for a combination nobody closed. After the receipt, committing,
    rebasing or pushing the recorded bytes is no new work, and opening a candidate on it asked for
    lanes on content already closed (report 9dbbfe70)."""
    lasting = sorted(set(cwg.durable_paths(cwg.marker_paths(entry or {}))))
    if not lasting:
        return None
    if kind not in VOUCHING_RECEIPTS or mark is None or (entry or {}).get("path_overflow"):
        return {}
    fingerprint = mark.content_fingerprint(lasting, deadline=time.monotonic() + CATCH_UP_BUDGET)
    return {"paths": lasting, "fp": fingerprint} if fingerprint else {}


HIGH_STALE_REASON = "HIGH candidate lacks a current APPROVED verdict"
STALE_REASONS = {
    "HIGH": HIGH_STALE_REASON,
    "XHIGH": "XHIGH candidate lacks a current APPROVED verdict",
}
XHIGH_LANE_REASON = (
    "XHIGH candidate's current APPROVED verdict did not come from an XHIGH lane ({} in the "
    "native lane, or a Codex turn on {} at {} effort)"
).format(cwg.XHIGH_REVIEWER, cwg.XHIGH_CODEX_MODEL, cwg.XHIGH_CODEX_EFFORT)
ROUND_REASONS = (
    "an invoked review has no terminal APPROVED verdict",
    "ordinary review exceeded",
    "ordinary review continued after terminal",
    "review activity continued after terminal",
)
# The tail of the block a `pr-ready` or `draft-blocked` receipt gets without a round-3 ESCALATE.
ESCALATE_REQUIRED = "requires round-3 ESCALATE or exhausted required evidence"
DETAIL_LIMIT = 600


def block_detail(reason, entry, evidence):
    """What a block rests on, in terms the session can check against its own transcript.

    The ledger held all of it before, and sessions filed anomaly reports for want of it: when
    the evidence starts counting and which open candidate that moment replaced, what stands
    between an approval and now, which rounds were read, and what keeps a repository from
    reading as restored (reports a269a6fc, 4b840373, 0e4aedc8, 32a3582f). A closure receipt refused
    for want of a round-3 ESCALATE shows the rounds too, and why the latest review result gave no
    verdict when the scan knows: a third round launched in a shape nothing could be bound from was
    missing, and the block said nothing of it (report 926b670b).
    """
    import code_work_gate_mark as mark
    notes = []
    rounds = reason.startswith(ROUND_REASONS) or ESCALATE_REQUIRED in reason
    stale = reason in STALE_REASONS.values()
    below_xhigh = reason == XHIGH_LANE_REASON
    if stale or below_xhigh or rounds or reason.startswith("simplify"):
        first_ts = entry.get("first_ts")
        if cwg.valid_ts(first_ts):
            note = "evidence for this candidate counts from {}".format(mark.clock(first_ts))
            displaced = entry.get("displaced")
            if isinstance(displaced, dict) and cwg.valid_ts(displaced.get("opened")):
                note += (
                    ", when it replaced the candidate open since {} ({}); lane results and review "
                    "rounds from before belong to that one"
                ).format(mark.clock(displaced["opened"]), mark.displacement_cause(displaced))
            notes.append(note)
    if stale:
        notes.append(approval_barriers(entry, evidence, mark.clock))
    elif below_xhigh:
        notes.append(xhigh_lane_note(evidence, mark.clock))
    elif rounds:
        notes.append(rounds_read(evidence, mark.clock))
    elif "cannot close a candidate that changed a lasting artifact" in reason:
        notes.append(restoration_blocker(entry))
    detail = "; ".join(note for note in notes if note)
    return detail if len(detail) <= DETAIL_LIMIT else detail[:DETAIL_LIMIT - 1] + "…"


def last_approved(evidence):
    """When the latest ordinary APPROVED was filed, or None when none was read."""
    return max((stamp for stamp, verdict in evidence.get("ordinary_reviews") or []
                if verdict == "APPROVED"), default=None)


def xhigh_lane_note(evidence, clock):
    """Which approval an XHIGH receipt met, and what the XHIGH lanes themselves stated."""
    approved = last_approved(evidence)
    stated = ", ".join("{} at {}".format(verdict, clock(stamp))
                       for stamp, verdict in sorted(evidence.get("xhigh_verdicts") or []))
    return "the APPROVED filed at {} came from a lane below XHIGH; XHIGH lanes stated {}".format(
        clock(approved) if approved is not None else "?", stated or "nothing")


def approval_barriers(entry, evidence, clock):
    """What stands between the last APPROVED and the candidate as it is now."""
    approved = last_approved(evidence)
    if approved is None:
        return "no APPROVED verdict was read for this candidate" + unbound_note(evidence, clock)
    marks = [mark for mark in entry.get("content_marks") or []
             if isinstance(mark, dict) and cwg.valid_ts(mark.get("ts")) and float(mark["ts"]) > approved]
    notes = [describe_mark(mark, clock) for mark in marks if unknown_mark(mark)]
    covered = covered_contents(entry, approved)
    if not covered:
        notes.append("no measurement of the lasting content comes before it")
    elif content_at(entry, float("inf")) not in covered:
        changed = [mark for mark in marks if not unknown_mark(mark) and mark.get("fp") not in covered]
        unmeasured = changed and (changed[-1].get("cause") or {}).get("reason") == UNMEASURED_REASON
        notes.append("the lasting content differs from what it approved{}".format(
            " (changed at {}{})".format(
                clock(changed[-1]["ts"]),
                ", a change no marker hook measured: a hook cancelled or timed out, a checkout, reset,"
                " merge or rebase that rewrote committed files and was no clean integration of upstream,"
                " or a write from outside this session's tools" if unmeasured else "",
            ) if changed else ""))
    if not notes:
        notes.append("the last lasting change is at {}".format(clock(entry.get("last_durable_ts"))))
    shown = "; ".join(notes[:3]) + ("; and {} more".format(len(notes) - 3) if len(notes) > 3 else "")
    return "the last APPROVED is filed at {} (for a background lane, its launch), and after it: {}".format(
        clock(approved), shown)


def describe_mark(mark, clock):
    """One barrier as the marker recorded it."""
    cause = mark.get("cause") if isinstance(mark.get("cause"), dict) else {}
    text = "{} {}".format(clock(mark["ts"]), cause.get("reason") or "a change nobody could attribute")
    if cause.get("command"):
        text += " `{}`".format(cause["command"])
    elif cause.get("tool"):
        text += " ({})".format(cause["tool"])
    if cause.get("landed"):
        text += " that ended in {}, which no snapshot covered".format(cause["landed"])
    if cause.get("skipped"):
        text += ", with {} repositories left unmeasured by the hook's time budget".format(cause["skipped"])
    if cause.get("no_snapshot"):
        text += ", with no snapshot from before it (its PreToolUse hook was cancelled or failed)"
    return text


def rounds_read(evidence, clock):
    """The ordinary rounds the scan read, and the review results it could not use."""
    rounds = sorted(evidence.get("ordinary_reviews") or [])
    text = ("ordinary rounds read: " + ", ".join(
        "{} {}".format(clock(stamp), verdict) for stamp, verdict in rounds[-6:])
    ) if rounds else "no ordinary round was read"
    early = [stamp for (stamp, verdict), (_, read) in zip(rounds, round_verdicts(rounds)) if verdict != read]
    if early:
        text += "; an ESCALATE before round 3 counts as that round's REVISE ({})".format(
            ", ".join(clock(stamp) for stamp in early[-3:]))
    unusable = sorted(stamp for stamp, kind, _ in evidence.get("review_events") or []
                      if kind not in ("ordinary", "closure"))
    if unusable:
        text += "; review results without a usable verdict at " + ", ".join(
            clock(stamp) for stamp in unusable[-4:])
    return text + unbound_note(evidence, clock)


def unbound_note(evidence, clock):
    """Why the latest review result without a usable verdict gave none — the latest by time, then by
    the order the scan filed it — when the scan recorded why for that very result (by its place in
    `review_events`); nothing otherwise, so no other result's reason is pinned on it."""
    unusable = [(stamp, place) for place, (stamp, kind, _) in enumerate(evidence.get("review_events") or [])
                if kind not in ("ordinary", "closure")]
    if not unusable:
        return ""
    stamp, place = max(unusable)
    why = dict(evidence.get("unbound_reasons") or []).get(place)
    return "; the result at {} could not be bound: {}".format(clock(stamp), why) if why else ""


def reminder(reason, block_number, operational, session_id="", repeated=False, transcript="",
             nonce="", detail=""):
    if operational:
        contract = (
            "This candidate changed no lasting artifact the gate could see: it ran commands or a "
            "throwaway script. Invoke development-verification and end with "
            "`[gate] operational: <what was established before executing>; <verified effect "
            "on the system>`, or, when nothing was modified at all, with "
            "`[gate] no-change: <reason>`, which needs no effect half. A lasting change made "
            "where the gate cannot see it, such as prose written through the shell, closes as "
            "`[gate] verified: <risk>; <candidate and checks>` with what that risk requires. No "
            "simplify pass and no adversarial review are required for operational work."
        )
    else:
        contract = (
            "Satisfy the observable evidence contract. development-verification must have been "
            "invoked once in this session, plus honest candidate-bound checks. Simplify evidence "
            "is a foreground lane result for this candidate: a STANDARD candidate needs one "
            "{lane} result, a HIGH candidate one result from each of the three lenses "
            "({lenses}), launched together. The lane results are the evidence, in whatever order "
            "they ran, so do not re-run a completed pass to satisfy this; two failed attempts of "
            "a required lane end draft-blocked. HIGH "
            "completion requires one adversarial APPROVED result newer than the final edit to a "
            "lasting artifact, from the Codex lane (/adversarial-review, launched in the "
            "background; run `codex_lane.py check` first and skip straight to the native lane "
            "on a recorded outage) or the native reviewer (/adversarial-review-internal, in "
            "the foreground or launched in the background and judged at its notification). "
            "An XHIGH candidate needs the same from the XHIGH profiles: {xlenses}, and an "
            "APPROVED from {xreviewer} or from a Codex round on {xmodel} at {xeffort} effort. "
            "ESCALATE is not terminal: continue through at most two closure validations to READY "
            "or BLOCKED."
        ).format(lane=SIMPLIFY_LANE, lenses=", ".join(SIMPLIFY_LENSES),
                 xlenses=", ".join(XHIGH_LENSES), xreviewer=cwg.XHIGH_REVIEWER,
                 xmodel=cwg.XHIGH_CODEX_MODEL, xeffort=cwg.XHIGH_CODEX_EFFORT)
    inbox = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate_inbox.py")
    return (
        "[Code Work Gate] Cannot finalize this candidate: {reason}.\n{detail}{contract}\n"
        "A turn may end while this session's own background task (a review, a test run, a "
        "server) is still running: the completion notification resumes the work, so wait for "
        "it instead of polling. "
        "This is finite enforcement block {n}/{cap} for the unchanged candidate.{repeat} "
        "If this block contradicts facts you can verify in the transcript — the evidence exists "
        "in the shape required, or the hook asks to repeat a lane that already ran — do not "
        "re-run lanes or poll: file `python \"{inbox}\" report --session {sid}{transcript} "
        "--nonce {nonce} --block "
        "\"{reason}\" --facts \"<what the transcript shows>\" --did \"<what you did instead>\"` "
        "and end with `[gate] anomaly-reported: <id>; <fact>`, which closes this candidate "
        "UNVERIFIED with the report attached (development-verification section 10)."
    ).format(reason=reason, contract=contract, n=block_number, cap=MAX_BLOCKS_PER_CANDIDATE,
             detail="What the hook read: {}.\n".format(detail) if detail else "",
             repeat=" Same reason as the previous block." if repeated else "",
             inbox=inbox, sid=session_id or "<session id>", nonce=nonce or "<nonce>",
             transcript=' --transcript \"{}\"'.format(transcript) if transcript else "")


def waiting_note(running, waits):
    """What the parent sees when a stop is allowed only because background work is running."""
    kinds = {"shell": "shell command", "agent": "background agent", "workflow": "workflow"}
    named = ", ".join(
        "{} ({}{}{})".format(
            task["id"], kinds.get(task["kind"], task["kind"]),
            ", review lane" if task.get("review") else "",
            ": " + task["label"] if task.get("label") else "",
        )
        for task in running[:4]
    )
    more = "" if len(running) <= 4 else " and {} more".format(len(running) - 4)
    return (
        "[Code Work Gate] The candidate is still open, but this session's background work is "
        "running: {}{}. The turn may end now; the completion notification resumes it. Do not "
        "poll the task. When the work is done, finish with the terminal receipt. "
        "(background wait {}/{} for this candidate)"
    ).format(named, more, waits, MAX_BACKGROUND_WAITS)


def main():
    data = cwg.read_payload()
    if data is None:
        allow()
        return

    try:
        key = cwg.session_key(data.get("session_id"))
        _SESSION["key"] = key
        _RESTORATION["memo"] = {}
        marker = cwg.marker_path(key)
        state_file = cwg.state_path(key)
        if not os.path.exists(marker):
            allow()
            return

        entry = cwg.read_json(marker) or {}
        if entry.get("closed"):
            allow()
            return
        caught_up = unmeasured_change(entry, time.time())
        if caught_up is not None:
            updated, added = caught_up
            if added is not None:
                cwg.log_event("durable", session=key, reason=UNMEASURED_REASON, fp=added["fp"][:12],
                              at=added["ts"])
            # Written back only when no marker hook wrote in between, since the measurement took
            # seconds: overwriting its record would lose an edit; skipping means the next stop asks
            # again.
            latest = cwg.read_json(marker) or {}
            if all(latest.get(field) == entry.get(field) for field in ("last_ts", "edits", "content_marks")):
                cwg.write_json(marker, updated)
            entry = updated
        candidate_ts = entry.get("last_ts")
        first_ts = float(entry.get("first_ts") or candidate_ts or 0.0)
        state = cwg.read_json(state_file) or {}
        key_now = candidate_key(entry)
        if state.get("candidate_key") != key_now:
            state["candidate_key"] = key_now
            state["blocks"] = 0
            state["waits"] = 0
        state["candidate_ts"] = candidate_ts

        receipt = receipt_of(data.get("last_assistant_message"))
        if receipt and receipt[0] == "anomaly-reported":
            accepted, note = anomaly_closure(receipt, state, key)
            if accepted:
                if close_cycle(marker, state_file, state, candidate_ts, receipt, key):
                    cwg.log_event("close", session=key, receipt="anomaly-reported", report=note)
                    allow("Code Work Gate recorded terminal state: anomaly-reported "
                          "(UNVERIFIED, report {})".format(note))
                else:
                    allow("Code Work Gate could not retire its state and is failing open.")
                return
            preflight_ok, reason = False, note
        else:
            preflight_ok, reason = receipt_preflight(receipt, entry)
        # The transcript is read once whatever the receipt looked like: the same scan answers
        # whether this session still has background work running.
        evidence = transcript_evidence(
            data.get("transcript_path"), first_ts, skill_since=0.0
        )
        if preflight_ok:
            valid, reason = evaluate_receipt(receipt, entry, evidence)
        else:
            valid = False
        if valid:
            if close_cycle(marker, state_file, state, candidate_ts, receipt, key):
                cwg.log_event("close", session=key, receipt=receipt[0], risk=receipt[2])
                allow("Code Work Gate recorded terminal state: {}".format(receipt[0]))
            else:
                allow("Code Work Gate could not retire its state and is failing open.")
            return

        running = in_flight(evidence)
        waits = int(state.get("waits") or 0)
        if running and waits < MAX_BACKGROUND_WAITS:
            state["waits"] = waits + 1
            if cwg.write_json(state_file, state):
                cwg.log_event("wait", session=key, reason=reason, waits=waits + 1,
                              tasks=[task["id"] for task in running][:8])
                allow(waiting_note(running, waits + 1))
                return

        blocks = int(state.get("blocks") or 0)
        if blocks >= MAX_BLOCKS_PER_CANDIDATE:
            exhausted = ("enforcement-exhausted", reason, None)
            cwg.log_event("exhausted", session=key, reason=reason)
            note = (
                "Code Work Gate exhausted its finite block budget for this unchanged candidate. "
                "The task is ending UNVERIFIED: {}."
            ).format(reason)
            if not close_cycle(marker, state_file, state, candidate_ts, exhausted, key):
                note += " Gate state could not be retired."
            allow(note)
            return

        repeated = blocks >= 1 and state.get("last_block_reason") == reason
        state["blocks"] = blocks + 1
        state["last_block_ts"] = time.time()
        state["last_block_reason"] = reason
        state["block_nonce"] = uuid.uuid4().hex[:12]
        if not cwg.write_json(state_file, state):
            allow("Code Work Gate state is unavailable and enforcement is failing open.")
            return
        try:
            detail = block_detail(reason, entry, evidence)
        except Exception:
            # The explanation must never cost the block itself.
            detail = ""
        cwg.log_event("block", session=key, reason=reason, block=blocks + 1,
                      candidate_class=candidate_class(entry), **({"detail": detail} if detail else {}))
        emit({
            "decision": "block",
            "reason": reminder(
                reason, blocks + 1, candidate_class(entry) == cwg.WORK_OPERATIONAL,
                session_id=str(data.get("session_id") or ""), repeated=repeated,
                transcript=str(data.get("transcript_path") or ""), nonce=state["block_nonce"],
                detail=detail,
            ),
        })
    except Exception as error:
        # Failing open is the contract; failing silently is not — the ledger keeps the class
        # of the failure so the inbox scan can surface it.
        cwg.log_event("hook_error", hook="stop", error=type(error).__name__,
                      session=cwg.session_key(data.get("session_id")))
        allow()


if __name__ == "__main__":
    main()
