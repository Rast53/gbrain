/**
 * v0.31 — Dream-cycle `consolidate` phase: facts → takes promotion.
 *
 * Per /plan-eng-review Phase 5:
 *
 *   For each (source_id, entity_slug) bucket of unconsolidated active facts:
 *     1. Skip if count < 3 OR oldest fact age < 24h.
 *     2. Cluster by embedding cosine — greedy threshold 0.85.
 *     3. For each cluster ≥ 2: pick the highest-confidence fact's text as
 *        the take claim (v0.31 ships without LLM synthesis to keep the
 *        cycle deterministic; see TODO at the bottom for the v0.32 Sonnet
 *        rewrite).
 *     4. Resolve entity_slug → pages.slug via resolveEntityPageId() with
 *        fallback chain: exact → slugify-variants → fuzzy (config-gated) →
 *        cross-source fallback (opt-in allowlist). Page must exist in the
 *        bucket's source (or an allowed fallback source); no auto-creation.
 *     5. INSERT into takes(kind='fact', holder='self', source=concatenated
 *        source_sessions). row_num = MAX existing for the page + 1.
 *     6. UPDATE contributing facts: consolidated_at = now() +
 *        consolidated_into = takes.id. NEVER DELETE.
 *
 * The phase's totals contribute to the runCycle CycleReport via
 * extractTotals (cycle.ts) — facts_consolidated + takes_written.
 */

import type { BrainEngine, FactRow } from '../../engine.ts';
import type { PhaseResult } from '../../cycle.ts';
import { cosineSimilarity } from '../../facts/classify.ts';
import { isAborted } from '../../abort-check.ts';

export interface ConsolidatePhaseOpts {
  dryRun?: boolean;
  /** In-phase keepalive callback. Awaited between buckets. */
  yieldDuringPhase?: () => Promise<void>;
  /**
   * #1972: cooperative-abort signal. Checked at the top of the bucket loop so a
   * long consolidate relinquishes its worker slot well under the 30s
   * force-evict instead of running to completion after cancellation.
   */
  signal?: AbortSignal;
  /** Cosine cluster threshold. Default 0.85. */
  clusterThreshold?: number;
  /** Minimum facts per (source, entity) bucket before consolidation. Default 3. */
  minFactsPerBucket?: number;
  /** Minimum age (ms) of the OLDEST fact in a bucket before consolidation. Default 24h. */
  minOldestAgeMs?: number;
}

// ---------------------------------------------------------------------------
// Slug resolution helper
// ---------------------------------------------------------------------------

export interface ResolveResult {
  pageId: number;
  path: 'exact' | 'slugified' | 'fuzzy' | 'fallback_source';
}

export interface ResolutionPaths {
  exact: number;
  slugified: number;
  fuzzy: number;
  fallback_source: number;
  unresolved: string[];
}

/**
 * Resolve an entity_slug to a page_id within a source-scoped chain.
 *
 * Chain:
 *   1. Exact match: pages.source_id = sourceId AND pages.slug = entitySlug
 *   2. Slugify-variants:
 *      a. First dash → slash (e.g. "projects-gbrain" → "projects/gbrain")
 *      b. Slash → dash (reverse, for "projects/gbrain" → "projects-gbrain")
 *      c. Lower-case canonical (already lower-case; included for consistency)
 *   3. Fuzzy match (gated by `consolidate.resolve_fuzzy_enabled`, default false).
 *      Uses pg_trgm similarity; requires unambiguous single result ≥ 0.7 score.
 *   4. Cross-source fallback: if the primary source didn't resolve AND
 *      `consolidate.resolve_fallback_sources` maps sourceId → [target sources],
 *      try exact + slugify-variants in each allowed fallback source.
 *
 * All steps 1–3 are strictly within `sourceId`. Cross-source fallback (step 4)
 * is opt-in only and logged. No global/source-unfiltered search ever.
 *
 * Returns null when no page is resolved.
 */
