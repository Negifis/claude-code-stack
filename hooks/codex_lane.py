"""
Codex lane circuit breaker.

The transcripts of 2026-08 show the Codex review lane failing to deliver a verdict in about
60% of its launches, almost always for one of two deterministic reasons the CLI prints itself:
the ChatGPT usage limit ("You've hit your usage limit ... try again at 3:30 PM") or a model
at capacity. Each failed launch still cost the parent several turns at full context, and then
the native lane ran anyway. So the failure is recorded once, with the time the CLI itself
named, and every later candidate checks the record before launching Codex instead of
rediscovering the outage the expensive way.

State: `<config home>/state/codex-lane.json`  {"unavailable_until": ts, "reason": str,
"recorded_ts": ts}. Fail-open everywhere: an unreadable record means "available".

CLI:  python codex_lane.py check      -> CODEX_LANE: available | unavailable until <time> (<why>)
      python codex_lane.py record <path-to-stderr-file>   (used by tests and by hand)
      python codex_lane.py clear
"""
import datetime
import json
import os
import re
import sys
import time

# Anchored to the CLI's own error lines. The stderr capture of a review also echoes the packet
# and the reviewer's report, and both may quote these phrases (a review of this very file
# does), so a phrase anywhere in the text is not evidence of an outage: only a line the CLI
# printed as an error is.
LIMIT_RE = re.compile(
    r"^ERR(?:OR)?:\s*You've hit your usage limit[^\n]*?try again at (\d{1,2}):(\d{2})\s*(AM|PM)?",
    re.IGNORECASE | re.MULTILINE,
)
LIMIT_PLAIN_RE = re.compile(r"^ERR(?:OR)?:\s*You've hit your usage limit", re.IGNORECASE | re.MULTILINE)
CAPACITY_RE = re.compile(
    r"^ERR(?:OR)?:\s*(?:Selected )?model is at capacity", re.IGNORECASE | re.MULTILINE
)
# When the CLI names no retry time, this is how long a launch is not worth trying again.
DEFAULT_OUTAGE = 30 * 60.0
# A usage-limit message without a time: the limit windows are hours, not minutes.
DEFAULT_LIMIT_OUTAGE = 3 * 3600.0
# The longest a single record may idle the lane, whatever time the CLI named: a wrong parse
# then costs one failed launch later, not a day of native-only reviews.
MAX_OUTAGE = 6 * 3600.0
# The CLI prints its refusal last; the tail is enough and keeps the packet echo out of reach.
STDERR_TAIL_BYTES = 4000
STDERR_REDIRECT_RE = re.compile(r"2>\s*\"?([^\s\"&|;]+\.err)\"?")


def config_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def state_path():
    return os.path.join(config_home(), "state", "codex-lane.json")


