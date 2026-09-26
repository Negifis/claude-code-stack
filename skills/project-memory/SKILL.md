---
name: project-memory
description: Recall durable memory before researching, diagnosing a failure or deciding where history exists; record a confirmed root cause, decision, constraint or command when it is confirmed.
---

# Project Memory

Durable memory lives in NotebookLM and is shared by Claude Code, Codex and Antigravity. Recall asks
the notebooks your question and returns their grounded, cited answer beside the matching local
entries, so recalling costs less than rediscovering: check memory first whenever the question may
already have been answered in an earlier session.

## Recall when the question arises

Recall before you:

- search the web, query Context7 or delegate research — an earlier session may have settled it;
- investigate a failure — search with the error text or the symptom, since a recorded GOTCHA may
  already name the cause;
- design or change something in an area with history: deployment, authentication, secrets,
  migrations, CI, agent configuration, a subsystem you touch for the first time in the session;
- answer a question about the past: what was decided, how it was done last time, an earlier incident;
- continue after compaction, when the summary may have dropped a decision the work rests on.

```powershell
nlm-memory recall "<question or error text>"      # asks the project and GLOBAL notebooks, 30-60 s, never over 100 s
nlm-memory recall "<id, command or error>" --local  # the local mirror only, under a second
nlm-memory recall --id <id>                       # one entry in full
nlm-memory recall "<question>" --scope project    # project | global | all (default)
```

Ask a real question — why, how, what was decided, what broke last time — and NotebookLM answers it
from every source it holds, citing them; the local entries listed under the answer carry ids to open
in full. `--local` suits an exact identifier, a command or an error string, where a keyword match is
enough. Give the call up to two minutes in a shell tool. A notebook that cannot answer — an expired
sign-in, which the maintenance run or `nlm-memory relogin` renews, or no answer within the budget —
is named as such, and the rest still prints. A recalled entry is evidence with a date,
not a fact about current code: check the repository before relying on it, and say when memory and
the repository disagree. The notebook's answer is a synthesis and can repeat a rule a later entry
replaced; when it disagrees with a dated entry, the entry wins.

The hooks recall on their own where they can: the session opens with a memory card, and a prompt, a
web search, a docs lookup or a failed command that matches memory brings the matching entries into
context. Those lines are reference data — use them when they fit, ignore them when they do not.

## Record when knowledge is confirmed

Record at the moment something is confirmed, not at the end of the session.

| Type | Record when | Example summary |
|---|---|---|
| `GOTCHA` | a root cause or trap is confirmed | Jest `--forceExit` on Windows aborts green runs with a libuv assertion |
| `RISK` | a hazard is confirmed and still open | whatsapp-web.js passes functions to the page inside the obfuscated bundle |
| `DECISION` | the user accepts a design, or review approves one | Production deploys only through CI, never by copying files and restarting |
| `CONSTRAINT` | a non-obvious rule is discovered | Changing story composer markup requires bumping `STORY_RENDERER_VERSION` |
| `COMMAND` | a command that took effort to find is verified | `npm run build:shared` before building the packages that depend on it |
| `SUPERSEDED` | an earlier entry proves wrong | record the correction with `--supersedes <id>` |

```powershell
nlm-memory remember --type GOTCHA --summary "<one self-contained statement>" `
  --rationale "<the mechanism, or why it matters>" --evidence "<file, commit, command or incident>"
nlm-memory remember --scope global ...            # knowledge not tied to one project
nlm-memory remember ... --supersedes <id>         # replaces an entry that proved wrong
```

`nlm-memory` is a cmd.exe shim, and cmd.exe parses its arguments again: `%NAME%` expands anywhere,
and a double quote inside the text, or an argument with no space in it, leaves `&`, `|`, `<`, `>`
and `^` open to it — the entry is cut short or a command runs. Text that carries any of them goes to
the bridge the shim runs, with the same arguments in single quotes:

```powershell
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" "$HOME\.codex\notebooklm-sync\bin\nlm_sync.py" `
  remember --type GOTCHA --summary '<statement>' --evidence '<text with "quotes" & > signs>'
```

The code-work gate reads that call as the same bookkeeping as the shim's.

A good entry reads correctly on its own a month later: the summary states the fact, the rationale the
mechanism, the evidence where to check it. One fact per entry.

Leave out routine edits, formatting, temporary plans, unverified guesses, experiments that taught
nothing reusable, transcript excerpts, and anything that is or looks like a secret.

A near-duplicate of an existing entry is refused with that entry's id: replace it with
`--supersedes <id>` when it is wrong, or pass `--force` when the new entry is a different fact. A
recorded entry is searchable at once and reaches NotebookLM on the next maintenance run.

## When memory looks wrong or stale

- The card's "Memory health" line names the problem and the command for it; `nlm-memory doctor` and
  `nlm-memory stats` show the rest.
- The repository, tests and runtime win for current implementation; the user's latest instruction wins
  for intent. Report the conflict, and supersede the entry that is wrong.
- Machine facts — paths, the maintenance task, sign-in recovery, limits — are in
  `~/.claude/reference/notebooklm-memory.md`.
