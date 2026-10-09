---
name: subagent-delegation
description: Use Claude Code subagents for bounded independent exploration, implementation, specialist checks, or review when they materially improve the result.
---

<!-- jev-stage:start -->
At diagnostics, context selection or selection among genuinely ambiguous checks/tools, follow the mandatory eligible scenarios in
the `jev-workflow` skill before making that comparison yourself.
Load its MCP tools once per session via ToolSearch `select:mcp__jev-workflow__judge,mcp__jev-workflow__prepare_and_delegate`, or use its
`stage.py` CLI; then apply IDs and inspect disputed sources.
Exact/formal decisions and final validation stay native; preserve required checks,
permission boundaries and secrets. If this role cannot use an allowed interface,
continue natively with intact sources and report the concrete limitation.
<!-- jev-stage:end -->

<!-- jev-search:start -->
Finding code: unless you already have the exact file or symbol, start with
`jev find "<what the code does>" <dir>` rather than a grep over several guessed names (`a\|b\|c`).
To check one property across files without reading them all, use
`jev ask "<yes/no question>" <files> -q`. Open what Jev cites with an explicit range; where `jev`
is not on PATH, use Grep and Glob.
In an exploration or review packet, ask the subagent to locate code with `jev find` first:
Explore and the reviewers with a shell run it. The simplify lenses have no shell: run it yourself
and put the files in their packet.
<!-- jev-search:end -->







# Subagent Delegation

Start with the primary agent. Delegate only when a bounded lane is independent enough to save
time, isolate verbose context, supply distinct expertise, or provide proportionate independent
judgment. A second reasoning context is the expensive resource; a parallel deterministic tool
call is not.

## Good lanes

- disjoint repository exploration with a named output (`Explore`, or a project explorer);
- a non-overlapping implementation scope with one file owner;
- a focused specialist check;
- one read-only adversarial review for high-risk work;
- the simplify lanes `simplify` requires: one `simplify-reviewer` for STANDARD, the three lens
  agents for HIGH, their `-xhigh` profiles for XHIGH — one pass, not a panel;
- independent QA whose evidence can be checked by the parent;
- an in-scope follow-up — a pre-existing failure or a fix the task turns up that does not belong
  in the candidate. Scope, isolation and the chip for out-of-scope work are defined in
  `development-verification` §4; neither kind is dropped as "not mine".

