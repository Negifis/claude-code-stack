---
name: compact-instructions
description: When writing or reviewing compaction and handoff summaries.
---

# Compact Instructions

When conversation is compacted, preserve:

- the user’s current task and last explicit request;
- file paths, function names, config keys, commands, errors, and tests already read or edited;
- selected skills and why they were selected, when relevant;
- confirmed root cause and active hypothesis;
- decisions explicitly approved by the user;
- unresolved blockers and verification status;
- temporary workarounds and required follow-up.

The durable carrier for all of this is the task checkpoint (`canonical-state` skill → `checkpoint.md`, `/checkpoint`): write or refresh it before an expected compaction, and the SessionStart hook restores it afterwards. A compact summary is a fallback, not the state.

Drop:

- raw tool-output dumps;
- search result listings;
- automatic memory context blocks;
- stale intermediate plans;
- repeated exploration details.
