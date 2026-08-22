---
name: local-windows-tooling
description: Before running shell commands on this Windows machine.
---

# Local Windows Tooling

- For code discovery (symbols, callers/callees, call chains, architecture) prefer `codebase-memory-mcp` graph tools over `rg`/`grep`; use `rg` for text/config content, non-code or unindexed files, or a stale/unavailable graph. See the `codebase-memory` skill.
- `rg` is located at:
  `C:\tools\ripgrep-15.1.0-x86_64-pc-windows-msvc\ripgrep-15.1.0-x86_64-pc-windows-msvc\rg.exe`
- Prefer `rg` / `rg --files` for text search.
- If plain `rg` is unavailable on Windows, use the configured absolute path above.
- Prefer PowerShell here-strings or stdin for complex multi-line prompts passed to CLIs.
- Verify which executable will run before relying on a CLI whose native install and npm/global install can shadow each other.
