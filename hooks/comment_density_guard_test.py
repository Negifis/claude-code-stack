import json
import os
import subprocess
import sys

HOOK = r"C:\Users\in\.claude\hooks\comment_density_guard.py"

CHRONY_BLOAT = """# Ubuntu's default is `makestep 1 3`: step only during the first three updates, slew
# afterwards.
# On this VM that turned a 387-second offset into a permanent one - chrony had eight
# sources online
# and could not correct it, because slewing that far takes days. VMware Tools was
# disciplining the
# same clock underneath, which is how it drifted in the first place; that is now
# disabled, leaving
# chrony as the only discipline and this box with no way back from a suspend or a host
# pause.
#
# It matters more than it used to: upstream DNS is DoT and unbound validates DNSSEC, so
# both certificate windows and signature windows fail closed on a wrong clock.
makestep 1 -1
"""

CHRONY_DENSE = """# Step clock on startup: suspended VM can resume with large skew, breaking TLS/DNSSEC.
makestep 1 -1
"""

OBVIOUS = """def total(items):
    return sum(i.price for i in items)
"""

WORKAROUND_UNTAGGED = """# The vendor SDK closes the socket without draining the read buffer, so the next
# request on the pooled connection reads the tail of the previous response.
# Their client caches the pool per process, and the reset only happens on an
# explicit close, which their context manager does not call.
# Upstream issue vendor-sdk#4412, open since 2024.
# Until that lands, force a fresh connection for every call on this path.
# The cost is one TCP handshake per request, which this endpoint can absorb.
# Do not switch this back to the pooled client without checking that issue.
# A pooled client here silently corrupts responses rather than failing.
session = build_session(pool=False)
"""

WORKAROUND_TAGGED = """# WORKAROUND: vendor-sdk#4412 - the SDK closes sockets without draining the read
# buffer, so a pooled connection returns the tail of the previous response.
# Their context manager never calls close(), so the pool is never reset.
# An unpooled session costs one handshake per call and is correct.
# Do not restore the pooled client until that issue is fixed: it corrupts
# responses silently instead of failing.
# Verified against sdk 3.9.2 and 4.0.1.
# Regression test: tests/test_vendor_pool.py::test_no_pool_reuse
# Owner: platform team.
session = build_session(pool=False)
"""

LINT_DIRECTIVES = """import os  # noqa: F401
# type: ignore[arg-type]
# pylint: disable=too-many-locals
# ruff: noqa: E501
# fmt: off
# shellcheck disable=SC2086
# mypy: ignore-errors
value = compute()
"""

NARRATION_ONELINE = """# Previously this used four workers, which deadlocked under load.
WORKERS = 8
"""

NARRATION_RU = """# Раньше здесь стоял таймаут 5 секунд, но оказалось, что этого мало.
TIMEOUT = 30
"""

DOCSTRING = '''def parse(payload):
    """
    Parse a wire payload into a Record.

    The wire format is a length-prefixed sequence of TLV triplets. Each triplet
    is a one-byte tag, a two-byte big-endian length, and that many bytes of
    value. Unknown tags are preserved verbatim so a round trip is lossless.

    Args:
        payload: raw bytes from the socket, without the frame header.

    Returns:
        Record with `tags` in wire order.

    Raises:
        ValueError: when a declared length runs past the end of the payload.
    """
    return Record(_triplets(payload))
'''

JSDOC = """/**
 * Format a duration for display.
 *
 * Rounds to the largest unit that keeps at least two significant digits, so
 * 3600 becomes "1h" and 5400 becomes "1.5h". Negative input is clamped to 0.
 *
 * @param {number} seconds - duration in seconds
 * @param {object} [opts]
 * @param {boolean} [opts.long] - spell the unit out
 * @returns {string}
 */
export function formatDuration(seconds, opts = {}) {
  return render(Math.max(0, seconds), opts);
}
"""

