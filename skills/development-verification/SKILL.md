---
name: development-verification
description: Final checks and finite risk-based review before completing implementation work, including autonomous closure after escalation.
---

# Development Verification

Establish the completion bar with the smallest amount of checking and independent judgment
that the risk requires. This skill is the only code-work gate. Do not wrap it in another review
loop.

## 1. Classify the work

Decide what the work produced before deciding how much verification it earns.

- **PERSISTENT** — a lasting artifact: repository source, configuration, infrastructure,
  schema, documentation, or executable agent instructions. Later runs read and re-execute it,
  so a defect keeps costing, and readability and reuse are worth paying for. Sections 3–9 apply.
- **OPERATIONAL** — an effect on a live system: commands against a device, server, database,
  cloud account or account state, together with the throwaway scripts and scratchpad files
  written to carry them out. The cost lands once, at execution. Section 2 applies.

One changed lasting artifact makes the candidate persistent. Mixed work is graded on its
persistent half; the operational half is verified by its observed effect, not by review. Work
that changed nothing — inspection, diagnosis, a report — closes with `[gate] no-change:` and
runs no cycle at all.

The decisive difference is *when* judgment still changes the outcome. For persistent work the
last responsible moment is before publication, so checks and review come at the end. For
operational work it is before the command runs: once the disk is wiped, the router has cut over
or the rows are gone, reviewing the script that did it cannot change anything and only spends
effort on code nobody will read again.

## 2. Operational track

All of the judgment goes before execution; none of it goes into polishing the script afterwards.

Before running anything that changes a live system:

1. Confirm the exact target from the system itself, not from the earlier plan or from memory —
   the disk number, host, interface, database, table, account, or environment.
2. Establish reversibility: a backup, an export, a rollback path, or an explicit statement that
   the action cannot be undone and what the blast radius is.
3. Run the read-only precheck or dry run first when one exists, and compare its output against
   what the change assumes.
4. If the action is irreversible with a wide blast radius, spend independent judgment here —
   one adversarial review of the plan, or a question to the user for an authority the rules
   reserve. Never after the fact.

After running it:

5. Verify the effect against the system itself: the state that was supposed to change did, the
   state that was supposed to survive did, and the rollback path is still available.
6. Separate what was verified from what is merely expected, and say which is which.

Do not run simplify, a code-quality lens, or an adversarial code review on a throwaway script
that has already executed. It reviews an outcome that cannot change, on code no future run will
read. If the same script is about to be promoted into a repository, it stops being throwaway and
enters the persistent track as an ordinary candidate.

Close the work with `[gate] operational: <what was established before executing>; <verified
effect>`, or with `[gate] no-change: <reason>` when nothing was modified. Both require this
skill to have been invoked; invoking it before execution, where it belongs, satisfies that.

## 3. Classify risk

- **LOW** — docs, comments, formatting, tests-only changes, generated output with no semantic
  effect, or a local reversible configuration edit with no runtime/public-contract change.
  Run the relevant deterministic check. No independent review is required.
- **STANDARD** — a bounded logic or user-visible change with limited blast radius. Run affected
  tests and static checks. Add simplification or independent review only for a concrete
  complexity, uncertainty, or integration risk.
- **HIGH** — security/auth/permissions, data/schema/migrations, concurrency/distributed state,
  public contracts, production/release, irreversible effects, or a broad cross-component diff.
  Run affected checks, one broad final check when warranted, and one independent adversarial
  review.

Judge risk by blast radius, reversibility, data sensitivity, observability, and rollback—not
line count or a sensitive-looking filename.

The Stop hook enforces a conservative lower bound on lasting artifacts so ambiguity cannot
silently downgrade work:

- tests/specs/fixtures only may be LOW;
- other source, executable agent instruction, configuration, or infrastructure edits are at
  least STANDARD;
- non-test paths containing auth, permissions/RBAC, security/crypto/secrets/tokens,
  migrations/schema, billing/payment/checkout, deploy/release, or production are at least HIGH,
  as are files whose purpose is to hold a secret (`.env*`, private keys, keystores);
