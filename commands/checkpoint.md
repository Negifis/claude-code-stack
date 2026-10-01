---
description: "Show, write, or refresh the task checkpoint for this directory (survives compaction and restarts)."
argument-hint: "[show|write|project]"
allowed-tools: "Bash(python3:*), Read, Write, Edit"
---

# Checkpoint

<!-- jev-stage:start -->
When restoring a checkpoint, retain the mandatory Jev intermediate-step policy and its tool/CLI availability, follow the mandatory eligible scenarios in
the `jev-workflow` skill before making that comparison yourself.
Load its MCP tools once per session via ToolSearch `select:mcp__jev-workflow__judge,mcp__jev-workflow__prepare_and_delegate`, or use its
`stage.py` CLI; then apply IDs and inspect disputed sources.
Exact/formal decisions and final validation stay native; preserve required checks,
permission boundaries and confidential data. If this role cannot use an allowed interface,
continue natively with intact sources and report the concrete limitation.
<!-- jev-stage:end -->







Argument: `$ARGUMENTS` (empty means `write`).

Resolve the path first:

```bash
python3 ~/.claude/hooks/continuity_checkpoint.py path
```

## show

Print the current checkpoint and say in one line whether it still matches the working tree:

```bash
python3 ~/.claude/hooks/continuity_checkpoint.py show
```

## write (default)

Write the canonical state of the current task to that path, replacing whatever is there. Use
the `canonical-state` skill's `checkpoint.md` format:

GOAL / STATUS / AUTHORITATIVE STATE / COMPLETED / CURRENT STATE / NEXT ACTION / ACCEPTANCE /
BLOCKERS / STOP CONDITION

Rules:

- Current requirements and confirmed facts only. Superseded ones are deleted, not annotated.
- `STATUS` is `active` while the work continues, `done` once it is finished — a checkpoint
  left `active` will be offered to the next session in this directory.
- `COMPLETED` holds what is done **and** verified, with how it was verified.
- `NEXT ACTION` is exactly one concrete step. Without it the checkpoint cannot be resumed from.
- No discussion history, no rejected options, no explanation of what changed.
- Keep it short enough to read in one glance — it is a resume point, not a report.

Then confirm in one line: path written and what the next action is.

## project

Same as `write`, but into `.claude/CHECKPOINT.md` in the current repository. Before writing,
check `.gitignore` and tell the user whether the file will be tracked. Use this only for a
long task where the repo is the natural home for the state.
