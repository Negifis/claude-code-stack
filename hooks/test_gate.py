"""Regression tests for the strict finite Code Work Gate."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_HOOK = os.path.join(HERE, "code_work_gate_stop.py")
# The hook reads the Codex CLI's rollout logs as proof a review actually ran; point both the
# suite and the hook subprocesses at a throwaway home so the developer's real one is untouched.
CODEX_HOME = os.path.join(tempfile.gettempdir(), "gate_codex_home")
os.environ["CODEX_HOME"] = CODEX_HOME
MARK_HOOK = os.path.join(HERE, "code_work_gate_mark.py")

sys.path.insert(0, HERE)
import code_work_gate_common as cwg  # noqa: E402
import code_work_gate_mark as marker_hook  # noqa: E402
import code_work_gate_stop as gate  # noqa: E402

PASSED = 0


def check(name, condition, detail=None):
    global PASSED
    if not condition:
        raise AssertionError("{}: {}".format(name, detail))
    PASSED += 1


def session():
    return "gate_test_{}".format(uuid.uuid4().hex)


def gate_paths(sid):
    key = cwg.session_key(sid)
    return cwg.marker_path(key), cwg.state_path(key)


def cleanup(sid, transcript=None):
    for path in gate_paths(sid):
        cwg.remove(path)
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


SIMPLIFY_LENSES = sorted(gate.SIMPLIFY_REVIEWERS)


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
    check("three-file standard requires simplify trio", result.get("decision") == "block", result)
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = base_events(include_simplify=True)
    simplify_wave(events, 128, "confirm", ["simplify-reuse-reviewer"])
    simplify_wave(events, 132, "third", ["simplify-reuse-reviewer"])
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check("a third pass of one lens is rejected", result.get("decision") == "block", result)
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
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = [skill_use(120, "development-verification", "skill-dev")]
    simplify_wave(events, 121, "lenses-first", SIMPLIFY_LENSES)
    events.append(skill_use(130, "simplify", "skill-after-lenses"))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check(
        "lenses that ran before the skill call still count",
        result.get("continue") is True and "decision" not in result,
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = base_events(include_simplify=True)
    simplify_wave(events, 128, "second", ["simplify-reuse-reviewer"])
    events.append(agent_use(132, "simplify-reuse-reviewer", "reuse-fail"))
    events.append(tool_result(132.5, "reuse-fail", "unavailable", is_error=True))
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check(
        "a lens that failed after its budget cannot be closed as verified",
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
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"], first_ts=200.0, last_ts=210.0)
    events = [skill_use(20.0, "development-verification", "skill-earlier")]
    simplify_wave(events, 21, "earlier-candidate", SIMPLIFY_LENSES)
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check(
        "lenses from an earlier candidate do not carry over",
        result.get("decision") == "block"
        and "no foreground result" in result.get("reason", ""),
        result,
    )
finally:
    cleanup(sid, locals().get("transcript"))

sid = session()
try:
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
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
    transcript = write_transcript(events)
    result = run(STOP_HOOK, {
        "session_id": sid,
        "transcript_path": transcript,
        "last_assistant_message": "[gate] verified: STANDARD; checks passed",
    })
    check(
        "three lenses spread across waves still complete the pass",
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
    check("three-file standard with trio passes", result.get("continue") is True and "decision" not in result, result)
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
    seed(sid, ["C:/repo/src/a.py", "C:/repo/src/b.py", "C:/repo/src/c.py"])
    events = [skill_use(120, "development-verification", "skill-dev")]
    events.append(skill_use(121, "simplify", "skill-simplify"))
    for index, subtype in enumerate(sorted({
        "simplify-reuse-reviewer",
        "simplify-efficiency-reviewer",
    })):
        call_id = "simplify-ok-{}".format(index)
        events.append(agent_use(122 + index, subtype, call_id))
        events.append(tool_result(122.5 + index, call_id, "No findings."))
    events.append(agent_use(
        125, "simplify-quality-reviewer", "simplify-quality-fail-1"
    ))
    events.append(tool_result(
        125.5, "simplify-quality-fail-1", "unavailable", is_error=True
    ))
    events.append(agent_use(
        127, "simplify-quality-reviewer", "simplify-quality-fail-2"
    ))
    events.append(tool_result(
        127.5, "simplify-quality-fail-2", "still unavailable", is_error=True
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
    check("high with trio and current approval passes", result.get("continue") is True and "decision" not in result, result)
finally:
    cleanup(sid, locals().get("transcript"))

HARNESS_TRAILER = (
    "\nagentId: acfcde3916804b008 (use SendMessage with to: 'acfcde3916804b008',"
    " summary: '<5-10 word recap>' to continue this agent)"
    "\n<usage>subagent_tokens: 45848\ntool_uses: 9\nduration_ms: 154586</usage>"
)

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
    ("duplicate verdict", "VERDICT: REVISE\nVERDICT: APPROVED"),
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
    "C:/Users/you/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py",
    "C:/tmp/sid/scratchpad/push.py",
    "/tmp/wipe.sh",
    "/var/tmp/rotate.py",
    "C:/Users/you/.claude/state/checkpoints/proj.md",
    "C:/Users/you/.claude/plans/plan.md",
):
    check("throwaway artifact is not gated: {}".format(path), not cwg.is_gated(path), path)
    check("throwaway artifact is not durable: {}".format(path), not cwg.durable_paths([path]), path)

for path in (
    "C:/tmp/demo-project/backend/src/services/featureRegistry.ts",
    "C:/Users/you/AppData/Local/Temp/build-clone/src/app.py",
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
    "C:/Users/you/AppData/Local/Temp/claude/proj/sid/scratchpad/probe.py",
    "/tmp/wipe.sh",
    "C:/tmp/sid/scratchpad/push.py",
):
    check("ephemeral matcher agrees with the gate: {}".format(path), cwg.is_ephemeral(path), path)

for path in (
    "C:/tmp/demo-project/backend/src/app.ts",
    "C:/repo/.claude/state-machine/runner.py",
    "C:/repo/src/scratchpadding.ts",
    "C:/repo/.claude/plans/rollout.md",
    "C:/repo/.claude/state/registry.json",
    "C:/backup/appdata/local/temp/keep.py",
):
    check("ephemeral matcher does not overreach: {}".format(path), not cwg.is_ephemeral(path), path)

for path in (
    "C:/Users/you/.claude/state/checkpoints/proj.md",
    "C:/Users/you/.claude/plans/plan.md",
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
    "an empty snapshot vouches only for its own repository",
    marker_hook.outside_snapshot(
        ["c:/users/you/.claude/hooks/gate.py"], "C:/repo"
    ) == ["c:/users/you/.claude/hooks/gate.py"],
)
check(
    "an empty snapshot vouches for paths under its root",
    marker_hook.outside_snapshot(["c:/repo/src/app.ts"], "C:/repo") == [],
)
check(
    "a sibling directory is not under the snapshot root",
    marker_hook.outside_snapshot(["c:/repo-two/src/app.ts"], "C:/repo") != [],
)
check(
    "throwaway paths never count as unvouched",
    marker_hook.outside_snapshot(["/tmp/probe.py", cwg.SHELL_MUTATION_PATH], "C:/repo") == [],
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

print("PASS: {} assertions".format(PASSED))
