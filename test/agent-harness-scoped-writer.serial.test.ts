/**
 * Regression guard for the codex-plugin door surface ORACLE.
 *
 * The plugin door (test/e2e/codex-plugin-install-real.serial.test.ts) pins the
 * spawned serve's `tools/list` against the declared starter surface. But
 * `stdioVisibleTools` fail-closes every `requiredScopes` op against the stdio
 * local writer's grant, and `seedBrainForAgent`'s manual seed never runs
 * `gbrain init` (which is what normally registers the stdio writer). Without a
 * capable writer the five scoped starter ops — put_skill, delete_skill,
 * join_brain, sync_brain_skills, leave_brain — vanish from the oracle and the
 * nightly heavy run goes red.
 *
 * `seedBrainForAgent(..., { registerStdioWriter: true })` registers the
 * complete fixture grant against the SAME seeded brain, so the oracle stays
 * unchanged. Neither test touches src/mcp/server.ts: the first pins that a
 * plain seed STILL fail-closes the scoped ops, the second pins that the
 * explicit fixture grant exposes the full declared surface.
 */
import { describe, test, expect } from 'bun:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { seedBrainForAgent } from './helpers/agent-harness.ts';
import { withEnv } from './helpers/with-env.ts';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import { stdioVisibleTools } from '../src/mcp/server.ts';
import { operations } from '../src/core/operations.ts';
import { filterOpsForSurface } from '../src/mcp/surface.ts';
import { readLocalWriter } from '../src/core/persistence/identity.ts';

/** The five scoped ops `stdioVisibleTools` fail-closes out of a bare seed's list. */
const SCOPED_STARTER_OPS = ['put_skill', 'delete_skill', 'join_brain', 'sync_brain_skills', 'leave_brain'];
const starterOps = filterOpsForSurface(operations, 'starter');

function homeEnv(home: string): Record<string, string | undefined> {
  return { GBRAIN_HOME: home, HOME: home, DATABASE_URL: undefined, GBRAIN_DATABASE_URL: undefined };
}

/** Open the seeded brain and return the stdio-visible starter tool names. */
async function visibleStarterTools(home: string): Promise<string[]> {
  const engine = new PGLiteEngine();
  await engine.connect({ engine: 'pglite', database_path: join(home, '.gbrain', 'brain.pglite') });
  try {
    return (await stdioVisibleTools(engine, starterOps)).map((op) => op.name);
  } finally {
    await engine.disconnect();
  }
}

describe('seedBrainForAgent stdio writer (codex-plugin door surface)', () => {
  test('a plain seed fail-closes the five scoped starter ops out of tools/list', async () => {
    const home = mkdtempSync(join(tmpdir(), 'gb-seed-scoped-'));
    try {
      await withEnv(homeEnv(home), async () => {
        await seedBrainForAgent(home, 'workspace');
      });
      const names = await withEnv(homeEnv(home), () => visibleStarterTools(home));
      for (const op of SCOPED_STARTER_OPS) expect(names).not.toContain(op);
    } finally {
      rmSync(home, { recursive: true, force: true });
    }
  }, 60_000);

  test('registerStdioWriter:true exposes the complete declared starter surface', async () => {
    const home = mkdtempSync(join(tmpdir(), 'gb-seed-scoped-'));
    try {
      await withEnv(homeEnv(home), async () => {
        await seedBrainForAgent(home, 'workspace', { registerStdioWriter: true });
      });
      const names = await withEnv(homeEnv(home), async () => {
        const engine = new PGLiteEngine();
        await engine.connect({ engine: 'pglite', database_path: join(home, '.gbrain', 'brain.pglite') });
        try {
          // The seed published a verifiable stdio-lane credential, not a bare file.
          const writer = await readLocalWriter(engine, 'stdio');
          expect(writer.lane).toBe('stdio');
          return (await stdioVisibleTools(engine, starterOps)).map((op) => op.name);
        } finally {
          await engine.disconnect();
        }
      });
      for (const op of SCOPED_STARTER_OPS) expect(names).toContain(op);
      expect(names.slice().sort()).toEqual(starterOps.map((op) => op.name).sort());
    } finally {
      rmSync(home, { recursive: true, force: true });
    }
  }, 60_000);
});
