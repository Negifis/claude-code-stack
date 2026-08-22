---
description: "Clean-room rebuild: discard the patched result and reconstruct it from the current requirements."
argument-hint: "[what to rebuild]"
---

# Clean-room rebuild

Target: `$ARGUMENTS` (empty means the deliverable of the current task).

Patching has stopped working. Do not edit the old result again.

1. Restate the canonical state in one short block: goal, current requirements, confirmed
   facts, constraints, acceptance criteria. Take it from the latest instructions and the
   working tree — not from earlier drafts or from the reasoning that produced the rejected
   version.
2. List what must **not** carry over: the structure, wording, ordering, and naming of the
   failed version, and every assumption the user replaced.
3. Read only the files the target actually needs.
4. Produce the new version from scratch.
5. Verify it against the acceptance criteria.
6. Sweep for residue of the superseded approach — renamed symbols, string literals, file and
   config names, doc anchors, comments, synonyms of a deleted concept.
7. Hand over the clean version alone: no comparison with the old one, no explanation of what
   was wrong, no changelog.

Full procedure: `canonical-state` skill -> `clean-room-rebuild.md`.
