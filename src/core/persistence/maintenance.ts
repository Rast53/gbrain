import { existsSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { acquireLock, releaseLock } from '../pglite-lock.ts';
import { assertManagedFilesystemWrite } from './filesystem-guard.ts';
import { OperationError } from '../ops/contract.ts';
import type { SqlEngine } from './model.ts';
import { managedPersistenceEnabled } from './ownership.ts';

export const UNSUPPORTED_MANAGED_BULK_WRITERS = ['cycle.extract_facts', 'extract-conversation-facts', 'conversation_facts_backfill', 'loops_extract'] as const;

/**
 * Resolve the `pull` flag for an AUTOMATIC maintenance sync on this brain.
 *
 * A managed brain routes sync through the persistence coordinator, which
 * refuses `git pull` outside an explicit drained maintenance window
 * (`Managed sync requires --no-pull`, `src/core/persistence/sync-discovery.ts`).
 * Asking for a pull there fails the cycle's sync phase with
 * `writer_coordinator_required`, so every autopilot pull decision (freshness
 * dispatch, per-source fan-out, inline cycle) routes through this helper and
 * resolves to no pull. Unmanaged brains keep the caller's `requested` decision
 * (typically `sourceConfigHasRemoteUrl`). False is always false — no DB read.
 */
export async function autoSyncPullAllowed(engine: SqlEngine, requested: boolean): Promise<boolean> {
  if (!requested) return false;
  return !(await managedPersistenceEnabled(engine));
}


/** Refuse unsupported multi-stage writers before providers, files or git change. */
export async function assertUnmanagedCanonicalWriter(engine: SqlEngine, operation: string): Promise<void> {
  const rows = await engine.executeRaw<{ enabled: boolean }>('SELECT enabled FROM persistence_brain WHERE singleton=1');
  if (rows?.[0]?.enabled) throw new OperationError('writer_coordinator_required',
    `${operation} cannot mutate a managed brain through the legacy writer.`,
    'Use supported persistence operations. Source topology and maintenance require a verified drain before migration.');
}

/** The legacy engine copier cannot transfer permanent IDs, withdrawals or ownership. */
export async function assertLegacyEngineMigration(engine: SqlEngine): Promise<void> {
  const tables = ['persistence_requests', 'fact_withdrawals', 'persistence_worktrees'] as const;
  // Older migration sources can predate these tables. Do not interpret any
  // other database failure as an empty history.
  const present = await engine.executeRaw<{ name: string }>(
    'SELECT name FROM unnest($1::text[]) AS t(name) WHERE to_regclass(name) IS NOT NULL', [tables]);
  for (const table of tables) {
    if (!present.some(row => row.name === table)) continue;
    const [row] = await engine.executeRaw<{ present: boolean }>(`SELECT EXISTS(SELECT 1 FROM ${table}) AS present`);
    if (row?.present) throw new OperationError('writer_coordinator_required',
      'Engine migration cannot discard durable write history, withdrawal protection, or canonical ownership.',
      'Keep this datastore. Use forward repair or a verified migration that preserves the complete persistence state.');
  }
}

/** Legacy reinit may rename only after acquiring the stable sibling kernel lock. */
export async function backupUnmanagedPglite(dataDir: string, backupDir: string): Promise<void> {
  assertManagedFilesystemWrite(dataDir);
  if (!existsSync(dataDir) || existsSync(backupDir)) throw new Error('PGLite backup paths changed; inspect before retrying.');
  const lock = await acquireLock(dataDir, { timeoutMs: 0 });
  try {
    assertManagedFilesystemWrite(dataDir);
    if (existsSync(backupDir)) throw new Error('PGLite backup already exists; inspect before retrying.');
    renameSync(dataDir, backupDir);
    // Metadata moved with the old datastore. Remove only this ticket's marker;
    // release still targets the original stable sibling native lock.
    lock.lockDir = join(backupDir, '.gbrain-lock');
  } finally { await releaseLock(lock); }
}

/**
 * Build the `skipped` PhaseResult for a legacy maintenance phase that a
 * managed brain refuses to run through the legacy path (#5175/#5180/#5203).
 * The phase reports `skipped` + `writer_coordinator_required` so a healthy
 * lane stays `ok` instead of `partial` forever.
 */
export function managedPhaseSkip<P extends string>(phase: P, summary: string) {
  return {
    phase,
    status: 'skipped' as const,
    duration_ms: 0,
    summary,
    details: { reason: 'writer_coordinator_required' },
  };
}
