# Usage optimization — 2026-09-02

What the local transcripts showed, what was changed, and what to compare in the next `/usage`.
Data and scripts: `~/.claude/state/usage-baseline/`. Pre-change copies of every edited file:
`~/.claude/backups/usage-optimization-20260902/`.

## 1. Baselines

### Runtime baseline (from the user's `/usage`, 2026-09-02)

| Metric | Value |
|---|---|
| weekly_all / weekly_scoped | 100% / 54% |
| requests 24h / 7d | 3,253 / 21,819 |
| usage in subagent-heavy sessions | 96% |
| requests with context > 150k | 87% |
| requests during 4+ parallel sessions | 38% |
| skill attribution | adversarial-review 21%, development-verification 9%, chip-handoff 6%, adversarial-review-internal 3%, simplify 3% |

### Historical baseline (local transcripts, deterministic extraction)

| Metric | Value |
|---|---|
| period | 2026-07-18 … 2026-09-02 (47 days) |
| main transcripts / subagent transcripts | 134 / 1,170 (all `claude-desktop`, versions 2.1.217–2.1.255) |
| processed | 134 / 1,170 (100%); 2 main transcripts had no API request |
| API requests | 58,972 (main 44,736; subagent 14,236) |
| context tokens (input + cache) | 20.8 B, of which subagents 6.3% |
| output tokens | 43 M (main 35.6 M, subagents 7.5 M) |
| main model / effort | Opus 5 on 96% of main requests; effort xhigh 40%, high 39%, max 20% |
| main context per request | median 390k, p75 609k, p90 808k; 90.1% of main requests > 150k; 97% of main context tokens sit in requests > 150k |
| first crossing of 150k | median at request 26, after the first user prompt |
| compactions | 50 in 18 sessions, 49 automatic, at a median of 998k tokens, 143 s each; post-compaction context median 115k |
| session shape | median 167 requests, 3.75 h, 3 user prompts; p90 871 requests |
| sessions with subagents | 92 of 134; 95% of all requests happen in them; subagent requests are 24% of requests |
| parallel sessions (5-minute window) | ≥2: 86%, ≥4: 41%, ≥6: 16% of main requests |
| receipts | 1,041: verified 538 (HIGH 450, STANDARD 71, LOW 17), operational 205, no-change 180, pr-ready 65, draft-blocked 53 |
| requests per receipt (code sessions) | median 40, mean 67; agents per receipt median 1.2 |
| Stop-hook gate blocks | 391 in 59 sessions (Aug–Sep), 0.38 per receipt; 20 of 41 code sessions had none; blocks 1/2/3 of the cap: 312/49/30 |
| simplify | 456 waves (219 full trios, 205 single-lens re-runs); 795 lane runs, all Sonnet 5 at effort max, 6–12 requests each, 545 s wall per wave; 56% of waves followed by an edit; 21% by nothing; lens overlap by file:line 6%; efficiency lane empty 39%, reuse 21%, quality 10% |
| adversarial | 321 candidates, 596 delta rounds; 29% of delta rounds had no edit or commit between rounds; 62 "confirmation" reviews after APPROVED, all approved again; native lane: 326 runs on Fable, 12 requests median, 379 s, APPROVED 258 / REVISE 36 / ESCALATE 3 |
| Codex lane | 530 `codex exec` calls, 222 with the review marker; 132 of those returned no verdict (median 15 s); 59 candidates ran Codex and then the native lane; stderr: usage limit ×9, model at capacity ×6 in the last 56 runs |
| built-ins | Explore 15 runs, all Opus (inherited), 29 requests, 3.6 M context tokens and 28k chars of output each; general-purpose 27 runs, 5 M context tokens each, p90 50 min; Plan 2 runs |
| chips | 60 `spawn_task` calls in 25 sessions; the 6% "chip-handoff" attribution came from one session (991 requests attributed after a single skill invocation) |
| always-on context | session bootstrap median 87k tokens (first request); subagent bootstrap 25–33k; skill listing 23.8 KB, agent listing 8.6 KB, CLAUDE.md 11.1 KB, hook injections ~9 KB per session |

