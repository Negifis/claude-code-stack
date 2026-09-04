---
name: development-verification
description: Final checks and finite risk-based review before completing implementation work, including autonomous closure after escalation.
---

# Development Verification

Establish the completion bar with the smallest amount of checking and independent judgment
that the risk requires. This skill is the only code-work gate. Do not wrap it in another review
loop, and do not run any part of it as a ritual.

## 1. Classify the work

- **PERSISTENT** — a lasting artifact: repository source, configuration, infrastructure,
  schema, documentation, or executable agent instructions. Later runs read and re-execute it,
  so a defect keeps costing. Sections 3–9 apply.
- **OPERATIONAL** — an effect on a live system: commands against a device, server, database,
  cloud account or account state, plus the throwaway scripts and scratchpad files written to
  carry them out. The cost lands once, at execution. Section 2 applies.

One changed lasting artifact makes the candidate persistent. Mixed work is graded on its
persistent half; the operational half is verified by its observed effect. Work that changed
nothing — inspection, diagnosis, a report — closes with `[gate] no-change:` and runs no cycle.

The difference is *when* judgment still changes the outcome: before publication for a lasting
artifact, before the command runs for a live system.

## 2. Operational track

Before running anything that changes a live system:

1. Confirm the exact target from the system itself — disk, host, interface, database, table,
   account, environment — not from the plan or from memory.
2. Establish reversibility: a backup, an export, a rollback path, or an explicit statement that
   the action cannot be undone and what the blast radius is.
3. Run the read-only precheck or dry run first when one exists and compare it with what the
   change assumes.
4. If the action is irreversible with a wide blast radius, spend independent judgment here —
   one adversarial review of the plan, or a question to the user for an authority the rules
   reserve. Never after the fact.

After running it: verify the effect against the system itself (what should have changed did,
what should have survived did, the rollback path still exists) and separate what was verified
from what is merely expected.

No simplify pass and no code review on a throwaway script that already executed. Close with
`[gate] operational: <what was established before executing>; <verified effect>` or
`[gate] no-change: <reason>`. Both require this skill to have been invoked; invoking it before
execution, where it belongs, satisfies that.

A lasting change that was undone — a rebase probe aborted, an edit reverted — leaves the
repository on the commit the candidate opened on and clean; the gate reads that from git and
accepts `operational` or `no-change` for it, so do not manufacture a review for nothing.

## 3. Classify risk

- **LOW** — docs, comments, formatting, tests-only changes, generated output with no semantic
  effect, or a local reversible configuration edit with no runtime/public-contract change.
  Run the relevant deterministic check. No independent review.
- **STANDARD** — a bounded logic or user-visible change with limited blast radius. Run
  affected tests and static checks. Add a simplify lane or independent review only for a
  concrete complexity, uncertainty, or integration risk.
- **HIGH** — security/auth/permissions, data/schema/migrations, concurrency/distributed
  state, public contracts, production/release, irreversible effects, or a broad
  cross-component diff. Run affected checks, one broad final check when warranted, one
  simplify lane, and one independent adversarial review.

Judge risk by blast radius, reversibility, data sensitivity, observability, and rollback — not
line count or a sensitive-looking filename. The Stop hook enforces a lower bound on lasting
artifacts so ambiguity cannot downgrade work:

- tests/specs/fixtures only may be LOW;
- other source, configuration, infrastructure or executable agent instructions are at least
  STANDARD;
- non-test paths containing auth, permissions/RBAC, security/crypto/secrets/tokens,
  migrations/schema, billing/payment/checkout, deploy/release, or production are at least
  HIGH, as are files whose purpose is to hold a secret;
- agent configuration that executes or grants authority — hooks, agent, command and skill
  definitions, settings and MCP wiring, `CLAUDE.md`, `AGENTS.md` — is at least HIGH. Prose
  beside it (rules, decisions, runbooks) is ordinary documentation at STANDARD.

Throwaway artifacts and unresolved shell mutations set no risk floor: they are operational work
under section 2. Raise risk above the floor when behavior requires it; never lower it. The
hooks name the candidate's class and floor when it opens and on every prompt while it is open,
so the receipt shape is never a surprise.