export async function resolveEntityPageId(
  engine: BrainEngine,
  sourceId: string,
  entitySlug: string,
  pathCounter: Pick<ResolutionPaths, 'exact' | 'slugified' | 'fuzzy' | 'fallback_source' | 'unresolved'>,
): Promise<number | null> {
  // ── 1. Exact match ───────────────────────────────────────────────────
  let pageId = await tryExactPage(engine, sourceId, entitySlug);
  if (pageId !== null) { pathCounter.exact += 1; return pageId; }

  // ── 2. Slugify-variants ──────────────────────────────────────────────
  const variants = slugVariants(entitySlug);
  for (const variant of variants) {
    if (variant === entitySlug) continue;
    pageId = await tryExactPage(engine, sourceId, variant);
    if (pageId !== null) { pathCounter.slugified += 1; return pageId; }
  }

  // ── 3. Fuzzy match (config-gated) ────────────────────────────────────
  const fuzzyEnabledStr = await engine.getConfig('consolidate.resolve_fuzzy_enabled');
  const fuzzyEnabled = fuzzyEnabledStr === 'true' || fuzzyEnabledStr === '1';
  if (fuzzyEnabled) {
    pageId = await tryFuzzyPage(engine, sourceId, entitySlug);
    if (pageId !== null) { pathCounter.fuzzy += 1; return pageId; }
  }

  // ── 4. Cross-source fallback (opt-in allowlist) ──────────────────────
  const fallbackRaw = await engine.getConfig('consolidate.resolve_fallback_sources');
  if (fallbackRaw) {
    let fallbackMap: Record<string, string[]> | null = null;
    try { fallbackMap = JSON.parse(fallbackRaw); } catch { /* invalid JSON, skip */ }
    if (fallbackMap && fallbackMap[sourceId] && Array.isArray(fallbackMap[sourceId])) {
      for (const targetSource of fallbackMap[sourceId]!) {
        // Exact in fallback source
        pageId = await tryExactPage(engine, targetSource, entitySlug);
        if (pageId !== null) { pathCounter.fallback_source += 1; return pageId; }
        // Slugify-variants in fallback source
        for (const variant of variants) {
          if (variant === entitySlug) continue;
          pageId = await tryExactPage(engine, targetSource, variant);
          if (pageId !== null) { pathCounter.fallback_source += 1; return pageId; }
        }
      }
    }
  }

  // ── Unresolved ───────────────────────────────────────────────────────
  pathCounter.unresolved.push(`${sourceId}:${entitySlug}`);
  return null;
}

/**
 * Slugify-variants for the fallback chain: tries common transformations
 * between dash-slug and slash-slug forms.
 */
export function slugVariants(slug: string): string[] {
  const variants: string[] = [];
  const lower = slug.toLowerCase();

  // First dash → slash: "projects-gbrain" → "projects/gbrain"
  const dashIdx = slug.indexOf('-');
  if (dashIdx > 0 && dashIdx < slug.length - 1 && !slug.includes('/')) {
    const slashForm = slug.slice(0, dashIdx) + '/' + slug.slice(dashIdx + 1);
    variants.push(slashForm);
  }

  // First slash → dash: "projects/gbrain" → "projects-gbrain"
  const slashIdx = slug.indexOf('/');
  if (slashIdx > 0 && slashIdx < slug.length - 1) {
    const dashForm = slug.slice(0, slashIdx) + '-' + slug.slice(slashIdx + 1);
    variants.push(dashForm);
  }

  // Lower-cased (already applied via `lower`; include distinct form if original
  // wasn't already lower-case)
  if (lower !== slug) {
    // Also apply first-char transformations on the lower-cased variant
    const ldashIdx = lower.indexOf('-');
    if (ldashIdx > 0 && ldashIdx < lower.length - 1 && !lower.includes('/')) {
      variants.push(lower.slice(0, ldashIdx) + '/' + lower.slice(ldashIdx + 1));
    }
    if (!variants.includes(lower)) {
      variants.push(lower);
    }
  }

  // Deduplicate; keep order
  return [...new Set(variants)];
}

