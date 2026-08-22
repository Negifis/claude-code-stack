"""
Continuity — PreToolUse loop guard.

Counts identical calls inside one generation. A generation ends at every user turn and
every file edit, so the counter only ever sees repeats that changed nothing: same tool,
same arguments, same file mtime, no edit and no new instruction in between.

What happens on the third such call depends on what the tool reads:

- `Read`, `Grep`, `Glob` answer from the working tree, which cannot have moved — the call
  is denied, because two identical runs already proved this source is exhausted.
- `Bash` and `PowerShell` can legitimately observe something external (a job finishing, a
  log growing, a service coming up). Those get a warning instead of a denial, so a real
  wait is not mistaken for a loop.

Anything unexpected fails open.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()

LIMIT = 3
DENY_TOOLS = ("Read", "Grep", "Glob")
WARN_TOOLS = ("Bash", "PowerShell")
# Purely cosmetic argument — keeping it would let the same call repeat forever behind a
# reworded description. Everything else, `timeout` included, changes what the call does.
IGNORED_ARGS = ("description",)

DENIED = (
    "[Loop guard] This exact call already ran {n} times in the current step — same arguments, same "
    "file state, no edit and no new user input in between, so a repeat cannot return anything new.\n"
    "Do one of these instead:\n"
    "- change the question: another tool, another pattern, a wider or narrower scope, a different "
    "source (graph tools, tests, logs, docs);\n"
    "- act on what you already have — edit, run the acceptance check, or answer;\n"
    "- if the artifact really did change, read the thing that changed rather than this again.\n"
    "If nothing can move the task forward, stop and report the concrete blocker and what would "
    "unblock it. Do not repeat this call."
)

WARNED = (
    "[Loop guard] This command has now run {n} times in the current step with identical arguments, "
    "with no file edit and no new user input in between.\n"
    "If you are waiting on something external, name what you are waiting for and how you will know "
    "it changed — and prefer a check that reports that directly.\n"
    "Otherwise this is a loop: change the check, act on what you already have, or report the "
    "blocker and what would unblock it."
)


def fingerprint(tool, payload):
    """Same tool, same arguments, same file contents -> same fingerprint."""
    material = {k: v for k, v in (payload or {}).items() if k not in IGNORED_ARGS}
    try:
        canon = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canon = str(material)
    stamp = ""
    if tool == "Read":
        try:
            stamp = str(os.path.getmtime((payload or {}).get("file_path")))
        except Exception:
            stamp = ""
    raw = "{}|{}|{}".format(tool, canon, stamp).encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def main():
    data = cc.read_stdin_json()
    try:
        tool = data.get("tool_name")
        payload = data.get("tool_input")
        if tool not in DENY_TOOLS + WARN_TOOLS or not isinstance(payload, dict):
            cc.quiet()
            return
        mark = fingerprint(tool, payload)

        def apply(state):
            count = int(state["calls"].get(mark) or 0) + 1
            state["calls"][mark] = count
            if count >= LIMIT:
                state["loop_hits"] = int(state.get("loop_hits") or 0) + 1
            return count

        persisted, count = cc.update_state(cc.session_key(data.get("session_id")), apply)
        # An unrecorded count would repeat forever at the same value, so it cannot justify a
        # denial or a warning. Say nothing instead.
        if not persisted or not count or count < LIMIT:
            cc.quiet()
            return

        if tool in DENY_TOOLS:
            cc.emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": DENIED.format(n=count - 1),
                }
            })
            return
        # Warn on the third identical run and on every third one after it — enough to break
        # an unnoticed loop, quiet enough not to bury a legitimate wait.
        if count % LIMIT == 0:
            cc.emit_context("PreToolUse", WARNED.format(n=count))
            return
        cc.quiet()
    except Exception:
        cc.quiet()


if __name__ == "__main__":
    main()
