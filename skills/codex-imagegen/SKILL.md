---
name: codex-imagegen
description: Generate an image or illustration on request — draw, нарисуй, сгенерируй картинку.
---

# Codex Image Generation

Claude cannot generate images itself. Codex ships a system skill `imagegen`
(`~/.codex/skills/.system/imagegen`) that uses the built-in `image_gen` tool — no
`OPENAI_API_KEY` needed in default mode. This skill routes image-generation requests to Codex.

**The core fact:** no model, Claude or Codex, "generates" SVG. Asked for a vector, both
hand-author `<path d="…">` coordinates — which is drawing by hand, badly, in a text editor.
The only real generation is raster. If you want a *generated* vector, you generate a raster
and vectorize it. That pipeline is below.

## When to use

- New raster: concept art, photorealistic shot, cover, hero, illustration, mockup, sprite, texture, logo.
- Edit an existing image: inpainting, background replacement, lighting change, object removal, compositing.
- Reference-guided generation: user supplies images for style/composition/mood.
- Transparent-background cutout.
- **SVG illustration that should look designed, not hand-plotted** — see "Generating SVG" below.

## When NOT to use

- Production icons — search an icon set (Lucide, Tabler, Heroicons) instead; better licensing, sizing, a11y.
- Architecture / flowchart / ER diagrams that a human must later edit — author those directly (draw.io, Mermaid, hand SVG).
- Editing a project asset that already exists in an editable native format.
- The user explicitly wants deterministic code-native output.

## How to invoke

Delegate through the `codex:codex-rescue` subagent using the **Agent tool**
(`subagent_type: "codex:codex-rescue"`). Do not call it as a skill. Do not run
`codex-companion.mjs` yourself — the subagent handles routing and returns Codex output verbatim.

Execution mode:
- **One image → foreground:** put `--fresh --wait` at the start of the prompt.
- **Many images / variants → background:** put `--fresh --background` at the start.

The subagent adds `--write` by default (needed so Codex can save the file).

### Prompt template to forward

```
--fresh --wait

Используй свой встроенный скилл imagegen (инструмент image_gen) для генерации изображения.
НЕ рисуй SVG руками, НЕ используй Python/Pillow/скрипты.

Описание: <subject, style, mood, colors, composition, aspect/size>

Сохрани итоговый файл в <destination, e.g. output/imagegen/> и верни абсолютный путь к PNG.
```

Fill the description with everything the user asked for. If the user gave no style, pick a
sensible one and say so. Always ask Codex to **return the final file path**.

Say "НЕ рисуй SVG руками, НЕ используй Python/Pillow" explicitly. Without it Codex will
sometimes satisfy the request by drawing shapes programmatically, which is not generation.

## Generating SVG

Three stages, all local, no API keys, no paid services:

```
image_gen (Codex)  →  vtracer (@neplex/vectorizer)  →  semantic tokenization
```

### 1. Generate the raster

Prompt as above. For UI/product illustration, spell out the hard constraints or the model
will bake in text and brand marks:

- flat vector style, minimal, calm, rounded geometry, one accent colour, generous negative space
- **no text, no letters, no digits, no labels, no brand logos, no watermarks**
- 3:2 landscape, very light background (a flat backdrop vectorizes cleanly; a gradient does not)

Typical output: 1536×1024 PNG, ~1.1 MB.

### 2. Vectorize

```bash
npm i @neplex/vectorizer @resvg/resvg-js       # once, in your working dir; no keys
node ~/.claude/skills/codex-imagegen/scripts/vectorize.mjs \
  in.png out.svg --tokens --check ./check
```

`scripts/vectorize.mjs` carries the tuned defaults, drops the backdrop, adds the `viewBox`,
tokenizes the fills, and renders both themes back to PNG so you can verify. It resolves its
deps from the **cwd**, so run `npm i` in the directory you call it from.

### 3. Verify by looking

**Render the SVG back to raster and open it.** Every mistake below produced a *smaller,
valid, plausible-looking* SVG. Size and a green XML parse prove nothing.

