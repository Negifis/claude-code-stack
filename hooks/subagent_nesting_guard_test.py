import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "subagent_nesting_guard.py")
sys.path.insert(0, HERE)

import code_work_gate_common as cwg  # noqa: E402
import code_work_gate_stop as gate  # noqa: E402
import subagent_nesting_guard as guard  # noqa: E402


def run(payload):
    """(decision, reason) the hook prints for one payload: "allow" when it prints nothing."""
    proc = subprocess.run([sys.executable, HOOK], input=payload if isinstance(payload, str)
                          else json.dumps(payload), text=True, encoding="utf-8",
                          capture_output=True, check=False)
    if proc.returncode != 0:
        return "exit {}".format(proc.returncode), proc.stderr.strip()
    if not proc.stdout.strip():
        return "allow", ""
    output = json.loads(proc.stdout)["hookSpecificOutput"]
    return output["permissionDecision"], output["permissionDecisionReason"]


def call(subtype=None, agent_id="a1f0c", tool="Agent"):
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"prompt": "bounded packet", "description": "lane"}}
    if subtype is not None:
        payload["tool_input"]["subagent_type"] = subtype
    if agent_id:
        payload["agent_id"] = agent_id
        payload["agent_type"] = "general-purpose"
    return payload


CASES = [
    ("main thread, any lane", call("general-purpose", agent_id=None), "allow"),
    ("main thread, --agent session", dict(call("general-purpose", agent_id=None),
                                          agent_type="coordinator"), "allow"),
    ("nested Explore", call("Explore"), "allow"),
    ("nested translator", call("translator"), "allow"),
    ("nested docs lookup", call("claude-code-guide"), "allow"),
    ("nested simplify lane", call("simplify-reviewer"), "allow"),
    ("nested XHIGH lens", call("simplify-efficiency-reviewer-xhigh"), "allow"),
    ("nested review lane", call("adversarial-reviewer"), "allow"),
    ("nested review, older tool name", call("adversarial-reviewer-xhigh", tool="Task"), "allow"),
    ("nested lane, other case", call("explore"), "allow"),
    ("nested lane, padded", call(" Explore "), "allow"),
    ("nested call with no input object", dict(call(), tool_input="Explore"), "deny"),
    ("nested general-purpose", call("general-purpose"), "deny"),
    ("nested plugin agent", call("python-development:python-pro"), "deny"),
    ("nested planner", call("Plan"), "deny"),
    ("nested call naming no type", call(), "deny"),
    ("nested blank type", call("  "), "deny"),
    ("a tool other than Agent", call("general-purpose", tool="Bash"), "allow"),
    ("malformed payload", "{not json", "allow"),
    ("payload that is no object", "[1, 2]", "allow"),
]


def main():
    ok = True
    for name, payload, want in CASES:
        got, reason = run(payload)
        flag = "OK " if got == want else "FAIL"
        ok = ok and got == want
        print("{} {:34} want={:5} got={}".format(flag, name, want, got))
        if got == "deny" and not all(lane in reason for lane in guard.HELPER_LANES + cwg.GATE_LANES):
            ok = False
            print("FAIL the deny reason names every lane that may start: {}".format(reason))
    # Every lane the Stop hook accepts as gate evidence has to be one a subagent may start, or a
    # subagent could never hand back work that is already through the gate.
    gate_lanes = set(gate.SIMPLIFY_REVIEWERS) | {cwg.REVIEWER, cwg.XHIGH_REVIEWER}
    missing = sorted(lane for lane in gate_lanes if lane.lower() not in guard.NESTED_LANES)
    ok = ok and not missing
    print("{} every gate lane may start nested {}".format("OK " if not missing else "FAIL", missing))
    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