def read_state():
    try:
        with open(state_path(), encoding="utf-8") as stream:
            data = json.load(stream)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_state(data):
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "{}.{}.tmp".format(path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def clear_state():
    try:
        os.remove(state_path())
    except FileNotFoundError:
        pass
    except Exception:
        return False
    return True


def retry_time(hour, minute, meridiem, now=None):
    """The next local wall-clock moment the CLI named, today or tomorrow."""
    now = datetime.datetime.now() if now is None else now
    hour = int(hour)
    if meridiem:
        if meridiem.upper() == "PM" and hour < 12:
            hour += 12
        if meridiem.upper() == "AM" and hour == 12:
            hour = 0
    candidate = now.replace(hour=hour % 24, minute=int(minute), second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    return candidate.timestamp()


def outage_from_text(text, now=None):
    """(unavailable_until, reason) when the text carries a known outage, else None."""
    text = str(text or "")
    now_ts = time.time() if now is None else now.timestamp()
    match = LIMIT_RE.search(text)
    if match and int(match.group(2)) <= 59 and int(match.group(1)) <= 23:
        until = min(
            retry_time(match.group(1), match.group(2), match.group(3), now=now),
            now_ts + MAX_OUTAGE,
        )
        return until, "codex usage limit, CLI said try again at {}:{}{}".format(
            match.group(1), match.group(2), (" " + match.group(3).upper()) if match.group(3) else ""
        )
    if LIMIT_PLAIN_RE.search(text):
        return now_ts + DEFAULT_LIMIT_OUTAGE, "codex usage limit"
    if CAPACITY_RE.search(text):
        return now_ts + DEFAULT_OUTAGE, "codex model at capacity"
    return None


def record_outage(text, now=None):
    """True when `text` names a known outage, whether or not the stored record changed: an
    outage already recorded with a later retry time is kept, never shortened."""
    found = outage_from_text(text, now=now)
    if not found:
        return False
    until, reason = found
    current = read_state() or {}
    # Never shorten an outage already recorded from a later retry time.
    if float(current.get("unavailable_until") or 0) > until:
        return True
    return write_state({"unavailable_until": until, "reason": reason, "recorded_ts": time.time()})


ASSIGNMENT_RE = re.compile(r"(?:^|[;&|\s])([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|]+)")
VARIABLE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# Where the lean review command keeps its captures; the fallback looks here when the redirect
# names a variable the hook cannot resolve from the command text.
CAPTURE_GLOB = "C:/tmp/codex-*.err"
# A capture written before the launch belongs to an earlier launch. Used only when the launch
# time is unknown to the caller.
CAPTURE_HORIZON = 30 * 60.0


def windows_path(raw):
    """Git Bash spells C:\\tmp as /c/tmp; the hook runs under the Windows Python."""
    drive = re.match(r"^/([a-zA-Z])/(.*)$", raw)
    return "{}:/{}".format(drive.group(1).upper(), drive.group(2)) if drive else raw


def stderr_file_of(command):
    """The stderr capture file a codex launch names, if the command redirects one.

    The hook sees the command as the agent wrote it, so a redirect such as
    `2>/c/tmp/codex-${REVIEW_ID}.err` arrives unexpanded; the variable is resolved from an
    assignment in the same command text when there is one. A path that still carries a
    variable is returned as-is and will not exist, which sends `record_from_command` to the
    newest capture instead.
    """
    text = str(command or "")
    match = STDERR_REDIRECT_RE.search(text)
    if not match:
        return None
    return windows_path(resolve_variables(text, match.group(1)))


def resolve_variables(command, token):
    """`token` with `$VAR`/`${VAR}` replaced from assignments in the same command text.

    A variable the command does not assign stays as written, so a caller can tell an unresolved
    path from a real one.
    """
    assignments = {
        name: value.strip("\"'") for name, value in ASSIGNMENT_RE.findall(str(command or ""))
    }
    return VARIABLE_RE.sub(lambda m: assignments.get(m.group(1), m.group(0)), str(token or ""))


def newest_capture(started=None, now=None):
    """The capture file written most recently, provided it was written after `started`."""
    import glob
    now = time.time() if now is None else now
    floor = float(started) if started else now - CAPTURE_HORIZON
    best = None
    for path in glob.glob(CAPTURE_GLOB):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= floor - 1.0 and (best is None or mtime > best[0]):
            best = (mtime, path)
    return best[1] if best else None


def record_from_command(command, tool_output="", started=None):
    """Inspect a finished `codex exec` call: its stderr file and what the tool returned.

    `started` is when the command began, when the caller knows it; it bounds the fallback to a
    capture this launch could have written.
    """
    if not re.search(r"\bcodex\s+exec\b", str(command or "")):
        return False
    texts = [str(tool_output or "")[-STDERR_TAIL_BYTES:]]
    path = stderr_file_of(command)
    if path and not os.path.isfile(path):
        path = newest_capture(started=started)
    if path:
        try:
            with open(path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - STDERR_TAIL_BYTES))
                texts.append(stream.read().decode("utf-8", "replace"))
        except Exception:
            pass
    return any(record_outage(text) for text in texts)


def status(now_ts=None):
    """(available, message). `now_ts` is an epoch float, unlike the `datetime` the parsers take."""
    now_ts = time.time() if now_ts is None else now_ts
    data = read_state()
    if not data:
        return True, "CODEX_LANE: available"
    try:
        until = float(data.get("unavailable_until") or 0)
        if until <= now_ts:
            return True, "CODEX_LANE: available"
        when = datetime.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        # A record that cannot be read is no record: the lane is tried, not idled.
        return True, "CODEX_LANE: available"
    return False, "CODEX_LANE: unavailable until {} ({}); use /adversarial-review-internal now".format(
        when, data.get("reason") or "recorded outage"
    )


def main(argv):
    command = argv[1] if len(argv) > 1 else "check"
    if command == "check":
        available, message = status()
        print(message)
        return 0 if available else 3
    if command == "record":
        text = ""
        if len(argv) > 2:
            try:
                with open(argv[2], encoding="utf-8", errors="replace") as stream:
                    text = stream.read()
            except Exception as exc:
                print("cannot read {}: {}".format(argv[2], exc))
                return 2
        else:
            text = sys.stdin.read()
        print("recorded" if record_outage(text) else "no outage found")
        return 0
    if command == "clear":
        print("cleared" if clear_state() else "could not clear")
        return 0
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
