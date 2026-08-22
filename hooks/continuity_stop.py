"""
Continuity — Stop hook (final output lint).

After the user has corrected the task, the finished answer must read as a clean first
version. This scans the last assistant message for the residue that survives a correction:
pointers at the previous version, apologies for the old interpretation, "now not X but Y".

Deliberately narrow. It runs only inside the correction's lint window (a few turns after the
correction, opened by the prompt hook), stands down on a turn where the user asked for
history, blocks at most once per turn and twice per correction episode, and accepts an
explicit `[lint] intentional: <why>` waiver on the final line. Prose only — fenced code and
quoted lines are skipped, because history inside a quote is the quote's.

"Once per turn" is enforced as once per generation, which is finer: an edit between two Stop
attempts in the same turn opens a fresh check, and the per-episode budget bounds the rest.

Fail-open: malformed input, an unpersisted budget spend, or any uncaught error lets the
session stop.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()

# Blocks allowed per correction episode. The prompt hook resets this on the next correction,
# so a long session never runs out of enforcement — and one episode can never wedge.
MAX_BLOCKS = 2

RESIDUE = [
    (r"вместо (?:предыдущ|прошл|прежн|стар|первоначальн)\w*", "points at the superseded version"),
    (r"как вы (?:и )?(?:просили|хотели|исправили|поправили|указали|уточнили|заметили)",
     "narrates the user's correction"),
    (r"по (?:вашему|вашей) (?:замечанию|правке|исправлению|уточнению)",
     "narrates the user's correction"),
    (r"в (?:прошлом|предыдущем|первом) (?:ответе|варианте|сообщении|черновике)",
     "refers to an earlier answer"),
    (r"(?:стар|прежн|предыдущ|первоначальн)\w* вариант", "keeps the rejected option in view"),
    (r"(?:ранее|раньше|изначально|сначала) я (?:писал|предлагал|сделал|думал|указал|использовал)",
     "recounts your earlier attempt"),
    (r"я (?:ошибся|был неправ|неправильно понял|перепутал)", "apologises for the old reading"),
    (r"теперь (?:не |больше не |уже не |вместо )", "frames the result as a change"),
    (r"\binstead of the (?:previous|earlier|old|original)", "points at the superseded version"),
    (r"\bas you (?:corrected|pointed out|noted|requested|asked)", "narrates the user's correction"),
    (r"\bin (?:my|the) (?:previous|earlier|last) (?:answer|response|version|draft)",
     "refers to an earlier answer"),
    (r"\bi was wrong (?:earlier|before)", "apologises for the old reading"),
    (r"\b(?:previously|earlier) i (?:wrote|suggested|assumed|said)", "recounts your earlier attempt"),
]
RESIDUE_RE = [(re.compile(p, re.I), label) for p, label in RESIDUE]

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
WAIVER_RE = re.compile(r"^\[lint\]\s*intentional\s*[:-]\s*(\S.*)$", re.I)

REASON = (
    "[Output lint] The task was corrected earlier this session, and the final message still carries "
    "edit history instead of a clean result:\n{findings}\n"
    "Rebuild the answer as a first version under the current requirements:\n"
    "- state what the result IS, not how it changed;\n"
    "- drop rejected options, superseded entities, and every pointer at what the earlier version "
    "said;\n"
    "- do not repeat a removed thing in order to say it is gone — describe what is there now;\n"
    "- keep only what a reader who never saw the earlier version needs.\n"
    "If the history genuinely is the deliverable — the user asked for a changelog, a diff, a "
    "migration, or an explanation of what went wrong — make the LAST line of your final message "
    "exactly:\n"
    "  [lint] intentional: <why the history has to stay>"
)


def fence_edge(line, fence):
    """
    (is_delimiter, fence_after_this_line). A block closes only on a matching delimiter of at
    least the opening length, so a shorter run inside a longer fence does not end it.
    """
    found = FENCE_RE.match(line)
    if not found:
        return False, fence
    mark = found.group(1)
    if fence is None:
        return True, (mark[0], len(mark))
    if mark[0] == fence[0] and len(mark) >= fence[1]:
        return True, None
    return True, fence


def prose_lines(message):
    """Body text only: fenced blocks and quoted lines carry someone else's history."""
    lines = []
    fence = None
    for line in str(message or "").splitlines():
        delimiter, fence = fence_edge(line, fence)
        if delimiter or fence or line.lstrip().startswith(">"):
            continue
        lines.append(cc.normalize(line))
    return lines


def scan(message):
    """Up to four distinct residue findings, each as (quoted fragment, what it is)."""
    findings = []
    seen = set()
    for line in prose_lines(message):
        for pattern, label in RESIDUE_RE:
            match = pattern.search(line)
            if not match or label in seen:
                continue
            fragment = line.strip()
            if len(fragment) > 100:
                start = max(0, match.start() - 20)
                fragment = "…" + fragment[start:start + 100].strip() + "…"
            findings.append((fragment, label))
            seen.add(label)
            if len(findings) >= 4:
                return findings
    return findings


def waived(message):
    """
    The waiver is only a waiver as the final word: a delimiter line after it, or anything
    inside a fence, leaves nothing standing.
    """
    last = ""
    fence = None
    for line in str(message or "").splitlines():
        delimiter, fence = fence_edge(line, fence)
        if delimiter:
            last = ""
        elif line.strip():
            last = "" if fence else line.strip()
    return bool(WAIVER_RE.match(last))


def armed(state):
    """
    Whether the lint applies to this answer at all. Everything here is scoped to the current
    correction episode — nothing latches for the rest of the session.
    """
    window = int(state.get("lint_until_turn") or 0)
    turns = int(state.get("turns") or 0)
    if window <= 0 or turns > window:
        return False
    if state.get("history_turn") == turns:
        return False
    if state.get("lint_blocked_gen") == int(state.get("generation") or 0):
        return False
    return int(state.get("lint_blocks") or 0) < MAX_BLOCKS


def main():
    data = cc.read_stdin_json()
    try:
        key = cc.session_key(data.get("session_id"))
        if not armed(cc.load_state(key)):
            cc.quiet()
            return

        message = data.get("last_assistant_message")
        if waived(message):
            cc.quiet()
            return
        findings = scan(message)
        if not findings:
            cc.quiet()
            return

        def spend(state):
            # Re-check under the lock: a concurrent turn may have closed the window.
            if not armed(state):
                return False
            state["lint_blocked_gen"] = int(state.get("generation") or 0)
            state["lint_blocks"] = int(state.get("lint_blocks") or 0) + 1
            return True

        persisted, spent = cc.update_state(key, spend)
        if not (persisted and spent):
            # The spent block did not reach disk, so the next Stop would see the same armed
            # state and block again — forever. Let the session stop instead.
            cc.quiet()
            return
        listed = "\n".join('  - "{}" — {}'.format(text, label) for text, label in findings)
        cc.emit({"decision": "block", "reason": REASON.format(findings=listed)})
    except Exception:
        cc.quiet()


if __name__ == "__main__":
    main()