LICENSE_HEADER = """# Copyright 2026 Example Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
"""

MARKDOWN = """<!-- This is a long prose document -->
Some text that goes on and on about the investigation we did previously.
"""

ALGORITHM_UNTAGGED = """# Compensated summation. The running total loses the low-order bits of every
# addend once it grows past the addend's magnitude, and those losses accumulate
# linearly with the number of terms.
# Carrying the lost part in a separate compensation term and folding it into the
# next addend bounds the total error by a constant instead.
# The subtraction order matters: writing it the obvious way lets the optimiser
# cancel the two operations and silently restore the naive sum.
# Do not enable fast-math on this translation unit.
# The reference is Kahan 1965 and Higham's error analysis.
total = compensated_sum(values)
"""

ALGORITHM_TAGGED = ALGORITHM_UNTAGGED.replace("# Compensated summation.",
                                              "# ALGORITHM: compensated summation.")

MECHANICAL = """# This function takes the config dict and returns a Settings object.
# It reads the host key from the dict.
# It reads the port key from the dict.
# It reads the user key from the dict.
# It reads the password key from the dict.
# Then it builds the Settings object.
# Then it returns the Settings object.
# The caller is build_client below.
# The dict comes from load_config above.
# If a key is missing a KeyError is raised.
def to_settings(cfg):
    return Settings(**cfg)
"""

MODEST = """# Ordering matters: the index is dropped inside the same transaction that rebuilds it,
# so a crash between the two cannot leave the table unindexed.
with tx():
    drop_index()
    build_index()
    analyze()
"""

LIVE_BLOAT = """# makestep 1 -1: Ubuntu default (1 3) only steps the first 3 updates, then
# slews. On this VM VMware Tools disciplined the clock independently and was
# disabled, leaving chrony as sole discipline with no post-suspend/pause
# resync path. An 8-source-online offset of 387s exceeded the 3-step budget
# and could not slew back (would take ~1 day), so it stuck permanently.
# Upstream DNS is DoT + DNSSEC-validating unbound, so a wrong clock closes
# both cert and signature validity windows. Do not revert to "1 3".
makestep 1 -1
"""

SCATTERED = """def build(cfg):
    host = cfg["host"]
    port = int(cfg["port"])
    # Port 0 means "pick a free one" only on the loopback listener.
    listener = bind(host, port)
    pool = Pool(size=cfg.get("pool", 4))
    # The registry must exist before the first worker starts.
    registry.ensure()
    workers = [Worker(pool) for _ in range(cfg["workers"])]
    # Draining is cooperative: a worker mid-request finishes it.
    listener.on_close(drain(workers))
    metrics.bind(listener)
    # Health checks bypass the pool so a saturated pool still reports up.
    health.attach(listener, direct=True)
    return Server(listener, workers)
"""

FOUR_LINE = """# The lock is taken before the read, not after, because a writer that lands
# between the two would make the returned generation stale without any way
# for the caller to notice. Holding it across both is cheap here: the read
# is memory-local and never blocks.
with lock:
    return store.generation
"""

