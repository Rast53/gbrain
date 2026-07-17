/**
 * P1-R3.5 regression (TASK-gbrain-canonical-post-closeout-hardening, N9):
 * a successful up_to_date sync must advance sources.last_sync_at.
 *
 * Before the fix, last_sync_at advanced ONLY together with last_commit
 * (writeSyncAnchor), so a no-op sync left the freshness bookmark stale
 * forever — and the autopilot freshness targeting re-dispatched a targeted
 * sync for the same source on every tick (observed live 2026-07-16/17:
 * sources reported stale 8-13h while targeted syncs succeeded up_to_date).
 *
 * Contract: success (incl. no-op) advances the stamp; failure never does —
 * last_sync_at keeps its "last SUCCESSFUL sync" semantics.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach, afterEach } from 'bun:test';
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'fs';
import { execSync } from 'child_process';
import { tmpdir } from 'os';
import { join } from 'path';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import { resetPgliteState } from './helpers/reset-pglite.ts';

let engine: PGLiteEngine;
let repoPath: string;
const SRC = 'n9-freshness-src';

function gitInit(repo: string): void {
  execSync('git init', { cwd: repo, stdio: 'pipe' });
  execSync('git config user.email "test@test.com"', { cwd: repo, stdio: 'pipe' });
  execSync('git config user.name "Test"', { cwd: repo, stdio: 'pipe' });
}

function pageMd(title: string): string {
  return `---\ntype: concept\ntitle: ${title}\n---\n\nBody for ${title}.\n`;
}

async function lastSyncAt(id: string): Promise<string | null> {
  const rows = await engine.executeRaw<{ last_sync_at: string | null }>(
    `SELECT last_sync_at FROM sources WHERE id = $1`,
    [id],
  );
  return rows[0]?.last_sync_at ?? null;
}

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
}, 60_000);

afterAll(async () => {
  if (engine) await engine.disconnect();
}, 60_000);

beforeEach(async () => {
  await resetPgliteState(engine);
  repoPath = mkdtempSync(join(tmpdir(), 'gbrain-n9-'));
  gitInit(repoPath);
  mkdirSync(join(repoPath, 'notes'), { recursive: true });
  writeFileSync(join(repoPath, 'notes', 'base.md'), pageMd('Base'));
  execSync('git add -A', { cwd: repoPath, stdio: 'pipe' });
  execSync('git commit -m "initial"', { cwd: repoPath, stdio: 'pipe' });
  await engine.executeRaw(
    `INSERT INTO sources (id, name, local_path, config, created_at)
     VALUES ($1, $1, $2, '{}'::jsonb, now()) ON CONFLICT (id) DO NOTHING`,
    [SRC, repoPath],
  );
});

afterEach(() => {
  if (repoPath) rmSync(repoPath, { recursive: true, force: true });
});

describe('P1-R3.5 — up_to_date freshness honesty (N9)', () => {
  test('[CRITICAL] successful up_to_date sync advances last_sync_at', async () => {
    const { performSync } = await import('../src/commands/sync.ts');

    const full = await performSync(engine, { repoPath, sourceId: SRC, full: true, noPull: true, noEmbed: true });
    expect(['synced', 'first_sync']).toContain(full.status);
    const t1 = await lastSyncAt(SRC);
    expect(t1).not.toBeNull();

    // Cross the timestamp granularity so the comparison is meaningful.
    await new Promise((r) => setTimeout(r, 1200));

    const second = await performSync(engine, { repoPath, sourceId: SRC, noPull: true, noEmbed: true });
    expect(second.status).toBe('up_to_date');
    const t2 = await lastSyncAt(SRC);
    expect(t2).not.toBeNull();
    expect(new Date(t2!).getTime()).toBeGreaterThan(new Date(t1!).getTime());
  }, 120_000);

  test('failed sync never advances last_sync_at', async () => {
    const { performSync } = await import('../src/commands/sync.ts');
    const FAIL_SRC = 'n9-fail-src';
    await engine.executeRaw(
      `INSERT INTO sources (id, name, local_path, config, created_at)
       VALUES ($1, $1, $2, '{}'::jsonb, now()) ON CONFLICT (id) DO NOTHING`,
      [FAIL_SRC, join(repoPath, 'does-not-exist')],
    );

    await expect(
      performSync(engine, {
        repoPath: join(repoPath, 'does-not-exist'),
        sourceId: FAIL_SRC,
        noPull: true,
        noEmbed: true,
      }),
    ).rejects.toThrow();

    expect(await lastSyncAt(FAIL_SRC)).toBeNull();
  }, 120_000);
});
