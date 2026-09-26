"""
Code Work Gate - PostToolUse marker.

Marks a session when code, configuration, infrastructure, or executable agent instructions are
edited. The Stop hook can then enforce a finite development-verification protocol if the final
answer has no valid terminal receipt.

The marker keeps first_ts, last_ts and last_durable_ts. Stop enforcement is keyed to last_ts and
has a hard retry cap, while a later edit becomes a new candidate. last_durable_ts records the
last change to a lasting artifact and is what review-freshness checks compare against, so
rerunning a throwaway script cannot expire a verdict. first_ts bounds every finite budget the
Stop hook counts, so it also restarts when the candidate identity changes or the marker goes
stale — a candidate abandoned without a terminal receipt must not spend the next candidate's
budgets.
Fail-open: any error returns continue=true.
"""
import json
import hashlib
import glob
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_work_gate_common as cwg  # noqa: E402

cwg.configure_utf8_streams()

# Matched one pipeline segment at a time, after harmless redirects are gone: read across the whole
# line, `echo …` in one segment and `>/dev/null` in another made a read write-shaped, and the
# command kept another session's concurrent edit as its own (report 5ed394cc). A writer behind a
# wrapper (`sudo tee`, `xargs rm`) still starts its segment.
SHELL_WRITE_RE = re.compile(
    r"(?i)(\bapply_patch\b|\bgit\s+(?:apply|mv)\b|"
    r"^\s*(?:(?:sudo|doas|xargs|env|nohup|time)\s+(?:-\S+(?:\s+[^-\s]\S*)?\s+|\w+=\S*\s+)*)?(?:rm|mv|tee)\b|"
    r"\bsed\b.*\s-i\b|"
    r"\bperl\b.*\s-pi\b|\b(Set-Content|Add-Content|Out-File)\b|"
    r"\b(Copy-Item|Move-Item|Remove-Item|Rename-Item|New-Item)\b|"
    r"\b(prettier|eslint|ruff)\b.*"
    r"(--write|--fix)\b|\b(cat|echo|printf)\b.*(?<![0-9])>>?(?!&))"
)
READ_ONLY_SHELL_RE = re.compile(
    r"(?i)^\s*(?:"
    r"git\s+(?:status|diff|log|show|rev-parse|ls-files)\b|"
    r"rg\b|grep\b|pwd\b|ls\b|dir\b|where\b|which\b|"
    r"Get-Content\b|Select-String\b|Test-Path\b|Get-Item\b|Get-ChildItem\b|"
    r"echo\b|(?:python|python3|node|claude)\s+--version\b"
    r")[^;&|>\r\n]*$"
)
VALIDATION_SHELL_RE = re.compile(
    r"(?i)^\s*(?:"
    r"(?:npm(?:\.cmd)?|pnpm(?:\.cmd)?|bun)\s+(?:test\b|run\s+"
    r"(?:test|typecheck|lint|build|check|validate|verify|ux:check)(?::[\w.-]+)?\b)|"
    r"yarn(?:\.cmd)?\s+(?:run\s+)?"
    r"(?:test|typecheck|lint|build|check|validate|verify)(?::[\w.-]+)?\b|"
    r"(?:python|python3|py)\s+-m\s+pytest\b|pytest\b|go\s+test\b|"
    r"cargo\s+(?:test|check|clippy|build)\b|dotnet\s+(?:test|build)\b|"
    r"(?:\.?[/\\])?gradlew(?:\.bat)?\s+(?:test|check|build)\b|"
    r"gradle\s+(?:test|check|build)\b|mvn(?:\.cmd)?\s+(?:test|verify)\b|"
    r"make\s+(?:test|check|lint|build|validate|verify)\b|"
    r"(?:npx(?:\.cmd)?|pnpm(?:\.cmd)?\s+exec|yarn(?:\.cmd)?\s+exec|bunx)\s+"
    r"(?:tsc|eslint|prettier|ruff)\b|"
    r"node(?:\.exe)?\s+(?:--test\b|(?:\.?[/\\])?(?:scripts?|tools?)[/\\]"
    r"[^\s;&|><`\r\n]*(?:test|check|lint|typecheck|validate|verify|build)"
    r"[^\s;&|><`\r\n]*\.(?:[cm]?js|ts)\b)"
    r")[^;&|><`\r\n]*$"
)

# The gate grades executable agent configuration HIGH, and on this machine that configuration
# lives outside any repository — exactly what a Git snapshot cannot see. A shell command that
# rewrote a hook therefore left the candidate marked <shell-mutation>, and a session whose whole
# product was a new hook graded as operational work needing neither simplify nor review. These
# trees are small and hand-edited, which is what makes snapshotting them affordable on every
# shell call. `plugins/` is deliberately excluded: it is machine-managed, an order of magnitude
# larger, and rewritten by the autoupdate hook, so watching it would mark sessions that touched
# nothing.
# Deliberately not the same set as the directories `AGENT_EXECUTABLE_PATH_RE` grades HIGH:
# `rules` and `reference` are watched but graded as prose. A gated tree left unwatched is not
# merely unseen: `outside_snapshot` then finds a recorded path no snapshot can vouch for, which
# expires the review verdict on every later shell call and leaves the candidate unable to close.
# `plugins` stays out because it is machine-managed and an order of magnitude larger.
AGENT_CONFIG_DIRS = (
    "hooks", "agents", "commands", "skills", "output-styles", "rules", "reference",
)
AGENT_CONFIG_FILES = (
    "settings.json", "settings.local.json", ".mcp.json", "claude.md", "agents.md",
)
SETTINGS_FILES = frozenset(("settings.json", "settings.local.json"))
# Keys the Claude Code app writes into a settings file from its own interface — /config, /model,
# /effort, the output-style picker — whenever the user changes them, outside any command. A
# rewrite confined to them changes nothing that runs or grants authority, and charging it to the
# command that happened to be running put an output-style switch into two sessions' candidates
# at HIGH (reports aa7603a1, 551e104e).
APP_SETTINGS_KEYS = frozenset((
    "outputStyle", "model", "effortLevel", "alwaysThinkingEnabled", "theme", "verbose",
    "editorMode", "language", "showThinkingSummaries",
))
# These trees are shared: a second session, an editor, or a background hook writing here while
# a command runs is attributed to that command, the same way a dirty worktree is attributed to
# the command that ran over it. The cost is a review round, never a missed one.
# Bookkeeping and vendored trees a configuration directory may still contain. Dot-directories
# are skipped wholesale for the same reason `plugins/` is: every one of them here is machine
# managed — a vendor namespace the CLI re-syncs (`.codex/skills/.system`), a virtualenv, a cache,
# a repository. Their churn arrives on whatever command happens to be running, which marked
# sessions that touched nothing. An edit made through Edit/Write is still gated by path.
AGENT_CONFIG_SKIP = {"__pycache__", "node_modules", "backups", "state", "plans"}
# Across all the homes together: a configuration this large is not the hand-edited tree this
# scan assumes, and proving nothing is safer than spending a Stop budget walking it.
AGENT_CONFIG_LIMIT = 8192
# Agent tooling copies its skill trees into every worktree, so one sync rewrites them on every
# branch at once and lands on whichever command was running. Inside a repository they are that
# copy, not the session's work; the sources they are copied from are watched in the homes above.
# Attribution only, like the skip list: an edit made through Edit/Write is still gated by path.
SYNCED_AGENT_TREE_RE = re.compile(r"/\.(?:agents|codex|claude)/skills/", re.IGNORECASE)
# Whether a shell command could have rewritten a file the snapshot did not see decides whether
# an unresolved mutation expires the review verdict. The rule is an allowlist of commands proven
# read-only, judged per pipeline segment: anything unknown, any redirect other than a discarded
# or merged stderr, a command substitution, a heredoc or a script block is write-capable.
# `git status --porcelain | wc -l` seven seconds after a Codex approval expired that approval on
# 2026-09-03 and cost the session a native review round it did not need; nothing that command
# can do touches a lasting artifact, while `tee`, `truncate` or `1>` plainly can.
READ_ONLY_COMMANDS = frozenset("""
    cd ls dir cat head tail grep egrep fgrep rg find fd wc sort uniq cut tr diff comm
    stat file du df pwd echo printf date true false test [ type which where whoami
    printenv jq column nl tac basename dirname realpath readlink md5sum sha1sum sha256sum tree cmp
    ps tasklist nproc uname sleep
    get-childitem get-content get-item get-command select-string select-object measure-object
    format-table format-list out-string write-output write-host test-path resolve-path
    get-process get-location get-date sort-object gci gc gi sls findstr
""".split())
GIT_READ_SUBCOMMANDS = frozenset("""
    status log diff show rev-parse ls-files ls-tree blame describe rev-list cat-file shortlog
    for-each-ref name-rev merge-base grep check-ignore diff-tree diff-index count-objects reflog
    var version help
""".split())
# The listing forms of git subcommands that also write: `git branch` alone lists, `git branch x`
# creates, and `git stash` alone stashes.
GIT_LISTING_RE = re.compile(
    r"^(?:(?:branch|tag|remote|worktree|config)\b(?:\s+(?:list|-l|--list|-a|-v|-vv|--all|"
    r"--show-current|--merged|--no-merged|--contains\s+\S+|--get(?:-all|-regexp)?\s+\S+))*"
    r"|stash\s+(?:list|show)\b[^\r\n]*)\s*$"
)
# Arguments that turn a listed command into a writer or a command runner. A command absent
# here can only write through a redirect, which is caught separately.
MUTATING_ARGS = {
    "find": re.compile(r"(?:^|\s)-(?:delete|exec(?:dir)?|ok(?:dir)?|fprint0?|fprintf|fls)\b"),
    "fd": re.compile(r"(?:^|\s)(?:-x|-X|--exec(?:-batch)?)\b"),
    "rg": re.compile(r"(?:^|\s)--pre\b"),
    # `-o` also inside a single-dash cluster or attached to its operand: `-uo f f`, `-ofile`.
    "sort": re.compile(r"(?:^|\s)(?:-(?!-)\w*o|--output\b)"),
    "tree": re.compile(r"(?:^|\s)-(?!-)\w*o"),
    "date": re.compile(r"(?:^|\s)(?:-s\b|--set\b)"),
    "git": re.compile(r"(?:^|\s)(?:-o\b|--output\b)"),
}
# gh talks to GitHub; only these forms are known not to touch the working tree or the local
# clone. Anything else — `pr checkout`, `repo clone`, an alias, an extension — is write-capable.
REPO_FLAGS = r"^(?:(?:-R|--repo)(?:=\S+|\s+\S+)\s+)*"
GH_READ_RE = re.compile(
    REPO_FLAGS + r"(?:"
    r"pr\s+(?:view|list|status|diff|checks|comment|edit|create|close|reopen|review|ready)|"
    r"issue\s+(?:view|list|status|create|comment|edit|close|reopen)|repo\s+(?:view|list)|"
    r"api|run\s+(?:view|list|watch)|release\s+(?:view|list)|search\s+\S+|label\s+list|"
    r"auth\s+status|browse)\b"
)
# glab likewise: these forms talk to GitLab and print to stdout. Anything else — `mr checkout`,
# `repo clone`, `ci artifact`, `release download`, an alias — is write-capable.
GLAB_READ_RE = re.compile(
    REPO_FLAGS + r"(?:"
    r"mr\s+(?:view|list|diff|note|create|update|approve|revoke|close|reopen|merge|subscribe|unsubscribe)|"
    r"issue\s+(?:view|list|note|create|update|close|reopen|subscribe|unsubscribe)|"
    r"ci\s+(?:view|list|status|trace|get|lint|run|retry|cancel)|"
    r"api|release\s+(?:view|list)|repo\s+view|label\s+list|auth\s+status|variable\s+(?:list|get))\b"
)
# Tools that write only their own bookkeeping, never a lasting artifact: the memory CLI keeps its
# queue and mirror under ~/.codex/notebooklm-sync, and the gate's inbox and Codex breaker write the
# home's `state/`. CLAUDE.md asks for `nlm-memory remember` the moment something is confirmed, often
# right after an approval, and under load its unresolved snapshot expired that approval (report
# e5533b32). Git Bash finds the shim only as `nlm-memory.cmd` or through a variable holding its path
# (`NLM=~/.local/bin/nlm-memory.cmd; $NLM remember …`). The snapshot still records whatever such a
# command measurably changed. Only the subcommands that write nothing but the tool's own home count:
# `rollback` restores files to paths a manifest names, `init`/`migrate`/`sync` reach into projects.
BOOKKEEPING_COMMANDS = {
    "nlm-memory": frozenset(("recall", "remember", "stats", "doctor", "status", "maintain")),
}
# Scripts that write only their own state — the hooks' and the memory bridge — by subcommand (None: all).
# `chip_handoff finish` merges in a scratch worktree under `state/chips` and moves only a branch no
# checkout holds, and the skill runs it right after the approval (report 44855de6); `close` writes the
# chip's card, `status` reads the cards; `open` cuts a worktree and stays write-capable.
STATE_ONLY_SCRIPTS = {
    "gate_inbox": None,
    "codex_lane": None,
    "chip_handoff": frozenset(("finish", "status", "close")),
    "nlm_sync": BOOKKEEPING_COMMANDS["nlm-memory"],  # BRIDGE_SCRIPT
}


def hook_script_re(names):
    """A pattern for the hooks' scripts of these names, spelled by any path."""
    return re.compile(r"(?:^|/)hooks/(?:{})\.py$".format("|".join(names)), re.IGNORECASE)


# The memory bridge the `nlm-memory` shim runs, called under an interpreter when an argument carries
# characters the shim's cmd.exe would parse (report c2a8dfe0). It lives under `~/.codex`, so unlike
# the hooks' scripts it still names that home.
BRIDGE_SCRIPT = "nlm_sync"
BRIDGE_SCRIPT_RE = re.compile(r"(?:^|/)notebooklm-sync/bin/nlm_sync\.py$", re.IGNORECASE)
STATE_ONLY_SCRIPT_RE = hook_script_re(sorted(set(STATE_ONLY_SCRIPTS) - {BRIDGE_SCRIPT}))
# A quoted shell word, the same alternatives `SHELL_WORD_RE` starts with.
QUOTED_TEXT = r"\"[^\"]*\"|'[^']*'"
QUOTED_TEXT_RE = re.compile(QUOTED_TEXT)
# What a Codex launch feeds on stdin, read before the command runs: the Stop hook binds the
# verdict to the session that was given exactly this text, whatever the file holds later.
STDIN_REDIRECT_RE = re.compile(r"(?<![<>])<(?!<)\s*\"?([^\s\"<>|;&]+)\"?")
PACKET_KEEP_BYTES = 256 * 1024
# A capture older than this belongs to a launch whose notification never came.
PACKET_CAPTURE_TTL = 24 * 3600.0
# `-c key=value` is not skipped: it can point a reading subcommand at an external program
# (`diff.external`, `core.fsmonitor`), and so falls through as an unknown subcommand.
GIT_GLOBAL_OPTIONS_RE = re.compile(
    r"^(?:(?:-C\s+\S+|--no-pager|--git-dir=\S+|--work-tree=\S+)\s+)+"
)
# Argument shapes that execute something whatever the command: a sub-expression or type
# accessor in PowerShell, a bracket expression or grouping in either shell.
EXECUTING_ARGUMENT_RE = re.compile(r"[(\[]|::")
# Only a discarded stream or a merged stderr is not a file the command may have written into.
HARMLESS_REDIRECT_RE = re.compile(
    r"(?:&|[12])?>>?\s*(?:/dev/null|\$null|nul)(?=$|[\s;&|)])|2>&1|1>&2|>&2", re.IGNORECASE
)
SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;&\r\n]")
# Each shell's own continuation marker, and what survives it. In bash a backslash escapes the
# newline, so only an odd run continues — an even one is escaped backslashes before a real
# separator. PowerShell continues on a backtick, and a trailing backslash there ends a path.
CONTINUATIONS = {
    "Bash": (re.compile(r"(?<!\\)((?:\\\\)*)\\\r?\n"), r"\1"),
    "PowerShell": (re.compile(r"`\r?\n"), ""),
}
COMMENT_START_RE = re.compile(r"(?:^|\s)#")
ENV_ASSIGNMENT_RE = re.compile(r"^(\w+)=(\"[^\"]*\"|'[^']*'|\S*)\s*")
WRAPPER_RE = re.compile(r"^(?:timeout\s+(?:-\S+\s+)*\S+|time|nohup|command|builtin)\s+")


def join_continuations(command, shell="Bash"):
    """One line again: the marker and the newline go, so a token split across them stays one."""
    text = str(command or "")
    pattern, keep = CONTINUATIONS.get(shell, CONTINUATIONS["Bash"])

    def join(match):
        line = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
        # A comment runs to the end of its line, marker and all, so that newline still separates.
        # Reading a quoted `#` as one costs a split that was not there, never a joined command.
        return match.group(0) if COMMENT_START_RE.search(line) else match.expand(keep)

    return pattern.sub(join, text)


def shell_segments(command, maxsplit=0, shell="Bash"):
    """The command's separate commands, line continuations joined first.

    Splitting a continued line invents segments: the documented multi-line Codex launch lost the
    `< packet` redirect to a segment of its own, so no capture was taken and every verdict it
    produced bound nothing (report 2b62e2fb). A separator inside quotes is an argument, not a
    boundary: `grep -e "a\\|b" f` cut at its `|` graded the pattern's tail as an unknown command,
    and the read became write-capable.
    """
    return split_unquoted(join_continuations(command, shell), maxsplit, shell)


def unquoted_separators(text, shell="Bash"):
    """Each separator standing outside quotes, as `(index, separator)`, then None if the quotes
    never balanced.

    The shell's escape character — a backslash in bash, a backtick in PowerShell — protects the
    next character except inside single quotes, and never a newline, which `join_continuations`
    has already dealt with. An `&` beside a `>` belongs to a redirect (`2>&1`, `&> log`), not a
    separator.
    """
    escape = "`" if shell == "PowerShell" else "\\"
    index, quote = 0, None
    while index < len(text):
        char = text[index]
        if char == escape and quote != "'" and index + 1 < len(text) and text[index + 1] not in "\r\n":
            index += 2
            continue
        if quote:
            quote = None if char == quote else quote
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        width = 2 if text.startswith(("||", "&&"), index) else (1 if char in "|;&\r\n" else 0)
        if char == "&" and ">" in (text[index - 1:index], text[index + 1:index + 2]):
            width = 0
        if width:
            yield index, text[index:index + width]
        index += width or 1
    if quote:
        yield None


