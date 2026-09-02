---
name: task-start
description: Start work on a GitLab issue in its own worktree, branch and named session. Use when the user says "берём issue N", "начни задачу N", "/task-start N", or when work is about to begin on a tracked issue and no branch exists yet. Not for continuing work already under way.
disable-model-invocation: false
---

# Start a task from its issue

One issue, one branch, one worktree, one session. The manual audit of 2026-08-28 found the
cost of skipping this: 112 sessions sharing one working tree, sessions no one could pair with
an issue, and two sessions editing the same files while each Stop gate blamed the other. The
pairing has to be free at the moment work starts, or it does not happen at all.

`$ARGUMENTS` is the issue number (with or without `#`).

## Steps

1. **Read the issue.** `glab issue view <N>` — title, labels, status. If it does not exist or
   is already closed, say so and stop; do not invent a branch for a ticket nobody filed.

2. **Check nobody is already on it.** `git branch -a --list "*<N>*"` and
   `glab mr list --search "<N>"`. If a branch or MR exists, offer to continue there instead of
   opening a second front — that is exactly how duplicates were born before.

3. **Derive the branch name**: `in/<slug>-<N>`, where `<slug>` is three or four words from the
   issue title, transliterated to lowercase ASCII with hyphens. Keep the number last so the
   audit and the SessionStart guard can both find it.

4. **Create the worktree**, from the repository root, so the session never shares a tree:

   ```bash
   claude --worktree in/<slug>-<N>
   ```

   Inside an existing session use the `EnterWorktree` tool with the same name. Never start the
   work in the main checkout: that is the shared-tree failure the guard warns about.

5. **Name the session**: `/rename #<N> <short title>`. The number must be in the name — the
   weekly audit pairs sessions to issues by it, and an unnamed session is invisible to it.

6. **Write the goal into the checkpoint** so a compaction or a restart does not lose it: run
   the `checkpoint` skill with the issue number, its acceptance criteria, and the branch.

7. **Say what was set up** in one line: issue, branch, worktree, session name. Then start the
   actual work under the ordinary rules — `engineering-workflow` for the change,
   `development-verification` before finishing.

## What this deliberately does not do

- It does not create the issue. Work without a ticket stays without a ticket; inventing one
  to satisfy a convention is bookkeeping, not tracking.
- It does not push the branch or open a merge request. The MR belongs at the end of the work,
  where `development-verification` puts it.
- It does not run on an issue already in progress. Continuing work resumes its session
  (`claude --resume "#<N>"`), it does not fork a second one.