CASES = [
    ("A  chrony bloat",        "Edit",  "chrony.conf", "", CHRONY_BLOAT,        "deny"),
    ("A' chrony dense",        "Edit",  "chrony.conf", "", CHRONY_DENSE,        "pass"),
    ("B  obvious code",        "Edit",  "billing.py",  "", OBVIOUS,             "pass"),
    ("C  workaround cited",    "Edit",  "client.py",   "", WORKAROUND_UNTAGGED, "pass"),
    ("C' workaround tagged",   "Edit",  "client.py",   "", WORKAROUND_TAGGED,   "pass"),
    ("D  narration one-line",  "Edit",  "conf.py",     "", NARRATION_ONELINE,   "deny"),
    ("D' narration russian",   "Edit",  "conf.py",     "", NARRATION_RU,        "deny"),
    ("E  lint directives",     "Edit",  "m.py",        "", LINT_DIRECTIVES,     "pass"),
    ("F  python docstring",    "Edit",  "parse.py",    "", DOCSTRING,           "pass"),
    ("G  jsdoc",               "Edit",  "fmt.js",      "", JSDOC,               "pass"),
    ("H  license header",      "Write", "new.py",      "", LICENSE_HEADER,      "pass"),
    ("I  markdown untouched",  "Edit",  "README.md",   "", MARKDOWN,            "pass"),
    ("J  algorithm untagged",  "Edit",  "sum.py",      "", ALGORITHM_UNTAGGED,  "deny"),
    ("J' algorithm tagged",    "Edit",  "sum.py",      "", ALGORITHM_TAGGED,    "pass"),
    ("K  mechanical retelling","Edit",  "cfg.py",      "", MECHANICAL,          "deny"),
    ("L  modest rationale",    "Edit",  "migrate.py",  "", MODEST,              "pass"),
    ("M  multiedit bloat",     "MultiEdit", "srv.go",  "", CHRONY_BLOAT.replace("#", "//"),
                                                                                "deny"),
    ("N  no-op edit",          "Edit",  "srv.go",      CHRONY_DENSE, CHRONY_DENSE, "pass"),
    ("O  live model bloat",    "Edit",  "chrony.conf", "", LIVE_BLOAT,          "deny"),
    ("P  scattered one-liners","Edit",  "server.py",   "", SCATTERED,           "pass"),
    ("Q  four-line rationale", "Edit",  "store.py",    "", FOUR_LINE,           "pass"),
]

TAGGED_ESSAY = "# WORKAROUND: the pool is disabled here.\n" + ("# filler line of prose that carries almost nothing at all\n" * 15) + "session = build(pool=False)\n"

GPL_HEADER = "# " + "\n# ".join([
    "Copyright (C) 2026 Example Corp.",
    "",
    "This program is free software: you can redistribute it and/or modify",
    "it under the terms of the GNU General Public License as published by",
    "the Free Software Foundation, either version 3 of the License, or",
    "(at your option) any later version.",
    "",
    "This program is distributed in the hope that it will be useful,",
    "but WITHOUT ANY WARRANTY; without even the implied warranty of",
    "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the",
    "GNU General Public License for more details.",
    "",
    "You should have received a copy of the GNU General Public License",
    "along with this program.  If not, see <https://www.gnu.org/licenses/>.",
]) + "\n\nimport sys\n"

CASES += [
    ("R  tagged essay",        "Edit",  "pool.py",  "", TAGGED_ESSAY, "deny"),
    ("S  gpl header",          "Write", "gpl.py",   "", GPL_HEADER,   "pass"),
]
# Verified false positives from the adversarial review round 1 -- these must pass.
FP_PREVIOUSLY = "# reuse previously computed hash to avoid rescanning the tree\nh = cache[key]\n"
FP_WHICH_IS_WHY = "# the API returns milliseconds, which is why we divide by 1000\nsecs = ms / 1000\n"
FP_COMPAT_OLD = "# COMPAT: the old version of the daemon sends bare LF, not CRLF\nline = raw.rstrip(b\"\\r\\n\")\n"
FP_REMOVED_UPSTREAM = "# this option was removed upstream in 4.2 and is ignored there\nopts[\"legacy\"] = True\n"
FP_CONTEXT_WORD = "# context: the caller already holds the write lock here\nstore.flush()\n"
FP_CULPRIT = "# a stale DNS entry was the culprit for the 5s connect latency\nresolver.cache_ttl = 30\n"

# Stray triple-quote closer must not swallow the code that follows it.
STRAY_TRIPLE = 'X = """\nhello\n"""\ny = 1\nz = 2\n'

DOTFILE = "# " + "\n# ".join(["filler narration line %d that says nothing" % i for i in range(9)]) + "\nnode_modules\n"

