---
name: current-docs
description: Before answering anything version-sensitive about a library, API, framework, CLI or vendor product.
---

# Current Documentation

For current library, framework, API, product, or tool behavior:

- Recall first: `nlm-memory recall "<library> <behavior>"`. An earlier session may have verified the same behavior; a recorded finding stands when its version and date still apply, and otherwise tells you what to re-check.
- Prefer Context7 MCP when available.
- Resolve the relevant Context7 library ID first unless the user already provided an explicit `/org/project` or `/org/project/version` ID.
- Use official docs, changelogs, RFCs, vendor docs, or source code as the source of truth.
- If Context7 is unavailable, use the best available local or official documentation.
- Mention the limitation only if it affects confidence or the outcome.

Do not rely on model memory for version-sensitive behavior: CLI flags, SDK APIs, auth flows, pricing/limits, model names, package defaults, cloud provider support, browser/runtime compatibility, security guidance, and deprecations.

When a finding took real research, record it with the version it holds for — as a `CONSTRAINT`, `GOTCHA` or `COMMAND` via `nlm-memory remember` (see `project-memory`).