## Tuning (learned the hard way)

| Knob | Value | Why |
|---|---|---|
| `filterSpeckle` | **12** | Proven floor. At 16+ it silently deletes small accent details — dots on connector lines, dots inside a chat bubble. The file gets 2× smaller and the drawing gets wrong. |
| `pathPrecision` | **1** | Free win: identical path count, ~35% smaller than `2` (41.3 → 26.6 KB). Pure coordinate rounding. |
| `colorPrecision` | 6 | Below 5 nearby tones merge and shading collapses. |
| `hierarchical` | `Stacked` | `Cutout` triples the path count for no visible gain on flat art. |
| `mode` | `Spline` | Curves, not polylines. |

Other hard-won facts:

- **vtracer emits no `viewBox`** — only `width`/`height`. Without one the SVG will not scale. Add it.
- **Drop the full-canvas backdrop path** (`d="M0 0h{W}v{H}H0Z"`). Keep it and the object
  "surface" colour collapses into the canvas colour: in dark theme, windows and shields lose
  their body and dissolve into the background. Transparent background, let the host card show through.
- **Tokenize, don't ship a light/dark pair.** Cluster each `fill="#…"` to its nearest semantic
  anchor (`surface / soft / line / accent / ok`) by RGB distance, emit `fill="var(--…)"`, and
  declare the palette in an embedded `<style>`. One themed file beats two rasters.
- **Never regenerate a dark variant from scratch.** The model will redraw it and the geometry
  drifts — nodes gain circles, bubbles change shape — so the illustration visibly jumps when
  the theme flips. Either recolour deterministically (`sharp`), or, better, tokenize and skip
  the second file entirely.
- **Check the host's theme before shipping a dark block.** An `@media (prefers-color-scheme: dark)`
  inside an SVG loaded via `<img>` follows the **OS**, not the app's theme toggle. If the app is
  light-only, that block is a bug: an OS-dark visitor gets a dark illustration on a white card.
- **Without `vite-plugin-svgr`, `currentColor` and outer CSS variables do not reach the SVG.**
  It renders as an isolated document. Colours must be declared inside it. (Under svgr the SVG
  is inlined and the tokens *do* resolve against the app.)
- Keep the root `aria-hidden="true" focusable="false"` for decorative art; empty `alt` on the `<img>`.
- Vite emits SVGs >4 KB as files; below that it inlines them as base64 data URIs.

## Tools that do NOT solve this (checked 2026-07-08)

- **SVGMaker MCP** — the only genuine text→SVG generator in the wild, needs a paid `SVGMAKER_API_KEY`.
- **SVG.new** — vectorization, API gated behind a Pro plan (~$10/mo). `vtracer` is free and local.
- **drawio-skill** — needs the draw.io desktop CLI for SVG export; and it targets architecture
  diagrams, not illustration.
- **diagram-design** — not a generator: instructions under which *you* hand-author the SVG again.
- **mcp-universal-icons** — icon lookup (Lucide/Tabler), not generation. Good at what it does.

## Save location

- Codex saves under `$CODEX_HOME/generated_images/...` by default, then copies into the workspace.
- If the user named a destination, pass it in the prompt. Default project destination: `output/imagegen/`.
- Keep the source PNGs out of git — they are large and the SVG is the deliverable.

## After Codex returns

Return the file path verbatim; do not paraphrase it away. Treat the output as a draft: inspect
it, check for baked text or brand marks (the model sneaks them in), and verify against the
constraints you set before you ship it.

## Notes / gotchas

- Default mode uses the built-in `image_gen` tool — **no API key required**. Only the CLI
  fallback (`gpt-image-1.5`, native transparency) needs `OPENAI_API_KEY`; don't switch to it
  unless the user asks.
- For a transparent background the default path uses a flat chroma-key backdrop and removes it
  locally — let Codex handle it.
- Codex has a Stop hook that appends a diary-confirmation message. When parsing its output,
  the *last* message is often that trailer, not the result.
