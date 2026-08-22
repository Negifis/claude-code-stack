"""
Continuity — UserPromptSubmit hook (intent update + loop reset + checkpoint nudge).

Every user turn opens a new generation, which clears the loop guard's call counters: the
user said something, so repeating a call is no longer a loop.

When the turn reads as a correction the hook injects the canonical-state contract and opens
a correction episode — a fresh output-lint window with a fresh block budget. A second
consecutive correction escalates to a clean-room rebuild, because patching the same result a
third time is the failure mode this system exists to stop.

Nothing here latches for the whole session: the lint window expires a few turns after the
last correction, and the "user asked for history" exemption is recomputed every turn.

Never blocks. Any failure degrades to a silent pass-through.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()

# The user rejecting what was produced: the old state is replaced, not extended.
# Bare task language ("не работает", "исправь") is deliberately absent — an opening bug
# report is a task, and treating it as a correction arms the lint on ordinary work.
STRONG = [
    r"\bя (?:же |ведь )?(?:просил|говорил|сказал|писал|уточнял)\b",
    r"\bя не (?:просил|говорил|это просил|об этом)\b",
    r"\bнет,\s*(?:не|это|я|давай)\b",
    r"\bне так\b", r"\bне то\b", r"\bне туда\b", r"\bне об этом\b",
    r"\bневерно\b", r"\bнеправильно\b",
    r"\bпеределай\b", r"\bотмени\b", r"\bоткати\b", r"\bверни как было\b",
    r"\bты не понял\b", r"\bты (?:же )?неправильно понял\b",
    r"\b(?:опять|снова|все ещё|все еще|по-прежнему) не\b",
    r"\bсколько (?:можно|раз)\b", r"\b(?:читай внимательно|перечитай)\b",
    r"\bэто не (?:то|так)\b", r"\bя просил не\b",
    r"\bnot what i (?:asked|wanted|said)\b", r"\bi (?:already )?said\b",
    r"\byou misunderstood\b", r"\bredo\b", r"\brevert (?:that|it)\b",
    r"\bthat'?s (?:wrong|not right)\b", r"\bstill (?:wrong|not|broken)\b",
]
# Requirements moving without a rejection: narrow, widen, or swap part of the target.
WEAK = [
    r"\bвместо\b", r"\bзамени\b", r"\bпоменяй\b", r"\bдавай не\b",
    r"\bлучше (?:сделай|используй|возьми|будет)\b",
    r"\bтеперь (?:нужно|надо|сделай|давай)\b",
    r"\bне нужно\b", r"\bубери\b",
    r"\binstead\b", r"\bchange it to\b", r"\bactually\b",
    r"\b(?:drop|remove) the\b",
]
# Turns where edit history IS the deliverable, so the output lint must stand down.
HISTORY = [
    r"changelog", r"чейнджлог", r"история (?:изменений|правок)", r"что изменилось",
    r"сравни(?:ть|)\s+верси", r"\bdiff\b", r"разбор (?:исправлени|правок|ошиб)",
    r"миграци", r"before\s*/?\s*after", r"до и после",
    r"почему\s+(?:ты\s+|это\s+|оно\s+)?.{0,20}(?:ошиб|неправ|не так|не сработ)",
    r"что пошло не так", r"\bwhat went wrong\b", r"\bwhy (?:was|were|did).{0,30}wrong\b",
]

STRONG_RE = re.compile("|".join(STRONG), re.I)
WEAK_RE = re.compile("|".join(WEAK), re.I)
HISTORY_RE = re.compile("|".join(HISTORY), re.I)

# How many further turns a correction keeps the output lint armed. Long enough to cover the
# answer the correction was about, short enough that an unrelated later turn is not linted.
LINT_WINDOW = 3
CHECKPOINT_EVERY = 10

CORRECTION = (
    "[Canonical state] That turn reads as a correction, not an addition. Before anything else:\n"
    "1) Classify it — replacement / addition / constraint / fact fix. If it is ambiguous, take the "
    "most likely reading from the wording and the last few turns and proceed; do not ask.\n"
    "2) Rewrite the canonical state: goal, authoritative requirements, confirmed facts, constraints, "
    "what is done and verified, current state, ONE next action, acceptance criteria, stop condition. "
    "The corrected requirement replaces the old one — the old one is gone, not demoted.\n"
    "3) Work only from the rewritten state. Do not defend, re-explain, or keep refining the "
    "interpretation the user just replaced, and do not re-explore what is already confirmed.\n"
    "4) The deliverable must read as a clean first version under the current requirements: no "
    "\"not X but Y\", no pointing at the previous version, no edit history — unless the user asked "
    "for a changelog, diff, or comparison.\n"
    "Full procedure: the canonical-state skill."
)

CLEAN_ROOM = (
    "[Clean-room rebuild] {n} corrections in a row on the same work — patching has now failed twice. "
    "Stop patching:\n"
    "- do not repeat the previous explanation and do not reuse the structure of the rejected result;\n"
    "- rebuild the affected deliverable from scratch from the current requirements, the confirmed "
    "facts, and only the files that matter;\n"
    "- check it against the acceptance criteria, then hand over the clean version alone.\n"
    "If these corrections are actually about different pieces of work, treat them separately and "
    "rebuild only the one that was rejected twice.\n"
    "Procedure: canonical-state skill -> clean-room-rebuild.md."
)

INTENT = (
    "[Intent update] The requirements moved. Update the canonical state before the next edit: what is "
    "superseded, what still holds, what the single next action is. The latest explicit instruction "
    "outranks your plan, earlier drafts, subagent conclusions, and the stored checkpoint."
)

CHECKPOINT = (
    "[Checkpoint] Long task in progress ({turns} turns, {edits} edits). Refresh the checkpoint at "
    "{path} — GOAL / STATUS / AUTHORITATIVE STATE / COMPLETED / CURRENT STATE / NEXT ACTION / "
    "ACCEPTANCE / BLOCKERS / STOP CONDITION — so a compaction or restart resumes from NEXT ACTION "
    "instead of re-exploring what is already settled."
)


def classify(prompt):
    """(strong, weak, history) for this turn. A slash command is a directive, not a message."""
    if prompt.strip().startswith("/"):
        return False, False, False
    return (bool(STRONG_RE.search(prompt)), bool(WEAK_RE.search(prompt)),
            bool(HISTORY_RE.search(prompt)))


def open_episode(state):
    """Start a correction episode: fresh lint window, fresh block budget."""
    state["episode"] = int(state.get("episode") or 0) + 1
    state["lint_blocks"] = 0
    state["lint_blocked_gen"] = None


def main():
    data = cc.read_stdin_json()
    try:
        prompt = cc.normalize(data.get("prompt") or data.get("user_input"))
        strong, weak, history = classify(prompt)

        def apply(state):
            cc.bump_generation(state)
            turns = state["turns"] = int(state.get("turns") or 0) + 1
            # Recomputed every turn: an exemption must not outlive the turn that earned it.
            if history:
                state["history_turn"] = turns
            if strong:
                state["corrections"] = int(state.get("corrections") or 0) + 1
                state["streak"] = int(state.get("streak") or 0) + 1
                open_episode(state)
                state["lint_until_turn"] = turns + LINT_WINDOW
            else:
                state["streak"] = 0
                if weak:
                    # A requirement change opens its own episode too: it deserves a fresh
                    # budget rather than inheriting one an earlier episode spent.
                    open_episode(state)
                    state["lint_until_turn"] = max(
                        int(state.get("lint_until_turn") or 0), turns + LINT_WINDOW)
            return turns, int(state.get("streak") or 0), int(state.get("edits") or 0)

        key = cc.session_key(data.get("session_id"))
        _, outcome = cc.update_state(key, apply)
        if outcome is None:
            cc.quiet()
            return
        turns, streak, edits = outcome

        parts = []
        if strong:
            parts.append(CORRECTION)
            if streak >= 2:
                parts.append(CLEAN_ROOM.format(n=streak))
        elif weak:
            parts.append(INTENT)

        if turns % CHECKPOINT_EVERY == 0:
            cwd = data.get("cwd") or os.getcwd()
            found = cc.find_checkpoint(cwd)
            parts.append(CHECKPOINT.format(
                turns=turns, edits=edits,
                path=found["path"] if found else cc.global_checkpoint_path(cwd),
            ))

        if not parts:
            cc.quiet()
            return
        cc.emit_context("UserPromptSubmit", "\n\n".join(parts))
    except Exception:
        cc.quiet()


if __name__ == "__main__":
    main()