## 4. Verify the candidate

- Bind evidence to the exact candidate diff/revision and the relevant configuration,
  dependency, environment, and artifact inputs.
- Run narrow checks first. After a remediation, rerun only checks whose covered code or inputs
  changed. Run a broad suite once on the final high-risk candidate, or again only when a later
  edit changed that suite's boundary. A no-edit review pass invalidates nothing.
- Separate current-scope failures from pre-existing, flaky, skipped, unavailable, or unrelated
  failures. State the exact limitation; do not expand the task to repair the baseline.
- Keep tool output out of context: tail or grep a log, run a suite with a failures-only
  reporter, read a fragment rather than a file, and never paste a diff twice.
- Work that outlasts a foreground call — a Codex review, a broad suite, a server — is
  launched into the background and the turn ends; its completion notification resumes the
  work. Never poll a background task (`sleep`/`tail` loops, `Monitor`, `TaskOutput`): the
  Stop hook lets a turn end while this session's own task is in flight, bounded to eight
  such stops per candidate and two hours per task, and a task that was stopped or reported
  `failed` is recorded as failed lane activity.

## 5. Bounded simplify

Simplification applies to lasting artifacts only and never to operational work. It is
required for HIGH and optional otherwise (use it for a concrete readability, reuse,
control-flow, type/error, resource, or efficiency concern).

- Follow the `simplify` skill: a local pass for small work; for a non-trivial scope, or
  whenever evidence is required, exactly one foreground `simplify-reviewer` lane
  (`run_in_background: false`) covering reuse, quality and efficiency in one report. Apply only
  accepted behavior-preserving findings and rerun affected checks.
- Maximum two runs of the lane per candidate: the second only as a delta confirmation after
  accepted edits on broad or high-risk work. A lane result that already exists for this
  candidate is the completed pass; never re-run it for bookkeeping, and never after review
  approval. A candidate ends with its receipt: edits made after a closed cycle — remediation
  after a rebase, a follow-up on the same branch — are a new candidate, and a lane result from
  the closed one does not carry over; run the lane once on the new delta.

## 6. Finite independent review

For HIGH risk on a lasting artifact, or when the user explicitly requests it, run exactly one
review lane per round, chosen in this order:

1. **Codex** through `/adversarial-review`, launched into the background
   (`run_in_background: true`) and judged at its completion notification from the rollout log
   Codex wrote in between, the packet opening on the contents of `agents/adversarial-reviewer.md`. Cross-engine
   judgment is worth more than a second opinion from the model that wrote the code. Run
   `python ~/.claude/hooks/codex_lane.py check` first: a recorded outage (usage limit, model
   at capacity) means the lane is skipped for this round, not retried.
2. **The native `adversarial-reviewer`** through `/adversarial-review-internal`, with
   `run_in_background: false` set explicitly, when Codex is unavailable, reports an outage,
   fails before a verdict on its one allowed resume, or the user declines it. Say which engine
   reviewed and why when it was not Codex.

Both lanes satisfy the gate; neither adds an obligation to run the other. Every verdict must be
observable — the native lane as a foreground result, the Codex lane as the rollout log bound at
its notification — never as a summary you wrote. One ledger and one round
budget span the lanes: switching engines continues the review, never restarts it. Add at most one specialist
only for a named non-overlapping risk; the lane that owns the verdict keeps it.

Obtain the verdict as the last step. Editing a lasting artifact after an approval invalidates
it and costs another round (a delta round on the interdiff, not a new round 1 of the whole
scope); reruns of a throwaway script or a maintenance command do not, and neither does an edit
reverted byte for byte — the gate measures content, not edit events. A clean merge or rebase
of an approved candidate needs nothing; one resolved by hand is a delta candidate whose
resolution diff gets the delta lane and a delta round.

```
MAX_REVIEW_ROUNDS = 3
BLOCKING_THRESHOLD = HIGH
```

- `CRITICAL`, `HIGH`, and explicit acceptance-criterion violations block. `MEDIUM`, `LOW`,
  `NIT`, and `FYI` are non-blocking unless the user explicitly lowers the threshold.
