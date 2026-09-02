#!/usr/bin/env python3
"""Install this stack into a Claude Code configuration directory.

Copies the skills, agents, commands, hooks, reference docs and output style into
`~/.claude`, then resolves `settings.example.json` against the real interpreter and
directory. Everything this installer replaces is backed up first, and the settings merge is
opt-in: it keeps the keys you already have and replaces only the hooks it owns.

    python install.py --dry-run          # show what would change
    python install.py                    # copy files, write settings.stack.json
    python install.py --merge-settings   # also merge the hooks into settings.json
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
PAYLOAD = ("skills", "agents", "commands", "hooks", "tools", "reference", "rules", "output-styles")
# Hook logs, caches and stamps are runtime droppings, not payload.
SKIP = {"__pycache__", ".git", ".pytest_cache"}
WINDOWS = os.name == "nt"


class InstallError(Exception):
    """A condition the user has to fix. Reported as one line, never as a traceback."""


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def normalize_path(text: str) -> str:
    """One spelling for a path, so two ways of writing the same file compare equal."""
    unified = text.replace("\\", "/")
    return unified.lower() if WINDOWS else unified


def template_script_paths(target: Path) -> set[str]:
    """Full paths of the hook scripts this stack registers — the identity a merge replaces.

    Read from the template, where every token is still unquoted, rather than from a rendered
    command: a config directory whose name contains a space renders as a quoted path, and
    re-splitting that on whitespace would tear one script into fragments that match nothing.

    Full paths, not basenames: a user who runs their own `code_work_gate_stop.py` out of some
    other directory keeps it, because only a command naming the copy inside this target's
    `hooks/` is one this installer put there.
    """
    settings = json.loads((REPO / "settings.example.json").read_text(encoding="utf-8"))
    found = set()
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                for token in shlex.split(hook.get("command", ""), posix=True):
                    if "__CLAUDE_DIR__" in token:
                        found.add(normalize_path(token.replace("__CLAUDE_DIR__", target.as_posix())))
    return found


def command_arguments(command: str) -> set[str]:
    """The arguments of one live hook command, normalized for comparison.

    Split with the grammar of the shell that will run it, so a Windows path keeps its
    backslashes instead of having them eaten as escapes.
    """
    try:
        tokens = shlex.split(command, posix=not WINDOWS)
    except ValueError:
        tokens = command.split()
    return {normalize_path(token.strip('"')) for token in tokens}


def write_json(path: Path, data: dict) -> None:
    """Write settings the way a human will read them: indented, unescaped, newline-terminated."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def make_backup_dir(target: Path, dry: bool) -> Path:
    """Claim a backup directory no other run can be writing into.

    A timestamp alone collides when two installs land in the same second, and the second run
    would overwrite the first one's only copy of the original settings. Exclusive creation is
    what makes the claim real, so the suffix loop is not decoration.
    """
    base = target / "backups" / f"stack-install-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    if dry:
        return base
    for suffix in ("", *(f"-{n}" for n in range(2, 100))):
        candidate = Path(f"{base}{suffix}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise InstallError(f"cannot claim a backup directory under {base.parent}")


def preflight(target: Path) -> None:
    """Reject a destination whose shape would abort the copy halfway through.

    Checked before the first write and identically in dry runs, so a dry run that reports
    success means the real run will not stop with half the payload installed. Every path the
    installer later reads or writes is covered, not only the payload tree.
    """
    def reject_broken_link(path: Path) -> None:
        # A dangling symlink answers False to exists() while still occupying the name, so a
        # shape check built on exists() alone would wave it through and fail mid-copy.
        if path.is_symlink() and not path.exists():
            raise InstallError(f"{path} is a symlink pointing nowhere; remove it first")

    reject_broken_link(target)
    if target.exists() and not target.is_dir():
        raise InstallError(f"{target} is a file, but the config directory has to be a directory")
    for path in (target / "backups", target / "CLAUDE.md",
                 target / "settings.stack.json", target / "settings.json"):
        reject_broken_link(path)
        if path.exists() and path.is_dir() != (path.name == "backups"):
            shape = "a directory" if path.is_dir() else "a file"
            needed = "a file" if path.name != "backups" else "a directory"
            raise InstallError(f"{path} is {shape}, but the installer needs {needed} there")
    for name in PAYLOAD:
        src = REPO / name
        if not src.is_dir():
            continue
        for path in src.rglob("*"):
            if any(part in SKIP for part in path.relative_to(src).parts):
                continue
            dest = target / name / path.relative_to(src)
            reject_broken_link(dest)
            if path.is_dir() and dest.exists() and not dest.is_dir():
                raise InstallError(f"{dest} is a file, but the payload needs a directory there")
            if path.is_file() and dest.is_dir():
                raise InstallError(f"{dest} is a directory, but the payload needs a file there")
        reject_broken_link(target / name)
        if (target / name).exists() and not (target / name).is_dir():
            raise InstallError(f"{target / name} is a file, but the payload needs a directory there")


def back_up(path: Path, root: Path, backup: Path) -> None:
    """Preserve one file under the backup directory, mirroring its path below root."""
    saved = backup / path.relative_to(root)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, saved)


