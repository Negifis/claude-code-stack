#!/usr/bin/env node
/**
 * Worktree and session hygiene audit.
 *
 * Answers the three questions the manual audit of 2026-08-28 had to answer by hand:
 * which worktrees still hold work nobody saved, which ones are stale enough to remove,
 * and which sessions never got tied to an issue.
 *
 * Read-only unless you pass --fix, which does only what cannot lose anything: prune
 * worktrees whose directory is already gone, and park unsaved work on a wip/ branch the
 * same way the WorktreeRemove hook does.
 *
 * Usage:
 *   node worktree-audit.mjs [repo-path] [--fix] [--json] [--stale-days=21]
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';

const args = process.argv.slice(2);
const flag = (name) => args.some((a) => a === `--${name}`);
const value = (name, fallback) => {
  const hit = args.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
};
const repo = args.find((a) => !a.startsWith('--')) || process.cwd();
const STALE_DAYS = Number(value('stale-days', '21'));
const SESSION_INDEX = join(homedir(), '.claude', 'state', 'session-index.jsonl');

// Untracked agent scaffolding appears in every worktree of a project that ships skills and
// a Codex config. Counting it as work would flag every tree and hide the real ones.
const NOISE = [/^\.agents\/skills\//, /^\.codex\/config\.toml$/, /^\.rxa\//, /^\.design-sync\//];
const NO_HOOKS_PATH = process.platform === 'win32' ? 'NUL' : '/dev/null';

function git(cwd, gitArgs, { allowFail = true, raw = false, env = null } = {}) {
  try {
    // Probing questions like `@{upstream}..HEAD` fail loudly on most branches here, and a
    // null answer is the whole point of asking, so git's stderr is noise, not information.
    const out = execFileSync('git', gitArgs, {
      cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
      stdio: ['ignore', 'pipe', 'ignore'],
      env: env ? { ...process.env, ...env } : process.env,
    });
    // Porcelain status leads with a space for an unstaged change, and trimming the output
    // would shift the first line's path by one character — enough to slip past every filter.
    return raw ? out : out.trim();
  } catch (error) {
    if (allowFail) return null;
    throw error;
  }
}

function worktrees(root) {
  const raw = git(root, ['worktree', 'list', '--porcelain']);
  if (raw === null) return [];
  return raw.split(/\r?\n\r?\n/).map((block) => {
    const path = /^worktree (.*)$/m.exec(block)?.[1];
    const branch = /^branch refs\/heads\/(.*)$/m.exec(block)?.[1] ?? null;
    return { path, branch, detached: /^detached$/m.test(block), prunable: /^prunable/m.test(block) };
  }).filter((w) => w.path);
}

function allChanges(path) {
  const raw = git(path, ['status', '--porcelain'], { raw: true });
  if (raw === null) return null;
  // Porcelain v1 puts the two status columns and one space in front of the path, and the
  // first column is a space for an unstaged change — so trimming before slicing eats the
  // wrong characters and every noise pattern then misses.
  return raw.split(/\r?\n/).filter((line) => line.length > 3)
    .map((line) => line.slice(3).trim().replace(/^"|"$/g, ''));
}

function heldByLiveSession(path) {
  const lockFile = join(homedir(), '.claude', 'state', 'tree-locks',
    `${String(path).replace(/\\/g, '/').toLowerCase().replace(/[^a-z0-9\-_]/g, '_').slice(-120)}.json`);
  if (!existsSync(lockFile)) return false;
  try {
    const holders = JSON.parse(readFileSync(lockFile, 'utf8')).sessions || {};
    const cutoff = Date.now() / 1000 - 12 * 3600;
    return Object.values(holders).some((h) => h && typeof h.ts === 'number' && h.ts > cutoff);
  } catch {
    return false;
  }
}


// Mirrors `unreachable_commits` in ~/.claude/hooks/worktree_snapshot.py, including the reason
// the ref list is spelled out: `--exclude=<glob> --branches` does not survive the `--not` that
// has to sit between them, and silently counts zero. Change both or neither.
function unreachableCommits(path, branch) {
  const upstream = git(path, ['rev-list', '--count', '@{upstream}..HEAD']);
  if (upstream !== null && /^\d+$/.test(upstream)) return Number(upstream);
  const refs = git(path, ['for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/remotes']);
  if (refs === null) return 0;
  const own = branch ? `refs/heads/${branch}` : null;
  const others = refs.split(/\r?\n/).map((r) => r.trim()).filter((r) => r && r !== own);
  const count = git(path, ['rev-list', '--count', 'HEAD', '--not', ...others]);
  return count !== null && /^\d+$/.test(count) ? Number(count) : 0;
}

function ageDays(path) {
  const stamp = git(path, ['log', '-1', '--format=%ct']);
  if (!stamp || !/^\d+$/.test(stamp)) return null;
  return Math.floor((Date.now() / 1000 - Number(stamp)) / 86400);
}

function sessionsWithoutIssue() {
  if (!existsSync(SESSION_INDEX)) return [];
  const seen = new Map();
  for (const line of readFileSync(SESSION_INDEX, 'utf8').split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      if (record.session_id) seen.set(record.session_id, record);
    } catch { /* a truncated line costs one record, not the audit */ }
  }
  return [...seen.values()]
    .filter((r) => r.branch && !/\d{2,6}/.test(r.branch))
    .filter((r) => r.cwd && existsSync(r.cwd));
}

