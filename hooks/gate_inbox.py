#!/usr/bin/env python3
"""Gate anomaly inbox.

Two kinds of record land in `state/gate-anomalies.jsonl`: reports an agent files when the Code
Work Gate's block contradicts facts it can verify in its transcript, and anomalies derived from
the gate's own ledger by fixed rules. The gate-ops session reads the unresolved ones on start.

  gate_inbox.py report --block "<block reason, verbatim>" --facts "<what the transcript shows>"
                       [--did "<what was done instead>"] [--kind <label>] [--session <id>]
                       [--transcript <path>] [--nonce <nonce from the block text>]
  gate_inbox.py list | show <id> | ack <id> [--note "<what fixed it>"] | scan | digest
  gate_inbox.py register [--title "<name>"]      # this session becomes the gate-ops session

Delivery is push, not pull: `report` prints the message to send to the registered gate-ops
session with the session-messaging tool, so the report starts a turn there while the reporting
session goes on with its work; the inbox file is the record and the fallback.

A report is evidence for the person who maintains the gate, never a key that opens it: the Stop
hook accepts `[gate] anomaly-reported: <id>; <fact>` only after it has blocked the candidate,
only for a report filed after that block and quoting its reason, and closes the candidate as
UNVERIFIED with the report attached.
"""
import argparse
import glob
import json
import math
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import code_work_gate_common as cwg  # noqa: E402

LEDGER_TAIL = 12
DIGEST_LIMIT = 10
# A verdict expired this soon after a review was stated is the pattern that cost a session a
# needless native round on 2026-09-03.
EXPIRY_WINDOW = 60.0
SUMMARY_CHARS = 100
RULE_ALIASES = {"verdict-expired-after-review": "verdict-expired-after-approval"}


def inbox_path():
    return os.path.join(cwg.config_home(), "state", "gate-anomalies.jsonl")


def registry_path():
    return os.path.join(cwg.config_home(), "state", "gate-ops-session.json")


def gate_ops():
    """The registered gate-ops session, or an empty dict."""
    data = cwg.read_json(registry_path())
    return data if isinstance(data, dict) and isinstance(data.get("session_id"), str) else {}


def delivery(record):
    """The message that carries a report to the gate-ops session, and how to send it."""
    ops = gate_ops()
    body = (
        "[gate anomaly] report {id} from session {session} ({cwd})\n"
        "block: {block}\n"
        "facts: {facts}\n"
        "did: {did}\n"
        "transcript: {transcript}\n"
        "show: python \"{inbox}\" show {id}"
    ).format(id=record["id"], session=record.get("session_id") or record["session"],
             cwd=record.get("cwd") or "?", block=record.get("block_reason") or "",
             facts=record.get("facts") or "", did=record.get("did") or "-",
             transcript=record.get("transcript") or "?",
             inbox=os.path.join(HERE, "gate_inbox.py"))
    if not ops:
        return body, ("No gate-ops session is registered (gate_inbox.py register there); the "
                      "report stays in the inbox for the next gate-ops session start.")
    how = (
        "Deliver it now, then continue your work: call mcp__ccd_session_mgmt__send_message with "
        "session_id \"{sid}\" and the message above (it arrives there as a turn). If that tool is "
        "not available, SendMessage to \"{name}\"; if neither is, the inbox holds the report."
    ).format(sid=ops["session_id"], name=ops.get("name") or ops["session_id"])
    return body, how


def read_jsonl(path):
    """The dict records of a JSONL file; a line that cannot be read, or a missing file, is skipped."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def number(value, default=0.0):
    """A finite float, or the default for anything a JSON line may have put there instead."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def read_records():
    """(reports, acks) from the inbox; a record whose id or ack is not a string is skipped."""
    reports, acks = [], {}
    for record in read_jsonl(inbox_path()):
        if isinstance(record.get("ack"), str):
            acks[record["ack"]] = record
        elif isinstance(record.get("id"), str) and isinstance(record.get("session"), str):
            reports.append(record)
    return reports, acks


def append(record):
    path = inbox_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def find_report(report_id, session_key=None):
    """The report with this id, filed by this session when a key is given."""
    for record in read_records()[0]:
        if record.get("id") == report_id and (session_key is None or record.get("session") == session_key):
            return record
    return None


def unresolved():
    reports, acks = read_records()
    return [record for record in reports if record["id"] not in acks]


def ledger_events():
    return list(read_jsonl(cwg.event_log_path()))


def find_transcript(session_id):
    hits = glob.glob(os.path.join(cwg.config_home(), "projects", "*", session_id + ".jsonl"))
    return max(hits, key=os.path.getmtime) if hits else ""


