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

If Codex fails before a verdict, resume that same session once:

- ordinarily, hand the round to `/adversarial-review-internal` with the ledger and the packet so
  far, and say that the native engine reviewed because Codex was unavailable. The round budget
  carries over; a lane that died verdict-less spent no round;
- when the user explicitly required cross-engine evidence, no fallback exists: record
  `REVIEW_UNAVAILABLE` after the failed resume and enter the canonical autonomous closure, which
  will normally end `DRAFT_BLOCKED` unless the required evidence becomes available.

## Reporting

Summarize the evidence and stable ledger; do not stream every intermediate review response to the
user. State that the independent opinion came from Codex when that fact matters. Finish through
development-verification with PR_READY, DRAFT_BLOCKED, or an ordinary verified receipt.
