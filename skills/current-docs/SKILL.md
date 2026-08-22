---
name: current-docs
description: Before answering anything version-sensitive about a library, API, framework, CLI or vendor product.
---

# Current Documentation

For current library, framework, API, product, or tool behavior:

- Prefer Context7 MCP when available.
- Resolve the relevant Context7 library ID first unless the user already provided an explicit `/org/project` or `/org/project/version` ID.
- Use official docs, changelogs, RFCs, vendor docs, or source code as the source of truth.
- If Context7 is unavailable, use the best available local or official documentation.
- Mention the limitation only if it affects confidence or the outcome.

Do not rely on memory for version-sensitive behavior: CLI flags, SDK APIs, auth flows, pricing/limits, model names, package defaults, cloud provider support, browser/runtime compatibility, security guidance, and deprecations.