- agent configuration that executes or grants authority — hooks, agent, command and skill
  definitions, settings and MCP wiring, `CLAUDE.md`, `AGENTS.md` — is at least HIGH because it
  changes what the agent may do in every later session. Prose that merely lives beside it, such
  as project rules, decisions and runbooks, is ordinary documentation at STANDARD.

Throwaway artifacts and unresolved shell mutations set no risk floor: they are operational work
under section 2, bounded by their own pre-execution check instead.

Raise risk above this lower bound when behavior or blast radius requires it. Never lower it.

## 4. Verify the candidate

- Bind evidence to the exact candidate diff/revision and the relevant configuration,
  dependency, environment, and artifact inputs.
- Run narrow checks first. After a remediation, rerun only checks whose covered code or inputs
  changed.
- Run a broad suite once on the final high-risk candidate, or again only when a later edit
  changed that suite's boundary. A no-edit review pass invalidates nothing.
- Separate current-scope failures from pre-existing, flaky, skipped, unavailable, or unrelated
  failures. State the exact limitation; do not expand the task to repair the baseline.

## 5. Bounded simplify

Simplification applies to lasting artifacts only. It is optional for LOW and a STANDARD
candidate touching fewer than three gated files, and required for HIGH and for STANDARD
candidates touching three or more gated files. It never applies to operational work.
Also use it whenever the diff has a concrete readability, reuse, control-flow, type/error,
resource, or efficiency concern.

- Invoke the `simplify` skill once. For a non-trivial diff, preserve its three parallel
  read-only reuse/quality/efficiency lenses; their combined output is one composite simplify
  pass, not three independent gates. Tiny or narrowly bounded work stays local.
- If the pass makes accepted behavior-preserving edits, one confirmation pass may revisit only
  the affected lenses for broad or high-risk work. Maximum: two runs of any one lens per
  candidate, with no exception.
- The evidence is the lens results themselves, in whatever order they ran. A completed pass is
  never re-run to satisfy bookkeeping.
- Rerun affected checks after edits. Do not run simplify again after review approval as a
  ritual.

## 6. Finite independent review

For HIGH risk on a lasting artifact, or when the user explicitly requests it, run exactly one
review lane and pick it in this order:

1. **Codex** through `/adversarial-review`, in the foreground, with the packet opening on the
   contents of `agents/adversarial-reviewer.md` and the review returned verbatim. Cross-engine
   judgment is worth more than a second opinion from the same model that wrote the code, so this
   is the default. Both lanes therefore review under the same role definition: one delivered by
   the harness, one carried in the packet.
2. **The native `adversarial-reviewer`** through `/adversarial-review-internal`, with
   `run_in_background: false` set explicitly, when Codex is unavailable, fails before a verdict
   on the one allowed resume, or the user declines it. Say which engine reviewed and why when it
   was not Codex.

Both lanes satisfy the gate; neither adds an obligation to run the other. Every verdict-bearing
call must return as an observable foreground result — an Agent call with the mode omitted
defaults to background in newer Claude Code versions, and a detached shell run leaves only a
launch acknowledgement. One ledger and one round budget span the lanes: switching engines
continues the review, never restarts it. Add at most one specialist only for a named
non-overlapping risk; the lane that owns the verdict keeps it.

Operational work does not enter this loop. When such an action deserves independent judgment,
it is spent once on the plan before execution, as section 2 requires.

Obtain the verdict as the last step. Editing a lasting artifact after an approval invalidates
it and costs another round; reruns of a throwaway script or a maintenance command do not.

Set:

```
MAX_REVIEW_ROUNDS = 3
BLOCKING_THRESHOLD = HIGH
```

- `CRITICAL`, `HIGH`, and explicit acceptance-criterion violations block. `MEDIUM`, `LOW`,
  `NIT`, and `FYI` are non-blocking unless the user explicitly lowers the threshold.
