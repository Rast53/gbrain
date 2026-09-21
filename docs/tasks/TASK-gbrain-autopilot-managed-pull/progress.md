# TASK: gbrain-autopilot-managed-pull

Objective: autopilot must not request `git pull` on a managed brain
(`persistence_brain.enabled`), in the cycle sync phase.

## Status

Done — managed-aware pull resolution + phase observability + tests.

## Changes

- `src/core/persistence/maintenance.ts` — new `autoSyncPullAllowed(engine, requested)`
  helper: false is always false, otherwise `!(await isManagedBrain(engine))`.
- `src/commands/autopilot.ts` — freshness `sync` dispatch and the inline
  `runCycle` fallback route `pull` through the helper; no unconditional
  `pull: true` remains.
- `src/commands/autopilot-fanout.ts` — per-source `autopilot-cycle` dispatch
  routes `pull` through the helper.
- `src/core/cycle.ts` — `runPhaseSync` forcibly clears `pull` on a managed
  brain (the persistence coordinator refuses it with
  `Managed sync requires --no-pull`) and reports
  `summary: "...; git pull skipped (managed brain)"` +
  `details.pullSkipped/pullSkippedReason` instead of failing the phase.
- Tests: `test/autopilot-managed-pull.test.ts`,
  `test/cycle-managed-sync-pull.serial.test.ts` (serial lane: it uses
  `mock.module()`, which the shard runner would leak across files).

## Round 2 (gate fixes)

- `check:test-isolation` R2 fix: renamed `test/cycle-managed-sync-pull.test.ts`
  to `test/cycle-managed-sync-pull.serial.test.ts` (the sanctioned escape hatch;
  the allowlist is not widened). This also removes the `mock.module()` leak that
  broke `test/sync-break-lock-all.test.ts` in shard 4
  (`TypeError: runBreakLock is not a function`). Behavior and assertions
  unchanged.
- `check:module-size` fix: raised the ceilings for
  `src/commands/autopilot.ts` (2671 → 2676) and `src/core/cycle.ts`
  (3213 → 3223) in `scripts/module-size-limits.tsv`, with the reason appended to
  each note column.
- Pre-existing (on master, out of scope): `src/core/minions/worker.ts`
  1566 > 1546 and `src/core/config.ts` 1795 > 1794.

## Test command (repo)

- Targeted: `bun test test/autopilot-managed-pull.test.ts test/cycle-managed-sync-pull.serial.test.ts test/autopilot-fanout.test.ts test/autopilot-fanout-wiring.test.ts`
- Autopilot/cycle regression set: `bun test test/autopilot-*.test.ts test/cycle-*.test.ts`
- Full unit runner: `bun run test` (`bash scripts/run-unit-parallel.sh`)
- Typecheck: `NODE_OPTIONS=--max-old-space-size=5120 bun run typecheck` (default
  Node heap OOMs on this repo)

## Pre-existing / environment failures (not from this change)

- `test/autopilot-install.test.ts` — 3 failures (`EACCES: mkdir
  /opt/dsh-runner/.gbrain`). The sandbox HOME is not writable and
  `gbrainPath()` caches the module-load home; reproduces with the change
  stashed.
- `test/jobs-autopilot-cycle-braindir.serial.test.ts` — 1 failure only when the
  serial files are run together in one process (passes alone); reproduces with
  the change stashed.

## Sources used

- `AGENTMAP.md`: **not present** at the repo root (root has `AGENTS.md` /
  `CLAUDE.md` only), so the pointer was not available.
- `code_def` / `code_callers` (gbrain MCP): **no MCP surface was available in
  this session**, so symbol lookup fell back to direct reads of
  `src/commands/autopilot.ts`, `src/commands/autopilot-fanout.ts`,
  `src/core/cycle.ts`, and
  `src/core/persistence/{maintenance,sync-discovery,sync-run}.ts`.
