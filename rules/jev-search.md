# Jev hooks

How to find code with Jev is in CLAUDE.md ("Jev — use it first"). What its hooks do:

- After a grep or Grep with 3+ alternative names over a directory, a hook may add
  "jev find ran on its own ..." with the files that carry it out: open them with an explicit
  range. Nothing added does not mean nothing exists.
- A Read of a large file may be narrowed by a hook to the window that answers the request; read
  another range with an explicit offset/limit when you need more.