def split_unquoted(text, maxsplit=0, shell="Bash"):
    """`SEGMENT_SPLIT_RE.split`, cutting only at separators that stand outside quotes.

    Text whose quotes do not balance is split the plain way, which cuts more, never less.
    """
    segments, start = [], 0
    for cut in unquoted_separators(text, shell):
        if cut is None:
            return SEGMENT_SPLIT_RE.split(text, maxsplit=maxsplit)
        if maxsplit and len(segments) >= maxsplit:
            break
        index, separator = cut
        segments.append(text[start:index])
        start = index + len(separator)
    segments.append(text[start:])
    return segments


def separated_segments(command, shell="Bash"):
    """The command's segments, each with the separator after it (`""` after the last), or None
    when its quotes do not balance and no segment's context can be read."""
    text = join_continuations(command, shell)
    pairs, start = [], 0
    for cut in unquoted_separators(text, shell):
        if cut is None:
            return None
        index, separator = cut
        pairs.append((text[start:index], separator))
        start = index + len(separator)
    pairs.append((text[start:], ""))
    return pairs


# Where a command works, read from its own literal directory changes: the snapshot has to be taken
# there before the command runs, or everything the command did there is unmeasured (reports
# 4b840373, 0e4aedc8, a269a6fc). Only a target the text spells completely is followed — no
# expansion, no glob, a directory that exists now — and a change inside a pipeline, a background
# job, a subshell or a heredoc is not, because it moves nothing the rest of the command runs in.
DIRECTORY_COMMANDS = frozenset(("cd", "chdir", "pushd", "set-location", "sl", "push-location"))
RETURN_COMMANDS = frozenset(("popd", "pop-location"))
DIRECTORY_OPTIONS = frozenset(("-l", "-p", "-e", "-@", "--", "-path", "-literalpath"))
SHELL_WORD_RE = re.compile(QUOTED_TEXT + r"|\S+")
LITERAL_DIRECTORY_RE = re.compile(r"^[^$`*?\[\]{}()%!<>|;&\r\n]+$")
MSYS_DRIVE_RE = re.compile(r"^/([a-zA-Z])(?=/|$)")
VARIABLE_REFERENCE_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
# Commands that can give a shell variable a new value: after one, no earlier value is trusted.
VARIABLE_WRITERS = frozenset((
    "for", "select", "read", "readarray", "mapfile", "export", "declare", "typeset", "local",
    "readonly", "set", "unset", "eval", "source", ".", "let", "getopts", "printf", "exec",
))
ASSIGNMENT_ANYWHERE_RE = re.compile(r"(?:^|[\s;&|(])[A-Za-z_]\w*\+?=")
# How many repositories a command's own directory changes add to its snapshot, beyond the one it
# starts in and the candidate's own. A target past this stays unmeasured.
MAX_DIRECTORY_REPOSITORIES = 2


def unquoted_word(word):
    """A shell word without its surrounding quotes, and whether it had them."""
    if len(word) > 1 and word[0] in "'\"" and word[-1] == word[0]:
        return word[1:-1], True
    return word, False


def literal_directory(arguments, current, shell="Bash", variables=None):
    """The existing directory a `cd`-like command's arguments name completely, or None.

    In bash, `$NAME` and `${NAME}` resolve from `variables` — the literal values the same command
    assigned before this point (report 265312d0: `CT="<chip tree>"; … cd "$CT" && …`).
    """
    words = [word for word in SHELL_WORD_RE.findall(arguments or "")
             if word.lower() not in DIRECTORY_OPTIONS]
    if len(words) != 1:
        return None
    target, quoted = unquoted_word(words[0])
    # Bash reads an unquoted backslash as an escape, so `cd C:\tmp` does not go to C:\tmp.
    if shell != "PowerShell" and not quoted and "\\" in target:
        return None
    if shell != "PowerShell" and variables and not words[0].startswith("'"):
        def value(match):
            found = variables.get(match.group(1) or match.group(2))
            # Unquoted, a value with blanks would split into several words.
            if found is None or (not quoted and re.search(r"\s", found)):
                return match.group(0)
            return found
        target = VARIABLE_REFERENCE_RE.sub(value, target)
    if not target or target == "-" or not LITERAL_DIRECTORY_RE.match(target):
        return None
    if target == "~" or target.startswith(("~/", "~\\")):
        target = os.path.expanduser("~") + target[1:]
    elif target.startswith("~"):
        return None
    drive = MSYS_DRIVE_RE.match(target)
    if drive:
        target = drive.group(1) + ":/" + target[2:].lstrip("/")
    elif target.startswith(("/", "\\")):
        # A root the shell maps on its own (`/tmp` in Git Bash), or a network share: not followed.
        return None
    if re.match(r"^[a-zA-Z]:(?![\\/])", target):
        return None
    if not re.match(r"^[a-zA-Z]:[\\/]", target):
        if current is None:
            return None
        target = os.path.join(current, target)
    resolved = os.path.normpath(target)
    return resolved if os.path.isdir(resolved) else None


def assignments_of(segment):
    """The `(name, value)` pairs a segment that only assigns variables sets, or None when it runs
    anything. A value the shell would still expand or unescape is None: it is not known here."""
    if any(token in segment for token in ("$(", "`", "<(", ">(")):
        return None
    pairs, rest = [], segment.strip()
    while rest:
        match = ENV_ASSIGNMENT_RE.match(rest)
        if not match:
            return None
        raw = match.group(2)
        value, quoted = unquoted_word(raw)
        if raw.startswith("'") and quoted:
            known = True
        elif quoted:
            # Inside double quotes a backslash escapes only these, and nothing tilde-expands.
            known = not (re.search(r"\\[\\\"$`]", value) or "$" in value
                         or value.startswith("~"))
        else:
            known = "\\" not in value and "$" not in value
        pairs.append((match.group(1), value if known else None))
        rest = rest[match.end():]
    return pairs


def directory_plan(command, cwd, shell="Bash"):
    """Where a command's literal directory changes take it: `(start, targets)`.

    `targets` are the directories it changes into, in order. `start` is where its first segment
    that runs anything runs, when only variable assignments and literal directory changes come
    before it; None when that cannot be read. A change the text does not spell completely loses
    the thread until a later absolute one picks it up again.
    """
    pairs = separated_segments(command, shell)
    if pairs is None:
        return None, []
    current = os.path.normpath(cwd) if cwd else None
    stack, targets, start, started = [], [], None, False
    before = ""
    variables = {}
    for segment, after in pairs:
        text = segment.strip()
        if not text or text.startswith("#"):
            # Nothing runs in an empty segment or a comment line.
            before = after or before
            continue
        opaque = "<<" in text or text[0] in "({"
        head, rest = command_head(text)
        detached = "|" in (before, after) or after == "&"
        before = after
        if not opaque and not detached and head in DIRECTORY_COMMANDS:
            target = literal_directory(rest, current, shell, variables)
            stack.append(current)
            current = target
            if target:
                targets.append(target)
            continue
        if not opaque and not detached and head in RETURN_COMMANDS:
            current = stack.pop() if stack else None
            continue
        assigned = None if opaque or head else assignments_of(text)
        if assigned is not None:
            for name, value in assigned:
                # An assignment inside a pipeline or a background job stays in its subshell.
                if value is None or detached:
                    variables.pop(name, None)
                else:
                    variables[name] = value
            continue
        if head in VARIABLE_WRITERS or ASSIGNMENT_ANYWHERE_RE.search(text) or "$((" in text:
            variables.clear()
        if not started:
            started, start = True, current
        if opaque:
            # A heredoc body is not shell, and a subshell's changes stay inside it.
            break
    return (start if started else current), targets


# A verdict covers content, not edit events: the marker keeps a fingerprint of its lasting
# paths at every durable change, so an edit that was reverted leaves the approved content — and
# the approval — in place. The index is read only for a candidate of up to INDEXED_FILES paths,
# and a wider one is measured on content alone rather than not at all; only a file or a total too
# large to read leaves the fingerprint unknown, and freshness then falls back to the timestamp
# rule.
FINGERPRINT_INDEXED_FILES = 64
FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024
FINGERPRINT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
# How much path history one marker keeps: the candidate's own paths and the fingerprint's domain
# are truncated alike, so the two stay comparable.
MARKER_PATH_CAP = 128
CONTENT_MARKS_KEPT = 32
SHELL_READ_ONLY = "READ_ONLY"
SHELL_VALIDATION = "VALIDATION"
SHELL_UNKNOWN = "UNKNOWN_OR_MUTATING"
# Backstop for a session resumed long after its candidate was left open: the identity check
# below cannot see a candidate that was abandoned on the branch the session is still sitting on,
# and outside a repository it is the only mechanism there is.
CANDIDATE_IDLE_LIMIT = 8 * 3600
# Subagents whose definitions leave them no way to edit (no Edit or Write tool) and whose contract
# is to read: the review lane may run `git show` or grep while it reviews. A hook fired inside a
# subagent names it (`agent_type`), and runs under the parent's session, so these commands landed in
# the parent's candidate: a reviewer's `… | sed … | sort | uniq -d` that the snapshot could not
# resolve under load expired the very verdict the reviewer was producing (report a1c7b71b). The names
# are the agent types the harness reports (code.claude.com/docs/en/hooks, "Common input fields": a hook
# inside a subagent gets `agent_id` and `agent_type`); a renamed profile silently falls back to the
# ordinary rules, which is the conservative side.
READ_ONLY_LANES = frozenset(("adversarial-reviewer", "explore", "plan"))


def read_only_lane(data):
    """Whether the hook fired inside a subagent that only reads: its commands prove no write unless
    the snapshot measures one."""
    return str(data.get("agent_type") or "").strip().lower() in READ_ONLY_LANES


def shell_write(data):
    """Recognize common shell-based file mutation without persisting the command text."""
    tool = str(data.get("tool_name") or "")
    if tool not in cwg.SHELL_TOOLS:
        return False
    command = str((data.get("tool_input") or {}).get("command") or "")
    cleaned = HARMLESS_REDIRECT_RE.sub(" ", join_continuations(command, tool))
    return any(SHELL_WRITE_RE.search(segment) for segment in split_unquoted(cleaned, shell=tool))


def command_head(segment):
    """The executable a pipeline segment starts with (lower-case basename, no `.exe`), and its
    arguments. Environment assignments and timing wrappers in front of it are skipped."""
    segment = segment.strip().lstrip("({!").strip()
    while True:
        stripped = WRAPPER_RE.sub("", ENV_ASSIGNMENT_RE.sub("", segment, count=1), count=1)
        if stripped == segment:
            break
        segment = stripped
    words = segment.split(None, 1)
    if not words:
        return "", ""
    token = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if token.endswith(".exe"):
        token = token[:-4]
    return token, (words[1] if len(words) > 1 else "")


SAFE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,39}$")


def command_label(command, shell="Bash"):
    """What the ledger keeps of a command: its first executable's name, or nothing recognizable.

    A segment that only assigns a variable (`REVIEW_ID=r2; cd …`) runs nothing and is passed
    over. A first token that is not a plain executable name — a PowerShell assignment such as
    `$token='…'`, a quoted path with spaces — is not copied at all, so no value it carries can
    reach the ledger.
    """
    if not command.strip():
        return ""
    # Split whole: an unsplit tail would hand `command_head` a separator inside an assignment.
    for segment in shell_segments(command, shell=shell):
        head = command_head(segment)[0]
        if head:
            return head if SAFE_LABEL_RE.match(head) else "(unrecognized)"
    return "(unrecognized)"


QUOTING_MARKS_RE = re.compile(r"[\"'\\`]")


def without_quote_marks(arguments):
    """The arguments as the program receives their words, quote marks and escapes gone: a quoted
    `"-delete"`, an escaped `\\-delete` or `"--output=f"` is still that option. Dropping characters
    never takes the space in front of an option, so a guard only ever sees more."""
    return QUOTING_MARKS_RE.sub("", arguments)


def git_read_only(arguments):
    """Whether a git invocation only reads: a reading subcommand, a listing form, no output file."""
    arguments = GIT_GLOBAL_OPTIONS_RE.sub("", arguments.strip())
    if MUTATING_ARGS["git"].search(without_quote_marks(arguments)):
        return False
    words = arguments.split()
    subcommand = words[0].lower() if words else ""
    if subcommand == "reflog":
        return len(words) == 1 or words[1] == "show"
    return subcommand in GIT_READ_SUBCOMMANDS or bool(GIT_LISTING_RE.match(arguments))


