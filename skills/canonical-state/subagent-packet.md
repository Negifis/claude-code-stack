# Subagent task packet

A subagent inherits none of the conversation and cannot tell a live requirement from one the
user replaced four turns ago. Send the canonical state, not the transcript.

## Packet

```text
GOAL
SCOPE
AUTHORITATIVE REQUIREMENTS
CONFIRMED FACTS
RELEVANT FILES
CURRENT STATE
EXPECTED OUTPUT
ACCEPTANCE CRITERIA
DO NOT INCLUDE
```

Rules for filling it:

- Every requirement in the packet is current. Nothing superseded travels with it.
- Confirmed facts are separated from assumptions, and assumptions are labelled as such.
- `DO NOT INCLUDE` names categories of unwanted output — history of attempts, rejected
  alternatives, a changelog of the lane's own edits. Do not re-list deleted entities there;
  naming them re-introduces them.
- One lane, one scope. Overlapping write sets produce conflicting results.
- Assign the tier from the Model Routing Gate and pass `model` explicitly (see
  `subagent-delegation`).

## Integrating what comes back

- Do not adopt the lane's framing automatically.
- Compare its result against the canonical state; drop anything resting on an assumption that
  is no longer current.
- Integrate confirmed conclusions only, and verify claims that matter yourself.
- Update the checkpoint with what the lane actually established.
- Do not launch a second lane on the same analysis without a reason — a different lens or an
  independent check is a reason; wanting a second opinion on the same lens is not.

The `SubagentStart` hook states the receiving half of this contract inside every lane.