async function tryExactPage(
  engine: BrainEngine,
  sourceId: string,
  slug: string,
): Promise<number | null> {
  try {
    const rows = await engine.executeRaw<{ id: number }>(
      `SELECT id FROM pages WHERE source_id = $1 AND slug = $2 AND deleted_at IS NULL LIMIT 1`,
      [sourceId, slug],
    );
    if (rows.length > 0) return rows[0].id;
  } catch {
    // Defensive: don't crash on SQL error.
  }
  return null;
}

/**
 * Fuzzy page resolution using pg_trgm similarity. Returns page_id only when
 * there is exactly one unambiguous candidate with score ≥ 0.7. Ambiguous
 * results (multiple candidates above threshold) → null + logged.
 */
async function tryFuzzyPage(
  engine: BrainEngine,
  sourceId: string,
  entitySlug: string,
): Promise<number | null> {
  const lc = entitySlug.toLowerCase();
  const fragment = entitySlug.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/-+/g, '-').replace(/^-+|-+$/g, '');
  try {
    const rows = await engine.executeRaw<{ id: number; slug: string; title: string; score: number }>(
      `SELECT id, slug, title,
         GREATEST(
           similarity(lower(title), $2),
           similarity(slug, $3)
         ) AS score
       FROM pages
       WHERE source_id = $1
         AND deleted_at IS NULL
         AND (
           lower(title) % $2
           OR slug ILIKE '%' || $3 || '%'
         )
       ORDER BY score DESC, slug ASC
       LIMIT 5`,
      [sourceId, lc, fragment],
    );
    if (rows.length === 0) return null;
    // Ambiguous: multiple candidates above threshold → skip.
    if (rows.length >= 2 && rows[1].score >= 0.7) return null;
    if (rows[0].score >= 0.7) return rows[0].id;
  } catch {
    // pg_trgm may not be available; fall through.
  }
  return null;
}

// ---------------------------------------------------------------------------
// Phase entry point
// ---------------------------------------------------------------------------