- Keep one stable finding ledger. Round 1 reviews the candidate. Later rounds inspect only the
  remediation delta, open blockers, affected interfaces, and direct regressions.
- A new round requires a changed candidate or new material evidence. Identical code and
  evidence cannot trigger another review, and an APPROVED candidate is not reviewed again to
  "confirm" it after a merge, a rebase or publication: the August 2026 transcripts held 62 such
  confirmation rounds, all of which approved again.
- If remediation changes code, rerun affected checks; spend the one remaining simplify
  confirmation only for a concrete complexity concern.
- `VERDICT: APPROVED` ends the gate immediately. There is no post-approval review or simplify
  pass. Round 3 with open blockers ends in `VERDICT: ESCALATE`; never force approval and never
  emit a fourth ordinary round.

`ESCALATE` is terminal only for the review loop; the parent continues immediately with the
autonomous closure below. If a required reviewer is unavailable after the bounded wait policy
in `subagent-delegation`, record `REVIEW_UNAVAILABLE` and enter the same closure.

## 7. Autonomous closure after escalation

The parent owns this phase. Do not ask the user to choose between ordinary technical options
merely because the review budget ended.

**Freeze and gather evidence once.** Freeze the candidate, acceptance criteria, check results,
and finding ledger. One focused evidence pass for the open blockers: current code, tests,
runtime behavior and repository contracts first; applicable ADRs or runbooks when intent is
disputed; Context7 only for a dependency whose documented behavior could decide a blocker
(`NOT_APPLICABLE` otherwise); current primary web sources only for an engineering, security or
release practice question. Stop when evidence decides the blocker and reuse it across passes.
External best practice cannot override explicit acceptance criteria or observed behavior.

**Select exactly one action.**

- `REMEDIATE` — root cause known; the fix stays in the current owning boundary, changes no
  architecture/public contract/security or data model, and affected checks cover it.
- `REDESIGN` — the blocker comes from the wrong owner, contract, state model, or
  architecture; the same cause survived a localized fix; or a safe patch would require
  suppression, broad fallback, or accumulating special cases. Replace the faulty owning
  approach, not the whole task.
- `VALIDATION_PENDING` — all acceptance criteria appear met, evidence is bound to the exact
  candidate, no substantiated blocker remains, rollback/publication prerequisites are
  documented. Pre-validation only, never terminal.
- `DRAFT_BLOCKED` — completion needs external access, a secret, unavailable infrastructure, a
  non-technical product choice, explicit acceptance of security/data-loss/irreversible risk,
  an unavailable required simplify/review lane, or no concrete recovery within the budget.

**At most two closure passes** (`MAX_CLOSURE_PASSES = 2`). A pass is one selected action, any
resulting edit and affected checks, then exactly one foreground `CLOSURE_VALIDATION` packet to
the same reviewer with the frozen ledger, new evidence, recovery delta, exact candidate and
affected checks — it checks only prior blockers, affected interfaces and direct regressions,
never the whole scope. `CLOSURE_VALIDATION: READY` → terminal `PR_READY`, publish without
validating again. `BLOCKED` at pass 1 must name a new concrete `REMEDIATE`/`REDESIGN` or go
straight to `DRAFT_BLOCKED`; `BLOCKED` at pass 2, or `REVIEW_UNAVAILABLE` blocking required
evidence, is terminal `DRAFT_BLOCKED`. Do not restart broad review or repeat an approach.

Ask the user only for choices reserved by the authority rules: product intent, acceptance of
security/data-loss/irreversible risk, protected access or secrets, or an otherwise unauthorized
production/external action. Technical uncertainty selects `REMEDIATE`, `REDESIGN`, or
`DRAFT_BLOCKED`; it is never a reason to wait.

## 8. Publish the closure result

The global instructions grant publication authority after `ESCALATE` or
`REVIEW_UNAVAILABLE`, subject to repository rules and dirty-worktree safety.

- `PR_READY`: stage only the owned scope, commit, push a non-protected owned branch, open a
  ready PR with acceptance evidence, exact checks, blocker dispositions, and residual risk.