def bookkeeping_command(executable):
    """The subcommands that write only a bookkeeping tool's own state, when the executable (by name
    or by path) is such a tool; an empty set otherwise."""
    name = unquoted_word(executable)[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return BOOKKEEPING_COMMANDS.get(re.sub(r"\.(?:exe|cmd|bat)$", "", name), frozenset())


def bookkeeping_segment(segment, known, shell="Bash"):
    """Whether a segment runs one of a bookkeeping tool's own-state subcommands — the tool named or
    spelled as a path, or reached through a variable in `known` (name to those subcommands) — or a
    state-only hook script an interpreter runs. Its free text may carry brackets and parentheses,
    which execute only outside quotes, so `EXECUTING_ARGUMENT_RE` is asked about the text with the
    quoted parts blanked; text whose quotes cannot be read is not proven."""
    text = segment.strip()
    first = SHELL_WORD_RE.match(text)
    if not first:
        return False
    word = first.group(0)
    second = SHELL_WORD_RE.match(text[first.end():].lstrip())
    subcommand = unquoted_word(second.group(0))[0].lower() if second else ""
    reference = None if word.startswith("'") else VARIABLE_REFERENCE_RE.fullmatch(unquoted_word(word)[0])
    if reference:
        runs = subcommand in known.get(reference.group(1) or reference.group(2), ())
    elif bookkeeping_command(word):
        runs = subcommand in bookkeeping_command(word)
    else:
        # Only an interpreter can run a hook script; anything else needs no parse of the segment.
        interpreter = unquoted_word(word)[0].replace("\\", "/").rsplit("/", 1)[-1]
        span = bookkeeping_script(text, bridge=True) if INTERPRETER_RE.match(interpreter) else None
        script = unquoted_word(text[span[0]:span[1]])[0].replace("\\", "/") if span else ""
        runs = False
        if script:
            allowed = STATE_ONLY_SCRIPTS[os.path.splitext(script.rsplit("/", 1)[-1])[0].lower()]
            after = SHELL_WORD_RE.match(text[span[1]:].lstrip())
            runs = allowed is None or (after is not None and unquoted_word(after.group(0))[0].lower() in allowed)
    arguments = blank_quoted(text[first.end():], shell) if runs else None
    return arguments is not None and not EXECUTING_ARGUMENT_RE.search(arguments)


# A command whose every write lands in a throwaway file writes nothing lasting: an MR description
# written into the session scratchpad through a quoted heredoc and read back with `$(cat …)`
# expired the verdict it followed (report dc30d302). A heredoc's body is data the command reads on
# stdin; a quoted delimiter keeps the shell out of it. `<<<` is a here-string, which has no body.
HEREDOC_OPERATOR_RE = re.compile(
    r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|(\\)?([A-Za-z_][\w.-]*))"
)
# `$(cat <one file>)` only reads; any other substitution may run anything, one nested in the
# file's name included.
READ_SUBSTITUTION_RE = re.compile(
    r"\$\(\s*cat\s+(?:'[^']*'|\"(?:[^\"`$\\]|\$(?!\())*\"|"
    r"(?:[^\s\"'`$()<>|;&\\]|\$\{?[A-Za-z_]\w*\}?)+)\s*\)"
)
OUTPUT_REDIRECT_RE = re.compile(r"(?:&|[0-9])?>>?")
# The whole word a redirect writes to: quoted and bare parts run together (`"$S"/x` is one word).
REDIRECT_TARGET_RE = re.compile(r"(?:'[^']*'|\"(?:[^\"\\]|\\.)*\"|\\.|[^\s'\"<>|;&()\\])+")
WORD_PART_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|([^'\"]+)")
ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:)?/")
# Variables the reading tools consult: one already exported keeps the export when a later segment
# assigns it, so a throwaway path given to one of these names is no plain shell variable.
TOOL_VARIABLE_RE = re.compile(
    r"(?i)^(?:(?:GIT|RIPGREP|RG|GLAB|GH|LD|DYLD|PYTHON|NODE|NPM|BASH|MSYS|LESS|XDG|CODEX|OPENAI)|"
    r"(?:PAGER|EDITOR|VISUAL|BROWSER|SHELL|ENV|IFS|PS4|SHELLOPTS|PROMPT_COMMAND|CDPATH|HOME|PATH|"
    r"TMPDIR|TEMP|TMP)$)"
)
# A PowerShell variable given a literal, or the two encoding variables a Python call needs: nothing
# runs (report c2a8dfe0). Any other `$env:` name could steer the programs after it.
POWERSHELL_LITERAL_ASSIGNMENT_RE = re.compile(
    r"(?i)^\$(?:(?!env:)[a-z_]\w*|env:(?:PYTHONUTF8|PYTHONIOENCODING))\s*=\s*"
    r"(?:'(?:[^']|'')*'|\"[^\"`$]*\"|\d+)$"
)


def scan_quotes(text, shell="Bash", single_only=False, quote=None):
    """`blank_quoted` over text that may open inside a quote: the text with its quoted spans blanked
    and the quote still open at its end, or None when bash's `$'…'` would take backslash escapes
    this reader does not follow."""
    escape = "`" if shell == "PowerShell" else "\\"
    out, index = list(text), 0
    while index < len(text):
        char = text[index]
        blank = quote is not None and not (single_only and quote == '"')
        if char == escape and quote != "'" and index + 1 < len(text):
            if blank:
                out[index] = out[index + 1] = " "
            index += 2
            continue
        if quote is not None:
            if blank:
                out[index] = " "
            if char == quote:
                quote = None
        elif char == "$" and shell != "PowerShell" and text[index + 1:index + 2] == "'":
            return None
        elif char in "'\"":
            quote = char
            if not (single_only and char == '"'):
                out[index] = " "
        index += 1
    return "".join(out), quote


def blank_quoted(text, shell="Bash", single_only=False):
    """The text with its quoted spans, quotes included, turned into spaces of the same length — only
    the single-quoted ones with `single_only`, where nothing expands; None when a quote never
    closes or opens as bash's `$'…'`. The shell's escape character protects the next character
    outside single quotes."""
    scanned = scan_quotes(text, shell, single_only)
    return None if scanned is None or scanned[1] is not None else scanned[0]


def without_heredoc_bodies(text):
    """The command with each heredoc's body cut out and its operator dropped, or None when that
    cannot be read safely: a body that would still be expanded and could run code (an unquoted
    delimiter with `$(` or a backtick in it), one that never ends, a line continued past an
    operator or out of a body (a continuation moves where the body starts or where the shell
    looks for its end), or an operator after a `#`, where the shell sees a comment and runs the
    lines taken for a body. Read before continuations are joined, and with quotes carried from
    line to line: a `<<` inside a quote is text."""
    if "<<" not in text:
        return text
    lines, kept, index, quote = text.split("\n"), [], 0, None
    while index < len(lines):
        line = lines[index]
        index += 1
        scanned = scan_quotes(line, quote=quote)
        if scanned is None:
            return None
        outside, quote = scanned
        found = [match for match in HEREDOC_OPERATOR_RE.finditer(line)
                 if outside[match.start():match.start() + 2] == "<<"]
        if found and ("#" in outside[:found[-1].start()] or line.endswith("\\")):
            return None
        for match in found:
            quoted = match.group(2) or match.group(3) or (match.group(5) if match.group(4) else None)
            delimiter = quoted or match.group(5)
            body = []
            while index < len(lines) and (lines[index].lstrip("\t") if match.group(1) else lines[index]) != delimiter:
                body.append(lines[index])
                index += 1
            if index >= len(lines) or any(entry.endswith("\\") for entry in body):
                return None
            index += 1
            if not quoted and any(token in "\n".join(body) for token in ("$(", "`")):
                return None
        for match in reversed(found):
            line = line[:match.start()] + " " + line[match.end():]
        kept.append(line)
    return "\n".join(kept)


def split_redirects(segment, shell="Bash"):
    """The segment without its output redirects, and the words they write to; None when a `>` has
    no word after it. A `>` inside quotes is text."""
    outside = blank_quoted(segment, shell)
    if outside is None:
        return None
    targets, spans = [], []
    for operator in OUTPUT_REDIRECT_RE.finditer(outside):
        if spans and operator.start() < spans[-1][1]:
            continue
        start = operator.end() + len(segment[operator.end():]) - len(segment[operator.end():].lstrip())
        word = REDIRECT_TARGET_RE.match(segment, start)
        if not word:
            return None
        targets.append(word.group(0))
        spans.append((operator.start(), word.end()))
    for start, end in reversed(spans):
        segment = segment[:start] + " " + segment[end:]
    return segment, targets


def resolve_word(word, literals):
    """The path a shell word spells — its quoted and bare parts joined, `$NAME` taken from
    `literals` — or None when anything else in it would expand: a substitution, an escape, a
    variable not in `literals`, a glob outside quotes."""
    parts, covered = [], 0
    for match in WORD_PART_RE.finditer(word):
        if match.start() != covered:
            return None
        covered = match.end()
        single, double, bare = match.groups()
        if single is not None:
            parts.append(single)
            continue
        text = bare if double is None else double
        if any(token in text for token in ("`", "$(", "\\")) or (
                double is None and any(token in text for token in "*?[")):
            return None
        unknown = []

        def expand(reference):
            name = reference.group(1) or reference.group(2)
            if name not in literals:
                unknown.append(name)
                return ""
            return literals[name]

        resolved = VARIABLE_REFERENCE_RE.sub(expand, text)
        if unknown or "$" in resolved:
            return None
        parts.append(resolved)
    return "".join(parts) if covered == len(word) else None


def throwaway_target(path):
    """Whether writing to this path leaves nothing lasting: an absolute throwaway path with no `.` or
    `..` step, a Git Bash `/c/…` spelling read as its drive. A relative path lands wherever the
    shell stands, and a step back out of a throwaway directory reaches anything."""
    path = MSYS_DRIVE_RE.sub(r"\1:", cwg.normalize_path(path))
    return bool(ABSOLUTE_PATH_RE.match(path) and not {".", ".."} & set(path.split("/"))
                and cwg.is_ephemeral(path))


def bare_brace(outside):
    """Whether text with its quotes blanked holds a brace group or an expansion: `${NAME}` is only
    a variable."""
    return "{" in outside and "{" in VARIABLE_REFERENCE_RE.sub(" ", outside)


def read_only_pipeline(command, shell="Bash"):
    """Whether every segment of the command is proven not to write a lasting artifact, arguments
    included: a command that only reads, or a bookkeeping tool that writes only its own state. A
    write into a throwaway file — spelled literally, or through a variable the same command gave a
    throwaway path — writes nothing lasting, a heredoc's body is data, `$(cat <file>)` only reads,
    and text inside quotes spells no shell syntax."""
    # A heredoc's body is cut out before continuations are joined: a quoted body keeps its
    # backslashes, and joining one would move where the reader finds its end.
    if shell != "PowerShell":
        command = without_heredoc_bodies(str(command or ""))
        if command is None:
            return False
    # Read as the shell reads it: a form broken across a continuation is one token, one redirect,
    # one command. Everything below then asks its question of the text that will actually run.
    joined = READ_SUBSTITUTION_RE.sub(" _read_ ", join_continuations(command, shell))
    # A substitution or a backtick expands unless single quotes hold it; the other forms are syntax
    # only outside quotes.
    code, outside = blank_quoted(joined, shell, single_only=True), blank_quoted(joined, shell)
    if code is None or outside is None or "$(" in code or "`" in code:
        return False
    if any(token in outside for token in ("<(", ">(", "<<")) or bare_brace(outside):
        return False
    # A merged stderr (`2>&1`) is dropped before splitting, or its `&` would cut the pipeline.
    cleaned = HARMLESS_REDIRECT_RE.sub(" ", joined)
    # Quotes that never balance leave no separator readable: every segment then counts as part of
    # a pipeline, where an assignment reaches nothing after it.
    pairs = separated_segments(cleaned, shell) or [
        (segment, "|") for segment in shell_segments(cleaned, shell=shell)
    ]
    known, literals, before = {}, {}, ""
    for segment, after in pairs:
        text = segment.strip()
        # A pipeline's segments and a background job run in subshells: an assignment made there
        # never reaches the segments after it.
        detached = "|" in (before, after) or after == "&"
        before = after
        if not text:
            continue
        if shell == "PowerShell" and POWERSHELL_LITERAL_ASSIGNMENT_RE.match(text):
            continue
        split = split_redirects(text, shell)
        if split is None:
            return False
        text, targets = split[0].strip(), split[1]
        if not all(throwaway_target(resolve_word(target, literals)) for target in targets):
            return False
        if not text:
            continue
        head, rest = command_head(text)
        # A segment that only gives a variable a bookkeeping tool's literal path runs nothing, and
        # the variable then names that tool; one that gives it a throwaway path lets a redirect
        # through it be read. Any other assignment stays unproven below.
        # `NAME=value` assigns only in bash; PowerShell would run it as a command.
        if not detached and not head and shell != "PowerShell":
            assigned = assignments_of(text)
            if assigned and all(value is not None and bookkeeping_command(value) for _, value in assigned):
                known.update((name, bookkeeping_command(value)) for name, value in assigned)
                continue
            if assigned and all(value is not None and cwg.is_ephemeral(value) and not TOOL_VARIABLE_RE.match(name)
                                for name, value in assigned):
                literals.update(assigned)
                continue
        # An environment assignment can redirect a reading command to an external program
        # (`GIT_EXTERNAL_DIFF`, `RIPGREP_CONFIG_PATH`), so a prefixed segment is not proven.
        if ENV_ASSIGNMENT_RE.match(text.lstrip("({!").strip()):
            return False
        if bookkeeping_segment(text, known, shell):
            continue
        # `printf -v` reads as a reading command and can still reassign a variable.
        if head in VARIABLE_WRITERS or ASSIGNMENT_ANYWHERE_RE.search(text) or "$((" in text:
            known.clear()
            literals.clear()
        arguments = blank_quoted(rest, shell)
        if arguments is None or EXECUTING_ARGUMENT_RE.search(arguments):
            return False
        if head == "git":
            if git_read_only(rest):
                continue
            return False
        if head == "gh":
            if GH_READ_RE.match(rest.strip()):
                continue
            return False
        if head == "glab":
            if GLAB_READ_RE.match(rest.strip()):
                continue
            return False
        if head not in READ_ONLY_COMMANDS:
            return False
        guard = MUTATING_ARGS.get(head)
        if guard and guard.search(without_quote_marks(rest)):
            return False
        # `uniq input output` writes its second operand.
        if head == "uniq" and len([w for w in rest.split() if not w.startswith("-")]) > 1:
            return False
    return True


# The review lane's launch, as `commands/adversarial-review.md` spells it: a bare `codex` (a path
# could name any program), optionally behind `timeout`.
REVIEW_EXEC_RE = re.compile(
    r"^(?:timeout\s+\d+[smhd]?\s+)?codex(?:\.exe)?\s+exec(?P<resume>\s+resume)?(?=\s|$)",
    re.IGNORECASE,
)
# `codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]` takes the session before or after the
# options (report 27c9dcd0); it never starts with a dash, so no option passes for one.
REVIEW_SESSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]*$")
# The options its template passes, each with the value it takes; any other — `-o <file>` writes
# one — is no pure launch.
REVIEW_FLAGS = frozenset(("-", "--ignore-user-config", "--skip-git-repo-check",
                          "--dangerously-bypass-approvals-and-sandbox"))
REVIEW_VALUE_OPTIONS = {
    "--disable": re.compile(r"^[a-z0-9_]+$"),
    "-m": re.compile(r"^[\w.-]+$"),
    "-c": re.compile(r"^[\w.]+=[\w.-]+$"),
    "-s": re.compile(r"^read-only$"),
    "--sandbox": re.compile(r"^read-only$"),
}
REVIEW_ID_RE = re.compile(r"^[\w.-]+$")


def review_launch_only(command, shell="Bash"):
    """Whether a shell command does nothing but launch the Codex review lane: `REVIEW_ID=<literal>`,
    at most one `cd` to a literal path, and one `codex exec` fed its packet on stdin (the one
    stdin redirect `codex_launch` binds) with the template's options and stderr in a throwaway
    file, joined by `;`, `&&` or a newline, marked as a review.
    Such a launch changes nothing a candidate holds — the lane is trusted to stay read-only exactly
    as the native reviewer is — so it expires no verdict wherever it starts, its own included: a
    candidate outside any repository had every Codex verdict expire at its own launch (report
    a5767180). A pipe, a background `&`, a substitution, another program, another option or a
    variable Codex itself reads makes it an ordinary command."""
    command = str(command or "")
    bound = codex_launch(command)["path"] if shell != "PowerShell" and cwg.REVIEW_INTENT_TOKEN in command else ""
    if not bound:
        return False
    text = join_continuations(command, shell)
    outside = blank_quoted(text, shell)
    if outside is None:
        return False
    # A comment runs from its `#` to the end of its line, not of the command.
    comment = COMMENT_START_RE.search(outside)
    while comment:
        start = outside.index("#", comment.start())
        end = text.find("\n", start)
        end = len(text) if end < 0 else end
        text, outside = text[:start] + text[end:], outside[:start] + outside[end:]
        comment = COMMENT_START_RE.search(outside, max(start - 1, 0))
    # A substitution or a backtick expands inside double quotes too.
    code = blank_quoted(text, shell, single_only=True)
    if (code is None or "$(" in code or "`" in code
            or any(token in outside for token in ("<(", ">(", "<<", "|"))
            or "&" in outside.replace("&&", "") or bare_brace(outside)):
        return False
    literals, launch, moved = {}, None, False
    for segment in split_unquoted(text, shell=shell):
        segment = segment.strip()
        if not segment:
            continue
        if launch is not None:
            return False
        assigned = assignments_of(segment)
        if assigned:
            if not all(name == "REVIEW_ID" and value and REVIEW_ID_RE.match(value) for name, value in assigned):
                return False
            literals.update(assigned)
            continue
        words = [word.group(0) for word in SHELL_WORD_RE.finditer(segment)]
        if words[0] == "cd":
            if moved or len(words) != 2 or resolve_word(words[1], literals) is None:
                return False
            moved = True
            continue
        launch = segment
    split = split_redirects(launch or "", shell)
    if not split or not split[1] or not all(throwaway_target(resolve_word(target, literals)) for target in split[1]):
        return False
    # The packet this launch reads is the one the Stop hook binds its verdict to: `codex_launch`
    # reads the raw text, where an assignment inside a comment would name another file.
    fed = STDIN_REDIRECT_RE.search(split[0])
    packet = resolve_word(fed.group(1), literals) if fed else None
    if not packet or MSYS_DRIVE_RE.sub(r"\1:", cwg.normalize_path(packet)) != cwg.normalize_path(bound):
        return False
    rest = STDIN_REDIRECT_RE.sub(" ", split[0], count=1).strip()
    head = REVIEW_EXEC_RE.match(rest)
    if not head:
        return False
    words, index, session = rest[head.end():].split(), 0, None
    while index < len(words):
        if words[index] in REVIEW_FLAGS:
            index += 1
            continue
        # A resumed round names its session once, or asks for the last one.
        if head.group("resume") and session is None and (
                words[index] == "--last" or REVIEW_SESSION_RE.match(words[index])):
            session = words[index]
            index += 1
            continue
        value = REVIEW_VALUE_OPTIONS.get(words[index])
        if value is None or index + 1 >= len(words) or not value.match(words[index + 1]):
            return False
        index += 2
    return True


def write_capable(data):
    """Whether this shell command could have rewritten a file the snapshot did not see.

    Broader than `shell_write`, which names the shapes that definitely write and therefore keep
    their whole delta in `own_delta`; this one only decides whether an unresolved mutation may
    expire a review verdict. Only a pipeline of commands proven not to write a lasting artifact —
    readers, and bookkeeping tools that write only their own state — is outside it, plus a
    validation command: reruns of the checks the skill asks for never edit source. A write the
    pipeline proves lands only in throwaway files is outside it too (report dc30d302), and so is
    a command that only launches the review lane (`review_launch_only`).
    """
    if str(data.get("tool_name") or "") not in cwg.SHELL_TOOLS:
        return False
    command = str((data.get("tool_input") or {}).get("command") or "")
    if review_launch_only(command, str(data.get("tool_name") or "")):
        return False
    if shell_write(data):
        return not read_only_pipeline(command, str(data.get("tool_name") or ""))
    if not command.strip():
        return False
    if VALIDATION_SHELL_RE.match(command) and "$(" not in command:
        return False
    return not read_only_pipeline(command, str(data.get("tool_name") or ""))


def only_own_state(data):
    """Whether this shell command proves it writes nothing lasting: readers, bookkeeping tools that
    write only their own state, writes that land only in throwaway files, and the review lane's
    launch. Stricter than `not write_capable`, which also lets a validation command through: a
    verdict survives a test run's unresolved mutation, but a test run can still write a lasting file
    no snapshot sees, so it moves the clock an invisible change is judged by (`last_write_ts`)."""
    tool = str(data.get("tool_name") or "")
    command = str((data.get("tool_input") or {}).get("command") or "")
    return tool in cwg.SHELL_TOOLS and (review_launch_only(command, tool) or read_only_pipeline(command, tool))


def shell_policy(data):
    """Classify shell calls without treating successful validation as a write."""
    if str(data.get("tool_name") or "") not in cwg.SHELL_TOOLS:
        return SHELL_UNKNOWN
    command = str((data.get("tool_input") or {}).get("command") or "")
    if shell_write(data):
        return SHELL_UNKNOWN
    if READ_ONLY_SHELL_RE.match(command):
        return SHELL_READ_ONLY
    if VALIDATION_SHELL_RE.match(command) and "$(" not in command:
        return SHELL_VALIDATION
    return SHELL_UNKNOWN


def shell_read_only(data):
    return shell_policy(data) == SHELL_READ_ONLY


def shell_snapshot_path(data):
    identity = "{}_{}".format(
        data.get("session_id") or "unknown",
        data.get("tool_use_id") or "pending",
    )
    return os.path.join(
        tempfile.gettempdir(),
        "cwg_shell_{}.json".format(cwg.session_key(identity)),
    )


def run_git(cwd, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            check=False,
            timeout=4,
        )
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def nul_paths(raw):
    if raw is None:
        return None
    return [
        os.fsdecode(item)
        for item in raw.split(b"\0")
        if item
    ]


def file_token(path):
    try:
        stat = os.stat(path, follow_symlinks=False)
        digest = hashlib.sha256()
        if stat.st_size <= 10 * 1024 * 1024 and os.path.isfile(path):
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            content = digest.hexdigest()
        else:
            content = "metadata"
        return "{}:{}:{}".format(stat.st_size, stat.st_mtime_ns, content)
    except FileNotFoundError:
        return "missing"
    except Exception:
        return "unreadable"


def git_snapshot(cwd):
    root_raw = run_git(cwd, "rev-parse", "--show-toplevel")
    if root_raw is None:
        return None
    root = os.fsdecode(root_raw).strip()
    sources = (
        ("worktree", ("diff", "--name-only", "-z", "--")),
        ("index", ("diff", "--cached", "--name-only", "-z", "--")),
        ("untracked", ("ls-files", "--others", "--exclude-standard", "-z")),
    )
    # PERF: ignored files stay unlisted. `ls-files --ignored` measured 19s here (3.8s even with
    # --directory) against 0.37s for the untracked listing, and a timeout fails the whole
    # snapshot open. A shell write onto a gitignored .env therefore grades as operational work;
    # the same edit through Edit/Write still grades HIGH.
    flags = {}
    for label, args in sources:
        names = nul_paths(run_git(root, *args))
        if names is None:
            return None
        for name in names:
            normalized = cwg.normalize_path(name)
            flags.setdefault(normalized, []).append(label)
    if len(flags) > 4096:
        return {"root": root, "overflow": True, "files": {}}
    files = {}
    for relative, labels in flags.items():
        absolute = os.path.join(root, *relative.split("/"))
        files[relative] = {
            "flags": sorted(labels),
            "token": file_token(absolute),
        }
    return {"root": root, "overflow": False, "files": files}


