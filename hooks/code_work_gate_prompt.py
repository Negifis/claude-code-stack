"""
Code Work Gate - UserPromptSubmit reminder.

Of the 391 Stop-hook blocks recorded in August 2026, 114 were "terminal receipt is missing or
malformed": the agent ended a turn on an open candidate without the receipt it already knew
it owed, usually many turns and one compaction after the candidate opened. The Stop hook then
taught the contract after the fact, at the price of one more full-context turn.

This hook states the same fact before the turn instead: while a candidate is open in this
session, one line names its class, its risk floor and the receipt that closes it. It injects
nothing when no candidate is open, never blocks, and fails open.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_work_gate_common as cwg  # noqa: E402

cwg.configure_utf8_streams()


def describe(entry):
    """The one-line reminder for an open marker; `entry` has already passed `candidate_shape`."""
    shape = cwg.candidate_shape(entry)
    since = ", open since {}".format(
        datetime.datetime.fromtimestamp(shape["first_ts"]).strftime("%H:%M")
    )
    if not shape["persistent"]:
        return (
            "[gate] Open candidate: OPERATIONAL (shell mutation, no lasting artifact){}. "
            "When this work is finished, end the final message with "
            "`[gate] operational: <pre-execution check>; <verified effect>` or "
            "`[gate] no-change: <reason>`; a closing message without a receipt is blocked."
        ).format(since)
    floor = shape["floor"]
    files = shape["files"]
    return (
        "[gate] Open candidate: PERSISTENT, path floor {} ({} lasting file{}{}). Requires {}. "
        "End the final message with `[gate] verified: {}; <candidate and decisive checks>` "
        "(or pr-ready/draft-blocked after autonomous closure); a closing message without a "
        "receipt is blocked."
    ).format(floor, files, "" if files == 1 else "s", since, cwg.receipt_requirements(floor), floor)


def main():
    data = cwg.read_payload() or {}
    try:
        key = cwg.session_key(data.get("session_id"))
        entry = cwg.read_json(cwg.marker_path(key))
        if cwg.candidate_shape(entry) is None:
            print(json.dumps({"continue": True, "suppressOutput": True}))
            return
        print(json.dumps({
            "continue": True,
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": describe(entry),
            },
        }, ensure_ascii=False))
    except Exception:
        print(json.dumps({"continue": True, "suppressOutput": True}))


if __name__ == "__main__":
    main()
