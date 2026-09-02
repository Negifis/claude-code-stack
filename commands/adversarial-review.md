---
description: "Run the default cross-engine Codex adversarial review lane for a candidate that needs independent judgment."
argument-hint: "[--required] [plan|code|file-path] [optional named risk]"
---

# Codex Adversarial Review

This is the default review lane. Codex judges the candidate from outside the model that wrote
it, which is the point of independent review; `/adversarial-review-internal` takes over only
when Codex cannot deliver a verdict or the user declines it.

Read development-verification before starting. It remains the single source of truth for risk,
blocking severity, the three-round cap, autonomous closure, and publication.

Raw arguments: $ARGUMENTS

## 0. Check the lane before paying for it

```bash
python "C:\Users\in\.claude\hooks\codex_lane.py" check
```

`CODEX_LANE: available` — continue below. `CODEX_LANE: unavailable until …` — the CLI itself
refused an earlier launch (usage limit, model at capacity) and named when to retry; do not
launch Codex for this round, hand the round to `/adversarial-review-internal` at once and say
that the native engine reviewed because Codex is out until that time. The record is written by
the PostToolUse hook from the CLI's own error text and expires on its own. In August 2026, 132
of 222 Codex review launches returned no verdict, most of them for exactly these two reasons,
and every one of them cost the parent several full-context turns before the native lane ran
anyway.

Run Codex in the foreground so its verdict is observable: the review reaches the transcript as
the result of the call, and a detached run leaves only a launch acknowledgement — as does
retrieving a finished job, which reviewed a candidate the gate cannot see. Carry the literal
marker `CODE_WORK_GATE_REVIEW` in the actual shell command: it is how a review that ran but could
not be attributed still counts as an unresolved lane instead of vanishing, and it keeps unrelated
command output out of the ledger. The result must end with its `VERDICT:` line; require that
verdict in the packet and return the output verbatim.

The verdict counts because Codex's own session log shows it saying that text while the call was
open. So return the review as Codex printed it — summarizing or trimming it in the same call
leaves nothing the log can vouch for — and let a review that ran but could not be attributed
stand as an unresolved lane rather than restating its verdict yourself.

When the user explicitly requires cross-engine evidence, `--required` is mandatory: include the
literal marker `CODE_WORK_GATE_REQUIRED` in the actual foreground shell command, which binds
this exact result to the candidate. A required call that answers without a verdict counts as an
unavailable reviewer and ends the work draft-blocked rather than falling back to the native
lane. Never use the marker for an ordinary round.

## The review lane runs lean

A review needs to read the candidate, run its checks, and reach current documentation. It does
not need this machine's MCP servers, plugins, memories, image or browser tools, and it must not
run the session hooks — their Stop hook fires on every round, fails visibly, and buys the review
nothing. Loading them costs ~7k tokens of context before the first file is read and spawns a
dozen servers per round.

So launch the lane with the user configuration ignored and only what a reviewer uses turned back
on. Session logging is unaffected: rollouts still land in `CODEX_HOME/sessions`, which is what
the gate binds the verdict to.

```bash
timeout 3600 codex exec --ignore-user-config \
  --disable plugins --disable hooks --disable memories \
  --disable multi_agent --disable multi_agent_v2 \
  --disable computer_use --disable browser_use --disable browser_use_external \
  --disable image_generation --disable apps --disable in_app_browser \
  --disable skill_search --disable tool_suggest \
  -m gpt-5.6-sol -c model_reasoning_effort=high -c tools.web_search=true \
  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
  - < /c/tmp/codex-packet-${REVIEW_ID}.md 2>/c/tmp/codex-${REVIEW_ID}.err  # CODE_WORK_GATE_REVIEW
```

No `--json` here, and that is the load-bearing part: the gate reads the last line of what the
call returned, so the result has to *be* the review. With the hooks disabled nothing appends a
trailer and plain mode prints the final message alone — verified: a lean run asked for two lines
returned exactly those two, the verdict last. A JSONL stream ends in `turn.completed` instead,
which parses as malformed however good the review was. Keep stderr aside for diagnostics; the
`2>/c/tmp/codex-<id>.err` capture is also what the outage record above is read from.

