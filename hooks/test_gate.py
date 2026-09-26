"""Regression tests for the strict finite Code Work Gate."""
import atexit
import datetime
import glob
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import code_work_gate_common as cwg
import code_work_gate_mark as marker_hook
import code_work_gate_stop as gate
import codex_lane

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_HOOK = os.path.join(HERE, "code_work_gate_stop.py")
# Every throwaway home below is named after this process. The suite tears these trees down and
# rebuilds them between cases, so two runs sharing one name delete each other's fixtures
# mid-scenario: that is what made the Codex-evidence cases fail intermittently whenever a second
# run - a mutation sweep, a second terminal - happened to overlap this one.
RUN = "gate_{}".format(os.getpid())
# POSIX keeps the system temp directory at /tmp, which the gate reads as a drive-root temp
# directory: a file in a subdirectory there that is no repository is a throwaway, so every
# lasting-file fixture below would be graded as one. The suite's temp root moves under the home
# directory, where a temp file is an ordinary file, as under Windows' AppData\Local\Temp. The hook
# subprocesses inherit it, which also keeps their state apart from real sessions'.
POSIX_TEMP = None
# Where real sessions' throwaway files go, whatever this suite does with its own temp root below.
SYSTEM_TEMP = tempfile.gettempdir()
if os.name != "nt":
    POSIX_TEMP = os.path.join(os.path.expanduser("~"), ".cache", RUN + "_tmp")
    os.makedirs(POSIX_TEMP, exist_ok=True)
    os.environ["TMPDIR"] = POSIX_TEMP
    tempfile.tempdir = None
# Synthetic places, as Windows spells them — a drive, Git Bash's /c/… form, the user's temp
# directory — and what each is on POSIX. A case that passes a place through the file system, a
# command's `cd` or a place pattern names it with `native`, so it means the same absolute place
# on both platforms.
NATIVE_SPELLINGS = (
    ("C:/Users/in/AppData/Local/Temp", "/tmp"),
    ("/c/Users/in", "/home/in"), ("C:/Users/in", "/home/in"), ("c:/users/in", "/home/in"),
    ("/c/tmp", "/tmp"), ("C:/tmp", "/tmp"), ("c:/tmp", "/tmp"),
    ("C:/repo", "/repo"), ("c:/repo", "/repo"),
)


def native(text):
    """`text` with each synthetic Windows place spelled as this platform spells it."""
    if os.name == "nt":
        return text
    for windows, posix in NATIVE_SPELLINGS:
        text = text.replace(windows, posix)
    return text
# The hook reads the Codex CLI's rollout logs as proof a review actually ran; point both the
# suite and the hook subprocesses at a throwaway home so the developer's real one is untouched.
CODEX_HOME = os.path.join(tempfile.gettempdir(), RUN + "_codex_home")
os.environ["CODEX_HOME"] = CODEX_HOME
# The marker snapshots the agent-configuration homes on every shell call, so a suite left
# pointing at the developer's real one fails whenever anything else writes there while a
# scenario is resolving - another session, an editor, the plugin autoupdater. Redirect it the
# same way, and restore this value rather than unsetting it where a case overrides it.
CLAUDE_CONFIG_DIR = os.path.join(tempfile.gettempdir(), RUN + "_claude_home")
os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR
# The third watched home has no environment override of its own: the marker resolves it from
# the user profile. `~/.agents/skills` is a tree the agent tooling re-syncs while other sessions
# run, so pointing the profile at a throwaway is the only way a scenario's empty delta means
# what the assertion says it means.
REAL_CONFIG_HOME = os.path.dirname(HERE)
AGENT_HOME = os.path.join(tempfile.gettempdir(), RUN + "_agent_home")
os.makedirs(AGENT_HOME, exist_ok=True)
os.environ["USERPROFILE"] = AGENT_HOME
os.environ["HOME"] = AGENT_HOME
MARK_HOOK = os.path.join(HERE, "code_work_gate_mark.py")
GATE_INBOX = os.path.join(HERE, "gate_inbox.py")
def _discard_fixtures():
    """Leave nothing of this run behind.

    The claim files matter as much as the trees: a leftover one keeps an open shell window
    naming a temp directory, and a real session resolving a path under that directory would
    read a finished test as a live competing writer.
    """
    for tree in (CODEX_HOME, CLAUDE_CONFIG_DIR, AGENT_HOME, POSIX_TEMP):
        if tree:
            shutil.rmtree(tree, ignore_errors=True)
    try:
        registry = cwg.claims_root()
        for name in os.listdir(registry):
            if name.startswith(RUN + "_test_"):
                cwg.remove(os.path.join(registry, name))
    except OSError:
        pass


atexit.register(_discard_fixtures)

mark = marker_hook

PASSED = 0


def check(name, condition, detail=None):
    global PASSED
    if not condition:
        raise AssertionError("{}: {}".format(name, detail))
    PASSED += 1


def session():
    # Carries this run's own prefix: the fixture cleanup below deletes claim files by name, and
    # a shared prefix would let a finishing run delete a concurrent one's live scenario state.
    return "{}_test_{}".format(RUN, uuid.uuid4().hex)


def _registry_at(path):
    """What one scan reports when the registry is the given path."""
    real = cwg.claims_root
    cwg.claims_root = lambda: path
    try:
        return cwg.foreign_activity("reader", time.time() - 1)
    finally:
        cwg.claims_root = real


def _crowded_registry_reports_overflow():
    """A directory full of files this scan ignores is still a directory it had to walk."""
    crowded = tempfile.mkdtemp(prefix="cwg_crowded_registry_")
    for index in range(cwg.SCAN_LIMIT + 1):
        with open(os.path.join(crowded, "noise{}.txt".format(index)), "w") as stream:
            stream.write("x")
    try:
        return _registry_at(crowded)[3] is True
    finally:
        shutil.rmtree(crowded, ignore_errors=True)


def _registry_states():
    """Overflow for a registry that does not exist yet, and for one that cannot be listed.

    They are not the same answer: nothing published yet is a complete scan of an empty world,
    while a directory that is there and unreadable leaves a hole the caller has to be told about.
    """
    missing = os.path.join(tempfile.gettempdir(), "cwg_no_such_registry_dir")
    blocked = os.path.join(tempfile.gettempdir(), "cwg_registry_not_a_directory")
    with open(blocked, "w", encoding="utf-8") as stream:
        stream.write("not a directory")
    try:
        return _registry_at(missing)[3], _registry_at(blocked)[3]
    finally:
        cwg.remove(blocked)


def gate_paths(sid):
    key = cwg.session_key(sid)
    return cwg.marker_path(key), cwg.state_path(key)


def cleanup(sid, transcript=None):
    for path in gate_paths(sid):
        cwg.remove(path)
    # A claim file outlives its marker by design, so a leftover one would make the next test's
    # session look like a concurrent writer and silently suppress its attribution.
    cwg.remove(cwg.claim_path(cwg.session_key(sid)))
    # A packet capture outlives its candidate by design — the notification it binds can arrive
    # after the cycle closed — so closing no longer sweeps one and the suite has to.
    for capture in glob.glob(cwg.packet_capture_path(cwg.session_key(sid), "*")):
        cwg.remove(capture)
    if transcript:
        cwg.remove(transcript)
    # Rollout logs are evidence: one left behind would prove a Codex run for the next test.
    shutil.rmtree(os.path.join(CODEX_HOME, "sessions"), ignore_errors=True)


def seed(sid, paths, first_ts=100.0, last_ts=110.0, durable_ts=None):
    marker, _ = gate_paths(sid)
    data = {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_path": paths[-1],
        "paths": [cwg.normalize_path(path) for path in paths],
        "edits": len(paths),
    }
    if durable_ts is not None:
        data["last_durable_ts"] = durable_ts
    check("seed marker", cwg.write_json(marker, data), data)
    return data


def iso(stamp):
    return datetime.datetime.fromtimestamp(
        stamp, tz=datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")


def entry(stamp, role, blocks):
    return {
        "type": role,
        "timestamp": iso(stamp),
        "message": {"role": role, "content": blocks},
    }


def skill_use(stamp, name, call_id):
    return entry(stamp, "assistant", [{
        "type": "tool_use",
        "id": call_id,
        "name": "Skill",
        "input": {"skill": name},
    }])


def agent_use(stamp, subtype, call_id, run_in_background=False, model=None):
    payload = {
        "subagent_type": subtype,
        "prompt": "bounded packet",
    }
    if run_in_background is not None:
        payload["run_in_background"] = run_in_background
    if model:
        payload["model"] = model
    return entry(stamp, "assistant", [{
        "type": "tool_use",
        "id": call_id,
        "name": "Agent",
        "input": payload,
    }])


def bash_use(stamp, call_id, command, run_in_background=None, tool="Bash"):
    """A shell call in the harness's own shape: foreground omits the mode field entirely."""
    payload = {"command": command}
    if run_in_background is not None:
        payload["run_in_background"] = run_in_background
    return entry(stamp, "assistant", [{
        "type": "tool_use",
        "id": call_id,
        "name": tool,
        "input": payload,
    }])


def tool_result(stamp, call_id, text, is_error=False):
    return entry(stamp, "user", [{
        "type": "tool_result",
        "tool_use_id": call_id,
        "is_error": is_error,
        "content": text,
    }])


def write_transcript(events):
    path = os.path.join(
        tempfile.gettempdir(), "gate_transcript_{}.jsonl".format(uuid.uuid4().hex)
    )
    with open(path, "w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


SIMPLIFY_LENSES = list(gate.SIMPLIFY_LENSES)
SIMPLIFY_LANE_ONLY = [gate.SIMPLIFY_LANE]
PROMPT_HOOK = os.path.join(HERE, "code_work_gate_prompt.py")


def simplify_wave(events, stamp, prefix, subtypes):
    for index, subtype in enumerate(subtypes):
        call_id = "{}-{}".format(prefix, index)
        events.append(agent_use(stamp, subtype, call_id))
        events.append(tool_result(stamp + 0.5, call_id, "No actionable findings."))
        stamp += 1
    return stamp


def base_events(include_simplify=False, lenses=SIMPLIFY_LENSES):
    events = [skill_use(120, "development-verification", "skill-dev")]
    if include_simplify:
        events.append(skill_use(121, "simplify", "skill-simplify"))
        simplify_wave(events, 122, "simplify", lenses)
    return events


def add_review(events, stamp, call_id, result, is_error=False, subtype="adversarial-reviewer",
               model=None):
    events.append(agent_use(stamp, subtype, call_id, model=model))
    events.append(tool_result(stamp + 0.5, call_id, result, is_error=is_error))


def add_codex_review(events, stamp, call_id, command, result, is_error=False,
                     run_in_background=None, tool="Bash", codex_ran=True, turn=None):
    """A shell review call, plus the rollout log the Codex CLI writes while it runs.

    `codex_ran=False` is the forgery case: the command printed a verdict, but no Codex process
    was running while the call was open.
    """
    events.append(bash_use(stamp, call_id, command,
                           run_in_background=run_in_background, tool=tool))
    events.append(tool_result(stamp + 0.5, call_id, result, is_error=is_error))
    if codex_ran:
        log_codex_run(stamp + 0.4, result, turn=turn)


CODEX_COMMAND = 'node "codex-companion.mjs" adversarial-review "--wait CODE_WORK_GATE_REVIEW"'
REQUIRED_CODEX_COMMAND = CODEX_COMMAND[:-1] + ' CODE_WORK_GATE_REQUIRED"'
CODEX_ERRAND_COMMAND = 'node "codex-companion.mjs" review "--wait CODE_WORK_GATE_REVIEW"'
CODEX_CLI_COMMAND = "codex exec --json - < /c/tmp/packet-CODE_WORK_GATE_REVIEW.md"
# A launch that feeds no packet on stdin: bound by the single-session rule, no capture needed.
CODEX_BG_COMMAND = "codex exec --json -  # CODE_WORK_GATE_REVIEW"


def review_text(verdict, subject="the auth session candidate"):
    """A review long enough to identify itself, as any real one is.

    Binding needs distinctive text: a result that says only the verdict line would match any
    session that ended the same way, so the hook refuses to bind one.
    """
    return (
        "Reviewed {}: read the changed files whole, traced the callers of the changed "
        "functions, and re-ran the affected checks. Coverage: the diff, its blast radius, and "
        "the fixtures that pin it. No open blocker remains beyond the notes above.\n\n"
        "VERDICT: {}"
    ).format(subject, verdict)


def closure_text(state, subject="the auth session candidate"):
    """A closure-validation result, as distinctive as the review it follows."""
    return review_text("APPROVED", subject).replace(
        "VERDICT: APPROVED", "CLOSURE_VALIDATION: {}".format(state)
    )


def codex_cli_output(text):
    """One reviewer message as `codex exec` puts it on the terminal.

    The CLI prints the final message while it streams, then its own footer, then the same
    message again as the run's last message — so a real verdict reaches the transcript twice.
    """
    return "{}\nhook: Stop\nhook: Stop Failed\ntokens used\n75\u00a0824\n{}".format(
        text, text
    )


AGENTS_DIR = os.path.join(os.path.dirname(HERE), "agents")


def profile_parts(name):
    """An agent profile's front matter and body, split where the harness splits them."""
    with open(os.path.join(AGENTS_DIR, name + ".md"), encoding="utf-8") as stream:
        _, front, body = stream.read().split("---", 2)
    return front, body


def reviewer_role_text():
    """The role text a Codex review session must have been given, as the hook reads it."""
    return profile_parts("adversarial-reviewer")[1]


def log_codex_run(stamp, logged="", role="assistant", said_at=None, briefed=True,
                  partial_role=False, briefed_at=None, filler_bytes=0, earlier=None,
                  packet="Round 1 packet.", turn=None):
    """One Codex rollout log in the CLI's own shape.

    `logged` is what the session said, and only an assistant record stamped inside the call's
    window can vouch for a result: `role` and `said_at` exist so a test can put the same text in
    the prompt the call supplied, or in the older part of a resumed session. `briefed` writes the
    reviewer role into the session's input, which is what marks it a review rather than an errand.
    `turn` is the (model, effort) the CLI records for the turn that says `logged`.
    """
    # Discovery only looks a week back, so the folder has to be today's, not a fixed date.
    day = os.path.join(CODEX_HOME, "sessions", *time.strftime("%Y %m %d").split())
    if not os.path.isdir(day):
        os.makedirs(day)
    path = os.path.join(day, "rollout-{}.jsonl".format(uuid.uuid4().hex))
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": iso(stamp), "type": "session_meta"}) + "\n")
        if briefed:
            given = reviewer_role_text()
            stream.write(json.dumps({
                "timestamp": iso(stamp if briefed_at is None else briefed_at),
                "type": "response_item",
                "payload": {"type": "message", "role": "developer",
                            "content": [{"type": "input_text",
                                         "text": (given[:300] if partial_role else given)
                                                 + "\n\n" + packet}]},
            }) + "\n")
        for at, text in earlier or ():
            # Earlier rounds of the same session, behind the bulk that follows them.
            stream.write(json.dumps({
                "timestamp": iso(at),
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": text}]},
            }) + "\n")
        written = 0
        while written < filler_bytes:
            # History of a resumed session: bulk between the brief and the fresh verdict.
            line = json.dumps({
                # Between the earlier rounds and the fresh one: a rollout log is chronological.
                "timestamp": iso(stamp - 0.1),
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "earlier round " * 300}]},
            }) + "\n"
            stream.write(line)
            written += len(line)
        if turn:
            stream.write(json.dumps({
                "timestamp": iso(stamp if said_at is None else said_at), "type": "turn_context",
                "payload": {"model": turn[0], "effort": turn[1]},
            }) + "\n")
        stream.write(json.dumps({
            "timestamp": iso(stamp if said_at is None else said_at),
            "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "output_text", "text": logged}]},
        }) + "\n")
    os.utime(path, (stamp, stamp))
    return path


def run(script, payload, python=sys.executable):
    proc = subprocess.run(
        [python, script],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    check("hook exits zero", proc.returncode == 0, proc.stderr)
    try:
        return json.loads(proc.stdout.strip())
    except Exception as exc:
        raise AssertionError("invalid hook JSON {!r}: {}".format(proc.stdout, exc))


sid = session()
try:
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
    check("no marker allows stop", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid)

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    payload = {"session_id": sid, "last_assistant_message": "done"}
    for expected in range(1, 4):
        result = run(STOP_HOOK, payload)
        check("finite block {}".format(expected), result.get("decision") == "block", result)
        check(
            "block counter {}".format(expected),
            "block {}/3".format(expected) in result.get("reason", ""),
            result,
        )
    result = run(STOP_HOOK, payload)
    check("fourth stop fails open as unverified", result.get("continue") is True, result)
    check("exhaustion is explicit", "UNVERIFIED" in result.get("systemMessage", ""), result)
finally:
    cleanup(sid)

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    events = base_events()
    simplify_wave(events, 122, "standard-lane", SIMPLIFY_LANE_ONLY)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "Done\n[gate] verified: STANDARD; unit tests passed",
    })
    check("standard with skill passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    result = run(STOP_HOOK, {
        "session_id": sid,
        "last_assistant_message": "[gate] verified: STANDARD; tests passed",
    })
    check("receipt cannot replace skill", result.get("decision") == "block", result)
finally:
    cleanup(sid)

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: LOW; formatter passed",
    })
    check("code cannot be downgraded to low", result.get("decision") == "block", result)
    check("minimum risk is explained", "below path-based minimum STANDARD" in result.get("reason", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/tests/app.test.py"])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: LOW; targeted test passed",
    })
    check("tests-only candidate may be low", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/.claude/skills/example/SKILL.md"])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; markdown checked",
    })
    check("agent-control path cannot be downgraded", result.get("decision") == "block", result)
    check("agent-control minimum is high", "below path-based minimum HIGH" in result.get("reason", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

# An operational candidate may close as `verified` at the risk it declares: prose written
# through the shell is a lasting change the snapshots do not see (report c07fc211).
for label, receipt, expect_ok, expect_reason in (
    ("an operational candidate closes as verified LOW with the protocol read",
     "[gate] verified: LOW; README rewritten through the shell and proofread", True, ""),
    ("verified STANDARD on an operational candidate still owes its simplify lane",
     "[gate] verified: STANDARD; command checked", False, "simplify lenses have no foreground result"),
    ("a closure receipt on an operational candidate names its own contract",
     "[gate] pr-ready: branch x", False, "changed no lasting artifact the gate could see"),
):
    sid = session()
    try:
        seed(sid, [cwg.SHELL_MUTATION_PATH])
        transcript = write_transcript(base_events())
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": receipt,
        })
        ok = result.get("continue") is True and "decision" not in result
        check(label, ok if expect_ok else (result.get("decision") == "block"
                                          and expect_reason in result.get("reason", "")), result)
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": (
            "[gate] operational: disk 2 confirmed non-system and offline before the wipe; "
            "GPT partition present afterwards"
        ),
    })
    check(
        "operational receipt closes an operational candidate without a review panel",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] operational: ran the maintenance script",
    })
    check(
        "operational receipt without a verified effect is malformed",
        result.get("decision") == "block",
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] no-change: read-only inspection, nothing was modified",
    })
    check(
        "no-change closes an operational candidate",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    transcript = write_transcript([])
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] no-change: nothing happened, honest",
    })
    check(
        "no-change still requires the protocol skill",
        result.get("decision") == "block"
        and "development-verification was not invoked" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH], first_ts=100.0, last_ts=110.0)
    events = [skill_use(40.0, "development-verification", "skill-early")]
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] operational: target confirmed offline; wipe verified",
    })
    check(
        "judgment invoked before execution counts for the operational candidate",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    transcript = write_transcript([])
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] operational: precheck done; service healthy",
    })
    check(
        "operational receipt still requires development-verification",
        result.get("decision") == "block"
        and "development-verification was not invoked" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] operational: checked; applied",
    })
    check(
        "an operational receipt cannot close a source change",
        result.get("decision") == "block"
        and "cannot close a candidate that changed a lasting artifact" in result.get("reason", ""),
        result,
    )
    check(
        "the refusal says what keeps the repository from reading as restored",
        "What the hook read: the marker records no repository" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/.claude/rules/operations.md"])
    events = base_events()
    simplify_wave(events, 122, "prose-lane", SIMPLIFY_LANE_ONLY)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; claim rechecked against release.yml",
    })
    check(
        "agent-config prose is standard, not high",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; targeted tests passed",
    })
    check(
        "three-file standard without a simplify lane is blocked",
        result.get("decision") == "block" and gate.SIMPLIFY_LANE in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = base_events(include_simplify=True)
    simplify_wave(events, 128, "confirm", SIMPLIFY_LENSES)
    simplify_wave(events, 132, "third", SIMPLIFY_LENSES)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check("a third pass of the simplify lane is rejected", result.get("decision") == "block", result)
    check(
        "the exhausted pass budget is explained",
        "pass cap" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

for paths, receipt, label, lanes in (
    (["C:/repo/tests/app.test.py"], "[gate] verified: LOW; checks passed", "low", []),
    (["C:/repo/src/app.py"], "[gate] verified: STANDARD; checks passed", "small standard",
     SIMPLIFY_LANE_ONLY),
):
    sid = session()
    try:
        seed(sid, paths)
        events = base_events()
        events.extend([
            skill_use(121, "simplify", "optional-simplify-1"),
            skill_use(122, "simplify", "optional-simplify-2"),
            skill_use(123, "simplify", "optional-simplify-3"),
        ])
        simplify_wave(events, 124, "required-lane", lanes)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": receipt,
        })
        check(
            "re-reading the skill without lenses is not a spent pass for {}".format(label),
            result.get("continue") is True and "decision" not in result,
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = [skill_use(120, "development-verification", "skill-dev")]
    simplify_wave(events, 121, "lenses-first", SIMPLIFY_LENSES)
    events.append(skill_use(130, "simplify", "skill-after-lenses"))
    add_review(events, 131, "review-lane-first", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; checks and review passed",
    })
    check(
        "a lane that ran before the skill call still counts",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    events.append(agent_use(132, "simplify-quality-reviewer", "lane-fail"))
    events.append(tool_result(132.5, "lane-fail", "unavailable", is_error=True))
    add_review(events, 134, "review-after-lane-fail", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; checks and review passed",
    })
    check(
        "a lane whose latest attempt failed cannot close a HIGH candidate as verified",
        result.get("decision") == "block"
        and "no foreground result" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"], first_ts=200.0, last_ts=210.0)
    events = [skill_use(20.0, "development-verification", "skill-earlier-candidate")]
    simplify_wave(events, 201, "current", SIMPLIFY_LENSES)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check(
        "the protocol skill counts once per session, not once per candidate",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=200.0, last_ts=210.0)
    events = [skill_use(20.0, "development-verification", "skill-earlier")]
    simplify_wave(events, 21, "earlier-candidate", SIMPLIFY_LENSES)
    add_review(events, 211, "review-current", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; checks and review passed",
    })
    check(
        "a lane from an earlier candidate does not carry over",
        result.get("decision") == "block"
        and "no foreground result" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = [skill_use(120, "development-verification", "skill-dev")]
    events.append(skill_use(121, "simplify", "skill-first"))
    for index, subtype in enumerate((
        "simplify-reuse-reviewer", "simplify-efficiency-reviewer"
    )):
        call_id = "first-partial-{}".format(index)
        events.append(agent_use(122 + index, subtype, call_id))
        events.append(tool_result(122.5 + index, call_id, "First wave result."))
    events.append(skill_use(128, "simplify", "skill-confirm"))
    events.append(agent_use(
        129, "simplify-quality-reviewer", "late-third-lens"
    ))
    events.append(tool_result(
        129.5, "late-third-lens", "Third lens result."
    ))
    add_review(events, 131, "review-trio-across-waves", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; checks and review passed",
    })
    check(
        "lenses spread across waves still complete a HIGH pass",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    transcript = write_transcript(base_events(include_simplify=True))
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; targeted tests passed",
    })
    check("three-file standard with a lane passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = base_events(include_simplify=True)
    events.append(skill_use(128, "simplify", "skill-confirm"))
    events.append(agent_use(
        129, "simplify-quality-reviewer", "simplify-confirm-quality"
    ))
    events.append(tool_result(
        129.5, "simplify-confirm-quality", "Confirmed affected naming edits."
    ))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; targeted tests passed",
    })
    check("one-lens simplify confirmation preserves first trio", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = base_events(include_simplify=True)
    events.append(agent_use(
        130, "simplify-quality-reviewer", "simplify-quality-failed"
    ))
    events.append(tool_result(
        130.5, "simplify-quality-failed", "temporary failure", is_error=True
    ))
    events.append(agent_use(
        132, "simplify-quality-reviewer", "simplify-quality-retry"
    ))
    events.append(tool_result(
        132.5, "simplify-quality-retry", "Retry completed."
    ))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; simplify retry passed",
    })
    check("failed simplify lens followed by success passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = [skill_use(120, "development-verification", "skill-dev")]
    events.append(skill_use(121, "simplify", "skill-simplify"))
    events.append(agent_use(125, "simplify-quality-reviewer", "simplify-lane-fail-1"))
    events.append(tool_result(
        125.5, "simplify-lane-fail-1", "unavailable", is_error=True
    ))
    events.append(agent_use(127, "simplify-quality-reviewer", "simplify-lane-fail-2"))
    events.append(tool_result(
        127.5, "simplify-lane-fail-2", "still unavailable", is_error=True
    ))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: simplify quality lens unavailable",
    })
    check("two failed required simplify attempts allow draft-blocked", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "optional-review", "HIGH-1 open.\nVERDICT: REVISE")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; targeted tests passed",
    })
    check("invoked review cannot be ignored", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; auth tests and review passed",
    })
    check("high with the three simplify lenses and current approval passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events()
    add_review(events, 130, "review-no-lane", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; auth tests and review passed",
    })
    check("high without a simplify lane is blocked", result.get("decision") == "block", result)
    check(
        "every missing lens is named",
        "no foreground result" in result.get("reason", "")
        and all(lens in result.get("reason", "") for lens in gate.SIMPLIFY_LENSES),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))


# --- simplify by risk: HIGH needs each of the three lenses, STANDARD one lane or the whole trio
def simplify_by_risk(paths, risk, subtypes):
    sid = session()
    try:
        seed(sid, paths)
        events = [skill_use(120, "development-verification", "skill-dev")]
        simplify_wave(events, 122, "by-risk", subtypes)
        if risk == "HIGH":
            add_review(events, 130, "review-by-risk", "No blockers.\nVERDICT: APPROVED")
        transcript = write_transcript(events)
        return run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: {}; checks passed".format(risk),
        })
    finally:
        cleanup(sid, locals().get("transcript"))


HIGH_PATHS = ["C:/repo/src/auth/session.ts"]
STANDARD_PATHS = ["C:/repo/src/app.py"]
result = simplify_by_risk(HIGH_PATHS, "HIGH", SIMPLIFY_LANE_ONLY)
check("a lone simplify-reviewer does not satisfy a HIGH candidate",
      result.get("decision") == "block"
      and all(lens in result.get("reason", "") for lens in gate.SIMPLIFY_LENSES), result)
result = simplify_by_risk(HIGH_PATHS, "HIGH", SIMPLIFY_LENSES[:2])
missing = result.get("reason", "").partition("(missing: ")[2].partition(")")[0]
check("a HIGH candidate missing one lens is blocked with exactly that lens named",
      result.get("decision") == "block" and missing == SIMPLIFY_LENSES[2], result)
result = simplify_by_risk(STANDARD_PATHS, "STANDARD", [])
check("a STANDARD candidate without a simplify lane is blocked with simplify-reviewer named",
      result.get("decision") == "block" and gate.SIMPLIFY_LANE in result.get("reason", ""), result)
result = simplify_by_risk(STANDARD_PATHS, "STANDARD", SIMPLIFY_LANE_ONLY)
check("one simplify-reviewer satisfies a STANDARD candidate",
      result.get("continue") is True and "decision" not in result, result)
result = simplify_by_risk(STANDARD_PATHS, "STANDARD", SIMPLIFY_LENSES)
check("the complete trio satisfies a STANDARD candidate",
      result.get("continue") is True and "decision" not in result, result)
result = simplify_by_risk(STANDARD_PATHS, "STANDARD", SIMPLIFY_LENSES[:2])
check("two lenses do not stand in for the STANDARD lane",
      result.get("decision") == "block" and gate.SIMPLIFY_LANE in result.get("reason", ""), result)
check("the receipt requirements name the simplify lanes per risk",
      gate.SIMPLIFY_LANE in cwg.receipt_requirements("STANDARD")
      and all(lens in cwg.receipt_requirements("HIGH") for lens in gate.SIMPLIFY_LENSES)
      and "simplify" not in cwg.receipt_requirements("LOW"),
      [cwg.receipt_requirements(level) for level in ("LOW", "STANDARD", "HIGH")])

# A closure receipt states no risk: it owes the simplify lanes of the candidate's own floor, and
# only an exhausted lane the candidate still needs unlocks draft-blocked.
sid = session()
try:
    seed(sid, STANDARD_PATHS)
    events = base_events()
    simplify_wave(events, 122, "closure-lane", SIMPLIFY_LANE_ONLY)
    add_review(events, 130, "closure-review-down", "model unavailable", is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: reviewer unavailable after bounded retry",
    })
    check("a STANDARD candidate closing draft-blocked owes one simplify lane, not the trio",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, HIGH_PATHS)
    events = base_events()
    for index, stamp in enumerate((125, 127)):
        call_id = "spent-lane-{}".format(index)
        events.append(agent_use(stamp, gate.SIMPLIFY_LANE, call_id))
        events.append(tool_result(stamp + 0.5, call_id, "unavailable", is_error=True))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: simplify unavailable",
    })
    check("an exhausted lane the candidate does not need never unlocks draft-blocked",
          result.get("decision") == "block"
          and all(lens in result.get("reason", "") for lens in gate.SIMPLIFY_LENSES), result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- background work: a review lane that went to the background, and turns that may end
def notification_text_for(task_id, output_file, status="completed"):
    return (
        "<task-notification>\n<task-id>{}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
        "<output-file>{}</output-file>\n<status>{}</status>\n"
        "<summary>Background command \"review\" {}</summary>\n</task-notification>"
    ).format(task_id, output_file, status, "completed (exit code 0)" if status == "completed" else status)


def notification_records(stamp, text, midturn=False):
    """The records the harness writes for one notification: a user turn when the session was
    idle; queued, attached and removed, never a user turn, when it was absorbed mid-turn."""
    if not midturn:
        return [entry(stamp, "user", [{"type": "text", "text": text}])]
    return [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(stamp), "content": text},
        {"type": "attachment", "timestamp": iso(stamp),
         "attachment": {"type": "queued_command", "prompt": text, "commandMode": "task-notification"}},
        {"type": "queue-operation", "operation": "remove", "timestamp": iso(stamp + 0.3), "content": text,
         "reason": "absorbed_mid_turn"},
    ]


def notification(stamp, task_id, output_file, status="completed"):
    return notification_records(stamp, notification_text_for(task_id, output_file, status))[0]


def midturn_notification(stamp, task_id, output_file, status="completed"):
    return notification_records(stamp, notification_text_for(task_id, output_file, status), midturn=True)


def background_review_events(now, task_id, out_file, ack_text, notify_status="completed", notify=True,
                             lenses=SIMPLIFY_LENSES):
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", lenses)
    events.append(bash_use(now - 700, "codex-" + task_id, CODEX_BG_COMMAND,
                           run_in_background=(True if "running in background" in ack_text else None)))
    events.append(tool_result(now - 699, "codex-" + task_id, ack_text))
    if notify:
        events.append(notification(now - 600, task_id, out_file, notify_status))
    return events


tasks_dir = os.path.join(AGENT_HOME, "tasks")
os.makedirs(tasks_dir, exist_ok=True)


def write_review_output(path, text, finished_at):
    """A background task's output file as the harness leaves it: last written when the task ended."""
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.utime(path, (finished_at, finished_at))


DETACHED_ACK = "Command running in background with ID: {id}. Output is being written to: {out}. You will be notified when it completes."
for label, ack in (
    ("a detached launch", DETACHED_ACK),
    ("a foreground launch the harness moved to the background",
     "Command did not complete within its 120s timeout and was moved to the background (ID: {id}). Output is being written to: {out}. You will be notified when it completes."),
    ("a detached launch that changed directory, with the harness's cwd note appended",
     "Command running in background with ID: {id}. Output is being written to: {out}. You will be notified when it completes. To check interim output, use Read on that file path.\nSession cwd remains C:\\Users\\in\\.claude\\hooks; directory changes made by the backgrounded command do not apply to subsequent commands."),
):
    sid = session()
    try:
        now = time.time()
        task_id = "btask" + uuid.uuid4().hex[:5]
        out_file = os.path.join(tasks_dir, task_id + ".output")
        write_review_output(out_file, codex_cli_output(review_text("APPROVED")), now - 601)
        seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
        events = background_review_events(now, task_id, out_file, ack.format(id=task_id, out=out_file))
        log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")))
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid, "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background",
        })
        check("{} is bound through its completion notification".format(label),
              result.get("continue") is True and "decision" not in result, result)
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "bpend" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    ack = DETACHED_ACK.format(id=task_id, out=out_file)
    transcript = write_transcript(background_review_events(now, task_id, out_file, ack, notify=False))
    payload = {"session_id": sid, "transcript_path": transcript,
               "last_assistant_message": "Waiting for the review.\n[gate] verified: HIGH; pending"}
    result = run(STOP_HOOK, payload)
    check("a pending background review lets the turn end",
          result.get("continue") is True and "decision" not in result, result)
    check("the waiting note names the task and forbids polling",
          task_id in result.get("systemMessage", "") and "Do not poll" in result.get("systemMessage", ""), result)
    state = cwg.read_json(gate_paths(sid)[1]) or {}
    check("a waiting stop is not a block", state.get("blocks", 0) == 0 and state.get("waits") == 1, state)
    for _ in range(gate.MAX_BACKGROUND_WAITS - 1):
        run(STOP_HOOK, payload)
    result = run(STOP_HOOK, payload)
    check("waiting stops are bounded per candidate", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "bdead" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    old = now - 4 * 3600
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=old - 200, last_ts=old - 100, durable_ts=old - 100)
    events = [skill_use(old - 190, "development-verification", "skill-dev")]
    simplify_wave(events, old - 180, "simplify", SIMPLIFY_LENSES)
    events.append(bash_use(old, "codex-dead", CODEX_BG_COMMAND, run_in_background=True))
    events.append(tool_result(old + 1, "codex-dead", DETACHED_ACK.format(id=task_id, out=out_file)))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; still waiting"})
    check("a background task older than the wait limit no longer holds the gate open",
          result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "bfail" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    ack = DETACHED_ACK.format(id=task_id, out=out_file)
    transcript = write_transcript(background_review_events(now, task_id, out_file, ack, notify_status="failed"))
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] draft-blocked: the background Codex lane failed"})
    check("a failed background review lane is current failure evidence for draft-blocked",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "bstop" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    ack = DETACHED_ACK.format(id=task_id, out=out_file)
    events = background_review_events(now, task_id, out_file, ack, notify=False)
    events.append(entry(now - 500, "assistant", [{
        "type": "tool_use", "id": "stop-1", "name": "TaskStop", "input": {"task_id": task_id},
    }]))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; still waiting"})
    check("a stopped background review is no longer in flight",
          result.get("decision") == "block" and "systemMessage" not in result, result)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] draft-blocked: the background Codex lane was stopped"})
    check("a stopped background review lane is current failure evidence for draft-blocked",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- a repository back on the commit the candidate opened on, and clean, changed nothing lasting
with tempfile.TemporaryDirectory(prefix="cwg_restored_") as tree:
    subprocess.run(["git", "-C", tree, "init", "-q"], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "config", "user.email", "t@example.com"], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "config", "user.name", "t"], check=False, capture_output=True)
    target = os.path.join(tree, "src", "app.py")
    os.makedirs(os.path.dirname(target))
    with open(target, "w", encoding="utf-8") as stream:
        stream.write("print('a')" + chr(10))
    subprocess.run(["git", "-C", tree, "add", "."], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "commit", "-q", "-m", "start"], check=False, capture_output=True)
    head = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("<<<<<<< probe" + chr(10))
        run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                        "cwd": tree, "tool_input": {"file_path": target}})
        data = cwg.read_json(marker) or {}
        check("a new cycle remembers the commit it opened on", data.get("head_at_start") == head, data.get("head_at_start"))
        events = [skill_use(120, "development-verification", "skill-dev")]
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] no-change: the probe was aborted"})
        check("a tree that still differs from the start commit keeps the candidate open",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("print('a')" + chr(10))
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] no-change: the probe was aborted and the tree restored"})
        check("a tree restored to the start commit closes as no-change",
              result.get("continue") is True and "decision" not in result, result)
    finally:
        cleanup(sid, locals().get("transcript"))
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("print('b')" + chr(10))
        run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                        "cwd": tree, "tool_input": {"file_path": target}})
        subprocess.run(["git", "-C", tree, "commit", "-q", "-am", "moved"], check=False, capture_output=True)
        events = [skill_use(120, "development-verification", "skill-dev")]
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] operational: checked; committed"})
        check("a clean tree on a different commit is not restored",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
        data = cwg.read_json(marker) or {}
        data["paths"] = list(data.get("paths") or []) + ["C:/elsewhere/lasting.py"]
        subprocess.run(["git", "-C", tree, "reset", "-q", "--hard", head], check=False, capture_output=True)
        cwg.write_json(marker, data)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] no-change: restored"})
        check("a lasting path outside the repository is not something git can vouch for",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
    finally:
        cleanup(sid, locals().get("transcript"))
    subprocess.run(["git", "-C", tree, "reset", "-q", "--hard", head], check=False, capture_output=True)

    def open_by_edit(sid, path, content):
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(content)
        run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                        "cwd": tree, "tool_input": {"file_path": path}})

    def stop_with(sid, transcript, message):
        return run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript, "last_assistant_message": message})

    # A capitalised component below the root: the whole tree is what git reports on.
    upper = os.path.join(tree, "Src", "App.py")
    os.makedirs(os.path.dirname(upper), exist_ok=True)
    with open(upper, "w", encoding="utf-8") as stream:
        stream.write("print('u')" + chr(10))
    subprocess.run(["git", "-C", tree, "add", "."], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "commit", "-q", "-m", "upper"], check=False, capture_output=True)
    head_upper = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    sid = session()
    try:
        open_by_edit(sid, upper, "print('changed')" + chr(10))
        transcript = write_transcript([skill_use(120, "development-verification", "skill-dev")])
        result = stop_with(sid, transcript, "[gate] no-change: reverted")
        check("a modified file under a capitalised directory keeps the candidate open",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
        with open(upper, "w", encoding="utf-8") as stream:
            stream.write("print('u')" + chr(10))
        result = stop_with(sid, transcript, "[gate] no-change: reverted")
        check("restored under a capitalised directory closes as no-change",
              result.get("continue") is True and "decision" not in result, result)
    finally:
        cleanup(sid, locals().get("transcript"))

    # A commit on a side branch, then back: the refs moved, so nothing is "restored".
    sid = session()
    try:
        open_by_edit(sid, upper, "print('side')" + chr(10))
        subprocess.run(["git", "-C", tree, "checkout", "-q", "-b", "side"], check=False, capture_output=True)
        subprocess.run(["git", "-C", tree, "commit", "-q", "-am", "side work"], check=False, capture_output=True)
        subprocess.run(["git", "-C", tree, "checkout", "-q", "master"], check=False, capture_output=True)
        transcript = write_transcript([skill_use(120, "development-verification", "skill-dev")])
        result = stop_with(sid, transcript, "[gate] operational: committed on a side branch; back on master")
        check("a commit on a side branch keeps the candidate open although HEAD is back",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
    finally:
        cleanup(sid, locals().get("transcript"))
        subprocess.run(["git", "-C", tree, "branch", "-q", "-D", "side"], check=False, capture_output=True)

    # A gitignored lasting file: git cannot see its change, so it cannot vouch for it.
    with open(os.path.join(tree, ".gitignore"), "w", encoding="utf-8") as stream:
        stream.write(".env" + chr(10))
    subprocess.run(["git", "-C", tree, "add", ".gitignore"], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "commit", "-q", "-m", "ignore"], check=False, capture_output=True)
    sid = session()
    try:
        secret = os.path.join(tree, ".env")
        open_by_edit(sid, secret, "TOKEN=1" + chr(10))
        transcript = write_transcript([skill_use(120, "development-verification", "skill-dev")])
        result = stop_with(sid, transcript, "[gate] no-change: nothing to see")
        check("an ignored lasting file keeps the candidate open even with a clean status",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
        os.remove(secret)
    finally:
        cleanup(sid, locals().get("transcript"))

    # A repository that does not ignore case cannot vouch for lower-cased paths, which the marker
    # keeps only where the file system ignores case; past the path cap the marker cannot name
    # every lasting path.
    sid = session()
    try:
        open_by_edit(sid, upper, "print('cased')" + chr(10))
        with open(upper, "w", encoding="utf-8") as stream:
            stream.write("print('u')" + chr(10))
        transcript = write_transcript([skill_use(120, "development-verification", "skill-dev")])
        subprocess.run(["git", "-C", tree, "config", "core.ignorecase", "false"], check=False, capture_output=True)
        result = stop_with(sid, transcript, "[gate] no-change: reverted")
        if cwg.CASE_FOLDED_PATHS:
            check("a repository that does not ignore case keeps the candidate open",
                  result.get("decision") == "block" and "still differs" in result["reason"], result)
        else:
            check("where paths keep their case, the repository's case setting does not matter",
                  result.get("continue") is True and "decision" not in result, result)
            open_by_edit(sid, upper, "print('cased')" + chr(10))
            with open(upper, "w", encoding="utf-8") as stream:
                stream.write("print('u')" + chr(10))
        subprocess.run(["git", "-C", tree, "config", "core.ignorecase", "true"], check=False, capture_output=True)
        marker, _ = gate_paths(sid)
        data = cwg.read_json(marker) or {}
        data["path_overflow"] = True
        cwg.write_json(marker, data)
        result = stop_with(sid, transcript, "[gate] no-change: reverted")
        check("a marker past the path cap keeps the candidate open",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
    finally:
        cleanup(sid, locals().get("transcript"))

    # A cycle a shell command opens remembers the state from before that command.
    sid = session()
    try:
        head_before = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        payload = {"session_id": sid, "tool_name": "Bash", "tool_use_id": "open-" + uuid.uuid4().hex[:6],
                   "cwd": tree, "tool_input": {"command": "git commit -q -am 'shell opened'"}}
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        # What the command did: committed one file and left another modified.
        with open(upper, "w", encoding="utf-8") as stream:
            stream.write("print('committed by the command')" + chr(10))
        subprocess.run(["git", "-C", tree, "commit", "-q", "-am", "shell opened"], check=False, capture_output=True)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("print('left dirty')" + chr(10))
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
        marker, _ = gate_paths(sid)
        data = cwg.read_json(marker) or {}
        check("a shell-opened cycle remembers the commit from before the command",
              data.get("head_at_start") == head_before and bool(data.get("refs_at_start")), data.get("head_at_start"))
        subprocess.run(["git", "-C", tree, "checkout", "-q", "--", "src/app.py"], check=False, capture_output=True)
        transcript = write_transcript([skill_use(120, "development-verification", "skill-dev")])
        result = stop_with(sid, transcript, "[gate] operational: committed; tree clean")
        check("a commit made by the opening command keeps the candidate open although the tree is clean again",
              result.get("decision") == "block" and "still differs" in result["reason"], result)
    finally:
        cleanup(sid, locals().get("transcript"))

# --- the same candidate resumed the next morning keeps its cycle, and its review rounds with it
stale = {"first_ts": 100.0, "last_ts": 200.0, "identity": "c:/repo#refs/heads/work",
         "paths": ["c:/repo/src/app.py"]}
day_later = 200.0 + 9 * 3600
check("an idle cycle continues when the same branch touches a file it already holds",
      marker_hook.continues_cycle(stale, day_later, "c:/repo#refs/heads/work", ["c:/repo/src/app.py"]) is True, stale)
check("an idle cycle still ends when a different file is touched",
      marker_hook.continues_cycle(stale, day_later, "c:/repo#refs/heads/work", ["c:/repo/src/other.py"]) is False, stale)
check("an idle cycle still ends when the branch changed",
      marker_hook.continues_cycle(stale, day_later, "c:/repo#refs/heads/next", ["c:/repo/src/app.py"]) is False, stale)
check("an idle cycle continues on an opaque shell mark on the same branch",
      marker_hook.continues_cycle(stale, day_later, "c:/repo#refs/heads/work", [cwg.SHELL_MUTATION_PATH]) is True, stale)
check("an idle cycle ends when the identity is unknown",
      marker_hook.continues_cycle(stale, day_later, None, ["c:/repo/src/app.py"]) is False, stale)
check("within the idle limit the cycle continues as before",
      marker_hook.continues_cycle(stale, 200.0 + 3600, "c:/repo#refs/heads/work", ["c:/repo/src/other.py"]) is True, stale)

# --- every way a command names a configuration home counts, and none of them is a false one
home_norm = cwg.normalize_path(CLAUDE_CONFIG_DIR).rstrip("/")
home_gitbash = "/" + home_norm[0] + home_norm[2:] if re.match(r"^[a-z]:/", home_norm) else home_norm
changed = os.path.join(CLAUDE_CONFIG_DIR, "hooks", "x.py")
for command, expect in (
    ("cd $HOME/.claude && python hooks/patch.py", True),
    ("cd {} && python hooks/patch.py".format(home_gitbash), True),
    ('cd "{}" && python hooks/patch.py'.format(CLAUDE_CONFIG_DIR), True),
    ("cd %USERPROFILE%\\.claude && python hooks/patch.py", True),
    ("cd $HOME/.codex; python x.py", True),
    ('python "$CLAUDE_CONFIG_DIR/hooks/patch.py"', True),
    ("python ${CODEX_HOME}/skills/sync.py", True),
    ("for M in 477 478; do glab api projects/1/merge_requests/$M; done", False),
    ("echo claudette", False),
    ("", False),
):
    got = marker_hook.on_home_ground(changed, "C:/tmp/worktree", ["C:/tmp/worktree"],
                                     {"tool_input": {"command": command}})
    check("on_home_ground {!r} -> {}".format(command[:44], expect), got is expect, command)
check("a command run inside a configuration home without a repository snapshot is on home ground",
      marker_hook.on_home_ground(changed, os.path.join(CLAUDE_CONFIG_DIR, "skills"), [], {"tool_input": {"command": "python sync.py"}}) is True, changed)
check("a trailing slash on the working directory does not lose the ground",
      marker_hook.on_home_ground("C:/tmp/worktree/src/a.py", "C:/tmp/worktree/", [], {"tool_input": {"command": "python x.py"}}) is True, "trailing slash")
check("on_home_ground survives malformed input",
      marker_hook.on_home_ground(changed, "", [None], {}) is False and marker_hook.on_home_ground(changed, None, [], {"tool_input": None}) is False, "malformed")
CHIP_TREE_CWD = os.path.join(CLAUDE_CONFIG_DIR, "state", "chips", "trees", "chip-tree")
for label, cwd, command, expect in (
    ("a chip worktree does not put the rest of its home on the ground", CHIP_TREE_CWD,
     'cd "{}" && python "{}" finish'.format(
         CHIP_TREE_CWD, os.path.join(CLAUDE_CONFIG_DIR, "hooks", "chip_handoff.py")), False),
    ("a command in the hooks directory still names its home", "C:/tmp/worktree",
     'cd "{}" && python x.py'.format(os.path.join(CLAUDE_CONFIG_DIR, "hooks")), True),
    ("a checkout's own .claude directory is no reference to the home", "C:/tmp/worktree",
     'cd "C:/repo/.claude/worktrees/wt" && grep -n "a\\|b" f | head -20', False),
    ("a remote call names no home", "C:/tmp/worktree", "curl -sS https://example.org | head -c 300", False),
    ("a write aimed at a bookkeeping script still names its home", "C:/tmp/worktree",
     "sed -i 's/a/b/' ~/.claude/hooks/codex_lane.py", True),
    ("a copy onto a bookkeeping script by its full path still names its home", "C:/tmp/worktree",
     'cp lane.py "{}"'.format(os.path.join(CLAUDE_CONFIG_DIR, "hooks", "gate_inbox.py")), True),
    ("running a bookkeeping script from elsewhere names no home", "C:/tmp/worktree",
     'python "{}" ack 1234abcd'.format(os.path.join(CLAUDE_CONFIG_DIR, "hooks", "gate_inbox.py")), False),
    ("a home followed by a comma is still named", "C:/tmp/worktree", "rg x --dirs ~/.claude,~/.codex", True),
):
    got = marker_hook.on_home_ground(changed, cwd, [cwd], {"tool_input": {"command": command}})
    check("on_home_ground: " + label, got is expect, command)

# --- another session's change under the shared configuration home is not this command's floor
for label, command, expect_high in (
    ("a command run in its repository does not inherit a floor from a change under the config home",
     "for M in 477 478; do glab api projects/1/merge_requests/$M; done", False),
    ("a command that names the config home still answers for a change under it",
     "python ~/.claude/hooks/patch.py && ls", True),
):
    with tempfile.TemporaryDirectory(prefix="cwg_ground_") as tree:
        sid = session()
        other = cwg.session_key(session())
        try:
            subprocess.run(["git", "-C", tree, "init", "-q"], check=False, capture_output=True)
            with open(os.path.join(tree, "README.md"), "w", encoding="utf-8") as stream:
                stream.write("hello" + chr(10))
            subprocess.run(["git", "-C", tree, "add", "."], check=False, capture_output=True)
            marker, _ = gate_paths(sid)
            payload = {"session_id": sid, "tool_name": "Bash", "tool_use_id": "ground-" + uuid.uuid4().hex[:6],
                       "cwd": tree, "tool_input": {"command": command}}
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            # Another session has a command in flight under the configuration home, and a
            # file appears there while this command runs: nobody can say whose it is.
            cwg.publish_claims(other, shell_start_ts=time.time() - 1, cwd=os.path.join(CLAUDE_CONFIG_DIR, "hooks"))
            foreign = os.path.join(CLAUDE_CONFIG_DIR, "hooks", "foreign_" + uuid.uuid4().hex[:6] + ".py")
            os.makedirs(os.path.dirname(foreign), exist_ok=True)
            with open(foreign, "w", encoding="utf-8") as stream:
                stream.write("# written by another session while the command ran" + chr(10))
            time.sleep(0.05)
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(label, bool(data.get("unattributed_durable")) is expect_high
                  and (foreign.replace(chr(92), "/").lower() not in [x.lower() for x in data.get("paths") or []]), data)
            if expect_high:
                # The whole marker is written or nothing is: record_paths runs behind a fail-open
                # `except`, so a mistake here leaves no marker and silently disables the gate
                # instead of blocking. Assert the real event reached the mark, flagged. `fp` may
                # legitimately be None — this candidate holds no measurable durable path yet.
                recorded = data.get("content_marks") or []
                check("an unattributed event writes a flagged mark instead of failing the hook open",
                      bool(recorded) and recorded[-1].get("unknown") is True, recorded)
            os.remove(foreign)
        finally:
            # A window opened by hand for a session that never runs again must not outlive the
            # scenario: `retire_claims` keeps an open one, and the next scenario's change under the
            # same tree then read as another session's work.
            cwg.remove(cwg.claim_path(other))
            cleanup(sid)

# --- a write another session makes under the home from a shell elsewhere is not this command's
# path: nothing marks it ambiguous, so it used to land in the delta outright (reports 27cd9fe8,
# ae5983b0, 8ae90973)
for label, command, name, expect_recorded in (
    ("a command that neither runs in nor names the home is not handed another session's write",
     "for M in 477 478; do glab api projects/1/merge_requests/$M; done", None, False),
    ("a command that names the home still answers for the change",
     "python ~/.claude/hooks/patch.py && ls", None, True),
    ("a shell write to a bookkeeping script is recorded like any other hook edit",
     "sed -i 's/a/b/' ~/.claude/hooks/codex_lane.py", "codex_lane.py", True),
):
    with tempfile.TemporaryDirectory(prefix="cwg_elsewhere_") as tree:
        sid = session()
        other = cwg.session_key(session())
        try:
            subprocess.run(["git", "-C", tree, "init", "-q"], check=False, capture_output=True)
            marker, _ = gate_paths(sid)
            payload = {"session_id": sid, "tool_name": "Bash", "tool_use_id": "elsewhere-" + uuid.uuid4().hex[:6],
                       "cwd": tree, "tool_input": {"command": command}}
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            cwg.publish_claims(other, shell_start_ts=time.time() - 1, cwd=tempfile.gettempdir())
            foreign = os.path.join(CLAUDE_CONFIG_DIR, "hooks", name or "foreign_" + uuid.uuid4().hex[:6] + ".py")
            os.makedirs(os.path.dirname(foreign), exist_ok=True)
            with open(foreign, "w", encoding="utf-8") as stream:
                stream.write("# written while the command ran" + chr(10))
            time.sleep(0.05)
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            recorded = cwg.normalize_path(foreign) in (data.get("paths") or [])
            check(label, recorded is expect_recorded and not data.get("unattributed_durable"), data)
            os.remove(foreign)
        finally:
            cwg.remove(cwg.claim_path(other))
            cleanup(sid)

# --- how a launch is read: any spelling of codex, one exec segment, no conditional execution
for command, fed, tail in (
    (native("timeout 3600 codex exec --ignore-user-config - < /c/tmp/p.md 2>/c/tmp/x.err  # CODE_WORK_GATE_REVIEW"), True, native("C:/tmp/p.md")),
    ('PYTHONIOENCODING=utf-8 "C:/tools/Codex.exe" exec - < "C:/tmp/q.md"', True, "C:/tmp/q.md"),
    ("CODEX EXEC - < p.md", True, "p.md"),
    ("true || codex exec - < p.md", True, ""),
    ("helper < other.md && codex exec - < target.md", True, "target.md"),
    ("codex exec - < a.md; codex exec - < b.md", True, ""),
    ("codex exec - <<'PY'", False, ""),
    ("codex exec -", False, ""),
    ("cat < notes.md | grep x", True, ""),
    (native("REVIEW_ID=r7; timeout 3600 codex exec - < /c/tmp/codex-packet-${REVIEW_ID}.md 2>/c/tmp/codex-${REVIEW_ID}.err"), True, native("C:/tmp/codex-packet-r7.md")),
    ("codex exec - < /c/tmp/codex-packet-${UNSET}.md", True, ""),
):
    launch = marker_hook.codex_launch(command)
    check("codex_launch reads {!r}".format(command[:48]),
          launch["fed"] is fed and launch["path"].replace(chr(92), "/").endswith(tail) and (bool(launch["path"]) == bool(tail)), launch)

# The launch the /adversarial-review command prescribes: one command written over several lines.
# Splitting on the continued newline left the redirect in a segment of its own, so nothing was
# captured and the verdict bound nothing (report 2b62e2fb).
CONTINUED_LAUNCH = (
    'cd "C:/repo" && REVIEW_ID=r9 ; timeout 3600 codex exec --ignore-user-config \\' + chr(10)
    + "  --disable plugins --disable hooks \\" + chr(10)
    + "  -m gpt-6-sol -c model_reasoning_effort=high \\" + chr(10)
    + native("  - < /c/tmp/codex-packet-${REVIEW_ID}.md 2>/c/tmp/codex-${REVIEW_ID}.err  # CODE_WORK_GATE_REVIEW")
)
launch = marker_hook.codex_launch(CONTINUED_LAUNCH)
check("a launch written over continued lines still names its packet",
      launch["fed"] is True
      and launch["path"].replace(chr(92), "/") == native("C:/tmp/codex-packet-r9.md"), launch)
check("the redirect stays in the segment that runs codex",
      any("codex exec" in segment and "codex-packet" in segment
          for segment in marker_hook.shell_segments(CONTINUED_LAUNCH)),
      marker_hook.shell_segments(CONTINUED_LAUNCH))
check("a token split across a continuation stays one token",
      marker_hook.join_continuations("--flag=abc\\" + chr(10) + "def") == "--flag=abcdef")
check("a bare newline still separates two commands",
      len(marker_hook.shell_segments("git status" + chr(10) + "git log")) == 2,
      marker_hook.shell_segments("git status" + chr(10) + "git log"))
check("a backslash that ends nothing is left alone",
      marker_hook.shell_segments(r"grep -e 'a\b' file") == [r"grep -e 'a\b' file"])
# Only an odd run of backslashes escapes the newline; an even one is escaped backslashes before a
# real separator, and joining there hid the writer on the next line.
for tail, joined in ((chr(92), True), (chr(92) * 2, False), (chr(92) * 3, True)):
    command = "echo a" + tail + chr(10) + "git commit -am x"
    check("a run of {} backslashes before the newline {} continues the line".format(
        len(tail), "" if joined else "never"),
        (len(marker_hook.shell_segments(command)) == 1) is joined, command)
    # Joined, bash runs one `echo` and nothing writes; separated, the `git commit` on the next
    # line does — and grading that read-only is what let a write pass without expiring a verdict.
    check("a writer after {} backslashes is graded write-capable: {}".format(
        len(tail), not joined),
        marker_hook.write_capable({"tool_name": "Bash", "tool_input": {"command": command}})
        is (not joined), command)
check("a continuation ending in CRLF is joined too",
      marker_hook.join_continuations("a" + chr(92) + chr(13) + chr(10) + "b") == "ab")
# PowerShell continues on a backtick; a trailing backslash there is the end of a path, and
# joining on it swallowed the next statement whole.
PS_PATH_THEN_WRITE = "Get-ChildItem C:" + chr(92) + "hooks" + chr(92) + chr(10) + "git commit -am x"
check("a PowerShell path ending in a backslash does not swallow the next statement",
      len(marker_hook.shell_segments(PS_PATH_THEN_WRITE, shell="PowerShell")) == 2,
      marker_hook.shell_segments(PS_PATH_THEN_WRITE, shell="PowerShell"))
check("that PowerShell command is still write-capable",
      marker_hook.write_capable(
          {"tool_name": "PowerShell", "tool_input": {"command": PS_PATH_THEN_WRITE}}) is True)
check("a PowerShell backtick continuation is one command",
      len(marker_hook.shell_segments("Get-Content a.txt `" + chr(10) + "  -Raw",
                                     shell="PowerShell")) == 1)
# Same splitter, second consumer: the continuation segments (`--disable`, `-m`) are no command at
# all, so a read-only pipeline written over several lines used to grade write-capable.
CONTINUED_READ = ("grep -n pattern \\" + chr(10) + "  src/app.py | \\" + chr(10) + "  head -5")
check("a read-only pipeline survives being written over several lines",
      marker_hook.read_only_pipeline(CONTINUED_READ) is True, CONTINUED_READ)
check("a write on a continued line is still a write",
      marker_hook.read_only_pipeline("grep -n x src/app.py \\" + chr(10) + "  ; rm -rf build") is False)
# The scrub of a harmless `2>&1` has to happen after the join, or half of it reads as a redirect
# into a file and the whole pipeline grades write-capable.
check("a merged stderr broken across a continuation is still harmless",
      marker_hook.read_only_pipeline("grep -n x src/app.py 2>\\" + chr(10) + "&1 | head -5") is True)

# Neither shell continues a comment: the marker belongs to the comment, so the newline still
# separates, and the command on the next line is a command.
for shell, marker in (("Bash", chr(92)), ("PowerShell", "`")):
    commented = "cat f # note " + marker + chr(10) + "git commit -am x"
    check("a {} continuation inside a comment does not swallow the next line".format(shell),
          len(marker_hook.shell_segments(commented, shell=shell)) == 2,
          marker_hook.shell_segments(commented, shell=shell))
    check("that {} command is still write-capable".format(shell),
          marker_hook.write_capable(
              {"tool_name": shell, "tool_input": {"command": commented}}) is True, commented)
check("a marker on the last line, after the trailing tag, is no continuation to undo",
      marker_hook.codex_launch(CONTINUED_LAUNCH)["path"].replace(chr(92), "/")
      == native("C:/tmp/codex-packet-r9.md"))
# The `||` and the assignment must be read on the joined text, or a launch that may be skipped
# binds a capture and a split assignment resolves to nothing.
check("a || split across a continuation still hides the launch",
      marker_hook.codex_launch("true |\\" + chr(10) + "| codex exec - < p.md")["path"] == "")
check("an assignment split across a continuation still resolves",
      marker_hook.codex_launch(
          "REVIEW_ID=\\" + chr(10) + native("r9; codex exec - < /c/tmp/codex-packet-${REVIEW_ID}.md")
      )["path"].replace(chr(92), "/") == native("C:/tmp/codex-packet-r9.md"))

# --- the fingerprint: records that cannot imitate one another, existence by presence, the index included
with tempfile.TemporaryDirectory(prefix="cwg_fp_") as tree:
    a = os.path.join(tree, "src", "a.py")
    b = os.path.join(tree, "src", "b.py")
    os.makedirs(os.path.dirname(a))
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('a')" + chr(10))
    only_a = marker_hook.content_fingerprint([a, b])
    with open(b, "wb") as stream:
        stream.write(b"<missing>")
    check("a file holding the old sentinel bytes is not a missing file",
          marker_hook.content_fingerprint([a, b]) != only_a, only_a)
    os.remove(b)
    check("an add that was deleted again is no change", marker_hook.content_fingerprint([a, b]) == only_a
          and marker_hook.content_fingerprint([a]) == only_a, only_a)
    subprocess.run(["git", "-C", tree, "init", "-q"], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "add", "."], check=False, capture_output=True)
    staged_a = marker_hook.content_fingerprint([a])
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('changed')" + chr(10))
    subprocess.run(["git", "-C", tree, "add", "."], check=False, capture_output=True)
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('a')" + chr(10))
    check("a staged blob that matches neither HEAD nor the disk is part of the record",
          marker_hook.content_fingerprint([a]) != staged_a, staged_a)
    subprocess.run(["git", "-C", tree, "checkout", "-q", "--", "."], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "reset", "-q"], check=False, capture_output=True)
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('reviewed')" + chr(10))
    reviewed = marker_hook.content_fingerprint([a])
    subprocess.run(["git", "-C", tree, "add", "."], check=False, capture_output=True)
    added = marker_hook.content_fingerprint([a])
    subprocess.run(["git", "-C", tree, "config", "user.email", "t@example.com"], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "config", "user.name", "t"], check=False, capture_output=True)
    subprocess.run(["git", "-C", tree, "commit", "-q", "-m", "reviewed"], check=False, capture_output=True)
    committed = marker_hook.content_fingerprint([a])
    check("staging and committing the reviewed bytes change nothing",
          reviewed == added == committed and reviewed is not None, (reviewed, added, committed))

    def git(*args):
        return subprocess.run(["git", "-C", tree] + list(args), check=False, capture_output=True, text=True, encoding="utf-8")

    # A tracked file: edit, stage, commit — the fingerprint of the reviewed bytes never moves.
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('tracked edit')" + chr(10))
    edited = marker_hook.content_fingerprint([a])
    git("add", ".")
    staged = marker_hook.content_fingerprint([a])
    git("commit", "-q", "-m", "tracked edit")
    landed = marker_hook.content_fingerprint([a])
    check("a tracked file's fingerprint survives staging and committing its reviewed bytes",
          edited == staged == landed and edited is not None, (edited, staged, landed))

    # A stale index committed: the reviewer saw Q on disk, P was staged and got committed.
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('P')" + chr(10))
    git("add", ".")
    with open(a, "w", encoding="utf-8") as stream:
        stream.write("print('Q')" + chr(10))
    at_review = marker_hook.content_fingerprint([a])
    git("commit", "-q", "-m", "stale index")
    check("committing a stale index changes the fingerprint the reviewer's bytes had",
          marker_hook.content_fingerprint([a]) != at_review, at_review)
    git("checkout", "-q", "--", ".")

    # A staged deletion of a file still on disk is a divergence.
    before_rm = marker_hook.content_fingerprint([a])
    git("rm", "-q", "--cached", "src/a.py")
    check("a staged deletion of a file still on disk changes the fingerprint",
          marker_hook.content_fingerprint([a]) != before_rm, before_rm)
    git("add", ".")

    # A non-ASCII name keys like any other.
    cyr = os.path.join(tree, "src", "\u0437\u0430\u043c\u0435\u0442\u043a\u0438.md")
    with open(cyr, "w", encoding="utf-8") as stream:
        stream.write("one" + chr(10))
    git("add", ".")
    git("commit", "-q", "-m", "cyrillic")
    with open(cyr, "w", encoding="utf-8") as stream:
        stream.write("two" + chr(10))
    cyr_edited = marker_hook.content_fingerprint([cyr])
    git("add", ".")
    cyr_staged = marker_hook.content_fingerprint([cyr])
    with open(cyr, "w", encoding="utf-8") as stream:
        stream.write("three" + chr(10))
    check("a non-ASCII file name is keyed, staged and diverged like any other",
          cyr_edited == cyr_staged and marker_hook.content_fingerprint([cyr]) != cyr_staged, (cyr_edited, cyr_staged))
    git("checkout", "-q", "--", ".")

with tempfile.TemporaryDirectory(prefix="cwg_unborn_") as unborn:
    subprocess.run(["git", "-C", unborn, "init", "-q"], check=False, capture_output=True)
    fresh = os.path.join(unborn, "new.py")
    with open(fresh, "w", encoding="utf-8") as stream:
        stream.write("print('new')" + chr(10))
    first = marker_hook.content_fingerprint([fresh])
    subprocess.run(["git", "-C", unborn, "add", "."], check=False, capture_output=True)
    check("a repository without a commit yet stages without changing the fingerprint",
          first == marker_hook.content_fingerprint([fresh]) and first is not None, first)
    silent = marker_hook.cwg.git_run
    try:
        marker_hook.cwg.git_run = lambda *args, **kwargs: None
        check("a fingerprint is unknown, not clean, when git cannot answer at all",
              marker_hook.content_fingerprint([fresh]) is None, "git silent")
    finally:
        marker_hook.cwg.git_run = silent

# --- a verdict covers content: an edit reverted byte-for-byte leaves the approval in place
def marker_with_marks(sid, marks):
    """`marks` are (ts, fp) or (ts, fp, unknown) — the third field flags an unattributed change."""
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100.0, last_ts=marks[-1][0], durable_ts=marks[-1][0])
    marker, _ = gate_paths(sid)
    data = cwg.read_json(marker)
    written = []
    for mark in marks:
        record = {"ts": mark[0], "fp": mark[1]}
        if len(mark) > 2 and mark[2]:
            record["unknown"] = True
        written.append(record)
    data["content_marks"] = written
    cwg.write_json(marker, data)


for label, marks, verdict_at, expect in (
    ("an approval between an edit and its byte-identical revert still covers the candidate",
     [(110.0, "fpA"), (140.0, "fpB"), (150.0, "fpA")], 120.0, True),
    ("an approval of content that was changed since does not",
     [(110.0, "fpA"), (140.0, "fpB"), (150.0, "fpA")], 145.0, False),
    ("a change the snapshot could not attribute leaves the content unknown",
     [(110.0, "fpA"), (150.0, None)], 120.0, False),
    ("an unattributable change between the verdict and a byte-identical revert is a barrier",
     [(110.0, "fpA"), (150.0, None), (160.0, "fpB"), (170.0, "fpA")], 120.0, False),
    ("a verdict given once the content was measured again covers a later edit-and-revert",
     [(110.0, "fpA"), (150.0, None), (160.0, "fpA"), (170.0, "fpB"), (180.0, "fpA")], 165.0, True),
    ("an unattributed change before the verdict keeps its measurement as the baseline",
     [(110.0, "fpA"), (150.0, "fpA", True), (180.0, "fpA")], 160.0, True),
    ("an unattributed change after the verdict is a barrier even though it measured the content",
     [(110.0, "fpA"), (180.0, "fpA", True)], 160.0, False),
    ("a marker without content marks keeps the strict timestamp rule",
     [], 120.0, False),
):
    sid = session()
    try:
        if marks:
            marker_with_marks(sid, marks)
        else:
            seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100.0, last_ts=150.0, durable_ts=150.0)
        events = base_events(include_simplify=True)
        add_review(events, verdict_at, "review-content", review_text("APPROVED"))
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] verified: HIGH; reviewed"})
        allowed = result.get("continue") is True and "decision" not in result
        check(label, allowed is expect, result)
    finally:
        cleanup(sid, locals().get("transcript"))

marks = marker_hook.content_marks_after([], 100.0, "fpA")
marks = marker_hook.content_marks_after(marks, 110.0, "fpA")
check("an unchanged measurement adds no mark", len(marks) == 1, marks)
marks = marker_hook.content_marks_after(marks, 120.0, "fpA", True)
check("an unattributed change is marked and still carries its measurement",
      len(marks) == 2 and marks[-1]["fp"] == "fpA" and marks[-1].get("unknown") is True, marks)
marks = marker_hook.content_marks_after(marks, 130.0, "fpA")
check("the first measurement after a barrier is recorded, not swallowed as unchanged",
      len(marks) == 3 and not marks[-1].get("unknown") and marks[-1]["fp"] == "fpA", marks)
marks = marker_hook.content_marks_after(marks, 140.0, None, True)
check("an unattributed change git could not measure is still a barrier",
      len(marks) == 4 and marks[-1]["fp"] is None and marks[-1].get("unknown") is True, marks)
marks = marker_hook.content_marks_after(marks, 150.0, None, False)
check("an attributed edit that could not be measured never counts as equal to an earlier blank",
      len(marks) == 5 and marks[-1]["fp"] is None and not marks[-1].get("unknown"), marks)

with tempfile.TemporaryDirectory(prefix="cwg_marks_") as tree:
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        target = os.path.join(tree, "src", "app.py")
        os.makedirs(os.path.dirname(target))
        for content in ("print('a')\n", "print('b')\n", "print('a')\n"):
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(content)
            run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                            "cwd": tree, "tool_input": {"file_path": target}})
        marks = (cwg.read_json(marker) or {}).get("content_marks") or []
        check("the marker fingerprints the lasting paths at every durable change",
              len(marks) == 3 and marks[0]["fp"] == marks[2]["fp"] != marks[1]["fp"]
              and all(isinstance(m["fp"], str) and len(m["fp"]) == 64 for m in marks), marks)
        run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                        "cwd": tree, "tool_input": {"file_path": target}})
        marks = (cwg.read_json(marker) or {}).get("content_marks") or []
        check("an edit that changed nothing adds no mark", len(marks) == 3, marks)
    finally:
        cleanup(sid)

# --- a notification absorbed mid-turn is read from the queue records the harness leaves
sid = session()
try:
    now = time.time()
    task_id = "bmid" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = background_review_events(now, task_id, out_file, DETACHED_ACK.format(id=task_id, out=out_file), notify=False)
    events.extend(midturn_notification(now - 600, task_id, out_file, "completed"))
    log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background"})
    check("a review whose notification was absorbed mid-turn is bound all the same",
          result.get("continue") is True and "decision" not in result and "background work" not in result.get("systemMessage", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "bmidf" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = background_review_events(now, task_id, out_file, DETACHED_ACK.format(id=task_id, out=out_file), notify=False)
    events.extend(midturn_notification(now - 600, task_id, out_file, "failed"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; still waiting"})
    check("a task that failed mid-turn is no longer in flight",
          result.get("decision") == "block" and "background work" not in result.get("systemMessage", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- the packet the launch fed in names the session among several running at once
def capture_launch(sid, call_id, command):
    """What the marker hook does before a Codex launch runs: keep the packet it feeds."""
    run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_use_id": call_id, "cwd": AGENT_HOME, "tool_input": {"command": command}})


PACKET_A = "Round 1 packet for the auth candidate: " + "the session store rotates ids on privilege change and the tests pin it. " * 4
PACKET_B = "Round 1 packet for another chat's candidate: " + "the deploy preamble names its failure and the fixtures cover the retry. " * 4
PACKET_ROLE_ONLY = "Round 1."

for label, packet_on_disk, rewrite_after, other_packet, expect_bound in (
    ("the session given this launch's packet is the one bound, another session alongside notwithstanding",
     PACKET_A, None, PACKET_B, True),
    ("a packet rewritten after the launch still binds the session the launch fed",
     PACKET_A, PACKET_B, PACKET_B, True),
    ("two sessions given the same packet are told apart by nothing and bind nothing",
     PACKET_A, None, PACKET_A, False),
    ("a packet that is all role, with nothing distinctive, binds nothing",
     PACKET_ROLE_ONLY, None, PACKET_B, False),
    ("a packet naming no session binds nothing",
     "Round 1 packet rewritten before the launch: " + "nobody was given these words in this window. " * 5, None, PACKET_B, False),
):
    sid = session()
    try:
        now = time.time()
        task_id = "bpkt" + uuid.uuid4().hex[:5]
        out_file = os.path.join(tasks_dir, task_id + ".output")
        packet_file = os.path.join(AGENT_HOME, "packet-" + task_id + ".md")
        with open(packet_file, "w", encoding="utf-8") as stream:
            stream.write(reviewer_role_text() + "\n\n" + packet_on_disk)
        seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
        events = [skill_use(now - 890, "development-verification", "skill-dev")]
        simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
        # The launch names the packet the way the shell does: a Git-Bash path for one case.
        shell_path = packet_file.replace(chr(92), "/")
        if expect_bound and re.match(r"^[A-Za-z]:/", shell_path):
            shell_path = "/" + shell_path[0].lower() + shell_path[2:]
        command = 'codex exec - < "{}"  # CODE_WORK_GATE_REVIEW'.format(shell_path)
        capture_launch(sid, "codex-" + task_id, command)
        if rewrite_after:
            with open(packet_file, "w", encoding="utf-8") as stream:
                stream.write(reviewer_role_text() + "\n\n" + rewrite_after)
        events.append(bash_use(now - 700, "codex-" + task_id, command, run_in_background=True))
        events.append(tool_result(now - 699, "codex-" + task_id, DETACHED_ACK.format(id=task_id, out=out_file)))
        events.append(notification(now - 600, task_id, out_file, "completed"))
        log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")), packet=PACKET_A)
        log_codex_run(now - 640, codex_cli_output(review_text("APPROVED", subject="another chat's candidate")),
                      packet=other_packet)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background"})
        bound = result.get("continue") is True and "decision" not in result
        check(label, bound is expect_bound, result)
        if expect_bound:
            check("closing the candidate keeps the packet captures, to be dropped by their own expiry",
                  glob.glob(cwg.packet_capture_path(cwg.session_key(sid), "*")), sid)
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "bnocap" + uuid.uuid4().hex[:4]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    command = 'helper < /c/tmp/other.md && codex exec - < /c/tmp/missing-packet.md  # CODE_WORK_GATE_REVIEW'
    events.append(bash_use(now - 700, "codex-" + task_id, command, run_in_background=True))
    events.append(tool_result(now - 699, "codex-" + task_id, DETACHED_ACK.format(id=task_id, out=out_file)))
    events.append(notification(now - 600, task_id, out_file, "completed"))
    log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background"})
    check("a launch that fed a packet but left no capture binds nothing, whatever else spoke",
          result.get("decision") == "block", result)
    check("the stdin redirect is read from the codex segment, not the first redirect on the line",
          marker_hook.codex_launch(command)["path"].replace(chr(92), "/").endswith("missing-packet.md"), marker_hook.codex_launch(command))
finally:
    cleanup(sid, locals().get("transcript"))

# --- a Codex launch with a path but no capture (its mark hook was cancelled) must not abort the
#     scan: everything after the aborting notification went unread, so a later in-flight task
#     earned no wait and the turn was blocked. Assert the later task is still seen.
sid = session()
try:
    now = time.time()
    codex_task = "bnocap" + uuid.uuid4().hex[:4]
    suite_task = "bsuite" + uuid.uuid4().hex[:4]
    codex_out = os.path.join(tasks_dir, codex_task + ".output")
    suite_out = os.path.join(tasks_dir, suite_task + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    # a background Codex review, path on stdin, but no capture_launch — so no capture file
    codex_cmd = "codex exec - < /c/tmp/nocap.md  # CODE_WORK_GATE_REVIEW"
    events.append(bash_use(now - 700, "codex-" + codex_task, codex_cmd, run_in_background=True))
    events.append(tool_result(now - 699, "codex-" + codex_task, DETACHED_ACK.format(id=codex_task, out=codex_out)))
    events.append(notification(now - 600, codex_task, codex_out, "completed"))
    # a later background suite still running: only a completed scan reaches and registers it
    events.append(bash_use(now - 100, "suite-" + suite_task, "python test_gate.py", run_in_background=True))
    events.append(tool_result(now - 99, "suite-" + suite_task, DETACHED_ACK.format(id=suite_task, out=suite_out)))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "waiting for the suite"})
    check("a capture-less Codex launch does not abort the scan: a later in-flight task still earns a wait",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- a background lane's verdict is what the rollout log says, not what the output file says
for label, file_text, logged, expect_bound in (
    ("the rollout, not the output file, states the verdict",
     codex_cli_output(review_text("APPROVED")), [codex_cli_output(review_text("REVISE"))], False),
    ("a missing output file costs the lane nothing",
     None, [codex_cli_output(review_text("APPROVED"))], True),
    ("two briefed sessions speaking in the window are ambiguous and bind nothing",
     codex_cli_output(review_text("APPROVED")),
     [codex_cli_output(review_text("APPROVED")),
      codex_cli_output(review_text("APPROVED", subject="an unrelated candidate"))], False),
):
    sid = session()
    try:
        now = time.time()
        task_id = "broll" + uuid.uuid4().hex[:5]
        out_file = os.path.join(tasks_dir, task_id + ".output")
        if file_text is not None:
            write_review_output(out_file, file_text, now - 601)
        seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
        events = background_review_events(now, task_id, out_file, DETACHED_ACK.format(id=task_id, out=out_file))
        for text in logged:
            log_codex_run(now - 650, text)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background"})
        bound = result.get("continue") is True and "decision" not in result
        check(label, bound is expect_bound and "background work" not in result.get("systemMessage", ""), result)
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "breq" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    write_review_output(out_file, codex_cli_output(review_text("APPROVED")), now - 601)
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    events.append(bash_use(now - 700, "codex-req", REQUIRED_CODEX_COMMAND, run_in_background=True))
    events.append(tool_result(now - 699, "codex-req", DETACHED_ACK.format(id=task_id, out=out_file)))
    events.append(notification(now - 600, task_id, out_file, "completed"))
    log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; required Codex evidence bound in the background"})
    check("a required Codex review bound in the background satisfies the requirement",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    task_id = "breqf" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    events.append(bash_use(now - 700, "codex-req", REQUIRED_CODEX_COMMAND, run_in_background=True))
    events.append(tool_result(now - 699, "codex-req", DETACHED_ACK.format(id=task_id, out=out_file)))
    events.append(notification(now - 600, task_id, out_file, "failed"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; required Codex evidence unavailable"})
    check("a failed required background review cannot close as verified",
          result.get("decision") == "block", result)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] draft-blocked: required Codex evidence unavailable"})
    check("a required background review that failed is an unavailable reviewer for draft-blocked",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- a foreground review that mentions a background task is still a review
sid = session()
try:
    now = time.time()
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    spoken = ("Command did not complete within its 120s timeout and was moved to the background "
              "(ID: example). Output is being written to: C:/tmp/example.output. You will be "
              "notified when it completes. To check interim output, use Read on that file path.\n"
              "Session cwd remains C:/tmp; directory changes made by the backgrounded command do not apply to subsequent commands.\n"
              + review_text("APPROVED"))
    add_codex_review(events, now - 700, "codex-fg", CODEX_CLI_COMMAND, codex_cli_output(spoken))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the foreground"})
    check("a foreground result that opens with the whole moved-to-background envelope is still a review",
          result.get("continue") is True and "decision" not in result and "background work" not in result.get("systemMessage", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- a closure packet sent before round-3 ESCALATE is not a closure validation (report 77226dfa)
def stop_with(sid, events, receipt):
    transcript = write_transcript(events)
    try:
        return run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": receipt,
        })
    finally:
        cleanup(sid, transcript)


VERIFIED_HIGH = "[gate] verified: HIGH; auth tests and review passed"
PR_READY = "[gate] pr-ready: branch review/gate"

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_review(events, 130, "review-1", "VERDICT: REVISE")
add_review(events, 132, "review-2", "VERDICT: REVISE")
add_review(events, 134, "closure-before-escalate", "CLOSURE_VALIDATION: READY")
add_review(events, 136, "review-3", review_text("APPROVED"))
result = stop_with(sid, events, VERIFIED_HIGH)
check("an ordinary APPROVED after a closure packet sent without ESCALATE verifies the candidate",
      result.get("continue") is True and "decision" not in result, result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_review(events, 130, "review-1", "VERDICT: REVISE")
add_review(events, 132, "review-2", "VERDICT: REVISE")
add_review(events, 134, "closure-before-escalate", "CLOSURE_VALIDATION: READY")
result = stop_with(sid, events, PR_READY)
check("a closure packet sent without ESCALATE is not terminal and does not open a closure phase",
      result.get("decision") == "block" and "terminal READY" not in result.get("reason", "")
      and "requires round-3 ESCALATE or exhausted" in result.get("reason", ""), result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_review(events, 130, "review-1", "VERDICT: REVISE")
add_review(events, 132, "review-2", "VERDICT: REVISE")
add_review(events, 133, "closure-before-escalate", "CLOSURE_VALIDATION: READY")
add_review(events, 134, "review-3", "VERDICT: ESCALATE")
result = stop_with(sid, events, PR_READY)
check("pr-ready after ESCALATE names the closure packet that came before it",
      result.get("decision") == "block"
      and "before the ESCALATE is not a closure validation" in result.get("reason", ""), result)
sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
add_review(events, 136, "closure-1", closure_text("READY"))
result = stop_with(sid, events, PR_READY)
check("the closure validation after ESCALATE counts although one came before it",
      result.get("continue") is True and "decision" not in result, result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_review(events, 130, "review-1", review_text("APPROVED"))
add_review(events, 132, "closure-after-approval", "CLOSURE_VALIDATION: READY")
result = stop_with(sid, events, VERIFIED_HIGH)
check("a closure packet sent without ESCALATE is still review activity after an approval",
      result.get("decision") == "block" and "continued after terminal APPROVED" in result.get("reason", ""),
      result)

# --- a native review lane launched into the background is judged at its notification
def agent_notification_text(task_id, result, status="completed"):
    return (
        "<task-notification>\n<task-id>{}</task-id>\n<tool-use-id>toolu_a</tool-use-id>\n"
        "<output-file>C:/tasks/{}.output</output-file>\n<status>{}</status>\n"
        "<summary>Agent \"review\" finished</summary>\n<result>{}</result>\n</task-notification>"
    ).format(task_id, task_id, status, html.escape(result, quote=False))


def agent_notification(stamp, task_id, result, status="completed", midturn=False):
    return notification_records(stamp, agent_notification_text(task_id, result, status), midturn)


def add_background_review(events, stamp, call_id, agent_id, subtype="adversarial-reviewer"):
    events.append(agent_use(stamp, subtype, call_id, run_in_background=True))
    events.append(tool_result(stamp + 0.5, call_id,
                              "Async agent launched successfully. (This tool result is internal metadata.)\n"
                              "agentId: {} (internal ID - do not mention to user.)".format(agent_id)))


PROBED = "Probe timings: 3000 notes then a non-note line: <15 ms; `[^\\r\\n]*?` cannot cross a line.\n\n"

for label, notices, receipt, expect_ok, expect_reason in (
    ("a background lane's verdict is read from its notification result, escaping undone",
     [(140, PROBED + review_text("APPROVED"), "completed", False)], VERIFIED_HIGH, True, ""),
    ("a lane that first stopped to wait for its own suite and then approved is one verdict",
     [(135, "Everything independent has been gathered; waiting for the suite.", "completed", False),
      (140, review_text("APPROVED"), "completed", False)], VERIFIED_HIGH, True, ""),
    ("the notification absorbed mid-turn carries the verdict too, its three records one notice",
     [(140, review_text("APPROVED"), "completed", True)], VERIFIED_HIGH, True, ""),
    ("a lane that completed without a verdict is review activity without an approval",
     [(140, "No verdict line here.", "completed", False)], VERIFIED_HIGH, False, "lacks a current APPROVED"),
    ("a killed lane is a failed lane",
     [(140, "", "killed", False)], VERIFIED_HIGH, False, "lacks a current APPROVED"),
    ("a second verdict from the same lane is activity after the first",
     [(135, review_text("APPROVED"), "completed", False),
      (140, review_text("REVISE"), "completed", False)], VERIFIED_HIGH, False, "continued after terminal APPROVED"),
    ("the same verdict stated again by a resumed lane is activity after the first",
     [(135, review_text("APPROVED"), "completed", False),
      (140, review_text("APPROVED", "the candidate, once more"), "completed", False)], VERIFIED_HIGH, False,
     "continued after terminal APPROVED"),
    ("a stop between two identical statements of the verdict is activity after the first",
     [(135, review_text("APPROVED"), "completed", False),
      (140, "", "stopped", False),
      (145, review_text("APPROVED"), "completed", False)], VERIFIED_HIGH, False, "continued after terminal APPROVED"),
    ("a resumed agent restating a byte-identical APPROVED is activity after the first",
     [(135, review_text("APPROVED"), "completed", False),
      (145, review_text("APPROVED"), "completed", False)], VERIFIED_HIGH, False, "continued after terminal APPROVED"),
):
    sid = session()
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_background_review(events, 130, "bg-review", "agent-" + label[:6].replace(" ", "-"))
    for stamp, text, status, midturn in notices:
        events.extend(agent_notification(stamp, "agent-" + label[:6].replace(" ", "-"), text, status, midturn))
    result = stop_with(sid, events, receipt)
    ok = result.get("continue") is True and "decision" not in result
    check(label, ok if expect_ok else (result.get("decision") == "block"
                                      and expect_reason in result.get("reason", "")), result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"], durable_ts=135)
events = base_events(include_simplify=True)
add_background_review(events, 130, "bg-review", "agent-edited")
events.extend(agent_notification(140, "agent-edited", review_text("APPROVED")))
result = stop_with(sid, events, VERIFIED_HIGH)
check("a background verdict is filed at the launch: a lasting edit after the launch expires it",
      result.get("decision") == "block" and "lacks a current APPROVED" in result.get("reason", ""), result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"], durable_ts=125)
events = base_events(include_simplify=True)
add_background_review(events, 130, "bg-review", "agent-clean")
events.extend(agent_notification(140, "agent-clean", review_text("APPROVED")))
result = stop_with(sid, events, VERIFIED_HIGH)
check("a lasting edit before the launch does not expire the background verdict",
      result.get("continue") is True and "decision" not in result, result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_review(events, 125, "review-1", review_text("APPROVED"))
add_background_review(events, 130, "bg-review", "agent-stopped")
events.append(entry(140, "assistant", [{"type": "tool_use", "id": "stop-1", "name": "TaskStop",
                                        "input": {"task_id": "agent-stopped"}}]))
result = stop_with(sid, events, VERIFIED_HIGH)
check("a background lane stopped by hand is failed activity that reopens an earlier approval",
      result.get("decision") == "block" and "continued after terminal APPROVED" in result.get("reason", ""),
      result)

# What a lane does after stating its verdict is activity after it (Codex R1-001)
for label, later, receipt, expect_reason in (
    ("a lane stopped by hand after stating APPROVED reopens that approval",
     [("stop", 145)], VERIFIED_HIGH, "continued after terminal APPROVED"),
    ("a lane killed after stating APPROVED reopens that approval",
     [("notice", 145, "", "killed")], VERIFIED_HIGH, "continued after terminal APPROVED"),
    ("a lane that went on without a verdict after stating APPROVED reopens that approval",
     [("notice", 145, "One more thought, no verdict.", "completed")], VERIFIED_HIGH,
     "continued after terminal APPROVED"),
):
    sid = session()
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_background_review(events, 130, "bg-review", "agent-later")
    events.extend(agent_notification(140, "agent-later", review_text("APPROVED")))
    for item in later:
        if item[0] == "stop":
            events.append(entry(item[1], "assistant", [{"type": "tool_use", "id": "stop-later", "name": "TaskStop",
                                                        "input": {"task_id": "agent-later"}}]))
        else:
            events.extend(agent_notification(item[1], "agent-later", item[2], item[3]))
    result = stop_with(sid, events, receipt)
    check(label, result.get("decision") == "block" and expect_reason in result.get("reason", ""), result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_review(events, 130, "review-1", "VERDICT: REVISE")
add_review(events, 132, "review-2", "VERDICT: REVISE")
add_review(events, 134, "review-3", "VERDICT: ESCALATE")
add_background_review(events, 136, "bg-closure", "agent-closure")
events.extend(agent_notification(140, "agent-closure", closure_text("READY")))
events.append(entry(145, "assistant", [{"type": "tool_use", "id": "stop-closure", "name": "TaskStop",
                                        "input": {"task_id": "agent-closure"}}]))
result = stop_with(sid, events, PR_READY)
check("a closure lane stopped after stating READY is activity after the terminal READY",
      result.get("decision") == "block" and "continued after terminal READY" in result.get("reason", ""), result)
sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events.extend(agent_notification(150, "agent-closure", closure_text("READY")))
result = stop_with(sid, events, PR_READY)
check("READY stated again after the stop does not hide the stop",
      result.get("decision") == "block" and "continued after terminal READY" in result.get("reason", ""), result)

# C-002: a rollout line that is valid JSON of the wrong type must not fail the scan
check("json_record returns a dict for a non-dict JSON line that passes the token guard",
      gate.json_record('"message"') == {} and gate.json_record('["agent_message"]') == {}
      and gate.json_record('{"message": "hi"}') == {"message": "hi"},
      (gate.json_record('"message"'), gate.json_record('["agent_message"]')))

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_background_review(events, 130, "bg-review", "agent-resumed")
events.extend(agent_notification(135, "agent-resumed", "", "killed"))
events.extend(agent_notification(140, "agent-resumed", review_text("APPROVED")))
result = stop_with(sid, events, VERIFIED_HIGH)
check("a lane killed and then resumed to its verdict is that verdict",
      result.get("continue") is True and "decision" not in result, result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_background_review(events, 90, "bg-review-old", "agent-early")
events.extend(agent_notification(140, "agent-early", review_text("APPROVED")))
result = stop_with(sid, events, VERIFIED_HIGH)
check("a lane launched before the candidate opened lends it no verdict",
      result.get("decision") == "block", result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_background_review(events, time.time() - 100, "bg-review", "agent-running")
result = stop_with(sid, events, "Waiting for the review lane.")
check("a background review lane still running lets the turn end",
      result.get("continue") is True and "decision" not in result, result)

# a native lane whose completion is only a queue enqueue (the turn ended before the attachment)
# stays in flight, so its verdict is never silently dropped (Codex closure finding)
sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
enq_launch = time.time() - 100
add_background_review(events, enq_launch, "bg-review", "agent-enq")
events.append({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(enq_launch + 40),
               "content": agent_notification_text("agent-enq", review_text("APPROVED"), "completed")})
result = stop_with(sid, events, VERIFIED_HIGH)
check("a native lane whose completion is only a queue enqueue stays in flight, not silently dropped",
      result.get("continue") is True and "decision" not in result, result)

# --- the acknowledgement's note lines: exact wording, nothing riding on them, no search on a near miss
NOTE_LINE = "Session cwd remains C:/tmp; directory changes made by the backgrounded command do not apply to subsequent commands."
MOVED_ACK = ("Command did not complete within its 120s timeout and was moved to the background (ID: {id}). "
             "Output is being written to: {out}. You will be notified when it completes. To check interim output, use Read on that file path.")
for label, text, foreground, expect in (
    ("a note carrying a verdict on its own line is not an acknowledgement",
     MOVED_ACK.format(id="x1", out="C:/tmp/x1.output") + chr(10) + NOTE_LINE + " VERDICT: APPROVED", True, None),
    ("a verdict on the line after the note is not an acknowledgement",
     MOVED_ACK.format(id="x2", out="C:/tmp/x2.output") + chr(10) + NOTE_LINE + chr(10) + "VERDICT: APPROVED", True, None),
    ("a note with a different wording is not an acknowledgement",
     DETACHED_ACK.format(id="x3", out="C:/tmp/x3.output") + chr(10) + "Session cwd remains C:/tmp; something else entirely.", False, None),
    ("two notes with CRLF line breaks are an acknowledgement",
     DETACHED_ACK.format(id="x4", out="C:/tmp/x4.output") + chr(13) + chr(10) + NOTE_LINE + chr(13) + chr(10) + NOTE_LINE + chr(13) + chr(10), False, "x4"),
    ("a note before the envelope is not an acknowledgement",
     NOTE_LINE + chr(10) + DETACHED_ACK.format(id="x5", out="C:/tmp/x5.output"), False, None),
):
    check(label, gate.background_ack(text, foreground) == expect, text[-80:])
near_miss = DETACHED_ACK.format(id="x6", out="C:/tmp/x6.output") + (chr(10) + NOTE_LINE + "   ") * 40 + chr(10) + "NOT-A-NOTE"
started_at = time.monotonic()
check("forty near-miss note lines are refused in one pass",
      gate.background_ack(near_miss, False) is None and time.monotonic() - started_at < 0.5,
      time.monotonic() - started_at)

# --- a server started before the candidate opened is still this session's running work
sid = session()
try:
    now = time.time()
    server_id = "bsrv" + uuid.uuid4().hex[:5]
    server_out = os.path.join(tasks_dir, server_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [
        bash_use(now - 3000, "srv-call", "npm run dev", run_in_background=True),
        tool_result(now - 2999, "srv-call", DETACHED_ACK.format(id=server_id, out=server_out)),
        skill_use(now - 890, "development-verification", "skill-dev"),
    ]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; the suite is still running"})
    check("a background task launched before the candidate lets the turn end",
          result.get("continue") is True and server_id in result.get("systemMessage", ""), result)
    events.append(entry(now - 500, "assistant", [{
        "type": "tool_use", "id": "stop-srv", "name": "TaskStop", "input": {"task_id": server_id},
    }]))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; the suite is still running"})
    check("stopping that task ends the allowance", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- a background verdict covers the candidate as launched, not as notified
sid = session()
try:
    now = time.time()
    task_id = "bedit" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    # The lasting edit lands after the launch and after Codex spoke, before the notification.
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 620, durable_ts=now - 620)
    events = background_review_events(now, task_id, out_file, DETACHED_ACK.format(id=task_id, out=out_file))
    log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background"})
    check("a durable edit after the launch expires a background verdict delivered later",
          result.get("decision") == "block" and "background work" not in result.get("systemMessage", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- lane results that mention task notifications are still lane results
sid = session()
try:
    now = time.time()
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    for index, lens in enumerate(gate.SIMPLIFY_LENSES):
        events.append(entry(now - 880 + index, "assistant", [{
            "type": "tool_use", "id": "simp-note-{}".format(index), "name": "Agent",
            "input": {"subagent_type": lens, "run_in_background": False,
                      "description": "simplify"},
        }]))
        events.append(tool_result(now - 870 + index, "simp-note-{}".format(index),
                                  "Checked the task-notification parsing and the <task-notification> handling: no findings."))
    events.append(entry(now - 700, "assistant", [{
        "type": "tool_use", "id": "rev-note", "name": "Agent",
        "input": {"subagent_type": "adversarial-reviewer", "run_in_background": False,
                  "description": "review"},
    }]))
    events.append(tool_result(now - 690, "rev-note",
                              "The task-notification branch is sound.\n" + review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; native review of the notification parser"})
    check("lane results mentioning task notifications are read as lane results",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- one notification record may carry several tasks, each with its own status
sid = session()
try:
    now = time.time()
    task_id = "bmix" + uuid.uuid4().hex[:5]
    other_id = "bmixo" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = background_review_events(now, task_id, out_file, DETACHED_ACK.format(id=task_id, out=out_file), notify=False)
    mixed = (
        "<task-notification>\n<task-id>{}</task-id>\n<status>completed</status>\n</task-notification>\n"
        "<task-notification>\n<task-id>{}</task-id>\n<status>failed</status>\n</task-notification>"
    ).format(other_id, task_id)
    events.append(entry(now - 600, "user", [{"type": "text", "text": mixed}]))
    log_codex_run(now - 650, codex_cli_output(review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the background"})
    check("a failed task in a batched notification is not judged by its neighbour's status",
          result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- a background lane that hit the usage limit trips the breaker at its notification
sid = session()
try:
    import codex_lane
    codex_lane.clear_state()
    now = time.time()
    task_id = "blim" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    err_path = os.path.join(AGENT_HOME, "codex-bg-limit.err")
    with open(err_path, "w", encoding="utf-8") as stream:
        stream.write("ERROR: You've hit your usage limit. Upgrade to Pro, or try again at 4:24 PM.\n")
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    events.append(bash_use(now - 700, "codex-lim", CODEX_BG_COMMAND + " 2>" + err_path.replace("\\", "/"),
                           run_in_background=True))
    events.append(tool_result(now - 699, "codex-lim", DETACHED_ACK.format(id=task_id, out=out_file)))
    events.append(notification(now - 600, task_id, out_file, "failed"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] draft-blocked: the Codex lane hit its usage limit"})
    check("a failed background lane still closes draft-blocked",
          result.get("continue") is True and "decision" not in result, result)
    available, message = codex_lane.status()
    check("the breaker learns a background lane's outage from its stderr capture",
          available is False and "usage limit" in message, message)
    codex_lane.clear_state()
finally:
    cleanup(sid, locals().get("transcript"))

# --- an anomaly report closes a blocked candidate UNVERIFIED, and nothing less than a report does
def inbox(*args, stdin=None, env=None):
    proc = subprocess.run([sys.executable, GATE_INBOX] + list(args), input=stdin, text=True,
                          encoding="utf-8", capture_output=True, check=False, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def file_report(sid, block_reason, facts="the transcript shows the evidence", nonce=""):
    code, out, err = inbox("report", "--session", sid, "--block", block_reason, "--facts", facts,
                           *(["--nonce", nonce] if nonce else []))
    check("gate_inbox report exits zero", code == 0, err)
    match = re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out)
    check("gate_inbox report prints an id", bool(match), out)
    return match.group(1)


def block_reason_of(result):
    match = re.search(r"Cannot finalize this candidate: (.*?)\.\n", result.get("reason") or "", re.S)
    return match.group(1) if match else ""


def nonce_of(result):
    match = re.search(r"--nonce ([0-9a-f]{12})", result.get("reason") or "")
    return match.group(1) if match else ""


def gate_inbox_path():
    return os.path.join(CLAUDE_CONFIG_DIR, "state", "gate-anomalies.jsonl")


sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    payload = {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: 0badc0de; the hook is wrong"}
    result = run(STOP_HOOK, payload)
    check("an anomaly receipt before any block is refused",
          result.get("decision") == "block" and "only available after the gate has blocked" in result["reason"], result)
    check("the block reminder offers the report command with the session id and a nonce",
          "gate_inbox.py" in result["reason"] and sid in result["reason"] and nonce_of(result), result)
    without_nonce = file_report(sid, block_reason_of(result), "the facts")
    refused = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; hook contradicts the transcript".format(without_nonce)})
    check("a report without the block's nonce is refused",
          refused.get("decision") == "block" and "nonce" in refused["reason"], refused)
    check("every block mints a fresh nonce", nonce_of(refused) and nonce_of(refused) != nonce_of(result), refused["reason"][-300:])
    wrong = file_report(sid, "some other reason entirely", nonce=nonce_of(refused))
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; hook contradicts the transcript".format(wrong)})
    check("a report that does not quote the block's reason is refused",
          result.get("decision") == "block" and "does not quote" in result["reason"], result)
    report_id = file_report(sid, block_reason_of(result), "APPROVED at 12:00 from a foreground lane after the last edit at 11:58", nonce=nonce_of(result))
    closed = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; hook contradicts the transcript".format(report_id)})
    check("a report filed after the block, carrying its nonce and quoting its reason, closes the candidate as anomaly-reported",
          closed.get("continue") is True and "anomaly-reported" in closed.get("systemMessage", "") and "UNVERIFIED" in closed.get("systemMessage", ""), closed)
    check("the anomaly closure retires the candidate", not os.path.exists(gate_paths(sid)[0]) or (cwg.read_json(gate_paths(sid)[0]) or {}).get("closed"), gate_paths(sid)[0])
    ledger_lines = open(cwg.event_log_path(), encoding="utf-8").read().splitlines()
    check("the ledger records the anomaly closure with its report id",
          any('"receipt": "anomaly-reported"' in line and report_id in line and sid in line for line in ledger_lines), report_id)
    code, out, _ = inbox("show", report_id)
    shown = json.loads(out)
    check("the report carries the marker, the state, the nonce and the hook's own view",
          shown.get("session") == cwg.session_key(sid) and shown["state"].get("blocks") == 3
          and shown["marker"].get("paths") == 1 and "hook_view" in shown
          and shown.get("block_nonce") == nonce_of(result), shown.get("state"))
    code, out, _ = inbox("list")
    check("the report is listed until acknowledged", report_id in out and wrong in out and without_nonce in out, out)
    for pending_id in (report_id, wrong, without_nonce):
        inbox("ack", pending_id, "--note", "test")
    code, out, _ = inbox("list")
    check("an acknowledged report leaves the list", code == 0 and report_id not in out and wrong not in out, out)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    first = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
    second = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
    check("a repeated block says so",
          second.get("decision") == "block" and "Same reason as the previous block" in second["reason"]
          and "Same reason" not in first["reason"], second)
    third = run(STOP_HOOK, {"session_id": sid, "transcript_path": "C:/x/session.jsonl", "last_assistant_message": "done"})
    check("the block reminder names the transcript when the hook was given one",
          '--transcript "C:/x/session.jsonl"' in third["reason"], third["reason"][-400:])
    report_id = file_report(sid, block_reason_of(third), "the transcript holds the evidence the hook denies", nonce=nonce_of(third))
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; the transcript holds the evidence".format(report_id)})
    check("a substantive block can be answered with a report",
          result.get("continue") is True and "UNVERIFIED" in result.get("systemMessage", ""), result)
    inbox("ack", report_id)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    early = file_report(sid, "HIGH candidate lacks a current APPROVED verdict")
    blocked = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: 0badc0de; the hook is wrong"})
    check("an anomaly receipt naming no report is refused",
          result.get("decision") == "block" and "names no report" in result["reason"], result)
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; the hook is wrong".format(early)})
    check("a report filed before the block cannot close the candidate",
          result.get("decision") == "block" and "nonce" in result["reason"], result)
    inbox("ack", early)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    blocked = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
    # A record written by hand: the minted nonce cannot be guessed, so the forger's best try is
    # the right reason and a timestamp that compares as fresh — NaN never compares as older.
    forged = uuid.uuid4().hex[:8]
    with open(gate_inbox_path(), "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"id": forged, "session": cwg.session_key(sid), "ts": float("nan"),
                                 "block_reason": block_reason_of(blocked), "block_nonce": "0" * 12}) + chr(10))
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; the hook is wrong".format(forged)})
    check("a hand-written record without the minted nonce is refused",
          result.get("decision") == "block" and "nonce" in result["reason"], result)
    forged_timed = uuid.uuid4().hex[:8]
    with open(gate_inbox_path(), "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"id": forged_timed, "session": cwg.session_key(sid), "ts": float("nan"),
                                 "block_reason": block_reason_of(result), "block_nonce": nonce_of(result)}) + chr(10))
    result = run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "[gate] anomaly-reported: {}; the hook is wrong".format(forged_timed)})
    check("a record with the nonce but no real timestamp is refused",
          result.get("decision") == "block" and "no real timestamp" in result["reason"], result)
    inbox("ack", forged)
    inbox("ack", forged_timed)
finally:
    cleanup(sid, locals().get("transcript"))

# --- filing a report inspects the transcript without writing the hook's ledger lines, nor the breaker
sid = session()
try:
    now = time.time()
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    add_review(events, now - 700, "rev-quiet", review_text("APPROVED"))
    transcript = write_transcript(events)
    run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript, "last_assistant_message": "done"})
    before = open(cwg.event_log_path(), encoding="utf-8").read().count(chr(10))
    code, out, err = inbox("report", "--session", sid, "--transcript", transcript, "--block", "x", "--facts", "y")
    after = open(cwg.event_log_path(), encoding="utf-8").read().count(chr(10))
    check("filing a report writes no ledger lines", code == 0 and after == before, (before, after, err))
    import codex_lane
    codex_lane.clear_state()
    limited = "blim" + uuid.uuid4().hex[:5]
    err_path = os.path.join(AGENT_HOME, "codex-report-limit.err")
    with open(err_path, "w", encoding="utf-8") as stream:
        stream.write("ERROR: You've hit your usage limit. Upgrade to Pro, or try again at 4:24 PM." + chr(10))
    events.append(bash_use(now - 500, "codex-rl", CODEX_BG_COMMAND + " 2>" + err_path.replace(chr(92), "/"), run_in_background=True))
    events.append(tool_result(now - 499, "codex-rl", DETACHED_ACK.format(id=limited, out=os.path.join(tasks_dir, limited + ".output"))))
    events.append(notification(now - 400, limited, os.path.join(tasks_dir, limited + ".output"), "failed"))
    transcript = write_transcript(events)
    code, out, err = inbox("report", "--session", sid, "--transcript", transcript, "--block", "x", "--facts", "y")
    check("filing a report does not touch the Codex breaker", code == 0 and codex_lane.status()[0] is True, codex_lane.status())
    inbox("ack", re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out).group(1))
    shown = json.loads(inbox("show", re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out).group(1))[1])
    check("the report still carries the hook's view of the transcript",
          shown["hook_view"].get("available") is True and shown["hook_view"].get("review_events"), shown["hook_view"])
    inbox("ack", shown["id"])
finally:
    cleanup(sid, locals().get("transcript"))

# --- a report is delivered to the registered gate-ops session, or stays in the inbox
registry = os.path.join(CLAUDE_CONFIG_DIR, "state", "gate-ops-session.json")
if os.path.exists(registry):
    os.remove(registry)
sid = session()
code, out, err = inbox("report", "--session", sid, "--block", "x", "--facts", "y")
check("without a gate-ops session the report stays in the inbox",
      code == 0 and "No gate-ops session is registered" in out and "[gate anomaly] report" in out, out)
inbox("ack", re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out).group(1))
code, out, err = inbox("register", "--session", "ops-session-1", "--name", "in-50 [915105]", "--title", "Gate ops")
check("a session registers as the gate-ops session", code == 0 and "ops-session-1" in out and os.path.exists(registry), err)
code, out, err = inbox("report", "--session", sid, "--block", "x", "--facts", "y", "--did", "continued")
check("a report names the registered session and the messaging tool",
      code == 0 and 'session_id "ops-session-1"' in out and "mcp__ccd_session_mgmt__send_message" in out
      and 'SendMessage to "in-50 [915105]"' in out and "did: continued" in out and "show:" in out, out)
inbox("ack", re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out).group(1))
for corrupt in ("[1, 2]", '{"session_id": "   "}', '{"session_id": 5}', "not json"):
    with open(registry, "w", encoding="utf-8") as stream:
        stream.write(corrupt)
    code, out, err = inbox("report", "--session", sid, "--block", "x", "--facts", "y")
    check("a registry naming no session ({!r}) means no gate-ops session, not a crash".format(corrupt[:12]),
          code == 0 and "No gate-ops session is registered" in out, err or out)
    inbox("ack", re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out).group(1))
os.remove(registry)
with tempfile.TemporaryDirectory(prefix="cwg_fresh_home_") as fresh_home:
    fresh_env = dict(os.environ, CLAUDE_CONFIG_DIR=fresh_home)
    code, out, err = inbox("register", "--session", "ops-fresh", env=fresh_env)
    check("register works first thing on a fresh configuration home",
          code == 0 and os.path.exists(os.path.join(fresh_home, "state", "gate-ops-session.json")), err)
    code, out, err = inbox("register", env=fresh_env)
    check("register without the session id says what to pass", code == 2 and "get_session self" in err, err)
cleanup(sid)

# --- the inbox digest reaches only a session started in the config home; the scan derives ledger anomalies
pending = file_report(session(), "HIGH candidate lacks a current APPROVED verdict")
code, out, _ = inbox("digest", stdin=json.dumps({"cwd": CLAUDE_CONFIG_DIR, "hook_event_name": "SessionStart"}))
check("the digest names unresolved reports for a gate-ops session",
      code == 0 and pending in out and "additionalContext" in out, out)
code, out, _ = inbox("digest", stdin=json.dumps({"cwd": AGENT_HOME, "hook_event_name": "SessionStart"}))
check("the digest stays silent elsewhere", code == 0 and out.strip() == "", out)
with open(gate_inbox_path(), "a", encoding="utf-8") as stream:
    stream.write('{"id": ["x"]}' + chr(10) + "not json" + chr(10) + '{"ack": 5}' + chr(10)
                 + json.dumps({"id": "deadbeef", "session": "s", "ts": "soon", "kind": 7, "block_reason": None}) + chr(10))
    stream.write(json.dumps({"id": "cafe0001", "session": "s", "ts": 1e300, "kind": "agent", "block_reason": "huge clock"}) + chr(10))
code, out, err = inbox("digest", stdin="[]")
check("the digest survives a list payload and malformed inbox lines", code == 0 and out.strip() == "", err)
code, out, err = inbox("digest", stdin=json.dumps({"cwd": CLAUDE_CONFIG_DIR}))
check("malformed inbox lines are skipped, real ones still shown", code == 0 and pending in out and "deadbeef" in out, err or out)
check("a timestamp beyond the platform clock is rendered blank, not raised", "cafe0001 ??-?? ??:??" in out, out)
inbox("ack", "cafe0001")
code, out, err = inbox("list")
check("the list survives malformed lines", code == 0 and pending in out, err)
inbox("ack", "deadbeef")
inbox("ack", pending)
with open(cwg.event_log_path(), "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"ts": time.time(), "kind": "exhausted", "session": "scan-test", "reason": "x"}) + "\n")
    # Written in the order the hooks write them: the edit first, the verdict it expired only at
    # the next Stop — the scan must judge by when things happened.
    stream.write(json.dumps({"ts": time.time() - 20, "kind": "durable", "session": "scan-test-2", "reason": "edit", "command": "git"}) + "\n")
    stream.write(json.dumps({"ts": time.time() - 5, "kind": "review", "session": "scan-test-2", "at": time.time() - 30, "engine": "codex", "verdict": "APPROVED"}) + "\n")
    for offset in (10, 5):
        stream.write(json.dumps({"ts": time.time() - offset, "kind": "review", "session": "scan-test-3", "engine": "codex-background", "verdict": None, "task": "btask-same"}) + "\n")
    edited_at = time.time() - 40
    stream.write(json.dumps({"ts": edited_at, "kind": "durable", "session": "scan-test-4", "reason": "edit"}) + chr(10))
    stream.write(json.dumps({"ts": time.time() - 3, "kind": "review", "session": "scan-test-4", "at": edited_at - 10, "engine": "native", "verdict": "APPROVED"}) + chr(10))
with open(gate_inbox_path(), "a", encoding="utf-8") as stream:
    # An incident a previous version recorded under the rule's old name.
    stream.write(json.dumps({"id": "0ldru1e0", "session": "scan-test-4", "ts": time.time(), "auto": True,
                             "kind": "auto:verdict-expired-after-review", "rule": "verdict-expired-after-review",
                             "event_at": edited_at}) + chr(10))
code, out, _ = inbox("scan")
check("the scan derives anomalies from the ledger", code == 0 and re.search(r"GATE_SCAN: [1-9]", out), out)
code, listed, _ = inbox("list")
check("the scan reports exhaustion and an approval expired right after it was stated",
      "auto:exhausted" in listed and "auto:verdict-expired-after-approval" in listed, listed)
expired_records = [json.loads(line) for line in open(gate_inbox_path(), encoding="utf-8") if '"scan-test-4"' in line]
check("an incident recorded under a rule's old name is not derived again under the new one",
      len([r for r in expired_records if "verdict-expired" in str(r.get("rule"))]) == 1, expired_records)
check("one unbound review task re-read by several Stop runs is one anomaly",
      sum(1 for line in listed.splitlines() if "scan-tes" in line and "unbound-background-review" in line) == 1, listed)
code, out, _ = inbox("scan")
check("a second scan adds nothing", "GATE_SCAN: 0 new" in out, out)

# --- the ledger records the decision
sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
    ledger = cwg.event_log_path()
    lines = open(ledger, encoding="utf-8").read().splitlines() if os.path.exists(ledger) else []
    check("a block is written to the gate event ledger",
          any('"kind": "block"' in line and sid in line for line in lines), ledger)
finally:
    cleanup(sid)

# --- write-capable commands are the only ones that expire a verdict through an unresolved mutation
for command, expected in (
    ('cd "C:/repo" && git status --porcelain | wc -l && git rev-parse --short HEAD', False),
    ("ls ~/.codex/sessions | tail -3", False),
    ("npm test -- --runInBand", False),
    ("python -c rewrite_source", True),
    ("python - <<'PY'\nimport io\nPY", True),
    ("ls > out.txt", True),
    ("timeout 3600 codex exec --ignore-user-config - < /c/tmp/p.md 2>/c/tmp/x.err", True),
    ("git merge --no-ff chip/x", True),
    ("git commit -m x && git push", True),
    ("sed -i 's/a/b/' file.py", True),
    ("rg pattern | tee reviewed.py", True),
    ("truncate -s 0 reviewed.py", True),
    ("echo hi 1> reviewed.py", True),
    ("grep -rn foo . 2>/dev/null | head -5", False),
    ("cat a.txt 2>&1 | head", False),
    ("git branch --show-current && git remote -v && git stash list", False),
    ("git branch new-branch", True),
    ("git stash", True),
    ("for f in *.py; do cat $f; done", True),
    ("curl -sL https://example.org | head", True),
    ("ls $(pwd)", True),
    ('PYTHONIOENCODING=utf-8 timeout 30 "C:/tools/python.exe" -c pass', True),
    ("find . -name '*.py' -delete", True),
    ("find . -name '*.py' | head", False),
    ("find . -name '*.py' -exec rm {} \\;", True),
    ("sort -o reviewed.py reviewed.py", True),
    ("sort --output=reviewed.py reviewed.py", True),
    ("uniq input reviewed.py", True),
    ("uniq -c input", False),
    ("tree -o out.txt", True),
    ("git diff --output=reviewed.py", True),
    ("git -C C:/repo log --oneline -3", False),
    ("git --no-pager diff HEAD~1 --stat", False),
    ("git reflog expire --all", True),
    ("git reflog", False),
    ("rg --pre cat foo", True),
    ("date -s '2020-01-01'", True),
    ("hostname newname", True),
    ("cat <(rm -f hooks/x.py)", True),
    ("diff <(git show HEAD:a) <(cat a)", True),
    ("sort -uo reviewed.py reviewed.py", True),
    ("sort -oreviewed.py input", True),
    ("sort --ignore-case input | head", False),
    ("GIT_EXTERNAL_DIFF=./evil.sh git diff", True),
    ("git -c diff.external=./evil.sh diff", True),
    ("git -c core.fsmonitor=./evil.sh status", True),
    ("RIPGREP_CONFIG_PATH=./evil rg foo", True),
    # A redirect into a throwaway file writes nothing lasting, in its Git Bash spelling too (dc30d302).
    (native("cd /c/tmp/repo && gh pr view 2 --json body -q .body > /c/tmp/x.md"), False),
    (native("cd /c/tmp/repo && gh pr view 2 --json body -q .body > /c/tmp/repo/body.md"), True),
    (native("gh pr edit 2 --body-file /c/tmp/x.md && gh pr view 2 --json url"), False),
    ("gh -R o/r pr view 2 && gh --repo=o/r api repos/o/r", False),
    ("gh pr checkout 7", True),
    ("gh --repo o/r pr checkout 7", True),
    ("gh co 7", True),
    ("gh repo clone o/r", True),
    ("gh extension install o/x", True),
    ("", False),
):
    check("write-capable: {!r} -> {}".format(command[:50], expected),
          marker_hook.write_capable({"tool_name": "Bash", "tool_input": {"command": command}}) is expected,
          command)

for command, expected in (
    ("Get-Content x.py | Select-String foo", False),
    ("Get-ChildItem -Recurse | Where-Object { $_.Length -gt 5MB }", True),
    ("Set-Content x.py 'y'", True),
    ("git status --porcelain | Measure-Object -Line", False),
    ("Write-Output ([IO.File]::WriteAllText('C:/x/auth.py','evil'))", True),
    ("Get-Content x.py | Select-Object -First 3", False),
):
    check("write-capable (PowerShell): {!r} -> {}".format(command[:50], expected),
          marker_hook.write_capable({"tool_name": "PowerShell", "tool_input": {"command": command}}) is expected,
          command)
# --- a discarded stream or a quoted separator does not make a read a write (report 5ed394cc)
for command, expected in (
    ("cd t && echo x && git merge-tree --write-tree a b >/dev/null 2>&1", False),
    ("echo x > notes.txt", True),
    ("cat a.txt >> b.txt", True),
    ("echo x | tee f.txt", True),
    ("sed -n 1p a.txt; grep -i x a.txt", False),
    ("sed -i s/a/b/ a.txt", True),
    ("echo x | sudo tee f.txt", True),
    ("cat a.txt | xargs tee b.txt", True),
    ("find . -name '*.tmp' -print0 | xargs -0 rm", True),
    ("grep tee notes.txt", False),
    ("echo x > nul.txt", True),
    # The null device only on Windows; elsewhere `nul` is a file like any other.
    ("echo x > nul", os.name != "nt"),
    ("echo x | sudo -u root tee f.txt", True),
    ("command -v rm", False),
    ("cat x &> f.txt", True),
    ("echo x >/dev/null.bak", True),
    ("Write-Output x; echo y >$null", False),
    ('echo "a; rm -rf x" > /dev/null', False),
):
    check("shell_write: {!r} -> {}".format(command[:48], expected),
          marker_hook.shell_write({"tool_name": "Bash", "tool_input": {"command": command}}) is expected,
          command)
QUOTED_ALTERNATION = 'grep -i "chip_handoff\\|chip-handoff" f'
check("a separator inside quotes is part of the argument",
      marker_hook.shell_segments(QUOTED_ALTERNATION) == [QUOTED_ALTERNATION],
      marker_hook.shell_segments(QUOTED_ALTERNATION))
check("a quoted alternation in a read stays a read",
      marker_hook.read_only_pipeline('grep -i "a\\|b" f | head -5') is True)
check("a separator outside the quotes still splits",
      len(marker_hook.shell_segments('echo "a;b" && rm x')) == 2,
      marker_hook.shell_segments('echo "a;b" && rm x'))
check("quotes that do not balance split the plain way",
      len(marker_hook.shell_segments("echo it's; rm x")) == 2,
      marker_hook.shell_segments("echo it's; rm x"))
check("an ampersand inside a redirect is no separator",
      marker_hook.shell_segments("cat x &> f.txt") == ["cat x &> f.txt"]
      and len(marker_hook.shell_segments("make 2>&1 | tee build.log")) == 2,
      (marker_hook.shell_segments("cat x &> f.txt"), marker_hook.shell_segments("make 2>&1 | tee build.log")))
check("a discarded stdout is no write",
      marker_hook.read_only_pipeline("git log --oneline -1 >/dev/null") is True)
check("the ledger keeps only the executable of a command",
      marker_hook.command_label('cd "C:/repo" && python deploy.py --token=SECRET') == "cd"
      and marker_hook.command_label("TOKEN=abc curl -H 'x: y' https://h") == "curl",
      marker_hook.command_label("TOKEN=abc curl -H 'x: y' https://h"))
check("a PowerShell assignment never reaches the ledger",
      marker_hook.command_label("$token='SECRET'; Get-Content x") == "(unrecognized)"
      and marker_hook.command_label("$env:API_KEY='SECRET'; git status") == "(unrecognized)"
      and marker_hook.command_label('"C:/tools/python.exe" script.py') == "python",
      marker_hook.command_label("$token='SECRET'; Get-Content x"))

with tempfile.TemporaryDirectory(prefix="cwg_quiet_") as outside:
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        run(MARK_HOOK, {
            "session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Write",
            "cwd": outside, "tool_input": {"file_path": os.path.join(outside, "src", "app.py")},
        })
        approved_at = (cwg.read_json(marker) or {}).get("last_durable_ts")
        payload = {
            "session_id": sid, "tool_name": "Bash", "tool_use_id": "quiet-shell",
            "cwd": outside,
            "tool_input": {"command": 'cd "%s" && git status --porcelain | wc -l && git rev-parse --short HEAD' % outside},
        }
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
        data = cwg.read_json(marker) or {}
        check("an unresolved read-only pipeline does not expire the verdict",
              cwg.valid_ts(approved_at) and data.get("last_durable_ts") == approved_at, data)
        check("the quiet command leaves the open candidate as it was, adding no mutation it did not make",
              data.get("paths") == [cwg.normalize_path(os.path.join(outside, "src", "app.py"))], data)
    finally:
        cleanup(sid)

# --- candidate_shape reads a marker exactly as the Stop hook's own classification does
for label, marker_entry in (
    ("plain source", {"first_ts": 100.0, "last_ts": 110.0, "paths": ["c:/repo/src/app.py"], "minimum_risk_seen": "STANDARD"}),
    ("tests only", {"first_ts": 100.0, "last_ts": 110.0, "paths": ["c:/repo/tests/app.test.py"], "minimum_risk_seen": "LOW"}),
    ("shell mutation only", {"first_ts": 100.0, "last_ts": 110.0, "paths": [cwg.SHELL_MUTATION_PATH], "minimum_risk_seen": "LOW"}),
    ("scratchpad only", {"first_ts": 100.0, "last_ts": 110.0, "paths": ["c:/users/in/appdata/local/temp/claude/x/scratchpad/run.py"], "minimum_risk_seen": None}),
    ("overflowed", {"first_ts": 100.0, "last_ts": 110.0, "paths": [cwg.SHELL_MUTATION_PATH], "minimum_risk_seen": "LOW", "path_overflow": True}),
    ("unattributed durable", {"first_ts": 100.0, "last_ts": 110.0, "paths": [cwg.SHELL_MUTATION_PATH], "minimum_risk_seen": "HIGH", "unattributed_durable": True}),
    ("legacy last_path", {"first_ts": 100.0, "last_ts": 110.0, "paths": [], "last_path": "C:/repo/src/auth/session.ts", "minimum_risk_seen": None}),
    ("auth and tests", {"first_ts": 100.0, "last_ts": 110.0, "paths": ["c:/repo/src/auth/session.ts", "c:/repo/tests/a.test.ts", "c:/repo/src/auth/session.ts"], "minimum_risk_seen": "HIGH"}),
):
    shape = cwg.candidate_shape(marker_entry)
    persistent = gate.candidate_class(marker_entry) == cwg.WORK_PERSISTENT
    check("candidate_shape agrees on persistence for {}".format(label), shape["persistent"] == persistent, (shape, persistent))
    if persistent:
        floor = cwg.max_risk(gate.minimum_risk(gate.marker_paths(marker_entry)), marker_entry.get("minimum_risk_seen"))
        check("candidate_shape agrees on the floor for {}".format(label), shape["floor"] == floor, (shape, floor))
    else:
        check("no floor for an operational shape ({})".format(label), shape["floor"] is None, shape)
check("a closed marker has no shape", cwg.candidate_shape({"first_ts": 1.0, "closed": True, "paths": ["c:/repo/src/app.py"]}) is None, "closed")
check("a marker without a cycle has no shape", cwg.candidate_shape({"paths": ["c:/repo/src/app.py"]}) is None, "no first_ts")

# --- the open-candidate reminder on every prompt, and silence without a candidate
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts", "C:/repo/src/app.py"])
    result = run(PROMPT_HOOK, {"session_id": sid, "prompt": "continue"})
    context = (result.get("hookSpecificOutput") or {}).get("additionalContext", "")
    check("prompt reminder names the open candidate", "Open candidate: PERSISTENT" in context, result)
    check("prompt reminder names the floor", "path floor HIGH" in context and "2 lasting files" in context, result)
    check("prompt reminder names the receipt", "[gate] verified: HIGH" in context, result)
finally:
    cleanup(sid)

sid = session()
try:
    result = run(PROMPT_HOOK, {"session_id": sid, "prompt": "hello"})
    check("prompt reminder is silent without a candidate", "hookSpecificOutput" not in result and result.get("continue") is True, result)
finally:
    cleanup(sid)

# --- the marker announces a candidate once, and again only when its floor rises
sid = session()
try:
    repo = os.path.join(AGENT_HOME, "note-repo")
    os.makedirs(repo, exist_ok=True)
    def mark_note(event, tool, path):
        result = run(MARK_HOOK, {
            "session_id": sid, "hook_event_name": event, "tool_name": tool,
            "tool_input": {"file_path": path}, "cwd": repo,
        })
        return (result.get("hookSpecificOutput") or {}).get("additionalContext")
    note = mark_note("PostToolUse", "Write", "C:/repo/src/app.py")
    check("first durable edit announces the candidate", bool(note) and "Candidate opened: PERSISTENT" in note and "floor STANDARD" in note, note)
    check("the announcement names the receipt", bool(note) and "[gate] verified: STANDARD" in note, note)
    check("a second edit of the same candidate is silent", mark_note("PostToolUse", "Edit", "C:/repo/src/app.py") is None, "silent")
    check("another file at the same floor is silent", mark_note("PostToolUse", "Edit", "C:/repo/src/other.py") is None, "silent")
    raised = mark_note("PostToolUse", "Write", "C:/repo/src/auth/session.ts")
    check("a rising floor is announced once", bool(raised) and "floor raised" in raised and "HIGH" in raised, raised)
    check("the raised floor is then silent", mark_note("PostToolUse", "Edit", "C:/repo/src/auth/session.ts") is None, "silent")
    check("PreToolUse never announces", mark_note("PreToolUse", "Write", "C:/repo/src/more.py") is None, "silent")
    shell = run(MARK_HOOK, {
        "session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "echo x > C:/repo/src/app.py"},
        "tool_response": {"stdout": "", "stderr": ""}, "cwd": repo,
    })
    check(
        "a shell mutation inside an announced candidate is silent",
        "hookSpecificOutput" not in shell and shell.get("continue") is True,
        shell,
    )
finally:
    cleanup(sid)
    shutil.rmtree(os.path.join(AGENT_HOME, "note-repo"), ignore_errors=True)

# --- the Codex lane circuit breaker reads the CLI's own refusal and expires on its own
codex_lane.clear_state()
try:
    check("no record means available", codex_lane.status()[0] is True, codex_lane.status())
    check(
        "unrelated stderr records nothing",
        codex_lane.record_outage("warning: Skill descriptions were shortened\nVERDICT: APPROVED") is False
        and codex_lane.status()[0] is True,
        codex_lane.status(),
    )
    now = datetime.datetime(2026, 9, 2, 12, 0, 0)
    found = codex_lane.outage_from_text(
        "ERROR: You've hit your usage limit. Upgrade to Pro, visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 3:30 PM.", now=now)
    check("usage limit names its retry time", found is not None and found[0] == now.replace(hour=15, minute=30).timestamp(), found)
    found = codex_lane.outage_from_text("ERROR: You've hit your usage limit ... try again at 9:15 AM.", now=now)
    check("a retry time already past is capped at the outage horizon", found is not None and found[0] == now.timestamp() + codex_lane.MAX_OUTAGE, found)
    found = codex_lane.outage_from_text("ERROR: You've hit your usage limit ... try again at 3:75 PM.", now=now)
    check("an impossible minute falls back to the default limit outage", found is not None and found[0] == now.timestamp() + codex_lane.DEFAULT_LIMIT_OUTAGE, found)
    quoted = (
        "user\nReview codex_lane.py: it matches the CLI text \"You've hit your usage limit ... try again "
        "at 3:30 PM\" and 'Selected model is at capacity'.\n\ncodex\nThe matcher is anchored, so a quoted "
        "hit your usage limit phrase does not count.\nVERDICT: APPROVED\ntokens used\n12 345\n"
    )
    check("a review that quotes the CLI phrases is not an outage", codex_lane.outage_from_text(quoted, now=now) is None, quoted)
    found = codex_lane.outage_from_text("ERROR: Selected model is at capacity. Please try a different model.", now=now)
    check("capacity is a bounded outage", found is not None and abs(found[0] - (now.timestamp() + codex_lane.DEFAULT_OUTAGE)) < 1, found)
    refusal = ('ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":'
               '"The \'gpt-6-sol\' model requires a newer version of Codex. Please upgrade to the latest '
               'app or CLI and try again."}}')
    found = codex_lane.outage_from_text(refusal, now=now)
    check("a model newer than the CLI is an outage that names the upgrade",
          found is not None and "upgrade" in found[1]
          and abs(found[0] - (now.timestamp() + codex_lane.DEFAULT_LIMIT_OUTAGE)) < 1, found)
    refusal = ('ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":'
               '"The \'gpt-6-sol\' model is not supported when using Codex with a ChatGPT account."}}')
    found = codex_lane.outage_from_text(refusal, now=now)
    check("the ChatGPT-account refusal of a new model is the same upgrade outage",
          found is not None and "upgrade" in found[1]
          and abs(found[0] - (now.timestamp() + codex_lane.DEFAULT_LIMIT_OUTAGE)) < 1, found)
    quoted_refusal = "codex\nThe model requires a newer version of Codex.\nVERDICT: APPROVED"
    check("a review quoting that refusal is not an outage",
          codex_lane.outage_from_text(quoted_refusal, now=now) is None, quoted_refusal)
    check(
        "a recorded outage makes the lane unavailable",
        codex_lane.record_outage("ERROR: Selected model is at capacity. Please try a different model.") is True
        and codex_lane.status()[0] is False and "unavailable until" in codex_lane.status()[1],
        codex_lane.status(),
    )
    check("clearing restores the lane", codex_lane.clear_state() and codex_lane.status()[0] is True, codex_lane.status())
    check(
        "the stderr redirect of the lean command is found",
        codex_lane.stderr_file_of(native("timeout 3600 codex exec --ignore-user-config - < /c/tmp/codex-packet-1.md 2>/c/tmp/codex-1.err  # CODE_WORK_GATE_REVIEW"))
        == native("C:/tmp/codex-1.err"),
        codex_lane.stderr_file_of(native("x 2>/c/tmp/codex-1.err")),
    )
    check("a Git Bash /c/... spelling is a drive only on Windows",
          codex_lane.windows_path("/c/tmp/codex-1.err")
          == ("C:/tmp/codex-1.err" if os.name == "nt" else "/c/tmp/codex-1.err"),
          codex_lane.windows_path("/c/tmp/codex-1.err"))
    err_path = os.path.join(AGENT_HOME, "codex-probe.err")
    with open(err_path, "w", encoding="utf-8") as stream:
        stream.write("tokens used\nERROR: You've hit your usage limit. try again at 3:30 PM.\n")
    check(
        "a finished codex exec call with a refusing stderr records the outage",
        codex_lane.record_from_command("codex exec --ignore-user-config - < p.md 2>" + err_path.replace("\\", "/"), "")
        is True and codex_lane.status()[0] is False,
        codex_lane.status(),
    )
    codex_lane.clear_state()
    check("an errand is ignored", codex_lane.record_from_command("git status", "You've hit your usage limit") is False, "ignored")
    check(
        "a redirect variable is resolved from the same command",
        codex_lane.stderr_file_of(native('REVIEW_ID=r7; timeout 3600 codex exec - < /c/tmp/codex-packet-${REVIEW_ID}.md 2>/c/tmp/codex-${REVIEW_ID}.err'))
        == native("C:/tmp/codex-r7.err"),
        codex_lane.stderr_file_of(native('REVIEW_ID=r7; x 2>/c/tmp/codex-${REVIEW_ID}.err')),
    )
    # The fallback globs the review command's capture directory, C:/tmp (Git Bash's /c/tmp) or
    # /tmp on Linux, by its absolute path, so it finds a capture whatever directory the hook runs
    # in. Spelled apart from CAPTURE_GLOB, the check fails when the glob drifts from the command;
    # the cases below show the fallback finding a capture in the directory its glob names.
    check("the capture fallback looks where the review command writes its captures",
          codex_lane.CAPTURE_GLOB == native("C:/tmp") + "/codex-*.err", codex_lane.CAPTURE_GLOB)
    # The cases below plant refusals, which a real session's fallback would read as its own
    # outage in the shared directory: they run in a directory of the suite's own.
    capture_dir = tempfile.mkdtemp(prefix=RUN + "_captures_")
    real_capture_glob = codex_lane.CAPTURE_GLOB
    codex_lane.CAPTURE_GLOB = os.path.join(capture_dir, "codex-*.err")
    stale = os.path.join(capture_dir, "codex-gate-test-stale.err")
    fresh = os.path.join(capture_dir, "codex-gate-test-fresh.err")
    try:
        launch = time.time()
        with open(stale, "w", encoding="utf-8") as stream:
            stream.write("ERROR: You've hit your usage limit. try again at 3:30 PM.\n")
        os.utime(stale, (launch - 600, launch - 600))
        with open(fresh, "w", encoding="utf-8") as stream:
            stream.write("VERDICT: APPROVED\ntokens used\n1 234\n")
        os.utime(fresh, (launch + 5, launch + 5))
        codex_lane.clear_state()
        check(
            "an unresolved redirect falls back to the newest capture written after the launch",
            codex_lane.record_from_command("codex exec - < p.md 2>/c/tmp/codex-${UNSET_ID}.err", "", started=launch) is False
            and codex_lane.status()[0] is True,
            codex_lane.status(),
        )
        with open(fresh, "w", encoding="utf-8") as stream:
            stream.write("ERROR: Selected model is at capacity. Please try a different model.\ntokens used\n1 234\n")
        os.utime(fresh, (launch + 6, launch + 6))
        check(
            "the newest capture's refusal is recorded",
            codex_lane.record_from_command("codex exec - < p.md 2>/c/tmp/codex-${UNSET_ID}.err", "", started=launch) is True
            and codex_lane.status()[0] is False,
            codex_lane.status(),
        )
        codex_lane.clear_state()
        check(
            "a capture older than the launch is never read",
            codex_lane.record_from_command("codex exec - < p.md 2>/c/tmp/codex-${UNSET_ID}.err", "", started=launch + 60) is False,
            codex_lane.status(),
        )
    finally:
        codex_lane.CAPTURE_GLOB = real_capture_glob
        shutil.rmtree(capture_dir, ignore_errors=True)
    codex_lane.write_state({"unavailable_until": "garbage", "reason": "x"})
    check("a corrupt record reads as available", codex_lane.status()[0] is True, codex_lane.status())
    codex_lane.clear_state()
    sid = session()
    try:
        result = run(MARK_HOOK, {
            "session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "timeout 3600 codex exec --ignore-user-config - < /c/tmp/p.md  # CODE_WORK_GATE_REVIEW"},
            "tool_response": {"stdout": "", "stderr": "ERROR: Selected model is at capacity. Please try a different model."},
            "cwd": AGENT_HOME,
        })
        check("the marker hook records an outage from the tool output", result.get("continue") is True and codex_lane.status()[0] is False, codex_lane.status())
    finally:
        cleanup(sid)
finally:
    codex_lane.clear_state()

HARNESS_TRAILER = (
    "\nagentId: acfcde3916804b008 (use SendMessage with to: 'acfcde3916804b008',"
    " summary: '<5-10 word recap>' to continue this agent)"
    "\n<usage>subagent_tokens: 45848\ntool_uses: 9\nduration_ms: 154586</usage>"
)

FENCE = chr(96) * 3
QUOTED_TRAILER = (
    "The trailer this hook has to strip looks like:\n"
    "<usage>subagent_tokens: 45848\ntool_uses: 9</usage>\n"
)
FENCED_QUOTED_TRAILER = (
    "The trailer this hook has to strip looks like:\n"
    "```\n<usage>subagent_tokens: 45848\ntool_uses: 9</usage>\n```\n"
)

for label, reviewer_text in (
    ("verdict with harness trailer", "No blockers.\nVERDICT: APPROVED" + HARNESS_TRAILER),
    (
        "verdict with usage-only trailer",
        "No blockers.\nVERDICT: APPROVED\n<usage>subagent_tokens: 1</usage>",
    ),
    (
        "verdict after an unfenced quoted trailer",
        QUOTED_TRAILER + "No blockers.\nVERDICT: APPROVED" + HARNESS_TRAILER,
    ),
    (
        "verdict after a fenced quoted trailer",
        FENCED_QUOTED_TRAILER + "No blockers.\nVERDICT: APPROVED" + HARNESS_TRAILER,
    ),
    (
        "verdict after a quoted trailer with no harness trailer",
        QUOTED_TRAILER + "No blockers.\nVERDICT: APPROVED",
    ),
    (
        "verdict the Codex CLI printed twice",
        codex_cli_output("No blockers.\nVERDICT: APPROVED"),
    ),
    (
        "verdict restated in another case",
        codex_cli_output("No blockers.\nVERDICT: APPROVED").replace(
            "VERDICT: APPROVED", "VERDICT: approved", 1
        ),
    ),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_review(events, 130, "review-{}".format(label), reviewer_text)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; review passed",
        })
        check(
            "{} is accepted".format(label),
            result.get("continue") is True and "decision" not in result,
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

for label, reviewer_text in (
    ("fenced verdict", "```\nVERDICT: APPROVED\n```"),
    ("trailing verdict prose", "VERDICT: APPROVED\nextra prose"),
    ("conflicting verdict values", "VERDICT: REVISE\nVERDICT: APPROVED"),
    (
        "two control kinds",
        "CLOSURE_VALIDATION: BLOCKED\nprose\nVERDICT: APPROVED",
    ),
    (
        "repeated verdict followed by prose",
        codex_cli_output("VERDICT: APPROVED") + "\nextra prose",
    ),
    (
        # Doubling makes the fences of an unbalanced result add up, which would expose the
        # second copy's verdict and hide the first copy's inside the fence.
        "unbalanced fence the CLI doubled into a balanced one",
        codex_cli_output("Reviewed the candidate.\n" + FENCE + "\nVERDICT: APPROVED"),
    ),
    (
        "prose after harness trailer",
        "VERDICT: APPROVED" + HARNESS_TRAILER + "\nextra prose",
    ),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_review(events, 130, "review-malformed-{}".format(label), reviewer_text)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; review passed",
        })
        check("{} is rejected".format(label), result.get("decision") == "block", result)
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-approved", "VERDICT: APPROVED")
    add_review(events, 132, "review-malformed-after", "Completed without a verdict.")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review passed",
    })
    check("malformed success after approval is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 129, "closure-too-early", "CLOSURE_VALIDATION: READY")
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("closure before escalate is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    events.append(agent_use(
        130, "adversarial-reviewer", "review-background", run_in_background=True
    ))
    events.append(tool_result(
        130.5, "review-background", "No blockers.\nVERDICT: APPROVED"
    ))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review passed",
    })
    check("explicit background review is not evidence", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    events.append(agent_use(
        130, "adversarial-reviewer", "review-omitted-mode", run_in_background=None
    ))
    events.append(tool_result(
        130.5, "review-omitted-mode", "No blockers.\nVERDICT: APPROVED"
    ))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review passed",
    })
    check("omitted review mode is not foreground evidence", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    add_review(events, 136, "closure-ready", "CLOSURE_VALIDATION: READY")
    add_review(events, 138, "closure-malformed-after", "Closure finished.")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("malformed success after closure ready is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-approved", "VERDICT: APPROVED")
    add_review(events, 132, "review-failed-after-approval", "model unavailable", is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review passed",
    })
    check("review failure after approval is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-optional",
                     'node "codex-companion.mjs" adversarial-review "--wait optional"',
                     "Codex unavailable", is_error=True)
    add_review(events, 130, "review-native", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; native review passed",
    })
    check("optional external failure does not block native gate", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-required-success",
                     REQUIRED_CODEX_COMMAND,
                     review_text("APPROVED"), run_in_background=False)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; required Codex review passed",
    })
    check("required external verdict passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# A required cross-engine call that answers without a verdict is an unavailable reviewer, not a
# satisfied requirement — the native lane's own approval cannot stand in for it.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-required-verdictless",
                     REQUIRED_CODEX_COMMAND,
                     "External review completed.", run_in_background=False)
    add_review(events, 130, "review-native", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; native review passed",
    })
    check("a verdictless required external result is not evidence", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-required-failure",
                     REQUIRED_CODEX_COMMAND,
                     "External review failed.", is_error=True, run_in_background=False)
    add_review(events, 130, "review-native", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; native review passed",
    })
    check("required external failure rejects verified", result.get("decision") == "block", result)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: required Codex unavailable",
    })
    check("required external failure allows draft-blocked", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# Codex is the primary review lane: its verdict satisfies HIGH on its own, in the shape the
# harness actually records — a foreground shell call carries no mode field at all.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-primary", CODEX_COMMAND, review_text("APPROVED"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex reviewed the auth candidate",
    })
    check("a Codex verdict alone satisfies HIGH", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-cli",
                     "Get-Content -Raw C:\\tmp\\packet.md | codex exec --json -m gpt-6-sol - "
                     "# CODE_WORK_GATE_REVIEW",
                     review_text("APPROVED"), tool="PowerShell")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex CLI reviewed the candidate",
    })
    check("the Codex CLI lane counts through any shell", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    # What the CLI really returns: the reviewer's message, the footer, then the message again.
    # The rollout log holds it once, which is what the result's tail has to bind to.
    add_codex_review(events, 128, "codex-cli-twice",
                     "codex exec --sandbox read-only - < /c/tmp/packet.md "
                     "# CODE_WORK_GATE_REVIEW",
                     codex_cli_output(review_text("APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved the auth candidate",
    })
    check("the CLI printing its verdict twice still counts as one review",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    # Repetition is tolerated, disagreement is not: a REVISE that the tail turns into an
    # APPROVED states two things and is no reviewer result at all.
    add_codex_review(events, 128, "codex-cli-disagreeing",
                     "codex exec --sandbox read-only - < /c/tmp/packet.md "
                     "# CODE_WORK_GATE_REVIEW",
                     review_text("REVISE")
                     + "\nhook: Stop\ntokens used\n1\n"
                     + review_text("APPROVED"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved the auth candidate",
    })
    check("two different verdicts in one result are still malformed",
          result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    # Closure validation runs through the same CLI, so it arrives doubled the same way.
    add_codex_review(events, 128, "codex-r1", CODEX_CLI_COMMAND, review_text("REVISE"))
    add_codex_review(events, 130, "codex-r2", CODEX_CLI_COMMAND, review_text("REVISE"))
    add_codex_review(events, 132, "codex-r3", CODEX_CLI_COMMAND, review_text("ESCALATE"))
    add_codex_review(events, 134, "codex-closure", CODEX_CLI_COMMAND,
                     codex_cli_output(closure_text("READY")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate-codex-lane",
    })
    check("a doubled closure validation counts once",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    # A real Codex run whose message leaves a fence open: doubling balances the fence count, so
    # only the second copy's verdict is visible. That is not a well-formed reviewer result.
    add_codex_review(events, 128, "codex-unbalanced-fence", CODEX_CLI_COMMAND,
                     codex_cli_output(review_text("APPROVED").replace(
                         "VERDICT: APPROVED", FENCE + "\nVERDICT: APPROVED")))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved the auth candidate",
    })
    check("doubling never balances an unbalanced fence into a verdict",
          result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# A verdict a command printed while no Codex process ran is not a review, whatever the command
# says — echoed text, a heredoc body, an escaped separator, a file read, a retrieved old job.
for label, command, shell in (
    ("an echoed verdict", "Write-Output 'VERDICT: APPROVED' # codex exec", "PowerShell"),
    ("an escaped separator", "Write-Output ignored `| codex exec 'VERDICT: APPROVED'", "PowerShell"),
    ("a heredoc body", "cat <<'EOF'\ncodex exec\nVERDICT: APPROVED\nEOF", "Bash"),
    ("a file read", "cat /c/tmp/old-review.md  # codex exec", "Bash"),
    ("a retrieved job", 'node "codex-companion.mjs" result cx_9f21', "Bash"),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_codex_review(events, 128, "codex-forged", command,
                         review_text("APPROVED"),
                         tool=shell, codex_ran=False)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex approved",
        })
        check("{} cannot supply a Codex verdict".format(label), result.get("decision") == "block", result)
    finally:
        cleanup(sid, locals().get("transcript"))

# The inverse error: a real Codex review must count whatever the command line looks like.
for label, command in (
    ("a quoted executable path", '"/usr/local/bin/codex" exec --json - # CODE_WORK_GATE_REVIEW'),
    ("a subshell", "OUT=$(codex exec --json - < /c/tmp/p.md) # CODE_WORK_GATE_REVIEW"),
    ("a wrapper script", "bash /c/tmp/run-review.sh CODE_WORK_GATE_REVIEW"),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_codex_review(events, 128, "codex-real", command, review_text("APPROVED"))
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex approved the candidate",
        })
        check("{} still counts as a review".format(label), result.get("continue") is True and "decision" not in result, result)
    finally:
        cleanup(sid, locals().get("transcript"))

# A Codex run that finished before this call opened belongs to an earlier candidate.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-stale", CODEX_CLI_COMMAND,
                     review_text("APPROVED"), codex_ran=False)
    log_codex_run(60.0, review_text("APPROVED"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved",
    })
    check("a Codex run outside the call window is not this review", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# Ambient Codex activity is not provenance: a concurrent rescue, or another session on the same
# machine, cannot vouch for output it never produced.
for label, logged in (
    ("an unrelated rescue", "Applied the patch and reran the tests."),
    ("a foreign candidate's review", review_text("APPROVED", "another repository")),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_codex_review(events, 128, "codex-launder", CODEX_CLI_COMMAND,
                         review_text("APPROVED"),
                         codex_ran=False)
        log_codex_run(128.4, logged)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex approved",
        })
        check("{} cannot vouch for another call's verdict".format(label), result.get("decision") == "block", result)
    finally:
        cleanup(sid, locals().get("transcript"))

# The session must have SAID it, inside this call: the same log also holds the prompt the call
# piped in, and a resumed session still holds every review it wrote before.
for label, kwargs in (
    ("text the call supplied as the prompt", {"role": "developer"}),
    ("text from the older part of a resumed session", {"said_at": 60.0}),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        verdict = review_text("APPROVED")
        add_codex_review(events, 128, "codex-echo", CODEX_CLI_COMMAND,
                         verdict, codex_ran=False)
        log_codex_run(128.4, verdict, **kwargs)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex approved",
        })
        check("{} is not a verdict".format(label), result.get("decision") == "block", result)
    finally:
        cleanup(sid, locals().get("transcript"))

# A result carrying nothing but the verdict identifies no review: it matches any session that
# ended the same way. And a record written before the call opened is an earlier one replayed,
# however narrow the gap.
for label, result_text_, log_kwargs in (
    ("a result that says only the verdict", "VERDICT: APPROVED", {}),
    ("a record from just before the call", review_text("APPROVED"), {"said_at": 124.5}),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_codex_review(events, 128, "codex-thin", CODEX_CLI_COMMAND,
                         result_text_, codex_ran=False)
        log_codex_run(128.4, result_text_, **log_kwargs)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex approved",
        })
        check("{} cannot bind".format(label), result.get("decision") == "block", result)
    finally:
        cleanup(sid, locals().get("transcript"))

# A verdict that cannot be attributed is not silently dropped: it is review activity that
# failed, so an approval before it no longer stands as the last word.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 120, "review-native", review_text("APPROVED"))
    add_codex_review(events, 128, "codex-unbound", CODEX_CLI_COMMAND,
                     review_text("REVISE"), codex_ran=False)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; native review approved it",
    })
    check("an unattributable verdict reopens an earlier approval", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# Only a call that declares itself the review lane is heard at all. An unrelated command whose
# output happens to end in a control line must not touch the ledger — least of all after a
# closure has landed, where there would be no way left to recover.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100.0, last_ts=110.0)
    events = base_events(include_simplify=True)
    add_review(events, 120, "review-1", review_text("REVISE"))
    add_review(events, 122, "review-2", review_text("REVISE"))
    add_review(events, 124, "review-3", review_text("ESCALATE"))
    add_review(events, 126, "closure-1", "Recovery checked.\n\nCLOSURE_VALIDATION: READY")
    add_codex_review(events, 130, "stray-cat", "cat /c/tmp/old-review.md",
                     review_text("REVISE"), is_error=True, codex_ran=False)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch owned/auth-session, handoff in the PR body",
    })
    check("an unrelated command cannot strand a completed closure",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# The brief has to be the whole role and it has to come first: a session quoting the opening
# lines, or one whose output predates the brief, has not been given the reviewer's definition.
for label, kwargs in (
    ("only the opening of the role", {"partial_role": True}),
    ("a brief that arrives after the output", {"briefed_at": 200.0}),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        add_codex_review(events, 128, "codex-half-briefed", CODEX_CLI_COMMAND,
                         review_text("APPROVED"), codex_ran=False)
        log_codex_run(128.4, review_text("APPROVED"), **kwargs)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: HIGH; Codex approved",
        })
        check("{} is not a briefing".format(label), result.get("decision") == "block", result)
    finally:
        cleanup(sid, locals().get("transcript"))

# A resumed session outgrows any read budget, and its rounds are spread through the log: each
# call has to find its own, whether it sits before the bulk or after it.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    rounds = [(126.0, review_text("REVISE", "round one of the same candidate")),
              (127.0, review_text("REVISE", "round two of the same candidate")),
              (128.0, review_text("REVISE", "round three of the same candidate"))]
    last = review_text("APPROVED", "round four of the same candidate")
    for at, text in rounds:
        add_codex_review(events, at, "codex-r{}".format(int(at)), CODEX_CLI_COMMAND, text,
                         codex_ran=False)
    add_codex_review(events, 129, "codex-last", CODEX_CLI_COMMAND, last, codex_ran=False)
    log_codex_run(129.4, last, filler_bytes=gate.CODEX_HEAD_BYTES, briefed_at=125.0,
                  earlier=[(at + 0.4, text) for at, text in rounds])
    transcript = write_transcript(events)
    gate._CODEX_RUNS.update(since=None, files=[], budget=gate.CODEX_SCAN_BUDGET)
    gate._CODEX_SAID.clear()
    verdicts = [v for _, v in gate.transcript_evidence(transcript, 100.0, 100.0)["ordinary_reviews"]]
    check("rounds behind the bulk of a resumed log are still counted",
          verdicts == ["REVISE", "REVISE", "REVISE", "APPROVED"], verdicts)
finally:
    cleanup(sid, locals().get("transcript"))

# An errored call whose review is provably a real one is heard as failed activity even unmarked:
# the marker only matters when nothing can attribute the result.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 120, "review-native", review_text("APPROVED"))
    add_codex_review(events, 128, "codex-bound-error", "codex exec --json - < /c/tmp/p.md",
                     review_text("REVISE"), is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; native review approved it",
    })
    check("a bound errored verdict is heard without the marker", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# A session that was never given the reviewer role is not a review, whatever it produced: this
# is the Codex lane's equivalent of the harness delivering the native reviewer's definition.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-unbriefed", CODEX_CLI_COMMAND,
                     review_text("APPROVED"), codex_ran=False)
    log_codex_run(128.4, review_text("APPROVED"), briefed=False)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved",
    })
    check("a session never given the reviewer role is not a review", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# A failed call still stated an opinion: the exit status decides whether it can be trusted as a
# verdict, never whether it is heard at all.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 120, "review-native", review_text("APPROVED"))
    add_codex_review(events, 128, "codex-errored", CODEX_CLI_COMMAND,
                     review_text("REVISE"), is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; native review approved it",
    })
    check("an errored verdict still reopens an earlier approval", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# The recovery direction: a bound Codex approval after a native REVISE is an ordinary round and
# closes the gate. Asserting a pass is what makes this fixture prove the binding worked.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 120, "review-native", review_text("REVISE"))
    add_codex_review(events, 128, "codex-bound", CODEX_CLI_COMMAND,
                     review_text("APPROVED", "the same candidate, after remediation"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved the remediation",
    })
    check("a bound Codex approval closes a round the native lane opened",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# A relevant transcript line that cannot be decoded leaves the record incomplete where verdicts
# live, so the approval before it is not the last word.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 128, "review-native", review_text("APPROVED"))
    transcript = write_transcript(events)
    with open(transcript, "a", encoding="utf-8") as stream:
        stream.write('{"type": "user", "timestamp": "' + iso(130) +
                     '", "message": {"role": "user", "content": [{"type": "tool_result"\n')
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; approved before the truncated line",
    })
    check("a truncated result line stops the scan from approving", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-piped",
                     "cd /c/repo && timeout 3600 codex exec --json - < /c/tmp/packet.md "
                     "| tee /c/tmp/out.txt  # CODE_WORK_GATE_REVIEW",
                     review_text("APPROVED"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex approved the candidate",
    })
    check("a real Codex invocation inside a pipeline still counts", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 128, "review-native", review_text("APPROVED"))
    events.append(entry(129, "assistant", [{
        "type": "tool_use", "id": "broken-block", "name": "Bash", "input": "not-an-object",
    }]))
    add_review(events, 130, "review-late", "New blocker.\n\nVERDICT: REVISE")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; approved before the malformed block",
    })
    check("a malformed block does not hide the reviews after it", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-detached",
                     'node "codex-companion.mjs" adversarial-review "--background"',
                     review_text("APPROVED"), run_in_background=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex reviewed the candidate",
    })
    check("a detached Codex launch supplies no verdict", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# A Codex call is an errand until its result carries the verdict line, so ordinary CLI use
# neither approves a candidate nor counts as review activity around a terminal verdict.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 129, "codex-errand", CODEX_ERRAND_COMMAND,
                     "Three suggestions, no verdict.")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex reviewed the candidate",
    })
    check("a verdictless Codex errand approves nothing", result.get("decision") == "block", result)

    events = base_events(include_simplify=True)
    add_review(events, 128, "review-native", "VERDICT: APPROVED")
    add_codex_review(events, 131, "codex-after", CODEX_ERRAND_COMMAND, "Style notes only.")
    transcript2 = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript2,
        "last_assistant_message": "[gate] verified: HIGH; native review passed",
    })
    check("a verdictless Codex errand does not reopen a closed review", result.get("continue") is True and "decision" not in result, result)
    cwg.remove(transcript2)
finally:
    cleanup(sid, locals().get("transcript"))

# One ledger across engines: a Codex round and a native round share the budget and the
# terminal-verdict guards, so switching engines mid-gate neither resets nor duplicates it.
sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-revise",
                     CODEX_COMMAND,
                     review_text("REVISE"))
    add_review(events, 130, "review-native", "Blocker fixed.\n\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; Codex round then native approval",
    })
    check("engines share one review ledger", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_codex_review(events, 128, "codex-first",
                     CODEX_COMMAND,
                     review_text("APPROVED"))
    add_review(events, 130, "review-extra", "Second opinion.\n\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; two approvals",
    })
    check("a native round after a Codex approval is still an illegal continuation", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=150)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-stale", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; auth tests and review passed",
    })
    check("stale high approval blocks", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=140)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-retired-approval", "VERDICT: APPROVED")
    add_review(events, 150, "review-current-approval", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; re-reviewed after the final edit",
    })
    check("approval retired by an edit can be renewed", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=200)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-retired-approval", "VERDICT: APPROVED")
    add_review(events, 210, "review-open-revise", "VERDICT: REVISE")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review passed",
    })
    check("revise after a retired approval still blocks", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=140)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 150, "review-2", "VERDICT: REVISE")
    add_review(events, 152, "review-3", "VERDICT: REVISE")
    add_review(events, 154, "review-4", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review eventually approved",
    })
    check("an edit between revisions does not reset the round counter", result.get("decision") == "block", result)
    check(
        "round overflow is explained",
        "MAX_REVIEW_ROUNDS" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=200)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    add_review(events, 210, "closure-1", "CLOSURE_VALIDATION: READY")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("remediation edits between escalate and closure keep closure reachable", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=200)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    add_review(events, 136, "closure-1", "CLOSURE_VALIDATION: BLOCKED")
    add_review(events, 210, "closure-2", "CLOSURE_VALIDATION: READY")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("a second closure pass survives its own remediation edit", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"], first_ts=100, last_ts=200)
    events = base_events()
    add_review(events, 130, "review-open", "VERDICT: REVISE")
    add_review(events, 132, "closure-rogue", "CLOSURE_VALIDATION: BLOCKED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check("a closure verdict cannot erase an open revise", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

for paths, receipt, label in (
    (["C:/repo/src/auth/session.ts"], "[gate] verified: HIGH; checks passed", "high"),
    (["C:/repo/src/app.py"], "[gate] verified: STANDARD; checks passed", "standard"),
):
    sid = session()
    try:
        seed(sid, paths, first_ts=100, last_ts=200)
        events = base_events(include_simplify=True)
        add_review(events, 130, "review-1", "VERDICT: REVISE")
        add_review(events, 132, "review-2", "VERDICT: REVISE")
        add_review(events, 134, "review-3", "VERDICT: ESCALATE")
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": receipt,
        })
        check(
            "editing past an escalate cannot reach verified unreviewed: {}".format(label),
            result.get("decision") == "block",
            result,
        )
        check(
            "escalate still demands closure: {}".format(label),
            "requires autonomous closure" in result.get("reason", ""),
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=200)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    add_review(events, 210, "review-fresh", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; fourth round approved",
    })
    check("an edit cannot convert an escalate into a fresh approval round", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100, last_ts=150)
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-failed-stale", "model unavailable", is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: reviewer unavailable",
    })
    check("stale reviewer failure is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    add_review(events, 136, "closure-ready", "CLOSURE_VALIDATION: READY")
    add_review(events, 138, "closure-failed-after-ready", "model unavailable", is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("review failure after closure ready is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-approved", "VERDICT: APPROVED")
    add_review(events, 132, "review-after-approval", "VERDICT: REVISE")
    add_review(events, 134, "review-illegal-escalate", "VERDICT: ESCALATE")
    add_review(events, 136, "closure-illegal", "CLOSURE_VALIDATION: READY")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("review cannot continue after approval", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "HIGH-1 open.\nVERDICT: REVISE")
    add_review(events, 132, "review-2", "HIGH-1 remains.\nVERDICT: REVISE")
    add_review(events, 134, "review-3", "HIGH-1 remains.\nVERDICT: ESCALATE")
    add_review(events, 136, "closure-1", "All blockers resolved.\nCLOSURE_VALIDATION: READY")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: https://github.com/example/repo/pull/42",
    })
    check("round-3 escalate plus ready closure passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: ESCALATE")
    add_review(events, 136, "closure-ready", "CLOSURE_VALIDATION: READY")
    add_review(events, 138, "closure-after-ready", "CLOSURE_VALIDATION: BLOCKED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: draft branch",
    })
    check("closure cannot continue after ready", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-early", "HIGH-1 open.\nVERDICT: ESCALATE")
    add_review(events, 132, "closure-early", "Resolved.\nCLOSURE_VALIDATION: READY")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] pr-ready: branch review/gate",
    })
    check("early escalate is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

# An ESCALATE before round 3 is that round's REVISE, and the review goes on (report 6d8e2c4c).
for label, reviews, receipt, passes, reason in (
    ("a round after an early ESCALATE may approve, closure packets between them being review activity",
     [(130, "HIGH-1 open.\nVERDICT: REVISE"), (132, "HIGH-1 remains.\nVERDICT: ESCALATE"),
      (134, "CLOSURE_VALIDATION: BLOCKED"), (136, "CLOSURE_VALIDATION: READY"), (138, "VERDICT: APPROVED")],
     VERIFIED_HIGH, True, ""),
    ("an early ESCALATE still leaves round 3 to escalate",
     [(130, "VERDICT: ESCALATE"), (132, "VERDICT: REVISE"), (134, "VERDICT: ESCALATE"),
      (136, "CLOSURE_VALIDATION: READY")], PR_READY, True, ""),
    ("an early ESCALATE opens no closure of its own",
     [(130, "VERDICT: REVISE"), (132, "VERDICT: ESCALATE"), (134, "CLOSURE_VALIDATION: READY")],
     PR_READY, False, "requires round-3 ESCALATE"),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"])
        events = base_events(include_simplify=True)
        for stamp, text in reviews:
            add_review(events, stamp, "review-{}".format(stamp), text)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": receipt})
        check(label, (result.get("continue") is True and "decision" not in result) if passes
              else (result.get("decision") == "block" and reason in result.get("reason", "")), result)
    finally:
        cleanup(sid, locals().get("transcript"))
check("the rounds a block reads name an early ESCALATE",
      "counts as that round's REVISE" in gate.rounds_read(
          {"ordinary_reviews": [(1790000000.0, "REVISE"), (1790000060.0, "ESCALATE")]}, marker_hook.clock),
      "rounds")

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "HIGH-1 open.\nVERDICT: REVISE")
    add_review(events, 132, "review-2", "HIGH-1 remains.\nVERDICT: REVISE")
    add_review(events, 134, "review-3", "HIGH-1 remains.\nVERDICT: ESCALATE")
    add_review(events, 136, "closure-1", "External secret is required.\nCLOSURE_VALIDATION: BLOCKED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: draft PR 43; staging secret unavailable",
    })
    check("blocked closure produces draft terminal", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# A READY that covers the candidate again once its content came back stays terminal: a later stale
# READY retires nothing through it (G20 review, round 1).
sid = session()
try:
    reverted = seed(sid, ["C:/repo/src/auth/session.ts"], last_ts=139.0, durable_ts=139.0)
    reverted["content_marks"] = [{"ts": 120.0, "fp": "a"}, {"ts": 137.0, "fp": "b"}, {"ts": 139.0, "fp": "a"}]
    cwg.write_json(gate_paths(sid)[0], reverted)
    events = base_events(include_simplify=True)
    for stamp, text in ((130, "VERDICT: REVISE"), (132, "VERDICT: REVISE"), (134, "VERDICT: ESCALATE"),
                        (136, "CLOSURE_VALIDATION: READY"), (138, "CLOSURE_VALIDATION: READY"),
                        (140, "CLOSURE_VALIDATION: READY")):
        add_review(events, stamp, "reverted-{}".format(stamp), text)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript, "last_assistant_message": PR_READY})
    # Nothing retires, so the third pass is refused: the pass cap is checked before terminality.
    check("a READY current again stays terminal through a later stale one",
          result.get("decision") == "block" and "MAX_CLOSURE_PASSES" in result.get("reason", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

# A READY a later lasting change or barrier made stale retires with the passes before it; a fresh
# pass may then close the candidate (report 27c9dcd0). The change lands at 137.
for label, closures, durable_ts, receipt, passes, reason in (
    ("a fresh closure pass after a stale READY closes the candidate",
     [(136, "READY"), (138, "READY")], 137.0, PR_READY, True, ""),
    ("a stale READY alone still asks for a current one",
     [(136, "READY")], 137.0, PR_READY, False, "pr-ready lacks current CLOSURE_VALIDATION: READY"),
    ("a current READY stays terminal",
     [(136, "READY"), (138, "READY")], 110.0, PR_READY, False, "closure validation continued after terminal READY"),
    ("a stale READY does not stand against a later BLOCKED",
     [(136, "READY"), (138, "BLOCKED")], 137.0, "[gate] draft-blocked: draft branch; staging secret unavailable",
     True, ""),
    ("a READY past the pass cap retires nothing, so the cap cannot be reset",
     [(135, "BLOCKED"), (136, "BLOCKED"), (137, "READY"), (138, "READY")], 137.8, PR_READY, False,
     "MAX_CLOSURE_PASSES"),
):
    sid = session()
    try:
        seed(sid, ["C:/repo/src/auth/session.ts"], last_ts=durable_ts, durable_ts=durable_ts)
        events = base_events(include_simplify=True)
        for stamp, text in ((130, "VERDICT: REVISE"), (132, "VERDICT: REVISE"), (134, "VERDICT: ESCALATE")):
            add_review(events, stamp, "review-{}".format(stamp), text)
        for stamp, verdict in closures:
            add_review(events, stamp, "closure-{}".format(stamp), "CLOSURE_VALIDATION: " + verdict)
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": receipt})
        check(label, (result.get("continue") is True and "decision" not in result) if passes
              else (result.get("decision") == "block" and reason in result.get("reason", "")), result)
    finally:
        cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-failed", "model unavailable", is_error=True)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] draft-blocked: reviewer unavailable after bounded retry",
    })
    check("review failure can end as draft-blocked", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = base_events(include_simplify=True)
    add_review(events, 130, "review-1", "VERDICT: REVISE")
    add_review(events, 132, "review-2", "VERDICT: REVISE")
    add_review(events, 134, "review-3", "VERDICT: REVISE")
    add_review(events, 136, "review-4", "VERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; review eventually approved",
    })
    check("fourth ordinary review is rejected", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/app.py"])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; tests passed\ntrailing prose",
    })
    check("receipt must be final line", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    marker, _ = gate_paths(sid)
    result = run(MARK_HOOK, {
        "session_id": sid,
        "tool_input": {"file_path": "C:/repo/src/app.py"},
    })
    result = run(MARK_HOOK, {
        "session_id": sid,
        "tool_input": {"file_path": "C:/repo/src/helper.py"},
    })
    data = cwg.read_json(marker)
    check("marker collects candidate paths", len(data.get("paths") or []) == 2, data)
finally:
    cleanup(sid)

sid = session()
try:
    marker, _ = gate_paths(sid)
    run(MARK_HOOK, {
        "session_id": sid,
        "tool_input": {"file_path": "C:/repo/AGENTS.md"},
    })
    data = cwg.read_json(marker)
    check("root AGENTS.md opens a gate", data is not None, data)
finally:
    cleanup(sid)

sid = session()
try:
    marker, _ = gate_paths(sid)
    run(MARK_HOOK, {
        "session_id": sid,
        "tool_name": "Bash",
        "tool_input": {"command": "apply_patch < change.diff"},
    })
    check("shell apply_patch creates marker", os.path.exists(marker), marker)
finally:
    cleanup(sid)

sid = session()
try:
    marker, _ = gate_paths(sid)
    run(MARK_HOOK, {
        "session_id": sid,
        "tool_name": "Bash",
        "tool_input": {"command": "git diff --check"},
    })
    check("read-only shell command does not create marker", not os.path.exists(marker), marker)
finally:
    cleanup(sid)

for command in (
    "Remove-Item -LiteralPath file.txt",
    "Rename-Item -LiteralPath old.txt -NewName new.txt",
    "rm file.txt",
    "mv old.txt new.txt",
    "git mv old.txt new.txt",
    "cp old.txt new.txt",
    "touch created.txt",
    "truncate -s 0 file.txt",
    "printf data > file.txt",
    "python -c \"open('file.txt','w').write('x')\"",
    "node -e \"require('fs').writeFileSync('file.txt','x')\"",
):
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        run(MARK_HOOK, {
            "session_id": sid,
            "tool_name": "PowerShell" if "Item" in command else "Bash",
            "tool_input": {"command": command},
        })
        check("shell mutation is marked: {}".format(command), os.path.exists(marker), marker)
    finally:
        cleanup(sid)

for command in ("git status --short", "git diff --check", "echo rm"):
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        run(MARK_HOOK, {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        })
        check("read-only shell stays unmarked: {}".format(command), not os.path.exists(marker), marker)
    finally:
        cleanup(sid)

for command in (
    "npm test",
    "npm run typecheck",
    "npm run lint",
    "npm run build",
    "pytest -q",
    "python -m pytest -q",
    "go test ./...",
    "cargo check",
    "dotnet test",
    "node scripts/ux/validate-system.js",
):
    payload = {
        "tool_name": "PowerShell" if command.startswith("dotnet") else "Bash",
        "tool_input": {"command": command},
    }
    check(
        "known validation shell policy: {}".format(command),
        marker_hook.shell_policy(payload) == marker_hook.SHELL_VALIDATION,
        marker_hook.shell_policy(payload),
    )

for command in (
    "npm test && rm generated.txt",
    "npm test > result.txt",
    "npm test $(touch generated.txt)",
    "node scripts/release.js",
):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    check(
        "unsafe or unknown shell policy: {}".format(command),
        marker_hook.shell_policy(payload) == marker_hook.SHELL_UNKNOWN,
        marker_hook.shell_policy(payload),
    )

sid = session()
try:
    marker, _ = gate_paths(sid)
    paths = ["C:/repo/src/file_{:03d}.py".format(index) for index in range(129)]
    paths.insert(0, "C:/repo/src/authentication/session.ts")
    marker_hook.record_paths({"session_id": sid}, paths)
    data = cwg.read_json(marker)
    check("marker caps diagnostic paths", len(data.get("paths") or []) == 128, data)
    check("marker records path overflow", data.get("path_overflow") is True, data)
    check("risk survives diagnostic path truncation", data.get("minimum_risk_seen") == "HIGH", data)
finally:
    cleanup(sid)

sid = session()
try:
    marker, _ = gate_paths(sid)
    opened = time.time() - 60
    seed(sid, ["c:/repo/src/app.py"], first_ts=opened, last_ts=opened + 5)
    marker_hook.record_paths({"session_id": sid}, ["C:/repo/src/helper.py"])
    data = cwg.read_json(marker)
    check("a marker written before identity tracking keeps its cycle", data["first_ts"] == opened, data)
finally:
    cleanup(sid)

with tempfile.TemporaryDirectory(prefix="cwg_git_snapshot_") as repo:
    subprocess.run(["git", "init", "--quiet", repo], check=True)
    os.makedirs(os.path.join(repo, "src", "authentication"), exist_ok=True)
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as stream:
        stream.write(".env*\n.claude/settings.local.json\n")
    tracked = os.path.join(repo, "src", "authentication", "session.ts")
    with open(tracked, "w", encoding="utf-8") as stream:
        stream.write("export const value = 1;\n")
    subprocess.run([
        "git", "-C", repo, "add", "--", ".gitignore", "src/authentication/session.ts"
    ], check=True)
    subprocess.run([
        "git", "-C", repo,
        "-c", "user.name=Code Work Gate",
        "-c", "user.email=gate@example.invalid",
        "commit", "--quiet", "-m", "seed",
    ], check=True)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        payload = {
            "session_id": sid,
            "tool_use_id": "shell-mutates",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "python -c writer"},
        }
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        with open(tracked, "w", encoding="utf-8") as stream:
            stream.write("export const value = 2;\n")
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUseFailure"))
        data = cwg.read_json(marker)
        check("failed shell snapshot captures actual changed path", any(
            path.endswith("/src/authentication/session.ts")
            for path in data.get("paths") or []
        ), data)
        check("shell snapshot preserves actual path risk", data.get("minimum_risk_seen") == "HIGH", data)
    finally:
        cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        payload = {
            "session_id": sid,
            "tool_use_id": "shell-no-change",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "python -c no_change"},
        }
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
        data = cwg.read_json(marker)
        check("unobserved non-read-only shell command is conservatively marked", data is not None, data)
        check(
            "unobserved shell command is operational, not high",
            data.get("minimum_risk_seen") == "LOW"
            and cwg.work_class(data.get("paths") or []) == cwg.WORK_OPERATIONAL,
            data,
        )
    finally:
        cleanup(sid)

    for index, command in enumerate((
        "npm test",
        "npm run typecheck",
        "npm run lint",
        "npm run build",
        "pytest -q",
        "go test ./...",
        "node scripts/ux/validate-system.js",
    )):
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "validation-no-change-{}".format(index),
                "tool_name": "Bash",
                "cwd": repo,
                "tool_input": {"command": command},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            check(
                "unchanged validation does not open gate: {}".format(command),
                not os.path.exists(marker),
                cwg.read_json(marker),
            )
        finally:
            cleanup(sid)

    # A lasting path outside every repository is measured on its own now, so a clean command keeps
    # the anchor; one past the measured bound is still something no snapshot vouches for.
    with tempfile.TemporaryDirectory(prefix="cwg_cross_root_") as elsewhere:
        for count, expires in ((1, False), (marker_hook.MAX_LOOSE_FILES + 1, True)):
            sid = session()
            try:
                marker, _ = gate_paths(sid)
                durable = [os.path.join(elsewhere, "hooks", "gate{}.py".format(index)) for index in range(count)]
                approved_at = time.time() - 60
                check("seed a candidate with {} loose lasting path(s)".format(count), cwg.write_json(marker, {
                    "first_ts": approved_at - 60, "last_ts": approved_at, "last_durable_ts": approved_at,
                    "last_path": durable[-1], "edits": count,
                    "paths": [cwg.normalize_path(path) for path in durable],
                }))
                payload = {
                    "session_id": sid,
                    "tool_use_id": "clean-snapshot-elsewhere-{}".format(count),
                    "tool_name": "Bash",
                    "cwd": repo,
                    "tool_input": {"command": "python -c write_outside_repo"},
                }
                run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
                run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
                data = cwg.read_json(marker) or {}
                check(
                    "a clean command {} with {} loose lasting path(s)".format(
                        "expires the anchor" if expires else "keeps the anchor", count),
                    (data.get("last_durable_ts", 0) > approved_at) is expires,
                    data,
                )
            finally:
                cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        payload = {
            "session_id": sid,
            "tool_use_id": "validation-mutates-source",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "npm test"},
        }
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        with open(tracked, "w", encoding="utf-8") as stream:
            stream.write("export const value = 3;\n")
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
        data = cwg.read_json(marker)
        check("validation snapshot records actual source mutation", any(
            path.endswith("/src/authentication/session.ts")
            for path in data.get("paths") or []
        ), data)
        check("validation mutation preserves path risk", data.get("minimum_risk_seen") == "HIGH", data)
    finally:
        cleanup(sid)

    for relative in (".env", ".claude/settings.local.json"):
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-ignored-{}".format(relative.replace("/", "-")),
                "tool_name": "Bash",
                "cwd": repo,
                "tool_input": {"command": "echo placeholder > {}".format(relative)},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            target = os.path.join(repo, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("placeholder\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker)
            check("ignored write is marked: {}".format(relative), data is not None, data)
            check(
                "a gitignored shell write is operational: {}".format(relative),
                cwg.work_class(data.get("paths") or []) == cwg.WORK_OPERATIONAL,
                data,
            )
        finally:
            cleanup(sid)

    with tempfile.TemporaryDirectory(prefix="cwg_outside_repo_") as outside:
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-outside-repo",
                "tool_name": "Bash",
                "cwd": repo,
                "tool_input": {"command": "python -c outside_writer"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            with open(os.path.join(outside, "config.py"), "w", encoding="utf-8") as stream:
                stream.write("enabled = True\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker)
            check("outside-repository write is marked", data is not None, data)
            check(
                "outside-repository write is operational work",
                cwg.work_class(data.get("paths") or []) == cwg.WORK_OPERATIONAL,
                data,
            )
        finally:
            cleanup(sid)

def switch_branch(directory, branch):
    subprocess.run(["git", "-C", directory, "checkout", "--quiet", "-b", branch], check=True)


def commit_paths(directory, relative, message):
    subprocess.run(["git", "-C", directory, "add", "--", relative], check=True)
    subprocess.run([
        "git", "-C", directory,
        "-c", "user.name=Code Work Gate",
        "-c", "user.email=gate@example.invalid",
        "commit", "--quiet", "-m", message,
    ], check=True)


def candidate_repo(directory, branch):
    """Repository whose HEAD carries the candidate identity, seeded with one commit."""
    subprocess.run(["git", "init", "--quiet", directory], check=True)
    switch_branch(directory, branch)
    os.makedirs(os.path.join(directory, "src"), exist_ok=True)
    with open(os.path.join(directory, "src", "seed.py"), "w", encoding="utf-8") as stream:
        stream.write("value = 1\n")
    commit_paths(directory, "src/seed.py", "seed")


def mark_edit(sid, repo, relative, content="changed = True", python=sys.executable):
    """One gated edit reported through the marker hook, as PostToolUse delivers it."""
    target = os.path.join(repo, *relative.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as stream:
        stream.write(content + chr(10))
    run(MARK_HOOK, {
        "session_id": sid,
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": repo,
        "tool_input": {"file_path": target},
    }, python)
    return cwg.normalize_path(target)


def mark_shell(sid, repo, command, action=None, python=sys.executable):
    """A mutating shell call as production delivers it: the snapshot pair around the work."""
    payload = {
        "session_id": sid,
        "tool_use_id": "shell-{}".format(uuid.uuid4().hex),
        "tool_name": "Bash",
        "cwd": repo,
        "tool_input": {"command": command},
    }
    run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"), python)
    if action:
        action()
    run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"), python)


def age_marker(sid, seconds):
    """Move an open candidate into the past so evidence can be placed inside its window."""
    marker, _ = gate_paths(sid)
    data = cwg.read_json(marker)
    data["first_ts"] = float(data["first_ts"]) - seconds
    data["last_ts"] = float(data["last_ts"]) - seconds
    check("age marker", cwg.write_json(marker, data), data)
    return data


with tempfile.TemporaryDirectory(prefix="cwg_candidate_identity_") as repo:
    candidate_repo(repo, "candidate-one")

    # One command, two writes: the sync's copy of a skill tree and the session's own source.
    # Only the source belongs to the candidate, and asserting both halves keeps this from
    # passing through the unresolved-command fallback if the snapshot ever stops working.
    sid = session()
    try:
        synced = os.path.join(repo, ".agents", "skills", "charon-ux-design")
        os.makedirs(synced, exist_ok=True)
        own = os.path.join(repo, "src", "session_store.py")

        def write_both():
            with open(os.path.join(synced, "SKILL.md"), "w", encoding="utf-8") as stream:
                stream.write("# copied into every worktree by the sync" + chr(10))
            with open(own, "w", encoding="utf-8") as stream:
                stream.write("VALUE = 2" + chr(10))

        mark_shell(sid, repo, "npm test", action=write_both)
        paths = (cwg.read_json(gate_paths(sid)[0]) or {}).get("paths") or []
        check(
            "the session's own write is named alongside a sync",
            any(path.endswith("/src/session_store.py") for path in paths),
            paths,
        )
        check(
            "a skill tree synced into the worktree is not the session's work",
            not any("/.agents/skills/" in path for path in paths),
            paths,
        )
    finally:
        cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        mark_edit(sid, repo, "src/avatar_cache.py")
        abandoned = age_marker(sid, 600)
        switch_branch(repo, "candidate-two")
        fresh_paths = [
            mark_edit(sid, repo, "src/max_client.py"),
            mark_edit(sid, repo, "src/max_state.py"),
            mark_edit(sid, repo, "src/max_errors.py"),
        ]
        data = cwg.read_json(marker)
        check(
            "a candidate abandoned without a receipt does not hold the window open",
            data["first_ts"] > abandoned["last_ts"],
            data,
        )
        check(
            "a new candidate does not inherit abandoned paths",
            sorted(data.get("paths") or []) == sorted(fresh_paths),
            data,
        )
        check(
            "the marker records the candidate identity",
            "candidate-two" in str(data.get("identity")),
            data,
        )

        opened = data["first_ts"]
        events = [skill_use(abandoned["first_ts"] + 1, "simplify", "abandoned-simplify")]
        events.append(skill_use(opened + 1, "development-verification", "fresh-dev"))
        events.append(skill_use(opened + 2, "simplify", "fresh-simplify-1"))
        stamp = simplify_wave(events, opened + 3, "fresh-lens", SIMPLIFY_LENSES)
        events.append(skill_use(stamp, "simplify", "fresh-simplify-2"))
        events.append(agent_use(stamp + 1, "simplify-quality-reviewer", "fresh-confirm"))
        events.append(tool_result(stamp + 1.5, "fresh-confirm", "Confirmed."))
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: STANDARD; checks passed for the new candidate",
        })
        check(
            "an abandoned candidate cannot spend the next candidate's simplify budget",
            result.get("continue") is True and "decision" not in result,
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-three")
        mark_edit(sid, repo, "src/media_url.py")
        abandoned = age_marker(sid, 600)
        switch_branch(repo, "candidate-four")
        mark_edit(sid, repo, "src/retry_policy.py")
        opened = cwg.read_json(marker)["first_ts"]
        events = []
        add_review(events, abandoned["first_ts"] + 1, "abandoned-review", "VERDICT: REVISE")
        events.append(skill_use(opened + 1, "development-verification", "fresh-dev"))
        events.append(skill_use(opened + 2, "simplify", "fresh-simplify"))
        simplify_wave(events, opened + 3, "fresh-lens", SIMPLIFY_LENSES)
        add_review(events, opened + 7, "fresh-review-1", "VERDICT: REVISE")
        add_review(events, opened + 8, "fresh-review-2", "VERDICT: REVISE")
        add_review(events, opened + 9, "fresh-review-3", "VERDICT: ESCALATE")
        add_review(events, opened + 10, "fresh-closure", "CLOSURE_VALIDATION: READY")
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] pr-ready: branch candidate-four",
        })
        check(
            "an abandoned review round does not count against the next candidate",
            result.get("continue") is True and "decision" not in result,
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-five")
        mark_edit(sid, repo, "src/upload_queue.py")
        abandoned = age_marker(sid, 600)
        switch_branch(repo, "candidate-six")
        mark_edit(sid, repo, "src/token_refresh.py")
        opened = cwg.read_json(marker)["first_ts"]
        events = []
        base = abandoned["first_ts"] + 1
        add_review(events, base, "abandoned-review-1", "VERDICT: REVISE")
        add_review(events, base + 1, "abandoned-review-2", "VERDICT: REVISE")
        add_review(events, base + 2, "abandoned-review-3", "VERDICT: ESCALATE")
        add_review(events, base + 3, "abandoned-closure-1", "CLOSURE_VALIDATION: BLOCKED")
        add_review(events, base + 4, "abandoned-closure-2", "CLOSURE_VALIDATION: BLOCKED")
        events.append(skill_use(opened + 1, "development-verification", "fresh-dev"))
        events.append(skill_use(opened + 2, "simplify", "fresh-simplify"))
        simplify_wave(events, opened + 3, "fresh-lens", SIMPLIFY_LENSES)
        add_review(events, opened + 7, "fresh-review-1", "VERDICT: REVISE")
        add_review(events, opened + 8, "fresh-review-2", "VERDICT: REVISE")
        add_review(events, opened + 9, "fresh-review-3", "VERDICT: ESCALATE")
        add_review(events, opened + 10, "fresh-closure-1", "CLOSURE_VALIDATION: BLOCKED")
        add_review(events, opened + 11, "fresh-closure-2", "CLOSURE_VALIDATION: READY")
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] pr-ready: branch candidate-six",
        })
        check(
            "an abandoned closure pass does not count against the next candidate",
            result.get("continue") is True and "decision" not in result,
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-seven")
        mark_edit(sid, repo, "src/same_candidate_one.py")
        opened = age_marker(sid, 600)
        mark_edit(sid, repo, "src/same_candidate_two.py")
        data = cwg.read_json(marker)
        check(
            "work continuing on one branch keeps a single window",
            data["first_ts"] == opened["first_ts"],
            data,
        )
        check("a continued candidate accumulates its paths", len(data.get("paths") or []) == 2, data)
        events = [skill_use(opened["first_ts"] + 1, "development-verification", "same-dev")]
        stamp = opened["first_ts"] + 2
        for index in range(3):
            stamp = simplify_wave(
                events, stamp, "same-wave-{}".format(index), SIMPLIFY_LENSES
            )
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] verified: STANDARD; checks passed",
        })
        check("the two-pass simplify cap still binds one candidate", result.get("decision") == "block", result)
        check(
            "the exhausted simplify cap is still explained",
            "pass cap" in result.get("reason", ""),
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-eight")
        mark_edit(sid, repo, "src/closure_cap.py")
        opened = age_marker(sid, 600)["first_ts"]
        events = [skill_use(opened + 1, "development-verification", "cap-dev")]
        events.append(skill_use(opened + 2, "simplify", "cap-simplify"))
        simplify_wave(events, opened + 3, "cap-lens", SIMPLIFY_LENSES)
        add_review(events, opened + 7, "cap-review-1", "VERDICT: REVISE")
        add_review(events, opened + 8, "cap-review-2", "VERDICT: REVISE")
        add_review(events, opened + 9, "cap-review-3", "VERDICT: ESCALATE")
        add_review(events, opened + 10, "cap-closure-1", "CLOSURE_VALIDATION: BLOCKED")
        add_review(events, opened + 11, "cap-closure-2", "CLOSURE_VALIDATION: BLOCKED")
        add_review(events, opened + 12, "cap-closure-3", "CLOSURE_VALIDATION: READY")
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {
            "session_id": sid,
            "transcript_path": transcript,
            "last_assistant_message": "[gate] pr-ready: branch candidate-eight",
        })
        check("the closure cap still binds one candidate", result.get("decision") == "block", result)
        check(
            "the exhausted closure cap is still explained",
            "MAX_CLOSURE_PASSES" in result.get("reason", ""),
            result,
        )
    finally:
        cleanup(sid, locals().get("transcript"))

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-nine")
        shared = mark_edit(sid, repo, "src/shared_scope.py")
        opened = age_marker(sid, 600)
        switch_branch(repo, "candidate-nine-published")
        mark_edit(sid, repo, "src/shared_scope.py")
        data = cwg.read_json(marker)
        check(
            "branching to publish the same files keeps the candidate window",
            data["first_ts"] == opened["first_ts"],
            data,
        )
        check("the republished path is not duplicated", data.get("paths") == [shared], data)
    finally:
        cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-ten")
        mark_edit(sid, repo, "src/stale_candidate.py")
        stale = age_marker(sid, marker_hook.CANDIDATE_IDLE_LIMIT + 600)
        mark_edit(sid, repo, "src/resumed_candidate.py")
        data = cwg.read_json(marker)
        check(
            "a candidate idle past the limit does not survive into a resumed session",
            data["first_ts"] > stale["last_ts"],
            data,
        )
    finally:
        cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-eleven")
        mark_edit(sid, repo, "src/abandoned_scope.py")
        mark_shell(sid, repo, "node scripts/release.js")
        abandoned = age_marker(sid, 600)
        mark_shell(
            sid, repo, "git checkout -b candidate-twelve",
            action=lambda: switch_branch(repo, "candidate-twelve"),
        )
        check(
            "an opaque shell mark does not settle the candidate comparison",
            cwg.read_json(marker)["first_ts"] == abandoned["first_ts"],
            cwg.read_json(marker),
        )
        fresh = mark_edit(sid, repo, "src/new_scope.py")
        data = cwg.read_json(marker)
        check(
            "the first disjoint edit after a branch switch opens the new candidate",
            data["first_ts"] > abandoned["last_ts"],
            data,
        )
        check("the new candidate carries only its own path", data.get("paths") == [fresh], data)
    finally:
        cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-thirteen")
        owned = mark_edit(sid, repo, "src/closure_scope.py")
        published = age_marker(sid, 600)
        mark_shell(
            sid, repo, "git checkout -b candidate-fourteen",
            action=lambda: switch_branch(repo, "candidate-fourteen"),
        )
        check(
            "branching to publish a candidate of file edits keeps its window",
            cwg.read_json(marker)["first_ts"] == published["first_ts"],
            cwg.read_json(marker),
        )
        mark_shell(
            sid, repo, "git commit -m owned-scope",
            action=lambda: commit_paths(repo, "src/closure_scope.py", "owned scope"),
        )
        data = cwg.read_json(marker)
        check(
            "committing the published scope keeps its window",
            data["first_ts"] == published["first_ts"],
            data,
        )
        check("the committed path is the candidate's own", owned in (data.get("paths") or []), data)
    finally:
        cleanup(sid)

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        switch_branch(repo, "candidate-overflowing")
        marker_hook.record_paths(
            {"session_id": sid, "cwd": repo},
            ["{}/src/file_{:03d}.py".format(repo, index) for index in range(129)],
        )
        overflowing = age_marker(sid, 600)
        check("the overflowing candidate is marked as such", overflowing["path_overflow"] is True, overflowing)
        switch_branch(repo, "candidate-past-overflow")
        mark_edit(sid, repo, "src/past_overflow.py")
        data = cwg.read_json(marker)
        check(
            "past the diagnostic path cap the candidate window is kept",
            data["first_ts"] == overflowing["first_ts"],
            data,
        )
    finally:
        cleanup(sid)

    with tempfile.TemporaryDirectory(prefix="cwg_linked_worktree_") as parent:
        worktree = os.path.join(parent, "published")
        subprocess.run([
            "git", "-C", repo, "worktree", "add", "--quiet", worktree, "-b", "linked-worktree"
        ], check=True)
        identity = marker_hook.candidate_identity(worktree)
        check(
            "a linked worktree resolves its own identity",
            identity is not None and identity.endswith("#refs/heads/linked-worktree"),
            identity,
        )
        check(
            "a linked worktree is a distinct candidate from its main tree",
            identity != marker_hook.candidate_identity(repo),
            identity,
        )
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", worktree], check=True)

    subprocess.run(["git", "-C", repo, "checkout", "--quiet", "--detach"], check=True)
    check("a detached HEAD reads as an unknown identity", marker_hook.candidate_identity(repo) is None)

with tempfile.TemporaryDirectory(prefix="cwg_no_repo_") as plain:
    check("a directory outside a repository has no identity", marker_hook.candidate_identity(plain) is None)

for path in (
    ".claude/skills/x/SKILL.md",
    ".agents/skills/x/SKILL.md",
    ".codex/agents/reviewer.md",
    ".env",
    ".env.local",
    "Dockerfile.prod",
):
    check("gated relative/config path: {}".format(path), cwg.is_gated(path), path)

for path in (
    "src/authentication.ts",
    "src/authorization.ts",
    "src/authn/session.ts",
    "src/authz/policy.ts",
    "src/permissions.ts",
    "src/credentials.ts",
    "db/migrations/001.sql",
    "src/securityPolicy.ts",
    "src/paymentService.ts",
):
    check("sensitive path is high: {}".format(path), gate.minimum_risk([path]) == "HIGH", path)

check("author is not auth", gate.minimum_risk(["src/author.ts"]) == "STANDARD")

for path in (
    native("C:/Users/in/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py"),
    native("C:/tmp/sid/scratchpad/push.py"),
    "/tmp/wipe.sh",
    "/var/tmp/rotate.py",
    native("C:/Users/in/.claude/state/checkpoints/proj.md"),
    native("C:/Users/in/.claude/plans/plan.md"),
):
    check("throwaway artifact is not gated: {}".format(path), not cwg.is_gated(path), path)
    check("throwaway artifact is not durable: {}".format(path), not cwg.durable_paths([path]), path)

for path in (
    native("C:/tmp/charon-whatsnew/backend/src/services/featureRegistry.ts"),
    native("C:/Users/in/AppData/Local/Temp/build-clone/src/app.py"),
):
    check("a working clone under a temp root stays gated: {}".format(path), cwg.is_gated(path), path)

check(
    "a scratch script alone is operational work",
    cwg.work_class(["c:/tmp/run.py", cwg.SHELL_MUTATION_PATH]) == cwg.WORK_OPERATIONAL,
)
check(
    "one repository file makes the candidate persistent",
    cwg.work_class([cwg.SHELL_MUTATION_PATH, "c:/repo/src/app.ts"]) == cwg.WORK_PERSISTENT,
)
check(
    "a scratch script does not raise risk",
    gate.minimum_risk(["c:/tmp/deploy-prod.py"]) == "LOW",
)

for path in (
    "C:/repo/.claude/hooks/gate.py",
    "C:/repo/.claude/agents/reviewer.md",
    "C:/repo/.claude/commands/ship.md",
    "C:/repo/.claude/skills/x/SKILL.md",
    "C:/repo/.claude/settings.json",
    "C:/repo/.claude/settings.local.json",
    "C:/repo/.mcp.json",
    "C:/repo/CLAUDE.md",
    "C:/repo/AGENTS.md",
):
    check("executable agent config stays high: {}".format(path), gate.minimum_risk([path]) == "HIGH", path)

for path in (
    "C:/repo/.claude/rules/operations.md",
    "C:/repo/.claude/decisions/007-support.md",
    "C:/repo/.claude/docs/runbook.md",
):
    check("agent-config prose is standard: {}".format(path), gate.minimum_risk([path]) == "STANDARD", path)

for path in (".env", ".env.production", "deploy/server.pem", "keys/id_ed25519"):
    check("secret file is high: {}".format(path), gate.minimum_risk([path]) == "HIGH", path)

check("environment plumbing is not a secret", gate.minimum_risk(["src/env.ts"]) == "STANDARD")

for path in (
    native("C:/Users/in/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py"),
    "/tmp/wipe.sh",
    native("C:/tmp/sid/scratchpad/push.py"),
):
    check("ephemeral matcher agrees with the gate: {}".format(path), cwg.is_ephemeral(path), path)

for path in (
    native("C:/tmp/charon-whatsnew/backend/src/app.ts"),
    native("C:/repo/.claude/state-machine/runner.py"),
    native("C:/repo/src/scratchpadding.ts"),
    native("C:/repo/.claude/plans/rollout.md"),
    native("C:/repo/.claude/state/registry.json"),
    "C:/backup/appdata/local/temp/keep.py",
):
    check("ephemeral matcher does not overreach: {}".format(path), not cwg.is_ephemeral(path), path)

for path in (
    native("C:/Users/in/.claude/state/checkpoints/proj.md"),
    native("C:/Users/in/.claude/plans/plan.md"),
    "/home/dev/.claude/state/checkpoints/proj.md",
):
    check("home bookkeeping is ephemeral: {}".format(path), cwg.is_ephemeral(path), path)

# A place is matched in the platform's case: on Windows `/TMP` and `.Claude` are `/tmp` and
# `.claude`, elsewhere they are other directories, and the files there are lasting.
for path in ("/TMP/wipe.sh", native("C:/Users/in/.Claude/state/checkpoints/proj.md")):
    check("a place pattern matches in the platform's case: {}".format(path),
          cwg.is_ephemeral(path) is cwg.CASE_FOLDED_PATHS, path)


# The harness's scratch tree at every depth, not only the scratchpad directory inside it.
SCRATCH_HELPERS = [
    native("C:/Users/in/AppData/Local/Temp/claude/bound_digests.py"),
    native("C:/Users/in/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py"),
    native("C:/Users/in/AppData/Local/Temp/claude/proj/sid/notes/helper.py"),
    "/tmp/claude/fix_r1.py",
]
for path in SCRATCH_HELPERS:
    check("agent scratch is ephemeral: {}".format(path), cwg.is_ephemeral(path), path)

check("a candidate built only of scratch helpers is operational",
      cwg.work_class(SCRATCH_HELPERS) == cwg.WORK_OPERATIONAL
      and cwg.durable_paths(SCRATCH_HELPERS) == [], SCRATCH_HELPERS)
# No snapshot answers for a temp tree, so a lasting path there expires every verdict the moment
# any write-capable command runs — which is how one `glab api` read killed an APPROVED.
check("scratch helpers leave nothing for outside_snapshot to flag",
      marker_hook.outside_snapshot(SCRATCH_HELPERS, ["C:/repo"], []) == [], SCRATCH_HELPERS)

for path in (
    "C:/tmp/claude-code-stack/hooks/code_work_gate_common.py",
    "C:/tmp/claude-code/src/app.ts",
    "C:/tmp/claudex/app.py",
):
    check("a directory merely starting with claude is not scratch: {}".format(path),
          not cwg.is_ephemeral(path), path)
    check("a directory merely starting with claude stays gated: {}".format(path),
          cwg.is_gated(path), path)


# A chip's git worktree is created under the configuration home, but it is a checkout of a
# project rather than the agent's own bookkeeping. Every file in it is graded by path exactly
# as the same file is in an ordinary worktree; before that, a delegated session's whole diff
# looked ephemeral, so its candidate stayed OPERATIONAL and no review could close it.
CHIP_TREE = native("C:/Users/in/.claude/state/chips/trees/wa-tg-tun-new-fix-a1b2c3d4")
ORDINARY_TREE = native("C:/Users/in/Desktop/Projects/wa-tg-tun-new")
for relative in (
    "backend/src/core/ConnectionManager.ts",
    "hooks/code_work_gate_common.py",
    "docker-compose.yml",
    "CLAUDE.md",
):
    chip_path = "{}/{}".format(CHIP_TREE, relative)
    ordinary_path = "{}/{}".format(ORDINARY_TREE, relative)
    check(
        "a chip worktree holds gated source: {}".format(relative),
        cwg.is_gated(chip_path) and not cwg.is_ephemeral(chip_path),
        chip_path,
    )
    check(
        "a chip worktree file is a lasting artifact: {}".format(relative),
        cwg.work_class([chip_path]) == cwg.WORK_PERSISTENT,
        chip_path,
    )
    check(
        "an ordinary worktree is graded exactly the same: {}".format(relative),
        cwg.is_gated(ordinary_path)
        and gate.minimum_risk([chip_path]) == gate.minimum_risk([ordinary_path]),
        (gate.minimum_risk([chip_path]), gate.minimum_risk([ordinary_path])),
    )

for path in (
    "/home/dev/.claude/state/chips/trees/app-fix-a1b2c3d4/src/main.go",
    "/home/dev/.claude/state/chips/trees/app-fix-a1b2c3d4/deploy/helm/values.yaml",
):
    check("a chip worktree in a posix home is source too: {}".format(path), cwg.is_gated(path), path)
    check(
        "a posix chip worktree file is a lasting artifact: {}".format(path),
        cwg.work_class([path]) == cwg.WORK_PERSISTENT,
        path,
    )

check(
    "a test file inside a chip worktree keeps its own risk class",
    gate.minimum_risk(["{}/backend/tests/connection.test.ts".format(CHIP_TREE)])
    == gate.minimum_risk(["{}/backend/tests/connection.test.ts".format(ORDINARY_TREE)])
    == "LOW",
)

# The exemption stops at the tree directory: the rest of `state` is the hooks' own bookkeeping
# and stays throwaway, including the chip cards and the index beside the worktrees themselves.
for path in (
    native("C:/Users/in/.claude/state/chips/trees/by-tree.json"),
    native("C:/Users/in/.claude/state/chips/a1b2c3d4.json"),
    native("C:/Users/in/.claude/state/gate-events.jsonl"),
    native("C:/Users/in/.claude/state/checkpoints/proj.md"),
    native("C:/Users/in/.claude/plans/rollout.md"),
    "/home/dev/.claude/state/chips/by-parent/parent.json",
):
    check("state outside a chip worktree stays ungated: {}".format(path), not cwg.is_gated(path), path)
    check("state outside a chip worktree stays throwaway: {}".format(path), cwg.is_ephemeral(path), path)

# The boundary is the tree directory: the worktree's own root is where the checkout begins, not
# a file in it, so naming it alone is still bookkeeping.
check("the tree directory itself stays throwaway", cwg.is_ephemeral(CHIP_TREE), CHIP_TREE)

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100.0, last_ts=140.0, durable_ts=110.0)
    events = base_events(include_simplify=True)
    add_review(events, 120.0, "review-1", review_text("APPROVED"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; approved candidate, scratch probe rerun after",
    })
    check(
        "a throwaway rerun after approval does not invalidate the verdict",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=100.0, last_ts=140.0, durable_ts=130.0)
    events = base_events(include_simplify=True)
    add_review(events, 120.0, "review-1", review_text("APPROVED"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; source edited after the verdict",
    })
    check(
        "a source edit after approval still invalidates the verdict",
        result.get("decision") == "block"
        and "lacks a current APPROVED verdict" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

with tempfile.TemporaryDirectory(prefix="cwg_unresolved_") as outside:
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        run(MARK_HOOK, {
            "session_id": sid,
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": outside,
            "tool_input": {"file_path": os.path.join(outside, "src", "app.py")},
        })
        approved_at = (cwg.read_json(marker) or {}).get("last_durable_ts")
        payload = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_use_id": "unresolved-shell-edit",
            "cwd": outside,
            "tool_input": {"command": "python -c rewrite_source"},
        }
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
        data = cwg.read_json(marker) or {}
        check(
            "an unresolved shell mutation expires the verdict on a durable cycle",
            cwg.valid_ts(approved_at) and data.get("last_durable_ts", 0) > approved_at,
            data,
        )
    finally:
        cleanup(sid)

check(
    "an empty snapshot vouches only for the trees it covers",
    marker_hook.outside_snapshot(
        [cwg.normalize_path("C:/Users/in/.claude/hooks/gate.py")], ["C:/repo"]
    ) == [cwg.normalize_path("C:/Users/in/.claude/hooks/gate.py")],
)
check(
    "a watched configuration tree is one of the trees a command is judged against",
    marker_hook.outside_snapshot(
        [cwg.normalize_path("C:/Users/in/.claude/hooks/gate.py")],
        ["C:/repo"],
        ["C:/Users/in/.claude/hooks"],
    ) == [],
)
check(
    # The scan opens six directories, not the home: claiming the home would vouch for the
    # machine-managed plugin tree the gate still grades HIGH.
    "an unwatched pocket of a configuration home is never vouched for",
    marker_hook.outside_snapshot(
        [cwg.normalize_path("C:/Users/in/.claude/plugins/repo/hook.js")],
        [],
        ["C:/Users/in/.claude/hooks", "C:/Users/in/.claude/skills"],
    ) != [],
)
check(
    "a skipped subdirectory inside a watched tree is not vouched for either",
    marker_hook.outside_snapshot(
        [cwg.normalize_path("C:/Users/in/.claude/skills/x/node_modules/tool.js")],
        [],
        ["C:/Users/in/.claude/skills"],
    ) != [],
)
check(
    "a watched file vouches for itself",
    marker_hook.outside_snapshot(
        [cwg.normalize_path("C:/Users/in/.claude/settings.json")], [], ["C:/Users/in/.claude/settings.json"]
    ) == [],
)
check(
    # `plans` and `state` are bookkeeping in a configuration home and ordinary source in a
    # repository; Git reports on them either way.
    "a repository vouches for source in a directory a configuration home would skip",
    marker_hook.outside_snapshot(
        [cwg.normalize_path("C:/repo/src/pages/plans/planrow.tsx"), cwg.normalize_path("C:/repo/src/state/store.ts")],
        ["C:/repo"],
    ) == [],
)
check(
    "an empty snapshot vouches for paths under its root",
    marker_hook.outside_snapshot([cwg.normalize_path("C:/repo/src/app.ts")], ["C:/repo"]) == [],
)
check(
    "a sibling directory is not under the snapshot root",
    marker_hook.outside_snapshot(["c:/repo-two/src/app.ts"], ["C:/repo"]) != [],
)
check(
    "no snapshot at all vouches for nothing",
    marker_hook.outside_snapshot(["c:/repo/src/app.ts"], []) == ["c:/repo/src/app.ts"],
)
check(
    "throwaway paths never count as unvouched",
    marker_hook.outside_snapshot(
        ["/tmp/probe.py", cwg.SHELL_MUTATION_PATH], ["C:/repo"]
    ) == [],
)

sid = session()
try:
    marker, _ = gate_paths(sid)
    scratch = os.path.join(SYSTEM_TEMP, "cwg_scratch_probe.py")
    run(MARK_HOOK, {
        "session_id": sid,
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "cwd": os.path.expanduser("~"),
        "tool_input": {"file_path": scratch},
    })
    check("writing a scratch script opens no cycle", not os.path.exists(marker), scratch)
finally:
    cleanup(sid)

# The agent configuration this gate grades HIGH lives outside any repository, so a shell command
# that rewrites a hook is invisible to the Git snapshot. These cover what the marker sees instead.
with tempfile.TemporaryDirectory(prefix="cwg_config_home_") as home:
    config_home = os.path.join(home, ".claude")
    os.makedirs(os.path.join(config_home, "hooks"))
    os.makedirs(os.path.join(config_home, "plugins"))
    hook_file = os.path.join(config_home, "hooks", "gate.py")
    with open(hook_file, "w", encoding="utf-8") as stream:
        stream.write("value = 1\n")
    os.environ["CLAUDE_CONFIG_DIR"] = config_home
    try:
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-rewrites-hook",
                "tool_name": "Bash",
                # Outside any repository, which is the case the Git snapshot cannot answer.
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "python patch_hook.py"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            with open(hook_file, "w", encoding="utf-8") as stream:
                stream.write("value = 2  # rewritten by the command\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a shell edit to a hook outside any repository is named",
                any(
                    path.endswith("/.claude/hooks/gate.py")
                    for path in data.get("paths") or []
                ),
                data,
            )
            check(
                "a hook rewritten through the shell is a HIGH persistent candidate",
                data.get("minimum_risk_seen") == "HIGH"
                and cwg.work_class(data.get("paths") or []) == cwg.WORK_PERSISTENT,
                data,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            reference = os.path.join(config_home, "reference", "codex-routing.md")
            os.makedirs(os.path.dirname(reference), exist_ok=True)
            with open(reference, "w", encoding="utf-8") as stream:
                stream.write("# routing" + chr(10))
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-writes-reference",
                "tool_name": "Bash",
                "cwd": os.path.join(config_home, "reference"),
                "tool_input": {"command": "python edit_reference.py"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            with open(reference, "a", encoding="utf-8") as stream:
                stream.write("one more line" + chr(10))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            paths = data.get("paths") or []
            check(
                "a reference document rewritten through the shell is named",
                any(path.endswith("/reference/codex-routing.md") for path in paths),
                data,
            )
            # A gated tree the snapshot does not watch would leave this path unvouched, and every
            # later shell call would then expire the review verdict and strand the candidate.
            probe = {
                "session_id": sid,
                "tool_use_id": "shell-after-reference",
                "tool_name": "Bash",
                "cwd": os.path.join(config_home, "reference"),
                "tool_input": {"command": "python probe.py"},
            }
            run(MARK_HOOK, dict(probe, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(probe, hook_event_name="PostToolUse"))
            after = cwg.read_json(marker) or {}
            check(
                "a later command does not re-expire a verdict over a watched reference tree",
                after.get("last_durable_ts") == data.get("last_durable_ts"),
                after,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-during-vendor-sync",
                "tool_name": "Bash",
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "npm test"},
            }
            vendor = os.path.join(config_home, "skills", ".system", "imagegen")
            os.makedirs(vendor, exist_ok=True)
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            with open(os.path.join(vendor, "SKILL.md"), "w", encoding="utf-8") as stream:
                stream.write("# resynced by the CLI\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a vendor namespace resynced mid-command opens no code candidate",
                cwg.work_class(data.get("paths") or []) == cwg.WORK_OPERATIONAL,
                data,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-writes-own-skill",
                "tool_name": "Bash",
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "python author_skill.py"},
            }
            own = os.path.join(config_home, "skills", "hand-written")
            os.makedirs(own, exist_ok=True)
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            with open(os.path.join(own, "SKILL.md"), "w", encoding="utf-8") as stream:
                stream.write("# authored here\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a hand-written skill in the same tree is still named",
                any(path.endswith(cwg.normalize_path("/skills/hand-written/SKILL.md"))
                    for path in data.get("paths") or []),
                data,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-leaves-config-alone",
                "tool_name": "Bash",
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "python probe.py"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a command that rewrote no configuration stays operational",
                data.get("minimum_risk_seen") == "LOW"
                and cwg.work_class(data.get("paths") or []) == cwg.WORK_OPERATIONAL,
                data,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-touches-plugins",
                "tool_name": "Bash",
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "claude plugin update"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            with open(os.path.join(config_home, "plugins", "tool.js"), "w",
                      encoding="utf-8") as stream:
                stream.write("module.exports = {};\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "the machine-managed plugin tree opens no code candidate",
                cwg.work_class(data.get("paths") or []) == cwg.WORK_OPERATIONAL,
                data,
            )
        finally:
            cleanup(sid)
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "shell-deletes-hook",
                "tool_name": "Bash",
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "python retire_hook.py"},
            }
            doomed = os.path.join(config_home, "hooks", "retired.py")
            with open(doomed, "w", encoding="utf-8") as stream:
                stream.write("value = 1\n")
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            os.remove(doomed)
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a hook deleted through the shell is named too",
                any(
                    path.endswith("/.claude/hooks/retired.py")
                    for path in data.get("paths") or []
                ),
                data,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "validation-outside-any-repository",
                "tool_name": "Bash",
                # No repository to vouch for the directory the build writes into, and the
                # configuration snapshot answers for other trees entirely.
                "cwd": tempfile.gettempdir(),
                "tool_input": {"command": "npm run build"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a validation command whose directory nothing watched is still marked",
                data.get("paths") == [cwg.SHELL_MUTATION_PATH],
                data,
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            payload = {
                "session_id": sid,
                "tool_use_id": "validation-inside-watched-tree",
                "tool_name": "Bash",
                # Run inside a watched tree that the snapshot proves unchanged: nothing to mark.
                "cwd": os.path.join(config_home, "hooks"),
                "tool_input": {"command": "npm run build"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            check(
                "a validation command that changed nothing where it ran opens no cycle",
                not os.path.exists(marker),
                cwg.read_json(marker),
            )
        finally:
            cleanup(sid)

        sid = session()
        try:
            marker, _ = gate_paths(sid)
            skipped_cwd = os.path.join(config_home, "skills", "demo", "state")
            os.makedirs(skipped_cwd, exist_ok=True)
            payload = {
                "session_id": sid,
                "tool_use_id": "validation-in-skipped-directory",
                "tool_name": "Bash",
                # Inside a watched tree by prefix, but in the bookkeeping the scan walks around,
                # so nothing here was read and the command is not on proven ground.
                "cwd": skipped_cwd,
                "tool_input": {"command": "npm run build"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a command run inside a skipped directory is not on proven ground",
                data.get("paths") == [cwg.SHELL_MUTATION_PATH],
                data,
            )
        finally:
            cleanup(sid)

        for label, action in (("created", "create"), ("deleted", "delete")):
            sid = session()
            try:
                marker, _ = gate_paths(sid)
                settings = os.path.join(config_home, "settings.local.json")
                if action == "delete":
                    with open(settings, "w", encoding="utf-8") as stream:
                        stream.write('{"permissions": {"allow": []}}\n')
                elif os.path.exists(settings):
                    os.remove(settings)
                payload = {
                    "session_id": sid,
                    "tool_use_id": "shell-{}-settings".format(label),
                    "tool_name": "Bash",
                    "cwd": tempfile.gettempdir(),
                    "tool_input": {"command": "python write_settings.py"},
                }
                run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
                if action == "create":
                    with open(settings, "w", encoding="utf-8") as stream:
                        stream.write('{"permissions": {"allow": ["Bash"]}}\n')
                else:
                    os.remove(settings)
                run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
                data = cwg.read_json(marker) or {}
                check(
                    "a settings file {} through the shell is named".format(label),
                    any(
                        path.endswith("/.claude/settings.local.json")
                        for path in data.get("paths") or []
                    ),
                    data,
                )
                check(
                    "a settings file {} through the shell is a HIGH candidate".format(label),
                    data.get("minimum_risk_seen") == "HIGH"
                    and cwg.work_class(data.get("paths") or []) == cwg.WORK_PERSISTENT,
                    data,
                )
            finally:
                if os.path.exists(os.path.join(config_home, "settings.local.json")):
                    os.remove(os.path.join(config_home, "settings.local.json"))
                cleanup(sid)

        # The app rewrites a settings file from its own interface — an output-style switch, /model
        # — while some command runs; that is no command's change (reports aa7603a1, 551e104e).
        for label, changed, named in (
            ("that only switches the app's own keys", {"outputStyle": "Concise", "model": "sonnet"}, False),
            ("that also moves a permission", {"outputStyle": "Concise", "permissions": {"allow": ["Bash"]}}, True),
        ):
            sid = session()
            settings = os.path.join(config_home, "settings.json")
            try:
                marker, _ = gate_paths(sid)
                original = {"outputStyle": "dense", "permissions": {"allow": []}}
                with open(settings, "w", encoding="utf-8") as stream:
                    json.dump(original, stream)
                payload = {
                    "session_id": sid,
                    "tool_use_id": "shell-app-settings-{}".format(named),
                    "tool_name": "Bash",
                    "cwd": tempfile.gettempdir(),
                    "tool_input": {"command": "python write_settings.py"},
                }
                run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
                with open(settings, "w", encoding="utf-8") as stream:
                    json.dump(dict(original, **changed), stream)
                run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
                data = cwg.read_json(marker) or {}
                check(
                    "a settings rewrite {} is {}".format(label, "named" if named else "not charged to the command"),
                    any(path.endswith("/.claude/settings.json") for path in data.get("paths") or []) is named,
                    data,
                )
            finally:
                if os.path.exists(settings):
                    os.remove(settings)
                cleanup(sid)
        with tempfile.TemporaryDirectory(prefix="cwg_app_settings_") as scratch:
            probe = os.path.join(scratch, "settings.json")
            with open(probe, "w", encoding="utf-8") as stream:
                json.dump({"outputStyle": "dense", "hooks": {}}, stream)
            plain = marker_hook.settings_digest(probe)
            with open(probe, "w", encoding="utf-8") as stream:
                json.dump({"outputStyle": "Concise", "theme": "dark", "hooks": {}}, stream)
            check("the app's own keys leave a settings digest as it was",
                  plain is not None and marker_hook.settings_digest(probe) == plain, plain)
            with open(probe, "w", encoding="utf-8") as stream:
                stream.write("{ broken")
            check("a settings file that does not parse has no digest",
                  marker_hook.settings_digest(probe) is None, "digest")
        watched = "c:/cfg/.claude/settings.json"
        earlier = {"overflow": False, "roots": ["c:/cfg/.claude/settings.json"], "files": {watched: "1:1"},
                   "settings": {watched: "same"}}
        later = dict(earlier, files={watched: "2:2"})
        check("a snapshot from before the settings digest still names the change",
              marker_hook.changed_config_paths({k: v for k, v in earlier.items() if k != "settings"}, later)
              == [watched], "legacy")
        check("a settings file unreadable before the command is named when it changes",
              marker_hook.changed_config_paths(dict(earlier, settings={watched: None}), later) == [watched],
              "unreadable")

        limit = marker_hook.AGENT_CONFIG_LIMIT
        try:
            marker_hook.AGENT_CONFIG_LIMIT = 1
            check(
                "a configuration tree past the cap proves nothing",
                marker_hook.config_snapshot().get("overflow") is True,
            )
        finally:
            marker_hook.AGENT_CONFIG_LIMIT = limit
    finally:
        os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR

with tempfile.TemporaryDirectory(prefix="cwg_config_unreadable_") as home:
    # A configuration directory that exists and cannot be listed must read as unknown, never as
    # a clean tree. Permissions are not portable enough to arrange here, so the refusal itself
    # is injected — what is under test is which answer the snapshot gives when a tree refuses.
    config_home = os.path.join(home, ".claude")
    os.makedirs(os.path.join(config_home, "hooks"))
    os.environ["CLAUDE_CONFIG_DIR"] = config_home
    real_scandir = os.scandir
    try:
        os.scandir = lambda path: (_ for _ in ()).throw(PermissionError(13, "denied"))
        check(
            "an unreadable configuration tree proves nothing",
            marker_hook.config_snapshot().get("overflow") is True,
        )
    finally:
        os.scandir = real_scandir
        os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR

with tempfile.TemporaryDirectory(prefix="cwg_config_notdir_") as home:
    # A plain file standing where a watched tree would be holds no gated file; refusing every
    # later command until someone finds it would be worse than reading it as empty.
    config_home = os.path.join(home, ".claude")
    os.makedirs(config_home)
    with open(os.path.join(config_home, "hooks"), "w", encoding="utf-8") as stream:
        stream.write("not a directory\n")
    os.environ["CLAUDE_CONFIG_DIR"] = config_home
    try:
        check(
            "a file standing where a watched tree would be is simply empty",
            marker_hook.config_snapshot().get("overflow") is False,
        )
    finally:
        os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR

with tempfile.TemporaryDirectory(prefix="cwg_config_bare_") as home:
    # A home holding none of the watched directories is ordinary, not unknown: most machines
    # have only some of them, and an absent tree changed nothing.
    config_home = os.path.join(home, ".claude")
    os.makedirs(config_home)
    os.environ["CLAUDE_CONFIG_DIR"] = config_home
    try:
        snapshot = marker_hook.config_snapshot()
        check(
            "a home missing every watched directory is still a clean snapshot",
            snapshot.get("overflow") is False
            and cwg.normalize_path(os.path.join(config_home, "hooks"))
            in (snapshot.get("roots") or []),
            snapshot,
        )
    finally:
        os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR

check(
    "a Git-only snapshot written before the upgrade is still read as one",
    marker_hook.stored_snapshot({"root": "c:/repo", "files": {}})
    == {"git": {"root": "c:/repo", "files": {}}, "config": None},
)

# Two sessions in one working directory, on one branch. The marker file is per session, but the
# snapshot a shell command is judged by reads a shared tree, so the second session used to end
# up holding the first session's edits: it could then close under no receipt at all, because
# `no-change` and `operational` are refused for a candidate that changed a lasting artifact and
# `verified` demands a simplify pass over a diff it never wrote.
with tempfile.TemporaryDirectory(prefix="cwg_two_sessions_") as repo:
    candidate_repo(repo, "shared-branch")
    editor = session()
    auditor = session()
    try:
        editor_marker, _ = gate_paths(editor)
        auditor_marker, _ = gate_paths(auditor)
        payload = {
            "session_id": auditor,
            "tool_use_id": "auditor-git-op",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "git commit --amend --no-edit"},
        }
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
        owned = mark_edit(editor, repo, "src/authentication/session.ts")
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))

        editor_entry = cwg.read_json(editor_marker) or {}
        auditor_entry = cwg.read_json(auditor_marker) or {}
        check(
            "the editing session still owns its own edit",
            owned in (editor_entry.get("paths") or [])
            and gate.candidate_class(editor_entry) == cwg.WORK_PERSISTENT,
            editor_entry,
        )
        check(
            "a neighbouring session's edit stays out of this candidate",
            owned not in (auditor_entry.get("paths") or []),
            auditor_entry,
        )
        check(
            "a session that edited nothing stays operational",
            gate.candidate_class(auditor_entry) == cwg.WORK_OPERATIONAL,
            auditor_entry,
        )
        accepted, why = gate.receipt_preflight(
            gate.receipt_of("[gate] no-change: read-only audit of the session state"),
            auditor_entry,
        )
        check("a session that edited nothing can close as no-change", accepted, why)

        # The same question with the neighbour writing through a shell command instead: while
        # its window is open the change is real but unattributable, and charging it to whichever
        # session happened to look at the tree is exactly the confusion being removed.
        editor_shell = {
            "session_id": editor,
            "tool_use_id": "editor-writer",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "python -c writer"},
        }
        auditor_shell = {
            "session_id": auditor,
            "tool_use_id": "auditor-status",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "git commit --amend --no-edit"},
        }
        run(MARK_HOOK, dict(editor_shell, hook_event_name="PreToolUse"))
        run(MARK_HOOK, dict(auditor_shell, hook_event_name="PreToolUse"))
        concurrent_target = os.path.join(repo, "src", "billing", "invoice.py")
        os.makedirs(os.path.dirname(concurrent_target), exist_ok=True)
        with open(concurrent_target, "w", encoding="utf-8") as stream:
            stream.write("total = 2" + chr(10))
        run(MARK_HOOK, dict(auditor_shell, hook_event_name="PostToolUse"))
        auditor_entry = cwg.read_json(auditor_marker) or {}
        check(
            "a change made while another session's command is running is not charged here",
            not any(
                path.endswith("/src/billing/invoice.py")
                for path in auditor_entry.get("paths") or []
            ),
            auditor_entry,
        )
        # The path is another session's to review; the grade is not. Nobody can be shown to own
        # this change, so the candidate keeps a floor for it - otherwise a session could reach
        # the operational contract simply by running its command next to a busy neighbour.
        check(
            "an unattributable change still costs this candidate its floor",
            gate.candidate_class(auditor_entry) == cwg.WORK_PERSISTENT
            and auditor_entry.get("minimum_risk_seen") == "HIGH",
            auditor_entry,
        )
        rejected, why = gate.receipt_preflight(
            gate.receipt_of("[gate] no-change: read-only audit of the session state"),
            auditor_entry,
        )
        check("an unattributable change cannot be closed as no-change", not rejected, why)
        # The floor sends this candidate for a review it must then be able to keep. Freshness is
        # measured against the last durable change, so an unattributable one has to anchor it:
        # left at zero the Stop hook falls back to the whole-cycle timestamp, and the next
        # command of any kind would expire the approval.
        anchored = auditor_entry.get("last_durable_ts")
        check(
            "an unattributable change anchors review freshness",
            cwg.valid_ts(anchored),
            auditor_entry,
        )
        run(MARK_HOOK, dict(editor_shell, hook_event_name="PostToolUse"))
        editor_entry = cwg.read_json(editor_marker) or {}
        check(
            "the session whose command wrote it still holds it",
            any(
                path.endswith("/src/billing/invoice.py")
                for path in editor_entry.get("paths") or []
            ),
            editor_entry,
        )

        # An edit announced but not yet completed. Claiming on PostToolUse alone leaves a race:
        # the announcement can land after a concurrent command has already resolved its diff.
        # The hook therefore also answers PreToolUse for an edit tool, publishing the claim
        # before the write; registering that matcher is what closes the race.
        announced = os.path.join(repo, "src", "authorization", "policy.py")
        os.makedirs(os.path.dirname(announced), exist_ok=True)
        editor_edits = len((cwg.read_json(editor_marker) or {}).get("paths") or [])
        run(MARK_HOOK, {
            "session_id": editor,
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "cwd": repo,
            "tool_input": {"file_path": announced},
        })
        check(
            "announcing an edit does not open a candidate by itself",
            len((cwg.read_json(editor_marker) or {}).get("paths") or []) == editor_edits,
            cwg.read_json(editor_marker),
        )
        # A session with no history of its own, so that neither half of the assertion below can
        # be satisfied by something an earlier scenario left on the auditor's marker: the floor
        # is sticky for a cycle by design, which would mask exactly what is being tested.
        observer = session()
        observer_marker, _ = gate_paths(observer)
        observer_shell = {
            "session_id": observer,
            "tool_use_id": "observer-status",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "git commit --amend --no-edit"},
        }
        run(MARK_HOOK, dict(observer_shell, hook_event_name="PreToolUse"))
        with open(announced, "w", encoding="utf-8") as stream:
            stream.write("allow = False" + chr(10))
        run(MARK_HOOK, dict(observer_shell, hook_event_name="PostToolUse"))
        observer_entry = cwg.read_json(observer_marker) or {}
        check(
            "an announced edit is out of a concurrent command's delta before it completes",
            not any(
                path.endswith("/src/authorization/policy.py")
                for path in observer_entry.get("paths") or []
            ),
            observer_entry,
        )
        # An announcement is not yet a write. It keeps the path out of this candidate, but it
        # cannot excuse the candidate: until the edit lands, nobody has been shown to own the
        # change, so the floor applies exactly as it does for an overlapping command.
        check(
            "an unconfirmed announcement narrows the question without excusing it",
            gate.candidate_class(observer_entry) == cwg.WORK_PERSISTENT
            and observer_entry.get("unattributed_durable") is True,
            observer_entry,
        )
        cleanup(observer)

        # Shared ground is decided per tree, and a repository passes no skip list: a directory
        # inside it named `state`, `plans` or `node_modules` is ordinary source, not the
        # bookkeeping those names mean in a configuration home. Judging the neighbour's working
        # directory by the configuration skip list would read it as outside the repository it
        # plainly sits in, and its writes would be charged here.
        vendored = os.path.join(repo, "node_modules", "pkg")
        os.makedirs(vendored, exist_ok=True)
        run(MARK_HOOK, dict(editor_shell, hook_event_name="PreToolUse", cwd=vendored,
                            tool_use_id="editor-vendored"))
        run(MARK_HOOK, dict(auditor_shell, hook_event_name="PreToolUse"))
        unclaimed = os.path.join(repo, "src", "deploy", "release.py")
        os.makedirs(os.path.dirname(unclaimed), exist_ok=True)
        with open(unclaimed, "w", encoding="utf-8") as stream:
            stream.write("shipped = True" + chr(10))
        run(MARK_HOOK, dict(auditor_shell, hook_event_name="PostToolUse"))
        auditor_entry = cwg.read_json(auditor_marker) or {}
        check(
            "a neighbour working in a repository subdirectory still shares its tree",
            not any(
                path.endswith("/src/deploy/release.py")
                for path in auditor_entry.get("paths") or []
            ),
            auditor_entry,
        )
        run(MARK_HOOK, dict(editor_shell, hook_event_name="PostToolUse", cwd=vendored,
                            tool_use_id="editor-vendored"))

        # A command whose own text says it writes keeps its whole delta. Another session
        # announcing the same path only means that session wrote it too; subtracting on that
        # weaker evidence would let a real in-place edit leave no candidate at all.
        contested = os.path.join(repo, "src", "authentication", "session.ts")
        os.makedirs(os.path.dirname(contested), exist_ok=True)
        run(MARK_HOOK, {
            "session_id": editor,
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "cwd": repo,
            "tool_input": {"file_path": contested},
        })
        writer = {
            "session_id": auditor,
            "tool_use_id": "auditor-sed",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "sed -i s/a/b/ src/authentication/session.ts"},
        }
        run(MARK_HOOK, dict(writer, hook_event_name="PreToolUse"))
        with open(contested, "w", encoding="utf-8") as stream:
            stream.write("export const value = 9;" + chr(10))
        run(MARK_HOOK, dict(writer, hook_event_name="PostToolUse"))
        auditor_entry = cwg.read_json(auditor_marker) or {}
        check(
            "a write-shaped command still owns the path it wrote",
            any(
                path.endswith("/src/authentication/session.ts")
                for path in auditor_entry.get("paths") or []
            ),
            auditor_entry,
        )
        # And the claim is published before the window closes: a reader that sees the window
        # gone must already be able to see what the command wrote, or the interval between the
        # two is one where nobody owns the change.
        registry = cwg.read_json(cwg.claim_path(cwg.session_key(auditor))) or {}
        check(
            "a resolved command closes its window only once its claims are readable",
            not registry.get("shell_start_ts")
            and any(
                path.endswith("/src/authentication/session.ts")
                for path in (registry.get("claims") or {})
            ),
            registry,
        )

        # A command killed before its PostToolUse leaves a window open. Another session must
        # not silently lose its own delta to it: attribution may hand every path away, but what
        # it must never do is leave no candidate at all for a tree that demonstrably changed.
        run(MARK_HOOK, dict(editor_shell, hook_event_name="PreToolUse",
                            tool_use_id="editor-killed"))
        validation = {
            "session_id": auditor,
            "tool_use_id": "auditor-validation",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "npm test"},
        }
        run(MARK_HOOK, dict(validation, hook_event_name="PreToolUse"))
        generated = os.path.join(repo, "src", "generated.ts")
        with open(generated, "w", encoding="utf-8") as stream:
            stream.write("export const built = true;" + chr(10))
        before_edits = int((cwg.read_json(auditor_marker) or {}).get("edits") or 0)
        run(MARK_HOOK, dict(validation, hook_event_name="PostToolUse"))
        auditor_entry = cwg.read_json(auditor_marker) or {}
        check(
            "a delta given away entirely still leaves a candidate",
            int(auditor_entry.get("edits") or 0) == before_edits + 1
            and cwg.SHELL_MUTATION_PATH in (auditor_entry.get("paths") or []),
            auditor_entry,
        )
        killed_marker, _ = gate_paths(auditor)
        killed_entry = cwg.read_json(killed_marker) or {}
        rejected, why = gate.receipt_preflight(
            gate.receipt_of("[gate] operational: checked the tree first; tests passed"),
            killed_entry,
        )
        check(
            "a window nobody closed cannot buy the operational contract",
            not rejected and gate.candidate_class(killed_entry) == cwg.WORK_PERSISTENT,
            why,
        )
        run(MARK_HOOK, dict(editor_shell, hook_event_name="PostToolUse",
                            tool_use_id="editor-killed"))

        # Ownership must never lapse mid-hook. Sampling the registry while the PostToolUse hook
        # is still resolving is the only way to see the interval the after-snapshot spans: with
        # the window closed first, every sample taken during it shows a command that has stopped
        # claiming to be writing and has not yet said what it wrote, and a session resolving
        # then takes those writes for its own.
        handover = os.path.join(repo, "src", "authentication", "handover.ts")
        os.makedirs(os.path.dirname(handover), exist_ok=True)
        sampler = {
            "session_id": auditor,
            "tool_use_id": "auditor-handover",
            "tool_name": "Bash",
            "cwd": repo,
            "tool_input": {"command": "sed -i s/a/b/ src/authentication/handover.ts"},
        }
        run(MARK_HOOK, dict(sampler, hook_event_name="PreToolUse"))
        with open(handover, "w", encoding="utf-8") as stream:
            stream.write("export const handed = true;" + chr(10))
        resolving = subprocess.Popen(
            [sys.executable, MARK_HOOK],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        resolving.stdin.write(json.dumps(dict(sampler, hook_event_name="PostToolUse")))
        resolving.stdin.close()
        samples = 0
        unowned = 0
        while resolving.poll() is None:
            snapshot = cwg.read_json(cwg.claim_path(cwg.session_key(auditor)))
            if snapshot is None:
                # A read that lost the race with the atomic replace is a missed sample, not a
                # lapse in ownership; counting it as one would make this test flaky.
                continue
            samples += 1
            if not snapshot.get("shell_start_ts") and not any(
                path.endswith("/src/authentication/handover.ts")
                for path in (snapshot.get("claims") or {})
            ):
                unowned += 1
            time.sleep(0.005)
        resolving.wait()
        settled = cwg.read_json(cwg.claim_path(cwg.session_key(auditor))) or {}
        # Only a run that ended up claiming the path can say anything about the interval before
        # it: a Git snapshot that could not be compared - the timeout is reachable on a loaded
        # machine - resolves nothing, and there is then no handover to observe. The mutation
        # this pins moves the window close earlier without changing what is claimed in the end,
        # so keying conclusiveness to the settled state does not weaken it.
        conclusive = any(
            path.endswith("/src/authentication/handover.ts")
            for path in (settled.get("claims") or {})
        )
        check(
            "a resolving command never stops owning what it wrote",
            samples <= 1 or unowned == 0 or not conclusive,
            "{} of {} samples owned by nobody; settled={}".format(unowned, samples, settled),
        )
    finally:
        cleanup(editor)
        cleanup(auditor)

# The finite block budget is documented as a hard cap per unchanged candidate. Keyed to the
# marker's last_ts it was not one: any later mark — the blocked turn's own checks, or a
# neighbouring session refreshing the marker — reset the counter and enforcement could block
# without end.
sid = session()
try:
    marker, _ = gate_paths(sid)
    seed(sid, ["C:/repo/src/app.py"])
    payload = {"session_id": sid, "last_assistant_message": "done"}
    for expected in range(1, 4):
        result = run(STOP_HOOK, payload)
        check(
            "block {} survives a marker refresh".format(expected),
            "block {}/3".format(expected) in result.get("reason", ""),
            result,
        )
        refreshed = cwg.read_json(marker)
        refreshed["last_ts"] = float(refreshed["last_ts"]) + 5.0
        refreshed["edits"] = int(refreshed.get("edits") or 0) + 1
        check("refresh marker", cwg.write_json(marker, refreshed), refreshed)
    result = run(STOP_HOOK, payload)
    check(
        "a refreshed timestamp cannot buy a fourth block",
        result.get("continue") is True and "UNVERIFIED" in result.get("systemMessage", ""),
        result,
    )
finally:
    cleanup(sid)

# The other half of the same rule: a genuinely new edit is a new candidate and does get a fresh
# budget, so the cap bounds disobedience without punishing progress.
sid = session()
try:
    marker, _ = gate_paths(sid)
    seed(sid, ["C:/repo/src/app.py"])
    payload = {"session_id": sid, "last_assistant_message": "done"}
    for expected in (1, 2):
        result = run(STOP_HOOK, payload)
        check(
            "block {} before the candidate grows".format(expected),
            "block {}/3".format(expected) in result.get("reason", ""),
            result,
        )
    grown = cwg.read_json(marker)
    grown["paths"] = list(grown["paths"]) + ["c:/repo/src/other.py"]
    grown["last_ts"] = float(grown["last_ts"]) + 5.0
    check("grow marker", cwg.write_json(marker, grown), grown)
    result = run(STOP_HOOK, payload)
    check(
        "a new edit restarts the block budget",
        "block 1/3" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid)


# The registry's own limits, driven directly: they decide whether a dead session can go on
# suppressing attribution, and none of them is reachable from a real-time scenario. The
# registry is redirected first — this suite runs inside a live session whose own claim file
# sits in the real one, and these checks are about what a named file does, not about it.
with tempfile.TemporaryDirectory(prefix="cwg_claims_unit_") as registry:
    stale = cwg.session_key(session())
    real_root = cwg.claims_root
    cwg.claims_root = lambda: registry
    try:
        now = time.time()
        target = native("c:/repo/src/app.py")
        check(
            "an announcement inside the window is foreign",
            cwg.publish_claims(stale, paths=[native("C:/repo/src/app.py")], now=now)
            and cwg.foreign_activity("reader", now - 1, now)[0] == {target},
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "an announcement made before the window is not",
            cwg.foreign_activity("reader", now + 2 * cwg.CLAIM_SLACK, now)[0] == set(),
        )
        check(
            "a session's own announcements are not foreign to itself",
            cwg.foreign_activity(stale, now - 1, now)[0] == set(),
        )
        check(
            "an open shell window is reported with its working directory",
            cwg.publish_claims(stale, shell_start_ts=now, cwd=native("C:/repo"), now=now)
            and cwg.foreign_activity("reader", now - 1, now)[2] == {native("c:/repo")},
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "an announcement outlives any prompt, however long it is left open",
            cwg.publish_claims(stale, paths=[native("C:/repo/src/slow.py")], pending=True,
                               now=now - cwg.SHELL_WINDOW_LIMIT - 60)
            and native("c:/repo/src/slow.py")
            in cwg.foreign_activity("reader", now, now + 2 * cwg.CLAIM_HORIZON)[1],
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "a file still holding an announcement survives the staleness sweep",
            os.path.exists(cwg.claim_path(stale)),
        )
        check(
            "a window left open by a killed command expires",
            cwg.foreign_activity("reader", now, now + cwg.SHELL_WINDOW_LIMIT + 1)[2] == set(),
        )
        check(
            "no elapsed time retires a file that still announces something",
            cwg.foreign_activity("reader", now, now + 400 * 86400.0)[1]
            == {native("c:/repo/src/slow.py")}
            and os.path.exists(cwg.claim_path(stale)),
            cwg.read_json(cwg.claim_path(stale)),
        )
        # Nor does anyone else's activity. A reader is not the writer of that file and cannot
        # know whether the prompt holding its edit was answered, so it may neither delete it nor
        # rewrite it to shrink it; only its own session ends an announcement.
        crowd = [cwg.session_key(session()) for _ in range(64)]
        check(
            "other sessions announcing edits do not displace an older announcement",
            all(
                cwg.publish_claims(other, paths=[native("C:/repo/src/x{}.py").format(index)],
                                   pending=True, now=now + index)
                for index, other in enumerate(crowd)
            )
            and native("c:/repo/src/slow.py")
            in cwg.foreign_activity("reader", now, now + 400 * 86400.0)[1]
            and os.path.exists(cwg.claim_path(stale)),
            sorted(os.listdir(cwg.claims_root()))[:3],
        )
        for other in crowd:
            cwg.remove(cwg.claim_path(other))
        check(
            "a registry that does not exist yet is an answer, not a hole",
            _registry_states() == (False, True),
            _registry_states(),
        )
        check(
            "entries this scan skips still spend its budget",
            _crowded_registry_reports_overflow(),
        )
        check(
            "a registry too large for one scan is unread, not silent",
            all(
                cwg.publish_claims(cwg.session_key(session()),
                                   paths=[native("C:/repo/src/many{}.py").format(index)], now=now)
                for index in range(cwg.SCAN_LIMIT + 1)
            )
            and cwg.foreign_activity("reader", now - 1, now)[3] is True
            and mark.own_delta("reader", native("c:/repo"), [native("c:/repo/src/mine.py")], now - 1, False,
                               [(native("c:/repo"), ())]) == ([], [native("c:/repo/src/mine.py")]),
            len(os.listdir(cwg.claims_root())),
        )
        for name in list(os.listdir(cwg.claims_root())):
            if name != os.path.basename(cwg.claim_path(stale)):
                cwg.remove(os.path.join(cwg.claims_root(), name))
        check(
            "a claim file past the horizon is dropped rather than believed",
            # Promoted first, because only a file with nothing outstanding is the sweep's to
            # take: an announcement is what keeps one alive past the horizon.
            cwg.publish_claims(stale, paths=[native("C:/repo/src/slow.py")], now=now)
            and cwg.publish_claims(stale, paths=[native("C:/repo/src/app.py")], now=now)
            and cwg.foreign_activity("reader", now, now + cwg.CLAIM_HORIZON + 1)
            == (set(), set(), set(), False)
            and not os.path.exists(cwg.claim_path(stale)),
        )
        with open(cwg.claim_path(stale), "w", encoding="utf-8") as stream:
            stream.write("{not json")
        check(
            "a malformed claim file is unread, not silence",
            # Present and unparseable is a session whose state this scan does not have, so it
            # raises nothing and claims nothing: it reports the gap instead.
            cwg.foreign_activity("reader", time.time() - 1) == (set(), set(), set(), True),
        )
        cwg.remove(cwg.claim_path(stale))
        check(
            "a relative announced path resolves to the same key the edit records",
            cwg.publish_claims(stale, paths=["src/app.py"], cwd=native("C:/repo"))
            and target in (cwg.read_json(cwg.claim_path(stale)) or {}).get("claims", {}),
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "settling a path promotes it out of the announced map",
            cwg.publish_claims(stale, paths=[native("C:/repo/src/app.py")], pending=True)
            and cwg.publish_claims(stale, paths=[native("C:/repo/src/app.py")])
            and target in (cwg.read_json(cwg.claim_path(stale)) or {}).get("claims", {})
            and target not in (cwg.read_json(cwg.claim_path(stale)) or {}).get("pending", {}),
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "an unconfirmed announcement is not read as a settled claim",
            cwg.publish_claims(stale, paths=[native("C:/repo/src/only-announced.py")], pending=True)
            and native("c:/repo/src/only-announced.py")
            not in cwg.foreign_activity("reader", time.time() - 1)[0]
            and native("c:/repo/src/only-announced.py")
            in cwg.foreign_activity("reader", time.time() - 1)[1],
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "a closed cycle retires the registry file",
            cwg.retire_claims(stale) and not os.path.exists(cwg.claim_path(stale)),
        )
        check(
            "a still-open shell window survives the cycle that closed around it",
            cwg.publish_claims(stale, shell_start_ts=time.time(), cwd=native("C:/repo"))
            and not cwg.retire_claims(stale)
            and os.path.exists(cwg.claim_path(stale)),
        )
    finally:
        cwg.claims_root = real_root


# The registry closes a race only if the announcement reaches it before the write does, and
# that depends on a hook registration, not on this code. A session that announces at
# PostToolUse alone can still have its claim land after a concurrent command resolved its own
# snapshot, so the matcher is part of the mechanism and is asserted here rather than assumed.
# Read from the configuration home this suite lives in, not from the redirected one the
# scenarios use: what matters is the deployment that actually runs these hooks.
# A live configuration directory holds settings.json; a checkout of the published stack
# holds only the template, whose hook registrations are the same facts.
settings_file = os.path.join(REAL_CONFIG_HOME, "settings.json")
if not os.path.exists(settings_file):
    settings_file = os.path.join(REAL_CONFIG_HOME, "settings.example.json")
with io.open(settings_file, encoding="utf-8") as stream:
    registered = json.load(stream)
events = registered.get("hooks") or {}
marker_events = {}
shell_form = []
isolated = []
for event, groups in events.items():
    for group in groups or ():
        for hook in group.get("hooks") or ():
            invocation = [str(hook.get("command") or "")] + [str(arg) for arg in hook.get("args") or ()]
            if any(os.path.basename(MARK_HOOK) in part for part in invocation):
                marker_events.setdefault(event, []).append(group.get("matcher") or "")
            if hook.get("type") == "command" and "args" not in hook:
                shell_form.append("{}: {}".format(event, invocation[0]))
            script = next((index for index, part in enumerate(invocation) if part.endswith(".py")), 0)
            if any(re.fullmatch(r"-[A-Za-z]*[IP][A-Za-z]*", part) for part in invocation[1:script]):
                isolated.append("{}: {}".format(event, " ".join(invocation)))
check(
    "a run only ever cleans up after itself",
    session().startswith(RUN + "_test_"),
    RUN,
)
check(
    "the marker answers both halves of a shell command",
    any("Bash" in matcher for matcher in marker_events.get("PreToolUse") or ())
    and any("Bash" in matcher for matcher in marker_events.get("PostToolUse") or ()),
    marker_events,
)
check(
    "an edit is announced before it lands, not only after",
    any("Edit" in matcher for matcher in marker_events.get("PreToolUse") or ())
    and any("Edit" in matcher for matcher in marker_events.get("PostToolUse") or ()),
    marker_events,
)
check(
    "a failed shell command is still marked",
    any("Bash" in matcher for matcher in marker_events.get("PostToolUseFailure") or ()),
    marker_events,
)
# Under Git Bash a timeout kills bash alone, and the Python it started can stay suspended on the
# hook's pipe with the session waiting on it; exec form (`args`) spawns the hook itself.
check(
    "every hook is spawned without a shell, so a timeout ends the hook itself",
    not shell_form,
    shell_form,
)
# A hook finds its sibling modules in its own folder, which Python puts first on sys.path when it
# runs a script; -I, -P and PYTHONSAFEPATH leave that folder out.
check(
    "every Python hook runs with its own folder on the import path",
    not isolated and not (registered.get("env") or {}).get("PYTHONSAFEPATH"),
    (isolated, (registered.get("env") or {}).get("PYTHONSAFEPATH")),
)


# --- a capture outlives the candidate it was taken under, and the role is cut out of a packet
# --- however it was assembled (report dd10c5ca, and the unbound round it followed)
def rollout_records(records):
    """One rollout log written record by record: (stamp, role, text).

    `log_codex_run` writes a single round, briefed with the role's body. Neither of the two
    shapes this section needs is expressible there: a session resumed for a second round holds
    two briefs and two answers in the one file the first round opened, and a packet assembled
    from the whole role file has to reach the log spelled exactly as the launch fed it. A role of
    "turn" writes the `turn_context` the CLI opens a turn with, `text` being its (model, effort).
    """
    day = os.path.join(CODEX_HOME, "sessions", *time.strftime("%Y %m %d").split())
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, "rollout-{}.jsonl".format(uuid.uuid4().hex))
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": iso(records[0][0]), "type": "session_meta"}) + "\n")
        for stamp, role, text in records:
            if role == "turn":
                stream.write(json.dumps({"timestamp": iso(stamp), "type": "turn_context",
                                         "payload": {"model": text[0], "effort": text[1]}}) + "\n")
                continue
            stream.write(json.dumps({
                "timestamp": iso(stamp), "type": "response_item",
                "payload": {"type": "message", "role": role, "content": [{
                    "type": "input_text" if role == "developer" else "output_text", "text": text,
                }]},
            }) + "\n")
    last = max(stamp for stamp, _, _ in records)
    os.utime(path, (last, last))
    return path


def review_notes(sid):
    """What this session filed in the gate ledger about its review lanes, in order."""
    path = cwg.event_log_path()
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    notes = []
    for line in lines:
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("kind") == "review" and record.get("session") == cwg.session_key(sid):
            notes.append(record)
    return notes


def packet_launch(packet_file):
    """The launch as the shell spells it, feeding the packet on stdin."""
    path = packet_file.replace(chr(92), "/")
    if re.match(r"^[A-Za-z]:/", path):
        path = "/" + path[0].lower() + path[2:]
    return 'codex exec - < "{}"  # CODE_WORK_GATE_REVIEW'.format(path)


def write_packet(task_id, text):
    path = os.path.join(AGENT_HOME, "packet-" + task_id + ".md")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


with open(os.path.join(os.path.dirname(HERE), "agents", "adversarial-reviewer.md"),
          encoding="utf-8") as stream:
    # The role file whole: what a packet carries when it was assembled by copying that file,
    # front matter included, rather than the body the hook reads to tell a briefed session.
    REVIEWER_FILE_TEXT = stream.read()
BACKGROUND_HIGH = "[gate] verified: HIGH; Codex reviewed in the background"

for label, role_in_packet, rival, expect_bound in (
    ("opening on the whole role file, front matter and all", REVIEWER_FILE_TEXT, False, True),
    ("opening on the role body alone", reviewer_role_text(), False, True),
    ("opening on the whole role file, with another chat given the same words",
     REVIEWER_FILE_TEXT, True, False),
):
    sid = session()
    try:
        now = time.time()
        task_id = "bform" + uuid.uuid4().hex[:4]
        out_file = os.path.join(tasks_dir, task_id + ".output")
        fed = role_in_packet + "\n\n" + PACKET_A
        command = packet_launch(write_packet(task_id, fed))
        seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800,
             durable_ts=now - 800)
        capture_launch(sid, "codex-" + task_id, command)
        events = [skill_use(now - 890, "development-verification", "skill-dev")]
        simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
        events.append(bash_use(now - 700, "codex-" + task_id, command, run_in_background=True))
        events.append(tool_result(now - 699, "codex-" + task_id,
                                  DETACHED_ACK.format(id=task_id, out=out_file)))
        events.append(notification(now - 600, task_id, out_file, "completed"))
        rollout_records([(now - 690, "developer", fed),
                         (now - 650, "assistant", codex_cli_output(review_text("APPROVED")))])
        if rival:
            rollout_records([
                (now - 688, "developer", fed),
                (now - 648, "assistant",
                 codex_cli_output(review_text("APPROVED", subject="another chat's candidate"))),
            ])
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": BACKGROUND_HIGH})
        bound = result.get("continue") is True and "decision" not in result
        check("a packet " + label + " binds what it should", bound is expect_bound, result)
        if rival:
            check("the rival chat leaves the launch with no single session to bind to",
                  any("no single briefed Codex verdict" in (note.get("reason") or "")
                      for note in review_notes(sid)), review_notes(sid))
    finally:
        cleanup(sid, locals().get("transcript"))

# The role file as an older copy of it: the body is intact, so a session given it is still
# briefed, but the front matter differs — the whole-file cut misses and only the body cut lands,
# leaving that front matter beside the brief. It is common to every packet built from that copy,
# so it must never be what names a session, however short the brief beside it.
DRIFTED_ROLE_FILE = REVIEWER_FILE_TEXT.replace("---", "---\nreview-packet-variant: 1", 1)
check("the drifted role file still carries the role body verbatim",
      reviewer_role_text() in DRIFTED_ROLE_FILE and DRIFTED_ROLE_FILE != REVIEWER_FILE_TEXT,
      DRIFTED_ROLE_FILE[:64])
SHORT_BRIEF = "Round 1 packet: the session store rotates ids on privilege change."

for label, assembled, expected in (
    ("the role file whole, then the brief", REVIEWER_FILE_TEXT + "\n\n" + PACKET_A, PACKET_A),
    ("the role body, then the brief", reviewer_role_text() + "\n\n" + PACKET_A, PACKET_A),
    ("a drifted copy of the role file, then the brief", DRIFTED_ROLE_FILE + "\n\n" + PACKET_A, PACKET_A),
    ("no role at all", PACKET_A, PACKET_A),
    ("nothing but the role", REVIEWER_FILE_TEXT, ""),
    # The packet contract puts the role first and verbatim, so there is nothing below it to name
    # a session by. Binding the words above it instead would name the session by a brief the
    # reviewer read before it was told what it was reviewing.
    ("the brief above the role", PACKET_A + "\n\n" + REVIEWER_FILE_TEXT, ""),
):
    packet = cwg.normalized(assembled)
    distinctive = gate.distinctive_of(packet)
    check("a packet assembled as " + label + " is named by its own words",
          distinctive == cwg.normalized(expected), distinctive[:80])
    check("what names a packet assembled as " + label + " is one unbroken run of it",
          distinctive in packet, distinctive[:80])

for label, brief, rival_brief, rival_verdict, expect_bound in (
    # Ours never answers; the rival was given the same drifted front matter and a brief of its
    # own. Binding by that front matter would hand this launch the rival's approval.
    ("a drifted front matter never stands in for a brief too short to name a session",
     SHORT_BRIEF, PACKET_B, "APPROVED", False),
    # Both were given that front matter, only one this brief.
    ("a packet whose front matter drifted still binds by the brief below it",
     PACKET_A, PACKET_B, "REVISE", True),
):
    sid = session()
    try:
        now = time.time()
        task_id = "bdrift" + uuid.uuid4().hex[:4]
        out_file = os.path.join(tasks_dir, task_id + ".output")
        fed = DRIFTED_ROLE_FILE + "\n\n" + brief
        command = packet_launch(write_packet(task_id, fed))
        seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800,
             durable_ts=now - 800)
        capture_launch(sid, "codex-" + task_id, command)
        events = [skill_use(now - 890, "development-verification", "skill-dev")]
        simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
        events.append(bash_use(now - 700, "codex-" + task_id, command, run_in_background=True))
        events.append(tool_result(now - 699, "codex-" + task_id,
                                  DETACHED_ACK.format(id=task_id, out=out_file)))
        events.append(notification(now - 600, task_id, out_file, "completed"))
        if expect_bound:
            rollout_records([(now - 690, "developer", fed),
                             (now - 650, "assistant", codex_cli_output(review_text("APPROVED")))])
        rollout_records([
            (now - 688, "developer", DRIFTED_ROLE_FILE + "\n\n" + rival_brief),
            (now - 648, "assistant", codex_cli_output(
                review_text(rival_verdict, subject="another chat's candidate"))),
        ])
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": BACKGROUND_HIGH})
        bound = result.get("continue") is True and "decision" not in result
        check(label, bound is expect_bound, result)
        if not expect_bound:
            check("a brief too short to name a session leaves the launch nothing to bind by",
                  any("nothing to bind by at launch" in (note.get("reason") or "")
                      for note in review_notes(sid)), review_notes(sid))
            # The block says why too, not only the ledger (report 926b670b).
            check("the block names why the result could not be bound",
                  "could not be bound: packet fed on stdin but nothing to bind by at launch"
                  in result.get("reason", ""), result)
    finally:
        cleanup(sid, locals().get("transcript"))

# A closure receipt refused for want of a round-3 ESCALATE shows the rounds read and why the last
# result gave no verdict: the third round had been launched in a shape nothing could be bound from,
# and the block said only what it required (report 926b670b).
escalate_detail = gate.block_detail(
    "pr-ready " + gate.ESCALATE_REQUIRED, {"first_ts": 100.0},
    {"ordinary_reviews": [(130.0, "REVISE"), (132.0, "REVISE")],
     "review_events": [(130.0, "ordinary", "REVISE"), (132.0, "ordinary", "REVISE"), (134.0, "unbound", None)],
     "unbound_reasons": [(2, "write the packet in its own call before launching")]})
check("a refused closure receipt names the rounds read and the result that bound nothing",
      "ordinary rounds read:" in escalate_detail and "without a usable verdict" in escalate_detail
      and "could not be bound: write the packet in its own call" in escalate_detail, escalate_detail)
later_detail = gate.block_detail(
    "pr-ready " + gate.ESCALATE_REQUIRED, {"first_ts": 100.0},
    {"ordinary_reviews": [(130.0, "REVISE")],
     "review_events": [(130.0, "ordinary", "REVISE"), (134.0, "unbound", None), (136.0, "malformed", None)],
     "unbound_reasons": [(1, "an earlier result's reason")]})
check("an earlier result's reason is never pinned on a later result without one",
      "without a usable verdict" in later_detail and "could not be bound" not in later_detail, later_detail)
# Equal stamps: the result the scan filed last is the latest, and only its own reason is named.
for label, events, reasons, expected in (
        ("of two unbound results at one moment, the one filed last is named",
         [(134.0, "unbound", None), (134.0, "unbound", None)],
         [(0, "the review task was stopped"), (1, "no Codex run's log shows the verdict the call printed")],
         "could not be bound: no Codex run's log shows"),
        ("an unbound result's reason is not pinned on another kind filed after it at the same moment",
         [(134.0, "unbound", None), (134.0, "malformed", None)], [(0, "the review task was stopped")], None)):
    same_moment = gate.unbound_note({"review_events": events, "unbound_reasons": reasons}, str)
    check(label, (expected in same_moment) if expected else same_moment == "", same_moment)

sid = session()
try:
    now = time.time()
    task_id = "bsurv" + uuid.uuid4().hex[:4]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    fed = REVIEWER_FILE_TEXT + "\n\n" + PACKET_A
    command = packet_launch(write_packet(task_id, fed))
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800,
         durable_ts=now - 800)
    capture_launch(sid, "codex-" + task_id, command)
    captured = glob.glob(cwg.packet_capture_path(cwg.session_key(sid), "*"))
    check("the launch is captured before it runs", len(captured) == 1, captured)

    # A long session closes some other candidate while the lane is still running: the reviewer
    # notifies after that, and the capture is the only thing that can name its session.
    closing = write_transcript(base_events())
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    closed = run(STOP_HOOK, {
        "session_id": sid, "transcript_path": closing,
        "last_assistant_message": "[gate] no-change: read-only inspection, nothing was modified",
    })
    check("the candidate in between closes",
          closed.get("continue") is True and "decision" not in closed, closed)
    check("closing a candidate leaves the captures of launches still in flight",
          glob.glob(cwg.packet_capture_path(cwg.session_key(sid), "*")) == captured, sid)
    cwg.remove(closing)

    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800,
         durable_ts=now - 800)
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    events.append(bash_use(now - 700, "codex-" + task_id, command, run_in_background=True))
    events.append(tool_result(now - 699, "codex-" + task_id,
                              DETACHED_ACK.format(id=task_id, out=out_file)))
    events.append(notification(now - 600, task_id, out_file, "completed"))
    rollout_records([(now - 690, "developer", fed),
                     (now - 650, "assistant", codex_cli_output(review_text("APPROVED")))])
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": BACKGROUND_HIGH})
    check("a capture outlives the candidate it was taken under and still binds at the notification",
          result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    now = time.time()
    first = "bres1" + uuid.uuid4().hex[:4]
    second = "bres2" + uuid.uuid4().hex[:4]
    out_first = os.path.join(tasks_dir, first + ".output")
    out_second = os.path.join(tasks_dir, second + ".output")
    fed_first = REVIEWER_FILE_TEXT + "\n\n" + PACKET_A
    fed_second = REVIEWER_FILE_TEXT + "\n\n" + PACKET_B
    launch_first = packet_launch(write_packet(first, fed_first))
    launch_second = packet_launch(write_packet(second, fed_second))
    seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 1500, last_ts=now - 1400,
         durable_ts=now - 1400)
    capture_launch(sid, "codex-" + first, launch_first)
    capture_launch(sid, "codex-" + second, launch_second)
    events = [skill_use(now - 1490, "development-verification", "skill-dev")]
    simplify_wave(events, now - 1480, "simplify", SIMPLIFY_LENSES)
    for task_id, command, out_file, at in ((first, launch_first, out_first, now - 1200),
                                           (second, launch_second, out_second, now - 900)):
        events.append(bash_use(at, "codex-" + task_id, command, run_in_background=True))
        events.append(tool_result(at + 1, "codex-" + task_id,
                                  DETACHED_ACK.format(id=task_id, out=out_file)))
        events.append(notification(at + 100, task_id, out_file, "completed"))
    # `codex exec resume` appends to the first round's log: both rounds live in this one file,
    # and each has to be read within its own launch-to-notification window.
    rollout_records([
        (now - 1190, "developer", fed_first),
        (now - 1150, "assistant", codex_cli_output(review_text("REVISE"))),
        (now - 890, "developer", fed_second),
        (now - 850, "assistant", codex_cli_output(review_text("APPROVED"))),
    ])
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": BACKGROUND_HIGH})
    check("two rounds resumed into one rollout close the candidate",
          result.get("continue") is True and "decision" not in result, result)
    verdicts = [(note.get("task"), note.get("verdict")) for note in review_notes(sid)]
    check("each resumed round binds the verdict of its own window",
          verdicts == [(first, "REVISE"), (second, "APPROVED")], verdicts)
finally:
    cleanup(sid, locals().get("transcript"))
# --- staging and committing already approved bytes leave the fingerprint where it was
# The gate measures content, not edit events, so a candidate whose files only moved between the
# worktree, the index and HEAD is still the candidate the reviewer read. These run through the
# hook rather than through content_fingerprint alone, because what used to move was never a file:
# it was the set of paths the fingerprint is taken over, which `git add` grew (report 42f294ba).
with tempfile.TemporaryDirectory(prefix="cwg_commit_fp_") as tree:
    def repo_git(*args):
        return subprocess.run(["git", "-C", tree] + list(args), check=False,
                              capture_output=True, text=True, encoding="utf-8")

    repo_git("init", "-q")
    repo_git("config", "user.email", "gate@example.invalid")
    repo_git("config", "user.name", "Code Work Gate")
    os.makedirs(os.path.join(tree, "hooks"))
    reviewed = os.path.join(tree, "hooks", "reviewed.py")
    carried = os.path.join(tree, "hooks", "carried.py")
    for seeded in (reviewed, carried):
        with open(seeded, "w", encoding="utf-8") as stream:
            stream.write("print('base')" + chr(10))
    repo_git("add", "-A")
    repo_git("commit", "-q", "-m", "base")

    def mark_git(sid, *args):
        """A git command as production delivers it: the snapshot pair around the work."""
        mark_shell(sid, tree, " ".join(("git",) + args), action=lambda: repo_git(*args))

    def measured(sid):
        """The marker and the fingerprint its last content mark recorded."""
        entry = cwg.read_json(cwg.marker_path(cwg.session_key(sid))) or {}
        marks = entry.get("content_marks") or []
        return entry, (marks[-1].get("fp") if marks else None)

    def still_covers(entry, stamp):
        return gate.content_covers(entry, stamp, float(entry.get("last_durable_ts") or 0.0))

    sid = session()
    try:
        mark_edit(sid, tree, "hooks/reviewed.py", "print('reviewed')")
        # A lasting write this candidate never had attributed to it - the copy a mirror script
        # makes, another session's file in a shared home - which the commit below then names.
        with open(carried, "w", encoding="utf-8") as stream:
            stream.write("print('carried')" + chr(10))
        verdict_ts = time.time()
        _, at_verdict = measured(sid)
        mark_git(sid, "add", "-A")
        _, at_staged = measured(sid)
        mark_git(sid, "commit", "-q", "-m", "landed")
        marker_entry, at_landed = measured(sid)
        pending = repo_git("status", "--porcelain").stdout
        check("staging and committing the reviewed bytes never move the fingerprint",
              at_verdict == at_staged == at_landed and at_verdict is not None,
              (at_verdict, at_staged, at_landed))
        check("the commit did name a lasting path the candidate had not recorded",
              len(marker_entry.get("paths") or []) == 2
              and len(marker_entry.get("content_paths") or []) == 1,
              marker_entry)
        check("no byte on disk moved while the candidate was staged and committed", pending == "", pending)
        check("a verdict stated before the commit still covers the candidate",
              still_covers(marker_entry, verdict_ts), marker_entry)
    finally:
        cleanup(sid)

    sid = session()
    try:
        mark_edit(sid, tree, "hooks/reviewed.py", "print('first')")
        verdict_ts = time.time()
        mark_git(sid, "add", "-A")
        mark_edit(sid, tree, "hooks/reviewed.py", "print('second')")
        marker_entry, _ = measured(sid)
        check("a change to the reviewed bytes still retires the verdict",
              not still_covers(marker_entry, verdict_ts), marker_entry)
    finally:
        cleanup(sid)
        repo_git("checkout", "-q", "--", ".")
        repo_git("reset", "-q")

    sid = session()
    try:
        mark_edit(sid, tree, "hooks/reviewed.py", "print('staged')")
        mark_git(sid, "add", "-A")
        mark_edit(sid, tree, "hooks/reviewed.py", "print('on disk')")
        verdict_ts = time.time()
        _, at_verdict = measured(sid)
        mark_git(sid, "commit", "-q", "-m", "stale index")
        marker_entry, after_commit = measured(sid)
        check("committing a stale index still reads as a change of content",
              after_commit != at_verdict and not still_covers(marker_entry, verdict_ts),
              (at_verdict, after_commit))
    finally:
        cleanup(sid)
        repo_git("checkout", "-q", "--", ".")
        repo_git("reset", "-q")

    # Wider than the indexed tier, where nothing used to be measured at all: content alone is a
    # measurement, so a large candidate keeps its approval across its own commit (report
    # eedcca07, a mass restore that left 128 paths in the marker).
    sid = session()
    try:
        wide = ["hooks/wide{}.py".format(index)
                for index in range(marker_hook.FINGERPRINT_INDEXED_FILES + 6)]

        def write_wide():
            for relative in wide:
                with open(os.path.join(tree, *relative.split("/")), "w", encoding="utf-8") as stream:
                    stream.write("print('wide')" + chr(10))

        mark_shell(sid, tree, "python -c write_wide", action=write_wide)
        marker_entry, at_verdict = measured(sid)
        verdict_ts = time.time()
        check("a candidate wider than the indexed tier is measured, not unknown",
              at_verdict is not None
              and len(marker_entry.get("content_paths") or [])
              > marker_hook.FINGERPRINT_INDEXED_FILES,
              marker_entry)
        mark_git(sid, "add", "-A")
        mark_git(sid, "commit", "-q", "-m", "wide")
        marker_entry, at_landed = measured(sid)
        check("a wide candidate keeps its approval across staging and committing",
              at_landed == at_verdict and still_covers(marker_entry, verdict_ts),
              (at_verdict, at_landed))
        with open(os.path.join(tree, *wide[0].split("/")), "w", encoding="utf-8") as stream:
            stream.write("print('wide, changed')" + chr(10))
        check("a wide candidate still notices a byte that changed",
              marker_hook.content_fingerprint(marker_entry.get("content_paths")) != at_landed,
              at_landed)
    finally:
        cleanup(sid)
        repo_git("checkout", "-q", "--", ".")

    # --- after the receipt, committing and rebasing the closed bytes opens nothing (report 9dbbfe70)
    def close_now(sid, kind="verified"):
        marker, state_file = gate_paths(sid)
        entry = cwg.read_json(marker) or {}
        state = cwg.read_json(state_file) or {}
        return gate.close_cycle(marker, state_file, state, entry.get("last_ts"),
                                (kind, "STANDARD; checks passed"), cwg.session_key(sid))

    def open_cycle(sid):
        entry = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))
        return bool(entry) and not entry.get("closed")

    def commit_and_replay():
        """A commit, then what a rebase replaying it does to a file upstream never touched: the same
        bytes written again, with a new modification time."""
        repo_git("add", "-A")
        repo_git("commit", "-q", "-m", "closed work")
        with open(reviewed, "rb") as stream:
            content = stream.read()
        with open(reviewed, "wb") as stream:
            stream.write(content)
        stamp = os.stat(reviewed).st_mtime + 5
        os.utime(reviewed, (stamp, stamp))

    sid = session()
    try:
        mark_edit(sid, tree, "hooks/reviewed.py", "print('closed')")
        check("the cycle closes on its receipt", close_now(sid) and not open_cycle(sid))
        closed = (cwg.read_json(gate_paths(sid)[1]) or {}).get("closed_content") or {}
        check("the close records the lasting files it closed on",
              closed.get("paths") == [cwg.normalize_path(reviewed)] and closed.get("fp"), closed)
        mark_shell(sid, tree, "git add -A && git commit -q -m x && git rebase origin/main",
                   action=commit_and_replay)
        check("committing and rebasing the closed bytes opens no candidate", not open_cycle(sid),
              cwg.read_json(gate_paths(sid)[0]))
        with open(cwg.event_log_path(), encoding="utf-8") as stream:
            settled = [line for line in stream
                       if '"settled"' in line and cwg.session_key(sid) in line]
        check("because the closed candidate's bytes settled it, not because nothing was seen",
              len(settled) == 1, settled)
        with open(carried, "w", encoding="utf-8") as stream:
            stream.write("print('carried, later')" + chr(10))
        mark_shell(sid, tree, "git add -A && git commit -q -m y", action=lambda: (
            repo_git("add", "-A"), repo_git("commit", "-q", "-m", "not closed")))
        check("committing a file the closed candidate never held still opens one", open_cycle(sid),
              cwg.read_json(gate_paths(sid)[0]))
    finally:
        cleanup(sid)
        repo_git("checkout", "-q", "--", ".")

    sid = session()
    try:
        mark_edit(sid, tree, "hooks/reviewed.py", "print('closed again')")
        close_now(sid)
        mark_edit(sid, tree, "hooks/reviewed.py", "print('changed after the receipt')")
        check("a changed byte after the receipt opens a candidate", open_cycle(sid),
              cwg.read_json(gate_paths(sid)[0]))
        shell = {"session_id": sid, "cwd": tree, "tool_name": "Bash",
                 "tool_input": {"command": "sed -i s/x/x/ hooks/reviewed.py"}}
        scratch = os.path.join(SYSTEM_TEMP, "claude", "proj", "sid", "scratchpad", "note.md")
        for label, extra, kwargs in (
            ("an unresolved command", [], {"unresolved": True}),
            ("an unattributed change", [], {"unattributed_risk": "STANDARD"}),
            ("a throwaway file beside the closed one", [scratch], {}),
        ):
            close_now(sid)
            check("the closed bytes are recorded before {}".format(label),
                  ((cwg.read_json(gate_paths(sid)[1]) or {}).get("closed_content") or {}).get("fp"))
            marker_hook.record_paths(shell, [reviewed] + extra, **kwargs)
            check("{} never settles, even on the closed bytes".format(label), open_cycle(sid),
                  cwg.read_json(gate_paths(sid)[0]))
        close_now(sid, kind="anomaly-reported")
        check("an UNVERIFIED close clears the record",
              (cwg.read_json(gate_paths(sid)[1]) or {}).get("closed_content") == {},
              cwg.read_json(gate_paths(sid)[1]))
        marker_hook.record_paths(shell, [reviewed])
        check("so committing its bytes opens a candidate", open_cycle(sid),
              cwg.read_json(gate_paths(sid)[0]))
    finally:
        cleanup(sid)
        repo_git("checkout", "-q", "--", ".")

    sid = session()
    try:
        mark_edit(sid, tree, "hooks/reviewed.py", "print('kept across a throwaway close')")
        close_now(sid)
        recorded = (cwg.read_json(gate_paths(sid)[1]) or {}).get("closed_content")
        scratch = os.path.join(SYSTEM_TEMP, "claude", "proj", "sid", "scratchpad", "run.py")
        marker_hook.record_paths({"session_id": sid, "cwd": tree, "tool_name": "Write",
                                  "tool_input": {"file_path": scratch}}, [scratch])
        close_now(sid, kind="operational")
        check("a close with no lasting file leaves the earlier record standing",
              (cwg.read_json(gate_paths(sid)[1]) or {}).get("closed_content") == recorded and recorded,
              cwg.read_json(gate_paths(sid)[1]))
    finally:
        cleanup(sid)
        repo_git("checkout", "-q", "--", ".")

sid = session()
try:
    marker, _ = gate_paths(sid)
    legacy = {"first_ts": time.time(), "last_ts": time.time(), "last_path": "c:/repo/a.py",
              "paths": ["c:/repo/a.py", "c:/repo/b.py"], "edits": 2}
    check("seed legacy marker", cwg.write_json(marker, legacy), legacy)
    resumed = marker_hook.cycle_start(marker, time.time(), None, ["c:/repo/a.py"])
    check("a marker written before the domain existed keeps measuring what it measured",
          resumed["content_paths"] == legacy["paths"], resumed)
finally:
    cleanup(sid)


# A lasting path whose folder is gone counts for the repository above it only if git there does not
# ignore it: a removed worktree's file walked up into the main checkout, which excludes
# `.claude/worktrees/` and never held it, and its snapshot used the extra-repository budget on every
# command (report a378343e); a tracked file deleted with its folder keeps its repository (G24 review).
with tempfile.TemporaryDirectory(prefix="cwg_removed_tree_") as main:
    candidate_repo(main, "main-line")
    os.makedirs(os.path.join(main, ".claude", "worktrees"))
    with open(os.path.join(main, ".git", "info", "exclude"), "a", encoding="utf-8") as stream:
        stream.write("**/.claude/worktrees/\n")
    gone = cwg.normalize_path(os.path.join(main, ".claude", "worktrees", "removed", "src", "gone.py"))
    deleted = cwg.normalize_path(os.path.join(main, "src", "deleted.py"))
    os.makedirs(os.path.join(main, "lib"))
    with open(os.path.join(main, "lib", "tracked.py"), "w", encoding="utf-8") as stream:
        stream.write("tracked = 1\n")
    commit_paths(main, "lib/tracked.py", "tracked")
    shutil.rmtree(os.path.join(main, "lib"))
    tracked = cwg.normalize_path(os.path.join(main, "lib", "tracked.py"))
    untracked = cwg.normalize_path(os.path.join(main, "scratch", "draft.py"))
    # Force-added under an ignored folder and committed, then removed with the removal staged: the
    # index no longer holds it and git calls it ignored, but HEAD still does (G24 review, round 3).
    with open(os.path.join(main, ".git", "info", "exclude"), "a", encoding="utf-8") as stream:
        stream.write("vendor/\n")
    os.makedirs(os.path.join(main, "vendor"))
    with open(os.path.join(main, "vendor", "lib.py"), "w", encoding="utf-8") as stream:
        stream.write("vendored = 1\n")
    subprocess.run(["git", "-C", main, "add", "-f", "--", "vendor/lib.py"], check=True)
    subprocess.run(["git", "-C", main, "-c", "user.name=Code Work Gate", "-c", "user.email=gate@example.invalid",
                    "commit", "--quiet", "-m", "vendored"], check=True)
    shutil.rmtree(os.path.join(main, "vendor"))
    subprocess.run(["git", "-C", main, "add", "-A"], check=True)
    vendored = cwg.normalize_path(os.path.join(main, "vendor", "lib.py"))
    for label, path, expected in (
            ("a removed worktree's file, which the main checkout ignores, is measured on its own", gone, ([], [gone])),
            ("a file deleted from a folder that is still there stays with its repository", deleted, None),
            ("a tracked file deleted with its folder stays with its repository", tracked, None),
            ("a missing file the repository does not ignore stays with it", untracked, None),
            ("an ignored file HEAD still holds, its removal staged, stays with its repository", vendored, None)):
        placed = marker_hook.candidate_trees({"paths": [path]}, "")
        check(label, placed == (expected or ([cwg.normalize_path(main)], [])), placed)

# --- the snapshot follows the candidate into its other repositories and its loose lasting files
# (reports b803660c, 5aadd867, 946d53ef)
with tempfile.TemporaryDirectory(prefix="cwg_candidate_trees_") as base:
    repo_a, repo_b, repo_c = (os.path.join(base, name) for name in ("a", "b", "c"))
    for directory, branch in ((repo_a, "publish-a"), (repo_b, "candidate-b"), (repo_c, "other-c")):
        candidate_repo(directory, branch)
    helper = os.path.join(base, "helpers", "patch.py")
    os.makedirs(os.path.dirname(helper))
    with open(helper, "w", encoding="utf-8") as stream:
        stream.write("print('patch')" + chr(10))

    def mark_moving(sid, start, end, command, action=None):
        """A shell call whose command moves the shell: the PostToolUse cwd is where it ended."""
        payload = {"session_id": sid, "tool_use_id": "moving-{}".format(uuid.uuid4().hex),
                   "tool_name": "Bash", "tool_input": {"command": command}}
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse", cwd=start))
        if action:
            action()
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse", cwd=end))

    def rewrite(path, content):
        def action():
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content + chr(10))
        return action

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        mark_edit(sid, repo_b, "src/candidate.py", "value = 2")
        run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Write",
                        "cwd": repo_b, "tool_input": {"file_path": helper}})
        anchored = cwg.read_json(marker)
        check("the candidate holds a file in its repository and a loose one",
              any(path.endswith("/b/src/candidate.py") for path in anchored.get("paths") or [])
              and cwg.normalize_path(helper) in (anchored.get("paths") or []), anchored)

        mark_shell(sid, repo_a, "glab mr update 1 --ready | python -c pass")
        quiet = cwg.read_json(marker)
        check("a command run from another repository that changes nothing keeps the verdict anchor",
              quiet["last_durable_ts"] == anchored["last_durable_ts"]
              and quiet.get("content_marks") == anchored.get("content_marks"), (anchored, quiet))

        mark_moving(sid, repo_a, repo_b, "cd ../b && glab mr view 1 | head -3")
        check("a command that moves the shell into the candidate's repository still compares",
              cwg.read_json(marker)["last_durable_ts"] == anchored["last_durable_ts"], cwg.read_json(marker))

        mark_moving(sid, repo_a, repo_c, "cd ../c && python tools/gen.py")
        entered = cwg.read_json(marker)
        check("a command that changes into another repository by a literal path is measured there",
              entered["last_durable_ts"] == anchored["last_durable_ts"], entered)

        mark_moving(sid, repo_a, repo_c, 'cd "$TARGET" && python tools/gen.py')
        unseen = cwg.read_json(marker)
        check("a command that ends in a repository no snapshot covered expires the anchor",
              unseen["last_durable_ts"] > anchored["last_durable_ts"], unseen)
        cause = (unseen.get("content_marks") or [{}])[-1].get("cause") or {}
        check("the barrier keeps where the command ended and what it was",
              cause.get("landed") == cwg.normalize_path(repo_c).rstrip("/")
              and cause.get("command") == "cd" and cause.get("reason") == "unresolved-write-capable", cause)

        mark_shell(sid, repo_a, "python tools/bump.py",
                   action=rewrite(os.path.join(repo_b, "src", "candidate.py"), "value = 3"))
        rewritten = cwg.read_json(marker)
        check("a command run elsewhere that rewrites the candidate records a measured change",
              rewritten["last_durable_ts"] > unseen["last_durable_ts"]
              and not (rewritten.get("content_marks") or [{}])[-1].get("unknown"), rewritten)

        mark_shell(sid, repo_b, "python ../helpers/patch.py", action=rewrite(helper, "print('again')"))
        helper_changed = cwg.read_json(marker)
        check("a loose lasting file the command rewrote is a measured change, not an unknown one",
              helper_changed["last_durable_ts"] > rewritten["last_durable_ts"]
              and not (helper_changed.get("content_marks") or [{}])[-1].get("unknown"), helper_changed)
    finally:
        cleanup(sid)


# --- a command's own literal directory changes decide where it is measured
# (reports 4b840373, 0e4aedc8, a269a6fc)
with tempfile.TemporaryDirectory(prefix="cwg_directory_plan_") as base:
    left, right = os.path.join(base, "left"), os.path.join(base, "right")
    os.makedirs(left)
    os.makedirs(right)
    forward = base.replace(chr(92), "/")
    # Git Bash's spelling of the drive path; a POSIX shell names the directory itself.
    git_bash = "/" + forward[0].lower() + forward[2:] if os.name == "nt" else forward
    right_forward = right.replace(chr(92), "/")
    right_backslashed = right.replace("/", chr(92))

    def plan(command, cwd, shell):
        start, targets = marker_hook.directory_plan(command, cwd, shell)
        named = [None if path is None else os.path.relpath(path, base) for path in [start] + targets]
        return named[0], named[1:]

    for label, command, cwd, shell, expected in (
        ("an absolute target is where the command works",
         "cd {} && python y".format(right_forward), left, "Bash", ("right", ["right"])),
        ("a Git Bash drive path after an assignment",
         'REVIEW_ID=r2; cd "{}/right" && codex exec - < p.md'.format(git_bash), base, "Bash", ("right", ["right"])),
        ("a variable target loses the thread", 'cd "$W" && make', left, "Bash", (None, [])),
        ("a relative target resolves against the directory",
         "cd ../right && glab mr view 1 | head -3", left, "Bash", ("right", ["right"])),
        ("a change after the work is a landing, not a start",
         "make && cd {}".format(right_forward), left, "Bash", ("left", ["right"])),
        ("a change inside a pipeline moves nothing",
         "cd {} | cat; wc f".format(right_forward), left, "Bash", ("left", [])),
        ("a subshell keeps its change to itself",
         "(cd {} && make)".format(right_forward), left, "Bash", ("left", [])),
        ("a directory that does not exist is no change",
         "cd {}/missing && make".format(forward), left, "Bash", (None, [])),
        ("PowerShell's Set-Location with a named path",
         "Set-Location -Path '{}'; git status".format(right), left, "PowerShell", ("right", ["right"])),
        ("an unquoted backslash path in bash is not that path",
         "cd {} && make".format(right_backslashed), left, "Bash", (None, [])),
        ("a heredoc body is not read as shell",
         "python - <<'PY'\ncd {}\nPY".format(right_forward), left, "Bash", ("left", [])),
        ("pushd is a change too", "pushd ../right && make && popd", left, "Bash", ("right", ["right"])),
        ("assignments alone run nowhere else", 'S="x y"; W="C:/q"', left, "Bash", ("left", [])),
        ("unbalanced quotes are not read", "cd {} && echo it's".format(right_forward), left, "Bash", (None, [])),
        ("a comment line before the change runs nothing",
         "# round 2\nREVIEW_ID=r2; cd {} && make".format(right_forward), left, "Bash", ("right", ["right"])),
    ):
        check("directory_plan: " + label, plan(command, cwd, shell) == expected,
              (command, plan(command, cwd, shell)))

check("an assignment ahead of the command does not hide its executable",
      marker_hook.command_label("REVIEW_ID=r2; cd /c/tmp/x && codex exec - < p.md") == "cd"
      and marker_hook.command_label("A=1; B=2; git push") == "git"
      and marker_hook.command_label("A=1;B=2;C=3;D=4;E=5;git push") == "git",
      marker_hook.command_label("A=1;B=2;C=3;D=4;E=5;git push"))

# --- a bookkeeping script is ignored only where an interpreter starting its segment runs it (G1 F7/F8)
home_hooks = os.path.join(CLAUDE_CONFIG_DIR, "hooks")
home_hook_change = os.path.join(home_hooks, "x.py")
for label, command, tool, expect in (
    ("a copy onto a hook through a directory called py still names the home",
     'cp build/py "{}"'.format(os.path.join(home_hooks, "codex_lane.py")), "Bash", True),
    ("the py launcher with a version flag runs a bookkeeping script",
     'py -3 "{}" list'.format(os.path.join(home_hooks, "gate_inbox.py")), "Bash", False),
    ("PowerShell's call operator with single quotes runs a bookkeeping script",
     "& 'C:\\Python312\\python.exe' '{}' show 1234abcd".format(os.path.join(home_hooks, "gate_inbox.py")),
     "PowerShell", False),
    ("a versioned interpreter runs a bookkeeping script",
     'python3.12 "{}" status'.format(os.path.join(home_hooks, "chip_handoff.py")), "Bash", False),
    ("interpreter options before the script",
     'python -X utf8 -u "{}" check'.format(os.path.join(home_hooks, "codex_lane.py")), "Bash", False),
    ("a lower-case -x takes no value, unlike -X",
     'python -x "{}" check'.format(os.path.join(home_hooks, "codex_lane.py")), "Bash", False),
    ("inline code naming a bookkeeping script is no run of it",
     'python -c "open(r\'{}\', \'w\')"'.format(os.path.join(home_hooks, "codex_lane.py")), "Bash", True),
    ("a bookkeeping script named mid-segment is an argument, not a run",
     'echo python "{}" > notes.txt'.format(os.path.join(home_hooks, "gate_inbox.py")), "Bash", True),
):
    got = marker_hook.on_home_ground(home_hook_change, "C:/tmp/worktree", ["C:/tmp/worktree"],
                                     {"tool_name": tool, "tool_input": {"command": command}})
    check("on_home_ground: " + label, got is expect, command)

# --- a command that begins by changing into a repository is measured there, wherever it started
with tempfile.TemporaryDirectory(prefix="cwg_entered_") as base:
    home_repo, other_repo = os.path.join(base, "home"), os.path.join(base, "other")
    for directory, branch in ((home_repo, "candidate"), (other_repo, "elsewhere")):
        candidate_repo(directory, branch)
    loose_dir = os.path.join(base, "loose")
    os.makedirs(loose_dir)
    home_forward = home_repo.replace(chr(92), "/")
    other_forward = other_repo.replace(chr(92), "/")

    def shell_between(sid, start, end, command, action=None, **extra):
        """A shell call that starts in one directory and whose shell ends in another; `extra` adds
        payload fields, such as the `agent_type` a hook inside a subagent receives."""
        payload = dict({"session_id": sid, "tool_use_id": "entered-{}".format(uuid.uuid4().hex),
                        "tool_name": "Bash", "tool_input": {"command": command}}, **extra)
        run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse", cwd=start))
        if action:
            action()
        run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse", cwd=end))

    def write_in_other():
        with open(os.path.join(other_repo, "src", "new_module.py"), "w", encoding="utf-8") as stream:
            stream.write("created = True" + chr(10))

    sid = session()
    try:
        marker, _ = gate_paths(sid)
        mark_edit(sid, home_repo, "src/candidate.py", "value = 2")
        anchored = cwg.read_json(marker)

        shell_between(sid, home_repo, other_repo,
                      'cd {} && S="x" && python - "$S" <<\'PY\'\nprint(1)\nPY'.format(other_forward))
        check("a command that changes into a repository outside the candidate is measured there",
              cwg.read_json(marker)["last_durable_ts"] == anchored["last_durable_ts"], cwg.read_json(marker))

        shell_between(sid, loose_dir, loose_dir,
                      'REVIEW_ID=r2; cd "{}" && timeout 3600 codex exec - < p.md'.format(home_forward))
        check("a review launched from outside any repository into the candidate's keeps its verdict",
              cwg.read_json(marker)["last_durable_ts"] == anchored["last_durable_ts"], cwg.read_json(marker))

        shell_between(sid, loose_dir, loose_dir, "python tools/bump.py")
        outside = cwg.read_json(marker)
        check("a command that works outside any repository still expires the anchor",
              outside["last_durable_ts"] > anchored["last_durable_ts"], outside)

        shell_between(sid, home_repo, home_repo,
                      "cd {} && python gen.py && cd {}".format(other_forward, home_forward),
                      action=write_in_other)
        written = cwg.read_json(marker)
        check("a write in a repository the command passed through is a measured path",
              any(path.endswith("/other/src/new_module.py") for path in written.get("paths") or [])
              and not (written.get("content_marks") or [{}])[-1].get("unknown"), written)
    finally:
        cleanup(sid)

    def write_in_home():
        with open(os.path.join(home_repo, "src", "generated.py"), "w", encoding="utf-8") as stream:
            stream.write("generated = True" + chr(10))

    # The repository the hook was given stays measured when the command starts somewhere else.
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        shell_between(sid, home_repo, other_repo, "cd {} && python build.py".format(other_forward),
                      action=write_in_home)
        started_elsewhere = cwg.read_json(marker) or {}
        check("a command that starts elsewhere still measures the repository it was run from",
              any(path.endswith("/home/src/generated.py") for path in started_elsewhere.get("paths") or []),
              started_elsewhere)
    finally:
        cleanup(sid)

    third_repo = os.path.join(base, "third")
    candidate_repo(third_repo, "third")
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        mark_edit(sid, third_repo, "src/candidate.py", "value = 3")
        anchored = cwg.read_json(marker)
        shell_between(sid, home_repo, home_repo, "cd {} && python build.py && cd -".format(other_forward))
        check("a command that returns to the repository it was run from lands on measured ground",
              cwg.read_json(marker)["last_durable_ts"] == anchored["last_durable_ts"], cwg.read_json(marker))
    finally:
        cleanup(sid)

    saved_budget = marker_hook.EXTRA_SNAPSHOT_BUDGET
    marker_hook.EXTRA_SNAPSHOT_BUDGET = -1.0
    try:
        over_budget = marker_hook.shell_snapshot(home_repo, None, [other_repo])
    finally:
        marker_hook.EXTRA_SNAPSHOT_BUDGET = saved_budget
    within_budget = marker_hook.shell_snapshot(home_repo, None, [other_repo])
    check("repositories past the time budget are left out of the snapshot, and counted",
          over_budget.get("repos") == [] and over_budget.get("skipped") == 1
          and len(within_budget.get("repos") or []) == 1 and not within_budget.get("skipped"),
          (over_budget, within_budget.get("repos")))

# --- a cycle that replaces an open one says so, and so do the block and the candidate note (a269a6fc)
with tempfile.TemporaryDirectory(prefix="cwg_displaced_") as repo:
    candidate_repo(repo, "first-branch")
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        mark_edit(sid, repo, "src/first.py")
        opened = cwg.read_json(marker)
        switch_branch(repo, "second-branch")
        second = os.path.join(repo, "src", "second.py")
        with open(second, "w", encoding="utf-8") as stream:
            stream.write("second = True" + chr(10))
        replacing = run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Write",
                                    "cwd": repo, "tool_input": {"file_path": second}})
        note = (replacing.get("hookSpecificOutput") or {}).get("additionalContext", "")
        replaced = cwg.read_json(marker)
        displaced = replaced.get("displaced") or {}
        check("a new cycle records the open one it replaced",
              replaced["first_ts"] != opened["first_ts"] and displaced.get("opened") == opened["first_ts"]
              and str(displaced.get("was")).endswith("first-branch")
              and str(displaced.get("now")).endswith("second-branch")
              and any(path.endswith("/src/second.py") for path in displaced.get("by") or []), replaced)
        check("the candidate note says which candidate it replaced and why",
              "It replaced the candidate open since" in note and "first-branch → second-branch" in note
              and "second.py not among its files" in note, note)
        check("the candidate note names the candidate's files", "(1 lasting file: second.py)" in note, note)
        mark_edit(sid, repo, "src/second.py", "second = 2")
        check("the record of the replacement stays with the cycle",
              (cwg.read_json(marker).get("displaced") or {}).get("opened") == opened["first_ts"],
              cwg.read_json(marker))
    finally:
        cleanup(sid)

now = time.time()
detail_entry = {
    "first_ts": now - 600, "last_ts": now - 10, "last_durable_ts": now - 100,
    "paths": ["c:/repo/src/auth/session.ts"],
    "content_marks": [
        {"ts": now - 500, "fp": "aaa"},
        {"ts": now - 100, "fp": "aaa", "unknown": True,
         "cause": {"reason": "unresolved-write-capable", "tool": "Bash", "command": "cd",
                   "landed": "c:/tmp/wt-other"}},
    ],
    "displaced": {"ts": now - 600, "opened": now - 3000, "was": "c:/repo#refs/heads/one",
                  "now": "c:/repo#refs/heads/two", "idle": False, "by": ["c:/repo/src/updater.go"]},
}
approved_evidence = {"ordinary_reviews": [(now - 200, "APPROVED")],
                     "review_events": [(now - 200, "ordinary", "APPROVED")]}
detail = gate.block_detail(gate.HIGH_STALE_REASON, detail_entry, approved_evidence)
check("a stale approval is explained by what came after it",
      "the last APPROVED is filed at" in detail
      and "unresolved-write-capable `cd` that ended in c:/tmp/wt-other" in detail, detail)
check("the block says when evidence starts counting and which candidate that moment replaced",
      "evidence for this candidate counts from" in detail and "branch one → two" in detail
      and "updater.go not among its files" in detail, detail)
rounds_detail = gate.block_detail(
    "an invoked review has no terminal APPROVED verdict", detail_entry,
    {"ordinary_reviews": [(now - 300, "REVISE")],
     "review_events": [(now - 300, "ordinary", "REVISE"), (now - 250, "unbound", None)]})
check("a round block lists the rounds it read and the results it could not use",
      "ordinary rounds read:" in rounds_detail and "REVISE" in rounds_detail
      and "without a usable verdict at" in rounds_detail, rounds_detail)
check("a missing receipt needs no detail",
      gate.block_detail("terminal receipt is missing or malformed", detail_entry, approved_evidence) == "",
      "empty")
check("a marker without its opening state says so",
      "records no repository" in gate.restoration_blocker({"paths": ["c:/repo/src/a.py"]}),
      gate.restoration_blocker({"paths": ["c:/repo/src/a.py"]}))

with tempfile.TemporaryDirectory(prefix="cwg_restoration_detail_") as repo:
    candidate_repo(repo, "restored")
    opening = {
        "identity": marker_hook.candidate_identity(repo),
        "head_at_start": marker_hook.head_commit(repo),
        "refs_at_start": cwg.refs_digest(repo),
        "paths": [cwg.normalize_path(os.path.join(repo, "src", "seed.py"))],
    }
    check("a repository on its opening commit and clean has nothing to report",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))
    with open(os.path.join(repo, "src", "seed.py"), "w", encoding="utf-8") as stream:
        stream.write("value = 2" + chr(10))
    commit_paths(repo, "src/seed.py", "moved on")
    check("a repository that moved on names the commit",
          gate.restoration_blocker(opening).startswith("HEAD is "), gate.restoration_blocker(opening))

# An aborted merge after a fetch, beside test output nobody touched (report fb6a9be6).
with tempfile.TemporaryDirectory(prefix="cwg_restoration_scope_") as repo:
    candidate_repo(repo, "restored")
    seed_path = cwg.normalize_path(os.path.join(repo, "src", "seed.py"))
    opening = {
        "identity": marker_hook.candidate_identity(repo),
        "head_at_start": marker_hook.head_commit(repo),
        "refs_at_start": cwg.refs_digest(repo),
        "paths": [seed_path],
    }
    subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/restored", "HEAD"], check=True)
    check("a fetch that moved a remote-tracking ref does not keep the candidate open",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))
    # Several lasting paths go to `check-ignore` in one call (report f82c87c7): an aborted merge's
    # file that is gone again, and one under an ignored directory.
    merged_away = cwg.normalize_path(os.path.join(repo, "src", "merged_away.py"))
    check("several lasting paths, none ignored, read as restored",
          gate.restoration_blocker(dict(opening, paths=[seed_path, merged_away])) == "",
          gate.restoration_blocker(dict(opening, paths=[seed_path, merged_away])))
    with open(os.path.join(repo, ".git", "info", "exclude"), "a", encoding="utf-8") as stream:
        stream.write("build/\n")
    several = dict(opening, paths=[seed_path, merged_away,
                                   cwg.normalize_path(os.path.join(repo, "build", "generated.py"))])
    check("an ignored one among several keeps it open, named",
          "gitignored" in gate.restoration_blocker(several) and "generated.py" in gate.restoration_blocker(several),
          gate.restoration_blocker(several))
    os.makedirs(os.path.join(repo, "test-results"), exist_ok=True)
    with open(os.path.join(repo, "test-results", "run.json"), "w", encoding="utf-8") as stream:
        stream.write("{}")
    check("an untracked file the candidate never touched does not keep it open",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))
    unresolved = dict(opening, paths=[seed_path, cwg.SHELL_MUTATION_PATH])
    check("after a command the gate could not resolve, the whole tree has to be clean",
          "could not resolve" in gate.restoration_blocker(unresolved), gate.restoration_blocker(unresolved))
    with open(os.path.join(repo, "src", "seed.py"), "w", encoding="utf-8") as stream:
        stream.write("value = 3" + chr(10))
    check("a path the candidate changed that still differs keeps it open, named",
          "still differs from HEAD (seed.py)" in gate.restoration_blocker(opening), gate.restoration_blocker(opening))
    subprocess.run(["git", "-C", repo, "checkout", "--quiet", "--", "src/seed.py"], check=True)
    subprocess.run(["git", "-C", repo, "branch", "side"], check=True)
    check("a branch that moved with no commit made here does not keep it open (report 2b8bbfb1)",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))
    subprocess.run(["git", "-C", repo, "tag", "marked"], check=True)
    check("a tag made since the opening keeps it open",
          "a ref other than a branch moved" in gate.restoration_blocker(opening), gate.restoration_blocker(opening))
    subprocess.run(["git", "-C", repo, "tag", "-d", "marked"], check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=Code Work Gate", "-c", "user.email=gate@example.invalid",
                    "notes", "add", "-m", "noted", "HEAD"], check=True, capture_output=True)
    check("a note made since the opening keeps it open too",
          "a ref other than a branch moved" in gate.restoration_blocker(opening), gate.restoration_blocker(opening))
    subprocess.run(["git", "-C", repo, "update-ref", "-d", "refs/notes/commits"], check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "branch", "-D", "side"], check=True, capture_output=True)
    everything = subprocess.run(["git", "-C", repo, "for-each-ref", "--format=%(refname) %(objectname)"],
                                capture_output=True, text=True, check=True).stdout
    legacy = dict(opening, refs_at_start=hashlib.sha256(everything.encode("utf-8")).hexdigest())
    check("a marker that digested every ref, remote-tracking ones included, still reads as restored",
          gate.restoration_blocker(legacy) == "", gate.restoration_blocker(legacy))
    local = dict(opening, refs_at_start=hashlib.sha256(cwg.local_refs(everything).encode("utf-8")).hexdigest())
    check("a marker that digested every local ref still reads as restored",
          gate.restoration_blocker(local) == "", gate.restoration_blocker(local))

# A repository's worktrees share one ref store: another session committing on its own branch moves a
# ref and makes nothing of this candidate's, while a commit made here on another branch is its work
# wherever it went (report 2b8bbfb1).
with tempfile.TemporaryDirectory(prefix="cwg_restoration_worktrees_") as base:
    repo, neighbour = os.path.join(base, "main"), os.path.join(base, "neighbour")
    candidate_repo(repo, "main")
    subprocess.run(["git", "-C", repo, "worktree", "add", "--quiet", "-b", "neighbour", neighbour],
                   check=True, capture_output=True)
    seed_path = cwg.normalize_path(os.path.join(repo, "src", "seed.py"))

    def worktree_opening(**extra):
        return dict({"identity": marker_hook.candidate_identity(repo), "head_at_start": marker_hook.head_commit(repo),
                     "refs_at_start": cwg.refs_digest(repo), "first_ts": time.time(), "paths": [seed_path]}, **extra)

    def commit_on(tree, branch, text):
        subprocess.run(["git", "-C", tree, "checkout", "--quiet", "-B", branch], check=True, capture_output=True)
        with open(os.path.join(tree, "src", "seed.py"), "w", encoding="utf-8") as stream:
            stream.write(text + chr(10))
        commit_paths(tree, "src/seed.py", text)

    opening = worktree_opening()
    commit_on(neighbour, "neighbour", "value = 'neighbour'")
    check("another worktree's commit on its own branch does not keep this candidate open",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))
    commit_on(repo, "side", "value = 'side'")
    subprocess.run(["git", "-C", repo, "checkout", "--quiet", "main"], check=True, capture_output=True)
    blocker = gate.restoration_blocker(opening)
    check("a commit made here on another branch keeps it open, naming the branch",
          "is on refs/heads/side" in blocker, blocker)
    subprocess.run(["git", "-C", repo, "branch", "--quiet", "-D", "side"], check=True, capture_output=True)
    check("the same commit, gone with its branch and never pushed, is no lasting change",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))
    started = time.time()
    commit_on(repo, "during", "value = 'during'")
    subprocess.run(["git", "-C", repo, "checkout", "--quiet", "main"], check=True, capture_output=True)
    # The marker records the opening command only after it finished, here five seconds later.
    late = dict(worktree_opening(), first_ts=time.time() + 5, opened_at=started)
    blocker = gate.restoration_blocker(late)
    check("a commit the opening command made itself is the candidate's",
          "is on refs/heads/during" in blocker, blocker)

# A commit pushed while the candidate was open, then reset away or left on a deleted branch, has left
# the repository though every local ref is back (G12 review, F1).
with tempfile.TemporaryDirectory(prefix="cwg_restoration_push_") as base:
    remote = os.path.join(base, "remote.git")
    subprocess.run(["git", "init", "--bare", "--quiet", remote], check=True)
    for trigger in ("reset", "branch"):
        repo = os.path.join(base, "work-" + trigger)
        candidate_repo(repo, "main")
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", remote], check=True)
        subprocess.run(["git", "-C", repo, "push", "--quiet", "origin", "main:base-" + trigger],
                       check=True, capture_output=True)
        time.sleep(1.1)
        opening = {
            "identity": marker_hook.candidate_identity(repo),
            "head_at_start": marker_hook.head_commit(repo),
            "refs_at_start": cwg.refs_digest(repo),
            "paths": [cwg.normalize_path(os.path.join(repo, "src", "seed.py"))],
            "first_ts": time.time(),
        }
        if trigger == "branch":
            subprocess.run(["git", "-C", repo, "checkout", "--quiet", "-b", "side"], check=True)
        with open(os.path.join(repo, "src", "seed.py"), "w", encoding="utf-8") as stream:
            stream.write("value = 9" + chr(10))
        commit_paths(repo, "src/seed.py", "published")
        target = "side" if trigger == "branch" else "main:pushed-" + trigger
        subprocess.run(["git", "-C", repo, "push", "--quiet", "origin", target], check=True, capture_output=True)
        if trigger == "branch":
            subprocess.run(["git", "-C", repo, "checkout", "--quiet", "main"], check=True)
            subprocess.run(["git", "-C", repo, "branch", "--quiet", "-D", "side"], check=True)
        else:
            subprocess.run(["git", "-C", repo, "reset", "--quiet", "--hard", "HEAD~1"], check=True)
        check("a commit pushed from the candidate and then {} keeps it open".format(
                  "left on a deleted branch" if trigger == "branch" else "reset away"),
              "was pushed" in gate.restoration_blocker(opening), gate.restoration_blocker(opening))
# Only commits made here count: HEAD standing on a fetched commit — a rebase probe, a look at the
# upstream branch, a fast-forward pull undone — publishes nothing (G12 review, F5).
with tempfile.TemporaryDirectory(prefix="cwg_restoration_visits_") as base:
    def visit_git(repo, *arguments, check_exit=True):
        result = subprocess.run(["git", "-C", repo, "-c", "user.name=Code Work Gate",
                                 "-c", "user.email=gate@example.invalid"] + list(arguments),
                                capture_output=True, text=True)
        if check_exit and result.returncode != 0:
            raise RuntimeError("git {} failed: {}".format(arguments, result.stderr))
        return result

    def visit_write(repo, text):
        with open(os.path.join(repo, "seed.py"), "w", encoding="utf-8") as stream:
            stream.write(text + chr(10))

    remote = os.path.join(base, "remote.git")
    subprocess.run(["git", "init", "--bare", "--quiet", remote], check=True)
    peer = os.path.join(base, "peer")
    subprocess.run(["git", "clone", "--quiet", remote, peer], check=True, capture_output=True)
    visit_git(peer, "checkout", "--quiet", "-B", "main")
    visit_write(peer, "value = 1")
    visit_git(peer, "add", ".")
    visit_git(peer, "commit", "--quiet", "-m", "seed")
    visit_git(peer, "push", "--quiet", "origin", "main")
    visit_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    def visit_clone(name, local_commit=False):
        repo = os.path.join(base, name)
        subprocess.run(["git", "clone", "--quiet", remote, repo], check=True, capture_output=True)
        if local_commit:
            visit_write(repo, "value = local")
            visit_git(repo, "commit", "--quiet", "-am", "local")
        return repo

    def visit_upstream(text):
        visit_git(peer, "pull", "--quiet", "--rebase", "origin", "main", check_exit=False)
        visit_write(peer, text)
        visit_git(peer, "commit", "--quiet", "-am", "upstream " + text)
        visit_git(peer, "push", "--quiet", "origin", "main")

    def visit_opening(repo):
        time.sleep(1.1)
        return {"identity": marker_hook.candidate_identity(repo), "head_at_start": marker_hook.head_commit(repo),
                "refs_at_start": cwg.refs_digest(repo), "first_ts": time.time(),
                "paths": [cwg.normalize_path(os.path.join(repo, "seed.py"))]}

    repo = visit_clone("rebase-probe", local_commit=True)
    visit_upstream("upstream-1")
    visit_git(repo, "fetch", "--quiet", "origin")
    opening = visit_opening(repo)
    visit_git(repo, "rebase", "origin/main", check_exit=False)
    visit_git(repo, "rebase", "--abort")
    check("a rebase probe onto the fetched upstream, aborted, reads as restored",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))

    repo = visit_clone("detached-look", local_commit=True)
    visit_upstream("upstream-2")
    visit_git(repo, "fetch", "--quiet", "origin")
    opening = visit_opening(repo)
    visit_git(repo, "checkout", "--quiet", "--detach", "origin/main")
    visit_git(repo, "checkout", "--quiet", "-")
    check("a detached look at the upstream branch and back reads as restored",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))

    repo = visit_clone("ff-pull")
    visit_upstream("upstream-3")
    opening = visit_opening(repo)
    visit_git(repo, "pull", "--quiet", "--ff-only", "origin", "main")
    visit_git(repo, "reset", "--quiet", "--hard", opening["head_at_start"])
    check("a fast-forward pull undone by a reset reads as restored",
          gate.restoration_blocker(opening) == "", gate.restoration_blocker(opening))

    repo = visit_clone("merge-push", local_commit=True)
    visit_upstream("upstream-4")
    visit_git(repo, "fetch", "--quiet", "origin")
    opening = visit_opening(repo)
    visit_git(repo, "merge", "--quiet", "-X", "ours", "--no-edit", "origin/main")
    visit_git(repo, "push", "--quiet", "origin", "HEAD:merged")
    visit_git(repo, "reset", "--quiet", "--hard", opening["head_at_start"])
    blocker = gate.restoration_blocker(opening)
    check("a merge commit made here, pushed and reset away keeps it open, naming the branch",
          "was pushed (refs/remotes/origin/merged)" in blocker, blocker)
for subject, made in (("commit: fix", True), ("commit (amend): fix", True), ("commit (merge): m", True),
                      ("merge origin/main: Merge made by the 'ort' strategy.", True),
                      ("pull origin main: Merge made by the 'ort' strategy.", True), ("cherry-pick: x", True),
                      ("rebase (pick): x", True), ("merge origin/main: Fast-forward", False),
                      ("pull --ff-only origin main: Fast-forward", False), ("rebase (start): checkout origin/main", False),
                      ("rebase (abort): returning to refs/heads/main", False), ("checkout: moving from a to b", False),
                      ("reset: moving to HEAD~1", False)):
    check("a reflog entry '{}' {} a commit made here".format(subject, "is" if made else "is not"),
          bool(gate.MADE_HERE_RE.match(subject)) is made, subject)
check("a rename in the work-tree column names its source too",
      gate.porcelain_paths("c:/repo", " R new.py\0old.py\0?? other.py\0")
      == {"c:/repo/new.py", "c:/repo/old.py", "c:/repo/other.py"},
      gate.porcelain_paths("c:/repo", " R new.py\0old.py\0?? other.py\0"))

# The report the gate asks for after a block writes nothing lasting, so it must not hand the
# unchanged candidate a new block budget (report bb01bd52).
with tempfile.TemporaryDirectory(prefix="cwg_report_key_") as repo:
    candidate_repo(repo, "reporting")
    sid = session()
    try:
        marker, _ = gate_paths(sid)
        mark_edit(sid, repo, "src/seed.py", "value = 5")
        opened_key = gate.candidate_key(cwg.read_json(marker))
        inbox = os.path.join(HERE, "gate_inbox.py").replace("\\", "/")
        mark_shell(sid, repo, 'git status --porcelain | wc -l; python "{}" report --session s --nonce n '
                              '--block "b" --facts "f (with parentheses)" --did "d"'.format(inbox))
        reported = cwg.read_json(marker) or {}
        check("a report filed after a block leaves the candidate's key as it was",
              gate.candidate_key(reported) == opened_key, reported.get("paths"))
        mark_shell(sid, repo, "python build.py")
        check("a command that may write still names its unresolved mutation",
              cwg.SHELL_MUTATION_PATH in ((cwg.read_json(marker) or {}).get("paths") or []),
              (cwg.read_json(marker) or {}).get("paths"))
    finally:
        cleanup(sid)

# A candidate whose lasting change no snapshot can see closes as `verified` against its last mark that
# could have written: bookkeeping after the approval leaves that clock alone, a possible write — a
# test run included — moves it, and the activity clock the idle limit reads moves on every mark
# (report cf223da5 and its review).
def invisible_change_stop(after_command):
    with tempfile.TemporaryDirectory(prefix="cwg_invisible_") as ground:
        flow_sid = session()
        try:
            mark_shell(flow_sid, ground, "python tune_config.py")
            marker, _ = gate_paths(flow_sid)
            opened = cwg.read_json(marker) or {}
            written = float(opened.get("last_write_ts") or 0.0)
            events = [skill_use(written - 5, "development-verification", "skill-dev")]
            simplify_wave(events, written + 0.1, "lens", SIMPLIFY_LENSES)
            add_review(events, written + 0.2, "review-1", review_text("APPROVED"))
            # The approval is filed within a second of the write; the command must come after it.
            time.sleep(max(0.0, written + 1.5 - time.time()))
            mark_shell(flow_sid, ground, after_command)
            later = cwg.read_json(marker) or {}
            kept = bool(written) and later.get("last_write_ts") == opened.get("last_write_ts")
            active = later.get("last_ts") != opened.get("last_ts")
            return kept, active, stop_with(flow_sid, events, VERIFIED_HIGH)
        finally:
            cleanup(flow_sid)


kept, active, result = invisible_change_stop('nlm-memory remember --type DECISION --summary "tuned the config"')
check("bookkeeping after the approval leaves the invisible candidate's write clock where it was",
      kept and active, result)
check("and its verified receipt is accepted", result.get("decision") != "block"
      and "recorded terminal state: verified" in result.get("systemMessage", ""), result)
for label, command in (("a command that could write", "python tune_config.py --again"),
                       ("a test run, which may write too", "npm test")):
    kept, active, result = invisible_change_stop(command)
    check(label + " moves the write clock and expires the approval",
          not kept and active and result.get("decision") == "block"
          and "lacks a current APPROVED" in result.get("reason", ""), result)

# A review lane's role proves no write for that clock: its command is judged like anyone's, and a
# record of it that brings only a throwaway path still moves the clock (G23 review, round 2).
LANE_SCRIPT = {"tool_name": "Bash", "agent_type": "adversarial-reviewer",
               "tool_input": {"command": "python tune_config.py > scratchpad/probe.txt"}}
check("a review lane's script does not prove it writes nothing lasting",
      marker_hook.read_only_lane(LANE_SCRIPT) and not marker_hook.only_own_state(LANE_SCRIPT))
lane_sid = session()
try:
    with tempfile.TemporaryDirectory(prefix="cwg_lane_write_") as ground:
        mark_shell(lane_sid, ground, "python tune_config.py")
        lane_marker, _ = gate_paths(lane_sid)
        before_lane = (cwg.read_json(lane_marker) or {}).get("last_write_ts")
        time.sleep(0.05)
        lane_data = dict(LANE_SCRIPT, session_id=lane_sid, cwd=ground)
        marker_hook.record_paths(lane_data, [cwg.normalize_path(os.path.join(tempfile.gettempdir(), "probe.txt"))],
                                 write_capable_command=False, quiet=marker_hook.only_own_state(lane_data))
        after_lane = (cwg.read_json(lane_marker) or {}).get("last_write_ts")
        check("such a record moves the write clock", cwg.valid_ts(before_lane) and cwg.valid_ts(after_lane)
              and after_lane > before_lane, (before_lane, after_lane))
finally:
    cleanup(lane_sid)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"], durable_ts=150)
stale_marker, _ = gate_paths(sid)
stale = cwg.read_json(stale_marker)
stale["content_marks"] = [
    {"ts": 110, "fp": "aaa"},
    {"ts": 150, "fp": "aaa", "unknown": True,
     "cause": {"reason": "unresolved-write-capable", "tool": "Bash", "command": "cd", "landed": "c:/tmp/wt-s5"}},
]
cwg.write_json(stale_marker, stale)
events = base_events(include_simplify=True)
add_review(events, 130, "review-1", review_text("APPROVED"))
result = stop_with(sid, events, VERIFIED_HIGH)
check("the block text carries what the hook read",
      result.get("decision") == "block" and "What the hook read:" in result.get("reason", "")
      and "ended in c:/tmp/wt-s5" in result.get("reason", ""), result)

# --- a reviewer continued with SendMessage: each round is read from its notification (report 7435cfb4)
FOREGROUND_TRAILER = ("\nagentId: {id} (use SendMessage with to: '{id}', summary: '<5-10 word recap>' "
                      "to continue this agent)\n<usage>subagent_tokens: 1</usage>")


def foreground_review(events, stamp, call_id, agent_id, result, subtype="adversarial-reviewer"):
    add_review(events, stamp, call_id, result + FOREGROUND_TRAILER.format(id=agent_id), subtype=subtype)


def send_message(events, stamp, call_id, agent_id, resumed=True):
    events.append(entry(stamp, "assistant", [{
        "type": "tool_use", "id": call_id, "name": "SendMessage",
        "input": {"to": agent_id, "message": "Round two: the remediation delta is attached."},
    }]))
    body = ({"success": True, "message": "Resuming agent " + agent_id, "resumedAgentId": agent_id}
            if resumed else {"success": True, "message": "Message queued for delivery"})
    events.append(tool_result(stamp + 0.2, call_id, json.dumps(body)))


def round_notification(stamp, agent_id, result, call_id, status="completed"):
    text = agent_notification_text(agent_id, result, status)
    named = "" if call_id is None else "<tool-use-id>{}</tool-use-id>\n".format(call_id)
    return notification_records(stamp, text.replace("<tool-use-id>toolu_a</tool-use-id>\n", named))


def round_stop(events, stamp, agent_id):
    events.append(entry(stamp, "assistant", [{"type": "tool_use", "id": "stop-" + agent_id,
                                               "name": "TaskStop", "input": {"task_id": agent_id}}]))


def rounds_case(steps, durable_ts=None):
    sid = session()
    seed(sid, ["C:/repo/src/auth/session.ts"], durable_ts=durable_ts)
    events = base_events(include_simplify=True)
    for step in steps:
        kind = step[0]
        if kind == "fg":
            foreground_review(events, step[1], "review-" + str(step[1]), step[2], review_text(step[3]))
        elif kind == "bg":
            add_background_review(events, step[1], "bg-" + str(step[1]), step[2])
        elif kind == "bg-notice":
            events.extend(agent_notification(step[1], step[2], review_text(step[3])))
        elif kind == "send":
            send_message(events, step[1], "send-" + str(step[1]), step[2], resumed=step[3])
        elif kind == "round":
            events.extend(round_notification(step[1], step[2], review_text(step[3]), step[4]))
        elif kind == "stop":
            round_stop(events, step[1], step[2])
    return stop_with(sid, events, VERIFIED_HIGH)


for label, steps, durable_ts, expect_ok, expect_reason in (
    ("a REVISE then a resumed APPROVED verifies the candidate",
     [("fg", 130, "agent-sm1", "REVISE"), ("send", 132, "agent-sm1", True),
      ("round", 140, "agent-sm1", "APPROVED", "send-132")], None, True, ""),
    ("two resumed rounds after a REVISE, the last APPROVED, verify the candidate",
     [("fg", 130, "agent-sm2", "REVISE"), ("send", 132, "agent-sm2", True),
      ("round", 136, "agent-sm2", "REVISE", "send-132"), ("send", 138, "agent-sm2", True),
      ("round", 142, "agent-sm2", "APPROVED", "send-138")], None, True, ""),
    ("a resumed round after an APPROVED is review continued after it",
     [("fg", 130, "agent-sm3", "APPROVED"), ("send", 132, "agent-sm3", True),
      ("round", 136, "agent-sm3", "REVISE", "send-132")], None, False, "continued after terminal APPROVED"),
    ("a message to an agent that is no reviewer is no round",
     [("fg", 130, "agent-sm4", "REVISE"), ("send", 132, "agent-other", True),
      ("round", 140, "agent-other", "APPROVED", "send-132")], None, False,
     "an invoked review has no terminal APPROVED verdict"),
    ("a background REVISE and a resumed APPROVED verify the candidate",
     [("bg", 130, "agent-sm5"), ("bg-notice", 134, "agent-sm5", "REVISE"), ("send", 136, "agent-sm5", True),
      ("round", 140, "agent-sm5", "APPROVED", "send-136")], None, True, ""),
    ("a message that resumed nothing starts no round",
     [("fg", 130, "agent-sm6", "REVISE"), ("send", 132, "agent-sm6", False),
      ("round", 140, "agent-sm6", "APPROVED", "send-132")], None, False,
     "an invoked review has no terminal APPROVED verdict"),
    ("a round's notification that names no call still belongs to the open round",
     [("fg", 130, "agent-sm7", "REVISE"), ("send", 132, "agent-sm7", True),
      ("round", 140, "agent-sm7", "APPROVED", None)], None, True, ""),
    ("a resumed verdict is filed at the SendMessage: an edit after it expires the verdict",
     [("fg", 130, "agent-sm8", "REVISE"), ("send", 132, "agent-sm8", True),
      ("round", 140, "agent-sm8", "APPROVED", "send-132")], 135, False, "lacks a current APPROVED"),
    ("an edit before the SendMessage does not expire the resumed verdict",
     [("fg", 130, "agent-sm9", "REVISE"), ("send", 132, "agent-sm9", True),
      ("round", 140, "agent-sm9", "APPROVED", "send-132")], 131, True, ""),
    ("a stop after the resumed verdict is activity after it",
     [("fg", 130, "agent-sm10", "REVISE"), ("send", 132, "agent-sm10", True),
      ("round", 140, "agent-sm10", "APPROVED", "send-132"), ("stop", 145, "agent-sm10")], None, False,
     "continued after terminal APPROVED"),
    ("a reviewer from before the candidate resumed inside it is that round",
     [("fg", 90, "agent-sm11", "REVISE"), ("send", 132, "agent-sm11", True),
      ("round", 140, "agent-sm11", "APPROVED", "send-132")], None, True, ""),
):
    result = rounds_case(steps, durable_ts)
    ok = result.get("continue") is True and "decision" not in result
    check("SendMessage: " + label, ok if expect_ok else (result.get("decision") == "block"
                                                        and expect_reason in result.get("reason", "")), result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
foreground_review(events, time.time() - 100, "review-live", "agent-live", review_text("REVISE"))
send_message(events, time.time() - 50, "send-live", "agent-live")
result = stop_with(sid, events, "Waiting for the second round.")
check("a resumed round still running lets the turn end",
      result.get("continue") is True and "decision" not in result, result)


# An agent that is no reviewer, resumed with SendMessage, is this session's own work in flight until
# its notification (report ac2ee4da).
def resumed_worker_stop(finished):
    worker_sid = session()
    seed(worker_sid, ["C:/repo/src/auth/session.ts"])
    worker_events = base_events(include_simplify=True)
    worker_events.append(agent_use(time.time() - 200, "general-purpose", "worker-launch", run_in_background=True))
    worker_events.append(tool_result(time.time() - 199.5, "worker-launch",
                                     "Async agent launched successfully. (This tool result is internal metadata.)\n"
                                     "agentId: agent-worker (internal ID - do not mention to user.)"))
    worker_events.extend(agent_notification(time.time() - 150, "agent-worker", "first part done"))
    send_message(worker_events, time.time() - 50, "send-worker", "agent-worker")
    if finished:
        worker_events.extend(agent_notification(time.time() - 10, "agent-worker", "all done"))
    return stop_with(worker_sid, worker_events, "Waiting for the worker.")


result = resumed_worker_stop(finished=False)
check("an agent that is no reviewer, resumed and still running, lets the turn end",
      result.get("continue") is True and "decision" not in result, result)
result = resumed_worker_stop(finished=True)
check("once the resumed agent has reported back, a missing receipt blocks again",
      result.get("decision") == "block" and "receipt is missing" in result.get("reason", ""), result)


# A Workflow runs in the background until its notification, so its launch is this session's own work
# in flight and a stop meanwhile waits (report 874ab23b); only a Workflow call's own result says so.
WORKFLOW_ACK = ("Workflow launched in background. Task ID: wz4woah7q\nSummary: Final editorial pass\n"
                "Transcript dir: C:\\Users\\in\\.claude\\projects\\p\\s\\workflows")


def workflow_stop(finished, tool="Workflow", name=None):
    flow_sid = session()
    seed(flow_sid, ["C:/repo/src/auth/session.ts"])
    flow_events = base_events(include_simplify=True)
    payload = {"script": "export const meta = {name: 'edit'}"} if tool == "Workflow" else {"command": "type ack.txt"}
    if name:
        payload["name"] = name
    flow_events.append(entry(time.time() - 120, "assistant",
                             [{"type": "tool_use", "id": "flow-launch", "name": tool, "input": payload}]))
    flow_events.append(tool_result(time.time() - 119.5, "flow-launch", WORKFLOW_ACK))
    if finished:
        flow_events.append(notification(time.time() - 10, "wz4woah7q", "C:/tasks/wz4woah7q.output"))
    return stop_with(flow_sid, flow_events, "Waiting for the workflow.")


result = workflow_stop(finished=False)
check("a workflow still running lets the turn end, named by its summary",
      result.get("continue") is True and "decision" not in result
      and "wz4woah7q (workflow: Final editorial pass)" in result.get("systemMessage", ""), result)
result = workflow_stop(finished=False, name="review-changes")
check("a saved workflow is named by its name",
      "wz4woah7q (workflow: review-changes)" in result.get("systemMessage", ""), result)
result = workflow_stop(finished=True)
check("once the workflow has reported back, a missing receipt blocks again",
      result.get("decision") == "block" and "receipt is missing" in result.get("reason", ""), result)
result = workflow_stop(finished=False, tool="Bash")
check("the workflow's words in another tool's result declare no work in flight",
      result.get("decision") == "block" and "receipt is missing" in result.get("reason", ""), result)

sid = session()
seed(sid, ["C:/repo/src/auth/session.ts"])
events = base_events(include_simplify=True)
add_background_review(events, 130, "bg-review", "agent-ledger")
events.extend(agent_notification(140, "agent-ledger", review_text("APPROVED")))
result = stop_with(sid, events, VERIFIED_HIGH)
ledger_notes = [note for note in review_notes(sid) if note.get("task") == "agent-ledger"]
check("the ledger files a background verdict at its launch and keeps when it was notified",
      result.get("continue") is True and ledger_notes and ledger_notes[-1].get("at") == 130
      and ledger_notes[-1].get("notified") == 140, ledger_notes)

# --- a launch whose capture is missing binds by the file when nothing wrote it after the launch (R4)
for label, written_before, expect_bound in (
    ("a packet file last written before the launch binds without a capture", True, True),
    ("a packet file written after the launch binds nothing without a capture", False, False),
):
    sid = session()
    try:
        now = time.time()
        task_id = "bfile" + uuid.uuid4().hex[:4]
        out_file = os.path.join(tasks_dir, task_id + ".output")
        fed = reviewer_role_text() + "\n\n" + PACKET_A
        packet_file = write_packet(task_id, fed)
        written_at = now - 710 if written_before else now - 650
        os.utime(packet_file, (written_at, written_at))
        command = packet_launch(packet_file)
        seed(sid, ["C:/repo/src/auth/session.ts"], first_ts=now - 900, last_ts=now - 800,
             durable_ts=now - 800)
        events = [skill_use(now - 890, "development-verification", "skill-dev")]
        simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
        events.append(bash_use(now - 700, "codex-" + task_id, command, run_in_background=True))
        events.append(tool_result(now - 699, "codex-" + task_id,
                                  DETACHED_ACK.format(id=task_id, out=out_file)))
        events.append(notification(now - 600, task_id, out_file, "completed"))
        rollout_records([(now - 690, "developer", fed),
                         (now - 640, "assistant", codex_cli_output(review_text("APPROVED")))])
        transcript = write_transcript(events)
        result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                                 "last_assistant_message": BACKGROUND_HIGH})
        bound = result.get("continue") is True and "decision" not in result
        check(label, bound is expect_bound, result)
        if not expect_bound:
            check("the ledger says why the launch bound nothing",
                  any("the file changed after the launch" in (note.get("reason") or "")
                      for note in review_notes(sid)), review_notes(sid))
    finally:
        cleanup(sid, locals().get("transcript"))

for cause in (
    {"reason": "unresolved-write-capable", "tool": "Bash", "command": "make", "skipped": 2},
    {"reason": "unresolved-write-capable", "tool": "Bash", "skipped": 2},
):
    barrier = gate.describe_mark({"ts": time.time(), "fp": "a", "unknown": True, "cause": cause},
                                 lambda stamp: "12:00:00")
    check("a barrier says how many repositories the time budget left out",
          barrier.endswith(", with 2 repositories left unmeasured by the hook's time budget")
          and barrier.count("(") <= 1, barrier)

# --- an intent-to-add placeholder is no staged content (report 265312d0)
check("the empty-blob ids are the empty blob in both object formats",
      marker_hook.EMPTY_BLOBS == {hashlib.sha1(b"blob 0\0").hexdigest(),
                                  hashlib.sha256(b"blob 0\0").hexdigest()},
      marker_hook.EMPTY_BLOBS)
with tempfile.TemporaryDirectory(prefix="cwg_intent_to_add_") as repo:
    candidate_repo(repo, "intent")
    fresh = os.path.join(repo, "src", "fresh.py")
    with open(fresh, "w", encoding="utf-8") as stream:
        stream.write("fresh = True" + chr(10))
    subprocess.run(["git", "-C", repo, "add", "-N", "--", "src/fresh.py"], check=True)
    fresh_path = cwg.normalize_path(fresh)
    placeholder = marker_hook.content_fingerprint([fresh_path])
    commit_paths(repo, "src/fresh.py", "fresh")
    check("committing a reviewed file over its intent-to-add entry keeps the fingerprint",
          placeholder is not None and marker_hook.content_fingerprint([fresh_path]) == placeholder,
          (placeholder, marker_hook.content_fingerprint([fresh_path])))
    staged = os.path.join(repo, "src", "staged.py")
    with open(staged, "w", encoding="utf-8") as stream:
        stream.write("staged = 1" + chr(10))
    subprocess.run(["git", "-C", repo, "add", "--", "src/staged.py"], check=True)
    with open(staged, "w", encoding="utf-8") as stream:
        stream.write("staged = 2" + chr(10))
    check("a staged new file that differs from the disk still diverges",
          marker_hook.staged_divergences(os.path.join(repo, "src"), ["staged.py"]) not in (None, []),
          marker_hook.staged_divergences(os.path.join(repo, "src"), ["staged.py"]))

# --- a variable the same command gave a literal value names a directory too (report 265312d0)
with tempfile.TemporaryDirectory(prefix="cwg_directory_variables_") as base:
    left, right = os.path.join(base, "left"), os.path.join(base, "right")
    os.makedirs(left)
    os.makedirs(right)
    right_forward = right.replace(chr(92), "/")

    def variable_plan(command):
        start, targets = marker_hook.directory_plan(command, left, "Bash")
        named = [None if path is None else os.path.relpath(path, base) for path in [start] + targets]
        return named[0], named[1:]

    for label, command, expected in (
        ("a quoted variable assigned before the change",
         'CT="{}"; git -C "$CT" status | head -3; cd "$CT" && python finish.py'.format(right_forward),
         ("left", ["right"])),
        ("an unquoted variable in braces",
         "CT={}; cd ${{CT}} && make".format(right_forward), ("right", ["right"])),
        ("a single-quoted value keeps its backslashes",
         "CT='{}'; cd \"$CT\" && make".format(right), ("right", ["right"])),
        ("read may have reassigned it",
         'CT="{}"; read CT; cd "$CT" && make'.format(right_forward), ("left", [])),
        ("export may have reassigned it",
         'CT="{}"; export CT=/elsewhere; cd "$CT"'.format(right_forward), ("left", [])),
        ("a computed value is unknown", 'CT=$(pwd); cd "$CT" && make', ("left", [])),
        ("a single-quoted reference is literal text",
         "CT={}; cd '$CT' && make".format(right_forward), (None, [])),
        ("an unassigned variable stays unknown", 'cd "$NOWHERE" && make', (None, [])),
        ("an assignment inside a pipeline does not persist",
         'CT="{}" | cat; cd "$CT" && make'.format(right_forward), ("left", [])),
    ):
        check("directory_plan with variables: " + label, variable_plan(command) == expected,
              (command, variable_plan(command)))

# --- a throwaway written into a drive-root temp subdirectory outside any repository (report ff2c5007)
if os.path.isdir("C:\\tmp"):
    with tempfile.TemporaryDirectory(prefix="cwg_scratch_rule_", dir="C:\\tmp") as scratch_dir, \
            tempfile.TemporaryDirectory(prefix="cwg_clone_rule_", dir="C:\\tmp") as clone_dir:
        candidate_repo(clone_dir, "clone")
        helper = os.path.join(scratch_dir, "wait-job.sh")
        check("a file in a temp subdirectory outside any repository is scratch",
              marker_hook.scratch_file(helper), helper)
        check("a file in a clone under a temp root is not scratch",
              not marker_hook.scratch_file(os.path.join(clone_dir, "src", "seed.py")), clone_dir)
        check("the user temp directory is not this rule's business",
              not marker_hook.scratch_file(os.path.join(tempfile.gettempdir(), "x", "y.py")), "user temp")
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            mark_edit(sid, clone_dir, "src/candidate.py", "value = 5")
            anchored = cwg.read_json(marker)
            with open(helper, "w", encoding="utf-8") as stream:
                stream.write("sleep 1" + chr(10))
            run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Write",
                            "cwd": clone_dir, "tool_input": {"file_path": helper}})
            after_helper = cwg.read_json(marker)
            check("a scratch helper written after an approval leaves the candidate as it was",
                  after_helper["last_durable_ts"] == anchored["last_durable_ts"]
                  and cwg.normalize_path(helper) not in (after_helper.get("paths") or []), after_helper)
        finally:
            cleanup(sid)


# --- a clean upstream merge is git's computation, not the session's writing (report f9920b99)
def git_as_gate(directory, *args, check=True):
    return subprocess.run(
        ["git", "-C", directory, "-c", "user.name=Code Work Gate", "-c", "user.email=gate@example.invalid"]
        + list(args), check=check, capture_output=True, text=True,
    )


def put(directory, relative, content):
    target = os.path.join(directory, *relative.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as stream:
        stream.write(content)


check("raw diff records keep the destination side of each path",
      marker_hook.raw_diff_records(":100644 100644 aaa bbb M\0src/a.py\0:100644 000000 ccc 000 D\0gone.py\0")
      == {"src/a.py": ("100644", "bbb"), "gone.py": ("000000", "000")})

# These scenarios pin the judge's logic, not this machine's load: under load its own deadline runs
# out first, which records the merge's files, the documented fallback.
os.environ["CWG_MERGE_JUDGE_BUDGET"] = "60"
with tempfile.TemporaryDirectory(prefix="cwg_merge_judge_") as base:
    remote = os.path.join(base, "remote.git")
    upstream = os.path.join(base, "upstream")
    work = os.path.join(base, "work")
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", remote], check=True)
    subprocess.run(["git", "clone", "--quiet", remote, upstream], check=True, capture_output=True)
    shared_base = "".join("line {}\n".format(number) for number in range(1, 21))
    for relative, content in (("src/app.py", "value = 1\n"), ("src/gone.py", "gone = 1\n"),
                              ("src/shared.py", shared_base), ("src/stay.py", "stay = 1\n")):
        put(upstream, relative, content)
    git_as_gate(upstream, "add", "-A")
    git_as_gate(upstream, "commit", "--quiet", "-m", "base")
    git_as_gate(upstream, "push", "--quiet", "origin", "main")
    # Cloned after the first push, so origin/HEAD names the default branch as a real clone's does.
    subprocess.run(["git", "clone", "--quiet", remote, work], check=True, capture_output=True)
    put(upstream, "src/app.py", "value = 2\n")
    put(upstream, "src/new.py", "new = 1\n")
    os.remove(os.path.join(upstream, "src", "gone.py"))
    put(upstream, "src/shared.py", "upstream line 1\n" + shared_base.split("\n", 1)[1])
    git_as_gate(upstream, "add", "-A")
    git_as_gate(upstream, "commit", "--quiet", "-m", "upstream")
    git_as_gate(upstream, "push", "--quiet", "origin", "main")
    git_as_gate(work, "fetch", "--quiet")
    start = git_as_gate(work, "rev-parse", "HEAD").stdout.strip()

    def restart():
        git_as_gate(work, "reset", "--quiet", "--hard", start)
        git_as_gate(work, "clean", "-fdq")

    def merge_upstream():
        git_as_gate(work, "merge", "--no-ff", "--no-commit", "origin/main", check=False)

    def recorded(sid):
        entry = cwg.read_json(cwg.marker_path(cwg.session_key(sid))) or {}
        return entry, set(entry.get("paths") or [])

    def at(relative):
        return cwg.normalize_path(os.path.join(work, *relative.split("/")))

    def covers_now(entry, stamp):
        return gate.content_covers(entry, stamp, float(entry.get("last_durable_ts") or 0.0))

    brought = {at("src/app.py"), at("src/new.py"), at("src/gone.py"), at("src/shared.py")}
    check("no merge is judged without the HEAD the snapshot recorded",
          marker_hook.merge_set_aside({"git": {"root": work}, "head": None, "merging": True}, None, brought,
                                      set(), time.monotonic() + 5) == set())

    sid = session()
    try:
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main", action=merge_upstream)
        marker_entry, paths = recorded(sid)
        check("a clean upstream merge records none of the files it brought in",
              bool(paths) and not (paths & brought), marker_entry)
        check("and leaves an operational candidate", not cwg.candidate_shape(marker_entry)["persistent"], marker_entry)
        mark_shell(sid, work, "git commit -m merge",
                   action=lambda: git_as_gate(work, "commit", "--quiet", "-m", "merge"))
        marker_entry, paths = recorded(sid)
        check("committing the merge records none of them either", not (paths & brought), marker_entry)
    finally:
        cleanup(sid)
        restart()

    sid = session()
    try:
        def merge_and_write():
            merge_upstream()
            with open(os.path.join(work, "src", "app.py"), "a", encoding="utf-8") as stream:
                stream.write("sneaked = True\n")
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main && echo sneaked >> src/app.py",
                   action=merge_and_write)
        marker_entry, paths = recorded(sid)
        check("a file written after the merge in the same command is recorded as rewritten",
              at("src/app.py") in paths and at("src/app.py") in (marker_entry.get("content_paths") or []), marker_entry)
        check("while the rest of the merge is not", not (paths & (brought - {at("src/app.py")})), marker_entry)
    finally:
        cleanup(sid)
        restart()

    sid = session()
    try:
        git_as_gate(work, "checkout", "--quiet", "-b", "side", "origin/main")
        put(work, "src/side.py", "side = 1\n")
        git_as_gate(work, "add", "src/side.py")
        git_as_gate(work, "commit", "--quiet", "-m", "side")
        git_as_gate(work, "checkout", "--quiet", "main")
        mark_shell(sid, work, "git merge --no-ff --no-commit side",
                   action=lambda: git_as_gate(work, "merge", "--no-ff", "--no-commit", "side", check=False))
        marker_entry, paths = recorded(sid)
        check("a merge of a branch that is not upstream records every file it brought in",
              {at("src/side.py"), at("src/new.py")} <= paths, marker_entry)
    finally:
        cleanup(sid)
        restart()
        git_as_gate(work, "branch", "--quiet", "-D", "side")

    sid = session()
    try:
        own = mark_edit(sid, work, "src/shared.py", shared_base + "own line 21")
        mark_shell(sid, work, "git commit -am own",
                   action=lambda: git_as_gate(work, "commit", "--quiet", "-am", "own"))
        mark_edit(sid, work, "src/extra.py", "extra = 1")
        verdict_ts = time.time()
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main", action=merge_upstream)
        marker_entry, paths = recorded(sid)
        check("a clean merge into the candidate's own file leaves a mark naming what it merged",
              bool((marker_entry.get("content_marks") or [{}])[-1].get("merge"))
              and not (paths & (brought - {own})), marker_entry)
        mark_shell(sid, work, "git add src/extra.py",
                   action=lambda: git_as_gate(work, "add", "src/extra.py"))
        marker_entry, _ = recorded(sid)
        check("a verdict from before the merge still covers the candidate after the next measurement",
              float(marker_entry.get("last_durable_ts") or 0.0) > verdict_ts and covers_now(marker_entry, verdict_ts), marker_entry)
        mark_edit(sid, work, "src/shared.py", "rewritten = True")
        marker_entry, _ = recorded(sid)
        check("an edit after the merge still retires it", not covers_now(marker_entry, verdict_ts), marker_entry)
    finally:
        cleanup(sid)
        restart()

    sid = session()
    try:
        mark_edit(sid, work, "src/shared.py", shared_base + "own line 21")
        mark_shell(sid, work, "git commit -am own",
                   action=lambda: git_as_gate(work, "commit", "--quiet", "-am", "own"))
        mark_edit(sid, work, "src/extra.py", "extra = 1")
        verdict_ts = time.time()

        def merge_and_stage():
            merge_upstream()
            git_as_gate(work, "add", "src/extra.py")
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main && git add src/extra.py",
                   action=merge_and_stage)
        marker_entry, _ = recorded(sid)
        check("a merge beside a staging of reviewed bytes still carries the verdict",
              float(marker_entry.get("last_durable_ts") or 0.0) > verdict_ts
              and bool((marker_entry.get("content_marks") or [{}])[-1].get("merge"))
              and covers_now(marker_entry, verdict_ts), marker_entry)
    finally:
        cleanup(sid)
        restart()

    sid = session()
    try:
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main", action=merge_upstream)
        mark_shell(sid, work, "git merge --abort", action=lambda: git_as_gate(work, "merge", "--abort"))
        marker_entry, paths = recorded(sid)
        check("abandoning a clean upstream merge records none of its files", not (paths & brought), marker_entry)
    finally:
        cleanup(sid)
        restart()

    sid = session()
    try:
        # Dirt from before the candidate, which the command that abandons the merge also discards.
        put(work, "src/stay.py", "stay = 2\n")
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main", action=merge_upstream)

        def abandon_and_discard():
            git_as_gate(work, "merge", "--abort")
            git_as_gate(work, "checkout", "--", "src/stay.py")
        mark_shell(sid, work, "git merge --abort && git checkout -- src/stay.py", action=abandon_and_discard)
        marker_entry, paths = recorded(sid)
        check("an abandoned merge sets aside only what it had staged, not dirt the same command discards",
              at("src/stay.py") in paths and not (paths & brought), marker_entry)
    finally:
        cleanup(sid)
        restart()

    sid = session()
    try:
        put(work, "src/shared.py", "own line 1\n" + shared_base.split("\n", 1)[1])
        git_as_gate(work, "commit", "--quiet", "-am", "own first line")
        mark_shell(sid, work, "git merge --no-ff --no-commit origin/main", action=merge_upstream)
        marker_entry, paths = recorded(sid)
        check("a conflicted path stays recorded while the clean ones do not",
              at("src/shared.py") in paths
              and not (paths & {at("src/app.py"), at("src/new.py"), at("src/gone.py")}), marker_entry)
    finally:
        cleanup(sid)
        restart()

    # --- a rebase onto upstream, or a merge committed by the same command, rewrites the candidate's
    # committed files on a clean tree, which no snapshot lists (report c4c78b99)
    later = time.monotonic() + 30
    check("no baseline is taken for a closed candidate or a command that names no integration",
          marker_hook.integration_baseline("git rebase origin/main",
                                           {"closed": True, "content_paths": [at("src/app.py")]}, later) is None
          and marker_hook.integration_baseline("git status", {"content_paths": [at("src/app.py")]}, later) is None)
    git_calls, real_git_run = [], cwg.git_run
    cwg.git_run = lambda *args, **kwargs: git_calls.append(args) or real_git_run(*args, **kwargs)
    try:
        baseline = marker_hook.integration_baseline("git pull --rebase", {"content_paths": [at("src/app.py")]}, later)
    finally:
        cwg.git_run = real_git_run
    check("the baseline runs no git and, for a clean file, is the full fingerprint",
          bool(baseline) and not git_calls and baseline["fp"] == marker_hook.content_fingerprint([at("src/app.py")]),
          (baseline, git_calls))

    def integration_case(label, command, action, carried, prepare=None, content=shared_base + "own line 21",
                         also=None):
        sid = session()
        try:
            own = mark_edit(sid, work, "src/shared.py", content)
            if also:
                also(sid)
            mark_shell(sid, work, "git commit -am own",
                       action=lambda: git_as_gate(work, "commit", "--quiet", "-am", "own"))
            verdict_ts = time.time()
            if prepare:
                prepare()
            mark_shell(sid, work, command, action=action)
            entry, paths = recorded(sid)
            caught = gate.unmeasured_change(entry, time.time())
            stopped = caught[0] if caught else entry
            check("{}: the verdict from before it {} the Stop hook's catch-up".format(
                      label, "survives" if carried else "does not survive"),
                  covers_now(stopped, verdict_ts) == carried,
                  (entry.get("content_marks"), stopped.get("content_marks")))
            if carried:
                check(label + ": its mark names the content it replaced, and no upstream file is recorded",
                      bool(entry["content_marks"][-1].get("merge")) and not (paths & (brought - {own})), entry)
        finally:
            cleanup(sid)
            git_as_gate(work, "sparse-checkout", "disable", check=False)
            git_as_gate(work, "rebase", "--abort", check=False)
            git_as_gate(work, "switch", "--quiet", "main", check=False)
            git_as_gate(work, "branch", "--quiet", "-D", "side", check=False)
            restart()

    def rebase_upstream():
        git_as_gate(work, "rebase", "--quiet", "origin/main")

    integration_case("a clean rebase onto upstream", "git rebase origin/main", rebase_upstream, True)
    integration_case("a clean rebase of a candidate that also deleted a file", "git rebase origin/main",
                     rebase_upstream, True,
                     also=lambda sid: mark_shell(sid, work, "git rm src/stay.py",
                                                 action=lambda: git_as_gate(work, "rm", "--quiet", "src/stay.py")))
    with open(os.path.join(work, ".git", "info", "exclude"), "a", encoding="utf-8") as stream:
        stream.write("src/local_settings.py\n")

    def rebase_and_delete_ignored():
        rebase_upstream()
        os.remove(os.path.join(work, "src", "local_settings.py"))

    integration_case("a lasting ignored file deleted beside the rebase", "git rebase origin/main && rm src/local_settings.py",
                     rebase_and_delete_ignored, False,
                     also=lambda sid: mark_edit(sid, work, "src/local_settings.py", "local = True"))

    def sparse_stay(sid):
        # Tracked and in the merged tree, but kept off the disk as a skip-worktree entry.
        mark_edit(sid, work, "src/stay.py", "stay = 2")
        mark_shell(sid, work, "git commit -m stay src/stay.py",
                   action=lambda: git_as_gate(work, "commit", "--quiet", "-m", "stay", "--", "src/stay.py"))
        git_as_gate(work, "sparse-checkout", "set", "--no-cone", "/*", "!/src/stay.py")

    integration_case("a rebase over a lasting file a sparse checkout keeps off the disk", "git rebase origin/main",
                     rebase_upstream, False, also=sparse_stay)
    integration_case("git pull --rebase", "git pull --rebase",
                     lambda: git_as_gate(work, "pull", "--quiet", "--rebase"), True)
    integration_case("a detached rebase", "git rebase origin/main", rebase_upstream, True,
                     prepare=lambda: git_as_gate(work, "switch", "--quiet", "--detach"))
    integration_case("a merge committed in the same command", "git merge --no-edit origin/main",
                     lambda: git_as_gate(work, "merge", "--quiet", "--no-edit", "origin/main"), True)

    def append_and_commit(line, message):
        with open(os.path.join(work, "src", "shared.py"), "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        git_as_gate(work, "commit", "--quiet", "-am", message)

    def rebase_then_commit():
        rebase_upstream()
        append_and_commit("sneaked = True", "sneak")

    integration_case("a commit after the rebase in the same command",
                     "git rebase origin/main && git commit -am sneak", rebase_then_commit, False)
    # What a cancelled marker hook leaves behind: an edit committed with no mark before the rebase.
    integration_case("a rebase of an edit no hook measured", "git rebase origin/main", rebase_upstream, False,
                     prepare=lambda: append_and_commit("unmeasured = True", "unmeasured"))

    def disguised_commit():
        # Size and modification time kept and staged by content: only the content fingerprint can tell.
        target = os.path.join(work, "src", "shared.py")
        kept = os.stat(target)
        with open(target, "rb") as stream:
            content = stream.read()
        with open(target, "wb") as stream:
            stream.write(content.replace(b"own line 21", b"own line 22"))
        os.utime(target, ns=(kept.st_atime_ns, kept.st_mtime_ns))
        blob = git_as_gate(work, "hash-object", "-w", "src/shared.py").stdout.strip()
        git_as_gate(work, "update-index", "--cacheinfo", "100644,{},src/shared.py".format(blob))
        git_as_gate(work, "commit", "--quiet", "-m", "disguised")

    integration_case("a rebase of an unmeasured edit that kept the file's size and time",
                     "git rebase origin/main", rebase_upstream, False, prepare=disguised_commit)

    def local_app_commit():
        put(work, "src/app.py", "value = 3\n")
        git_as_gate(work, "commit", "--quiet", "-am", "local app")

    def merge_resolving_by_hand():
        git_as_gate(work, "merge", "--no-edit", "origin/main", check=False)
        put(work, "src/app.py", "value = 4\n")
        git_as_gate(work, "add", "src/app.py")
        git_as_gate(work, "-c", "core.editor=true", "commit", "--quiet", "--no-edit")

    integration_case("a merge whose conflict in another file was resolved by hand in the same command",
                     "git merge origin/main; git add src/app.py && git commit --no-edit",
                     merge_resolving_by_hand, False, prepare=local_app_commit)

    def side_branch():
        git_as_gate(work, "branch", "--quiet", "--force", "side", "origin/main")
        git_as_gate(work, "switch", "--quiet", "side")
        put(work, "src/side.py", "side = 1\n")
        git_as_gate(work, "add", "src/side.py")
        git_as_gate(work, "commit", "--quiet", "-m", "side")
        git_as_gate(work, "switch", "--quiet", "main")

    integration_case("a rebase onto a branch that is not upstream", "git rebase side",
                     lambda: git_as_gate(work, "rebase", "--quiet", "side"), False, prepare=side_branch)

    def resolve_by_hand():
        git_as_gate(work, "rebase", "origin/main", check=False)
        put(work, "src/shared.py", "resolved line 1\n" + shared_base.split("\n", 1)[1])
        git_as_gate(work, "add", "src/shared.py")
        git_as_gate(work, "-c", "core.editor=true", "rebase", "--continue")

    integration_case("a conflict resolved by hand in the same command",
                     "git rebase origin/main || git rebase --continue", resolve_by_hand, False,
                     content="own line 1\n" + shared_base.split("\n", 1)[1])

del os.environ["CWG_MERGE_JUDGE_BUDGET"]

# --- a read-only lane's own commands do not expire the verdict it is producing (report a1c7b71b)
check("cmp only compares", marker_hook.read_only_pipeline("cmp backend/schema.sql mirror/schema.sql && echo same"))
check("the read-only lanes are the reviewer, its XHIGH profile, Explore and Plan",
      marker_hook.read_only_lane({"agent_type": "adversarial-reviewer"})
      and marker_hook.read_only_lane({"agent_type": cwg.XHIGH_REVIEWER})
      and marker_hook.read_only_lane({"agent_type": "Explore"})
      and marker_hook.read_only_lane({"agent_type": "Plan"})
      and not marker_hook.read_only_lane({"agent_type": "general-purpose"})
      and not marker_hook.read_only_lane({}))


with tempfile.TemporaryDirectory(prefix="cwg_lane_") as base:
    home, other = os.path.join(base, "home"), os.path.join(base, "other")
    candidate_repo(home, "lane-home")
    candidate_repo(other, "lane-other")
    # The target is computed, so the plan cannot follow it: the command lands in a repository no
    # snapshot covered, and is unresolved.
    unresolved = 'cd "$(git rev-parse --show-toplevel)/../other" && ls src | sed "s/_.*//" | sort | uniq -d'
    reviewer = {"agent_id": "agent-review", "agent_type": "adversarial-reviewer"}
    for label, extra, expires in (("the parent's", {}, True), ("a reviewer's", reviewer, False)):
        sid = session()
        try:
            mark_edit(sid, home, "src/candidate.py", "value = 3")
            anchored = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))
            time.sleep(0.05)
            shell_between(sid, home, other, unresolved, **extra)
            after = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))
            moved = after["last_durable_ts"] != anchored["last_durable_ts"]
            check("{} unresolved command {} the verdict".format(label, "expires" if expires else "keeps"),
                  moved == expires and (expires or after.get("paths") == anchored.get("paths")), after)
        finally:
            cleanup(sid)
    sid = session()
    try:
        mark_edit(sid, home, "src/candidate.py", "value = 4")
        anchored = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))

        def lane_writes():
            with open(os.path.join(home, "src", "candidate.py"), "w", encoding="utf-8") as stream:
                stream.write("value = 5" + chr(10))
        shell_between(sid, home, home, "sed -i s/4/5/ src/candidate.py", action=lane_writes, **reviewer)
        after = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))
        check("a write a reviewer makes where the snapshot looks still expires the verdict",
              after["last_durable_ts"] != anchored["last_durable_ts"]
              and (after.get("content_marks") or [{}])[-1].get("fp")
              != (anchored.get("content_marks") or [{}])[-1].get("fp"), after)
    finally:
        cleanup(sid)

# --- bookkeeping tools and GitLab calls do not write lasting artifacts (report e5533b32)
for command, expected in (
    ('nlm-memory remember --type GOTCHA --summary "a (b) [c]" --evidence "x"', True),
    ('~/.local/bin/nlm-memory.cmd recall "q"', True),
    ('NLM=~/.local/bin/nlm-memory.cmd; $NLM remember --summary "x" 2>&1 | tail -1', True),
    ('NLM=~/.local/bin/nlm-memory.cmd; "$NLM" recall "q"', True),
    ("NLM=~/.local/bin/nlm-memory.cmd; '$NLM' recall q", False),
    ('NLM=~/.local/bin/nlm-memory.cmd; printf -v NLM rm; $NLM -rf x', False),
    ('NLM=/usr/bin/rm; $NLM -rf x', False),
    ('NLM=~/.local/bin/nlm-memory.cmd | $NLM remember', False),
    ('$NLM remember', False),
    ('nlm-memory remember --summary x (y)', False),
    ('python "C:/Users/in/.claude/hooks/gate_inbox.py" ack abcd --note "x (y)"', True),
    ('python "C:/Users/in/.claude/hooks/chip_handoff.py" open --title x', False),
    ('cd "C:/x" && python "C:/Users/in/.claude/hooks/chip_handoff.py" finish --chip c1 --message "done (all); ok"', True),
    ('python C:/Users/in/.claude/hooks/chip_handoff.py status', True),
    ('python C:/Users/in/.claude/hooks/chip_handoff.py close --chip c1 --accept', True),
    ('python C:/Users/in/.claude/hooks/chip_handoff.py', False),
    ('python C:/Users/in/.claude/hooks/chip_handoff.py rename --chip c1', False),
    ('glab mr view 637 -F json', True),
    ('glab -R group/proj mr view 1', True),
    ('glab api projects/1/merge_requests', True),
    ('glab issue note 463 -m "text"', True),
    # `$(cat <file>)` only reads (report dc30d302).
    ('glab mr update 637 --ready --description "$(cat f)"', True),
    ('glab mr checkout 637', False),
    ('glab ci artifact main build', False),
    ('glab mr merge 637', True),
    ('glab issue update 1 --title x', True),
    ('glab repo view', True),
    ('glab variable get FOO', True),
    # Only the subcommands that write nothing but the tool's own home.
    ('nlm-memory rollback --backup C:/x', False),
    ('NLM=~/.local/bin/nlm-memory.cmd; $NLM rollback --backup C:/x', False),
    ('nlm-memory init', False),
    ('nlm-memory', False),
    # `${NLM}` is the same variable as `$NLM`, not a brace group (report a5767180).
    ('NLM=~/.local/bin/nlm-memory.cmd; ${NLM} remember x', True),
    ('NLM=~/.local/bin/nlm-memory.cmd; ${NLM} rollback x', False),
):
    check("read-only pipeline: {}".format(command), marker_hook.read_only_pipeline(command) == expected)
check("in PowerShell `NAME=value` is no assignment, so the variable names no tool",
      not marker_hook.read_only_pipeline('NLM=~/.local/bin/nlm-memory.cmd; $NLM remember x', "PowerShell")
      and marker_hook.read_only_pipeline('nlm-memory remember --summary "x (y)"', "PowerShell"))

# --- a write that lands only in a throwaway file, a heredoc's body and `$(cat …)` (report dc30d302);
# the memory bridge under an interpreter and PowerShell's literal assignments (report c2a8dfe0)
SCRATCHPAD = native("C:/Users/in/AppData/Local/Temp/claude/C--Users-in/0738d3d1/scratchpad")
BRIDGE = native("C:/Users/in/.codex/notebooklm-sync/bin/nlm_sync.py")
REPORTED_MR = (
    'S="{0}"; cat > "$S/mr.md" <<\'EOF\'\nCloses #484 (with) {{braces}} & > x | y\n$(not run)\nEOF\n'
    'cd "C:/tmp/chip" && glab mr create --draft --title "fix(gateway): x" '
    '--description "$(cat "$S/mr.md")" --remove-source-branch --yes 2>&1 | tail -3'
).format(SCRATCHPAD)
REPORTED_BRIDGE = (
    "$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'\n"
    "$ev = 'sh -c \"{ sleep 2; } & exit 0\" > x'\n"
    "& 'C:\\Py\\python.exe' '" + BRIDGE.replace("/", "\\") + "' remember --type GOTCHA --summary 's' --evidence $ev"
)
for label, shell, command, expected in (
    ("the reported MR shape", "Bash", REPORTED_MR, True),
    ("a literal redirect into the scratchpad", "Bash", 'git diff > "{}/d.patch"'.format(SCRATCHPAD), True),
    ("two throwaway targets through one variable", "Bash",
     'S="{0}"; git log > "$S/a"; git diff >> "$S"/b'.format(SCRATCHPAD), True),
    ("an unquoted heredoc of plain text", "Bash", 'cat > "{}/x" <<EOF\nplain $HOME text\nEOF'.format(SCRATCHPAD), True),
    ("a quoted `>` is text", "Bash", 'grep "a > b" file.txt', True),
    # A backslash separates only on Windows; elsewhere these three name files of their own.
    ("a Windows path in single quotes", "PowerShell",
     "git diff > '" + SCRATCHPAD.replace("/", "\\") + "\\d.patch'", os.name == "nt"),
    ("the bridge's remember under python", "Bash",
     'python "{}" remember --type GOTCHA --summary "a > b & {{c}}" --evidence "e"'.format(BRIDGE), True),
    ("the reported PowerShell bridge call", "PowerShell", REPORTED_BRIDGE, os.name == "nt"),
    ("the project-memory skill's direct call", "PowerShell",
     "$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'\n"
     "& \"$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe\" "
     "\"$HOME\\.codex\\notebooklm-sync\\bin\\nlm_sync.py\" `\n"
     "  remember --type GOTCHA --summary '<statement>' --evidence '<text with \"quotes\" & > signs>'",
     os.name == "nt"),
    ("a heredoc into a lasting file", "Bash", 'cat > "src/notes.md" <<\'EOF\'\nx\nEOF', False),
    ("an unquoted heredoc that substitutes", "Bash", 'cat > "{}/x" <<EOF\n$(rm -rf src)\nEOF'.format(SCRATCHPAD), False),
    ("a heredoc fed to python", "Bash", "python - <<'PY'\nprint(1)\nPY", False),
    ("a substitution that pipes", "Bash", 'glab mr create --description "$(cat f | sh)"', False),
    ("a substitution nested in the quoted name", "Bash",
     'glab mr create --description "$(cat "$(rm -rf src)")"', False),
    ("a backtick nested in the quoted name", "Bash", 'glab mr create --description "$(cat "`rm -rf src`")"', False),
    ("a here-string, which has no body", "Bash", "cat <<<EOF\nrm -rf src\nEOF", False),
    ("an operator inside a comment", "Bash", "echo hi # <<EOF\nrm -rf src\nEOF", False),
    ("an operator inside a quote carried over lines", "Bash",
     'echo "start\n<<EOF\n" ; rm -rf src ; echo "\nEOF\n"', False),
    ("a body line continued onto the delimiter", "Bash",
     "cat > \"{0}/x\" <<'EOF'\ndata\\\nEOF\nrm -rf src\nEOF".format(SCRATCHPAD), False),
    ("an operator line continued", "Bash",
     "cat > \"{0}/x\" <<'EOF' \\\n&& rm -rf src\nbody\nEOF".format(SCRATCHPAD), False),
    ("bash's $'…' quoting", "Bash", "echo $'\\'' '$(rm -rf src)'", False),
    ("a tool variable given a throwaway path", "Bash", 'GIT_EXTERNAL_DIFF="{}/x.sh"; git diff'.format(SCRATCHPAD), False),
    ("a variable given a lasting path", "Bash", 'S="C:/repo"; cat > "$S/x" <<\'EOF\'\ny\nEOF', False),
    ("a variable assigned inside a pipeline", "Bash",
     'S="{}" | cat; cat > "$S/x" <<\'EOF\'\ny\nEOF'.format(SCRATCHPAD), False),
    ("a quoted part with a bare tail", "Bash",
     'S="C:/tmp/claude-code-stack"; cat > "$S"/install.py <<\'EOF\'\nx\nEOF', False),
    ("a step back out of the scratchpad", "Bash",
     'cat > "{}/../../../repo/x" <<\'EOF\'\nx\nEOF'.format(SCRATCHPAD), False),
    ("a relative target", "Bash", "git diff > notes.md", False),
    ("a glob target", "Bash", "git diff > {}/*".format(SCRATCHPAD), False),
    ("a quoted -delete", "Bash", 'find . "-delete"', False),
    ("an escaped -delete", "Bash", "find . \\-delete", False),
    ("a quoted --output", "Bash", 'git diff "--output=C:/repo/x"', False),
    ("braces outside quotes", "Bash", "echo {a,b}", False),
    ("the bridge's rollback", "Bash", 'python "{}" rollback --to x'.format(BRIDGE), False),
    ("the bridge's hooks", "Bash", 'python "{}" hook-stop'.format(BRIDGE), False),
    ("a PowerShell tool variable", "PowerShell", "$env:GIT_EXTERNAL_DIFF = 'x'; git diff", False),
    ("a PowerShell assignment from a command", "PowerShell", "$x = Get-Content a.txt; Get-Item b", False),
    ("a PowerShell script block", "PowerShell", "Get-ChildItem | Where-Object { $_.Length -gt 1 }", False),
):
    check("read-only pipeline, {}".format(label),
          marker_hook.read_only_pipeline(command, shell) is expected, command)
check("the reported MR command can expire no verdict, though it plainly writes",
      marker_hook.shell_write({"tool_name": "Bash", "tool_input": {"command": REPORTED_MR}})
      and not marker_hook.write_capable({"tool_name": "Bash", "tool_input": {"command": REPORTED_MR}}))
# A command that does nothing but launch the Codex review lane writes nothing a candidate holds, so it
# expires no verdict wherever it starts; one that also does anything else is an ordinary command
# (report a5767180).
REVIEW_FLAGS = ("--ignore-user-config \\\n  --disable plugins --disable hooks --disable memories \\\n"
                "  -m gpt-6-sol -c model_reasoning_effort=high -c tools.web_search=true \\\n"
                "  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \\\n")
REVIEW_TAIL = native("  - < /c/tmp/codex-packet-${REVIEW_ID}.md 2>/c/tmp/codex-${REVIEW_ID}.err  # CODE_WORK_GATE_REVIEW")
REVIEW_LAUNCH = "REVIEW_ID=g17r1; timeout 3600 codex exec " + REVIEW_FLAGS + REVIEW_TAIL
for label, command, expected in (
    ("the command's own template", REVIEW_LAUNCH, True),
    ("a resumed round", "REVIEW_ID=g17r2; timeout 3600 codex exec resume 01a0ccaf-ab45-7dc0-aef7-0b0593908992 "
     + REVIEW_FLAGS + REVIEW_TAIL, True),
    # `resume [OPTIONS] [SESSION_ID]`: the session after the options (report 27c9dcd0), or the last one.
    ("a resumed round naming its session after the options", "REVIEW_ID=g20r2; timeout 3600 codex exec resume "
     + REVIEW_FLAGS + "  01a0ce53-b934-77b1-ad82-c5c39113957b \\\n" + REVIEW_TAIL, True),
    ("a resumed round asking for the last session",
     "REVIEW_ID=g20r3; timeout 3600 codex exec resume --last " + REVIEW_FLAGS + REVIEW_TAIL, True),
    ("a resumed round naming two sessions", "REVIEW_ID=g20r4; timeout 3600 codex exec resume "
     "01a0ce53-b934-77b1-ad82-c5c39113957b " + REVIEW_FLAGS + "  01a0ccaf-ab45-7dc0-aef7-0b0593908992 \\\n"
     + REVIEW_TAIL, False),
    ("a session word in a round that resumes nothing",
     REVIEW_LAUNCH.replace("--skip-git-repo-check", "--skip-git-repo-check 01a0ce53-b934-77b1-ad82-c5c39113957b"),
     False),
    ("a launch after a cd", native("cd /c/Users/in && ") + REVIEW_LAUNCH, True),
    ("a launch followed by a write", REVIEW_LAUNCH + native("\necho x > /c/Users/in/.claude/new.md"), False),
    ("a write followed by a launch", native("echo x > /c/Users/in/.claude/new.md; ") + REVIEW_LAUNCH, False),
    ("stderr into a lasting file",
     REVIEW_LAUNCH.replace(native("2>/c/tmp/codex-${REVIEW_ID}.err"), native("2>/c/Users/in/.claude/x.err")), False),
    ("no review marker", REVIEW_LAUNCH.replace("# CODE_WORK_GATE_REVIEW", ""), False),
    ("a packet from a heredoc", "codex exec - <<'EOF'\nreview\nEOF\n# CODE_WORK_GATE_REVIEW", False),
    ("output piped on", REVIEW_LAUNCH.replace("  # CODE_WORK_GATE_REVIEW",
                                              native(" | tee /c/Users/in/.claude/out.md  # CODE_WORK_GATE_REVIEW")), False),
    ("an id computed at run time", REVIEW_LAUNCH.replace("REVIEW_ID=g17r1", "REVIEW_ID=$(date +%s)"), False),
    ("a tool variable beside it", native("GIT_DIR=/c/tmp/x; ") + REVIEW_LAUNCH, False),
    ("another codex subcommand", native("REVIEW_ID=x; codex login - < /c/tmp/codex-packet-x.md  # CODE_WORK_GATE_REVIEW"), False),
    ("the subcommand in capitals, as codex_launch reads it", REVIEW_LAUNCH.replace("codex exec", "codex EXEC"), True),
    # The G17 review's own attacks: an option that writes, options read at run time, a program by path,
    # a reader beside the launch, a variable Codex reads.
    ("an option that writes a file",
     REVIEW_LAUNCH.replace("--skip-git-repo-check", native("--skip-git-repo-check -o /c/Users/in/Documents/review.md")), False),
    ("options read at run time",
     REVIEW_LAUNCH.replace("--skip-git-repo-check", native("--skip-git-repo-check $(cat /c/tmp/options.txt)")), False),
    ("a codex named by path", REVIEW_LAUNCH.replace("codex exec", "./codex exec"), False),
    ("a pipe onward, even to a reader", REVIEW_LAUNCH.replace("  # CODE_WORK_GATE_REVIEW", " | cat  # CODE_WORK_GATE_REVIEW"), False),
    ("a reader on the next line", REVIEW_LAUNCH + "\ngit status", False),
    ("CODEX_HOME given a value", native("CODEX_HOME=/c/Users/in/Documents/codex; ") + REVIEW_LAUNCH, False),
    ("an option the template does not pass", REVIEW_LAUNCH.replace("--skip-git-repo-check", "--skip-git-repo-check --json"), False),
    ("stderr merged instead of kept aside", REVIEW_LAUNCH.replace(native("2>/c/tmp/codex-${REVIEW_ID}.err"), "2>&1"), False),
    ("sent to the background by `&`",
     REVIEW_LAUNCH.replace("  # CODE_WORK_GATE_REVIEW", " &  # CODE_WORK_GATE_REVIEW"), False),
    ("an `&` inside the comment, which runs nothing", REVIEW_LAUNCH + " &", True),
    ("two launches", REVIEW_LAUNCH + "\n" + REVIEW_LAUNCH, False),
    ("a substitution inside double quotes", REVIEW_LAUNCH.replace("-m gpt-6-sol", '-m "$(true)"'), False),
    ("a second stdin redirect", REVIEW_LAUNCH.replace("  # CODE_WORK_GATE_REVIEW", native(" < /c/tmp/other.md  # CODE_WORK_GATE_REVIEW")), False),
    ("comment lines around it", "# round 1\n# CODE_WORK_GATE_REVIEW\n" + REVIEW_LAUNCH + "\n# done", True),
    ("a command after two comment lines", "# a\n# b\ngit status\n" + REVIEW_LAUNCH, False),
    # Comments that start their lines: cutting one used to leave the loop on the same spot.
    ("three comment lines in a row", "#a\n#b\n#c\n" + REVIEW_LAUNCH, True),
    # The G17 round-2 attacks: a comment naming another packet for the binding, and more than one `cd`.
    ("a comment that assigns another id", REVIEW_LAUNCH + " REVIEW_ID=g17r9", False),
    ("two cd segments", native("cd /c/Users/in; cd /c/tmp; ") + REVIEW_LAUNCH, False),
    ("a cd after the launch", REVIEW_LAUNCH + native("\ncd /c/tmp"), False),
):
    check("a review launch only: {}".format(label), marker_hook.review_launch_only(command) is expected, command)
check("a PowerShell launch is not read as one", not marker_hook.review_launch_only(REVIEW_LAUNCH, "PowerShell"))
check("the variables Codex reads are tool variables everywhere",
      not marker_hook.read_only_pipeline("OPENAI_BASE_URL=/tmp/claude/x; git status")
      and not marker_hook.read_only_pipeline("CODEX_HOME=/tmp/claude/x; git status"))
check("the plain launch is no write-capable command, the one that also writes is",
      not marker_hook.write_capable({"tool_name": "Bash", "tool_input": {"command": REVIEW_LAUNCH}})
      and marker_hook.write_capable({"tool_name": "Bash", "tool_input": {
          "command": REVIEW_LAUNCH + native("\necho x > /c/Users/in/.claude/new.md")}}))
check("a Git Bash drive path to a throwaway file is one", marker_hook.read_only_pipeline(native("git diff > /c/tmp/d.patch")))
check("`${NAME}` is no brace group, a brace group still is",
      marker_hook.read_only_pipeline("grep x ${FILE}") is True
      and marker_hook.read_only_pipeline("{ git status; }") is False
      and marker_hook.read_only_pipeline("echo {a,b}") is False)
check("the bridge is bookkeeping only under an interpreter, never for the home rule",
      marker_hook.bookkeeping_script('python "{}" remember x'.format(BRIDGE), bridge=True) is not None
      and marker_hook.bookkeeping_script('python "{}" remember x'.format(BRIDGE)) is None)
# The hooks' own scripts are named in the platform's case and by its own separator: elsewhere
# `GATE_INBOX.py` and `hooks\gate_inbox.py` are other files, which may write anything.
UPPER_INBOX = 'python "{}" ack x'.format(native("C:/Users/in/.claude/hooks/GATE_INBOX.py"))
BACKSLASHED_INBOX = 'python "hooks\\gate_inbox.py" ack x'
check("a state-only hook script is recognized only as the platform names it",
      (marker_hook.bookkeeping_script(UPPER_INBOX) is not None) is cwg.CASE_FOLDED_PATHS
      and (marker_hook.bookkeeping_script(BACKSLASHED_INBOX) is not None) is (os.name == "nt"),
      (marker_hook.bookkeeping_script(UPPER_INBOX), marker_hook.bookkeeping_script(BACKSLASHED_INBOX)))

# --- the directory holding a configuration home is not inside it (report b26e3641)
agents_home = os.path.join(AGENT_HOME, ".agents")
skill_file = os.path.join(agents_home, "skills", "x", "SKILL.md")


def grounded(cwd, command, roots=(), elsewhere=True):
    return marker_hook.on_home_ground(skill_file, cwd, list(roots),
                                      {"tool_name": "PowerShell", "tool_input": {"command": command}},
                                      elsewhere=elsewhere)


check("a command run in the directory holding a home does not own a home change another session announced",
      not grounded(AGENT_HOME, "Stop-Process -Id 1"))
check("nor does a repository there", not grounded(AGENT_HOME, "git status", roots=[AGENT_HOME]))
check("a change nobody else announced stays on the holding directory's ground",
      grounded(AGENT_HOME, "python patch_skill.py", elsewhere=False))
check("a command run inside the home owns it", grounded(agents_home, "ls"))
check("and one that spells the home by its own name from the directory holding it",
      grounded(AGENT_HOME, "Set-Content .agents/skills/x/SKILL.md 1")
      and grounded(AGENT_HOME, "cat ./.agents/skills/x/SKILL.md"))
check("a project's own dot-directory is not that home", not grounded(AGENT_HOME, "cat proj/.agents/x.md"))

with tempfile.TemporaryDirectory(prefix="cwg_bookkeeping_") as base:
    home, other = os.path.join(base, "home"), os.path.join(base, "other")
    candidate_repo(home, "bookkeeping-home")
    candidate_repo(other, "bookkeeping-other")
    for command, expires, tool in (('NLM=~/.local/bin/nlm-memory.cmd; $NLM recall "why"', False, "Bash"),
                                   ('python -c "print(1)"', True, "Bash"),
                                   (REPORTED_MR, False, "Bash"),
                                   # Its backslashed bridge path is another file off Windows.
                                   (REPORTED_BRIDGE, os.name != "nt", "PowerShell"),
                                   (REVIEW_LAUNCH, False, "Bash"),
                                   (REVIEW_LAUNCH + "\necho x > src/new.py", True, "Bash")):
        sid = session()
        try:
            mark_edit(sid, home, "src/candidate.py", "value = 6")
            anchored = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))
            # The shell ends in a repository no snapshot covered, so the command is unresolved.
            shell_between(sid, home, other, command, tool_name=tool)
            after = cwg.read_json(cwg.marker_path(cwg.session_key(sid)))
            check("an unresolved {} {} the verdict".format(command.split()[0], "expires" if expires else "keeps"),
                  (after["last_durable_ts"] != anchored["last_durable_ts"]) == expires, after)
        finally:
            cleanup(sid)

# --- a change no marker hook measured still retires the verdict from before it (report 8db8b3d2)
with tempfile.TemporaryDirectory(prefix="cwg_unmeasured_") as repo:
    candidate_repo(repo, "unmeasured")
    fixed = os.path.join(repo, "src", "candidate.py")
    sid = session()
    try:
        target = mark_edit(sid, repo, "src/candidate.py", "value = 1")
        marker, _ = gate_paths(sid)
        marker_entry = cwg.read_json(marker)
        check("a measurement keeps the lasting paths' stats",
              target in (marker_entry.get("content_stats") or {}), marker_entry)
        unchanged = gate.unmeasured_change(marker_entry, time.time())
        check("with nothing changed there is nothing to catch up, only the time the stats matched",
              unchanged is not None and unchanged[1] is None
              and unchanged[0]["content_marks"] == marker_entry["content_marks"], unchanged)
        before_fix = time.time()
        time.sleep(0.05)
        # The fix lands while the marker hooks are cancelled, so no mark records it.
        with open(fixed, "w", encoding="utf-8") as stream:
            stream.write("value = 2" + chr(10))
        fixed_at = os.stat(fixed).st_mtime_ns / 1e9
        time.sleep(0.05)
        after_fix = time.time()
        caught, added = gate.unmeasured_change(marker_entry, time.time())
        check("the unmeasured change gets a mark at its modification time",
              (added or {}).get("cause", {}).get("reason") == gate.UNMEASURED_REASON
              and abs(added["ts"] - fixed_at) < 0.01, caught)
        durable = float(caught.get("last_durable_ts") or 0.0)
        check("a verdict from before the change is stale and one from after it covers",
              not gate.content_covers(caught, before_fix, durable) and gate.content_covers(caught, after_fix, durable),
              caught)
        filler = [{"ts": 1.0 + index, "fp": "old{}".format(index)} for index in range(marker_hook.CONTENT_MARKS_KEPT)]
        crowded, crowded_added = gate.unmeasured_change(
            dict(marker_entry, content_marks=filler + list(marker_entry["content_marks"])), time.time())
        check("the catch-up keeps the marker's cap on marks and still reports the mark it added",
              len(crowded["content_marks"]) == marker_hook.CONTENT_MARKS_KEPT
              and crowded_added is not None and crowded["content_marks"][-1]["fp"] == added["fp"], crowded)
        barriers = gate.approval_barriers(caught, {"ordinary_reviews": [(before_fix, "APPROVED")]}, marker_hook.clock)
        check("the block says no marker hook measured it", "no marker hook measured" in barriers, barriers)
        with open(fixed, "w", encoding="utf-8") as stream:
            stream.write("value = 2" + chr(10))
        touched, touched_added = gate.unmeasured_change(caught, time.time())
        check("rewriting the same bytes updates the stats and adds no mark",
              touched_added is None and touched["content_marks"] == caught["content_marks"], touched)
        # A copy that kept its source's older time claims a moment the stats still matched.
        with open(fixed, "w", encoding="utf-8") as stream:
            stream.write("value = 9" + chr(10))
        kept_time = float(touched["content_stats_at"]) - 30
        os.utime(fixed, (kept_time, kept_time))
        noticed = time.time()
        copied, copied_added = gate.unmeasured_change(touched, noticed)
        check("a change whose time predates the last match is anchored when it is noticed",
              copied_added is not None and copied_added["ts"] == noticed, copied)
        os.remove(fixed)
        gone_at = time.time() + 1
        removed, removed_added = gate.unmeasured_change(copied, gone_at)
        check("a file that is gone anchors at the moment it was noticed",
              removed_added is not None and removed_added["ts"] == gone_at, removed)
        with open(fixed, "w", encoding="utf-8") as stream:
            stream.write("value = 3" + chr(10))
        run(STOP_HOOK, {"session_id": sid, "last_assistant_message": "done"})
        kept = cwg.read_json(marker) or {}
        check("the Stop hook keeps the catch-up in the marker",
              ((kept.get("content_marks") or [{}])[-1].get("cause") or {}).get("reason") == gate.UNMEASURED_REASON
              and kept.get("content_stats", {}).get(target), kept)
        check("a marker from before the stats were kept is left alone",
              gate.unmeasured_change(dict(kept, content_stats=None), time.time()) is None)
    finally:
        cleanup(sid)

# Under a deadline a hash that did not come back is unknown, not a divergence (G6 review N1); a file
# the index holds as HEAD does needs no hash at all (report 53529bba).
with tempfile.TemporaryDirectory(prefix="cwg_hash_deadline_") as repo:
    candidate_repo(repo, "hash-deadline")
    source = os.path.join(repo, "src")
    real_text = cwg.git_text
    cwg.git_text = lambda where, arguments, timeout, stdin=None: (
        None if arguments[:1] == ["hash-object"] else real_text(where, arguments, timeout, stdin))
    try:
        clean = marker_hook.staged_divergences(source, ["seed.py"], deadline=time.monotonic() + 30)
        with open(os.path.join(source, "seed.py"), "w", encoding="utf-8") as stream:
            stream.write("value = 2\n")
        subprocess.run(["git", "-C", repo, "add", "--", "src/seed.py"], check=True)
        with open(os.path.join(source, "seed.py"), "w", encoding="utf-8") as stream:
            stream.write("value = 3\n")
        bounded = marker_hook.staged_divergences(source, ["seed.py"], deadline=time.monotonic() + 30)
        unbounded = marker_hook.staged_divergences(source, ["seed.py"])
    finally:
        cwg.git_text = real_text
    check("a file the index holds as HEAD does is known clean without a hash", clean == [], clean)
    check("a hash that fails under a deadline leaves the answer unknown, while without one it still gives one",
          bounded is None and unbounded not in (None, []), (bounded, unbounded))

# A repository asks git a fixed number of times however many folders the candidate spans, and a
# folder placed by its path answers as git placing it does (report 53529bba).
with tempfile.TemporaryDirectory(prefix="cwg_many_folders_") as repo:
    candidate_repo(repo, "many-folders")
    folders = ["src", "src/Deep/Er", "docs", "lib/One", "lib/Two"]
    for folder in folders:
        os.makedirs(os.path.join(repo, *folder.split("/")), exist_ok=True)
        with open(os.path.join(repo, *folder.split("/"), "Mod.py"), "w", encoding="utf-8") as stream:
            stream.write("one = 1\n")
        commit_paths(repo, folder + "/Mod.py", "add " + folder)
    deep = os.path.join(repo, "src", "Deep", "Er", "Mod.py")
    inner = os.path.join(repo, "vendor", "inner")
    candidate_repo(inner, "inner")
    # Staged away from both HEAD and the disk: one file in each repository.
    for target, relative, root in ((deep, "src/Deep/Er/Mod.py", repo),
                                   (os.path.join(inner, "src", "seed.py"), "src/seed.py", inner)):
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("two = 2\n")
        subprocess.run(["git", "-C", root, "add", "--", relative], check=True)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("three = 3\n")
    by_dir = {cwg.normalize_path(os.path.join(repo, *folder.split("/"))): [cwg.normalize_path("Mod.py")] for folder in folders}
    by_dir[cwg.normalize_path(os.path.join(inner, "src"))] = ["seed.py"]
    calls, real_run = [], cwg.git_run
    cwg.git_run = lambda *args, **kwargs: calls.append(args[1][:2]) or real_run(*args, **kwargs)
    try:
        together = marker_hook.staged_divergences_by_dir(by_dir)
        os.environ["GIT_CEILING_DIRECTORIES"] = tempfile.gettempdir() + "-unrelated"
        located = len(calls)
        marker_hook.staged_divergences_by_dir(by_dir)
        asked = [call for call in calls[located:] if call[:1] == ["rev-parse"]]
    finally:
        cwg.git_run = real_run
        os.environ.pop("GIT_CEILING_DIRECTORIES", None)
    alone = {directory: marker_hook.staged_divergences(directory, names) for directory, names in by_dir.items()}
    check("six folders in two repositories ask git four times per repository", located == 8, calls[:located])
    check("a folder placed by its path answers as git placing it does", together == alone, (together, alone))
    check("a mixed-case folder spelled in lower case keeps its staged divergence",
          [name for name, _ in together[cwg.normalize_path(os.path.dirname(deep))]] == [cwg.normalize_path("Mod.py")], together)
    check("a folder inside a nested repository belongs to that repository",
          [name for name, _ in together[cwg.normalize_path(os.path.join(inner, "src"))]] == ["seed.py"], together)
    check("with discovery moved by the environment, git places every folder itself",
          len(asked) == len(by_dir), asked)
    # A folder that is gone is no repository, as git says of it, even inside one whose index still
    # holds a change staged there.
    two = os.path.join(repo, "lib", "Two")
    with open(os.path.join(two, "Mod.py"), "w", encoding="utf-8") as stream:
        stream.write("staged = 1\n")
    subprocess.run(["git", "-C", repo, "add", "--", "lib/Two/Mod.py"], check=True)
    shutil.rmtree(two)
    first, gone = cwg.normalize_path(os.path.join(repo, "docs")), cwg.normalize_path(two)
    check("a folder that is not there is placed by git, which calls it no repository",
          marker_hook.staged_divergences_by_dir({first: ["mod.py"], gone: ["mod.py"]})[gone] == [])

# A link or a junction on the way up to a repository already found may lead git elsewhere, so git
# places that folder itself. Python below 3.12 has no `isjunction`: the call raised there, the
# fail-open marker dropped the whole write, and a file a shell command put in a second folder of a
# repository never reached the candidate.
with tempfile.TemporaryDirectory(prefix="cwg_linked_folder_") as repo:
    from hygiene_hooks_test import NO_OLDER_PYTHON, make_junction, older_python

    candidate_repo(repo, "linked-folder")
    top = cwg.normalize_path(subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"],
                                            capture_output=True, text=True, check=True).stdout.strip())
    plain = os.path.join(repo, "src", "plain")
    os.makedirs(plain)
    # The junction leads into the same temporary tree, so removing the tree can reach nothing else.
    os.makedirs(os.path.join(repo, "target", "inner"))
    make_junction(os.path.join(repo, "target"), os.path.join(repo, "src", "linked"))
    behind = os.path.join(repo, "src", "linked", "inner")
    plain_answer, behind_answer = (marker_hook.repository_of(folder, [top]) for folder in (plain, behind))
    check("a folder of a repository already found is placed by its path",
          plain_answer == (top, "src/plain"), plain_answer)
    check("a folder behind a junction or a link is left to git", behind_answer is None, behind_answer)
    older = older_python()
    if older is None:
        print(NO_OLDER_PYTHON)
    else:
        sid = session()
        try:
            mark_edit(sid, repo, "src/plain/one.py", python=older)
            written = os.path.join(repo, "src", "other", "two.py")

            def write_second_folder():
                os.makedirs(os.path.dirname(written))
                with open(written, "w", encoding="utf-8") as stream:
                    stream.write("two = 2\n")

            mark_shell(sid, repo, "python -c writer", action=write_second_folder, python=older)
            paths = (cwg.read_json(gate_paths(sid)[0]) or {}).get("paths") or []
            check("under Python older than 3.12 a shell command's file in a second folder reaches the candidate",
                  any(path.endswith("/src/other/two.py") for path in paths), paths)
            with open(cwg.event_log_path(), encoding="utf-8") as stream:
                durable = [line for line in stream if '"durable"' in line and cwg.session_key(sid) in line]
            check("and the ledger records it as a durable change",
                  any("/src/other/two.py" in line for line in durable), durable)
        finally:
            cleanup(sid)
        probe = subprocess.run([older, "-c", "; ".join((
            "import json, sys",
            "sys.path.insert(0, sys.argv[1])",
            "import code_work_gate_mark as marker",
            "print(json.dumps([marker.repository_of(folder, [sys.argv[2]]) for folder in sys.argv[3:]]))",
        )), HERE, top, plain, behind], capture_output=True, text=True, encoding="utf-8")
        check("and places both folders as this interpreter does, a junction included",
              probe.returncode == 0 and json.loads(probe.stdout) == [[top, "src/plain"], None],
              probe.stdout + probe.stderr)

# Two index entries whose names differ only in case, as a repository made elsewhere can hold: the
# last one listed answers for the name, so a change staged to the other is not the candidate's, and
# committing it leaves the fingerprint alone (G21 review, round 1).
with tempfile.TemporaryDirectory(prefix="cwg_case_pair_") as repo:
    candidate_repo(repo, "case-pair")
    lower = os.path.join(repo, "src", "pair.py")
    with open(lower, "w", encoding="utf-8") as stream:
        stream.write("pair = 1\n")
    commit_paths(repo, "src/pair.py", "pair")

    def pair_git(*args):
        return subprocess.run(["git", "-C", repo, "-c", "user.name=Code Work Gate", "-c",
                               "user.email=gate@example.invalid"] + list(args),
                              capture_output=True, text=True, check=True).stdout.strip()

    blob = pair_git("rev-parse", ":src/pair.py")
    pair_git("update-index", "--add", "--cacheinfo", "100644,{},src/Pair.py".format(blob))
    pair_git("commit", "-q", "-m", "upper twin")
    other = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input="twin = 2\n",
                           capture_output=True, text=True, check=True).stdout.strip()
    pair_git("update-index", "--cacheinfo", "100644,{},src/Pair.py".format(other))
    before_commit = marker_hook.content_fingerprint([cwg.normalize_path(lower)])
    twin = marker_hook.staged_divergences(os.path.join(repo, "src"), ["pair.py"])
    pair_git("commit", "-q", "-m", "twin changed")
    check("a change staged to a name's case twin is not the name's divergence", twin == [], twin)
    check("committing the twin's change leaves the fingerprint alone",
          before_commit is not None and marker_hook.content_fingerprint([cwg.normalize_path(lower)]) == before_commit,
          before_commit)

check("only the platform's own separator becomes a slash, and only Windows lowers a path",
      cwg.normalize_path("Src\\A.py") == ("src/a.py" if cwg.CASE_FOLDED_PATHS else "Src\\A.py"),
      cwg.normalize_path("Src\\A.py"))
# Where case and a backslash are part of a name, `A.py` and `a.py` are two files with an index
# entry each, and `x\y.py` is one file: a change staged to `A.py` is its own divergence and moves
# its fingerprint, and a change to `x\y.py` moves that file's (Linux port review, round 1).
if not cwg.CASE_FOLDED_PATHS:
    with tempfile.TemporaryDirectory(prefix="cwg_case_twins_") as repo:
        candidate_repo(repo, "case-twins")
        for name, text in (("A.py", "upper = 1\n"), ("a.py", "lower = 1\n"), ("x\\y.py", "slash = 1\n")):
            with open(os.path.join(repo, "src", name), "w", encoding="utf-8") as stream:
                stream.write(text)
        commit_paths(repo, "src", "twins")
        upper = os.path.join(repo, "src", "A.py")
        before_stage = marker_hook.content_fingerprint([cwg.normalize_path(upper)])
        staged = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input="upper = 2\n",
                                capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", repo, "update-index", "--cacheinfo", "100644,{},src/A.py".format(staged)],
                       check=True)
        twins = marker_hook.staged_divergences(os.path.join(repo, "src"), ["A.py", "a.py"])
        check("a change staged to `A.py` is its own divergence beside `a.py`", twins == [("A.py", staged)], twins)
        check("the staged change moves `A.py`'s fingerprint", before_stage is not None
              and marker_hook.content_fingerprint([cwg.normalize_path(upper)]) != before_stage, before_stage)
        slashed = os.path.join(repo, "src", "x\\y.py")
        before_edit = marker_hook.content_fingerprint([cwg.normalize_path(slashed)])
        with open(slashed, "w", encoding="utf-8") as stream:
            stream.write("slash = 2\n")
        check("a change to a name holding a backslash moves that file's fingerprint", before_edit is not None
              and marker_hook.content_fingerprint([cwg.normalize_path(slashed)]) != before_edit, before_edit)

# An unmerged path diverges with its last stage, as the index listing gave it.
with tempfile.TemporaryDirectory(prefix="cwg_unmerged_") as repo:
    candidate_repo(repo, "ours")
    seed_file = os.path.join(repo, "src", "seed.py")

    def merge_git(*args):
        return subprocess.run(["git", "-C", repo, "-c", "user.name=Code Work Gate", "-c",
                               "user.email=gate@example.invalid"] + list(args), capture_output=True, text=True)

    merge_git("checkout", "-q", "-b", "theirs")
    with open(seed_file, "w", encoding="utf-8") as stream:
        stream.write("value = 'theirs'\n")
    merge_git("commit", "-q", "-am", "theirs")
    merge_git("checkout", "-q", "ours")
    with open(seed_file, "w", encoding="utf-8") as stream:
        stream.write("value = 'ours'\n")
    merge_git("commit", "-q", "-am", "ours")
    merge_git("merge", "-q", "theirs")
    stages = {line.split()[2]: line.split()[1] for line in merge_git("ls-files", "-s", "--", "src/seed.py").stdout.splitlines()}
    unmerged = marker_hook.staged_divergences(os.path.join(repo, "src"), ["seed.py"])
    check("an unmerged path diverges with its last stage", bool(stages.get("3"))
          and unmerged == [("seed.py", stages["3"])], (unmerged, stages))

for raw, expected in (("7", 7.0), ("abc", 3.5), ("-1", 3.5), ("", 3.5)):
    os.environ["CWG_TEST_BUDGET"] = raw
    check("a budget override of {!r} reads as {}".format(raw, expected),
          marker_hook.budget_override("CWG_TEST_BUDGET", 3.5) == expected)
del os.environ["CWG_TEST_BUDGET"]

chain = {"content_marks": [{"ts": 100.0, "fp": "A"}, {"ts": 200.0, "fp": "B", "merge": "A"}]}
check("a clean merge carries the verdict that covered what it merged",
      gate.covered_contents(chain, 150.0) == {"A", "B"} and gate.content_covers(chain, 150.0, 250.0), chain)
edited = {"content_marks": [{"ts": 100.0, "fp": "A"}, {"ts": 150.0, "fp": "E"},
                            {"ts": 200.0, "fp": "B", "merge": "E"}]}
check("a merge of content no verdict covered carries nothing", not gate.content_covers(edited, 120.0, 250.0), edited)
later = {"content_marks": chain["content_marks"] + [{"ts": 300.0, "fp": "C"}]}
check("an edit after a carried merge retires the verdict", not gate.content_covers(later, 150.0, 350.0), later)
reverted = {"content_marks": later["content_marks"] + [{"ts": 400.0, "fp": "B"}]}
check("and undoing that edit brings it back", gate.content_covers(reverted, 150.0, 450.0), reverted)
unmeasured = {"content_marks": [{"ts": 200.0, "fp": "B", "merge": "A"}]}
check("a merge mark covers nothing for a verdict that no measurement precedes",
      gate.covered_contents(unmeasured, 150.0) == set() and not gate.content_covers(unmeasured, 150.0, 250.0),
      unmeasured)
barriers = gate.approval_barriers(later, {"ordinary_reviews": [(150.0, "APPROVED")]}, marker_hook.clock)
check("the block names the edit after the merge, not the merge",
      "changed at {}".format(marker_hook.clock(300.0)) in barriers, barriers)

# --- a barrier whose pre-command snapshot never arrived says so (report e09cd889)
check("a barrier without a pre-command snapshot says so",
      "no snapshot from before it" in gate.describe_mark(
          {"ts": 100.0, "cause": {"reason": "unresolved-write-capable", "command": "glab", "no_snapshot": True}},
          marker_hook.clock))
with tempfile.TemporaryDirectory(prefix="cwg_no_snapshot_") as repo:
    candidate_repo(repo, "no-snapshot")
    sid = session()
    try:
        mark_edit(sid, repo, "src/candidate.py", "value = 7")
        run(MARK_HOOK, {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                        "tool_use_id": "shell-{}".format(uuid.uuid4().hex), "cwd": repo,
                        "tool_input": {"command": "glab api projects | python -c 'print(1)'"}})
        marker_entry = cwg.read_json(cwg.marker_path(cwg.session_key(sid))) or {}
        cause = (marker_entry.get("content_marks") or [{}])[-1].get("cause") or {}
        check("a command whose PreToolUse hook left no snapshot is marked so",
              cause.get("no_snapshot") is True and cause.get("reason") == "unresolved-write-capable", marker_entry)
    finally:
        cleanup(sid)


# --- closing survives a hook killed part-way through it (report 267de208)
class Killed(Exception):
    pass


sid = session()
try:
    marker, state_file = gate_paths(sid)
    seed(sid, ["C:/repo/src/auth.py"])
    blocked = {"candidate_key": "k", "blocks": 1, "block_nonce": "n1", "last_block_ts": 1.0,
               "last_block_reason": "HIGH candidate lacks a current APPROVED verdict"}
    check("seed block state", cwg.write_json(state_file, blocked))
    receipt = ("anomaly-reported", "abcdef12; the approval is current", None)
    real_remove, real_write = cwg.remove, cwg.write_json
    cwg.remove = lambda path: False if path == marker else real_remove(path)
    cwg.write_json = lambda path, data: False if path == marker else real_write(path, data)
    try:
        closed = gate.close_cycle(marker, state_file, dict(blocked), 110.0, receipt, cwg.session_key(sid))
    finally:
        cwg.remove, cwg.write_json = real_remove, real_write
    check("a marker that cannot be retired leaves the block on record for the same receipt",
          not closed and os.path.exists(marker) and cwg.read_json(state_file) == blocked,
          cwg.read_json(state_file))

    def killed(_):
        raise Killed()
    real_retire = cwg.retire_claims
    cwg.retire_claims = killed
    try:
        gate.close_cycle(marker, state_file, dict(blocked), 110.0, receipt, cwg.session_key(sid))
    except Killed:
        pass
    finally:
        cwg.retire_claims = real_retire
    check("a hook killed while sweeping has already retired the candidate",
          not os.path.exists(marker) and (cwg.read_json(state_file) or {}).get("blocks") == 0,
          cwg.read_json(state_file))
finally:
    cleanup(sid)


# --- XHIGH: the lanes an XHIGH receipt is held to, and where each lane's level is read from
XHIGH_RECEIPT = "[gate] verified: XHIGH; KDF rewrite, XHIGH lenses and an XHIGH approval"
XHIGH_SUBJECT = "the KDF rewrite"
ASTRA_TURN = (cwg.XHIGH_CODEX_MODEL, cwg.XHIGH_CODEX_EFFORT)
SOL_TURN = ("gpt-6-sol", "high")
# The launch may name any model: only the turn the rollout log records decides the level.
ASTRA_COMMAND = CODEX_CLI_COMMAND.replace(
    "codex exec", "codex exec -m gpt-6-astra -c model_reasoning_effort=ultra")
NOT_XHIGH_LANE = "did not come from an XHIGH lane"


def xhigh_case(add_lanes, receipt=XHIGH_RECEIPT, lenses=gate.XHIGH_LENSES, last_ts=110.0,
               durable_ts=None):
    """A crypto-path candidate whose simplify pass ran `lenses`, then `add_lanes(events)`."""
    sid = session()
    seed(sid, ["C:/repo/src/crypto/kdf.ts"], last_ts=last_ts, durable_ts=durable_ts)
    events = base_events(include_simplify=True, lenses=lenses)
    add_lanes(events)
    return stop_with(sid, events, receipt)


def xhigh_native(events, subtype=cwg.XHIGH_REVIEWER, verdict="APPROVED", stamp=130, model=None):
    add_review(events, stamp, "xreview-{}".format(stamp), review_text(verdict, XHIGH_SUBJECT),
               subtype=subtype, model=model)


def reuse_lens_overridden(events):
    """The XHIGH reuse lens run with a model passed on the call, then the XHIGH reviewer approves."""
    events.append(agent_use(125, gate.XHIGH_LENSES[0], "xreuse-override", model="sonnet"))
    events.append(tool_result(125.5, "xreuse-override", "No actionable findings."))
    xhigh_native(events)


def resumed_approval(subtype):
    """A reviewer on `subtype` answers REVISE, then approves in a round SendMessage resumed."""
    def add_lanes(events):
        foreground_review(events, 130, "xreview-1", "agent-x2", review_text("REVISE", XHIGH_SUBJECT),
                          subtype=subtype)
        send_message(events, 132, "send-132", "agent-x2")
        events.extend(round_notification(140, "agent-x2", review_text("APPROVED", XHIGH_SUBJECT),
                                         "send-132"))
    return add_lanes


def native_background_approval(events):
    add_background_review(events, 130, "xbg-1", "agent-x1", subtype=cwg.XHIGH_REVIEWER)
    events.extend(agent_notification(140, "agent-x1", review_text("APPROVED", XHIGH_SUBJECT)))


def stale_xhigh_then_high(events):
    """An XHIGH approval that an edit at 133 made stale, then a current approval below XHIGH."""
    xhigh_native(events, stamp=130)
    xhigh_native(events, subtype="adversarial-reviewer", stamp=135)


def turn_records(first, logged, said, tie):
    """A briefed log's records from `first` on: turns (model, effort) and "said", in this order,
    each at its own stamp or, with `tie`, all at one — as records written in one millisecond are."""
    records = [(first, "developer", reviewer_role_text() + "\n\nXHIGH turn packet.")]
    for index, item in enumerate(logged):
        stamp = first + 0.1 + (0.0 if tie else index / 10.0)
        records.append((stamp, "assistant", said) if item == "said" else (stamp, "turn", item))
    return records


def codex_turns(*logged, tie=False):
    """A foreground Codex round whose log holds `logged` (see `turn_records`)."""
    said = codex_cli_output(review_text("APPROVED", XHIGH_SUBJECT))

    def add_lanes(events):
        events.append(bash_use(130, "xcodex-2", CODEX_COMMAND))
        events.append(tool_result(131, "xcodex-2", said))
        rollout_records(turn_records(130.1, logged, said, tie))
    return xhigh_case(add_lanes)


def xhigh_background(turn=None, logged=None):
    """An XHIGH candidate whose Codex round ran in the background: its turn logged as `turn`, or
    its log holding `logged` at one stamp (see `turn_records`)."""
    now = time.time()
    task_id = "xtask" + uuid.uuid4().hex[:5]
    out_file = os.path.join(tasks_dir, task_id + ".output")
    said = codex_cli_output(review_text("APPROVED", XHIGH_SUBJECT))
    write_review_output(out_file, said, now - 601)
    sid = session()
    seed(sid, ["C:/repo/src/crypto/kdf.ts"], first_ts=now - 900, last_ts=now - 800, durable_ts=now - 800)
    events = background_review_events(now, task_id, out_file,
                                      DETACHED_ACK.format(id=task_id, out=out_file),
                                      lenses=gate.XHIGH_LENSES)
    if logged:
        rollout_records(turn_records(now - 660, logged, said, tie=True))
    else:
        log_codex_run(now - 650, said, turn=turn)
    return stop_with(sid, events, XHIGH_RECEIPT)


def verified(result):
    return result.get("continue") is True and "decision" not in result


def blocked(result, *reasons):
    return result.get("decision") == "block" and all(
        reason in result.get("reason", "") for reason in reasons)


check("XHIGH ranks above HIGH, and no path floor reaches it",
      cwg.RISK_ORDER["XHIGH"] > cwg.RISK_ORDER["HIGH"]
      and cwg.minimum_risk(["C:/repo/src/crypto/kdf.ts"]) == "HIGH", cwg.RISK_ORDER)
check("an XHIGH receipt parses with its level",
      gate.receipt_of(XHIGH_RECEIPT) == ("verified", XHIGH_RECEIPT.split(": ", 1)[1], "XHIGH"),
      gate.receipt_of(XHIGH_RECEIPT))
check("an XHIGH lens stands in for its HIGH lens, at HIGH and in the STANDARD trio",
      gate.simplify_missing("STANDARD", {lens: "current" for lens in gate.XHIGH_LENSES}) == []
      and gate.simplify_missing("HIGH", {gate.SIMPLIFY_LENSES[0]: "current",
                                         gate.XHIGH_LENSES[1]: "current",
                                         gate.XHIGH_LENSES[2]: "current"}) == [])
for label, add_lanes, receipt, lenses, expect in (
    ("the XHIGH lenses and the XHIGH reviewer's approval verify an XHIGH receipt",
     xhigh_native, XHIGH_RECEIPT, gate.XHIGH_LENSES, None),
    ("an approval from the HIGH reviewer does not, and the block names what it read",
     lambda events: xhigh_native(events, subtype="adversarial-reviewer"), XHIGH_RECEIPT,
     gate.XHIGH_LENSES, (NOT_XHIGH_LANE, "came from a lane below XHIGH; XHIGH lanes stated nothing")),
    ("an XHIGH REVISE is no approval",
     lambda events: xhigh_native(events, verdict="REVISE"), XHIGH_RECEIPT, gate.XHIGH_LENSES,
     ("no terminal APPROVED",)),
    ("the HIGH lenses do not stand in for their XHIGH runs",
     xhigh_native, XHIGH_RECEIPT, gate.SIMPLIFY_LENSES, (gate.XHIGH_LENSES[0],)),
    ("the XHIGH lanes satisfy a HIGH receipt",
     xhigh_native, VERIFIED_HIGH, gate.XHIGH_LENSES, None),
    ("a resumed round of the XHIGH reviewer keeps its level",
     resumed_approval(cwg.XHIGH_REVIEWER), XHIGH_RECEIPT, gate.XHIGH_LENSES, None),
    ("a resumed round of the HIGH reviewer does not gain it",
     resumed_approval("adversarial-reviewer"), XHIGH_RECEIPT, gate.XHIGH_LENSES, (NOT_XHIGH_LANE,)),
    ("the XHIGH reviewer's verdict read at its background notification is an XHIGH approval",
     native_background_approval, XHIGH_RECEIPT, gate.XHIGH_LENSES, None),
    ("the XHIGH reviewer run with a model passed on the call is below XHIGH",
     lambda events: xhigh_native(events, model="haiku"), XHIGH_RECEIPT, gate.XHIGH_LENSES,
     (NOT_XHIGH_LANE,)),
    ("an XHIGH lens run with a model passed on the call is not that XHIGH lens",
     reuse_lens_overridden, XHIGH_RECEIPT, gate.XHIGH_LENSES[1:], (gate.XHIGH_LENSES[0],)),
    ("but it still proves its HIGH lens",
     reuse_lens_overridden, VERIFIED_HIGH, gate.XHIGH_LENSES[1:], None),
):
    result = xhigh_case(add_lanes, receipt, lenses)
    check("XHIGH: " + label, verified(result) if expect is None else blocked(result, *expect), result)
result = xhigh_case(stale_xhigh_then_high, last_ts=133.0, durable_ts=133.0)
check("XHIGH: a stale XHIGH approval lends no level to a current approval below it",
      blocked(result, NOT_XHIGH_LANE), result)
for label, command, turn, expect_ok in (
    ("a Codex round whose turn ran on the XHIGH model and effort is an XHIGH approval",
     CODEX_COMMAND, ASTRA_TURN, True),
    ("a Codex round the command says ran on the XHIGH model but whose log shows Sol is not",
     ASTRA_COMMAND, SOL_TURN, False),
    ("a Codex round on the XHIGH model below its effort is not",
     CODEX_COMMAND, (cwg.XHIGH_CODEX_MODEL, "high"), False),
    ("a Codex round whose log records no turn is not", CODEX_COMMAND, None, False),
):
    result = xhigh_case(lambda events: add_codex_review(
        events, 130, "xcodex-1", command,
        codex_cli_output(review_text("APPROVED", XHIGH_SUBJECT)), turn=turn))
    check("XHIGH foreground: " + label,
          verified(result) if expect_ok else blocked(result, NOT_XHIGH_LANE), result)
result = codex_turns(ASTRA_TURN, "said")
check("XHIGH foreground: the turn opened before the verdict decides its level", verified(result), result)
result = codex_turns(SOL_TURN, "said", ASTRA_TURN)
check("XHIGH foreground: an XHIGH turn after the verdict does not raise it",
      blocked(result, NOT_XHIGH_LANE), result)
result = codex_turns(SOL_TURN, "said", ASTRA_TURN, tie=True)
check("XHIGH foreground: nor does one logged in the verdict's own millisecond",
      blocked(result, NOT_XHIGH_LANE), result)
result = codex_turns(ASTRA_TURN, "said", SOL_TURN, tie=True)
check("XHIGH foreground: in one millisecond, the turn opened before the verdict still decides",
      verified(result), result)
result = xhigh_background(logged=(SOL_TURN, "said", ASTRA_TURN))
check("XHIGH background: an XHIGH turn in the verdict's millisecond does not raise it",
      blocked(result, NOT_XHIGH_LANE), result)
result = xhigh_background(ASTRA_TURN)
check("XHIGH background: a Codex round on the XHIGH model and effort is an XHIGH approval",
      verified(result), result)
result = xhigh_background(SOL_TURN)
check("XHIGH background: a Codex round on the HIGH lane's model is not",
      blocked(result, NOT_XHIGH_LANE), result)

# The XHIGH profiles are the HIGH prompts re-pinned: a drift between the two would review XHIGH
# work by rules no HIGH lane follows.
for base, xhigh, pins in [
    (lens, stronger, ("model: claude-opus-5-5", "effort: max"))
    for lens, stronger in zip(gate.SIMPLIFY_LENSES, gate.XHIGH_LENSES)
] + [("adversarial-reviewer", cwg.XHIGH_REVIEWER, ("effort: max",))]:
    front, body = profile_parts(xhigh)
    check("the {} profile runs the {} prompt unchanged".format(xhigh, base),
          body == profile_parts(base)[1], xhigh)
    check("the {} profile is named for its lane and pinned to {}".format(xhigh, ", ".join(pins)),
          "\nname: {}\n".format(xhigh) in front
          and all("\n{}\n".format(pin) in front for pin in pins), front)


print("PASS: {} assertions".format(PASSED))
