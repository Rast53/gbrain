/**
 * Shape-keyed schema self-heals that the version ledger cannot see.
 *
 * `runMigrations` runs these on every pass (including no-pending). CLI/serve
 * boot historically skipped `runMigrations` when `hasPendingMigrations` was
 * false (`tryRunPendingMigrations` → `not_needed`), so a drifted
 * `idx_timeline_dedup` (or a missing pages upsert arbiter) survived forever
 * on an otherwise-current brain. Both call sites share this helper.
 *
 * Best-effort + idempotent: a no-op on a healthy schema; `gbrain doctor`
 * surfaces the same probes independently if a heal throws.
 */
import type { BrainEngine } from './engine.ts';
import { repairTimelineDedupIndex } from './timeline-dedup-repair.ts';
import { repairPagesUpsertArbiter } from './pages-upsert-arbiter.ts';

export async function runSchemaSelfHeals(engine: BrainEngine): Promise<void> {
  // #2038/#3737: idx_timeline_dedup must match ON CONFLICT (page_id, date,
  // md5(summary), source). A merge-renumbered migration can stamp the version
  // counter past the change while the index stays 3-column or raw-summary.
  try {
    const r = await repairTimelineDedupIndex(engine);
    if (r.repaired) {
      console.error(
        `[migrate] healed idx_timeline_dedup drift (#2038): ${r.before.join(',') || '(absent)'} ` +
        `→ page_id,date,md5(summary),source` +
        (r.collapsedDuplicates > 0 ? ` (collapsed ${r.collapsedDuplicates} duplicate row(s))` : ''),
      );
    }
  } catch { /* best-effort; doctor reports the drift if this couldn't run */ }

  // #550: same drift class for the pages upsert arbiter. When the
  // UNIQUE(source_id, slug) constraint vanishes (partial restore, manual DDL,
  // name-only migration guards), EVERY putPage fails with "no unique or
  // exclusion constraint" and neither re-initSchema nor the version counter
  // can see it. ADD-only self-heal; refuses (loudly) on duplicate rows.
  try {
    const p = await repairPagesUpsertArbiter(engine);
    if (p.repaired) {
      console.error(`[migrate] restored pages_source_slug_key UNIQUE(source_id, slug) (#550)`);
    } else if (p.reason === 'duplicates') {
      console.error(
        `[migrate] cannot restore pages_source_slug_key: ${p.duplicateGroups} duplicate ` +
        `(source_id, slug) group(s) exist — page upserts will keep failing until the ` +
        `duplicates are resolved (#550). See \`gbrain doctor\`.`,
      );
    }
  } catch { /* best-effort; doctor reports the drift if this couldn't run */ }
}