def copy_tree(src: Path, dst: Path, root: Path, backup: Path, dry: bool) -> tuple[int, int]:
    """Copy src over dst, backing up every file that already differs. Returns (new, replaced)."""
    new = replaced = 0
    for path in sorted(src.rglob("*")):
        if any(part in SKIP for part in path.relative_to(src).parts):
            continue
        target = dst / path.relative_to(src)
        if path.is_dir():
            if not dry:
                target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            if target.read_bytes() == path.read_bytes():
                continue
            replaced += 1
            if not dry:
                back_up(target, root, backup)
        else:
            new += 1
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return new, replaced


# cmd.exe treats all of these as syntax, and every one of them is legal in a Windows filename.
CMD_SPECIAL = set(" \t&()[]{}^=;+,`~<>|'")
# Quoting does not neutralize these: %NAME% expands inside double quotes, !NAME! expands too
# wherever delayed expansion is enabled, and a literal quote cannot be represented at all.
CMD_REJECT = {'"': "a quote", "%": "a % that cmd.exe would expand",
              "!": "a ! that delayed expansion would eat"}


def shell_quote(argument: str) -> str:
    """Quote one argument for the shell Claude Code launches hook commands through."""
    if not WINDOWS:
        return shlex.quote(argument)
    for character, why in CMD_REJECT.items():
        if character in argument:
            raise InstallError(f"this path carries {why}, which cmd.exe cannot be made to "
                               f"take literally: {argument}")
    return f'"{argument}"' if any(c in argument for c in CMD_SPECIAL) else argument


def resolve_command(command: str, target: Path, python: str) -> str:
    """Rebuild one template command against this machine, quoting every argument itself.

    The template is parsed as JSON before this runs, so the placeholders arrive as plain text
    and each one becomes exactly one argument. Substituting into the raw JSON instead would
    make a path containing a quote or a backslash produce a file that no longer parses, and a
    path containing shell syntax would be interpreted rather than used.
    """
    return " ".join(
        shell_quote(token.replace("__CLAUDE_DIR__", target.as_posix()))
        if token != "__PYTHON__" else shell_quote(python)
        for token in shlex.split(command, posix=True)
    )


