"""
Continuity — self-test.

Drives every continuity hook as a real subprocess and asserts on what it returned. Covers
the seven scenarios the system was built for, the false positives that would make it worse
than nothing, the checkpoint trust rules, and the concurrency the hooks actually run under.
Run it after changing any hook or any pattern:

    python ~/.claude/hooks/continuity_selftest.py

Exit code 0 = everything passed. Session state and temporary checkpoints are cleaned up on
the way out; nothing outside the temp dir and the test's own checkpoint file is touched.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

HOOKS = os.path.dirname(os.path.abspath(__file__))
CWD = os.path.join(tempfile.gettempdir(), "continuity-selftest-project")
RESULTS = []
SESSIONS = set()


def run(hook, payload):
    """Call a hook the way Claude Code does and return its parsed JSON answer."""
    proc = subprocess.run(
        [sys.executable, os.path.join(HOOKS, hook)],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
    )
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {"_unparsed": proc.stdout, "_stderr": proc.stderr}


def context_of(answer):
    return str((answer.get("hookSpecificOutput") or {}).get("additionalContext") or "")


def denied(answer):
    return (answer.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


def blocked(answer):
    return answer.get("decision") == "block"


def check(scenario, condition, detail):
    RESULTS.append((scenario, bool(condition), detail))


def prompt(sid, text):
    return run("continuity_prompt.py", {"session_id": sid, "cwd": CWD, "prompt": text})


def stop(sid, message):
    return run("continuity_stop.py",
               {"session_id": sid, "cwd": CWD, "last_assistant_message": message})


def start(source, cwd=CWD, sid="selftest-start"):
    return context_of(run("continuity_session_start.py",
                          {"session_id": sid, "cwd": cwd, "source": source}))


def forget(sid):
    """Start a scenario from empty session state, and remember the id for cleanup."""
    SESSIONS.add(sid)
    cc.write_json(cc.state_path(cc.session_key(sid)), {})
    return sid


def armed(sid):
    """
    A fresh session that has just been corrected — the only state the output lint runs in.
    Each lint assertion needs its own: the lint blocks at most once per generation, so
    reusing a session would let the next assertion pass without being tested.
    """
    forget(sid)
    prompt(sid, "нет, не так")
    return sid


def write_checkpoint(body, cwd=CWD, encoding="utf-8"):
    path = cc.global_checkpoint_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body.encode(encoding))
    return path


def drop_checkpoint(cwd=CWD):
    try:
        os.remove(cc.global_checkpoint_path(cwd))
    except OSError:
        pass


LIVE_CHECKPOINT = (
    "GOAL\nShip the importer.\n\nSTATUS\nactive\n\n"
    "NEXT ACTION\nWire the CSV reader to the queue.\n"
)

# Takes the OS lock on a path and holds it for N seconds, announcing when it is held.
HOLDER = """
import sys, time
try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
handle = open(sys.argv[1], 'a+b')
handle.seek(0)
if msvcrt is not None:
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
elif fcntl is not None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print('locked', flush=True)
time.sleep(float(sys.argv[2]))
"""


# 1. Entity replacement: the finished answer must not carry the replaced entity.
def scenario_entity_replacement():
    dirty = stop(armed("selftest-1a"), "Готово. Теперь не Postgres, а SQLite — как вы просили.")
    clean = stop(armed("selftest-1b"), "Хранилище — SQLite. Схема создаётся при первом запуске.")
    check("1 entity replacement", blocked(dirty), "residue answer is blocked")
    check("1 entity replacement", not blocked(clean), "clean answer passes")


# 2. Direction change: a correction rewrites the state, a softer change updates intent.
def scenario_direction_change():
    strong = context_of(prompt(forget("selftest-2a"),
                               "стоп, это не то — переделай под другой подход"))
    weak = context_of(prompt(forget("selftest-2b"), "давай вместо этого возьмём другой парсер"))
    neutral = context_of(prompt("selftest-2b", "добавь тест на пустой ввод"))
    check("2 direction change", "[Canonical state]" in strong, "correction injects the contract")
    check("2 direction change", "Rewrite the canonical state" in strong, "state rewrite demanded")
    check("2 direction change", "[Intent update]" in weak, "requirement change injects intent update")
    check("2 direction change", not neutral, "an ordinary request injects nothing")


# 3. Repeated failure: the second correction in a row escalates to a clean-room rebuild.
def scenario_repeated_failure():
    sid = forget("selftest-3")
    first = context_of(prompt(sid, "нет, не так"))
    second = context_of(prompt(sid, "опять не то, я же просил иначе"))
    check("3 repeated failure", "[Clean-room rebuild]" not in first, "first correction only patches")
    check("3 repeated failure", "[Clean-room rebuild]" in second, "second escalates to rebuild")
    check("3 repeated failure", "do not reuse the structure" in second, "old structure is barred")


# 4. Loop guard: deterministic reads are denied, shell repeats are warned, edits reopen.
def scenario_loop_guard():
    sid = forget("selftest-4")
    target = os.path.join(CWD, "sample.txt")
    read = {"session_id": sid, "cwd": CWD, "tool_name": "Read", "tool_input": {"file_path": target}}
    first, second, third = (run("continuity_loop_guard.py", read) for _ in range(3))
    run("continuity_progress.py", {"session_id": sid, "cwd": CWD,
                                   "tool_input": {"file_path": target}})
    after_edit = run("continuity_loop_guard.py", read)
    other = run("continuity_loop_guard.py", dict(read, tool_input={"file_path": target + "2"}))
    check("4 loop guard", not denied(first) and not denied(second), "two identical reads allowed")
    check("4 loop guard", denied(third), "third identical read denied")
    check("4 loop guard", not denied(after_edit), "an edit reopens the loop counter")
    check("4 loop guard", not denied(other), "a different call is unaffected")

    sid = forget("selftest-4b")
    shell = {"session_id": sid, "cwd": CWD, "tool_name": "Bash",
             "tool_input": {"command": "gh run view 42", "description": "check CI"}}
    run("continuity_loop_guard.py", shell)
    run("continuity_loop_guard.py", shell)
    warned = run("continuity_loop_guard.py", shell)
    check("4 loop guard", not denied(warned), "a repeated shell call is never denied")
    check("4 loop guard", "[Loop guard]" in context_of(warned), "third shell repeat is warned")
    run("continuity_loop_guard.py",
        dict(shell, tool_input={"command": "gh run view 42", "description": "again"}))
    counts = list((cc.load_state(cc.session_key(sid)).get("calls") or {}).values())
    check("4 loop guard", counts == [4],
          "rewording the description keeps one counter (got {})".format(counts))

    sid = forget("selftest-4c")
    for seconds in (1000, 30000, 300000):
        answer = run("continuity_loop_guard.py",
                     {"session_id": sid, "cwd": CWD, "tool_name": "Bash",
                      "tool_input": {"command": "npm test", "timeout": seconds}})
    check("4 loop guard", not denied(answer) and not context_of(answer),
          "a different timeout is a different call")


# 5. Compaction recovery: the checkpoint comes back and points at NEXT ACTION.
def scenario_compaction_recovery():
    write_checkpoint(LIVE_CHECKPOINT)
    try:
        context = start("compact")
        check("5 compaction recovery", "Wire the CSV reader" in context, "checkpoint body restored")
        check("5 compaction recovery", "Resume from NEXT ACTION" in context, "resume point named")
        check("5 compaction recovery", "do not re-explore" in context, "re-exploration ruled out")
    finally:
        drop_checkpoint()
    missing = start("compact", cwd=CWD + "-empty")
    check("5 compaction recovery", "Rebuild the canonical state" in missing,
          "no checkpoint after compaction -> rebuild the state")


# 6. Delegated lane: the packet is authoritative and contradictions come back, not assumptions.
def scenario_subagent_contract():
    context = context_of(run("continuity_subagent.py",
                             {"session_id": "selftest-6", "cwd": CWD,
                              "agent_type": "general-purpose"}))
    check("6 subagent contract", "authoritative state" in context, "packet declared authoritative")
    check("6 subagent contract", "report the contradiction" in context, "contradictions surface")
    check("6 subagent contract", "evidence, not the final answer" in context,
          "output framed as evidence")

    # The contract must survive a checkpoint being present — that path used to throw and take
    # the whole injection down with it.
    write_checkpoint(LIVE_CHECKPOINT)
    try:
        with_checkpoint = context_of(run("continuity_subagent.py",
                                         {"session_id": "selftest-6b", "cwd": CWD,
                                          "agent_type": "general-purpose"}))
        check("6 subagent contract", "[Delegated lane contract]" in with_checkpoint,
              "the contract still arrives when a checkpoint exists")
        check("6 subagent contract", cc.global_checkpoint_path(CWD) in with_checkpoint,
              "the checkpoint is named as background")
    finally:
        drop_checkpoint()


# 7. Clean final text: residue blocks, code blocks do not, waiver and history requests stand down.
def scenario_clean_output():
    check("7 clean output",
          blocked(stop(armed("selftest-7a"), "Раньше я предлагал очередь, сейчас всё проще.")),
          "narrated earlier attempt is blocked")
    check("7 clean output",
          not blocked(stop(armed("selftest-7b"),
                           "Готово.\n\n```text\nтеперь не A, а B\n```\n\nМодуль собран.")),
          "residue inside a fenced block is ignored")
    check("7 clean output",
          not blocked(stop(armed("selftest-7c"),
                           "Вместо предыдущего варианта используется кэш.\n"
                           "[lint] intentional: пользователь просил разбор правок")),
          "explicit waiver stands down")

    sid = armed("selftest-7d")
    prompt(sid, "покажи changelog по этим правкам")
    check("7 clean output", not blocked(stop(sid, "Теперь не A, а B — как вы просили.")),
          "a requested changelog exempts that turn")
    check("7 clean output",
          blocked(stop(armed("selftest-7e"), "Раньше я предлагал очередь — теперь не так.")),
          "the exemption does not carry into the next correction")


# 8. The guards stay quiet on ordinary work — a false block costs more than a missed one.
def scenario_false_positives():
    sid = forget("selftest-8a")
    task = context_of(prompt(sid, "исправь падение парсера на пустом вводе"))
    check("8 false positives", not task, "an ordinary fix request injects nothing")
    check("8 false positives", not blocked(stop(sid, "Готово, тесты проходят.")),
          "no lint without a correction")
    check("8 false positives",
          not context_of(prompt(forget("selftest-8b"), "не работает авторизация, исправь")),
          "an opening bug report is not a correction")
    check("8 false positives",
          not blocked(stop(armed("selftest-8c"),
                           "Причина: кэш возвращал устаревший ключ, а не конфиг. "
                           "Исправлено в loader.py.")),
          "a root-cause explanation is not edit history")
    check("8 false positives",
          not blocked(stop(armed("selftest-8d"),
                           "Флаг legacy_mode больше не читается сборкой — он удалён из конфига.")),
          "describing current code is not edit history")

    sid = forget("selftest-8e")
    prompt(sid, "ты неправильно понял. объясни, почему ты ошибся")
    check("8 false positives", not blocked(stop(sid, "Я ошибся, потому что взял старый конфиг.")),
          "a requested post-mortem is allowed")

    sid = armed("selftest-8f")
    for _ in range(4):
        prompt(sid, "продолжай")
    check("8 false positives", not blocked(stop(sid, "Раньше я предлагал очередь.")),
          "the lint window closes a few turns after the correction")

    # A spent episode must not disarm the next requirement change.
    sid = armed("selftest-8g")
    stop(sid, "Раньше я предлагал очередь.")
    prompt(sid, "ещё раз: нет, не так")
    stop(sid, "Раньше я предлагал очередь.")
    prompt(sid, "вместо очереди используй пул")
    check("8 false positives", blocked(stop(sid, "Вместо предыдущего варианта — пул.")),
          "a requirement change gets a fresh block budget")


# 9. Checkpoint trust: only a live checkpoint on a continuing session is authoritative.
def scenario_checkpoint_trust():
    write_checkpoint(LIVE_CHECKPOINT)
    try:
        check("9 checkpoint trust", "unverified lead" in start("startup"),
              "a fresh startup gets a lead, not current state")
        check("9 checkpoint trust", "not restored" in start("clear"),
              "clear does not restore the checkpoint")
        check("9 checkpoint trust", "Resume from NEXT ACTION" in start("resume"),
              "resume restores it as authoritative")
    finally:
        drop_checkpoint()

    write_checkpoint("GOAL\nShip it.\n\nSTATUS\ndone\n\nNEXT ACTION\nnothing\n")
    try:
        check("9 checkpoint trust", "marked done" in start("compact"),
              "a finished checkpoint is not restored as state")
    finally:
        drop_checkpoint()

    # A fresh start gets the head of the checkpoint only; a continuing session gets it whole.
    write_checkpoint(LIVE_CHECKPOINT + "\nCOMPLETED\nParsed the manifest, verified by the unit suite.\n")
    try:
        fresh = start("startup")
        check("9 checkpoint trust", "Ship the importer." in fresh and "Wire the CSV reader" in fresh,
              "a fresh startup still sees GOAL and NEXT ACTION")
        check("9 checkpoint trust", "Parsed the manifest" not in fresh and "Only GOAL, STATUS and NEXT ACTION" in fresh,
              "a fresh startup does not get the body of an unrelated task's checkpoint")
        check("9 checkpoint trust", "Parsed the manifest" in start("resume"),
              "a resumed session gets the whole checkpoint")
    finally:
        drop_checkpoint()

    write_checkpoint("GOAL\nShip it.\n\nSTATUS\nactive\n\nNEXT ACTION\n\n"
                     "ACCEPTANCE\nThe importer runs.\n\nBLOCKERS\nnone\n")
    try:
        check("9 checkpoint trust", "no NEXT ACTION recorded" in start("compact"),
              "an empty NEXT ACTION is not filled in by the next section")
    finally:
        drop_checkpoint()

    write_checkpoint("GOAL\nShip it.\n\nSTATUS\nactive\n\nNEXT ACTION: Wire the reader.\n")
    try:
        check("9 checkpoint trust", "Resume from NEXT ACTION" in start("compact"),
              "an inline NEXT ACTION value is recognised")
    finally:
        drop_checkpoint()

    write_checkpoint("GOAL\nShip it.\n\nNEXT ACTION\nGo.\n", encoding="utf-16")
    try:
        check("9 checkpoint trust", "not valid UTF-8" in start("compact"),
              "an undecodable checkpoint is reported, not injected")
    finally:
        drop_checkpoint()

    path = write_checkpoint(LIVE_CHECKPOINT)
    old = time.time() - (cc.STALE_AFTER_DAYS + 5) * 86400
    os.utime(path, (old, old))
    try:
        check("9 checkpoint trust", "past the staleness horizon" in start("compact"),
              "a checkpoint past the staleness horizon is only a lead")
    finally:
        drop_checkpoint()


# 9b. A project checkpoint only wins while it is usable.
def scenario_project_checkpoint():
    project = os.path.join(CWD, cc.PROJECT_CHECKPOINT)
    os.makedirs(os.path.dirname(project), exist_ok=True)
    write_checkpoint(LIVE_CHECKPOINT)
    try:
        with open(project, "w", encoding="utf-8") as f:
            f.write("GOAL\nOld task.\n\nSTATUS\ndone\n\nNEXT ACTION\nnothing\n")
        context = start("compact")
        check("9b project checkpoint", "Wire the CSV reader" in context,
              "a finished project file does not hide the live global one")
        check("9b project checkpoint", "was not used" in context,
              "the checkpoint that lost is named")

        with open(project, "w", encoding="utf-8") as f:
            f.write("GOAL\nCurrent task.\n\nSTATUS\nactive\n\nNEXT ACTION\nRun the migration.\n")
        check("9b project checkpoint", "Run the migration" in start("compact"),
              "a usable project file wins")

        with open(project, "w", encoding="utf-8") as f:
            f.write("GOAL\nHuge task.\n\nSTATUS\nactive\n\nNEXT ACTION\nRun the migration.\n"
                    + "CURRENT STATE\n" + ("x" * (cc.MAX_INJECT_CHARS + 100)))
        check("9b project checkpoint", "Wire the CSV reader" in start("compact"),
              "an oversized project file does not shadow a complete global one")
    finally:
        try:
            os.remove(project)
        except OSError:
            pass
        drop_checkpoint()


# 10. Concurrency: hooks on one event run in parallel and must not overwrite each other.
def scenario_concurrency():
    sid = forget("selftest-10")
    calls = [{"session_id": sid, "cwd": CWD, "tool_name": "Grep",
              "tool_input": {"pattern": "sym{}".format(i)}} for i in range(16)]
    procs = [subprocess.Popen([sys.executable, os.path.join(HOOKS, "continuity_loop_guard.py")],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
             for _ in calls]
    for proc, payload in zip(procs, calls):
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        proc.stdin.close()
    for proc in procs:
        proc.wait()
    kept = len(cc.load_state(cc.session_key(sid)).get("calls") or {})
    check("10 concurrency", kept == len(calls),
          "all {} parallel updates survived (kept {})".format(len(calls), kept))


# 11. Broken plumbing must never turn into a permanent block or a phantom denial.
def scenario_resilience():
    sid = armed("selftest-11a")
    path = cc.state_path(cc.session_key(sid))
    os.chmod(path, stat.S_IREAD)
    try:
        check("11 resilience",
              not blocked(stop(sid, "Раньше я предлагал очередь, сейчас всё проще.")),
              "a state write that cannot land does not block the stop")
    finally:
        os.chmod(path, stat.S_IWRITE)

    sid = forget("selftest-11b")
    lock = cc.state_path(cc.session_key(sid)) + ".lock"
    # Held for far longer than the checks need: releasing early would silently turn this
    # into a test of the uncontended path.
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, lock, "60"],
                              stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()  # wait until the lock is actually held
        call = {"session_id": sid, "cwd": CWD, "tool_name": "Read",
                "tool_input": {"file_path": os.path.join(CWD, "sample.txt")}}
        answers = [run("continuity_loop_guard.py", call) for _ in range(3)]
        check("11 resilience", not any(denied(a) for a in answers),
              "a lock held by another process suppresses the guard instead of guessing")
        check("11 resilience", not (cc.load_state(cc.session_key(sid)).get("calls") or {}),
              "nothing was written without the lock")
    finally:
        holder.terminate()
        holder.wait()

    # A marker file left behind by a process that died carries no ownership: the OS released
    # the lock with the process, so the next hook takes it immediately.
    sid = forget("selftest-11c")
    with open(cc.state_path(cc.session_key(sid)) + ".lock", "wb") as f:
        f.write(b"leftover")
    run("continuity_loop_guard.py", {"session_id": sid, "cwd": CWD, "tool_name": "Grep",
                                     "tool_input": {"pattern": "after-dead-holder"}})
    check("11 resilience", (cc.load_state(cc.session_key(sid)).get("calls") or {}),
          "an orphaned lock file does not block work")


# 11b. The checkpoint CLI is what /checkpoint calls first; it must never throw.
def scenario_checkpoint_cli():
    def cli(*args):
        return subprocess.run(
            [sys.executable, os.path.join(HOOKS, "continuity_checkpoint.py"), *args,
             "--cwd", CWD],
            capture_output=True, text=True, encoding="utf-8")

    empty = cli("path")
    check("11b checkpoint cli", empty.returncode == 0 and not empty.stderr.strip(),
          "path works with no checkpoint present")
    check("11b checkpoint cli", empty.stdout.strip() == cc.global_checkpoint_path(CWD),
          "it names the file to write")

    write_checkpoint(LIVE_CHECKPOINT)
    try:
        live = cli("path")
        shown = cli("show")
        check("11b checkpoint cli", live.returncode == 0 and not live.stderr.strip(),
              "path works with a checkpoint present")
        check("11b checkpoint cli", live.stdout.strip() == cc.global_checkpoint_path(CWD),
              "it names the existing file")
        check("11b checkpoint cli", "Wire the CSV reader" in shown.stdout,
              "show prints the checkpoint")
    finally:
        drop_checkpoint()

    template = cli("template")
    check("11b checkpoint cli",
          template.returncode == 0 and "NEXT ACTION" in template.stdout,
          "template prints the empty form")


# 11c. The long-task nudge fires on the tenth turn and points at the right file.
def scenario_checkpoint_nudge():
    sid = forget("selftest-11d")
    contexts = [context_of(prompt(sid, "продолжай")) for _ in range(10)]
    check("11c checkpoint nudge", not any("[Checkpoint]" in c for c in contexts[:9]),
          "no nudge before the tenth turn")
    check("11c checkpoint nudge", "[Checkpoint]" in contexts[9], "the tenth turn nudges")
    check("11c checkpoint nudge", cc.global_checkpoint_path(CWD) in contexts[9],
          "the nudge names the path to write")


# 12. With no OS locking backend the hooks must go inert, not guess.
def scenario_no_locking_backend():
    sid = forget("selftest-12")
    key = cc.session_key(sid)
    saved = (cc.msvcrt, cc.fcntl)
    cc.msvcrt = cc.fcntl = None
    try:
        before = cc.read_json(cc.state_path(key))
        outcome = cc.update_state(key, lambda state: state.update({"touched": True}))
        after = cc.read_json(cc.state_path(key))
        check("12 no locking backend", outcome == (False, None),
              "update reports neither persistence nor a result")
        check("12 no locking backend", after == before, "the state file is left untouched")
    finally:
        cc.msvcrt, cc.fcntl = saved


def main():
    for scenario in (scenario_entity_replacement, scenario_direction_change,
                     scenario_repeated_failure, scenario_loop_guard,
                     scenario_compaction_recovery, scenario_subagent_contract,
                     scenario_clean_output, scenario_false_positives,
                     scenario_checkpoint_trust, scenario_project_checkpoint,
                     scenario_concurrency, scenario_resilience,
                     scenario_checkpoint_cli, scenario_checkpoint_nudge,
                     scenario_no_locking_backend):
        scenario()
    for sid in SESSIONS | {"selftest-start"}:
        try:
            os.remove(cc.state_path(cc.session_key(sid)))
        except OSError:
            pass

    failed = 0
    for scenario, ok, detail in RESULTS:
        print("{}  {} — {}".format("PASS" if ok else "FAIL", scenario, detail))
        failed += 0 if ok else 1
    print("\n{}/{} checks passed".format(len(RESULTS) - failed, len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
