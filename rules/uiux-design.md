---
paths:
  - "**/*.tsx"
  - "**/*.jsx"
  - "**/*.vue"
  - "**/*.svelte"
  - "**/*.astro"
  - "**/*.razor"
  - "**/*.html"
  - "**/*.styles.ts"
  - "**/*.styles.js"
  - "**/*.css"
  - "**/*.scss"
  - "**/*.sass"
  - "**/*.less"
  - "**/components/**"
  - "**/ui/**"
  - "**/styles/**"
  - "**/design-system/**"
  - "**/*.stories.ts"
  - "**/*.stories.js"
  - "**/tailwind.config.ts"
  - "**/tailwind.config.js"
---

<!-- jev-stage:start -->
When selecting among existing headline/format/content/tool candidates (generation itself stays with the main model), follow the mandatory eligible scenarios in
the `jev-workflow` skill before making that comparison yourself.
Load its MCP tools once per session via ToolSearch `select:mcp__jev-workflow__judge,mcp__jev-workflow__prepare_and_delegate`, or use its
`stage.py` CLI; then apply IDs and inspect disputed sources.
Exact/formal decisions and final validation stay native; preserve required checks,
permission boundaries and confidential data. If this role cannot use an allowed interface,
continue natively with intact sources and report the concrete limitation.
<!-- jev-stage:end -->







# UI/UX and Design Rule

When work affects user-facing UI, interface text, layout, animation, onboarding, landing pages, checkout/paywalls, or design-system behavior:

- treat UX, visual hierarchy, accessibility, responsive behavior, and interface copy as first-class requirements;
- use purposeful restrained motion and support `prefers-reduced-motion`;
- preserve keyboard and touch accessibility;
- avoid layout shift, overlap, and hidden focus states;
- do not add dependencies unless justified;
- verify final copy and UI states against project vocabulary and existing patterns.