export async function runPhaseConsolidate(
  engine: BrainEngine,
  opts: ConsolidatePhaseOpts = {},
): Promise<PhaseResult> {
  const dryRun = opts.dryRun === true;
  const threshold = opts.clusterThreshold ?? 0.85;
  const minPerBucket = opts.minFactsPerBucket ?? 3;
  const minOldestAgeMs = opts.minOldestAgeMs ?? 24 * 60 * 60 * 1000;

  let factsConsolidated = 0;
  let takesWritten = 0;
  let bucketsProcessed = 0;
  let bucketsSkipped = 0;

  const resolutionPaths: ResolutionPaths = {
    exact: 0, slugified: 0, fuzzy: 0, fallback_source: 0, unresolved: [],
  };

  // Pull every (source_id, entity_slug) bucket of unconsolidated facts.
  // Uses the partial idx_facts_unconsolidated index.
  let buckets: Array<{ source_id: string; entity_slug: string; count: number }>;
  try {
    buckets = await engine.executeRaw<{
      source_id: string; entity_slug: string; count: number;
    }>(`
      SELECT source_id, entity_slug, COUNT(*)::int AS count
      FROM facts
      WHERE consolidated_at IS NULL
        AND expired_at IS NULL
        AND entity_slug IS NOT NULL
      GROUP BY source_id, entity_slug
      HAVING COUNT(*) >= ${minPerBucket}
    `);
  } catch (err) {
    return {
      phase: 'consolidate',
      status: 'fail',
      duration_ms: 0,
      summary: 'failed to scan unconsolidated facts',
      details: { error: err instanceof Error ? err.message : String(err) },
      error: {
        class: 'ConsolidateScanFailed',
        code: 'consolidate_scan_failed',
        message: err instanceof Error ? err.message : String(err),
      },
    };
  }

  for (const b of buckets) {
    // #1972: bail at the top of the bucket loop on abort. Each prior bucket's
    // per-row INSERT/consolidate is already committed, so breaking returns a
    // valid partial envelope (the inner cluster loop is bounded at limit 100,
    // so no inner guard is needed).
    if (isAborted(opts.signal)) break;
    if (opts.yieldDuringPhase) {
      try { await opts.yieldDuringPhase(); } catch { /* keepalive errors non-fatal */ }
    }

    const facts = await engine.listFactsByEntity(b.source_id, b.entity_slug, {
      activeOnly: true,
      limit: 100,
    });
    // Re-filter to unconsolidated since listFactsByEntity returns all active.
    const unconsolidated = facts.filter(f => f.consolidated_at == null);
    if (unconsolidated.length < minPerBucket) {
      bucketsSkipped += 1;
      continue;
    }

    // Age gate: oldest must be at least minOldestAgeMs old.
    const oldest = unconsolidated.reduce((min, f) =>
      f.valid_from.getTime() < min.valid_from.getTime() ? f : min,
    );
    if (Date.now() - oldest.valid_from.getTime() < minOldestAgeMs) {
      bucketsSkipped += 1;
      continue;
    }

    bucketsProcessed += 1;
    const clusters = clusterFacts(unconsolidated, threshold);

    // Resolve entity_slug → page_id with fallback chain.
    const pageId = await resolveEntityPageId(engine, b.source_id, b.entity_slug, resolutionPaths);
    if (pageId === null) continue;

    // Existing row_num max for this page → start appending after it.
    const rowMaxRows = await engine.executeRaw<{ max: number }>(
      `SELECT COALESCE(MAX(row_num), 0)::int AS max FROM takes WHERE page_id = $1`,
      [pageId],
    );
    let nextRowNum = (rowMaxRows[0]?.max ?? 0) + 1;

    for (const cluster of clusters) {
      if (cluster.length < 2) continue;
      // Take selection: pick the highest-confidence fact's text as the
      // take claim (v0.31 deterministic). v0.32 will swap to a Sonnet
      // synthesis pass.
      const best = cluster.reduce((a, b) => (b.confidence > a.confidence ? b : a));
      const avgWeight = cluster.reduce((s, f) => s + f.confidence, 0) / cluster.length;
      const sources = Array.from(new Set(cluster.map(c => c.source_session ?? c.source).filter(Boolean))).join(',');
      const sinceISO = cluster
        .map(c => c.valid_from)
        .reduce((min, d) => (d < min ? d : min))
        .toISOString()
        .slice(0, 10);

      if (dryRun) {
        // Pretend we did it.
        takesWritten += 1;
        factsConsolidated += cluster.length;
        nextRowNum += 1;
        continue;
      }

      // v0.35.4 (D-CDX-4) — semantic upsert. The full dream cycle runs
      // `extract_facts` BEFORE `consolidate`; `extract_facts` hard-deletes
      // and re-inserts page facts via deleteFactsForPage + insertFacts,
      // which clears `consolidated_at` on every fact. Without this lookup,
      // a second cycle run would re-INSERT a duplicate take via
      // `MAX(row_num)+1`, silently poisoning trajectory + scorecard data.
      // Match on (page_id, claim, since_date) — the natural identity of a
      // promoted take.
      const existing = await engine.executeRaw<{ id: number }>(
        `SELECT id FROM takes
         WHERE page_id = $1 AND claim = $2 AND since_date = $3
         LIMIT 1`,
        [pageId, best.fact, sinceISO],
      );

      let takeId: number;
      if (existing.length > 0) {
        // Re-promotion of a cluster we already wrote a take for. Refresh
        // the source-aggregation string (new fact rows may carry new
        // source_session values that the prior run didn't see); leave
        // row_num + weight untouched to keep the take's identity stable.
        takeId = existing[0].id;
        await engine.executeRaw(
          `UPDATE takes SET source = $1, updated_at = now() WHERE id = $2`,
          [sources.slice(0, 200), takeId],
        );
      } else {
        const inserted = await engine.addTakesBatch([{
          page_id: pageId,
          row_num: nextRowNum,
          claim: best.fact,
          kind: 'fact',
          holder: 'self',
          weight: clamp01(avgWeight),
          since_date: sinceISO,
          source: sources.slice(0, 200),
          active: true,
        }]);
        if (inserted < 1) continue;

        const idRows = await engine.executeRaw<{ id: number }>(
          `SELECT id FROM takes WHERE page_id = $1 AND row_num = $2`,
          [pageId, nextRowNum],
        );
        if (idRows.length === 0) {
          nextRowNum += 1;
          continue;
        }
        takeId = idRows[0].id;
        nextRowNum += 1;
        takesWritten += 1;
      }

      // Mark all contributing facts consolidated.
      for (const f of cluster) {
        await engine.consolidateFact(f.id, takeId);
        factsConsolidated += 1;
      }

      // v0.35.4 (D-CDX-4 part 2) — chronological valid_until writeback.
      const chronological = [...cluster].sort((a, b) => {
        const t = a.valid_from.getTime() - b.valid_from.getTime();
        if (t !== 0) return t;
        return a.id - b.id;
      });
      for (let i = 0; i < chronological.length - 1; i++) {
        const older = chronological[i];
        const newer = chronological[i + 1];
        await engine.executeRaw(
          `UPDATE facts
             SET valid_until = $1
           WHERE id = $2
             AND (valid_until IS DISTINCT FROM $1)`,
          [newer.valid_from, older.id],
        );
      }
    }
  }

  return {
    phase: 'consolidate',
    status: factsConsolidated > 0 ? 'ok' : 'ok',
    duration_ms: 0,
    summary: dryRun
      ? `(dry-run) would promote ${factsConsolidated} facts into ${takesWritten} takes across ${bucketsProcessed} buckets`
      : `promoted ${factsConsolidated} facts into ${takesWritten} takes across ${bucketsProcessed} buckets`,
    details: {
      dryRun,
      facts_consolidated: factsConsolidated,
      takes_written: takesWritten,
      buckets_processed: bucketsProcessed,
      buckets_skipped: bucketsSkipped,
      resolution_paths: resolutionPaths,
    },
  };
}

/**
 * Greedy cosine clustering. Iterate facts sorted by valid_from DESC; each
 * fact joins the first cluster whose centroid (the first member, for
 * simplicity) is within `threshold` cosine. Otherwise starts a new cluster.
 *
 * Facts with no embedding cluster on their own (single-element cluster);
 * the consolidate phase only writes takes from clusters of size ≥ 2, so
 * no-embedding singletons sit out the cycle. v0.32+ fact-extraction
 * pipeline ensures embeddings are computed at insertFact time.
 */
function clusterFacts(facts: FactRow[], threshold: number): FactRow[][] {
  const sorted = [...facts].sort((a, b) => b.valid_from.getTime() - a.valid_from.getTime());
  const clusters: FactRow[][] = [];
  for (const f of sorted) {
    if (!f.embedding) {
      clusters.push([f]);
      continue;
    }
    let placed = false;
    for (const c of clusters) {
      const head = c[0];
      if (!head.embedding) continue;
      if (cosineSimilarity(f.embedding, head.embedding) >= threshold) {
        c.push(f);
        placed = true;
        break;
      }
    }
    if (!placed) clusters.push([f]);
  }
  return clusters;
}

function clamp01(x: number): number {
  if (!Number.isFinite(x)) return 0.5;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}
