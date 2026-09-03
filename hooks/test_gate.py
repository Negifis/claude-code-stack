"""Regression tests for the strict finite Code Work Gate."""
import atexit
import datetime
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

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_HOOK = os.path.join(HERE, "code_work_gate_stop.py")
# Every throwaway home below is named after this process. The suite tears these trees down and
# rebuilds them between cases, so two runs sharing one name delete each other's fixtures
# mid-scenario: that is what made the Codex-evidence cases fail intermittently whenever a second
# run - a mutation sweep, a second terminal - happened to overlap this one.
RUN = "gate_{}".format(os.getpid())
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
    for tree in (CODEX_HOME, CLAUDE_CONFIG_DIR, AGENT_HOME):
        shutil.rmtree(tree, ignore_errors=True)
    try:
        registry = cwg.claims_root()
        for name in os.listdir(registry):
            if name.startswith(RUN + "_test_"):
                cwg.remove(os.path.join(registry, name))
    except OSError:
        pass


atexit.register(_discard_fixtures)

sys.path.insert(0, HERE)
import code_work_gate_common as cwg  # noqa: E402
import code_work_gate_mark as marker_hook  # noqa: E402
mark = marker_hook
import code_work_gate_stop as gate  # noqa: E402

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


def agent_use(stamp, subtype, call_id, run_in_background=False):
    payload = {
        "subagent_type": subtype,
        "prompt": "bounded packet",
    }
    if run_in_background is not None:
        payload["run_in_background"] = run_in_background
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


SIMPLIFY_LENSES = [gate.SIMPLIFY_LANE]
LEGACY_LENSES = sorted(gate.SIMPLIFY_REVIEWERS - {gate.SIMPLIFY_LANE})
PROMPT_HOOK = os.path.join(HERE, "code_work_gate_prompt.py")
import codex_lane  # noqa: E402


def simplify_wave(events, stamp, prefix, subtypes):
    for index, subtype in enumerate(subtypes):
        call_id = "{}-{}".format(prefix, index)
        events.append(agent_use(stamp, subtype, call_id))
        events.append(tool_result(stamp + 0.5, call_id, "No actionable findings."))
        stamp += 1
    return stamp


def base_events(include_simplify=False):
    events = [skill_use(120, "development-verification", "skill-dev")]
    if include_simplify:
        events.append(skill_use(121, "simplify", "skill-simplify"))
        simplify_wave(events, 122, "simplify", SIMPLIFY_LENSES)
    return events


def add_review(events, stamp, call_id, result, is_error=False):
    events.append(agent_use(stamp, "adversarial-reviewer", call_id))
    events.append(tool_result(stamp + 0.5, call_id, result, is_error=is_error))


def add_codex_review(events, stamp, call_id, command, result, is_error=False,
                     run_in_background=None, tool="Bash", codex_ran=True):
    """A shell review call, plus the rollout log the Codex CLI writes while it runs.

    `codex_ran=False` is the forgery case: the command printed a verdict, but no Codex process
    was running while the call was open.
    """
    events.append(bash_use(stamp, call_id, command,
                           run_in_background=run_in_background, tool=tool))
    events.append(tool_result(stamp + 0.5, call_id, result, is_error=is_error))
    if codex_ran:
        log_codex_run(stamp + 0.4, result)


CODEX_COMMAND = 'node "codex-companion.mjs" adversarial-review "--wait CODE_WORK_GATE_REVIEW"'
REQUIRED_CODEX_COMMAND = CODEX_COMMAND[:-1] + ' CODE_WORK_GATE_REQUIRED"'
CODEX_ERRAND_COMMAND = 'node "codex-companion.mjs" review "--wait CODE_WORK_GATE_REVIEW"'
CODEX_CLI_COMMAND = "codex exec --json - < /c/tmp/packet-CODE_WORK_GATE_REVIEW.md"


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


def reviewer_role_text():
    """The role text a Codex review session must have been given, as the hook reads it."""
    with open(os.path.join(os.path.dirname(HERE), "agents", "adversarial-reviewer.md"),
              encoding="utf-8") as stream:
        return stream.read().split("---", 2)[-1]