- `DRAFT_BLOCKED`: preserve completed work and, when the branch can be published safely, open
  a draft PR naming the exact blocker, evidence, failed/unavailable check, and required
  decision. A draft is a handoff, not approval to merge.
- Never merge, deploy, force-push, bypass branch protection, weaken required checks, or
  include unrelated user changes. If safe publication is impossible, leave an owned branch and
  a complete handoff, report the external blocker, and finish without waiting.

## 9. Terminal receipt for the finite Stop hook

End implementation work with exactly one factual receipt as the final non-empty line:

```
[gate] verified: <LOW|STANDARD|HIGH>; <candidate and decisive checks/review>
[gate] operational: <what was established before executing>; <verified effect on the system>
[gate] no-change: <why nothing was modified>
[gate] pr-ready: <PR URL, or branch plus exact publication handoff>
[gate] draft-blocked: <draft PR URL, or exact external publication blocker>
[gate] anomaly-reported: <report id>; <the verifiable contradiction>
```

The receipt is one line of evidence — the candidate and the check, verdict or blocker that
decided it — never a retelling of the protocol or a list of what was inspected. Checks that
passed as expected need no prose elsewhere; a failure of any kind is stated under section 4
regardless. `verified` states the risk of a lasting artifact; `operational` carries both
halves; `no-change` closes an inspection-only step with its reason as a clause; edits made and
then reverted close as `verified` at the path floor, stating the worktree was restored.
`pr-ready` and `draft-blocked` follow the autonomous closure only; `anomaly-reported` closes
UNVERIFIED with a report (section 10). Never emit `[gate] escalated`. A closing message without a receipt is blocked by the Stop hook at most
three times per unchanged candidate; the receipt records evidence and never substitutes for
running the work.

## 10. When the hook is wrong

The Stop hook is right by default: a missing or misplaced receipt, a `REVISE`, an edit after the
approval, a lane launched in the wrong mode are your mistakes, not the hook's. An anomaly is a
block that contradicts facts you can verify in the transcript: the required evidence exists in
the required shape (a foreground APPROVED after the last lasting edit, the simplify lane's
result), the hook demands a lane this skill forbids repeating, the same block returns after its
demand was met exactly, the marker names files you never touched, the hook failed or timed out,
or its advice contradicts the breaker or this skill. Not an anomaly: a lane result that
belongs to a candidate already closed by a receipt — the block names the candidate that
opened afterwards, and that one needs its own delta pass.

Order: check the three facts once — the receipt is the last line and well-formed, the last
lasting edit precedes the verdict, the lanes ran in the foreground — and fix what is yours. If
the block still stands, do not re-run lanes, do not poll, do not argue with the hook: file the
report and finish honestly.

```
python "~/.claude/hooks/gate_inbox.py" report --session <session id> --nonce <nonce> \
  --block "<the block reason, verbatim>" \
  --facts "<what the transcript shows, with timestamps or tool ids>" --did "<what you did instead>"
```

Copy the command from the block text: it carries the session id, the transcript path and the
nonce the hook minted for that block, and the receipt is accepted only for a report carrying
that nonce. It prints an id and the message for the gate-ops session: deliver that message at
once with `mcp__ccd_session_mgmt__send_message` to the session id it names (or `SendMessage`
to the name it gives when that tool is absent), then go on with your work — the report is
handled there, not by you, and no reply is needed. End with
`[gate] anomaly-reported: <id>; <the contradiction in one line>`. The hook accepts that
receipt only after it has blocked this candidate, for a report filed after that block and
quoting its reason, and closes the candidate UNVERIFIED with the report attached — once per
candidate, never as `verified`. Overuse shows up in the gate-ops session as a pattern and is
treated as one.

## Incident hotfix order

For an active production incident where a full gate would prolong user-visible harm:
characterize enough of the failure boundary to avoid making it worse; apply the smallest
reversible mitigation; run the narrowest useful smoke and record the deployed revision;
restore service, then finish the proportionate high-risk verification in the same task. Report
emergency mitigation and final reviewed rollout as separate states.
