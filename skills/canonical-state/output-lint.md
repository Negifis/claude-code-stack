# Final output lint

Run this over any result that reaches the user after the task was corrected. It takes one
pass and it is the difference between a clean deliverable and a visibly patched one.

## The pass

1. Find everything that exists only because of an older version of the task.
2. Delete references to rejected options and to mistakes already fixed.
3. Check that the text states the result rather than explaining the change to it.
4. Look for the tells:
   - "теперь не X, а Y" / "now not X but Y"
   - "вместо предыдущего", "instead of the previous"
   - "как вы исправили", "как вы просили", "as you corrected", "as you asked"
   - "больше не", "no longer" — when it points at your own earlier output
   - "ранее я", "я ошибся", "previously I", "I was wrong"
   - any other trace of the editing history that the reader has no use for
5. Reassemble it as a clean first version under the current requirements.

## What the answer contains

- The current task, not the conversation.
- Current entities only.
- A definite final status.

## What it does not contain

- Your struggle with the context.
- Questions already settled.
- Old variants kept for comparison.
- A changelog nobody asked for.
- A full log of your actions when the user asked for the result.
- An offer to continue work that is finished.

## Reviews

A review ends in exactly one of three verdicts:

- accepted;
- accepted with specific reservations;
- not accepted, with the complete list of blockers.

After the listed problems are fixed, run one final check. Do not turn a review into an
open-ended hunt for improvements outside the agreed scope.

## Exception

Edit history stays when the user asked for it: changelog, diff, review of the corrections, a
migration, a version comparison, or an explanation of what went wrong.

The Stop hook lints for this residue automatically in the few turns following a correction,
and stands down on a turn where the user asked for history. When the history is genuinely the
deliverable, make the last line of the final message exactly:

```text
[lint] intentional: <why the history has to stay>
```