def agent_config_roots():
    """The agent-configuration homes, honouring the environment overrides the tools read."""
    return (
        cwg.config_home(),
        cwg.codex_home(),
        os.path.join(os.path.expanduser("~"), ".agents"),
    )


def file_metadata(stat):
    """What the configuration snapshot stores per file: enough to see that it was written."""
    return "{}:{}".format(stat.st_size, stat.st_mtime_ns)


def scan_config_tree(directory, files):
    """Record every gated file under one configuration directory.

    False means the tree could not be read, and the snapshot must then prove nothing rather
    than report it clean: an absent directory is ordinary — most homes have only some of these
    — but one that exists and cannot be listed would otherwise read as "nothing changed here",
    which is the one answer that must never be guessed. The Git snapshot fails the same way.
    """
    stack = [directory]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except (FileNotFoundError, NotADirectoryError):
            # Absent, or a plain file standing where a tree would be: either way it holds no
            # gated file. Refusing every command until someone finds it would be worse.
            continue
        except OSError:
            return False
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    # Dot-prefixed and named-vendor trees alike: see AGENT_CONFIG_SKIP.
                    if not (entry.name.startswith(".")
                            or entry.name.lower() in AGENT_CONFIG_SKIP):
                        stack.append(entry.path)
                    continue
                normalized = cwg.normalize_path(entry.path)
                if not cwg.is_gated(normalized):
                    continue
                # On Windows the directory listing already carries size and timestamps, so this
                # costs no further syscall — which is what keeps the scan affordable per call.
                stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                # Written and removed inside the command: the other snapshot will not hold it
                # either, so the pair still agrees.
                continue
            except OSError:
                return False
            files[normalized] = file_metadata(stat)
            if len(files) > AGENT_CONFIG_LIMIT:
                return False
    return True


def settings_digest(path):
    """A digest of a settings file without the keys the app writes itself (`APP_SETTINGS_KEYS`),
    or None when the file is absent or not a JSON object."""
    data = cwg.read_json(path)
    if data is None:
        return None
    kept = {key: value for key, value in data.items() if key not in APP_SETTINGS_KEYS}
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode("utf-8")).hexdigest()


def config_snapshot():
    """Size and mtime of the agent-configuration files a shell command could rewrite.

    Metadata rather than content, unlike the Git snapshot: that one hashes the handful of files
    Git already reports as changed, while this one looks at every file in the trees on every
    shell call. The question here is only whether this command wrote here, which is what size
    and mtime answer. Settings files also keep the digest of everything but the app's own keys,
    so a rewrite the app made from its interface can be told apart.
    """
    files = {}
    settings = {}
    roots = []
    for root in agent_config_roots():
        if not os.path.isdir(root):
            continue
        for name in AGENT_CONFIG_DIRS:
            tree = os.path.join(root, name)
            if not scan_config_tree(tree, files):
                return {"overflow": True, "roots": [], "files": {}}
            # Each watched tree vouches for itself. Claiming the home instead would vouch for
            # `plugins/` and every other pocket this scan never opens, and an empty delta would
            # then read as proof that a hook nobody looked at is unchanged.
            roots.append(cwg.normalize_path(tree))
        for name in AGENT_CONFIG_FILES:
            path = os.path.join(root, name)
            # Watched whether or not it exists right now: creating or deleting a settings file
            # is the event to catch, and letting its existence decide what the snapshot covers
            # would make exactly that write incomparable instead of naming it.
            roots.append(cwg.normalize_path(path))
            try:
                stat = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            files[cwg.normalize_path(path)] = file_metadata(stat)
            if name in SETTINGS_FILES:
                settings[cwg.normalize_path(path)] = settings_digest(path)
    return {"overflow": False, "roots": roots, "files": files, "settings": settings}


def shell_snapshot(cwd, marker=None, directories=(), origin=None):
    """Everything a shell command could change that the marker is able to name.

    Given the open marker, it also covers where the candidate lives when that is not under the
    command's directory: a session publishes from one worktree what it wrote in another, or keeps
    helper scripts beside a checkout. Those repositories are snapshotted too, and each lasting file
    that belongs to no repository gets a token of its own. `directories` are the ones the command
    itself changes into, and their repositories are snapshotted as well; `origin` is the directory
    the hook was given when the command starts elsewhere, whose repository the command can still
    write to. Trees past the time budget are left out, and so stay unmeasured.
    """
    started = time.monotonic()
    snapshot = {"git": git_snapshot(cwd), "config": config_snapshot()}
    own = cwg.normalize_path((snapshot["git"] or {}).get("root") or "").rstrip("/")
    open_marker = isinstance(marker, dict) and not marker.get("closed")
    roots, loose = candidate_trees(marker, own) if open_marker else ([], [])
    cache, entered = {}, []
    for directory in directories:
        root = repository_root(directory, cache)
        if root and root != own and root not in roots and root not in entered:
            entered.append(root)
    roots = roots + entered[:MAX_DIRECTORY_REPOSITORIES]
    base = repository_root(origin, cache) if origin else ""
    if base and base != own and base not in roots:
        roots.append(base)
    if open_marker or roots:
        snapshot["repos"] = []
        for root in roots:
            if time.monotonic() - started > EXTRA_SNAPSHOT_BUDGET:
                snapshot["skipped"] = len(roots) - len(snapshot["repos"])
                break
            snapshot["repos"].append(git_snapshot(root))
        snapshot["loose"] = {path: file_token(path) for path in loose}
    return snapshot


# How far a command's snapshot follows the candidate out of its directory. Each repository costs a
# Git snapshot inside the PreToolUse budget; a path past these stays unmeasured, and a write-capable
# command then expires the verdict, which is the conservative side.
MAX_CANDIDATE_REPOSITORIES = 2
MAX_LOOSE_FILES = 64
# Seconds of a hook's own time after which no further repository is snapshotted or compared: the
# PreToolUse hook has twenty before a shell command and ten before an edit, the PostToolUse hook
# fifteen, and a cancelled hook records nothing at all.
EXTRA_SNAPSHOT_BUDGET = 4.0
EXTRA_COMPARE_BUDGET = 2.5
# The merge judge's own limit, from the same start: it runs only around a merge, costs about six git
# calls, and the record written after it has to fit in what is left of the hook's fifteen seconds. On a
# loaded machine it can run out, and the merge's files are then recorded as they were before the
# judge existed. The environment override is the test suite's, so its verdicts do not ride on load.
def budget_override(name, default):
    """A positive number of seconds from the environment, or the default for anything else."""
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


MERGE_JUDGE_BUDGET = budget_override("CWG_MERGE_JUDGE_BUDGET", 3.5)
# Measuring a closed candidate's files after its receipt: past it, the command is simply recorded.
SETTLE_BUDGET = 3.0


# A subdirectory of a drive-root temp directory holds real clones (C:/tmp/<project>) and agents'
# throwaway helpers (C:/tmp/zdd) side by side, and a repository is what tells them apart. A file
# written there outside any repository is throwaway (report ff2c5007). It is decided when the file
# is written, so the path rules stay pure and a scratch directory removed later does not turn the
# files it held into lasting ones.
DRIVE_TEMP_SUBTREE_RE = re.compile(cwg.DRIVE_TEMP_ROOT + r"[^/]+/")


def scratch_file(path):
    """Whether a written file is a throwaway in a drive-root temp directory, outside any repository."""
    normalized = cwg.normalize_path(path)
    return bool(DRIVE_TEMP_SUBTREE_RE.match(normalized)) and not repository_root(normalized, {})


def repository_root(path, cache):
    """The working tree holding a path — the nearest directory whose `.git` leads to a HEAD — or ''.

    Read from disk, never through git, because it runs for every lasting path before every
    write-capable command; `cache` keeps the answer for each directory visited.
    """
    directory = cwg.normalize_path(path).rstrip("/")
    if directory and not os.path.isdir(directory):
        directory = directory.rsplit("/", 1)[0] if "/" in directory else ""
    visited, root = [], ""
    while directory and "/" in directory:
        if directory in cache:
            root = cache[directory]
            break
        visited.append(directory)
        pointer = head_pointer(directory)
        if pointer and os.path.isfile(pointer):
            root = directory
            break
        directory = directory.rsplit("/", 1)[0]
    for seen in visited:
        cache[seen] = root
    return root


def candidate_trees(marker, own_root):
    """The other repositories holding the marker's lasting paths, and its lasting paths in none.

    A path whose folder is gone is walked up like any other, but the repository found counts only
    if git there does not ignore it or HEAD still holds it. A removed worktree's file was handed to
    the main checkout, which excludes `.claude/worktrees/` and so never held it, and that checkout's
    snapshot then cost every command the extra-repository budget (report a378343e); a tracked file
    deleted with its folder keeps its repository, its deletion staged or not (G24 review). An
    ignored one HEAD does not hold is measured by its own token, which says whether it comes back.
    Git is asked once per such repository, and no answer keeps it.
    """
    watched = watched_trees()
    cache, roots, loose, orphaned = {}, [], [], {}

    def place(root, path):
        if not root:
            if len(loose) < MAX_LOOSE_FILES:
                loose.append(path)
        elif root != own_root and root not in roots:
            roots.append(root)

    for path in cwg.durable_paths(marker.get("paths") or []):
        if any(covers(tree, path, AGENT_CONFIG_SKIP) for tree in watched):
            continue
        root = repository_root(path, cache)
        if root and not os.path.isdir(os.path.dirname(path)):
            orphaned.setdefault(root, []).append(path)
        else:
            place(root, path)
    for root, paths in orphaned.items():
        ignored = ignored_paths(root, paths)
        held = held_in_head(root, ignored) if ignored else set()
        for path in paths:
            place("" if path in ignored and path not in held else root, path)
    return roots[:MAX_CANDIDATE_REPOSITORIES], loose


def held_in_head(root, paths):
    """The normalized `paths` HEAD holds in the repository at `root`, compared the marker's way;
    all of them when git cannot tell. A staged deletion takes a path out of the index, and git then
    calls a force-added file under an ignored folder ignored, though HEAD still holds it."""
    answer = cwg.git_run(root, ["ls-tree", "-r", "-z", "--name-only", "HEAD"], timeout=2.0)
    if not answer or answer[0] != 0:
        return set(paths)
    listed = {cwg.normalize_path(os.path.join(root, name)) for name in answer[1].split("\0") if name}
    return {path for path in paths if path in listed}


def ignored_paths(root, paths):
    """The normalized `paths` git ignores in the repository at `root`; none when it cannot tell."""
    answer = cwg.git_run(root, ["check-ignore", "--stdin", "-z"], timeout=1.5,
                         stdin="".join(path + "\0" for path in paths))
    # Exit 0: the paths it prints are ignored; 1: none is; anything else is no answer.
    if not answer or answer[0] != 0:
        return set()
    return {cwg.normalize_path(name if os.path.isabs(name) else os.path.join(root, name))
            for name in answer[1].split("\0") if name}


def watched_trees():
    """Every tree and file the configuration snapshot watches, normalized."""
    return [
        cwg.normalize_path(os.path.join(root, name))
        for root in agent_config_roots()
        for name in AGENT_CONFIG_DIRS + AGENT_CONFIG_FILES
    ]


def vouching_tree(path, roots):
    """The snapshotted tree a changed path belongs to, with the skip list that vouched for it.

    The skip list travels with the tree because it differs by kind: a repository passes none —
    Git reports every file beneath its root whatever the directory is called — while a
    configuration home walks around its bookkeeping and vendored pockets. Asking the same
    question of a repository with the configuration skip list would read a subdirectory named
    `state`, `plans` or `node_modules` as outside the tree it plainly sits in.
    """
    for root, skip in roots:
        if covers(root, path, skip):
            return root, skip
    return None, ()


def own_delta(session, cwd, candidates, window_start, write_shaped, roots):
    """Split a snapshot delta into what this command answers for and what nobody can name.

    A before/after diff of the working repository and the configuration homes sees every write
    those trees received while the command ran, not only this command's. Another session editing
    the same checkout therefore landed in this candidate, and a session that changed nothing of
    its own could be left holding paths it must then review, simplify and close on — which no
    receipt it can honestly write covers.

    A write-shaped command keeps its whole delta. Its own text says it wrote something, which is
    direct evidence about this session; another session announcing the same path only says that
    session wrote it too. Subtracting on the weaker evidence would let a real in-place edit of a
    sensitive file leave no candidate at all, and losing a durable write out of the gate is worse
    than one extra review round.

    For every other command, two subtractions, in order of how much they prove. A path another
    session announced inside this window is that session's, recorded in its own marker. What is
    left over is charged here unless another session had a shell command in flight *in the same
    tree*: then the change is real, nobody can attribute it, and the only thing tying it to this
    command is that this session happened to look at that tree at that moment. Shared ground is
    the whole test: a command running in an unrelated repository proves nothing about this one,
    and treating every concurrent command anywhere on the machine as a competing writer would
    drop attribution far more often than it would correct it.

    The subtractions differ in what the caller owes afterwards, which is why they are reported
    separately. A settled claim is positively someone else's, recorded in that session's marker,
    and carries no obligation here. Everything else dropped - a write another session announced
    but has not confirmed, or a command it had in flight in this tree - is owned by nobody the
    registry can name: it may well be this command's, so the caller keeps a conservative floor
    for it even though it must not record another session's file. Ambiguity narrows what a
    session is asked about; it must never reduce what a session owes.

    An unconfirmed announcement is deliberately on the ambiguous side. Treating it as ownership
    would mean an edit that was declared and then denied could silently absolve another session
    of a durable write it really made, for as long as the announcement stood.

    A registry too large to read in one scan puts everything left over on the ambiguous side for
    the same reason: what was not read cannot be evidence that nobody else owns it.
    """
    if write_shaped:
        return list(candidates), []
    claimed, announced, busy_dirs, overflow = cwg.foreign_activity(session, window_start)
    mine = []
    ambiguous = []
    for path in candidates:
        normalized = cwg.absolute_path(path, cwd)
        if normalized in claimed:
            continue
        if normalized in announced:
            ambiguous.append(normalized)
            continue
        tree, skip = vouching_tree(normalized, roots)
        if tree and any(covers(tree, busy, skip) for busy in busy_dirs):
            ambiguous.append(normalized)
            continue
        if overflow:
            # More sessions than one scan may read. A path missing from what was read is not
            # thereby this command's: unread is not silent, so it goes where everything else
            # nobody can be shown to own goes.
            ambiguous.append(normalized)
            continue
        # The normalized form, not the raw one: what this command is judged to have written and
        # what it then announces must be the same string, or the two disagree on one path.
        mine.append(normalized)
    return mine, ambiguous


def stored_snapshot(data):
    """One stored pre-command snapshot, including the Git-only shape written before the
    configuration homes were watched: a session upgraded mid-flight must not read as a command
    whose effect could not be resolved."""
    # A Git snapshot always names its root at the top level; the current shape never does.
    if isinstance(data, dict) and "root" in data:
        return {"git": data, "config": None}
    return data if isinstance(data, dict) else {}


def changed_config_paths(before, after):
    """Absolute configuration paths this command rewrote, or None when nothing is provable.

    A settings file whose digest without the app's own keys is the same on both sides was
    rewritten from the app's interface, not by the command, and is left out.
    """
    if (
        not before
        or not after
        or before.get("overflow")
        or after.get("overflow")
        or before.get("roots") != after.get("roots")
    ):
        return None
    earlier, later = before.get("settings") or {}, after.get("settings") or {}
    return [
        path for path in rewritten(before, after)
        if earlier.get(path) is None or earlier.get(path) != later.get(path)
    ]


def rewritten(before, after):
    """Keys whose recorded state differs between two snapshots of the same tree."""
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    return [
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    ]


def snapshot_changes(before, after):
    """Every gated path two snapshots of one repository disagree on, each paired with whether
    the command rewrote its bytes - or None when nothing is provable.

    Staging and committing move a file between the worktree, the index and HEAD without touching
    a byte of it, and every such move makes the two snapshots disagree about it. The path stays
    the candidate's to answer for - it keeps its risk and its work class - but this command did
    not change it, and letting it into the fingerprint's domain is what retired approvals of the
    very bytes being committed (reports 42f294ba, 877f7bf2).

    A path that left the listing - committed, or clean again - is measured against the file on
    disk now, which `git commit` leaves exactly as the snapshot recorded it. A path that only
    appeared counts as rewritten: nothing recorded what it held before, and one extra review
    round is the safe side of that ignorance.
    """
    if (
        not before
        or not after
        or before.get("root") != after.get("root")
        or before.get("overflow")
        or after.get("overflow")
    ):
        return None
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    root = after["root"]
    changes = []
    for relative in rewritten(before, after):
        if not cwg.is_gated(relative) or SYNCED_AGENT_TREE_RE.search("/" + relative):
            continue
        absolute = os.path.join(root, *relative.split("/"))
        was = (before_files.get(relative) or {}).get("token")
        landed = after_files.get(relative)
        now = landed.get("token") if landed else file_token(absolute)
        changes.append((absolute, was is None or was != now))
    return changes


def changed_and_rewritten(changes):
    """The paths `snapshot_changes` reported, and the subset whose bytes the command rewrote."""
    return [path for path, _ in changes], [path for path, bytes_moved in changes if bytes_moved]


# Bringing the project's integration branch into a candidate's branch recorded every upstream file
# as this session's work: 127 paths from 14 merged requests graded the candidate HIGH and demanded
# lanes for code nobody here had written (report f9920b99). Git can state what a merge contributes,
# so the judge compares content rather than trusting the command: a path is set aside only when the
# index and the file on disk hold exactly what `git merge-tree` computes from the HEAD before the
# command and the merged commit, and only when that commit is reachable from a remote's default
# branch. A feature branch, a chip's branch or the session's own push is not upstream, and bringing
# it in records its files as it always did. A write or commit before or after the merge in the same
# command, and a path resolved by hand, leave bytes the merge did not compute and stay recorded.
# The trust is in the ref, not in who wrote what it reaches, and that is what stays open: work the
# session itself pushed to the default branch, the branch a remote that is a local clone had checked
# out when it was cloned, and a remote-tracking ref moved by hand all read as upstream.
OID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
MERGE_BLOB_MODES = frozenset(("100644", "100755"))
MERGE_DELETED_MODE = "000000"


def root_prefix(root):
    """The normalized form of a repository root that its files' normalized paths start with."""
    return cwg.normalize_path(root).rstrip("/") + "/"


