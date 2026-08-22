---
name: claude-code-uiux-design
description: For anything the end user sees — UI, UX, accessibility, interface copy, visual and product design, landing, pricing, onboarding.
---

# Claude Code UX/UI and Design Delegation

Use Claude Code for end-user-visible surfaces and artifacts: UI/UX, interface copy,
public/product/marketing text, visual ideas, visual/product design, motion, onboarding, landing
pages, paywalls, checkout, mockups, and similar work a user will see or read. This route does
not satisfy the Code Work Gate by itself; the parent applies only the candidate-bound QA,
optional simplify, and risk-proportionate review required by `development-verification`.

Trigger this route for any task whose primary output or review target is:

- product/interface copy: buttons, labels, empty states, error messages, onboarding, settings, tooltips, notifications, microcopy, form validation, and user-flow text;
- public/product/marketing copy that an end user will read, including landing pages, feature announcements, app-store copy, paywalls, checkout text, and in-product education;
- UX/UI design: information architecture, user journeys, wireframes, interaction models, responsive behavior, accessibility, visual hierarchy, layout, design tokens, and design-system decisions;
- visual exploration: visual ideas, mockups, art direction, motion direction, presentation-style visuals, and product surfaces tightly coupled to what the user sees.

## Model and reasoning

- Keep design, copy, and user-visible product judgment with the primary Claude agent. Do not
  create a separate session merely to route models.
- Start with the configured model and effort. Increase depth for a broad product flow,
  accessibility-sensitive interaction, pricing/checkout, auth, or another named high-risk
  decision; routine copy and localized UI edits do not require maximum effort.
- Use a separate `claude -p` session only when the caller explicitly requests it or when a
  bounded owned implementation genuinely benefits from isolation.
- If a selected model or effort is unavailable, retry once with the closest supported setting
  and state the material limitation. Do not walk an open-ended fallback ladder.

## Claude CLI launch notes

- Before relying on Claude CLI, verify the executable that will actually run: `Get-Command claude` and `claude --version`. Native installs can shadow an npm global `@anthropic-ai/claude-code` package, so do not assume a package update changed the active binary.
- For a non-interactive run, choose model and effort proportionately and inspect the init event
  to confirm the resolved model and available tools/MCP servers/skills.
- For delegated Claude work that may need follow-up, use session persistence: run the first call in progress-visible streaming mode when the work is long, visual, or tool-using, store the first available `session_id` from the stream/json output, and continue with `claude -p --resume <session_id> ...`. Do not use `--no-session-persistence` for delegated work that may need iteration.
- With `claude -p` / `--print`, provide input through stdin or as the prompt argument. In PowerShell, prefer a here-string variable or pipe; otherwise complex quoted flags can leave Claude with no prompt and produce `Input must be provided either through stdin or as a prompt argument when using --print`.
- Do not use `--bare` with native/keychain-authenticated Claude unless API-key auth is configured. Bare mode skips the normal logged-in environment and can fail with `Not logged in`.
- Avoid `--permission-mode plan` for non-interactive `-p` automation runs; it can behave like an interactive planning session and stall. Use a tight prompt plus explicit permissions, or a trusted automation sandbox with skipped permissions and post-diff review.
- For long, visual, or tool-using runs, prefer progress-visible stream JSON output with the
  selected model/effort and a writable permission mode. Keep session persistence on when one
  bounded follow-up may be needed.
- Treat visual/user-visible implementation as writable delegation, not read-only review. Give Claude a bounded owned write scope in the prompt and let it edit those files directly. Use read-only delegation only when the user explicitly asks for critique, review, or ideas without implementation.
- Do not pass a read-only `--tools` or `--allowedTools` set for visual implementation. Include edit-capable tools such as `Edit`, `Write`, and needed `Bash`, or use `--tools default`; add `--add-dir` when Claude must edit owned directories outside the current working directory.
- Use `--dangerously-skip-permissions` only in a trusted sandbox or disposable worktree, only with a prompt that limits the write scope, and always verify `git diff --name-only` afterward. If not using skipped permissions, keep the allowed tool list simple and explicit, and use a writable permission mode such as `--permission-mode acceptEdits` when supported.
- Treat local permission warnings such as unmatched deny rules as non-fatal until Claude exits nonzero; they may print before an otherwise valid run.
- If a Claude CLI run appears silent or stuck, inspect the process, stream events, and file diff
  once, then use the bounded wait/follow-up policy from `subagent-delegation`. Do not relaunch a
  duplicate session or wait without a task-specific limit.
- For end-user-visible work, prompt the selected primary model as the
  designer/writer/implementer with ownership of the assigned visual/user-visible scope, not
  only as a reviewer. Explicitly include UX, visual design, interface copy, public copy, and
  motion requirements: purposeful restrained motion, `prefers-reduced-motion`, keyboard/touch
  accessibility, stable layout without overlap or shift, and no new dependencies unless
  justified.
- A separate Claude contribution is still a draft. The parent inspects the diff, enforces
  repository conventions, invokes `simplify` before final review when it is useful (preserving
  its three-lens pass), runs candidate-bound checks, and follows `development-verification`,
  whose review lane is Codex first and the native reviewer as fallback.

## Output and review requirements

- Claude Code output is evidence, not automatic final truth. The main agent remains responsible for factual accuracy, safety, repository/product consistency, localization, accessibility, and final integration.
- For interface copy, require a UX-writing pass: user goal, action clarity, error recovery, state specificity, tone, localization constraints, and accessibility labels.
- For design work, require a design-review pass: user flow, visual hierarchy, constraints, edge states, responsive behavior, accessibility, and consistency with the existing design system.
- Preserve project conventions and product vocabulary. Do not introduce new design patterns, components, claims, metrics, certifications, or outcomes without source evidence or user approval.