def resolve_settings(target: Path, python: str) -> dict:
    """Resolve the template's hooks against this machine, dropping ones this platform can't run."""
    settings = json.loads((REPO / "settings.example.json").read_text(encoding="utf-8"))
    hooks: dict[str, list] = {}
    for event, groups in settings.get("hooks", {}).items():
        kept_groups = []
        for group in groups:
            kept = []
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                # The plugin updater is a PowerShell script; nothing else in the payload is
                # platform-bound, and a POSIX box has no powershell.exe to run it with. The
                # worktree audit runs under node, which every platform can supply.
                if ".ps1" in command and not WINDOWS:
                    continue
                kept.append({**hook, "command": resolve_command(command, target, python)})
            if kept:
                kept_groups.append({**{k: v for k, v in group.items() if k != "hooks"}, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
    settings["hooks"] = hooks
    return settings


def load_live_settings(path: Path) -> dict:
    """Read the settings about to be merged into. A hand-edited file is the one input here
    that the repo does not control, so a broken one gets a usable message, not a traceback."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InstallError(
            f"{path} is not valid UTF-8 JSON ({exc}). "
            "Fix it, or install without --merge-settings and merge by hand."
        ) from exc


def merge_settings(live: dict, stack: dict, owned_paths: set[str]) -> dict:
    """Overlay the stack's settings on the live ones without dropping unrelated keys.

    Keys only the user has survive untouched, and `env`-style dicts are merged key by key. A
    key both sides define with a non-dict value — `availableModels`, `outputStyle` — takes the
    stack's value wholesale, so a customized one is worth re-applying after an install.

    Hooks are matched by the full path of the payload script a command runs, not by the command
    text. That is what makes a second install idempotent even when the interpreter path changed:
    the stale registration is replaced rather than joined by a second one that fires the same
    hook twice. A hook of the user's own is only ever touched if it runs a script out of this
    target's own `hooks/` directory, which is to say a script this installer put there.
    """
    merged = dict(live)
    for key, value in stack.items():
        if key == "hooks":
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value

    hooks: dict[str, list] = {}
    for event, groups in live.get("hooks", {}).items():
        kept = []
        for group in groups:
            survivors = [
                hook for hook in group.get("hooks", [])
                # Exact argument equality, not a substring: `…/stop.py.disabled` contains an
                # owned path but runs a different file, and is somebody else's business.
                if not command_arguments(hook.get("command", "")) & owned_paths
            ]
            if survivors:
                kept.append({**{k: v for k, v in group.items() if k != "hooks"}, "hooks": survivors})
        if kept:
            hooks[event] = kept
    for event, groups in stack.get("hooks", {}).items():
        hooks.setdefault(event, []).extend(groups)
    merged["hooks"] = hooks
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=Path, default=None, help="config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--python", default=sys.executable, help="interpreter the hooks should run under")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    parser.add_argument("--merge-settings", action="store_true", help="merge hooks into settings.json (backed up first)")
    args = parser.parse_args()

    target = (args.target or claude_dir()).expanduser().resolve()
    dry = args.dry_run

    # Resolve before touching anything: an unquotable path should fail with nothing installed.
    stack = resolve_settings(target, args.python)
    preflight(target)

    backup = make_backup_dir(target, dry)
    print(f"target      {target}")
    print(f"interpreter {args.python}")
    print(f"backups     {backup}{' (dry run — nothing written)' if dry else ''}\n")

    for name in PAYLOAD:
        src = REPO / name
        if not src.is_dir():
            continue
        new, replaced = copy_tree(src, dst=target / name, root=target, backup=backup, dry=dry)
        print(f"  {name:<14} {new:>3} new, {replaced:>3} replaced")

    claude_md = target / "CLAUDE.md"
    if claude_md.exists():
        print(f"\n  CLAUDE.md already exists — left untouched. Compare with {REPO / 'CLAUDE.md'}")
    else:
        if not dry:
            shutil.copy2(REPO / "CLAUDE.md", claude_md)
        print(f"\n  CLAUDE.md      {'would be installed' if dry else 'installed'}")

    resolved = target / "settings.stack.json"
    if not dry:
        if resolved.exists() and resolved.read_text(encoding="utf-8") != json.dumps(
            stack, indent=2, ensure_ascii=False
        ) + "\n":
            back_up(resolved, target, backup)
        write_json(resolved, stack)
    print(f"\n  settings.stack.json {'would be written to' if dry else 'written to'} {resolved}")

    live_path = target / "settings.json"
    if args.merge_settings:
        merged = merge_settings(load_live_settings(live_path), stack,
                                template_script_paths(target))
        if dry:
            print("  would merge into settings.json")
        else:
            if live_path.exists():
                back_up(live_path, target, backup)
            write_json(live_path, merged)
            print(f"  settings.json merged (previous version saved in {backup})")
    else:
        print("  settings.json untouched — merge it yourself, or re-run with --merge-settings")

    print("\nNext: restart Claude Code, then run `python hooks/test_gate.py` to confirm the gate works.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InstallError, OSError) as exc:
        # An unwritable target, a file where the payload expects a directory, a locked config —
        # all of it is the user's filesystem, and none of it is worth a stack trace.
        print(f"install failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