const safeLabel = (label) => label.replace(/[^A-Za-z0-9-_.]/g, '-').slice(0, 60);

/**
 * Whether an existing wip/ branch already holds this exact state.
 *
 * Globs on the sanitized label, the same name `snapshotBranch` creates from: matching on the
 * raw basename never finds anything once the name contains a character the sanitizer
 * rewrites, and the sweep then parks the same tree again every week.
 *
 * `pointer` is the tree OID for a dirty park and the HEAD commit for a ref-only one, because
 * those are what each park pins.
 */
function alreadyParked(path, label, pointer, kind) {
  const existing = git(path, ['for-each-ref', '--format=%(refname:short)',
    `refs/heads/wip/${safeLabel(label)}-*`]);
  if (!existing) return false;
  const suffix = kind === 'tree' ? '^{tree}' : '';
  return existing.split(/\r?\n/).map((b) => b.trim()).filter(Boolean)
    .some((branch) => git(path, ['rev-parse', `${branch}${suffix}`]) === pointer);
}

function snapshotBranch(path, label) {
  const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const safe = safeLabel(label);
  let target = `wip/${safe}-${stamp}`;
  for (let n = 2; n < 50 && git(path, ['rev-parse', '--verify', '--quiet', target]) !== null; n += 1) {
    target = `wip/${safe}-${stamp}-${n}`;
  }
  return target;
}

const report = { repo, worktrees: [], sessionsWithoutIssue: [], fixed: [] };
const list = worktrees(repo);
const doFix = flag('fix');