CASES += [
    ("T  fp: previously computed", "Edit", "hash.py",   "", FP_PREVIOUSLY,       "pass"),
    ("U  fp: which is why",        "Edit", "time.py",   "", FP_WHICH_IS_WHY,     "pass"),
    ("V  fp: COMPAT old version",  "Edit", "proto.py",  "", FP_COMPAT_OLD,       "pass"),
    ("W  fp: removed upstream",    "Edit", "opts.py",   "", FP_REMOVED_UPSTREAM, "pass"),
    ("X  fp: context: prefix",     "Edit", "lock.py",   "", FP_CONTEXT_WORD,     "pass"),
    ("Y  fp: was the culprit",     "Edit", "dns.py",    "", FP_CULPRIT,          "pass"),
    ("Z  stray triple quote",      "Edit", "lit.py",    "", STRAY_TRIPLE,        "pass"),
    ("AA dotfile now covered",     "Edit", ".gitignore","", DOTFILE,             "deny"),
]
# Verified false positives from adversarial review round 2 (F9/F10) -- must pass.
FP_USER_FLAG = "# bail early if the user requested --quiet\nif opts.quiet: return\n"
FP_BEFORE_COMMIT = "# flush buffers before the commit is durable\nbuf.flush()\n"
FP_SECOND_ATTEMPT = "# on the second attempt, fall back to the mirror URL\nurl = MIRROR\n"
FP_SAFETY_ATTEMPT = "# SAFETY: retries capped at 3; on the third attempt we give up\nMAX_RETRIES = 3\n"
FP_POSTMORTEM_CITED = "# see the 2024 postmortem: keep this under 100ms, https://internal/pm/123\nbudget_ms = 100\n"
FP_RU_INITIAL = "# \u0438\u0437\u043d\u0430\u0447\u0430\u043b\u044c\u043d\u043e \u043f\u0443\u0441\u0442\u043e\u0439 \u0441\u043f\u0438\u0441\u043e\u043a, \u0437\u0430\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u043b\u0435\u043d\u0438\u0432\u043e\nitems = []\n"
FP_RU_EARLIER = "# \u0441\u043c. \u0440\u0430\u043d\u0435\u0435 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u043d\u044b\u0439 \u0442\u0438\u043f \u0432 models.py\nrec: Record = load()\n"

# Still narration -- must deny.
TP_USER_TASK = "# the user asked for this to be configurable per tenant\nTENANT_OVERRIDE = True\n"
TP_ATTEMPT_FAILED = "# first attempt failed because the pool was shared across threads\npool = local_pool()\n"
TP_POSTMORTEM_BARE = "# postmortem: the queue backed up for six hours before anyone noticed\nALERT_AFTER = 60\n"

# The ALGORITHM: bypass a live session produced: 12-line mechanical retelling.
ALGO_BYPASS = "# ALGORITHM: name.strip().lower().replace() pipeline, step by step:\n" + "".join(
    ["#   %d. step %d explained in plain words for the reader\n" % (i, i) for i in range(1, 12)]
) + "def normalize(name):\n    return name.strip()\n"