- Keep one stable finding ledger. Round 1 reviews the candidate. Later rounds inspect only the
  remediation delta, open blockers, affected interfaces, and direct regressions—not the entire
  scope again.
- A new round requires either a changed candidate or new material evidence. Identical code and
  evidence cannot trigger another review.
- If remediation changes code, rerun affected checks. Use the one allowed affected-lens
  confirmation only for a concrete complexity concern and only while the absolute two-invocation
  cap remains; never open a third simplify pass.
- `VERDICT: APPROVED` ends the gate immediately. There is no post-approval review or simplify
  pass.
- Round 3 with open blockers ends in `VERDICT: ESCALATE`. Never force approval and never emit a
  fourth ordinary review round.

`ESCALATE` is terminal only for the review loop. The parent continues immediately with the
autonomous closure below. If a required reviewer is unavailable after the bounded wait policy
in `subagent-delegation`, record `REVIEW_UNAVAILABLE` and enter the same closure; do not wait
indefinitely or claim review success.

## 7. Autonomous closure after escalation

The parent owns this phase. Do not ask the user to choose between ordinary technical options
merely because the review budget ended.

### Freeze and gather evidence once

Freeze the candidate, acceptance criteria, check results, and finding ledger. Perform one
focused evidence pass for the open blockers:

1. Current code, tests, runtime behavior, and repository contracts are the source of truth.
2. Read applicable project ADRs or runbooks when intent or an accepted constraint is disputed.
3. Query Context7 only for a relevant library, framework, API, or CLI whose documented behavior
   could decide a blocker. Record `NOT_APPLICABLE` when no such dependency exists.
4. Consult current primary web sources when the unresolved question is an engineering,
   security, or release practice rather than project behavior.

Stop when evidence decides the blocker. Reuse it across closure passes. Do not repeat generic
research unless a dependency, contract, or material assumption changed. External best practice
cannot override explicit acceptance criteria or observed repository/runtime behavior.

### Select exactly one action

- `REMEDIATE` — the root cause is known; the fix stays in the current owning boundary, does not
  change architecture/public contracts/security or data models, and can be covered by affected
  checks.
- `REDESIGN` — the blocker comes from the wrong owner, contract, state model, or architecture;
  the same cause survived a localized fix; or a safe patch would require suppression, broad
  fallback, or accumulating special cases.
- `VALIDATION_PENDING` — the parent believes all acceptance criteria are met, evidence is bound
  to the exact candidate, no substantiated blocker remains, and rollback/publication
  prerequisites are documented. This is a pre-validation state, never a terminal result.
- `DRAFT_BLOCKED` — completion needs external access, a secret, unavailable infrastructure, a
  genuinely non-technical product choice, explicit acceptance of security/data-loss/
  irreversible risk, an unavailable required simplify/review evidence lane, or no concrete
  recovery remains within the pass budget.

`REDESIGN` means replace the faulty owning approach, not restart the whole task. Preserve valid
tests, evidence, interfaces, and unrelated working code.

### Run at most two closure passes

Set `MAX_CLOSURE_PASSES = 2` and start `CLOSURE_PASS = 0`.

A closure pass consists of one selected action (`REMEDIATE`, `REDESIGN`, or
`VALIDATION_PENDING`), any resulting edit and affected checks, followed by exactly one
`CLOSURE_VALIDATION` attempt. Increment `CLOSURE_PASS` when that validation starts.

1. For `REMEDIATE` or `REDESIGN`, implement the smallest complete change and rerun affected
   deterministic checks. Run a broad check only if the changed boundary requires it. Then enter
   `VALIDATION_PENDING` in the same pass.
2. In `VALIDATION_PENDING`, send the same reviewer one foreground `CLOSURE_VALIDATION` packet
   containing the frozen ledger, new evidence, recovery delta, exact candidate, and affected
   checks. This is not review round 4: it checks only prior blockers, affected interfaces, and
   regressions directly introduced by the recovery delta.
