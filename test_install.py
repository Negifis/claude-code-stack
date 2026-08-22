"""Regression tests for install.py — the parts that touch a user's live configuration.

Run with `python test_install.py`. No test framework, same style as the hook suites.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"FAIL {name}: {detail}")


def run_installer(target, *flags, python=sys.executable):
    return subprocess.run(
        [sys.executable, str(HERE / "install.py"), "--target", str(target),
         "--python", python, *flags],
        capture_output=True, text=True,
    )


def hook_commands(settings):
    return [h["command"] for groups in settings.get("hooks", {}).values()
            for g in groups for h in g["hooks"]]


# --- command resolution and quoting -------------------------------------------------------

def test_posix_quoting():
    was = install.WINDOWS
    install.WINDOWS = False
    try:
        hostile = Path("/tmp/$(id)/a b/`x`/'q'/$HOME")
        out = install.resolve_command(
            "__PYTHON__ -S __CLAUDE_DIR__/hooks/guard.py", hostile, "/usr/bin/python3"
        )
        tokens = install.shlex.split(out, posix=True)
        check("posix: three arguments survive", len(tokens) == 3, out)
        check("posix: script path is one literal argument",
              tokens[2] == f"{hostile.as_posix()}/hooks/guard.py", tokens)
        # A real shell has to reproduce the path byte for byte rather than expanding anything
        # in it — the tokenizer above proves the split, this proves the expansion.
        sh = shutil.which("sh")
        if sh:
            echoed = subprocess.run([sh, "-c", f"printf %s {out.split(' ', 2)[2]}"],
                                    capture_output=True, text=True)
            check("posix: shell reproduces the path verbatim",
                  echoed.returncode == 0
                  and echoed.stdout == f"{hostile.as_posix()}/hooks/guard.py",
                  f"rc={echoed.returncode} out={echoed.stdout!r}")
        else:
            print("SKIP posix: no sh on PATH to verify expansion")
    finally:
        install.WINDOWS = was


def test_windows_quoting():
    was = install.WINDOWS
    install.WINDOWS = True
    try:
        out = install.resolve_command(
            "__PYTHON__ __CLAUDE_DIR__/hooks/stop.py",
            Path("C:/Users/a b/.claude"), "C:/Program Files/Python/python.exe",
        )
        check("windows: spaces are quoted", out.count('"') == 4, out)
        for hostile, why in ((Path("C:/%USERNAME%/.claude"), "percent"), (Path('C:/a"b/.claude'), "quote")):
            try:
                install.resolve_command("__PYTHON__ __CLAUDE_DIR__/x.py", hostile, "python.exe")
                check(f"windows: {why} path rejected", False, "no error raised")
            except install.InstallError:
                check(f"windows: {why} path rejected", True)
    finally:
        install.WINDOWS = was


def test_powershell_hook_is_windows_only():
    for windows, expected in ((True, True), (False, False)):
        was = install.WINDOWS
        install.WINDOWS = windows
        try:
            resolved = install.resolve_settings(Path("/tmp/cfg"), "python3")
            present = any(".ps1" in c for c in hook_commands(resolved))
            check(f"ps1 hook present on windows={windows}", present is expected, present)
        finally:
            install.WINDOWS = was


# --- merge behavior -----------------------------------------------------------------------

def test_merge_is_idempotent_across_interpreters():
    stack_a = install.resolve_settings(Path("/tmp/cfg"), "/usr/bin/python3.11")
    stack_b = install.resolve_settings(Path("/tmp/cfg"), "/usr/bin/python3.12")
    owned = install.hook_scripts()
    once = install.merge_settings({}, stack_a, owned)
    twice = install.merge_settings(once, stack_a, owned)
    switched = install.merge_settings(once, stack_b, owned)
    check("merge: second identical run adds nothing",
          len(hook_commands(twice)) == len(hook_commands(once)), hook_commands(twice))
    check("merge: switching interpreter does not double-register",
          len(hook_commands(switched)) == len(hook_commands(once)), hook_commands(switched))
    check("merge: no stale interpreter survives",
          not any("python3.11" in c for c in hook_commands(switched)), hook_commands(switched))


def test_merge_preserves_user_content():
    live = {
        "myOwnKey": {"keep": 1},
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-script --flag"}]}]},
    }
    merged = install.merge_settings(live, install.resolve_settings(Path("/tmp/cfg"), "python3"),
                                    install.hook_scripts())
    check("merge: unrelated top-level key survives", merged.get("myOwnKey") == {"keep": 1}, merged.keys())
    check("merge: unrelated permissions survive",
          merged["permissions"]["allow"] == ["Bash(ls:*)"], merged.get("permissions"))
    check("merge: unrelated user hook survives",
          "my-own-script --flag" in hook_commands(merged), hook_commands(merged))


# --- filesystem safety --------------------------------------------------------------------

def test_backup_dirs_are_unique():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        first = install.make_backup_dir(target, dry=False)
        second = install.make_backup_dir(target, dry=False)
        check("backup: two runs in the same second get separate directories", first != second,
              f"{first} == {second}")


def test_preflight_blocks_before_writing():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "agents").write_text("not a directory", encoding="utf-8")
        result = run_installer(target)
        check("preflight: real run refuses a type conflict", result.returncode == 1, result.stderr)
        check("preflight: nothing was installed alongside it",
              not (target / "skills").exists(), sorted(p.name for p in target.iterdir()))
        check("preflight: no traceback", "Traceback" not in result.stderr, result.stderr)
        dry = run_installer(target, "--dry-run")
        check("preflight: dry run reports the same conflict", dry.returncode == 1, dry.stdout)


def test_stack_file_is_backed_up_before_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        run_installer(target)
        (target / "settings.stack.json").write_text('{"mine": true}', encoding="utf-8")
        run_installer(target)
        saved = list((target / "backups").glob("*/settings.stack.json"))
        check("backup: an edited settings.stack.json is preserved", len(saved) == 1, saved)
        if saved:
            check("backup: it holds the edited content",
                  json.loads(saved[0].read_text(encoding="utf-8")) == {"mine": True},
                  saved[0].read_text(encoding="utf-8"))


def test_broken_settings_json_is_reported_not_clobbered():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        broken = '{ "env": { , }'
        (target / "settings.json").write_text(broken, encoding="utf-8")
        result = run_installer(target, "--merge-settings")
        check("broken settings: exits 1", result.returncode == 1, result.returncode)
        check("broken settings: no traceback", "Traceback" not in result.stderr, result.stderr)
        check("broken settings: file left intact",
              (target / "settings.json").read_text(encoding="utf-8") == broken)


def test_full_install_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        first = run_installer(target, "--merge-settings")
        check("install: first run succeeds", first.returncode == 0, first.stderr)
        settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
        expected = len(hook_commands(install.resolve_settings(target, sys.executable)))
        check("install: every stack hook is registered",
              len(hook_commands(settings)) == expected, hook_commands(settings))
        check("install: skills landed", (target / "skills" / "simplify" / "SKILL.md").exists())
        check("install: CLAUDE.md landed", (target / "CLAUDE.md").exists())

        edited = target / "skills" / "simplify" / "SKILL.md"
        edited.write_text("local change", encoding="utf-8")
        run_installer(target)
        saved = list((target / "backups").glob("*/skills/simplify/SKILL.md"))
        check("install: a locally edited payload file is backed up", len(saved) == 1, saved)
        if saved:
            check("install: the backup holds the local edit",
                  saved[0].read_text(encoding="utf-8") == "local change")


for test in [
    test_posix_quoting,
    test_windows_quoting,
    test_powershell_hook_is_windows_only,
    test_merge_is_idempotent_across_interpreters,
    test_merge_preserves_user_content,
    test_backup_dirs_are_unique,
    test_preflight_blocks_before_writing,
    test_stack_file_is_backed_up_before_overwrite,
    test_broken_settings_json_is_reported_not_clobbered,
    test_full_install_round_trip,
]:
    test()

if FAILED:
    print(f"\nFAILED {len(FAILED)} of {PASSED + len(FAILED)}")
    sys.exit(1)
print(f"PASS: {PASSED} assertions")