def raw_diff_records(listing):
    """{path: (mode, oid)} of the destination side of `git diff-tree`/`diff-index` `-z` raw output.

    Read with `--no-renames`, so every record is one header and one path."""
    fields = listing.split("\0")
    found = {}
    for index in range(0, len(fields) - 1, 2):
        header, path = fields[index].lstrip(":").split(), fields[index + 1]
        if len(header) == 5 and path:
            found[path] = (header[1], header[3])
    return found


def git_result_until(deadline, root, arguments, stdin=None):
    """(exit code, stdout) of `git -C root …` when it finishes before `deadline`, else None."""
    remaining = deadline - time.monotonic()
    return cwg.git_run(root, arguments, timeout=remaining, stdin=stdin) if remaining > 0 else None


def git_until(deadline, root, arguments, codes=(0,), stdin=None):
    """stdout of `git -C root …` when it exits with one of `codes` before `deadline`, else None."""
    result = git_result_until(deadline, root, arguments, stdin)
    return result[1] if result and result[0] in codes else None


def merge_head_path(root):
    """Where the repository at `root` keeps `MERGE_HEAD`: beside its own HEAD, which for a linked
    worktree is its private git directory, not the shared one. None outside a repository."""
    pointer = head_pointer(root) if root else None
    return os.path.join(os.path.dirname(pointer), "MERGE_HEAD") if pointer else None


def merge_in_progress(root):
    """Whether a merge is in progress at `root`, read from disk — no git call."""
    path = merge_head_path(root)
    return bool(path) and os.path.isfile(path)


def merge_outcome(root, before_head, deadline):
    """How a command that ran around a merge left it: `("merged", commit)` or `("abandoned", None)`,
    else `(None, None)`.

    Merged is a merge still in progress on a HEAD the command did not move, with exactly one
    `MERGE_HEAD`, or a merge commit with exactly two parents whose first is `before_head`; the
    commit is the one merged in. Abandoned is no merge in progress and HEAD still `before_head`.
    An octopus, or a HEAD that also moved some other way, is neither.
    """
    line = (git_until(deadline, root, ["rev-list", "--parents", "-n", "1", "HEAD"]) or "").split()
    if not line:
        return None, None
    try:
        with open(merge_head_path(root), encoding="utf-8") as stream:
            heads = stream.read().split()
    except FileNotFoundError:
        heads = None
    except (OSError, TypeError):
        return None, None
    if heads is None:
        if line[0] == before_head:
            return "abandoned", None
        partner = line[2] if len(line) == 3 and line[1] == before_head else None
    else:
        partner = heads[0] if len(heads) == 1 and line[0] == before_head else None
    return ("merged", partner) if partner and OID_RE.fullmatch(partner) else (None, None)


def upstream_commit(root, commit, deadline):
    """Whether `commit` is reachable from a remote's default branch (`refs/remotes/<remote>/HEAD`):
    none of its history lies outside theirs. No remote naming a default branch trusts nothing."""
    outside = git_until(deadline, root, ["rev-list", "-n", "1", commit, "--not", "--glob=refs/remotes/*/HEAD"])
    return outside is not None and not outside.strip()


def merge_set_aside(before, after_git, changed, recorded, deadline):
    """Of `changed` (normalized, absolute), the paths an upstream merge accounts for, in the
    repository the command started in.

    Only a command that found a merge in progress or left one is judged, which costs nothing but a
    look at `MERGE_HEAD` for every other command. `before` is the stored pre-command snapshot — its
    listing, the HEAD it recorded, whether a merge was in progress — and `after_git` the listing after
    the command. A merge of an upstream commit sets aside what it computed (`clean_merge_paths`). A
    merge abandoned on the HEAD it started from returned to HEAD what it had staged: a path that was
    staged and nothing else before the command, is clean now and was never `recorded` by the
    candidate, came from the merge; dirt or untracked files the same command discarded did not.
    Anything git cannot answer before `deadline` sets nothing aside.
    """
    earlier = before.get("git") or {}
    root, before_head = earlier.get("root"), before.get("head")
    if not root or not OID_RE.fullmatch(str(before_head or "")) or not changed:
        return set()
    merging_before = bool(before.get("merging"))
    if not (merging_before or merge_in_progress(root)):
        return set()
    outcome, partner = merge_outcome(root, before_head, deadline)
    if outcome == "merged" and upstream_commit(root, partner, deadline):
        return clean_merge_paths(root, before_head, partner, changed, deadline)
    if outcome == "abandoned" and merging_before:
        prefix = root_prefix(root)
        staged = {prefix + relative for relative, entry in (earlier.get("files") or {}).items()
                  if isinstance(entry, dict) and entry.get("flags") == ["index"]}
        listed = {prefix + relative for relative in (after_git or {}).get("files") or {}}
        return {path for path in changed if path in staged and path not in listed and path not in recorded}
    return set()


def merge_tree(root, before_head, partner, deadline):
    """The tree `git merge-tree` writes for merging `partner` into `before_head`, and whether that
    merge had no conflict; (None, False) when git cannot say before `deadline`."""
    result = git_result_until(deadline, root, ["merge-tree", "--write-tree", "--no-messages", before_head, partner])
    # It exits 1 for a merge with conflicts and still writes the tree it would stage.
    tree = result[1].split("\n", 1)[0].strip() if result and result[0] in (0, 1) else ""
    return (tree, result[0] == 0) if OID_RE.fullmatch(tree) else (None, False)


def clean_merge_paths(root, before_head, partner, changed, deadline):
    """Of `changed`, the paths the merge of `partner` into `before_head` left exactly as git
    computes it (`paths_as_merged`)."""
    tree = merge_tree(root, before_head, partner, deadline)[0]
    return paths_as_merged(root, before_head, tree, changed, deadline) if tree else set()


def paths_as_merged(root, before_head, tree, changed, deadline, untouched=False, gone=()):
    """Of `changed`, the paths the index and the disk hold exactly as `tree`, a merge into `before_head`.

    A path qualifies when it differs between `before_head` and `tree`, the index holds the tree's
    entry for it, and the file on disk hashes to its blob (or the merge deleted it and it is gone).
    With `untouched`, so does a path the merge left as `before_head` had it, when the index holds
    it, unconflicted, as the tree does and the disk agrees: its bytes are the ones it had, however
    often the command rewrote the file; and a path of `gone`, which the caller saw absent before,
    when neither the tree nor the index holds it and nothing is on disk (a sparse checkout's
    skip-worktree entry is held). Only regular files qualify; a link, a submodule and a path
    neither tree tracks stay recorded.
    """
    merged = git_until(deadline, root, ["diff-tree", "-r", "-z", "--no-renames", before_head, tree, "--"])
    staged = git_until(deadline, root, ["diff-index", "--cached", "-z", "--no-renames", tree, "--"])
    if merged is None or staged is None:
        return set()
    prefix = root_prefix(root)
    # Where the index differs from the merged tree, a conflict stage included.
    differs = {prefix + cwg.normalize_path(path) for path in raw_diff_records(staged)}
    expected = {}
    for path, (mode, oid) in raw_diff_records(merged).items():
        key = prefix + cwg.normalize_path(path)
        if key in changed and key not in differs:
            expected[key] = (path, mode, oid)
    rest = {key for key in changed if key not in expected and key not in differs} if untouched else set()
    indexed = set()
    if rest:
        listing = git_until(deadline, root, ["ls-files", "-s", "-z"])
        if listing is None:
            return set()
        # The index names them in git's own spelling, and holds the merged tree's entry for each.
        for entry in listing.split("\0"):
            fields, _, path = entry.partition("\t")
            fields = fields.split()
            key = prefix + cwg.normalize_path(path)
            if key in rest:
                indexed.add(key)
                if len(fields) == 3 and fields[2] == "0":
                    expected[key] = (path, fields[0], fields[1])
    # Outside `differs` and the index, the merged tree does not hold it either.
    aside = {key for key in gone if key in rest and key not in indexed and not os.path.lexists(key)}
    present = []
    for key, (path, mode, oid) in expected.items():
        on_disk = os.path.join(root, *path.split("/"))
        if mode == MERGE_DELETED_MODE:
            if not os.path.lexists(on_disk):
                aside.add(key)
        elif mode in MERGE_BLOB_MODES and "\n" not in path and os.path.isfile(on_disk) \
                and not os.path.islink(on_disk):
            present.append((key, path, oid))
    if present:
        # Hashed by path, so the same line-ending and filter rules apply as to `git add`.
        hashed = git_until(deadline, root, ["hash-object", "--stdin-paths"],
                           stdin="".join(path + "\n" for _, path, _ in present))
        oids = (hashed or "").split()
        if len(oids) == len(present):
            aside.update(key for (key, _, oid), got in zip(present, oids) if got == oid)
    return aside


# INVARIANT: the snapshots list only what differs from HEAD, so a rebase or a merge committed by one
# command over the candidate's committed files leaves no delta (report c4c78b99); only
# `integration_set_aside` carries such a command, and every other HEAD move is left to the Stop
# catch-up. These words trigger its measurement and judge nothing: an alias that rebases is not carried.
INTEGRATION_WORD_RE = re.compile(r"\b(?:rebase|merge|pull)\b", re.IGNORECASE)
# Seconds after the pre-command hook started (its timeout is 15) by which the baseline must be taken.
INTEGRATION_BASELINE_DEADLINE = 8.0
# `rebase (pick): …`, or under `git pull --rebase` its command line as the action.
REBASE_STEP_RE = re.compile(r"(?P<action>[^\t:()]+?) \((?P<step>[a-z-]+)\)(?:: |$)")
# How much of the end of the HEAD reflog a rebase's entries are looked for in.
REFLOG_TAIL_BYTES = 256 * 1024


def rebase_onto(root, before_head, head):
    """The commit a rebase run from start to end inside one command replayed `before_head` onto,
    read from the working tree's own HEAD reflog, or None.

    Newest first, every entry back to the rebase's `(start)` must be one of its steps under one
    action, the start must leave `before_head` and the newest entry reach `head`, and no rebase may
    be in progress. A detached rebase writes no `(finish)`, so none is required. The start entry
    checks out the commit the rebase replays onto, so its new value is that commit.
    """
    pointer = head_pointer(root)
    if not pointer:
        return None
    git_dir = os.path.dirname(pointer)
    if any(os.path.exists(os.path.join(git_dir, name)) for name in ("rebase-merge", "rebase-apply")):
        return None
    try:
        with open(os.path.join(git_dir, "logs", "HEAD"), "rb") as stream:
            stream.seek(0, os.SEEK_END)
            cut = stream.seek(max(0, stream.tell() - REFLOG_TAIL_BYTES))
            lines = stream.read().decode("utf-8", "replace").splitlines()[1 if cut else 0:]
    except OSError:
        return None
    action, newest = None, None
    for line in reversed(lines):
        fields, _, message = line.partition("\t")
        fields = fields.split()
        step = REBASE_STEP_RE.match(message)
        if len(fields) < 2 or not step or action not in (None, step.group("action")):
            return None
        action, newest = step.group("action"), newest or fields[1]
        if step.group("step") == "start":
            onto = fields[1]
            return onto if fields[0] == before_head and newest == head and OID_RE.fullmatch(onto) else None
    return None


def integration_partner(root, before_head, deadline):
    """`(HEAD, partner)` when a command moved HEAD off `before_head` by a merge commit on it (the
    partner is its second parent) or a rebase of it (the commit replayed onto); else None."""
    line = (git_until(deadline, root, ["rev-list", "--parents", "-n", "1", "HEAD"]) or "").split()
    if not line or line[0] == before_head or merge_in_progress(root):
        return None
    partner = line[2] if len(line) == 3 and line[1] == before_head else rebase_onto(root, before_head, line[0])
    return (line[0], partner) if partner else None


def open_content(marker):
    """The lasting paths of an open candidate below the path cap, sorted; empty otherwise."""
    if not isinstance(marker, dict) or marker.get("closed"):
        return []
    paths = marker.get("content_paths") or []
    return sorted(set(cwg.durable_paths(paths))) if len(paths) < MARKER_PATH_CAP else []


def integration_baseline(command, marker, deadline):
    """What `integration_set_aside` needs from before a command that names a rebase, a merge or a
    pull: the open candidate's lasting paths, their stats and the fingerprint of their content."""
    paths = open_content(marker) if INTEGRATION_WORD_RE.search(command) else []
    # No git before the command: the judge needs every lasting file clean, where this is the full digest.
    fingerprint = content_fingerprint(paths, deadline=deadline, staged=False) if paths else None
    return {"paths": paths, "stats": content_stats(paths), "fp": fingerprint} if fingerprint else None


def last_fingerprint(marks):
    """The fingerprint the newest content mark holds, or None."""
    return next((mark.get("fp") for mark in reversed(marks or []) if isinstance(mark, dict)), None)


def integration_set_aside(before, after_git, marker, deadline):
    """The candidate's lasting files, when the command carried them through a clean integration of
    an upstream commit; else nothing.

    The carry holds only what these establish together: the content the command started from is the
    one the candidate last measured (the fingerprint, taken before it, equals the last mark's), and
    every lasting file lies in the repository the command started in, clean before and after it, so
    its bytes went from `before_head`'s to HEAD's. HEAD moved by a merge commit on `before_head` or a
    rebase of it onto an upstream commit, the merge `git merge-tree` computes for the two has no
    conflict, HEAD's whole tree is that merge — a hand resolution anywhere is not — and the index and
    the disk hold it for each lasting file (`paths_as_merged`).
    """
    baseline, earlier = before.get("integration"), before.get("git") or {}
    root, before_head = earlier.get("root"), before.get("head")
    if (not isinstance(baseline, dict) or not root or not OID_RE.fullmatch(str(before_head or ""))
            or not after_git or earlier.get("overflow") or after_git.get("overflow")
            or open_content(marker) != baseline.get("paths")
            or last_fingerprint(marker.get("content_marks")) != baseline.get("fp")):
        return set()
    paths = set(baseline["paths"])
    moved = content_stats(baseline["paths"]) != baseline.get("stats")
    if not moved:
        return set()
    prefix = root_prefix(root)
    listed = {prefix + relative for relative in list(earlier.get("files") or {}) + list(after_git.get("files") or {})}
    if any(not path.startswith(prefix) or path in listed for path in paths):
        return set()
    integrated = integration_partner(root, before_head, deadline)
    if not integrated or not upstream_commit(root, integrated[1], deadline):
        return set()
    tree, clean = merge_tree(root, before_head, integrated[1], deadline)
    head_tree = (git_until(deadline, root, ["rev-parse", integrated[0] + "^{tree}"]) or "").strip()
    if not clean or head_tree != tree:
        return set()
    # A file the candidate deleted earlier is carried as it was once no tree or index holds it.
    absent = {path for path in paths if baseline["stats"].get(path) is None and not os.path.lexists(path)}
    aside = paths_as_merged(root, before_head, tree, paths, deadline, untouched=True, gone=absent)
    return paths if aside == paths else set()