`REVIEW_ID` is the id minted for this gate invocation, and the packet is the file written for
this round — one packet per round, overwritten on a resume. Plain mode prints no session id, so
capture it from the log the round itself wrote: list the `rollout-*.jsonl` names before
launching, list them again when the call returns, and take the difference. Exactly one new file
is the round's own session, and its name carries the id to resume. Anything else — none, or
several because another Codex session ran alongside — means the id is unknown, and the delta
round then starts a fresh review with the whole packet rather than resuming a stranger's thread.
Compare the sets, never the timestamps: those names are truncated to whole seconds, so a
concurrent session can look newer than the round that actually ran. `--last` is only for the
case where nothing else on the machine could have started a session in between.

The sandbox bypass is not optional here: the Windows restricted-token sandbox fails to start
(`CreateProcessWithLogonW 1326`), so `-s read-only` leaves the reviewer unable to run a single
command; the read-only discipline comes from the role text in the packet instead. Both names of
the multi-agent feature stay listed because only one is live in this build and defaults drift.

Shell and web search both survive this — verified by a probe that read a local file and searched
the web in one lean turn. Delta rounds repeat the same flags on `codex exec resume`, since they
are per-invocation and `--ignore-user-config` does not disturb the stored session. Reasoning
effort is `high` for every round: `xhigh` round-1 runs were the ones that exhausted the Codex
usage window in August 2026 and turned the lane into a fallback to the native reviewer;
`reviewer.toml` on the Codex side runs the same role at `high`.

Four residuals worth knowing. Skills are discovered from `CODEX_HOME/skills` rather than from
the config, so they still load and still consume their 2% budget; there is no global switch, and
`--ignore-user-config` even drops the per-skill disables the user set. The global
`CODEX_HOME/AGENTS.md` loads as well — instructions are not configuration, so ignoring the config
leaves them in place, and on this machine that is another 9.7 KB in every round. MCP servers are
defined only in that config, so ignoring it should leave none — the probe showed no MCP activity and
started immediately instead of spending the baseline's twenty seconds spawning servers, which is
consistent but is not a direct enumeration. And a documentation MCP such as context7 is not
re-added on purpose: its key would end up in the command line, and web search already covers
current documentation. Add it through an environment variable if a round genuinely needs it.

## Candidate and packet

Open every packet with the contents of `~/.claude/agents/adversarial-reviewer.md` below its front
matter, verbatim and first. That file is the reviewer's role on this machine: the native lane
gets it from the harness through `subagent_type`, and this lane gets it because you put it there.
The gate looks for it in what the session was given, so a packet without it is an errand whose
verdict is never heard — and a paraphrase is not the role.

Then freeze the exact plan or code candidate, acceptance criteria, project rules, and decisive
check results. Give Codex a self-contained read-only packet with:

- review round number;
- exact diff/base or plan path;
- relevant files and commands;
- risk and named attack surface;
- stable finding ledger;
- remediation delta and affected interfaces on later rounds;
- severity bar and the required final verdict.

Do not pass the conversation or secrets. Disable nested reviewer delegation for this lane. Keep
one external review session and resume it for delta rounds when the integration supports resume.

## Run once, then return to the canonical state machine

Keep one Codex session and resume it with only the delta and ledger when another canonical round
or closure validation is actually required. After every result, return control to
`development-verification`; this command does not redefine its severity threshold, caps,
transitions, autonomous closure, or authority boundary.

If Codex fails before a verdict:

- when stderr names a usage limit or a model at capacity, do not resume: the record is already
  written, and the round goes to `/adversarial-review-internal` now;
- for any other failure, resume that same session once; if that fails too, ordinarily hand the
  round to `/adversarial-review-internal` with the ledger and the packet so far, and say that the
  native engine reviewed because Codex was unavailable. The round budget carries over; a lane
  that died verdict-less spent no round;
- when the user explicitly required cross-engine evidence, no fallback exists: record
  `REVIEW_UNAVAILABLE` after the failed resume and enter the canonical autonomous closure, which
  will normally end `DRAFT_BLOCKED` unless the required evidence becomes available.

## Reporting

Summarize the evidence and stable ledger; do not stream every intermediate review response to the
user. State that the independent opinion came from Codex when that fact matters. Finish through
development-verification with PR_READY, DRAFT_BLOCKED, or an ordinary verified receipt.