def log_codex_run(stamp, logged="", role="assistant", said_at=None, briefed=True,
                  partial_role=False, briefed_at=None, filler_bytes=0, earlier=None):
    """One Codex rollout log in the CLI's own shape.

    `logged` is what the session said, and only an assistant record stamped inside the call's
    window can vouch for a result: `role` and `said_at` exist so a test can put the same text in
    the prompt the call supplied, or in the older part of a resumed session. `briefed` writes the
    reviewer role into the session's input, which is what marks it a review rather than an errand.
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
                                                 + "\n\nRound 1 packet."}]},
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
        stream.write(json.dumps({
            "timestamp": iso(stamp if said_at is None else said_at),
            "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "output_text", "text": logged}]},
        }) + "\n")
    os.utime(path, (stamp, stamp))
    return path


def run(script, payload):
    proc = subprocess.run(
        [sys.executable, script],
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
    transcript = write_transcript(base_events())
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

sid = session()
try:
    seed(sid, [cwg.SHELL_MUTATION_PATH])
    transcript = write_transcript(base_events())
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; command checked",
    })
    check("operational candidate rejects a code receipt", result.get("decision") == "block", result)
    check(
        "operational candidate names its own contract",
        "changed no lasting artifact" in result.get("reason", ""),
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
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/.claude/rules/operations.md"])
    transcript = write_transcript(base_events())
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
        "three-file standard passes without a simplify lane",
        result.get("continue") is True and "decision" not in result,
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

for paths, receipt, label in (
    (["C:/repo/tests/app.test.py"], "[gate] verified: LOW; checks passed", "low"),
    (["C:/repo/src/app.py"], "[gate] verified: STANDARD; checks passed", "small standard"),
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
    events.append(agent_use(132, gate.SIMPLIFY_LANE, "lane-fail"))
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
    add_review(events, 131, "review-legacy-trio", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; checks and review passed",
    })
    check(
        "legacy lenses spread across waves still complete a HIGH pass",
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
    events.append(agent_use(125, gate.SIMPLIFY_LANE, "simplify-lane-fail-1"))
    events.append(tool_result(
        125.5, "simplify-lane-fail-1", "unavailable", is_error=True
    ))
    events.append(agent_use(127, gate.SIMPLIFY_LANE, "simplify-lane-fail-2"))
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
    events = base_events()
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
    check("high with one simplify lane and current approval passes", result.get("continue") is True and "decision" not in result, result)
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
        "the missing lane is named",
        "no foreground result" in result.get("reason", "") and gate.SIMPLIFY_LANE in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/auth/session.ts"])
    events = [skill_use(120, "development-verification", "skill-dev")]
    simplify_wave(events, 122, "legacy", LEGACY_LENSES)
    add_review(events, 130, "review-legacy", "No blockers.\nVERDICT: APPROVED")
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: HIGH; auth tests and review passed",
    })
    check("the legacy trio still satisfies a HIGH candidate", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

# --- background work: a review lane that went to the background, and turns that may end
def notification(stamp, task_id, output_file, status="completed"):
    text = (
        "<task-notification>\n<task-id>{}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
        "<output-file>{}</output-file>\n<status>{}</status>\n"
        "<summary>Background command \"review\" {}</summary>\n</task-notification>"
    ).format(task_id, output_file, status, "completed (exit code 0)" if status == "completed" else status)
    return entry(stamp, "user", [{"type": "text", "text": text}])


def background_review_events(now, task_id, out_file, ack_text, notify_status="completed", notify=True):
    events = [skill_use(now - 890, "development-verification", "skill-dev")]
    simplify_wave(events, now - 880, "simplify", SIMPLIFY_LENSES)
    events.append(bash_use(now - 700, "codex-" + task_id, CODEX_CLI_COMMAND,
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
    ("a detached launch",
     "Command running in background with ID: {id}. Output is being written to: {out}. You will be notified when it completes."),
    ("a foreground launch the harness moved to the background",
     "Command did not complete within its 120s timeout and was moved to the background (ID: {id}). Output is being written to: {out}. You will be notified when it completes."),
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
    events.append(bash_use(old, "codex-dead", CODEX_CLI_COMMAND, run_in_background=True))
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
              + review_text("APPROVED"))
    add_codex_review(events, now - 700, "codex-fg", CODEX_CLI_COMMAND, codex_cli_output(spoken))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {"session_id": sid, "transcript_path": transcript,
                             "last_assistant_message": "[gate] verified: HIGH; Codex reviewed in the foreground"})
    check("a foreground result that opens with the whole moved-to-background envelope is still a review",
          result.get("continue") is True and "decision" not in result and "background work" not in result.get("systemMessage", ""), result)
finally:
    cleanup(sid, locals().get("transcript"))

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
    events.append(entry(now - 880, "assistant", [{
        "type": "tool_use", "id": "simp-note", "name": "Agent",
        "input": {"subagent_type": "simplify-reviewer", "run_in_background": False,
                  "description": "simplify"},
    }]))
    events.append(tool_result(now - 870, "simp-note",
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
    events.append(bash_use(now - 700, "codex-lim", CODEX_CLI_COMMAND + " 2>" + err_path.replace("\\", "/"),
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
def inbox(*args, stdin=None):
    proc = subprocess.run([sys.executable, GATE_INBOX] + list(args), input=stdin, text=True,
                          encoding="utf-8", capture_output=True, check=False)
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
    events.append(bash_use(now - 500, "codex-rl", CODEX_CLI_COMMAND + " 2>" + err_path.replace(chr(92), "/"), run_in_background=True))
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
with open(registry, "w", encoding="utf-8") as stream:
    stream.write("[1, 2]")
code, out, err = inbox("report", "--session", sid, "--block", "x", "--facts", "y")
check("a corrupt registry means no gate-ops session, not a crash",
      code == 0 and "No gate-ops session is registered" in out, err)
inbox("ack", re.search(r"GATE_ANOMALY: ([0-9a-f]{8})", out).group(1))
os.remove(registry)
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
        check("the quiet command still keeps the candidate open",
              cwg.SHELL_MUTATION_PATH in (data.get("paths") or []), data)
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
    check(
        "a recorded outage makes the lane unavailable",
        codex_lane.record_outage("ERROR: Selected model is at capacity. Please try a different model.") is True
        and codex_lane.status()[0] is False and "unavailable until" in codex_lane.status()[1],
        codex_lane.status(),
    )
    check("clearing restores the lane", codex_lane.clear_state() and codex_lane.status()[0] is True, codex_lane.status())
    check(
        "the stderr redirect of the lean command is found",
        codex_lane.stderr_file_of("timeout 3600 codex exec --ignore-user-config - < /c/tmp/codex-packet-1.md 2>/c/tmp/codex-1.err  # CODE_WORK_GATE_REVIEW")
        == "C:/tmp/codex-1.err",
        codex_lane.stderr_file_of("x 2>/c/tmp/codex-1.err"),
    )
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
        codex_lane.stderr_file_of('REVIEW_ID=r7; timeout 3600 codex exec - < /c/tmp/codex-packet-${REVIEW_ID}.md 2>/c/tmp/codex-${REVIEW_ID}.err')
        == "C:/tmp/codex-r7.err",
        codex_lane.stderr_file_of('REVIEW_ID=r7; x 2>/c/tmp/codex-${REVIEW_ID}.err'),
    )
    capture_dir = codex_lane.CAPTURE_GLOB.rsplit("/", 1)[0]
    os.makedirs(capture_dir, exist_ok=True)
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
        for stray in (stale, fresh):
            try:
                os.remove(stray)
            except OSError:
                pass
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
                     "Get-Content -Raw C:\\tmp\\packet.md | codex exec --json -m gpt-5.6-sol - "
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

    with tempfile.TemporaryDirectory(prefix="cwg_cross_root_") as elsewhere:
        sid = session()
        try:
            marker, _ = gate_paths(sid)
            durable = os.path.join(elsewhere, "hooks", "gate.py")
            run(MARK_HOOK, {
                "session_id": sid,
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "cwd": repo,
                "tool_input": {"file_path": durable},
            })
            approved_at = (cwg.read_json(marker) or {}).get("last_durable_ts")
            payload = {
                "session_id": sid,
                "tool_use_id": "clean-snapshot-elsewhere",
                "tool_name": "Bash",
                "cwd": repo,
                "tool_input": {"command": "python -c write_outside_repo"},
            }
            run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
            run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))
            data = cwg.read_json(marker) or {}
            check(
                "a clean snapshot cannot vouch for durable paths outside its repository",
                cwg.valid_ts(approved_at) and data.get("last_durable_ts", 0) > approved_at,
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


def mark_edit(sid, repo, relative):
    """One gated edit reported through the marker hook, as PostToolUse delivers it."""
    target = os.path.join(repo, *relative.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as stream:
        stream.write("changed = True\n")
    run(MARK_HOOK, {
        "session_id": sid,
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": repo,
        "tool_input": {"file_path": target},
    })
    return cwg.normalize_path(target)


def mark_shell(sid, repo, command, action=None):
    """A mutating shell call as production delivers it: the snapshot pair around the work."""
    payload = {
        "session_id": sid,
        "tool_use_id": "shell-{}".format(uuid.uuid4().hex),
        "tool_name": "Bash",
        "cwd": repo,
        "tool_input": {"command": command},
    }
    run(MARK_HOOK, dict(payload, hook_event_name="PreToolUse"))
    if action:
        action()
    run(MARK_HOOK, dict(payload, hook_event_name="PostToolUse"))


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
    "C:/Users/in/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py",
    "C:/tmp/sid/scratchpad/push.py",
    "/tmp/wipe.sh",
    "/var/tmp/rotate.py",
    "C:/Users/in/.claude/state/checkpoints/proj.md",
    "C:/Users/in/.claude/plans/plan.md",
):
    check("throwaway artifact is not gated: {}".format(path), not cwg.is_gated(path), path)
    check("throwaway artifact is not durable: {}".format(path), not cwg.durable_paths([path]), path)

for path in (
    "C:/tmp/charon-whatsnew/backend/src/services/featureRegistry.ts",
    "C:/Users/in/AppData/Local/Temp/build-clone/src/app.py",
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
    "C:/Users/in/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py",
    "/tmp/wipe.sh",
    "C:/tmp/sid/scratchpad/push.py",
):
    check("ephemeral matcher agrees with the gate: {}".format(path), cwg.is_ephemeral(path), path)

for path in (
    "C:/tmp/charon-whatsnew/backend/src/app.ts",
    "C:/repo/.claude/state-machine/runner.py",
    "C:/repo/src/scratchpadding.ts",
    "C:/repo/.claude/plans/rollout.md",
    "C:/repo/.claude/state/registry.json",
    "C:/backup/appdata/local/temp/keep.py",
):
    check("ephemeral matcher does not overreach: {}".format(path), not cwg.is_ephemeral(path), path)

for path in (
    "C:/Users/in/.claude/state/checkpoints/proj.md",
    "C:/Users/in/.claude/plans/plan.md",
    "/home/dev/.claude/state/checkpoints/proj.md",
):
    check("home bookkeeping is ephemeral: {}".format(path), cwg.is_ephemeral(path), path)

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
        ["c:/users/in/.claude/hooks/gate.py"], ["C:/repo"]
    ) == ["c:/users/in/.claude/hooks/gate.py"],
)
check(
    "a watched configuration tree is one of the trees a command is judged against",
    marker_hook.outside_snapshot(
        ["c:/users/in/.claude/hooks/gate.py"],
        ["C:/repo"],
        ["C:/Users/in/.claude/hooks"],
    ) == [],
)
check(
    # The scan opens six directories, not the home: claiming the home would vouch for the
    # machine-managed plugin tree the gate still grades HIGH.
    "an unwatched pocket of a configuration home is never vouched for",
    marker_hook.outside_snapshot(
        ["c:/users/in/.claude/plugins/repo/hook.js"],
        [],
        ["C:/Users/in/.claude/hooks", "C:/Users/in/.claude/skills"],
    ) != [],
)
check(
    "a skipped subdirectory inside a watched tree is not vouched for either",
    marker_hook.outside_snapshot(
        ["c:/users/in/.claude/skills/x/node_modules/tool.js"],
        [],
        ["C:/Users/in/.claude/skills"],
    ) != [],
)
check(
    "a watched file vouches for itself",
    marker_hook.outside_snapshot(
        ["c:/users/in/.claude/settings.json"], [], ["C:/Users/in/.claude/settings.json"]
    ) == [],
)
check(
    # `plans` and `state` are bookkeeping in a configuration home and ordinary source in a
    # repository; Git reports on them either way.
    "a repository vouches for source in a directory a configuration home would skip",
    marker_hook.outside_snapshot(
        ["c:/repo/src/pages/plans/planrow.tsx", "c:/repo/src/state/store.ts"],
        ["C:/repo"],
    ) == [],
)
check(
    "an empty snapshot vouches for paths under its root",
    marker_hook.outside_snapshot(["c:/repo/src/app.ts"], ["C:/repo"]) == [],
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
    scratch = os.path.join(tempfile.gettempdir(), "cwg_scratch_probe.py")
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
                any(path.endswith("/skills/hand-written/skill.md")
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
        target = "c:/repo/src/app.py"
        check(
            "an announcement inside the window is foreign",
            cwg.publish_claims(stale, paths=["C:/repo/src/app.py"], now=now)
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
            cwg.publish_claims(stale, shell_start_ts=now, cwd="C:/repo", now=now)
            and cwg.foreign_activity("reader", now - 1, now)[2] == {"c:/repo"},
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "an announcement outlives any prompt, however long it is left open",
            cwg.publish_claims(stale, paths=["C:/repo/src/slow.py"], pending=True,
                               now=now - cwg.SHELL_WINDOW_LIMIT - 60)
            and "c:/repo/src/slow.py"
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
            == {"c:/repo/src/slow.py"}
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
                cwg.publish_claims(other, paths=["C:/repo/src/x{}.py".format(index)],
                                   pending=True, now=now + index)
                for index, other in enumerate(crowd)
            )
            and "c:/repo/src/slow.py"
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
                                   paths=["C:/repo/src/many{}.py".format(index)], now=now)
                for index in range(cwg.SCAN_LIMIT + 1)
            )
            and cwg.foreign_activity("reader", now - 1, now)[3] is True
            and mark.own_delta("reader", "c:/repo", ["c:/repo/src/mine.py"], now - 1, False,
                               [("c:/repo", ())]) == ([], ["c:/repo/src/mine.py"]),
            len(os.listdir(cwg.claims_root())),
        )
        for name in list(os.listdir(cwg.claims_root())):
            if name != os.path.basename(cwg.claim_path(stale)):
                cwg.remove(os.path.join(cwg.claims_root(), name))
        check(
            "a claim file past the horizon is dropped rather than believed",
            # Promoted first, because only a file with nothing outstanding is the sweep's to
            # take: an announcement is what keeps one alive past the horizon.
            cwg.publish_claims(stale, paths=["C:/repo/src/slow.py"], now=now)
            and cwg.publish_claims(stale, paths=["C:/repo/src/app.py"], now=now)
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
            cwg.publish_claims(stale, paths=["src/app.py"], cwd="C:/repo")
            and target in (cwg.read_json(cwg.claim_path(stale)) or {}).get("claims", {}),
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "settling a path promotes it out of the announced map",
            cwg.publish_claims(stale, paths=["C:/repo/src/app.py"], pending=True)
            and cwg.publish_claims(stale, paths=["C:/repo/src/app.py"])
            and target in (cwg.read_json(cwg.claim_path(stale)) or {}).get("claims", {})
            and target not in (cwg.read_json(cwg.claim_path(stale)) or {}).get("pending", {}),
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "an unconfirmed announcement is not read as a settled claim",
            cwg.publish_claims(stale, paths=["C:/repo/src/only-announced.py"], pending=True)
            and "c:/repo/src/only-announced.py"
            not in cwg.foreign_activity("reader", time.time() - 1)[0]
            and "c:/repo/src/only-announced.py"
            in cwg.foreign_activity("reader", time.time() - 1)[1],
            cwg.read_json(cwg.claim_path(stale)),
        )
        check(
            "a closed cycle retires the registry file",
            cwg.retire_claims(stale) and not os.path.exists(cwg.claim_path(stale)),
        )
        check(
            "a still-open shell window survives the cycle that closed around it",
            cwg.publish_claims(stale, shell_start_ts=time.time(), cwd="C:/repo")
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
for event, groups in events.items():
    for group in groups or ():
        for hook in group.get("hooks") or ():
            if "code_work_gate_mark" in str(hook.get("command") or ""):
                marker_events.setdefault(event, []).append(group.get("matcher") or "")
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


print("PASS: {} assertions".format(PASSED))