def hook_view(transcript, marker, key):
    """What the Stop hook's own scan sees in this transcript — the other side of the report.

    The scan is the hook's own code, run here for inspection only: its side effects — ledger
    lines, the Codex breaker — are switched off, so filing a report changes nothing but the inbox.
    """
    if not transcript:
        return {"available": False, "reason": "transcript not found"}
    try:
        import code_work_gate_stop as gate
        gate._SESSION.update(key=key, effects=False)
        first_ts = float(marker.get("first_ts") or marker.get("last_ts") or 0.0)
        evidence = gate.transcript_evidence(transcript, first_ts, skill_since=0.0)
        return {
            "available": True,
            "skills": {name: round(stamp) for name, stamp in evidence["skills"].items()},
            "simplify": {name: len(runs) for name, runs in evidence["simplify_successes"].items()},
            "simplify_failures": {name: len(runs) for name, runs in evidence["simplify_failures"].items()},
            "review_events": [(round(stamp), kind, value) for stamp, kind, value in evidence["review_events"][-6:]],
            "external_calls": len(evidence["external_calls"]),
            "external_results": [(round(stamp), status) for stamp, _, _, status in evidence["external_results"][-4:]],
            "in_flight": [task["id"] for task in gate.in_flight(evidence)],
            "background_done": len(evidence["background_done"]),
            "scan_failed": bool(evidence.get("scan_failed")),
        }
    except Exception as error:  # the report must land even when the hook's scan cannot run
        return {"available": False, "reason": type(error).__name__}


def report(args):
    session_id = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not session_id:
        print("GATE_ANOMALY: session id unknown; pass --session <id>", file=sys.stderr)
        return 2
    key = cwg.session_key(session_id)
    marker = cwg.read_json(cwg.marker_path(key)) or {}
    state = cwg.read_json(cwg.state_path(key)) or {}
    transcript = args.transcript or find_transcript(session_id)
    try:
        shape = cwg.candidate_shape(marker) if marker else None
    except Exception:
        shape = None
    record = {
        "id": uuid.uuid4().hex[:8],
        "ts": time.time(),
        "kind": args.kind,
        "session": key,
        "session_id": session_id,
        "cwd": os.getcwd(),
        "transcript": transcript,
        "block_reason": args.block.strip(),
        "block_nonce": (args.nonce or "").strip(),
        "facts": args.facts.strip(),
        "did": (args.did or "").strip(),
        "marker": {
            "first_ts": marker.get("first_ts"), "last_ts": marker.get("last_ts"),
            "last_durable_ts": marker.get("last_durable_ts"), "edits": marker.get("edits"),
            "minimum_risk_seen": marker.get("minimum_risk_seen"),
            "path_overflow": marker.get("path_overflow"),
            "unattributed_durable": marker.get("unattributed_durable"),
            "paths": len(marker.get("paths") or []), "shape": shape,
        },
        "state": {name: state.get(name) for name in (
            "blocks", "waits", "last_block_ts", "last_block_reason", "candidate_key")},
        "ledger": [event for event in ledger_events() if event.get("session") == key][-LEDGER_TAIL:],
        "hook_view": hook_view(transcript, marker, key),
    }
    append(record)
    body, how = delivery(record)
    print("GATE_ANOMALY: {}".format(record["id"]))
    print("--- message for the gate-ops session ---")
    print(body)
    print("--- delivery ---")
    print(how)
    print("Close with: [gate] anomaly-reported: {}; <the verifiable contradiction, one line>".format(record["id"]))
    return 0


def register(args):
    """Make the calling session the gate-ops session that reports are delivered to."""
    # The id the messaging tool takes is the desktop app's session id (`get_session self` →
    # sessionId), not the hook-side CLAUDE_CODE_SESSION_ID, so it has to be given explicitly.
    session_id = (args.session or "").strip()
    if not session_id:
        print("GATE_OPS: pass --session <sessionId from mcp__ccd_session_mgmt__get_session self>",
              file=sys.stderr)
        return 2
    record = {"session_id": session_id, "name": (args.name or "").strip(),
              "title": (args.title or "").strip(), "registered_at": time.time(),
              "cwd": os.getcwd()}
    if not cwg.write_json(registry_path(), record):
        print("GATE_OPS: could not write {}".format(registry_path()), file=sys.stderr)
        return 1
    print("GATE_OPS: reports will be delivered to session {}{}".format(
        session_id, " ({})".format(record["name"]) if record["name"] else ""))
    return 0


def when(stamp):
    """A timestamp as `MM-DD HH:MM`, or a blank for one the platform clock cannot render."""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(number(stamp)))
    except (OverflowError, OSError, ValueError):
        return "??-?? ??:??"


def summary(record):
    when_filed = when(record.get("ts"))
    text = str(record.get("block_reason") or record.get("rule") or "")
    return "{} {} {} {}: {}".format(
        record["id"], when_filed, str(record.get("kind") or "?"), record["session"][:8],
        text[:SUMMARY_CHARS],
    )


def list_reports(args):
    pending = unresolved()
    if not pending:
        print("GATE_INBOX: empty")
        return 0
    for record in pending:
        print(summary(record))
    return 0


