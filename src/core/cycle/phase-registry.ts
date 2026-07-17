/**
 * P1-R2.1 (TASK-gbrain-canonical-post-closeout-hardening): required vs
 * optional phase registry.
 *
 * required = phases whose failure silently corrupts freshness, the data
 * plane or retention safety. A required-phase failure flips the outer
 * cycle/global report to 'failed' — and, via the worker's terminal-status
 * convention, minion_jobs.status to 'failed' — so it can no longer hide
 * inside a 'completed' queue job (the pre-P1-R2 lie: 6/6 cycles 'completed'
 * while sync/embed/recompute failed inside them).
 *
 * Optional phases degrade quality but never corrupt: their failure yields
 * 'partial' and no page. The registry is the single module both the cycle
 * (deriveStatus) and the deterministic harness (P1-R8) read.
 */

/** autopilot-cycle (per-source) + legacy single-source cycles. */
export const CYCLE_REQUIRED_PHASES: ReadonlySet<string> = new Set([
  // sync: a failed sync leaves the source stale while freshness stamps used
  // to advance anyway (findings N2/N9) — the data plane silently rots.
  'sync',
  // extract: page content → links/timeline; a failure leaves canonical
  // content unindexed with no other producer to backfill it.
  'extract',
]);

/** autopilot-global-maintenance (brain-wide). */
export const GLOBAL_REQUIRED_PHASES: ReadonlySet<string> = new Set([
  // embed: vectors ARE the retrieval plane; a silent embed failure strands
  // new content unsearchable (finding N4).
  'embed',
  // purge: retention safety — soft-deleted rows accumulate forever without it.
  'purge',
]);

/**
 * Required set per job kind. `autopilot-global-maintenance` uses the global
 * set; every other cycle-shaped job (per-source, legacy single-source,
 * single-phase makePhaseHandler wrappers) uses the cycle set.
 */
export function requiredPhasesForJob(jobName: string): ReadonlySet<string> {
  if (jobName === 'autopilot-global-maintenance') return GLOBAL_REQUIRED_PHASES;
  return CYCLE_REQUIRED_PHASES;
}
