/**
 * `graph_bulk_info` MCP op + engine coverage.
 *
 * Spec: docs/superpowers/specs/2026-07-08-gbrain-frontend-unification-design.md §6.2.
 * Hard prerequisite for gbrain-portal /graph MVP — batch metadata fetch so the
 * portal doesn't N+1-call `get_page` per node during subgraph rendering.
 *
 * Contract under test:
 *   - op accepts { slugs: string[] }, caps at 500 (BULK_INFO_CAP).
 *   - returns { [slug]: { title, type, tags, updated_at } } — NO content/chunks/compiled_truth.
 *   - unknown slugs are skipped silently (not present in the result map).
 *   - soft-deleted pages are excluded (deleted_at IS NULL).
 *   - source-scoped via ctx.sourceId / federated allowedSources.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from 'bun:test';
import { operations, operationsByName } from '../src/core/operations.ts';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import type { OperationContext } from '../src/core/operations.ts';
import { resetPgliteState } from './helpers/reset-pglite.ts';

let engine: PGLiteEngine;

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
}, 60_000);

afterAll(async () => {
  if (engine) await engine.disconnect();
}, 60_000);

function makeCtx(opts: { remote?: boolean; sourceId?: string } = {}): OperationContext {
  return {
    engine,
    remote: opts.remote ?? false,
    config: {},
    logger: console,
    dryRun: false,
    sourceId: opts.sourceId,
  } as unknown as OperationContext;
}

describe('graph_bulk_info — op declaration', () => {
  test('registered in the operations array', () => {
    const op = operations.find((o) => o.name === 'graph_bulk_info');
    expect(op).toBeDefined();
  });

  test('findable via operationsByName', () => {
    expect(operationsByName['graph_bulk_info']).toBeDefined();
    expect(operationsByName['graph_bulk_info'].name).toBe('graph_bulk_info');
  });

  test('scope is read; localOnly not truthy (HTTP-MCP accessible)', () => {
    const op = operationsByName['graph_bulk_info'];
    expect(op.scope).toBe('read');
    expect(op.localOnly).not.toBe(true);
  });

  test('declares a required slugs array param', () => {
    const op = operationsByName['graph_bulk_info'];
    expect(op.params.slugs).toBeDefined();
    expect(op.params.slugs.type).toBe('array');
    expect(op.params.slugs.required).toBe(true);
  });
});

describe('graph_bulk_info — handler behavior', () => {
  beforeEach(async () => {
    await resetPgliteState(engine);
    await engine.putPage('people/alice', {
      type: 'person',
      title: 'Alice',
      compiled_truth: 'SHOULD NEVER BE RETURNED',
      timeline: '',
    });
    await engine.putPage('companies/acme', {
      type: 'company',
      title: 'Acme',
      compiled_truth: 'secret content',
      timeline: '',
    });
    await engine.addTag('people/alice', 'vip');
    await engine.addTag('people/alice', 'founder');
  });

  test('returns minimal metadata for a batch of slugs', async () => {
    const op = operationsByName['graph_bulk_info'];
    const result = (await op.handler(makeCtx(), {
      slugs: ['people/alice', 'companies/acme'],
    })) as Record<string, unknown>;

    expect(Object.keys(result).sort()).toEqual(['companies/acme', 'people/alice']);

    const alice = result['people/alice'] as Record<string, unknown>;
    expect(alice.title).toBe('Alice');
    expect(alice.type).toBe('person');
    expect(Array.isArray(alice.tags)).toBe(true);
    expect((alice.tags as string[]).sort()).toEqual(['founder', 'vip']);
    expect(alice.updated_at).toBeDefined();
    // Privacy/size contract: NO heavy payload fields.
    expect(alice).not.toHaveProperty('compiled_truth');
    expect(alice).not.toHaveProperty('content');
    expect(alice).not.toHaveProperty('chunks');
    expect(alice).not.toHaveProperty('timeline');
    expect(alice).not.toHaveProperty('frontmatter');
  });

  test('skips unknown slugs silently', async () => {
    const op = operationsByName['graph_bulk_info'];
    const result = (await op.handler(makeCtx(), {
      slugs: ['people/alice', 'does/not/exist'],
    })) as Record<string, unknown>;
    expect(Object.keys(result)).toEqual(['people/alice']);
    expect(result['does/not/exist']).toBeUndefined();
  });

  test('empty slugs array returns empty object', async () => {
    const op = operationsByName['graph_bulk_info'];
    const result = (await op.handler(makeCtx(), { slugs: [] })) as Record<string, unknown>;
    expect(result).toEqual({});
  });

  test('rejects batch exceeding the 500 cap', async () => {
    const op = operationsByName['graph_bulk_info'];
    const big = Array(501).fill('people/alice');
    await expect(op.handler(makeCtx(), { slugs: big })).rejects.toThrow(/500/);
  });

  test('excludes soft-deleted pages', async () => {
    await engine.softDeletePage('people/alice');
    const op = operationsByName['graph_bulk_info'];
    const result = (await op.handler(makeCtx(), {
      slugs: ['people/alice', 'companies/acme'],
    })) as Record<string, unknown>;
    // alice is soft-deleted → excluded; acme remains.
    expect(Object.keys(result)).toEqual(['companies/acme']);
  });

  test('honors source scope (federated multi-source)', async () => {
    // Seed the SAME slug in a second source with a distinct title.
    await engine.executeRaw(
      `INSERT INTO sources (id, name) VALUES ('other-src', 'other-src') ON CONFLICT (id) DO NOTHING`,
    );
    await engine.executeRaw(
      `INSERT INTO pages (slug, source_id, type, title, compiled_truth, timeline)
       VALUES ('people/alice', 'other-src', 'person', 'Alice (other-src)', '', '')`,
    );

    const op = operationsByName['graph_bulk_info'];
    // Scoped to default source → sees the default-source title.
    const scoped = (await op.handler(makeCtx({ sourceId: 'default' }), {
      slugs: ['people/alice'],
    })) as Record<string, { title: string }>;
    expect(scoped['people/alice'].title).toBe('Alice');

    // Scoped to other-src → sees the other-source title.
    const other = (await op.handler(makeCtx({ sourceId: 'other-src' }), {
      slugs: ['people/alice'],
    })) as Record<string, { title: string }>;
    expect(other['people/alice'].title).toBe('Alice (other-src)');
  });
});

describe('getBulkPageMeta — engine method', () => {
  beforeEach(async () => {
    await resetPgliteState(engine);
    await engine.putPage('wiki/alpha', { type: 'note', title: 'Alpha', compiled_truth: '', timeline: '' });
    await engine.putPage('wiki/beta', { type: 'note', title: 'Beta', compiled_truth: '', timeline: '' });
  });

  test('returns a slug-keyed map of metadata', async () => {
    const map = await engine.getBulkPageMeta(['wiki/alpha', 'wiki/beta', 'wiki/missing']);
    expect(map.size).toBe(2);
    expect(map.has('wiki/alpha')).toBe(true);
    expect(map.has('wiki/beta')).toBe(true);
    expect(map.has('wiki/missing')).toBe(false);
    const alpha = map.get('wiki/alpha');
    expect(alpha?.title).toBe('Alpha');
    expect(alpha?.type).toBe('note');
    expect(Array.isArray(alpha?.tags)).toBe(true);
    expect(typeof alpha?.updated_at).toBe('string');
  });

  test('empty input returns empty map', async () => {
    const map = await engine.getBulkPageMeta([]);
    expect(map.size).toBe(0);
  });
});