3. `CLOSURE_VALIDATION: READY` transitions to terminal `PR_READY`. Publish without validating
   `PR_READY` again.
4. `CLOSURE_VALIDATION: BLOCKED` with `CLOSURE_PASS < MAX_CLOSURE_PASSES` must name a new
   concrete `REMEDIATE` or `REDESIGN` action. If none exists, transition directly to terminal
   `DRAFT_BLOCKED`.
5. `CLOSURE_VALIDATION: BLOCKED` at pass 2, or `REVIEW_UNAVAILABLE` that prevents required
   independent evidence, transitions to terminal `DRAFT_BLOCKED`. Do not restart broad review,
   repeat the same approach, or open another gate.

Ask the user only for choices reserved by the authority rules: changing product intent,
accepting security/data-loss/irreversible risk, supplying protected access or secrets, or
authorizing an otherwise unauthorized production/external action. Technical uncertainty
selects `REMEDIATE`, `REDESIGN`, or `DRAFT_BLOCKED`; it is not itself a reason to wait.

## 8. Publish the closure result

The global instructions grant publication authority after `ESCALATE` or
`REVIEW_UNAVAILABLE`, subject to repository rules and dirty-worktree safety.

- In terminal `PR_READY`, stage only the owned scope, commit, push a non-protected owned branch,
  and open a normal ready-for-review PR. Include acceptance evidence, exact checks, blocker
  dispositions, and residual risk.
- In terminal `DRAFT_BLOCKED`, preserve completed work and, when the branch can be published
  safely, open a draft PR naming the exact blocker, evidence, failed/unavailable check, and
  required decision. A draft is a handoff artifact, not approval to merge.
- Never merge, deploy, force-push, bypass branch protection, weaken required checks, or include
  unrelated user changes.
- If safe publication itself is impossible, leave an owned branch and a complete PR-ready
  handoff, report the exact external blocker, and finish without waiting.

## 9. Terminal receipt for the finite Stop hook

End implementation work with exactly one factual receipt as the final non-empty line:

```
[gate] verified: <LOW|STANDARD|HIGH>; <candidate and decisive checks/review>
[gate] operational: <what was established before executing>; <verified effect on the system>
[gate] no-change: <why nothing was modified>
[gate] pr-ready: <PR URL, or branch plus exact publication handoff>
[gate] draft-blocked: <draft PR URL, or exact external publication blocker>
```

The receipt is one line and it is evidence, not a report: the candidate, and the check, verdict
or blocker that decided it. It never lists what was inspected, retells how the protocol ran, or
repeats what the answer above it already said. Verification that ran and passed as expected
needs no prose anywhere in the final message; the receipt is where it is recorded. A failure
is never "expected" for this purpose: current-scope, pre-existing, flaky or skipped, it is
stated under section 4 regardless.

The receipt must match what the work produced. `verified` is for a lasting artifact and states
its risk. `operational` is for an effect on a live system and states both halves: what was
established before the command ran and what was observed afterwards. `no-change` closes a step
that touched no lasting artifact and changed no system state — inspection, diagnosis, a report —
and gives the reason as a clause, not a survey of everything that was read;
never dress such a step as a graded code candidate. Edits that were made and then reverted stay
on the persistent track and close as `verified` at the path-based risk floor, stating that the
worktree was restored. Use `pr-ready` or `draft-blocked` only after the autonomous closure. Never emit `[gate] escalated`; escalation is an internal transition, not
a terminal task state. The receipt records evidence—it does not substitute for running the work.

## Incident hotfix order

For an active production incident where a full gate would prolong user-visible harm:

1. Characterize enough of the failure boundary to avoid making it worse.
2. Apply the smallest reversible mitigation.
3. Run the narrowest useful smoke and record the deployed artifact/revision.
4. Restore service, then finish the proportionate high-risk verification in the same task.

Report emergency mitigation and final reviewed rollout as separate states.