for (const [index, tree] of list.entries()) {
  const alive = existsSync(tree.path);
  const every = alive ? allChanges(tree.path) : null;
  const changes = every ? every.filter((f) => !NOISE.some((rx) => rx.test(f))) : null;
  const held = alive ? heldByLiveSession(tree.path) : false;
  const ahead = alive && !tree.prunable ? unreachableCommits(tree.path, tree.branch) : 0;
  const age = alive ? ageDays(tree.path) : null;
  const entry = {
    path: tree.path,
    branch: tree.branch ?? (tree.detached ? '(detached)' : null),
    missing: !alive,
    prunable: tree.prunable,
    unsavedFiles: changes ? changes.length : 0,
    noiseFiles: every && changes ? every.length - changes.length : 0,
    sample: changes ? changes.slice(0, 3) : [],
    unreachableCommits: ahead,
    heldByLiveSession: held,
    ageDays: age,
    // Stale means nothing would be lost, so it counts every change, noise included. The
    // noise filter decides what is worth *reporting* as work; letting it also decide what is
    // safe to delete would recommend removing a worktree whose only edit was to a filtered
    // path — and a hand-run `git worktree remove` never reaches the snapshot hook.
    stale: age !== null && age > STALE_DAYS && every !== null && every.length === 0 && ahead === 0,
  };

  // A tree a live session is sitting in is not the sweep's business: its owner is still
  // editing, and the WorktreeRemove hook covers it when that session ends.
  if (doFix && alive && !tree.prunable && !held && every !== null
    && (every.length > 0 || ahead > 0)) {
    const label = tree.path.split(/[\\/]/).filter(Boolean).pop() || 'worktree';
    let saved = null;
    if (every.length > 0) {
      // Plumbing, not `switch -c`: these worktrees stay alive after the sweep, and moving a
      // live session's HEAD onto a wip branch behind its back recreates exactly the
      // misattribution this whole system exists to end. It also works mid-merge, where
      // `switch` refuses. `add -A` writes the index; nothing else is touched.
      const head = git(tree.path, ['rev-parse', 'HEAD']);
      const scratch = join(tmpdir(), `worktree-audit-${process.pid}-${index}.index`);
      const scratchEnv = { GIT_INDEX_FILE: scratch };
      // Seed from HEAD: an empty index makes `add -A` re-apply ignore rules to files that
      // are tracked and ignored, dropping their edits and recording them as deletions.
      const seeded = head ? git(tree.path, ['read-tree', head], { env: scratchEnv }) : '';
      const staged = seeded === null ? null : git(tree.path, ['add', '-A'], { env: scratchEnv });
      const treeish = staged === null ? null : git(tree.path, ['write-tree'], { env: scratchEnv });
      try { rmSync(scratch, { force: true }); } catch { /* a leftover scratch index is inert */ }
      // Re-parking an unchanged tree every sweep would bury the real rescues under a fresh
      // branch per run, and this sweep visits every worktree weekly.
      if (treeish !== null && alreadyParked(tree.path, label, treeish, 'tree')) {
        entry.alreadyParked = true;
      } else {
        const commit = treeish === null ? null : git(tree.path, ['commit-tree', treeish,
          ...(head ? ['-p', head] : []),
          '-m', `snapshot: unfinished work from worktree ${label}`]);
        const target = snapshotBranch(tree.path, label);
        if (commit && git(tree.path, ['branch', target, commit]) !== null) saved = target;
      }
    } else {
      // The ref-only park needs the same guard: a clean-but-ahead tree would otherwise
      // collect a fresh branch at the same commit on every weekly sweep.
      const head = git(tree.path, ['rev-parse', 'HEAD']);
      if (head && alreadyParked(tree.path, label, head, 'commit')) {
        entry.alreadyParked = true;
      } else {
        const target = snapshotBranch(tree.path, label);
        if (git(tree.path, ['branch', target, 'HEAD']) !== null) saved = target;
      }
    }
    if (saved) {
      entry.savedTo = saved;
      report.fixed.push({ path: tree.path, savedTo: saved });
    } else if (!entry.alreadyParked) {
      entry.saveFailed = true;
      report.fixed.push({ path: tree.path, failed: 'could not park the work on a branch' });
    }
  }

  report.worktrees.push(entry);
}

if (doFix) {
  const pruned = git(repo, ['worktree', 'prune', '-v']);
  if (pruned) report.fixed.push({ pruned: pruned.split(/\r?\n/).filter(Boolean) });
}

report.sessionsWithoutIssue = sessionsWithoutIssue().map((r) => ({
  session_id: r.session_id, cwd: r.cwd, branch: r.branch,
}));

if (flag('json')) {
  console.log(JSON.stringify(report, null, 2));
  process.exit(0);
}

const unsaved = report.worktrees.filter((w) => w.unsavedFiles > 0 || w.unreachableCommits > 0);
const stale = report.worktrees.filter((w) => w.stale);
const gone = report.worktrees.filter((w) => w.missing || w.prunable);

console.log(`Worktree audit — ${repo}`);
console.log(`  ${report.worktrees.length} worktrees, ${unsaved.length} holding unsaved work, ` +
  `${stale.length} stale (>${STALE_DAYS}d, clean), ${gone.length} removable`);

if (unsaved.length) {
  console.log('\nUnsaved work (never remove these without a snapshot):');
  for (const w of unsaved) {
    const saved = w.savedTo ? ` -> saved to ${w.savedTo}` : '';
    console.log(`  ${w.path}  [${w.branch}]  files=${w.unsavedFiles} commits=${w.unreachableCommits}${saved}`);
  }
}
if (stale.length) {
  console.log(`\nStale and clean — safe to remove with \`git worktree remove\`:`);
  for (const w of stale) console.log(`  ${w.path}  [${w.branch}]  ${w.ageDays}d`);
}
if (gone.length) {
  console.log('\nDirectory already gone — `git worktree prune` clears these:');
  for (const w of gone) console.log(`  ${w.path}`);
}
if (report.sessionsWithoutIssue.length) {
  console.log('\nSessions whose branch carries no issue number:');
  for (const s of report.sessionsWithoutIssue) console.log(`  ${s.branch}  ${s.cwd}`);
}
if (report.fixed.length) console.log(`\nFixed: ${JSON.stringify(report.fixed)}`);
if (!unsaved.length && !stale.length && !gone.length) console.log('\nNothing to clean up.');
