/**
 * P1-R2 (TASK-gbrain-canonical-post-closeout-hardening): truthful status tests.
 * Registry mapping + deriveStatus required/optional semantics.
 */

import { describe, test, expect } from 'bun:test';
import {
  CYCLE_REQUIRED_PHASES,
  GLOBAL_REQUIRED_PHASES,
  requiredPhasesForJob,
} from '../src/core/cycle/phase-registry.ts';
import { deriveStatus } from '../src/core/cycle.ts';
import type { PhaseResult } from '../src/core/cycle.ts';

function pr(phase: string, status: 'ok' | 'warn' | 'fail' | 'skipped'): PhaseResult {
  return {
    phase: phase as never,
    status,
    duration_ms: 10,
    summary: `${phase} ${status}`,
    details: {},
    ...(status === 'fail' ? { error: { class: 'InternalError', code: 'X', message: 'boom' } } : {}),
  } as PhaseResult;
}

const TOTALS_ZERO = {
  lint_fixes: 0, backlinks_added: 0, pages_synced: 0, pages_extracted: 0,
  pages_embedded: 0, orphans_found: 0, transcripts_processed: 0,
  synth_pages_written: 0, patterns_written: 0,
  pages_emotional_weight_recomputed: 0, edges_resolved: 0, edges_ambiguous: 0,
} as never;

const TOTALS_WORK = { ...TOTALS_ZERO, pages_synced: 1 } as never;

describe('P1-R2.1 phase registry', () => {
  test('cycle required = sync/extract; global required = embed/purge', () => {
    expect([...CYCLE_REQUIRED_PHASES].sort()).toEqual(['extract', 'sync']);
    expect([...GLOBAL_REQUIRED_PHASES].sort()).toEqual(['embed', 'purge']);
  });

  test('requiredPhasesForJob maps job kinds; everything else is optional', () => {
    expect(requiredPhasesForJob('autopilot-global-maintenance')).toBe(GLOBAL_REQUIRED_PHASES);
    expect(requiredPhasesForJob('autopilot-cycle')).toBe(CYCLE_REQUIRED_PHASES);
    expect(requiredPhasesForJob('anything-else')).toBe(CYCLE_REQUIRED_PHASES);
    for (const optional of ['lint', 'backlinks', 'synthesize', 'propose_takes', 'recompute_emotional_weight', 'orphans', 'grade_takes']) {
      expect(CYCLE_REQUIRED_PHASES.has(optional)).toBe(false);
      expect(GLOBAL_REQUIRED_PHASES.has(optional)).toBe(false);
    }
  });
});

describe('P1-R2.2 deriveStatus truthful semantics', () => {
  test('required-phase failure → failed (cannot hide in partial)', () => {
    const phases = [pr('lint', 'ok'), pr('sync', 'fail'), pr('extract', 'ok')];
    expect(deriveStatus(phases, TOTALS_WORK, CYCLE_REQUIRED_PHASES)).toBe('failed');
  });

  test('optional-phase failure only → partial, no page', () => {
    const phases = [pr('sync', 'ok'), pr('extract', 'ok'), pr('lint', 'fail')];
    expect(deriveStatus(phases, TOTALS_WORK, CYCLE_REQUIRED_PHASES)).toBe('partial');
  });

  test('required + optional failures → failed', () => {
    const phases = [pr('sync', 'fail'), pr('lint', 'fail')];
    expect(deriveStatus(phases, TOTALS_WORK, CYCLE_REQUIRED_PHASES)).toBe('failed');
  });

  test('global-maintenance registry: embed failure → failed; orphans failure → partial', () => {
    expect(deriveStatus([pr('embed', 'fail'), pr('purge', 'ok')], TOTALS_WORK, GLOBAL_REQUIRED_PHASES)).toBe('failed');
    expect(deriveStatus([pr('embed', 'ok'), pr('orphans', 'fail')], TOTALS_WORK, GLOBAL_REQUIRED_PHASES)).toBe('partial');
  });

  test('warn-only → partial; optional-only all-fail → partial; empty → failed', () => {
    expect(deriveStatus([pr('sync', 'ok'), pr('lint', 'warn')], TOTALS_WORK, CYCLE_REQUIRED_PHASES)).toBe('partial');
    // Optional-only failures never page — even when every selected phase failed
    // (e.g. lint-only cycle). Required failures already covered above.
    expect(deriveStatus([pr('lint', 'fail'), pr('backlinks', 'fail')], TOTALS_WORK, CYCLE_REQUIRED_PHASES)).toBe('partial');
    expect(deriveStatus([pr('lint', 'fail')], TOTALS_ZERO, CYCLE_REQUIRED_PHASES)).toBe('partial');
    expect(deriveStatus([], TOTALS_ZERO, CYCLE_REQUIRED_PHASES)).toBe('failed');
  });

  test('all ok: work → ok, no work → clean', () => {
    expect(deriveStatus([pr('sync', 'ok'), pr('extract', 'ok')], TOTALS_WORK, CYCLE_REQUIRED_PHASES)).toBe('ok');
    expect(deriveStatus([pr('sync', 'ok'), pr('extract', 'ok')], TOTALS_ZERO, CYCLE_REQUIRED_PHASES)).toBe('clean');
  });
});