Attribution note: `/usage` attributes every later request to the most recently invoked skill.
"adversarial-review 21%" is therefore the share of full-context main turns spent *after*
invoking the review command (packet, remediation, closure), not the reviewer's own cost; the
reviewer lanes themselves are 6.3% of context tokens.

## 2. Root causes, ranked by cost

1. **Context size, not subagents.** 90% of main requests carried 150k–1M tokens because the
   1M window compacted only at ~1M. This is 93% of all context tokens. Subagents are 6%.
2. **Mandatory simplify trio on every HIGH and three-file STANDARD candidate**: three Sonnet
   contexts at effort max, plus single-lens re-runs forced by the Stop hook's "three named
   lenses" rule (84 of 391 blocks), plus the parent's orchestration turns at 400k+.
3. **Codex lane failing 60% of the time** on quota/capacity, each failure costing several
   parent turns before the native lane ran — both engines on 20% of candidates.
4. **Review rounds without a changed candidate**: 29% of delta rounds; 62 post-approval
   confirmation reviews.
5. **Stop-hook blocks after the fact**: 114 "receipt missing" blocks, each one more
   full-context turn, on candidates opened long before.
6. **Built-in Explore/general-purpose inheriting Opus/xhigh**: rare (42 runs) but 3–5 M
   context tokens each.
7. **Always-on overhead**: ~80k tokens per session and 25–33k per lane; the codebase-memory
   PreToolUse gate also wasted one blocked tool call per session.
8. **Parallel sessions (41% at ≥4)** are independent user tasks (goals, scheduled tasks,
   chips), each carrying its own 400k context. Configuration cannot serialize them; making
   each cheaper is the lever.

## 3. Changes

| Area | Before | After |
|---|---|---|
| `settings.json` | compaction at the 1M limit | `autoCompactWindow: "300k"` (simulated on the real request sequences with the measured 115k floor: −54% context tokens, median one compaction per session, 300 vs 50 compactions over the period) |
| `settings.json` | skill listing 23.8 KB | `skillListingMaxDescChars: 320`; chip-handoff description shortened |
| `settings.json` | `code-simplifier` plugin on | off (unused duplicate lane) |
| `settings.json` hooks | codebase-memory `cbm-session-reminder` ×4 and `cbm-code-discovery-gate` | removed (the gate blocked the first Read/Grep/Glob of every session; the reminder duplicated the skills) |
| `settings.json` hooks | — | `UserPromptSubmit` → `code_work_gate_prompt.py`: one line naming the open candidate and its receipt; silent otherwise |
| `code_work_gate_stop.py` | HIGH and 3-file STANDARD require the three named lenses | HIGH requires one `simplify-reviewer` result (legacy trio still accepted); STANDARD requires none; per-lane 2-pass cap kept |
| `code_work_gate_mark.py` | silent | announces a candidate once when it opens and once when its floor rises; records a Codex outage from a finished `codex exec` call |
| `codex_lane.py` | — | circuit breaker: `check` prints `CODEX_LANE: available` or the CLI-named retry time; state in `state/codex-lane.json` |
| `agents/simplify-reviewer.md` | three lens agents, Sonnet, effort max, no turn cap | one lane, Sonnet, effort medium, `maxTurns: 40`; three lenses in one report |
| `agents/simplify-*-reviewer.md` | present | removed (backup kept) |
| `agents/adversarial-reviewer.md` | Fable/high, no turn cap | Fable/high, `maxTurns: 60` (p97 of observed runs is 40, max 71) |
| `agents/Explore.md` | built-in inherits Opus | custom override: Sonnet, medium, `maxTurns: 50`, output capped at ~800 words |
| `skills/development-verification` | 18.0 KB | 14.8 KB; simplify §5 and review §6 rewritten; no other semantic change |
| `skills/simplify` | three-lane wave | local pass or one lane |
| `skills/subagent-delegation` | trio exception; model guidance generic | routing by lane; `general-purpose` gets `model: "sonnet"` unless judgment is needed |
| `commands/adversarial-review.md` | Codex first, resume on failure, effort xhigh | breaker check first; no resume on quota/capacity; effort high |
| `CLAUDE.md` | 11.1 KB | 7.0 KB (hook contract and trio exception moved to the skill; NotebookLM block condensed) |
| `settings.json` env | `ANTHROPIC_DEFAULT_FABLE_MODEL: claude-fable-5` | `claude-fable-5-1` (the Opus 5 and Sonnet 5 pins were already current); the project's `CLAUDE_CODE_SUBAGENT_MODEL` raised from `claude-sonnet-4-6` to `claude-sonnet-5` |
| `continuity_session_start.py` | full checkpoint body injected on every fresh start | GOAL/STATUS/NEXT ACTION only on `startup`/`fork`; full body on `resume`/`compact` |
| `reference/model-routing.md`, `reference/environment.md` | — | updated to the new lanes and files |