A subagent never opens subagents: `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` refuses it. A lane
that turns up more work reports it, and the parent routes it. At most three subagents run at
once (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=3`). The HIGH simplify lenses take all three slots,
so they start when no other lane is running; a launch refused at the cap is not retried at once
but made again after a running lane's completion notification.

Keep small, linear, tightly coupled, destructive, sensitive, and ordinary sequential work local;
an in-scope follow-up that must stay a separate change is the exception and goes to a subagent
whatever its size (`development-verification` §4).
Do not form a reviewer panel, duplicate a scope, or delegate merely because agents are
available. One optional lane is enough; add another only for a named, non-overlapping result.

## Specialist lanes

When a lane above needs stack expertise, use the `claude-code-workflows` plugin agent for it
(`subagent_type` as listed; the plugin's skills load by themselves when relevant):

| Work | Agent |
|---|---|
| a failure independent of the candidate, e.g. an in-scope follow-up | `debugging-toolkit:debugging-toolkit-debugger` |
| developer tooling, project setup | `debugging-toolkit:debugging-toolkit-dx-optimizer` |
| writing or extending a test suite | `full-stack-orchestration:full-stack-orchestration-test-automator` |
| profiling, load, observability | `full-stack-orchestration:full-stack-orchestration-performance-engineer` |
| the one security specialist `development-verification` allows | `full-stack-orchestration:full-stack-orchestration-security-auditor` |
| CI/CD, Docker, Nomad, deployment pipelines | `full-stack-orchestration:full-stack-orchestration-deployment-engineer` |
| cloud infrastructure, IaC | `deployment-validation:deployment-validation-cloud-architect` |
| Kubernetes, Helm | `kubernetes-operations:kubernetes-operations-kubernetes-architect` |
| monorepo, build systems | `developer-essentials:monorepo-architect` |
| schema changes, migrations, DB operations | `database-migrations:database-admin` |
| query and index tuning | `database-migrations:database-migrations-database-optimizer` |
| Python, FastAPI, Django | `python-development:python-pro`, `python-development:python-development-fastapi-pro`, `python-development:python-development-django-pro` |
| JavaScript, TypeScript | `javascript-typescript:javascript-pro`, `javascript-typescript:typescript-pro` |
| Rust, Go, C, C++ | `systems-programming:rust-pro`, `systems-programming:golang-pro`, `systems-programming:c-pro`, `systems-programming:cpp-pro` |
| React components, mobile apps | `frontend-mobile-development:frontend-mobile-development-frontend-developer`, `frontend-mobile-development:frontend-mobile-development-mobile-developer` |

- Pass `model` on every plugin lane: `sonnet` for bounded implementation, tests and local fixes;
  `opus` for architecture, a security judgment or a hard root cause; `haiku` only for mechanical
  work. The profiles' own models do not follow this (`model-routing.md`).
- Whatever a plugin agent's description says, diagnosing the task's own failure stays with the
  parent (`root-cause-engineering`), and so do architecture, backend and API design, integration
  and the final decision.
- The plugins' commands run only when the user asks for them. `/full-stack-feature` stops at
  checkpoints for approval by design, so it is never the default path for feature work.
- The plugins' commands name their agents without the plugin prefix (e.g.
  `full-stack-orchestration-test-automator`), which Claude Code rejects with "Agent type … not
  found" (verified on 2.1.286); launch the prefixed name from the table instead.

## Task packet

Give each lane only current state: goal and concrete output; authoritative requirements and
acceptance criteria; relevant files/evidence; explicit read/write scope and exclusions; the
verification expected. Do not pass the conversation. The parent owns decomposition,
requirements, integration, verification, and the final answer.

Before a research or investigation lane, run `nlm-memory recall` on its question and put what
memory holds in the packet, so the lane starts from it instead of rediscovering it.

A lane that runs on `fable` also needs what Fable 5.1 does not do unprompted, unless its agent
file already says it: that it runs autonomously and states an assumption instead of asking;
that it requests every independent read or search in one response; and the exact shape of its
final message, the only thing the parent sees. The reasons and the rest of the list are in
`~/.claude/reference/model-routing.md` under Claude Fable 5.1.

## Ownership and review

- One writer per file or tightly coupled scope.
- Review lanes are strictly read-only, do not delegate further, and do not start a code-work
  gate for their inspection.
- Check every returned claim against the diff, repository, command output, or runtime evidence.
  A subagent conclusion is not self-validating.
- Reuse the same lane for follow-up and send only the delta, open findings, and new evidence.
  Never create a replacement just because the first lane is slow.

## Model and effort

The agent profile's model and effort are the default; the profiles in `~/.claude/agents` are
routed by lane already (`Explore` and the simplify lanes on Sonnet/medium,
`adversarial-reviewer` on Fable/high). Override only for a clear reason:

- a deterministic lookup or a named command: `haiku`;
- `general-purpose` and any built-in lane without a profile: pass `model: "sonnet"` unless
  the task is a genuine root-cause, architecture or security judgment — otherwise it inherits
  the main session's model and effort, which is the most expensive combination available;
- architecture, security, root cause, adversarial review: a strong reasoning tier.

Prefer a bounded lane: state the expected size of the answer and stop conditions in the packet.
If an explicit model is unavailable, retry once with the closest supported tier or keep the
work in the parent; do not walk a fallback ladder. Record which tier ran when it matters to
the evidence.

## Bounded wait

A lane still running when no useful local work remains is not waited on by polling: end the
turn, and its completion notification resumes the work with the result. `sleep`/`tail` loops,
`Monitor` and `TaskOutput` are each a full-context request that buys nothing; the Stop hook
lets a turn end while this session's own background task is in flight (at most eight such
stops per candidate, two hours per task). Send one focused follow-up only when the lane asked
a question.

If the lane died — a `failed` notification, a stop, or no notification within the wait limit —
do not recreate it or wait indefinitely:

- for optional work, skip it and state the limitation;
- for a required high-risk review, record REVIEW_UNAVAILABLE and enter the autonomous closure
  in development-verification.