def head_pointer(directory):
    """Path of the HEAD file governing `directory`, following a linked worktree pointer."""
    entry = os.path.join(directory, ".git")
    if os.path.isdir(entry):
        return os.path.join(entry, "HEAD")
    if not os.path.isfile(entry):
        return None
    try:
        with open(entry, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except Exception:
        return None
    for line in text.splitlines():
        if line.lower().startswith("gitdir:"):
            return os.path.join(directory, line.split(":", 1)[1].strip(), "HEAD")
    return None


def candidate_identity(cwd):
    """Repository root and branch of the working directory, or None when either is unknown.

    The branch is what distinguishes one candidate from the next: the marker is otherwise
    retired only by a terminal receipt, so a candidate abandoned without one keeps its window
    open and the next candidate inherits its already spent simplify, review and closure budgets.

    HEAD itself is deliberately not part of the identity, because committing is a normal step
    inside one candidate, and a detached HEAD reads as unknown so a rebase cannot flap the
    identity mid-candidate. Read from disk rather than through run_git: this runs on every
    gated edit, and an unreadable HEAD is simply an unknown identity.
    """
    try:
        current = os.path.abspath(cwd or os.getcwd())
    except Exception:
        return None
    pointer = None
    while pointer is None:
        pointer = head_pointer(current)
        if pointer is None:
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
    try:
        with open(pointer, encoding="utf-8", errors="replace") as stream:
            head = stream.read().strip()
    except Exception:
        return None
    if not head.startswith("ref:"):
        return None
    ref = head.split(":", 1)[1].strip()
    return "{}#{}".format(cwg.normalize_path(current), ref) if ref else None


def identity_mismatch(stored, identity):
    """Whether a recorded identity and the current one are both known and differ."""
    return bool(stored and identity and stored != identity)


def named_paths(incoming):
    """The paths a mark names, without the opaque shell mark."""
    return [path for path in incoming if path != cwg.SHELL_MUTATION_PATH]


def idle_past_limit(existing, now):
    """Whether an open marker has had no mark for longer than the idle limit."""
    last_ts = existing.get("last_ts")
    return bool(cwg.valid_ts(last_ts) and now - float(last_ts) > CANDIDATE_IDLE_LIMIT)


def continues_cycle(existing, now, identity, incoming):
    """Whether an open marker still describes the candidate being edited now."""
    if not cwg.valid_ts(existing.get("first_ts")):
        return False
    named = named_paths(incoming)
    recorded = set(existing.get("paths") or [])
    touches_recorded = any(path in recorded for path in named)
    # An opaque shell mark on the same branch resumes too: the morning's first `git push`
    # names no file, and restarting on it would discard the rounds the same way.
    resumed = identity and existing.get("identity") == identity and (touches_recorded or not named)
    # The idle limit is a backstop for a candidate abandoned on its branch, not a clock on
    # honest work: the same branch touching a file the cycle already holds is the same
    # candidate the next morning, and restarting it would discard the review rounds it had —
    # on 2026-09-04 that turned a REVISE, REVISE, ESCALATE sequence into an illegal lone
    # ESCALATE.
    if not resumed and idle_past_limit(existing, now):
        return False
    if not identity_mismatch(existing.get("identity"), identity):
        # An unknown identity is not evidence of a new candidate. Treating it as one would hand
        # a fresh budget to work that merely ran one edit outside a repository.
        return True
    # A branch change alone is not a new candidate either: branching mid-candidate and then
    # publishing the same scope is routine, and the escalation procedure requires it. What marks
    # a different candidate is a different branch touching none of the recorded files. Past the
    # diagnostic path cap that comparison is no longer sound, so the cycle is kept.
    if existing.get("path_overflow"):
        return True
    # An opaque shell mark names no file, so it can neither confirm nor deny a different
    # candidate. Counting it as disjoint would make the branch switch itself — recorded exactly
    # this way — restart the window and drop the evidence of the work being published.
    if not named:
        return True
    return touches_recorded


def displacement(existing, now, identity, incoming):
    """What a new cycle keeps about the open one it replaced: the Stop hook names it when a lane
    result from before the replacement no longer counts (report a269a6fc)."""
    return {
        "ts": now,
        "opened": float(existing["first_ts"]),
        "was": existing.get("identity"),
        "now": identity,
        "idle": idle_past_limit(existing, now),
        "by": named_paths(incoming)[:3],
    }


def cycle_start(marker, now, identity, incoming):
    """Preserve the current cycle, open a new one for a new candidate, and tolerate the
    legacy marker format."""
    fresh = {
        "first_ts": now,
        "last_write_ts": None,
        "edits": 0,
        "paths": [],
        "minimum_risk_seen": None,
        "path_overflow": False,
        "identity": identity,
        "last_durable_ts": 0.0,
        "unattributed_durable": False,
        "content_paths": [],
        "content_marks": [],
        "content_stats": {},
        "content_stats_at": None,
        "head_at_start": None,
        "refs_at_start": None,
        "opened_at": None,
        "displaced": None,
    }
    existing = cwg.read_json(marker)
    if existing:
        if existing.get("closed"):
            return fresh
        if not continues_cycle(existing, now, identity, incoming):
            if cwg.valid_ts(existing.get("first_ts")):
                fresh["displaced"] = displacement(existing, now, identity, incoming)
            return fresh
        stored = existing.get("identity")
        # A cycle kept across an identity mismatch keeps the identity it was opened with:
        # the first mark after a branch switch is usually the switch itself, recorded as
        # <shell-mutation> and overlapping every candidate that ever ran one. Adopting the new
        # identity there would settle the comparison before the edits that actually reveal a
        # different candidate. Where there is no mismatch, the current identity is adopted, so
        # a transient unreadable HEAD cannot erase the discriminator for the rest of the cycle.
        mismatch = identity_mismatch(stored, identity)
        carried = existing.get("last_durable_ts")
        return {
            "first_ts": float(existing["first_ts"]),
            "last_write_ts": existing.get("last_write_ts"),
            "edits": int(existing.get("edits") or 0),
            "paths": list(existing.get("paths") or []),
            "minimum_risk_seen": existing.get("minimum_risk_seen"),
            "path_overflow": bool(existing.get("path_overflow")),
            "identity": stored if mismatch else (identity or stored),
            "last_durable_ts": float(carried) if cwg.valid_ts(carried) else 0.0,
            "unattributed_durable": bool(existing.get("unattributed_durable")),
            # COMPAT: a marker written before the domain was recorded separately measured every
            # diagnostic path, so that is what its fingerprints describe. Seeding the domain
            # from them keeps a candidate open across the upgrade comparable with itself.
            "content_paths": list(
                existing["content_paths"]
                if existing.get("content_paths") is not None
                else existing.get("paths") or []
            ),
            "content_marks": list(existing.get("content_marks") or []),
            "content_stats": dict(existing.get("content_stats") or {}),
            "content_stats_at": existing.get("content_stats_at"),
            "head_at_start": existing.get("head_at_start"),
            "refs_at_start": existing.get("refs_at_start"),
            "opened_at": existing.get("opened_at"),
            "displaced": existing.get("displaced"),
        }
    if os.path.exists(marker):
        try:
            fresh["first_ts"] = os.path.getmtime(marker)
        except Exception:
            pass
    return fresh


def settled_by_closure(data, marker, incoming, unresolved=False, unattributed_risk=None):
    """Whether, with no candidate open, this command named only lasting files the session's last
    closed candidate held and left every byte of them as that candidate closed on: committing,
    rebasing or pushing work after its receipt is no new work (report 9dbbfe70). The Stop hook
    records those files and their `content_fingerprint` when it closes the cycle. A command the
    snapshots could not resolve, or a change nobody could be shown to own, may have touched
    anything, so it never settles."""
    if unresolved or unattributed_risk or not incoming:
        return False
    state = cwg.read_json(cwg.state_path(cwg.session_key(data.get("session_id")))) or {}
    closed = state.get("closed_content") or {}
    paths, fingerprint = closed.get("paths") or [], closed.get("fp")
    if not fingerprint or not set(incoming) <= set(paths):
        return False
    existing = cwg.read_json(marker)
    return (not existing or bool(existing.get("closed"))) and content_fingerprint(
        paths, deadline=time.monotonic() + SETTLE_BUDGET) == fingerprint


def covers(root, path, skipped=()):
    """Whether a snapshot of `root` looked at `path`.

    `skipped` names the subdirectories that snapshot walks around, together with the dot-prefixed
    ones it always does; anything below either was never read, so nothing about it was proved. A
    repository passes no skip list: Git reports on every tracked and untracked file beneath its
    root whatever the directory is called, and `plans` or `state` there is ordinary source, not
    the bookkeeping those names mean in a configuration home.
    """
    if path == root:
        return True
    if not root or not path.startswith(root + "/"):
        return False
    parts = path[len(root) + 1:].split("/")
    if skipped and any(part.startswith(".") for part in parts[:-1]):
        return False
    return not any(part in skipped for part in parts)


def outside_snapshot(paths, roots, watched=(), vouched=()):
    """Durable recorded paths no snapshot taken for this command says anything about.

    `roots` are repositories and `watched` the configuration trees — the same question, asked of
    two snapshots that see their own trees differently. `vouched` are lasting files outside both,
    measured one by one around the command.
    """
    repositories = [cwg.normalize_path(root) for root in roots or () if root]
    trees = [cwg.normalize_path(root) for root in watched or () if root]
    measured = {cwg.normalize_path(path) for path in vouched or ()}
    return [
        path
        for path in cwg.durable_paths(paths)
        if path not in measured
        and not any(covers(root, path) for root in repositories)
        and not any(covers(root, path, AGENT_CONFIG_SKIP) for root in trees)
    ]


def record_paths(data, candidate_paths, unresolved=False, snapshot_roots=(),
                 watched_roots=(), unattributed_risk=None, write_capable_command=True,
                 opening=None, content_changed=None, vouched=(), unseen=None, skipped=0,
                 merged=(), no_snapshot=False, quiet=False):
    """Append diagnostic paths while preserving monotonic risk beyond the 128-path cap.

    `unattributed_risk` is the grade of a lasting change seen during this command that no
    session can be shown to own. Its path stays out of the marker - it may be another session's
    file, and recording it would put that session's work into this candidate - but the grade
    does not, because it may equally be this command's own write, and letting ambiguity close
    the cycle under the operational contract would be a way out of the gate rather than a
    narrower question. Sticky for the cycle: a later resolvable command does not make an earlier
    unattributable one go away.

    `content_changed` names the subset of `candidate_paths` whose bytes this command actually
    rewrote; None means all of them, which is what a tool that writes a file reports. Only that
    subset joins the fingerprint's domain - see `snapshot_changes` for why a path the command
    only moved between the worktree, the index and HEAD must stay out of it. Everything else the
    path carries - its risk, its work class, the freshness anchor - is unchanged, because the
    repository really did gain those bytes and the candidate still answers for them.

    `unresolved` means a mutation was observed but the snapshot could not name what it touched,
    so it may have been a source edit made through the shell. `snapshot_roots` bounds what an
    empty delta actually proves: each snapshot vouches for its own tree and for nothing else —
    the working repository, and the agent-configuration homes this machine keeps outside any
    repository. `vouched` are the candidate's lasting files outside every snapshotted tree that
    were measured one by one around the command. `unseen` is the repository the command ended in
    when no snapshot covered it, `skipped` the number of repositories the time budget left out,
    and `no_snapshot` says the pre-command snapshot never arrived (its hook was cancelled or
    failed); the content mark keeps all three, so a block can say what went unmeasured. The
    placeholder of a command proven not to write (`write_capable_command` false) joins only a
    candidate that holds no lasting path yet: for one that does, it adds nothing to answer for and
    would only give it a new block budget (report bb01bd52).

    `merged` are the paths a clean upstream merge left as it computed them (`merge_set_aside`, and
    for a rebase or a one-step merge `integration_set_aside`), recorded nowhere. When some of them are the candidate's own lasting files, and the command
    rewrote no other lasting byte and left nothing unresolved or unattributed, the mark with the new
    content names the content it replaced (`merge`): the verdicts that covered the candidate cover
    the merge of it, as a clean merge needs nothing.
    """
    marker = cwg.marker_path(cwg.session_key(data.get("session_id")))
    now = time.time()
    incoming = [
        normalized
        for normalized in (cwg.normalize_path(path) for path in candidate_paths)
        if normalized
    ]
    if settled_by_closure(data, marker, incoming, unresolved, unattributed_risk):
        cwg.log_event("settled", session=cwg.session_key(data.get("session_id")),
                      paths=cwg.durable_paths(incoming)[:5])
        return True
    cycle = cycle_start(marker, now, candidate_identity(data.get("cwd")), incoming)
    paths = cycle["paths"]
    # A command proven not to write — a reader, a bookkeeping tool — adds nothing a candidate
    # holding lasting files answers for, and a new path would hand that unchanged candidate a new
    # block budget: the report the gate asks for after a block did exactly that (report bb01bd52).
    adds = write_capable_command or incoming != [cwg.SHELL_MUTATION_PATH] or not cwg.durable_paths(paths)
    if adds:
        for normalized in incoming:
            if normalized not in paths:
                paths.append(normalized)
    if content_changed is None:
        rewrote = incoming
    else:
        named = {cwg.normalize_path(path) for path in content_changed}
        rewrote = [path for path in incoming if path in named]
    content_paths = cycle["content_paths"]
    for normalized in cwg.durable_paths(rewrote):
        if normalized not in content_paths:
            content_paths.append(normalized)
    observed_risk = cwg.minimum_risk(paths)
    minimum_risk_seen = cwg.max_risk(
        cycle["minimum_risk_seen"], observed_risk, unattributed_risk
    )
    unattributed_durable = cycle["unattributed_durable"] or bool(unattributed_risk)
    overflow = cycle["path_overflow"] or len(paths) > 128
    # Freshness of a review verdict is measured against the last change to a lasting artifact,
    # not against any mark at all. Rewriting a throwaway script or re-running a maintenance
    # command after an approval does not touch what the reviewer read, and treating it as a new
    # edit forced a fresh review round for work the verdict already covered. An unresolved
    # mutation is the exception: it could have edited source through the shell, so once the
    # cycle holds durable paths it must expire the verdict the same way a named edit would.
    # Only a command that could actually write expires a verdict on the strength of what the
    # snapshot could not see; the snapshot's own evidence (a durable path in `incoming`) always
    # does. A read-only pipeline the policy regex does not recognise, or a git failure under
    # load, must not cost a review round.
    expired = write_capable_command and (
        unresolved or bool(outside_snapshot(paths, snapshot_roots, watched_roots, vouched))
    )
    # An unattributable durable change anchors freshness too, even though its path is not
    # recorded: without an anchor such a candidate keeps last_durable_ts at zero, the Stop hook
    # falls back to the whole-cycle timestamp, and every later command then expires the very
    # approval the floor made it go and get.
    last_durable_ts = (
        now
        if cwg.durable_paths(incoming)
        or unattributed_risk
        or (expired and cwg.durable_paths(paths))
        else cycle["last_durable_ts"]
    )
    content_marks = cycle.get("content_marks") or []
    # The candidate's own files a clean upstream merge changed. When that merge is all this command
    # changed in them, the mark it leaves names the content it replaced, whatever else the command
    # named without rewriting (a `git add` of reviewed bytes beside the merge).
    carried = [path for path in cwg.durable_paths(merged) if path in content_paths]
    carry_from = last_fingerprint(content_marks) \
        if carried and not cwg.durable_paths(rewrote) and not unattributed_risk and not unresolved else None
    # What the lasting paths looked like at the last measurement, and when, for the Stop hook to spot
    # a change no hook measured; replaced whenever this call measures.
    stats, stats_at = cycle.get("content_stats") or {}, cycle.get("content_stats_at")
    if not cycle.get("edits") and cycle.get("head_at_start") is None:
        # The commit and refs the cycle opened on: a repository back on them, clean, has changed
        # nothing lasting, whatever happened in between (a rebase probe that was aborted, an
        # edit undone). For a cycle a shell command opens, that state is what the pre-command
        # snapshot saw — the command itself may have moved HEAD; an Edit cannot, so the state
        # after it is the state before it. A shell opening without a snapshot leaves both unknown.
        if opening is not None:
            cycle["head_at_start"] = opening.get("head")
            cycle["refs_at_start"] = opening.get("refs")
            # Before the command ran: the commits it made itself are the candidate's too.
            cycle["opened_at"] = opening.get("ts")
        else:
            cwd = str(data.get("cwd") or os.getcwd())
            cycle["head_at_start"] = head_commit(cwd)
            cycle["refs_at_start"] = cwg.refs_digest(cwd)
    if last_durable_ts == now:
        # A change the snapshot could not attribute may have touched anything, so it is a
        # barrier — but the bytes of the recorded paths are still measurable, and measuring
        # them is what lets a verdict stated afterwards be shown to cover. Recording no
        # fingerprint at all used to erase the baseline: a verdict that followed such a
        # command could never be proved current, and the next named edit — a `git add` of the
        # very bytes the reviewer read — retired it (reports 3d343b8b,
        # dfa8a850). So the flag and the measurement are now separate. Measuring on every
        # unattributed change costs the git calls the old short-circuit saved; the cost is
        # bounded by the fingerprint's own budgets and is nothing at all while the candidate has
        # rewritten no lasting byte, which is the common case for a shell mutation before any
        # edit.
        unknown = bool(unattributed_risk or not cwg.durable_paths(incoming))
        fingerprint = content_fingerprint(content_paths)
        stats, stats_at = content_stats(content_paths[-MARKER_PATH_CAP:]), now
        cause = command_cause(data, "edit" if cwg.durable_paths(incoming) else
                              "unattributed" if unattributed_risk else "unresolved-write-capable")
        if unseen:
            cause["landed"] = unseen
        if skipped:
            cause["skipped"] = skipped
        if no_snapshot:
            cause["no_snapshot"] = True
        content_marks = content_marks_after(content_marks, now, fingerprint, unknown, cause,
                                            merge=None if unknown else carry_from)
        cwg.log_event(
            "durable", session=cwg.session_key(data.get("session_id")),
            paths=[p for p in cwg.durable_paths(incoming)][:5],
            fp=(fingerprint or "")[:12],
            **cause
        )
    elif carry_from:
        # Nothing else lasting happened, so no mark would record the merged bytes; this one does.
        fingerprint = content_fingerprint(content_paths)
        stats, stats_at = content_stats(content_paths[-MARKER_PATH_CAP:]), now
        if fingerprint and fingerprint != carry_from:
            content_marks = content_marks_after(content_marks, now, fingerprint,
                                                cause=command_cause(data, "merge"), merge=carry_from)
    if merged:
        last = content_marks[-1] if content_marks and isinstance(content_marks[-1], dict) else {}
        cwg.log_event("merge", session=cwg.session_key(data.get("session_id")),
                      set_aside=len(merged), own_files=len(carried),
                      carried=bool(last.get("merge")) and last.get("ts") == now,
                      paths=sorted(merged)[:3])
    # The last mark that could have written, apart from `last_ts`, which every mark moves and the idle
    # limit reads: for a candidate whose lasting change no snapshot can see it is what an approval
    # must postdate, and the bookkeeping CLAUDE.md asks for right after a verdict (`nlm-memory
    # remember`, the gate inbox) expired that approval (report cf223da5). Only a command proven to
    # write nothing lasting (`only_own_state`) that brought no lasting path leaves it.
    quiet = quiet and not cwg.durable_paths(incoming) and not unattributed_risk
    return cwg.write_json(marker, {
        "first_ts": cycle["first_ts"],
        "last_ts": now,
        "last_write_ts": cycle["last_write_ts"] if quiet else now,
        "last_durable_ts": last_durable_ts,
        # The legacy field is read as one of the paths, so it must not name what was left out.
        "last_path": str(candidate_paths[-1] if adds else paths[-1]),
        "edits": cycle["edits"] + 1,
        "paths": paths[-MARKER_PATH_CAP:],
        "minimum_risk_seen": minimum_risk_seen,
        "path_overflow": overflow,
        "identity": cycle["identity"],
        "unattributed_durable": unattributed_durable,
        "content_paths": content_paths[-MARKER_PATH_CAP:],
        "content_marks": content_marks,
        "content_stats": stats,
        "content_stats_at": stats_at,
        "head_at_start": cycle.get("head_at_start"),
        "refs_at_start": cycle.get("refs_at_start"),
        "opened_at": cycle.get("opened_at"),
        "displaced": cycle.get("displaced"),
    })


def codex_launch(command):
    """How a shell command launches Codex: whether anything is fed on stdin, and the packet file.

    `fed` is any stdin redirect anywhere in the command; `path` is the file read by the one
    segment whose executable is codex — any spelling, `.exe` or not, behind `timeout` or an
    environment assignment — running `exec`. It is empty when that segment cannot be named: no
    codex segment, several, or a `||` that may skip it. The Stop hook treats a launch that fed
    something it cannot bind by as binding nothing.
    """
    command = join_continuations(command)
    fed = bool(STDIN_REDIRECT_RE.search(command))
    if "||" in command:
        return {"fed": fed, "path": ""}
    fed_by = []
    for segment in shell_segments(command):
        head, rest = command_head(segment)
        words = rest.split()
        if head == "codex" and words and words[0].lower() == "exec":
            matches = STDIN_REDIRECT_RE.findall(segment)
            fed_by.append(matches[-1] if matches else "")
    if len(fed_by) != 1 or not fed_by[0]:
        return {"fed": fed, "path": ""}
    import codex_lane
    # The command arrives as the agent wrote it, `${REVIEW_ID}` and all; the value comes from
    # an assignment in the same command. A variable nothing assigns leaves no file to capture.
    path = codex_lane.resolve_variables(command, fed_by[0])
    return {"fed": fed, "path": "" if "$" in path else codex_lane.windows_path(path)}


def forget_stale_captures(session, now=None):
    """Drop this session's packet captures older than a day: their launches never reported back."""
    now = time.time() if now is None else now
    for path in glob.glob(cwg.packet_capture_path(session, "*")):
        try:
            if now - os.path.getmtime(path) > PACKET_CAPTURE_TTL:
                os.remove(path)
        except OSError:
            pass


def packet_text(path):
    """What a launch feeds Codex from this file, as both hooks keep it: `(text, truncated)`, or
    None when the file cannot be read."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(PACKET_KEEP_BYTES + 1)
    except OSError:
        return None
    return (cwg.normalized(raw[:PACKET_KEEP_BYTES].decode("utf-8", "replace")),
            len(raw) > PACKET_KEEP_BYTES)


def capture_packet(data, session):
    """Keep what this launch is about to feed Codex, so a later rewrite of the file changes nothing."""
    command = str((data.get("tool_input") or {}).get("command") or "")
    path = codex_launch(command)["path"]
    if not path or not data.get("tool_use_id"):
        return
    forget_stale_captures(session)
    text, truncated = packet_text(path) or ("", False)
    # `text` and `truncated` are what the Stop hook reads; `path` and `ts` are for a person
    # opening the capture to see which launch it belonged to.
    cwg.write_json(cwg.packet_capture_path(session, str(data.get("tool_use_id"))), {
        "path": path, "ts": time.time(), "text": text, "truncated": truncated,
    })


# How a command names a configuration home it might write to: the home's own path, its
# shell spellings, or the environment variable that points at it. A `.claude` segment alone is
# none of these: `<repo>/.claude/worktrees/<tree>` is a checkout (reports 27cd9fe8, 8ae90973).
HOME_REFERENCE_RE = re.compile(
    r"(?i)(?:(?:~|\$home|\$\{home\}|\$userprofile|\$\{userprofile\}|%userprofile%|\$env:userprofile|"
    r"\$env:home)[/\\]\.(?:claude|codex|agents)(?=$|[\s\"'/\\;&|),:`])|claude_config_dir|codex_home)"
)
# Spellings under a configuration home that refer to nothing it holds: a chip's worktree is a
# checkout of a project, and running one of the hooks' bookkeeping commands writes only the home's
# `state/`, which no snapshot watches (report f5f9116f). For those commands only the run position
# counts — the script an interpreter starting its segment runs, past the interpreter's own
# options — and the same path as the target of a write names the home like any other. Matched on
# the normalized command.
CHIP_TREES = "state/chips/trees"
CHIP_TREE_SPELLING_RE = re.compile(r"[^\s\"'`]*/" + re.escape(CHIP_TREES) + r"/[^\s\"'`/]+")
INTERPRETER_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pythonw|py)(?:\.exe)?$", re.IGNORECASE)
# Interpreter options that take the next word as their value, read before any lower-casing:
# `-X utf8` takes one, `-x` takes none.
INTERPRETER_VALUE_OPTIONS = frozenset(("-W", "-X", "--check-hash-based-pycs"))


def bookkeeping_script(segment, bridge=False):
    """The span of the bookkeeping script an interpreter starting this segment runs, or None; the
    memory bridge counts only when `bridge` is set, since it does name its home."""
    words = list(SHELL_WORD_RE.finditer(segment))
    position = 0
    while position < len(words) and ENV_ASSIGNMENT_RE.match(words[position].group(0)):
        position += 1
    if position >= len(words):
        return None
    interpreter = unquoted_word(words[position].group(0))[0].replace("\\", "/").rsplit("/", 1)[-1]
    if not INTERPRETER_RE.match(interpreter):
        return None
    position += 1
    while position < len(words):
        option = unquoted_word(words[position].group(0))[0]
        if option == "--":
            position += 1
            break
        if not option.startswith("-"):
            break
        if option.startswith("--"):
            position += 2 if option.lower() in INTERPRETER_VALUE_OPTIONS else 1
            continue
        if option[:2] in INTERPRETER_VALUE_OPTIONS:
            position += 2 if len(option) == 2 else 1
            continue
        if "c" in option or "m" in option:
            # `-c code` or `-m module`: what runs is not a script file.
            return None
        position += 1
    if position < len(words):
        script = unquoted_word(words[position].group(0))[0].replace("\\", "/")
        if STATE_ONLY_SCRIPT_RE.search(script) or (bridge and BRIDGE_SCRIPT_RE.search(script)):
            return words[position].span()
    return None


# The last command read, since `on_home_ground` asks about it once for every path in a delta.
_SPELLED = {}


def without_bookkeeping(command, shell="Bash"):
    """The normalized command with the spellings that name nothing a home holds blanked out."""
    key = (command, shell)
    if key not in _SPELLED:
        kept = []
        for segment in shell_segments(command, shell=shell):
            span = bookkeeping_script(segment)
            if span is not None:
                segment = segment[:span[0]] + " " + segment[span[1]:]
            kept.append(cwg.normalize_path(segment))
        _SPELLED.clear()
        _SPELLED[key] = CHIP_TREE_SPELLING_RE.sub(" ", " ; ".join(kept))
    return _SPELLED[key]


def on_home_ground(path, cwd, snapshot_roots, data, elsewhere=False):
    """Whether an unattributed change at this path can be this command's own.

    It can when the path lies under the repository the command ran in, under the command's
    working directory or the configuration home holding it, or under a configuration home the
    command names — by its path in any spelling (Windows, Git Bash), by `~/.claude`-style
    shorthand, by the environment variable that points at it, or by its own name from the
    directory holding it. For a path another session announced or was writing beside (`elsewhere`),
    a home is reached only from inside it: the directory holding a home is not in it. What that
    costs: a session's own unspelled write into a home takes no floor when it lands on the very file
    another session announced, beside another session's shell in that home, or while the registry
    overflowed. A change elsewhere was seen
    only because the homes are shared between sessions, and grading it here would hand this
    candidate another session's floor. A path a command builds at run time without spelling
    the home is out of reach by design. A chip's worktree lies under a home without being part
    of it, so neither working there nor naming it, nor running the hooks' bookkeeping commands,
    puts the rest of the home on this command's ground.
    """
    path = cwg.normalize_path(path)
    cwd = cwg.normalize_path(cwd).rstrip("/")
    homes = [cwg.normalize_path(root).rstrip("/") for root in agent_config_roots()]
    grounds = [cwg.normalize_path(root).rstrip("/") for root in snapshot_roots if root]
    grounds.append(cwd)
    # Only for paths another session accounts for (report b26e3641): a hook rewritten by a script
    # run from the home directory, which nobody else announced, must still land here.
    holding = next((home for home in homes if home and covers(home, path)), "") if elsewhere else ""
    if holding:
        grounds = [ground for ground in grounds if covers(holding, ground)]
    grounds.extend(
        home for home in homes
        if home and cwd and covers(home, cwd) and not covers(home + "/" + CHIP_TREES, cwd)
    )
    if any(covers(ground, path) for ground in grounds if ground):
        return True
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return False
    spelled = without_bookkeeping(command, str(data.get("tool_name") or "Bash"))
    for home in homes:
        if not home:
            continue
        spellings = [home]
        if re.match(r"^[a-z]:/", home):
            spellings.append("/" + home[0] + home[2:])
        if any(spelling in spelled for spelling in spellings):
            return True
        # From the directory that holds a home, the home's own name (`.claude/skills/x.md`) spells
        # it too, at the start of a word or after `./`.
        if cwd and home != cwd and covers(cwd, home) and re.search(
                r"(?:^|[\s\"'=;&|(]|\./)" + re.escape(home[len(cwd) + 1:]) + r"(?=/|[\s\"';&|)]|$)", spelled):
            return True
    return bool(HOME_REFERENCE_RE.search(spelled))


def head_commit(cwd):
    """The commit HEAD points at in this working directory, or None outside a repository."""
    head = (cwg.git_text(cwd, ["rev-parse", "HEAD"], timeout=5) or "").strip()
    return head if OID_RE.fullmatch(head) else None


def content_fingerprint(paths, deadline=None, staged=True):
    """A digest of the lasting paths as they are now, or None when it cannot be known cheaply —
    including, when a `deadline` (`time.monotonic()`) is given, not before it. Without `staged` the
    index is not read and no git runs: the digest is the full one whenever no path has a staged
    divergence, as for files that are clean.

    Every record is domain-separated and length-prefixed — kind, path, mode, bytes or link
    target — so no content can imitate another record. A path that no longer exists contributes
    nothing: the candidate is what is on disk, so an add that was deleted again is no change,
    while a file the approval covered going missing is one. Inside a repository a staged blob
    that matches neither HEAD nor the file on disk is part of the record — something was put in
    the index that nobody reviewed — while staging or committing the reviewed bytes changes
    nothing: `git add` and `git commit` after an approval are not edits.

    The index is read only while the candidate is narrow enough (`FINGERPRINT_INDEXED_FILES`),
    and a wider candidate is measured on its content alone, carrying a marker of its own so such a
    measurement can never equal an indexed one.
    Refusing to measure a wide candidate at all is what made the promise above unkeepable for
    every large one (report eedcca07): a mass restore had left 128 paths in the marker, nothing
    was measured from then on, and the first durable event after the approval — the commit of
    the approved bytes — retired it. An empty set of paths is a measurement too: a candidate
    that has changed no lasting byte yet is not an unknown one.
    """
    durable = sorted(set(cwg.durable_paths(paths)))
    if deadline is not None and time.monotonic() > deadline:
        return None
    indexed = len(durable) <= FINGERPRINT_INDEXED_FILES
    budget = FINGERPRINT_MAX_TOTAL_BYTES
    digest = hashlib.sha256()

    def add(kind, *fields):
        digest.update(kind)
        for field in fields:
            data = field if isinstance(field, bytes) else str(field).encode("utf-8", "replace")
            digest.update(struct.pack(">Q", len(data)))
            digest.update(data)

    if not indexed:
        add(b"T", "content")
    by_dir = {}
    for path in durable:
        try:
            if os.path.islink(path):
                add(b"L", path, os.readlink(path))
                continue
            if not os.path.exists(path):
                continue
            if os.path.isdir(path):
                add(b"D", path)
                continue
            size = os.path.getsize(path)
            budget -= size
            if size > FINGERPRINT_MAX_BYTES or budget < 0:
                return None
            with open(path, "rb") as stream:
                content = stream.read()
            add(b"F", path, os.stat(path).st_mode & 0o777, content)
            if indexed and staged:
                by_dir.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
        except OSError:
            return None
    divergences = staged_divergences_by_dir(by_dir, deadline) if by_dir else {}
    for directory in sorted(by_dir):
        if divergences[directory] is None:
            return None
        for name, oid in divergences[directory]:
            add(b"S", os.path.join(directory, name), oid)
    return digest.hexdigest()


def content_stats(paths):
    """Size and modification time of each lasting path, None for one that is gone.

    Stored beside every measurement, so the Stop hook can tell cheaply that a lasting path changed
    after it — the change a cancelled marker hook never recorded (report 8db8b3d2).
    """
    stats = {}
    for path in sorted(set(cwg.durable_paths(paths))):
        try:
            stat = os.stat(path)
        except OSError:
            stats[path] = None
            continue
        stats[path] = [stat.st_size, stat.st_mtime_ns]
    return stats


# The index entry `git add -N` leaves for a new file is the empty blob, in either object format.
# It stages no bytes, so staging the reviewed file over it later changes nothing (report 265312d0).
EMPTY_BLOBS = frozenset((
    "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
    "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",
))


# Variables that move git's search for a repository away from the plain walk up the directory
# tree: with any of them set, only git itself can say which repository a directory belongs to.
DISCOVERY_ENVIRONMENT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")


def staged_divergences(directory, names, deadline=None):
    """What `staged_divergences_by_dir` answers for one directory."""
    return staged_divergences_by_dir({directory: names}, deadline)[directory]


def repository_of(directory, roots):
    """(root, folder) for `directory` inside one of the normalized repository `roots` already found,
    the folder spelled as the file system spells it, when git's own walk up from the directory would
    reach that root; None when only git can say. That walk stops at the first folder holding a
    `.git` entry or laid out as a bare repository, so either one on the way puts the directory in
    another repository; a link or a junction on the way may lead git somewhere else entirely, and a
    directory that is not there is no repository at all. The spelling matters because git lists
    paths as the file system spells them and matches pathspecs case-sensitively; for the directory
    git places itself, `--show-prefix` gives the same spelling.
    """
    if not os.path.isdir(directory):
        return None
    normalized = cwg.normalize_path(directory).rstrip("/")
    for root in sorted(roots, key=len, reverse=True):
        if not covers(root, normalized):
            continue
        folder = normalized
        while folder != root:
            if os.path.islink(folder) or os.path.isjunction(folder) or os.path.lexists(folder + "/.git") or (
                    os.path.isfile(folder + "/HEAD") and os.path.isdir(folder + "/objects")
                    and os.path.isdir(folder + "/refs")):
                return None
            folder = folder.rsplit("/", 1)[0]
        spelled = os.path.realpath(directory).replace("\\", "/").rstrip("/")
        return (root, spelled[len(root) + 1:]) if covers(root, cwg.normalize_path(spelled)) else None
    return None


def staged_divergences_by_dir(by_dir, deadline=None):
    """{directory: divergences} for `{directory: names}`. A directory's divergences are (name,
    staged oid) for the files whose index blob matches neither HEAD nor the disk and (name,
    "deleted") for a file on disk and in HEAD that the index no longer holds; [] outside a
    repository; None for anything git could not answer, including whether this is a repository at
    all, so the caller records an unknown fingerprint rather than a false one. With a `deadline`
    (`time.monotonic()`), every git call gets only the time left, and too little left is None.

    Git runs per repository, not per directory. At about a quarter of a second a process on this
    machine, four calls per directory took a 17-file candidate spread over nine folders past the
    three-second budget of both hooks that measure a closed candidate, and committing the bytes a
    receipt had just closed opened a new candidate (report 53529bba); a 59-file candidate took the
    marker hook itself past its timeout (report 8376c8c1). `rev-parse` places the first directory of
    a repository and `repository_of` the others, and one `ls-files` and one `ls-tree` list the index
    and HEAD for all of the repository's folders at once: the listings the per-folder calls made,
    so every name keeps the entry it had, a pair of names that differ only in case and an unmerged
    path's last stage included. Only a path staged away from HEAD needs its disk blob, which
    `hash-object` computes as `add` would store it, with the same line-ending and filter rules, so
    staging the reviewed bytes never diverges. Output is read NUL-separated, so a name git would
    quote (non-ASCII, a tab) keys like any other, and a name is keyed by Python's own lower-casing,
    the same one the marker uses.
    """

    def ask(where, arguments, cap, text=True):
        """git's stdout (`git_text`), or `(code, stdout)` with `text=False`, within the time left;
        None when too little is left, as for any question git could not answer."""
        timeout = cap if deadline is None else min(cap, deadline - time.monotonic())
        if timeout < 0.25:
            return None
        return (cwg.git_text if text else cwg.git_run)(where, arguments, timeout=timeout)

    results, repositories = {}, {}
    placeable = not any(os.environ.get(name) for name in DISCOVERY_ENVIRONMENT)
    for directory in sorted(by_dir):
        placed = repository_of(directory, repositories.keys()) if placeable else None
        if placed is None:
            located = ask(directory, ["rev-parse", "--show-toplevel", "--show-prefix"], 5, text=False)
            if located is None:
                results[directory] = None
                continue
            lines = located[1].split("\n") + [""]
            if located[0] != 0 or not lines[0].strip():
                results[directory] = []
                continue
            root = cwg.normalize_path(lines[0].strip()).rstrip("/")
            repositories.setdefault(root, (lines[0].strip(), {}))
            placed = root, lines[1].rstrip("/")
        repositories[placed[0]][1][directory] = placed[1]
    for toplevel, folders in repositories.values():
        results.update(repository_divergences(toplevel, folders, by_dir, ask, deadline))
    return results


def repository_divergences(toplevel, folders, by_dir, ask, deadline):
    """`staged_divergences_by_dir` for the directories of one repository; `folders` maps each to its
    path from `toplevel` as git spells it, "" for the top folder."""
    unknown = dict.fromkeys(folders)
    # "./" is the top folder; any other folder's pathspec lists what lies below it.
    specs = ["--"] + sorted({folder + "/" if folder else "./" for folder in folders.values()})
    index = ask(toplevel, ["--literal-pathspecs", "ls-files", "-s", "-z"] + specs, 10)
    tree = ask(toplevel, ["--literal-pathspecs", "ls-tree", "-z", "HEAD"] + specs, 10, text=False) \
        if index is not None else None
    if tree is not None and tree[0] != 0:
        # Only a HEAD with no commit yet, which `rev-parse` confirms, holds nothing; any other
        # failure to list it is unknown.
        born = ask(toplevel, ["rev-parse", "--verify", "-q", "HEAD"], 5, text=False)
        tree = (0, "") if born and born[0] != 0 else None
    if tree is None:
        return unknown

    def entries(listing, oid_field):
        # `ls-files -s -z`: "<mode> <oid> <stage>\t<path>"; `ls-tree -z`: "<mode> <type> <oid>\t<path>".
        # The last entry of a name wins, an unmerged path's last stage among them.
        found = {}
        for record in listing.split("\0"):
            head, _, path = record.partition("\t")
            parts = head.split()
            if len(parts) == 3 and path:
                folder, _, name = path.rpartition("/")
                found[(folder, name.lower())] = (parts[oid_field], path)
        return found

    staged = entries(index, 1)
    committed = {key: oid for key, (oid, _) in entries(tree[1], 2).items()}

    found, pending = {}, []
    for directory, folder in folders.items():
        found[directory] = []
        for name in sorted(by_dir[directory]):
            key = (folder, name.lower())
            on_disk = os.path.isfile(os.path.join(directory, name))
            entry = staged.get(key)
            if entry is None:
                if key in committed and on_disk:
                    found[directory].append((name, "deleted"))
            elif entry[0] != committed.get(key) and not (entry[0] in EMPTY_BLOBS and key not in committed):
                found[directory].append((name, entry[0]))
                if on_disk:
                    pending.append((directory, name, entry))
    settled = set()
    if pending:
        # `hash-object` takes paths on disk, not pathspecs; one missing path fails the whole call,
        # and every file then counts as divergent, which can only cost a review round.
        hashed = ask(toplevel, ["hash-object", "--"] + [entry[1] for _, _, entry in pending], 10)
        if hashed is None and deadline is not None:
            # Under a deadline a failed hash is more likely the clock than a missing path: unknown.
            return unknown
        settled = {(directory, name) for (directory, name, entry), blob in zip(pending, (hashed or "").split())
                   if blob == entry[0]}
    return {directory: [(name, oid) for name, oid in divergent if (directory, name) not in settled]
            for directory, divergent in found.items()}


def command_cause(data, reason):
    """What a content mark says about the change that left it, for a person reading a block."""
    tool = str(data.get("tool_name") or "")
    return {
        "reason": reason,
        "tool": tool,
        "command": command_label(str((data.get("tool_input") or {}).get("command") or ""), tool),
    }


def content_marks_after(marks, now, fingerprint, unknown=False, cause=None, merge=None):
    """The marks with this change appended: a new one when the content or the barrier state moves.

    `unknown` says the change could not be attributed, so it is a barrier no verdict older than
    it may cross. It is recorded beside the measurement rather than instead of it: the bytes of
    the recorded paths are still what they are, and a verdict stated after the barrier is judged
    against them. An unattributed change always gets its own mark, and so does the first
    measurement after one, so neither transition is swallowed by the equal-content shortcut.
    A fingerprint that could not be measured at all never counts as equal to an earlier
    unmeasurable one either, attributed or not: two blanks say nothing about the same bytes.
    `cause` is what the mark says about the change for a person reading a block, and `merge` the
    content a clean upstream merge turned into this one (see `record_paths`).
    """
    kept = [mark for mark in (marks or []) if isinstance(mark, dict)]
    mark = {"ts": now, "fp": fingerprint}
    if unknown:
        mark["unknown"] = True
    if cause:
        mark["cause"] = cause
    if merge:
        mark["merge"] = merge
    if (
        not kept
        or unknown
        or fingerprint is None
        or kept[-1].get("fp") != fingerprint
        or kept[-1].get("unknown")
    ):
        kept.append(mark)
    return kept[-CONTENT_MARKS_KEPT:]


def candidate_note(before, after):
    """One line for the model when the candidate it is editing opened, or its floor rose.

    The Stop hook can only teach after the model has tried to finish, at the price of one more
    full-context turn: 114 of the 391 blocks recorded in August 2026 were a missing receipt on
    a candidate the session had opened long before. This states the contract at the moment it
    starts to apply and again only when it tightens; every other edit stays silent.
    """
    old = cwg.candidate_shape(before)
    new = cwg.candidate_shape(after)
    if new is None:
        return None
    if old is not None and old["first_ts"] == new["first_ts"] and (
        old["persistent"], old["floor"]
    ) == (new["persistent"], new["floor"]):
        return None
    if not new["persistent"]:
        return (
            "[gate] Candidate opened: OPERATIONAL (a shell mutation, no lasting artifact yet). "
            "Close it with `[gate] operational: <pre-execution check>; <verified effect>` or "
            "`[gate] no-change: <reason>` as the last line of the final message (a lasting "
            "change the gate cannot see, such as prose written through the shell: "
            "`[gate] verified: <risk>; …`)."
        )
    opened = old is None or old["first_ts"] != new["first_ts"] or not old["persistent"]
    files = new["files"]
    names = []
    for path in reversed(cwg.durable_paths(cwg.marker_paths(after))):
        name = cwg.basename(path)
        if name not in names:
            names.append(name)
    shown = ": " + ", ".join(names[:3]) + (", …" if len(names) > 3 else "") if names else ""
    return (
        "[gate] Candidate {}: PERSISTENT, path floor {} ({} lasting file{}{}). Requires {}. Close "
        "it with `[gate] verified: {}; <candidate and decisive checks>` as the last line "
        "(pr-ready/draft-blocked only after autonomous closure).{}"
    ).format(
        "opened" if opened else "floor raised",
        new["floor"], files, "" if files == 1 else "s", shown,
        cwg.receipt_requirements(new["floor"]), new["floor"],
        displacement_note(before, after),
    )


def displacement_note(before, after):
    """What the candidate note adds when this mark replaced a candidate that was still open."""
    displaced = (after or {}).get("displaced")
    if not isinstance(displaced, dict) or not before or before.get("closed"):
        return ""
    if displaced.get("opened") != before.get("first_ts"):
        return ""
    return (
        " It replaced the candidate open since {} ({}); lane results and review rounds from before "
        "this point belong to that candidate, so this one's review starts at round 1."
    ).format(clock(displaced["opened"]), displacement_cause(displaced))


def clock(stamp):
    """A local time of day for the notes, or `?` when the stamp is not one."""
    return time.strftime("%H:%M:%S", time.localtime(float(stamp))) if cwg.valid_ts(stamp) else "?"


def displacement_cause(displaced):
    """Why a cycle replaced the one before it, in the words of the notes."""
    reasons = []
    if displaced.get("idle"):
        reasons.append("idle past {} hours".format(CANDIDATE_IDLE_LIMIT // 3600))
    was, now = displaced.get("was"), displaced.get("now")
    if was != now:
        reasons.append("branch {} → {}".format(identity_branch(was), identity_branch(now)))
    named = [cwg.basename(path) for path in displaced.get("by") or []]
    if named:
        reasons.append("{} not among its files".format(", ".join(named)))
    return "; ".join(reasons) or "a different candidate"


def identity_branch(identity):
    """The branch an identity names, or `unknown`."""
    ref = str(identity or "").rsplit("#", 1)[-1] if identity else ""
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else (ref or "unknown")


def tool_output_text(data):
    """The finished tool's output as text, one stream per line, whatever shape the harness
    delivered it in — the breaker matches the CLI's error lines at line start."""
    response = data.get("tool_response")
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return "\n".join(str(value) for value in response.values() if isinstance(value, str))
    try:
        return json.dumps(response, ensure_ascii=False)
    except Exception:
        return str(response)


