/**
 * Cycle sync phase on a MANAGED brain must not request a git pull.
 *
 * A managed brain routes sync through the persistence coordinator, which
 * refuses `git pull` outside an explicit drained maintenance window
 * (`Managed sync requires --no-pull`, src/core/persistence/sync-discovery.ts).
 * The autopilot/cycle sync phase therefore has to force noPull=true on a
 * managed brain and record WHY the pull was skipped, instead of failing the
 * phase with `writer_coordinator_required`.
 *
 * `performSync` is stubbed so the assertion is about the phase's pull decision
 * + observability note, not about setting up a full managed source binding.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, mock, test } from 'bun:test';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import { resetPgliteState } from './helpers/reset-pglite.ts';
import { withEnv, emptyHome } from './helpers/with-env.ts';

interface SyncCallOpts { noPull?: boolean; dryRun?: boolean; [key: string]: unknown }
const performSyncCalls: SyncCallOpts[] = [];
mock.module('../src/commands/sync.ts', () => ({
  performSync: async (_engine: unknown, opts: SyncCallOpts) => {
    performSyncCalls.push(opts);
    return {
      status: 'synced',
      fromCommit: null,
      toCommit: 'deadbeef',
      added: 1,
      modified: 0,
      deleted: 0,
      renamed: 0,
      chunksCreated: 1,
      pagesAffected: [],
      filesImported: 1,
    };
  },
  SyncLockBusyError: class SyncLockBusyError extends Error {
    override name = 'SyncLockBusyError';
  },
}));

const { runCycle } = await import('../src/core/cycle.ts');

let engine: PGLiteEngine;
let brainDir: string;

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
  brainDir = mkdtempSync(join(tmpdir(), 'gbrain-managed-pull-'));
  writeFileSync(join(brainDir, '.gitkeep'), '');
}, 120_000);

afterAll(async () => {
  await engine.disconnect();
  rmSync(brainDir, { recursive: true, force: true });
  mock.restore();
});

beforeEach(async () => {
  await resetPgliteState(engine);
  performSyncCalls.length = 0;
});

async function setManaged(enabled: boolean): Promise<void> {
  await engine.executeRaw('UPDATE persistence_brain SET enabled=$1 WHERE singleton=1', [enabled]);
}

describe('cycle sync phase pull policy on a managed brain', () => {
  test('managed brain: requested pull is forced off and the phase records the skip', async () => {
    await withEnv({ GBRAIN_HOME: emptyHome() }, async () => {
      await setManaged(true);

      const report = await runCycle(engine, { brainDir, phases: ['sync'], pull: true });
      const sync = report.phases.find((p) => p.phase === 'sync');
      expect(sync).toBeDefined();

      // No failure — the sync phase still runs, just without a pull.
      expect(sync!.status).toBe('ok');
      expect(sync!.error).toBeUndefined();
      expect(report.status).not.toBe('failed');

      // The phase asked the managed sync path for noPull (the coordinator's
      // hard requirement) instead of `pull: true`.
      expect(performSyncCalls.length).toBe(1);
      expect(performSyncCalls[0].noPull).toBe(true);

      // Observability: the skipped pull is disclosed in the phase result.
      expect(sync!.details.pullSkipped).toBe(true);
      expect(sync!.details.pullSkippedReason).toBe('managed_brain');
      expect(sync!.summary).toContain('git pull skipped (managed brain)');
    });
  }, 60_000);

  test('unmanaged brain: requested pull is honored (noPull stays false, no skip note)', async () => {
    await withEnv({ GBRAIN_HOME: emptyHome() }, async () => {
      await setManaged(false);

      const report = await runCycle(engine, { brainDir, phases: ['sync'], pull: true });
      const sync = report.phases.find((p) => p.phase === 'sync');
      expect(sync).toBeDefined();
      expect(sync!.status).toBe('ok');

      expect(performSyncCalls.length).toBe(1);
      expect(performSyncCalls[0].noPull).toBe(false);
      expect(sync!.details.pullSkipped).toBeUndefined();
      expect(sync!.summary).not.toContain('git pull skipped');
    });
  }, 60_000);

  test('unmanaged brain: no pull requested stays noPull (default cron-safe)', async () => {
    await withEnv({ GBRAIN_HOME: emptyHome() }, async () => {
      await setManaged(false);

      const report = await runCycle(engine, { brainDir, phases: ['sync'] });
      const sync = report.phases.find((p) => p.phase === 'sync');
      expect(sync).toBeDefined();

      expect(performSyncCalls.length).toBe(1);
      expect(performSyncCalls[0].noPull).toBe(true);
      expect(sync!.details.pullSkipped).toBeUndefined();
    });
  }, 60_000);
});
