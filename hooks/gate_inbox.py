#!/usr/bin/env python3
"""Gate anomaly inbox.

Two kinds of record land in `state/gate-anomalies.jsonl`: reports an agent files when the Code
Work Gate's block contradicts facts it can verify in its transcript, and anomalies derived from
the gate's own ledger by fixed rules. The gate-ops session reads the unresolved ones on start.

  gate_inbox.py report --block "<block reason, verbatim>" --facts "<what the transcript shows>"
                       [--did "<what was done instead>"] [--kind <label>] [--session <id>]
                       [--transcript <path>]
  gate_inbox.py list | show <id> | ack <id> [--note "<what fixed it>"] | scan | digest

A report is evidence for the person who maintains the gate, never a key that opens it: the Stop
hook accepts `[gate] anomaly-reported: <id>; <fact>` only after it has blocked the candidate,
only for a report filed after that block and quoting its reason, and closes the candidate as
UNVERIFIED with the report attached.
"""
import argparse
import glob
import json
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


def inbox_path():
    return os.path.join(cwg.config_home(), "state", "gate-anomalies.jsonl")


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


def read_records():
    """(reports, acks) from the inbox."""
    reports, acks = [], {}
    for record in read_jsonl(inbox_path()):
        if record.get("ack"):
            acks[str(record["ack"])] = record
        elif record.get("id"):
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

    The scan is the hook's own code, run here for inspection only: its ledger lines are switched
    off so filing a report writes nothing the Stop hook would not have written itself.
    """
    if not transcript:
        return {"available": False, "reason": "transcript not found"}
    try:
        import code_work_gate_stop as gate
        gate._SESSION.update(key=key, ledger=False)
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
    print("GATE_ANOMALY: {}".format(record["id"]))
    print("Close with: [gate] anomaly-reported: {}; <the verifiable contradiction, one line>".format(record["id"]))
    return 0


def summary(record):
    when = time.strftime("%m-%d %H:%M", time.localtime(float(record.get("ts") or 0)))
    text = record.get("block_reason") or record.get("rule") or ""
    return "{} {} {} {}: {}".format(
        record["id"], when, record.get("kind") or "?", str(record.get("session") or "")[:8],
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


def scan(args):
    """Derive the anomalies the ledger shows on its own, each once."""
    import code_work_gate_stop as gate
    reports, _ = read_records()
    seen = {(r.get("session"), r.get("rule"), r.get("event_at")) for r in reports if r.get("auto")}
    per_session = {}
    for event in ledger_events():
        per_session.setdefault(event.get("session") or "?", []).append(event)
    added = 0
    for key, events in per_session.items():
        last_review = None
        for event in events:
            kind, stamp = event.get("kind"), event.get("ts")
            rule = None
            if kind == "exhausted":
                rule = "exhausted"
            elif kind == "hook_error":
                rule = "hook-error"
            elif kind == "wait" and int(event.get("waits") or 0) >= gate.MAX_BACKGROUND_WAITS:
                rule = "waits-exhausted"
            elif kind == "review" and event.get("verdict"):
                last_review = float(event.get("at") or stamp or 0)
            elif kind == "review" and event.get("engine") == "codex-background":
                rule = "unbound-background-review"
            elif (kind == "durable" and event.get("reason") == "unresolved-write-capable"
                  and last_review and 0 <= float(stamp or 0) - last_review <= EXPIRY_WINDOW):
                rule = "verdict-expired-after-review"
            if not rule:
                continue
            # One incident, however many Stop runs re-read it: a review event keyed by the
            # moment it was stated or by its task, a state event by when it was written.
            event_at = event.get("at") or event.get("task") or stamp
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
    commands.add_parser("digest").set_defaults(run=digest)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    cwg.configure_utf8_streams()
    sys.exit(main(sys.argv[1:]))