CASES += [
    ("AB fp: user requested flag", "Edit", "cli.py",   "", FP_USER_FLAG,        "pass"),
    ("AC fp: before the commit",   "Edit", "tx.py",    "", FP_BEFORE_COMMIT,    "pass"),
    ("AD fp: on second attempt",   "Edit", "retry.py", "", FP_SECOND_ATTEMPT,   "pass"),
    ("AE fp: SAFETY third attempt","Edit", "retry.py", "", FP_SAFETY_ATTEMPT,   "pass"),
    ("AF fp: cited postmortem",    "Edit", "perf.py",  "", FP_POSTMORTEM_CITED, "pass"),
    ("AG fp: ru izn. pustoy",      "Edit", "ru.py",    "", FP_RU_INITIAL,       "pass"),
    ("AH fp: ru ranee obyavl.",    "Edit", "ru.py",    "", FP_RU_EARLIER,       "pass"),
    ("AI tp: user asked for this", "Edit", "cfg.py",   "", TP_USER_TASK,        "deny"),
    ("AJ tp: first attempt failed","Edit", "pool.py",  "", TP_ATTEMPT_FAILED,   "deny"),
    ("AK tp: bare postmortem",     "Edit", "alert.py", "", TP_POSTMORTEM_BARE,  "deny"),
    ("AL algorithm-tag bypass",    "Edit", "slug.py",  "", ALGO_BYPASS,         "deny"),
]
# Verified false positives from adversarial review round 3 (F12) -- must pass.
FP_REQUESTED_IT = "# resend the invite if the user requested it\nnotify(user)\n"
FP_ATTEMPT_MID = "# fall back to IPv4 when the first attempt failed with ETIMEDOUT\nfamily = AF_INET\n"
FP_BEFORE_FIX = "# remove this shim before the fix ships upstream\nshim.install()\n"

# Still narration -- must deny.
TP_BEFORE_CHANGE = "# before this change, the pool was shared across every worker\npool = local_pool()\n"

CASES += [
    ("AM fp: requested it",     "Edit", "notify.py", "", FP_REQUESTED_IT, "pass"),
    ("AN fp: attempt mid-line", "Edit", "net.py",    "", FP_ATTEMPT_MID,  "pass"),
    ("AO fp: before the fix",   "Edit", "shim.py",   "", FP_BEFORE_FIX,   "pass"),
    ("AP tp: before this change","Edit","pool.py",   "", TP_BEFORE_CHANGE,"deny"),
]
# Russian user-phrase asymmetry, simplify wave 3.
FP_RU_DOMAIN_USER = "# \u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u0445\u043e\u0442\u0435\u043b \u043e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f, \u043f\u043e\u044d\u0442\u043e\u043c\u0443 \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u043c \u0444\u043b\u0430\u0433\nif not user.notify: return\n"
TP_RU_CHAT_USER = "# \u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u043f\u0440\u043e\u0441\u0438\u043b \u043c\u0435\u043d\u044f \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043a\u044d\u0448 \u0438\u043c\u0435\u043d\u043d\u043e \u0437\u0434\u0435\u0441\u044c\nCACHE = {}\n"

CASES += [
    ("AQ fp: ru domain user",  "Edit", "notify.py", "", FP_RU_DOMAIN_USER, "pass"),
    ("AR tp: ru chat user",    "Edit", "cache.py",  "", TP_RU_CHAT_USER,   "deny"),
]

def run(tool, path, old, new, session):
    if tool == "Write":
        payload = {"file_path": path, "content": new}
    elif tool == "MultiEdit":
        payload = {"file_path": path, "edits": [{"old_string": old, "new_string": new}]}
    else:
        payload = {"file_path": path, "old_string": old, "new_string": new}
    body = json.dumps({"session_id": session, "cwd": os.getcwd(),
                       "hook_event_name": "PreToolUse", "tool_name": tool,
                       "tool_input": payload})
    proc = subprocess.run([sys.executable, HOOK], input=body, capture_output=True, text=True,
                          encoding="utf-8")
    if proc.returncode != 0:
        return "error", proc.stderr.strip()[:200]
    out = json.loads(proc.stdout)
    hs = out.get("hookSpecificOutput") or {}
    if hs.get("permissionDecision") == "deny":
        return "deny", hs.get("permissionDecisionReason", "").splitlines()[0]
    return "pass", ""


def main():
    ok = True
    for i, (name, tool, path, old, new, want) in enumerate(CASES):
        got, detail = run(tool, path, old, new, "case%d-%d" % (os.getpid(), i))
        flag = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print("{} {:24} want={:4} got={:5} {}".format(flag, name, want, got, detail))
    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
