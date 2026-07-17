/**
 * P1-R2.3 (TASK-gbrain-canonical-post-closeout-hardening): tw-msk-side
 * failure alert outbox. The worker appends one JSONL row per required-failure
 * run; the host-side gbrain-alert-emitter.timer drains the file and posts to
 * Telegram. The path never depends on Helsinki being up.
 *
 * Best effort by design: an outbox write failure must never break a job.
 */
import { appendFileSync, mkdirSync } from 'fs';
import { dirname } from 'path';

const OUTBOX_PATH =
  process.env.GBRAIN_FAILURE_OUTBOX ??
  `${process.env.GBRAIN_HOME ?? '/opt/gbrain/home'}/.gbrain/alert-outbox.jsonl`;

export function appendFailureOutbox(row: Record<string, unknown>): void {
  try {
    mkdirSync(dirname(OUTBOX_PATH), { recursive: true });
    appendFileSync(OUTBOX_PATH, JSON.stringify({ ts: new Date().toISOString(), ...row }) + '\n', { mode: 0o600 });
  } catch (e) {
    console.warn(`[failure-outbox] append failed (best effort): ${e instanceof Error ? e.message : String(e)}`);
  }
}
