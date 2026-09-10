/**
 * Soft-deleted pages must not leak through the link API / portal.
 *
 * getLinks / getBacklinks (federated, scoped, and unscoped) and
 * traverseGraph / traversePaths hide pages with deleted_at set. restore_page
 * brings them back. The origin LEFT JOIN is intentionally unfiltered.
 *
 * Negative contract: getPage({includeDeleted:true}) / listPages recovery
 * window, unscoped cross-source live edges (reconcileLinks / back-link
 * validators), and purgeDeletedPages are unchanged.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from 'bun:test';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import { resetPgliteState } from './helpers/reset-pglite.ts';
import { backLinkValidator } from '../src/core/output/validators/back-link.ts';

let engine: PGLiteEngine;

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
});

afterAll(async () => {
  await engine.disconnect();
});

beforeEach(async () => {
  await resetPgliteState(engine);
});

const page = (title: string) => ({
  type: 'note' as const,
  title,
  compiled_truth: `Content of ${title}`,
  timeline: '',
  frontmatter: {},
});

async function seedLinkedPair(): Promise<void> {
  await engine.putPage('people/alice-example', page('Alice Example'));
  await engine.putPage('companies/acme-example', page('Acme Example'));
  await engine.addLink(
    'people/alice-example',
    'companies/acme-example',
    'works at',
    'works_at',
  );
}

const SCOPES: Array<{ label: string; opts?: { sourceId?: string; sourceIds?: string[] } }> = [
  { label: 'unscoped', opts: undefined },
  { label: 'scoped', opts: { sourceId: 'default' } },
  { label: 'federated', opts: { sourceIds: ['default'] } },
];

describe('getLinks / getBacklinks hide soft-deleted endpoints', () => {
  for (const { label, opts } of SCOPES) {
    test(`${label}: soft-delete target drops outbound + inbound; restore returns them`, async () => {
      await seedLinkedPair();

      const beforeOut = await engine.getLinks('people/alice-example', opts);
      const beforeIn = await engine.getBacklinks('companies/acme-example', opts);
      expect(beforeOut.map((l) => l.to_slug)).toContain('companies/acme-example');
      expect(beforeIn.map((l) => l.from_slug)).toContain('people/alice-example');

      await engine.softDeletePage('companies/acme-example');

      const afterOut = await engine.getLinks('people/alice-example', opts);
      const afterIn = await engine.getBacklinks('companies/acme-example', opts);
      expect(afterOut.map((l) => l.to_slug)).not.toContain('companies/acme-example');
      expect(afterIn.map((l) => l.from_slug)).not.toContain('people/alice-example');

      expect(await engine.restorePage('companies/acme-example')).toBe(true);
      const restoredOut = await engine.getLinks('people/alice-example', opts);
      const restoredIn = await engine.getBacklinks('companies/acme-example', opts);
      expect(restoredOut.map((l) => l.to_slug)).toContain('companies/acme-example');
      expect(restoredIn.map((l) => l.from_slug)).toContain('people/alice-example');
    });

    test(`${label}: soft-delete source drops outbound + inbound; restore returns them`, async () => {
      await seedLinkedPair();
      await engine.softDeletePage('people/alice-example');

      expect(await engine.getLinks('people/alice-example', opts)).toEqual([]);
      const inbound = await engine.getBacklinks('companies/acme-example', opts);
      expect(inbound.map((l) => l.from_slug)).not.toContain('people/alice-example');

      expect(await engine.restorePage('people/alice-example')).toBe(true);
      expect((await engine.getLinks('people/alice-example', opts)).map((l) => l.to_slug))
        .toContain('companies/acme-example');
    });
  }

  test('origin LEFT JOIN is unfiltered: deleted origin still surfaces origin_slug', async () => {
    await engine.putPage('people/alice-example', page('Alice Example'));
    await engine.putPage('companies/acme-example', page('Acme Example'));
    await engine.putPage('meetings/2026-04-03', page('Meeting'));
    await engine.addLink(
      'people/alice-example',
      'companies/acme-example',
      'mentioned',
      'mentions',
      'markdown',
      'meetings/2026-04-03',
      'body',
    );

    await engine.softDeletePage('meetings/2026-04-03');
    const links = await engine.getLinks('people/alice-example', { sourceId: 'default' });
    expect(links).toHaveLength(1);
    expect(links[0].to_slug).toBe('companies/acme-example');
    expect(links[0].origin_slug).toBe('meetings/2026-04-03');
  });
});

describe('traverseGraph / traversePaths hide soft-deleted nodes', () => {
  async function seedChain(): Promise<void> {
    await engine.putPage('people/alice-example', page('Alice Example'));
    await engine.putPage('companies/acme-example', page('Acme Example'));
    await engine.putPage('companies/widget-co', page('Widget Co'));
    await engine.addLink('people/alice-example', 'companies/acme-example', 'works at', 'works_at');
    await engine.addLink('companies/acme-example', 'companies/widget-co', 'partner', 'partners_with');
  }

  for (const { label, opts } of SCOPES) {
    test(`${label}: soft-delete mid node drops it from traverse; restore returns it`, async () => {
      await seedChain();
      const traverseOpts = opts ? { ...opts } : undefined;

      const before = await engine.traverseGraph('people/alice-example', 2, traverseOpts);
      expect(before.map((n) => n.slug)).toEqual(expect.arrayContaining([
        'people/alice-example',
        'companies/acme-example',
        'companies/widget-co',
      ]));

      await engine.softDeletePage('companies/acme-example');
      const after = await engine.traverseGraph('people/alice-example', 2, traverseOpts);
      const afterSlugs = after.map((n) => n.slug);
      expect(afterSlugs).toContain('people/alice-example');
      expect(afterSlugs).not.toContain('companies/acme-example');
      // Walk cannot hop through a deleted mid-node to reach widget-co.
      expect(afterSlugs).not.toContain('companies/widget-co');
      const seed = after.find((n) => n.slug === 'people/alice-example');
      expect(seed?.links.map((l) => l.to_slug) ?? []).not.toContain('companies/acme-example');

      expect(await engine.restorePage('companies/acme-example')).toBe(true);
      const restored = await engine.traverseGraph('people/alice-example', 2, traverseOpts);
      expect(restored.map((n) => n.slug)).toEqual(expect.arrayContaining([
        'people/alice-example',
        'companies/acme-example',
        'companies/widget-co',
      ]));
    });

    test(`${label}: traversePaths out/in/both drop edges to a soft-deleted neighbor`, async () => {
      await seedChain();
      const pathOpts = { depth: 2, ...(opts ?? {}) };

      const beforeOut = await engine.traversePaths('people/alice-example', { ...pathOpts, direction: 'out' });
      expect(beforeOut.some((e) => e.to_slug === 'companies/acme-example')).toBe(true);

      await engine.softDeletePage('companies/acme-example');
      const afterOut = await engine.traversePaths('people/alice-example', { ...pathOpts, direction: 'out' });
      expect(afterOut.some((e) => e.to_slug === 'companies/acme-example')).toBe(false);
      expect(afterOut.some((e) => e.to_slug === 'companies/widget-co')).toBe(false);

      const afterIn = await engine.traversePaths('companies/widget-co', { ...pathOpts, direction: 'in' });
      expect(afterIn.some((e) => e.from_slug === 'companies/acme-example')).toBe(false);

      const afterBoth = await engine.traversePaths('people/alice-example', { ...pathOpts, direction: 'both' });
      expect(afterBoth.some((e) =>
        e.from_slug === 'companies/acme-example' || e.to_slug === 'companies/acme-example',
      )).toBe(false);

      expect(await engine.restorePage('companies/acme-example')).toBe(true);
      const restoredOut = await engine.traversePaths('people/alice-example', { ...pathOpts, direction: 'out' });
      expect(restoredOut.some((e) => e.to_slug === 'companies/acme-example')).toBe(true);
    });
  }

  test('traverseGraph of a soft-deleted seed returns empty (not the tombstone node)', async () => {
    await seedChain();
    await engine.softDeletePage('people/alice-example');
    expect(await engine.traverseGraph('people/alice-example', 2)).toEqual([]);
    expect(await engine.restorePage('people/alice-example')).toBe(true);
    const restored = await engine.traverseGraph('people/alice-example', 2);
    expect(restored.map((n) => n.slug)).toContain('people/alice-example');
  });
});

describe('negative: recovery window, unscoped cross-source, purge', () => {
  test('getPage(includeDeleted) and listPages recovery semantics are unchanged', async () => {
    await engine.putPage('people/alice-example', page('Alice Example'));
    await engine.softDeletePage('people/alice-example');

    expect(await engine.getPage('people/alice-example')).toBeNull();
    const surfaced = await engine.getPage('people/alice-example', { includeDeleted: true });
    expect(surfaced).not.toBeNull();
    expect(surfaced!.deleted_at).toBeInstanceOf(Date);

    const listed = await engine.listPages({ limit: 100 });
    expect(listed.map((p) => p.slug)).not.toContain('people/alice-example');
    const listedDeleted = await engine.listPages({ limit: 100, includeDeleted: true });
    expect(listedDeleted.map((p) => p.slug)).toContain('people/alice-example');

    expect(await engine.restorePage('people/alice-example')).toBe(true);
    expect(await engine.getPage('people/alice-example')).not.toBeNull();
  });

  test('unscoped getLinks still returns live cross-source edges (reconcileLinks contract)', async () => {
    await engine.executeRaw(
      `INSERT INTO sources (id, name) VALUES ('wiki', 'wiki') ON CONFLICT DO NOTHING`,
    );
    await engine.putPage('people/alice-example', page('Alice Example'));
    await engine.putPage('people/bob-example', page('Bob Example'), { sourceId: 'wiki' });
    await engine.addLink(
      'people/alice-example',
      'people/bob-example',
      'knows',
      'knows',
      'markdown',
      undefined,
      undefined,
      { fromSourceId: 'default', toSourceId: 'wiki' },
    );

    const unscoped = await engine.getLinks('people/alice-example');
    expect(unscoped.map((l) => l.to_slug)).toContain('people/bob-example');

    const scopedDefault = await engine.getLinks('people/alice-example', { sourceId: 'default' });
    expect(scopedDefault.map((l) => l.to_slug)).toContain('people/bob-example');

    await engine.softDeletePage('people/bob-example', { sourceId: 'wiki' });
    expect((await engine.getLinks('people/alice-example')).map((l) => l.to_slug))
      .not.toContain('people/bob-example');

    expect(await engine.restorePage('people/bob-example', { sourceId: 'wiki' })).toBe(true);
    expect((await engine.getLinks('people/alice-example')).map((l) => l.to_slug))
      .toContain('people/bob-example');
  });

  test('back-link validator still sees live unscoped reverse edges', async () => {
    await engine.putPage('people/alice-example', page('Alice Example'));
    await engine.putPage('people/bob-example', page('Bob Example'));
    await engine.addLink('people/alice-example', 'people/bob-example', 'knows', 'knows');
    await engine.addLink('people/bob-example', 'people/alice-example', 'knows', 'knows');

    const findings = await backLinkValidator.validate({
      slug: 'people/alice-example',
      type: 'person',
      compiledTruth: 'x',
      timeline: '',
      frontmatter: {},
      engine,
    });
    expect(findings).toEqual([]);
  });

  test('purgeDeletedPages still hard-deletes past the recovery window', async () => {
    await engine.putPage('people/alice-example', page('Alice Example'));
    await engine.putPage('people/bob-example', page('Bob Example'));
    await engine.softDeletePage('people/alice-example');
    await engine.executeRaw(
      `UPDATE pages SET deleted_at = now() - INTERVAL '73 hours' WHERE slug = $1`,
      ['people/alice-example'],
    );

    const result = await engine.purgeDeletedPages(72);
    expect(result.slugs).toContain('people/alice-example');
    expect(result.slugs).not.toContain('people/bob-example');
    const remaining = await engine.executeRaw<{ slug: string }>(
      `SELECT slug FROM pages WHERE slug = ANY($1::text[])`,
      [['people/alice-example', 'people/bob-example']],
    );
    const slugs = remaining.map((r) => r.slug);
    expect(slugs).not.toContain('people/alice-example');
    expect(slugs).toContain('people/bob-example');
  });
});
