#!/usr/bin/env node
/**
 * Turn an image_gen raster into a clean, theme-ready SVG.
 *
 *   node vectorize.mjs <input.png> <output.svg> [--tokens] [--keep-backdrop] [--check <dir>]
 *
 * Requires (install once, no API keys):
 *   npm i @neplex/vectorizer @resvg/resvg-js
 *
 * Defaults are the settings proven on flat SaaS-style illustrations. Read the
 * SKILL.md "Tuning" section before changing them — filterSpeckle in particular
 * silently deletes small accent details when raised.
 */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';

// This script lives in the skill directory, but the deps are installed wherever you
// are working. Node resolves bare imports relative to the *script*, not the cwd, so
// resolve them from the cwd explicitly.
const requireFromCwd = createRequire(path.join(process.cwd(), 'noop.js'));
let vectorizer;
try {
  vectorizer = requireFromCwd('@neplex/vectorizer');
} catch {
  console.error('Missing deps. Run this first, in the directory you are calling from:\n  npm i @neplex/vectorizer @resvg/resvg-js');
  process.exit(1);
}
const { vectorize, optimize, ColorMode, Hierarchical, PathSimplifyMode, OptimizePreset } = vectorizer;

const args = process.argv.slice(2);
const [input, output] = args.filter((a) => !a.startsWith('--'));
if (!input || !output) {
  console.error('usage: node vectorize.mjs <input.png> <output.svg> [--tokens] [--keep-backdrop] [--check <dir>]');
  process.exit(1);
}
const useTokens = args.includes('--tokens');
const keepBackdrop = args.includes('--keep-backdrop');
const checkIdx = args.indexOf('--check');
const checkDir = checkIdx !== -1 ? args[checkIdx + 1] : null;

// filterSpeckle 12 is the proven floor: 16+ eats dots on connector lines and inside bubbles.
// pathPrecision 1 is free: same path count, ~35% smaller than 2.
const OPTS = {
  colorMode: ColorMode.Color,
  hierarchical: Hierarchical.Stacked,
  mode: PathSimplifyMode.Spline,
  filterSpeckle: 12,
  colorPrecision: 6,
  layerDifference: 20,
  cornerThreshold: 60,
  spliceThreshold: 45,
  lengthThreshold: 4,
  maxIterations: 10,
  pathPrecision: 1,
};

// "surface" is the body of an object (card, window, shield); the canvas backdrop is
// a separate concern and is dropped unless --keep-backdrop.
const ANCHORS = [
  { tok: 'surface', ref: '#f8fafc' },
  { tok: 'surface', ref: '#ffffff' },
  { tok: 'soft', ref: '#e2e8f0' },
  { tok: 'line', ref: '#64748b' },
  { tok: 'accent', ref: '#2563eb' },
  { tok: 'ok', ref: '#16a34a' },
];
const LIGHT = { surface: '#f8fafc', soft: '#e2e8f0', line: '#64748b', accent: '#2563eb', ok: '#16a34a' };
const DARK = { surface: '#1e293b', soft: '#334155', line: '#94a3b8', accent: '#60a5fa', ok: '#4ade80' };

const hex2rgb = (h) => {
  h = h.replace('#', '');
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
};
const nearest = (hex) => {
  const c = hex2rgb(hex);
  let best = ANCHORS[0], bd = Infinity;
  for (const a of ANCHORS) {
    const r = hex2rgb(a.ref);
    const d = Math.hypot(c[0] - r[0], c[1] - r[1], c[2] - r[2]);
    if (d < bd) { bd = d; best = a; }
  }
  return best.tok;
};

const png = await readFile(input);
let svg = await optimize(await vectorize(png, OPTS), { preset: OptimizePreset.Default });

// vtracer emits width/height but never a viewBox -> the SVG would not scale.
const m = svg.match(/^<svg[^>]*width="(\d+)"[^>]*height="(\d+)"[^>]*>/);
if (!m) throw new Error('could not read width/height from vtracer output');
const [w, h] = [m[1], m[2]];

if (!keepBackdrop) {
  const re = new RegExp(`<path fill="#[0-9a-fA-F]{3,6}" d="M0 0h${w}v${h}H0Z"/>`);
  if (!re.test(svg)) console.warn('warn: no full-canvas backdrop path found to drop');
  svg = svg.replace(re, '');
}

let style = '';
if (useTokens) {
  const used = new Set();
  svg = svg.replace(/fill="(#[0-9a-fA-F]{3,6})"/g, (_, hex) => {
    const tok = nearest(hex.toLowerCase());
    used.add(tok);
    return `fill="var(--${tok})"`;
  });
  const decls = [...used].map((t) => `--${t}:${LIGHT[t]}`).join(';');
  style = `<style>:root{${decls}}</style>`;
  console.log(`tokens used: ${[...used].join(', ')}`);
}

svg = svg.replace(/^<svg[^>]*>/, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" aria-hidden="true" focusable="false">${style}`);
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, svg, 'utf8');
console.log(`${output}  ${(Buffer.byteLength(svg) / 1024).toFixed(1)} KB  ${(svg.match(/<path/g) || []).length} paths`);

// Size proves nothing. Render it back and LOOK at it.
if (checkDir) {
  const { Resvg } = requireFromCwd('@resvg/resvg-js');
  await mkdir(checkDir, { recursive: true });
  const base = path.basename(output, '.svg');
  const themes = useTokens ? [['light', LIGHT, '#ffffff'], ['dark', DARK, '#0f172a']] : [['as-is', null, '#ffffff']];
  for (const [name, vals, card] of themes) {
    let flat = svg.replace(style, '');
    if (vals) for (const [t, v] of Object.entries(vals)) flat = flat.split(`var(--${t})`).join(v);
    flat = flat.replace(/^<svg([^>]*)>/, `<svg$1><rect x="0" y="0" width="${w}" height="${h}" fill="${card}"/>`);
    const out = path.join(checkDir, `${base}-${name}.png`);
    await writeFile(out, new Resvg(flat, { fitTo: { mode: 'width', value: 900 } }).render().asPng());
    console.log(`  check: ${out}`);
  }
  console.log('\nNow OPEN those PNGs and compare against the source. Do not skip this.');
}