Deliberately unchanged: the HIGH path floor and its regexes; the three-block cap; the
freshness rule (an approval must be newer than the last edit to a lasting artifact, so a
rebase or merge still costs a delta round); Codex as the default engine; the marketing and
geo agent profiles (unused in the period, no evidence either way); the NotebookLM hooks (their
latency is 1–1.5 s per event, their context 4 KB per session); the project's own `.claude`
directory, except that the stale `CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6` in its
`settings.local.json` was raised to `claude-sonnet-5` on request (on a runtime before 2.1.251
that variable still overrides the reviewer's model, so it stays a caveat);
`GITLAB_TOKEN` in `settings.json` env (a secret in a file that has a public example — flagged).
`CLAUDE_CODE_SUBAGENT_MODEL` was not set globally for the same version reason: the
`minimumVersion` floor (2.1.246) is below 2.1.251, where the override semantics changed.

## 4. Expected effect per representative workload

Derived from the measured lane costs and the new contract, not from new runs.

| Workload | Before (lanes, engines) | After |
|---|---|---|
| LOW fix | 0 lanes | 0 lanes (unchanged) |
| STANDARD, 1–2 files | 0 lanes; simplify optional | unchanged |
| STANDARD, 3+ files | 3 Sonnet/max lanes required, ~9 min wall | 0 required; local pass or one Sonnet/medium lane by judgment |
| HIGH change | 3 Sonnet/max lanes + 1 Fable review; second trio in 45% of candidates; Codex attempt fails 60% then native | 1 Sonnet/medium lane + 1 Fable review; Codex skipped on a recorded outage |
| review remediation round | delta round, sometimes re-run trio | delta round only; one lane confirmation at most |
| post-approval merge/rebase | 62 confirmation reviews observed | forbidden by the skill text; the hook still demands a fresh approval only when a lasting artifact changed |
| repository exploration | Explore on Opus, 3.6 M tokens | Explore on Sonnet/medium, capped at 50 turns and ~800 words |
| long autonomous session | context 390k median, compaction at 1M | compaction at 300k with the checkpoint restored; median context ~200k (simulated) |
| new task in a directory with an old checkpoint | full old checkpoint injected | three-section lead only |
| Stop happy path | 0.38 blocks per receipt | candidate and receipt announced in advance; blocks for "receipt missing" and "lenses missing" expected to fall |

## 5. Verification

- `hooks/test_gate.py`: 747 → 813 assertions PASS (simplify contract, prompt reminder, marker
  announcements, Codex breaker, shell-path silence, marker-reading equivalence between the
  reminders and the Stop hook).
- `hooks/continuity_selftest.py`: 70/70 (compact startup lead added).
- `hooks/chip_handoff_test.py`, `hygiene_hooks_test.py`, `comment_density_guard_test.py`: pass.
- Review of this change (native `adversarial-reviewer`, one round plus two delta rounds;
  Codex declined for this cycle by the user's single-cycle instruction): APPROVED with one
  medium finding — the breaker's phrase matcher could be tripped by a Codex review that quotes
  the CLI's error text; fixed by anchoring to `^ERROR:` lines, scanning only the 4 KB tail the
  CLI writes last, capping any record at 6 h, and rejecting an impossible minute. The delta
  round found that the documented launch redirects stderr through `${REVIEW_ID}`, which the
  hook sees unexpanded; the breaker now resolves the variable from the command and otherwise
  reads the newest capture written after the launch.
- Live: the marker announcement fired in this session on the first HIGH edit; the initial
  implementation repeated it on every shell call (a variable clash with the shell snapshot),
  fixed and covered by a test.
- The custom `Explore` and `simplify-reviewer` profiles could not be observed in a fresh
  session from this one: the winget CLI (2.1.236) is not authenticated (`401`), every real
  session runs in the desktop app, and the runtime reads the agent directory once at session
  start (one `agent_listing_delta` per session in the transcripts). The override relies on the
  documented rule "a custom subagent named Explore overrides the built-in and respects its own
  model"; check `/agents` in the next desktop session. The runtime loads the agent roster once
  per session: the session that created `simplify-reviewer` could not launch it ("Agent type
  not found") and ran its own gate on the still-listed legacy lane instead.

## 6. What to compare in the next `/usage`

| Metric | Baseline | Direction expected |
|---|---|---|
| requests with context > 150k | 87% | down; the floor after compaction is ~115k, so the honest target is a median near 200k rather than "rare > 150k" |
| subagent-heavy share | 96% | roughly flat as a *session* label; subagent requests per HIGH candidate down from ~4–7 lanes to 2 |
| 4+ parallel sessions | 38% | unchanged by configuration; watch requests per session instead |
| requests / 24h, / 7d | 3,253 / 21,819 | down with fewer lanes, fewer blocks, fewer Codex retries |
| adversarial-review attribution | 21% | down (fewer failed Codex launches, no post-approval confirmations) |
| development-verification / simplify / adversarial-review-internal | 9% / 3% / 3% | simplify down; internal roughly flat |
| chip-handoff | 6% | an attribution artifact; expect near 0 |
| Stop-hook blocks per receipt | 0.38 | down |
| delta rounds without a changed candidate | 29% | down |
| Codex launches without a verdict | 60% | down to the residual (real failures only) |

Rerun the extractor to refresh the historical side:
`python ~/.claude/state/usage-baseline/extract.py` then `aggregate.py`, `timeline.py`,
`analyze2.py`, `blocks.py` (they write next to themselves).

## 7. Confidence

- High: the context-size finding, the lane counts, the Stop-block classification, the Codex
  failure modes (all counted from complete transcripts).
- Medium: the simplify "acted on" rate (an edit before the next gate step is a proxy for
  acting on findings); the 29% of delta rounds without a candidate change (rebases and merges
  were not in the edit set, so some of those rounds were legitimate).
- Low: the exact saving from `autoCompactWindow: 300k` (simulation assumes the same work per
  request and a fixed 115k floor) and the quality effect of one simplify lane versus three (no
  outcome data exists for either).

## 8. Rollback

Copy the files back from `~/.claude/backups/usage-optimization-20260902/` (including the
removed `agents/simplify-*-reviewer.md` and the two `cbm-*` hooks), delete
`agents/simplify-reviewer.md`, `agents/Explore.md`, `hooks/codex_lane.py`,
`hooks/code_work_gate_prompt.py`, and remove the three added keys and the prompt hook from
`settings.json`.

## 9. Addendum 2026-09-03: background lanes and false expiry

Trigger: session `08244b4e` (worktree `charon-ownership-auth-model-c625ae`) kept spending
requests after a Codex APPROVED. Replaying its transcript against the hooks found three
independent defects, all fixed in this addendum's change set.

| Finding | Evidence | Fix |
|---|---|---|
| Codex reviews launched with `run_in_background: true`, or moved to the background by the Bash timeout, left only a launch acknowledgement in the transcript; the gate never saw a verdict. | 34 background launches with 0 verdicts across the history, ~13 auto-backgrounded, 14 ack-sized results of 222 marked calls; in `08244b4e` every Codex round but one. | The Stop hook binds a backgrounded marked call at its `<task-notification>` to the verdict one briefed Codex session stated in the rollout log between launch and notification; the output file is not evidence, and two sessions speaking in the window bind nothing. `/adversarial-review` now launches in the background by design. |
| The model polled the running lane instead of ending the turn: `tail`/`sleep`/`Monitor`/`TaskOutput` calls, each a full-context request. | 364 polling calls in `08244b4e`. | The Stop hook lets a turn end while this session's own background task is in flight (own launches only, `MAX_BACKGROUND_WAITS = 8` per candidate, `BACKGROUND_WAIT_LIMIT = 2 h` per task, `TaskStop`/`failed` counted as failed activity). Skills and the command say: end the turn, never poll. |
| The one foreground APPROVED was expired 7 s later by `git status --porcelain \| wc -l && git rev-parse --short HEAD` (an unresolved shell mutation on a durable candidate), which produced block #3 and a needless native round. | Marker `last_durable_ts` 14:03:41 vs verdict 14:03:34; the replay does not reproduce the snapshot failure, so the unresolved branch was taken. | Expiry through an unresolved snapshot now requires a write-capable command, meaning anything not proven read-only: an allowlist per pipeline segment (`ls`, `cat`, `grep`, `wc`, git's reading subcommands…), with any redirect other than a discarded or merged stderr, a substitution, a heredoc or a script block counting as writing. Read-only pipelines keep the candidate open but never expire a verdict. |

Also added: `state/gate-events.jsonl`, an append-only ledger of gate decisions, so the next
question of this kind is answered from one file instead of a transcript replay.
`test_gate.py` grew from 813 to 946 assertions (both acknowledgement shapes bound, pending wait
allowed and capped, stale task blocks, stopped or failed lane → draft-blocked, a required lane
bound in the background, the verdict read from the rollout rather than the output file, a
foreground review that mentions a background task is still a review, a task launched before the
candidate is tracked, ledger line, write-capable tables for Bash and PowerShell, quiet pipeline
keeps the verdict).

Residual for that session: its gate state already holds three blocks for the unchanged
candidate, so its next Stop ends `UNVERIFIED` unless the candidate changes; the fix applies to
new turns and new sessions.

## 10. Addendum 2026-09-03: gate anomaly inbox

Agents in any session can now disagree with a block on the record instead of re-running lanes
or polling: `hooks/gate_inbox.py report` files the block reason, the verifiable facts, what was
done instead, the marker and state, the session's ledger tail and the Stop hook's own view of
the transcript into `state/gate-anomalies.jsonl`; the receipt `[gate] anomaly-reported: <id>;
<fact>` closes the candidate UNVERIFIED, and only after a block, for a report carrying the nonce
that block minted and quoting its reason (development-verification section 10). `gate_inbox.py scan` derives
anomalies from the ledger by fixed rules (exhaustion, waits exhausted, unbound background
review, an approval expired by a durable edit within a minute of being stated, a hook failure — the Stop hook now logs
`hook_error` instead of failing open silently); `digest` runs as a SessionStart hook and shows
the unresolved reports to a session started from `~/.claude`. `test_gate.py`: 946 → 1155.

Three more findings from this session's own ledger, fixed in the same change set: a task
notification absorbed while the turn was running never becomes a user record (only the
`queue-operation` and `attachment` records the harness leaves), so the Stop hook now reads
those too; two Codex reviews running at once from different chats made the rollout binding
ambiguous, so a background launch that fed a packet on stdin is now bound to the session that
was given that packet; and `gh pr edit` run outside a repository expired a verdict, so `gh` is
read-only unless it touches the tree (`pr checkout`, `pr merge`, `repo clone`…).

First report delivered by push (session issue-255, 2026-09-03 17:40): an approval expired by an
edit that was reverted byte for byte. Fixed: the marker fingerprints its lasting paths at every
durable change and the Stop hook judges freshness by content — a verdict covers the candidate
when the bytes it reviewed are the bytes on disk, whatever edits happened in between.

Third pushed report (session Charon, 2026-09-04 14:15): a `glab api` loop run in a worktree got a
HIGH floor from an unattributed change under `~/.claude` that the gate-ops session was making at
that moment. Fixed: an unattributed change under a root the command neither ran in nor names
raises no floor. The gate-ops session's own closing then hit a fourth pattern: a cycle restarted
after the 8-hour idle limit discarded the candidate's earlier review rounds, so the ESCALATE
sequence check failed. Fixed: the idle limit no longer ends a cycle the same branch resumes on a
file it already holds.
