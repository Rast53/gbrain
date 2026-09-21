/**
 * Autopilot pull policy on a MANAGED brain (persistence_brain.enabled).
 *
 * A managed brain routes sync through the persistence coordinator, which
 * refuses a git pull outside an explicit drained maintenance window
 * (`Managed sync requires --no-pull`, src/core/persistence/sync-discovery.ts).
 * An automatic `autopilot-cycle` job that carries pull=true therefore fails the
 * cycle's sync phase with `writer_coordinator_required`.
 *
 * The per-source fan-out must dispatch pull=false on a managed brain while an
 * unmanaged brain keeps the remote-url decision (`sourceConfigHasRemoteUrl`).
 *
 * No module mock: `isManagedBrain` is a one-row `persistence_brain.enabled`
 * read, so the stub engine's `executeRaw` drives the real decision path.
 */
import { describe, test, expect } from 'bun:test';
import { dispatchPerSource } from '../src/commands/autopilot-fanout.ts';
import type { BrainEngine, SourceRow } from '../src/core/engine.ts';

function src(id: string, config: Record<string, unknown> = {}): SourceRow {
  return {
    id,
    name: null,
    local_path: `/tmp/${id}`,
    last_sync_at: null,
    config,
  } as unknown as SourceRow;
}

const REMOTE = { remote_url: 'https://github.com/example/repo' };

function makeStubs(sources: SourceRow[], managed: boolean) {
  const added: Array<{ name: string; data: Record<string, unknown>; opts: Record<string, unknown> }> = [];
  let nextId = 100;
  const engine = {
    kind: 'postgres' as const,
    listAllSources: async () => sources,
    executeRaw: async (sql: string) => {
      if (sql.includes('persistence_brain')) return [{ enabled: managed }];
      return [];
    },
  } as unknown as BrainEngine;
  const queue = {
    add: async (name: string, data: Record<string, unknown>, opts: Record<string, unknown>) => {
      added.push({ name, data, opts });
      return { id: nextId++ };
    },
  } as unknown as Parameters<typeof dispatchPerSource>[1];
  const fanoutOpts = {
    repoPath: '/tmp/brain',
    slot: '2026-06-01T12:00:00.000Z',
    timeoutMs: 600_000,
    fanoutMax: 4,
    jsonMode: true,
    emit: (_line: string) => {},
    log: (_line: string) => {},
    pathExists: (_path: string) => true,
  };
  return { engine, queue, added, fanoutOpts };
}

describe('autopilot fan-out pull policy on a managed brain', () => {
  test('managed brain: remote-url source dispatches autopilot-cycle with pull=false', async () => {
    const { engine, queue, added, fanoutOpts } = makeStubs([src('alpha', REMOTE)], true);
    await dispatchPerSource(engine, queue, fanoutOpts);

    expect(added.length).toBe(1);
    expect(added[0].name).toBe('autopilot-cycle');
    expect((added[0].data as Record<string, unknown>).source_id).toBe('alpha');
    expect((added[0].data as Record<string, unknown>).pull).toBe(false);
  });

  test('managed brain: local-only source also dispatches pull=false', async () => {
    const { engine, queue, added, fanoutOpts } = makeStubs([src('local')], true);
    await dispatchPerSource(engine, queue, fanoutOpts);

    expect(added.length).toBe(1);
    expect((added[0].data as Record<string, unknown>).pull).toBe(false);
  });

  test('unmanaged brain: remote-url source keeps pull=true (sourceConfigHasRemoteUrl)', async () => {
    const { engine, queue, added, fanoutOpts } = makeStubs([src('alpha', REMOTE)], false);
    await dispatchPerSource(engine, queue, fanoutOpts);

    expect(added.length).toBe(1);
    expect((added[0].data as Record<string, unknown>).pull).toBe(true);
  });

  test('unmanaged brain: local-only source keeps pull=false (no remote)', async () => {
    const { engine, queue, added, fanoutOpts } = makeStubs([src('local')], false);
    await dispatchPerSource(engine, queue, fanoutOpts);

    expect(added.length).toBe(1);
    expect((added[0].data as Record<string, unknown>).pull).toBe(false);
  });
});
