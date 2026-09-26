/**
 * Managed brain phase skips (fork #5180/#5203): the legacy `lint fix` path
 * writes through the filesystem guard a managed brain refuses
 * (`writer_coordinator_required`), which used to fail the whole per-source
 * lane (healthy cycles reported `partial` forever). The fork restores the
 * skip layer on top of 0.58: the phase reports `skipped` with the reason
 * instead of `fail`. Dry-run and unmanaged lanes are unchanged.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, test } from 'bun:test';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import { resetPgliteState } from './helpers/reset-pglite.ts';

const { runPhaseLint } = await import('../src/core/cycle.ts');

let engine: PGLiteEngine;
let brainDir: string;

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
  brainDir = mkdtempSync(join(tmpdir(), 'gbrain-managed-phase-skip-'));
  writeFileSync(join(brainDir, '.gitkeep'), '');
}, 120_000);

afterAll(async () => {
  await engine.disconnect();
  rmSync(brainDir, { recursive: true, force: true });
});

beforeEach(async () => {
  await resetPgliteState(engine);
});

async function setManaged(enabled: boolean): Promise<void> {
  await engine.executeRaw('UPDATE persistence_brain SET enabled=$1 WHERE singleton=1', [enabled]);
}

describe('cycle lint phase on a managed brain', () => {
  test('managed brain: lint fix reports skipped with writer_coordinator_required', async () => {
    await setManaged(true);
    const result = await runPhaseLint(brainDir, false, engine);
    expect(result.status).toBe('skipped');
    expect(result.details?.reason).toBe('writer_coordinator_required');
    expect(result.summary).toContain('lint fix skipped');
  });

  test('managed brain: dry-run lane is not skipped', async () => {
    await setManaged(true);
    const result = await runPhaseLint(brainDir, true, engine);
    expect(result.status).not.toBe('skipped');
  });

  test('unmanaged brain: lint fix runs (not skipped)', async () => {
    await setManaged(false);
    const result = await runPhaseLint(brainDir, false, engine);
    expect(result.status).not.toBe('skipped');
  });
});
