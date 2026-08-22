"""
Continuity — checkpoint CLI.

    python continuity_checkpoint.py path      # where this directory's checkpoint lives
    python continuity_checkpoint.py show      # path + contents, or a notice if there is none
    python continuity_checkpoint.py template  # the empty checkpoint to fill in

`path` returns the checkpoint this directory would actually resume from — the project file
when it is usable, the global one otherwise — or the global path to write to when there is
none. Add `--cwd <dir>` to target another directory. Used by /checkpoint and by anything that
needs the path after a compaction.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_common as cc  # noqa: E402

cc.configure_utf8_streams()

TEMPLATE = """# Task checkpoint

GOAL
<the finished result, one or two lines>

STATUS
active

AUTHORITATIVE STATE
<current requirements, confirmed facts, constraints — superseded ones are deleted, not marked>

COMPLETED
<what is done AND verified, with how it was verified>

CURRENT STATE
<files, tests, implementation as they stand right now>

NEXT ACTION
<exactly one concrete next step>

ACCEPTANCE
<how to tell the task is finished>

BLOCKERS
<real current blockers only, or: none>

STOP CONDITION
<when this work ends>
"""


def target(cwd):
    found = cc.find_checkpoint(cwd)
    return found["path"] if found else cc.global_checkpoint_path(cwd)


def main():
    args = sys.argv[1:]
    command = args[0] if args else "path"
    cwd = os.getcwd()
    if "--cwd" in args:
        index = args.index("--cwd")
        if index + 1 < len(args):
            cwd = args[index + 1]

    if command == "template":
        print(TEMPLATE)
        return 0
    if command == "path":
        print(target(cwd))
        return 0
    if command == "show":
        found = cc.find_checkpoint(cwd)
        if not found:
            print("No checkpoint for {}.\nWrite one to: {}".format(
                cwd, cc.global_checkpoint_path(cwd)))
            return 0
        if not found["readable"]:
            print("{} is not valid UTF-8 — rewrite it.".format(found["path"]))
            return 1
        flags = []
        if found["done"]:
            flags.append("marked done")
        if not found["has_next_action"]:
            flags.append("no NEXT ACTION")
        if found["truncated"]:
            flags.append("truncated for display")
        note = " [{}]".format(", ".join(flags)) if flags else ""
        print("{} (updated {:.1f} days ago){}\n\n{}".format(
            found["path"], found["age_days"], note, found["text"]))
        return 0

    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
