/**
 * Write-path auto-link / auto-timeline for trusted vs remote put_page.
 *
 * Trusted local writers (capture CLI, ctx.remote === false) extract links and
 * timeline entries in the same put_page call. Untrusted remote MCP writers
 * still cannot plant graph edges — auto_links is skipped with {skipped:'remote'}.
 * The daily extract --stale / extract timeline --source db sweep remains the
 * backup for remote writes (see extract-db / extract-stale).
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from 'bun:test';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import { resetPgliteState } from './helpers/reset-pglite.ts';
import { operationsByName } from '../src/core/operations.ts';
import type { OperationContext } from '../src/core/operations.ts';

let engine: PGLiteEngine;

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
}, 60_000);

afterAll(async () => {
  await engine.disconnect();
}, 60_000);

beforeEach(async () => {
  await resetPgliteState(engine);
  await engine.setConfig('auto_link', 'true');
  await engine.setConfig('auto_timeline', 'true');
});

function makeCtx(overrides: Partial<OperationContext> = {}): OperationContext {
  return {
    engine,
    config: { engine: 'pglite' } as any,
    logger: { info: () => {}, warn: () => {}, error: () => {} },
    dryRun: false,
    remote: false,
    sourceId: 'default',
    ...overrides,
  };
}

function timelineDates(slug: string): Promise<string[]> {
  return engine.getTimeline(slug).then(entries =>
    entries.map(e => isoDay(e.date)).sort(),
  );
}

function isoDay(d: unknown): string {
  if (d instanceof Date) return d.toISOString().slice(0, 10);
  const s = String(d);
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  const parsed = new Date(s);
  return Number.isNaN(parsed.getTime()) ? s.slice(0, 10) : parsed.toISOString().slice(0, 10);
}

const PROBE_PAGE = `---
type: note
title: Write-path extract probe
---

Mentions [Alice](people/alice).

<!-- timeline -->

## Timeline

- 2026-09-11: probe event from capture CLI
`;

describe('put_page write-path extract', () => {
  test('trusted write extracts links immediately (not via cron)', async () => {
    await engine.putPage('people/alice', { type: 'person', title: 'Alice', compiled_truth: '', timeline: '' });
    const result = await operationsByName['put_page'].handler(makeCtx(), {
      slug: 'test/write-path-extract-probe',
      content: PROBE_PAGE,
    });
    expect((result as any).auto_links).toBeDefined();
    expect((result as any).auto_links.skipped).toBeUndefined();
    expect((result as any).auto_links.created).toBeGreaterThan(0);
    const links = await engine.getLinks('test/write-path-extract-probe');
    expect(links.length).toBeGreaterThan(0);
    expect(links.some(l => l.to_slug === 'people/alice')).toBe(true);
  });

  test('trusted write extracts unbolded timeline-column bullets in the same call', async () => {
    const result = await operationsByName['put_page'].handler(makeCtx(), {
      slug: 'test/write-path-extract-probe',
      content: PROBE_PAGE,
    });
    expect((result as any).auto_timeline).toEqual({ created: 1 });
    expect(await timelineDates('test/write-path-extract-probe')).toEqual(['2026-09-11']);
  });

  test('remote MCP put_page skips auto_links (security gate)', async () => {
    await engine.putPage('people/alice', { type: 'person', title: 'Alice', compiled_truth: '', timeline: '' });
    const result = await operationsByName['put_page'].handler(makeCtx({ remote: true }), {
      slug: 'test/write-path-extract-probe',
      content: PROBE_PAGE,
    });
    expect((result as any).auto_links).toEqual({ skipped: 'remote' });
    expect((result as any).auto_timeline).toEqual({ skipped: 'remote' });
    expect(await engine.getLinks('test/write-path-extract-probe')).toEqual([]);
    expect(await engine.getTimeline('test/write-path-extract-probe')).toEqual([]);
  });

  test('auto_timeline=false trusted write does not extract timeline', async () => {
    await engine.setConfig('auto_timeline', 'false');
    const result = await operationsByName['put_page'].handler(makeCtx(), {
      slug: 'test/write-path-extract-probe',
      content: PROBE_PAGE,
    });
    expect((result as any).auto_timeline).toBeUndefined();
    expect(await engine.getTimeline('test/write-path-extract-probe')).toEqual([]);
  });

  test('trusted write threads source_id so non-default pages get timeline rows', async () => {
    await engine.executeRaw(
      `INSERT INTO sources (id, name) VALUES ('wiki', 'wiki') ON CONFLICT (id) DO NOTHING`,
    );
    const result = await operationsByName['put_page'].handler(makeCtx({ sourceId: 'wiki' }), {
      slug: 'test/write-path-extract-probe',
      content: PROBE_PAGE,
    });
    expect((result as any).auto_timeline).toEqual({ created: 1 });
    const rows = await engine.executeRaw<{ n: string }>(
      `SELECT COUNT(*)::text AS n FROM timeline_entries t
       JOIN pages p ON p.id = t.page_id
       WHERE p.slug = 'test/write-path-extract-probe' AND p.source_id = 'wiki'`,
    );
    expect(Number(rows[0]?.n ?? 0)).toBe(1);
  });
});