def main():
    hook_started = time.monotonic()
    data = cwg.read_payload() or {}
    output = {"continue": True}
    try:
        path = cwg.edited_path(data.get("tool_input"))
        tool = str(data.get("tool_name") or "")
        event = str(data.get("hook_event_name") or "")
        is_shell = tool in cwg.SHELL_TOOLS
        policy = shell_policy(data) if is_shell else None
        lane = read_only_lane(data)

        session = cwg.session_key(data.get("session_id"))
        cwd = str(data.get("cwd") or os.getcwd())
        gated_edit = cwg.is_gated(path) and not scratch_file(path)
        marker_before = (
            cwg.read_json(cwg.marker_path(session)) if event == "PostToolUse" else None
        )

        if event == "PreToolUse":
            if is_shell:
                capture_packet(data, session)
                if policy != SHELL_READ_ONLY:
                    started = time.time()
                    command = str((data.get("tool_input") or {}).get("command") or "")
                    start, entered = directory_plan(command, cwd, tool)
                    # A command that begins by changing into a repository does its work there,
                    # not in the directory the hook was given: the Codex launch
                    # `REVIEW_ID=r; cd <repo> && codex exec …` run from C:/tmp expired its own
                    # verdict, because C:/tmp is no repository and nothing measured it (report
                    # a269a6fc). A start outside any repository measures no more than the hook's
                    # own directory does, so that stays the ground.
                    root_cache = {}
                    ground = start if start and repository_root(start, root_cache) else cwd
                    # The window is published before the command runs, so a session resolving a
                    # diff that overlaps it can see that someone else was writing.
                    cwg.publish_claims(
                        session, shell_start_ts=started, cwd=ground, now=started
                    )
                    open_marker = cwg.read_json(cwg.marker_path(session))
                    cwg.write_json(
                        shell_snapshot_path(data),
                        dict(
                            shell_snapshot(
                                ground, open_marker, entered,
                                origin=None if ground == cwd else cwd,
                            ),
                            ts=started,
                            head=head_commit(ground),
                            refs=cwg.refs_digest(ground),
                            start=ground,
                            merging=merge_in_progress(repository_root(ground, root_cache)),
                            integration=integration_baseline(
                                command, open_marker, hook_started + INTEGRATION_BASELINE_DEADLINE
                            ),
                        ),
                    )
            elif gated_edit:
                # Announced before the write lands, not after it: a claim published only once
                # the edit is done can arrive after a concurrent command has already resolved
                # its diff, which is the race this whole registry exists to close. Pending until
                # the edit actually completes - an announcement is not yet a write, and must not
                # excuse another session from one.
                cwg.publish_claims(session, paths=[path], pending=True, cwd=cwd)
            print(json.dumps({"continue": True}))
            return

        shell_paths = None
        observed = False
        floor = None
        snapshot_roots = []
        watched_roots = []
        # Whether anything watched the directory the command ran in. A command writes there by
        # default, so an empty delta from a snapshot of some other tree says nothing about it.
        home_ground = False
        resolving = is_shell and policy != SHELL_READ_ONLY
        shell_started = None
        if resolving:
            snapshot_file = shell_snapshot_path(data)
            before = stored_snapshot(cwg.read_json(snapshot_file))
            shell_started = before.get("ts")
            cwg.remove(snapshot_file)
            earlier_git = before.get("git")
            # Compared at the root the command started in: a command that moves the shell into
            # another directory moves the hook's cwd with it, and a snapshot taken there cannot be
            # compared with the one taken before (reports 5aadd867, 946d53ef).
            after = shell_snapshot(
                earlier_git["root"] if isinstance(earlier_git, dict) and earlier_git.get("root") else cwd
            )
            repo_changes = snapshot_changes(earlier_git, after["git"])
            # What an upstream merge brought in is git's computation, not this session's writing
            # (report f9920b99): it leaves the delta here and is handed on for the verdict carry.
            # Judged in the repository the command started in, whose HEAD the snapshot recorded.
            merged = set()
            if repo_changes:
                own = marker_before if marker_before is not None else cwg.read_json(cwg.marker_path(session))
                merged = merge_set_aside(
                    before, after["git"], {cwg.normalize_path(path) for path, _ in repo_changes},
                    set(cwg.marker_paths(own or {})), hook_started + MERGE_JUDGE_BUDGET,
                )
                repo_changes = [change for change in repo_changes
                                if cwg.normalize_path(change[0]) not in merged]
            # A rebase or a one-step merge onto upstream rewrites committed files and leaves no
            # delta at all; the candidate's own files it rewrote are judged the same way (report
            # c4c78b99).
            merged |= integration_set_aside(before, after["git"], marker_before,
                                            hook_started + MERGE_JUDGE_BUDGET)
            config_paths = changed_config_paths(before.get("config"), after["config"])
            # Of everything the snapshots disagree on, the paths whose bytes this command
            # rewrote. A watched home has no index to move a file through, so every change
            # there is one.
            rewrote = []
            # Each source answers for its own tree, so one of them proving nothing narrows what
            # the command is known not to have touched instead of discarding the other's answer.
            if repo_changes is not None:
                shell_paths, rewrote = changed_and_rewritten(repo_changes)
                snapshot_roots.append((after["git"] or {}).get("root"))
                home_ground = True
            if config_paths is not None:
                shell_paths = (shell_paths or []) + config_paths
                rewrote = rewrote + config_paths
                config_roots = after["config"].get("roots") or []
                watched_roots.extend(config_roots)
                home_ground = home_ground or any(
                    covers(root, cwg.normalize_path(before.get("start") or cwd), AGENT_CONFIG_SKIP)
                    for root in config_roots
                )
            # The other repositories the candidate lives in, and its lasting files in none: a
            # command run elsewhere can change them, and one that did not must not read as a
            # change nobody measured (reports b803660c, 946d53ef).
            earlier_repos = before.get("repos") or []
            skipped = int(before.get("skipped") or 0)
            for index, earlier in enumerate(earlier_repos):
                if time.monotonic() - hook_started > EXTRA_COMPARE_BUDGET:
                    # Left uncompared, the tree vouches for nothing, which the rules below
                    # already treat as unmeasured; the mark says how many were left out.
                    skipped += len(earlier_repos) - index
                    break
                if not isinstance(earlier, dict) or not earlier.get("root"):
                    continue
                changes = snapshot_changes(earlier, git_snapshot(earlier["root"]))
                if changes is None:
                    continue
                named, moved = changed_and_rewritten(changes)
                shell_paths = (shell_paths or []) + named
                rewrote = rewrote + moved
                snapshot_roots.append(earlier["root"])
            vouched = []
            for loose, token in (before.get("loose") or {}).items():
                if file_token(loose) != token:
                    shell_paths = (shell_paths or []) + [loose]
                    rewrote = rewrote + [loose]
                vouched.append(loose)
            # A command that ended in a repository no snapshot covered may have written there.
            landed = repository_root(cwd, {})
            moved_unseen = bool(landed) and not any(
                covers(cwg.normalize_path(root).rstrip("/"), landed) for root in snapshot_roots if root
            )
            unseen = landed if moved_unseen else None
            # The tree did change even when the merge accounts for all of it, so the command is
            # still recorded, and the carry needs that record.
            observed = bool(merged)
            if shell_paths:
                observed = True
                shell_paths, ambiguous = own_delta(
                    session,
                    cwd,
                    shell_paths,
                    before.get("ts"),
                    shell_write(data),
                    [(cwg.normalize_path(root), ()) for root in snapshot_roots if root]
                    + [(root, AGENT_CONFIG_SKIP) for root in watched_roots],
                )
                # A change under a root the command neither ran in nor names is another
                # session's work seen through a shared home, not this command's: on
                # 2026-09-04 a `glab api` loop run in a worktree inherited a HIGH floor from
                # the gate-ops session editing hooks under ~/.claude at that moment.
                unattributed = [
                    path for path in cwg.durable_paths(ambiguous)
                    if on_home_ground(path, cwd, snapshot_roots, data, elsewhere=True)
                ]
                floor = cwg.minimum_risk(unattributed) if unattributed else None
                # The same holds for what `own_delta` charged here: another session's write under a
                # configuration home lands in this delta whenever its claim comes after the diff or
                # its shell ran in some other tree (reports 27cd9fe8, ae5983b0, 8ae90973, 5ed394cc).
                # A command that neither ran there nor names the home did not write it.
                shell_paths = [
                    path for path in shell_paths
                    if not any(covers(tree, path, AGENT_CONFIG_SKIP) for tree in watched_roots)
                    or on_home_ground(path, cwd, snapshot_roots, data)
                ]

        try:
            if gated_edit:
                cwg.publish_claims(session, paths=[path], cwd=cwd)
                record_paths(data, [path or cwg.SHELL_MUTATION_PATH])
            elif resolving:
                if merged:
                    # This command wrote them, so a concurrent session must not be charged for them.
                    cwg.publish_claims(session, paths=sorted(merged), cwd=cwd)
                if shell_paths:
                    cwg.publish_claims(session, paths=shell_paths, cwd=cwd)
                    # A read-only lane's write is recorded like anyone's, and what it measurably
                    # changed expires the verdict through the paths themselves; only what the
                    # snapshot could not see is not held against the verdict the lane is producing.
                    record_paths(data, shell_paths, unresolved=moved_unseen,
                                 snapshot_roots=snapshot_roots,
                                 watched_roots=watched_roots, unattributed_risk=floor,
                                 write_capable_command=write_capable(data) and not lane,
                                 quiet=only_own_state(data),
                                 content_changed=rewrote, vouched=vouched, unseen=unseen,
                                 skipped=skipped, merged=merged,
                                 opening={"head": before.get("head"), "refs": before.get("refs"),
                                          "ts": before.get("ts")})
                elif not lane and (observed or not home_ground or policy == SHELL_UNKNOWN):
                    # An empty delta is not proof of no write: ignored files, and paths
                    # outside both the repository and the configuration homes, are invisible
                    # to either snapshot. Unknown or mutating commands therefore open a
                    # conservative operational candidate, and `None` — neither snapshot could
                    # be compared, as opposed to an empty list from one that could — proves
                    # nothing even for a validation command.
                    # `observed` carries a third case: the tree really did change and
                    # attribution gave every path away. That must still leave a candidate this
                    # session can be asked about rather than nothing at all. A read-only lane is
                    # the exception: it has no editing tool and its contract is to read, a write it
                    # makes where the snapshot looks is recorded above, and a mark here would
                    # expire the verdict it is still producing.
                    record_paths(
                        data,
                        [cwg.SHELL_MUTATION_PATH],
                        unresolved=not home_ground or moved_unseen,
                        snapshot_roots=snapshot_roots,
                        watched_roots=watched_roots,
                        unattributed_risk=floor,
                        write_capable_command=write_capable(data),
                        quiet=only_own_state(data),
                        vouched=vouched,
                        unseen=unseen,
                        skipped=skipped,
                        merged=merged,
                        no_snapshot=not before,
                        opening={"head": before.get("head"), "refs": before.get("refs"),
                                 "ts": before.get("ts")},
                    )
        finally:
            # Closed on every path out of this block, and deliberately not before it: closing
            # it ahead of the after-snapshot left an interval as wide as a full Git snapshot
            # in which this command had neither an open window nor its resolved claims, and a
            # session resolving inside that interval would take its writes for its own. An
            # exception during the resolution above this block leaves the window open instead,
            # which SHELL_WINDOW_LIMIT absorbs the same way it absorbs a killed command.
            if resolving:
                cwg.publish_claims(session, shell_start_ts=0)

        if event == "PostToolUse":
            if is_shell:
                # A Codex launch that the CLI itself refused (usage limit, model at capacity)
                # is recorded once, so the next candidate skips the lane instead of paying for
                # the same refusal again. Reading the command text here is attribution of an
                # outage, never proof that a review ran: that stays with the Stop hook.
                try:
                    # Imported here, not at the top: the breaker is optional, and a marker
                    # that cannot import it must still mark.
                    import codex_lane
                    codex_lane.record_from_command(
                        str((data.get("tool_input") or {}).get("command") or ""),
                        tool_output_text(data),
                        started=shell_started,
                    )
                except Exception:
                    pass
            # `marker_before`, not `before`: the shell branch above reuses `before` for its
            # snapshot, and comparing a snapshot to the marker announced the candidate on every
            # shell call.
            note = candidate_note(marker_before, cwg.read_json(cwg.marker_path(session)))
            if note:
                output["hookSpecificOutput"] = {
                    "hookEventName": "PostToolUse",
                    "additionalContext": note,
                }
    except Exception:
        pass
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
