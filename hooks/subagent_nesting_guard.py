"""
PreToolUse guard on Agent/Task: inside a subagent, only helper and gate lanes may start.

`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=2` gives a subagent a second layer for two purposes: a
lookup or translation on a helper profile, and the simplify and review lanes that let it hand
its work back already through the gate. Splitting its own task again is not one of them; that
stays with the parent. Claude Code ignores a type list in a subagent's `tools`, and a
`permissions.deny` rule would bind the main conversation as well, so the allowlist lives here.

A call is nested when the hook input carries `agent_id`, which the harness sets for a tool call
made inside a subagent and leaves out for the main conversation, `--agent` included. Fails open
on a malformed payload: the depth limit still holds.
"""
import json
import sys

import code_work_gate_common as cwg

HELPER_LANES = ("Explore", "translator", "claude-code-guide")
NESTED_LANES = frozenset(name.lower() for name in HELPER_LANES + cwg.GATE_LANES)
REASON = (
    "Inside a subagent only helper and gate lanes may start: {}. Do this part yourself, or "
    "report it in your result so the parent routes it."
).format(", ".join(HELPER_LANES + cwg.GATE_LANES))


def decision(data):
    """The deny reason for this call, or None when it may run."""
    if not isinstance(data, dict) or not data.get("agent_id"):
        return None
    # The registration's matcher already narrows to these; a wider one must not deny other tools.
    if data.get("tool_name") not in ("Agent", "Task"):
        return None
    payload = data.get("tool_input")
    subtype = str(payload.get("subagent_type") or "") if isinstance(payload, dict) else ""
    if subtype.strip().lower() in NESTED_LANES:
        return None
    return REASON


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        reason = decision(json.loads(sys.stdin.read()))
    except Exception:
        reason = None
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))


if __name__ == "__main__":
    main()