def show(args):
    record = find_report(args.id)
    if record is None:
        print("GATE_INBOX: no report {}".format(args.id), file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def ack(args):
    if find_report(args.id) is None:
        print("GATE_INBOX: no report {}".format(args.id), file=sys.stderr)
        return 1
    append({"ack": args.id, "ts": time.time(), "note": (args.note or "").strip()})
    print("GATE_INBOX: acknowledged {}".format(args.id))
    return 0


def moment(event):
    """When an event happened: a review when the verdict was stated, anything else when written."""
    return number(event.get("at")) or number(event.get("ts"))


def scan(args):
    """Derive the anomalies the ledger shows on its own, each once.

    Events are judged in the order they happened, not the order the Stop hook wrote them: a
    verdict is only appended when a Stop runs, which may be after the edit that expired it.
    """
    import code_work_gate_stop as gate
    reports, _ = read_records()
    # A rule renamed between versions still names the same incident.
    seen = {(r.get("session"), RULE_ALIASES.get(r.get("rule"), r.get("rule")), r.get("event_at"))
            for r in reports if r.get("auto")}
    per_session = {}
    for event in ledger_events():
        per_session.setdefault(str(event.get("session") or "?"), []).append(event)
    added = 0
    for key, events in per_session.items():
        last_approval = None
        for event in sorted(events, key=moment):
            kind, stamp = event.get("kind"), moment(event)
            rule = None
            if kind == "exhausted":
                rule = "exhausted"
            elif kind == "hook_error":
                rule = "hook-error"
            elif kind == "wait" and number(event.get("waits")) >= gate.MAX_BACKGROUND_WAITS:
                rule = "waits-exhausted"
            elif kind == "review" and event.get("verdict"):
                last_approval = stamp if event.get("verdict") == "APPROVED" else None
            elif kind == "review" and event.get("engine") == "codex-background":
                rule = "unbound-background-review"
            elif kind == "durable" and last_approval and 0 <= stamp - last_approval <= EXPIRY_WINDOW:
                rule = "verdict-expired-after-approval"
            if not rule:
                continue
            # One incident, however many Stop runs re-read it: a review event keyed by the
            # moment it was stated or by its task, a state event by when it was written.
            event_at = event.get("at") or event.get("task") or event.get("ts")
            if (key, rule, event_at) in seen:
                continue
            seen.add((key, rule, event_at))
            append({
                "id": uuid.uuid4().hex[:8], "ts": time.time(), "auto": True,
                "kind": "auto:" + rule, "rule": rule, "event_at": event_at,
                "session": key, "event": event,
            })
            added += 1
    print("GATE_SCAN: {} new".format(added))
    return 0


def digest(args):
    """SessionStart hook: the unresolved reports, only for a session started in the config home."""
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cwd = os.path.abspath(str(payload.get("cwd") or os.getcwd()))
    home = os.path.abspath(cwg.config_home())
    if not (cwd == home or cwd.startswith(home + os.sep)):
        return 0
    pending = unresolved()
    if not pending:
        return 0
    lines = [
        "[Gate inbox] {} unresolved anomaly report(s). `python hooks/gate_inbox.py show <id>` for "
        "the report and the hook's own view; `ack <id> --note` when the cause is fixed.".format(len(pending))
    ]
    lines.extend("- " + summary(record) for record in pending[-DIGEST_LIMIT:])
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": str(payload.get("hook_event_name") or "SessionStart"),
        "additionalContext": "\n".join(lines),
    }}))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="gate_inbox.py")
    commands = parser.add_subparsers(dest="command", required=True)
    filing = commands.add_parser("report")
    filing.add_argument("--block", required=True, help="the block reason, verbatim")
    filing.add_argument("--facts", required=True, help="what the transcript verifiably shows")
    filing.add_argument("--did", default="", help="what was done instead of obeying")
    filing.add_argument("--kind", default="agent", help="a short label for the anomaly")
    filing.add_argument("--session", default="", help="session id (default: CLAUDE_CODE_SESSION_ID)")
    filing.add_argument("--transcript", default="", help="transcript path (default: found by session id)")
    filing.add_argument("--nonce", default="", help="the nonce printed in the block text")
    filing.set_defaults(run=report)
    commands.add_parser("list").set_defaults(run=list_reports)
    showing = commands.add_parser("show")
    showing.add_argument("id")
    showing.set_defaults(run=show)
    acking = commands.add_parser("ack")
    acking.add_argument("id")
    acking.add_argument("--note", default="")
    acking.set_defaults(run=ack)
    commands.add_parser("scan").set_defaults(run=scan)
    registering = commands.add_parser("register")
    registering.add_argument("--session", default="", help="the desktop app's sessionId of this session (get_session self)")
    registering.add_argument("--name", default="", help="the name ListAgents shows for this session")
    registering.add_argument("--title", default="", help="the session's title, for the record")
    registering.set_defaults(run=register)
    commands.add_parser("digest").set_defaults(run=digest)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    cwg.configure_utf8_streams()
    sys.exit(main(sys.argv[1:]))
