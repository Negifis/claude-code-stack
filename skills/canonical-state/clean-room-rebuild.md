# Clean-room rebuild

For when patching has stopped working.

## Triggers

- The same mistake appears twice.
- The previous fix visibly failed to address what the user objected to.
- Two consecutive corrections landed on the same piece of work.
- The result has been patched so often that its structure now belongs to a requirement nobody
  holds any more.

## Rebuild

1. Stop patching. The next edit to the old artifact is the wrong move.
2. Do not repeat the previous explanation, and do not defend the reading the user replaced.
3. Take only: the current requirements, the confirmed facts, and the files that actually
   matter.
4. Produce a new result from those. Do not inherit the structure, wording, ordering, or naming
   of the failed version — a rebuild that keeps its skeleton reproduces its assumptions.
5. Check it against the acceptance criteria.
6. Hand over the clean version alone.

## Ordinary error recovery

When the user says the result is wrong but patching has not failed yet:

- Do not just fix the sentence they pointed at.
- Decide first whether the model of the task changed or only one detail did.
- Update the canonical state.
- Remove what the old assumption caused, everywhere it reached.
- Rebuild the affected part from scratch.
- Sweep the rest of the result for consequences of the same error — a wrong fact usually
  propagated into conclusions drawn from it.

## Sweeping the residue

Before finishing, search for what survives only because of the superseded version: renamed
symbols, string literals, file and config names, doc anchors, test names, comments, synonyms
of a deleted concept. Grep for the old names. Residue is what makes a "fixed" result still
read as a patched one.
