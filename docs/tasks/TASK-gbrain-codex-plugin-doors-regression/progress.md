# TASK: gbrain-codex-plugin-doors-regression

Objective: the nightly heavy run is green on the codex plugin door — all five
scoped ops (`put_skill`, `delete_skill`, `join_brain`, `leave_brain`,
`sync_brain_skills`) appear in the test seed brain's `tools/list` without
weakening the production fail-closed design.

## Status

Done — harness-side fix (Variant 1). Production gate untouched; oracle unchanged.

## Root cause

`STARTER_OPS` (`src/mcp/surface.ts`) deliberately lists the five ops, but each
carries `requiredScopes` (`skill_editor` for put/delete; `skills_member_self`
for join/leave/sync). `stdioVisibleTools` (`src/mcp/server.ts`) fail-closes
every `requiredScopes` op whose scopes the *verified stdio local writer* does
not satisfy. `seedBrainForAgent` created a real PGLite brain by calling
`engine.initSchema()` directly — it never runs `gbrain init`/activation, which
is what normally registers the stdio writer. So `readLocalWriter(engine,
'stdio')` threw, `scopes` stayed `[]`, and all five ops were subtracted from
`tools/list`.

Reproduced deterministically against the seed fixture:

```
total starter: 38 visible: 33
MISSING: [ "put_skill", "delete_skill", "join_brain", "sync_brain_skills", "leave_brain" ]
```

The codex door's oracle (`test/e2e/codex-plugin-install-real.serial.test.ts`
step (e)) compares `tools/list` to `filterOpsForSurface(operations, 'starter')`
exactly, so the door went red.

## Fix (Variant 1 — harness)

- `test/helpers/agent-harness.ts` — `seedBrainForAgent` gains an opt-in
  `registerStdioWriter?: boolean`. When set it calls `registerLocalWriter(engine,
  'stdio', { sourceIds: ['*'], operations: null, scopes:
  SEEDED_STDIO_WRITER_SCOPES, slugPrefixes: null }, true)` while `GBRAIN_HOME`
  is pinned to the seed home, so the spawned `gbrain serve` child reads and
  verifies the same credential. The seed brain now resembles an owner-granted
  brain instead of one with no writer at all.
  `SEEDED_STDIO_WRITER_SCOPES` is *derived from the starter surface itself*
  (`read`, `write`, plus every `requiredScopes` of a starter op), so the fixture
  grant cannot drift out of sync with the oracle. Historically that resolves to
  `read`, `write`, `skill_editor`, `skills_member_self`.
- `test/e2e/codex-plugin-install-real.serial.test.ts` — the seed opts in with
  `{ registerStdioWriter: true }`. The oracle is unchanged.

No source under `src/` changed; `stdioVisibleTools` still fail-closes a writer
that lacks the scopes, and `requiredScopes` stays on all five ops.

## Regression test

`test/agent-harness-scoped-writer.serial.test.ts` (serial: real PGLite +
`process.env`):

1. a plain seed still fail-closes the five scoped starter ops out of
   `tools/list` (the production posture is preserved);
2. `registerStdioWriter: true` exposes the complete declared starter surface
   (and the seed holds a verifiable stdio-lane credential, not a bare file).

Verified discriminating: the second case fails on the pre-fix helper (the flag
is ignored, the five stay hidden) and passes with the fix.

## Evidence

```
bun test test/agent-harness-scoped-writer.serial.test.ts \
         test/mcp-stdio-gate-list.test.ts \
         test/agent-harness-seed.serial.test.ts \
         test/mcp-surface.test.ts \
         test/server-degraded-scope.test.ts                    # 36 pass
bun test test/mcp-surface.test.ts test/server-degraded-scope.test.ts \
         test/operations-trust-boundary.test.ts \
         test/shared-skills-authority.test.ts \
         test/e2e/codex-plugin-install-real.serial.test.ts     # 58 pass / 2 skip (no codex bin)
NODE_OPTIONS=--max-old-space-size=5120 bun run typecheck        # clean
bash scripts/check-test-isolation.sh                            # OK
# guard checks (test-names, privacy, fixture-privacy, newlines,
# source-config-leak): all exit 0
# spawn probe: seed w/ registerStdioWriter → `bun run src/cli.ts serve
#              --surface starter --source-guard` → tools/list == starter
#              surface (38/38, zero diff)
```

## Sources used

- `AGENTMAP.md`: **not present** at the repo root (only `AGENTS.md` /
  `CLAUDE.md`), so the pointer was not available.
- `code_def` / `code_callers` (gbrain MCP): **no MCP surface was available in
  this session**, so symbol lookup fell back to direct reads of
  `src/mcp/server.ts` (`stdioVisibleTools`), `src/mcp/surface.ts`
  (`STARTER_OPS`), `src/core/scope.ts` (`operationScopesAllowed`),
  `src/core/persistence/identity.ts` (`registerLocalWriter` / `verifyLocalWriter`),
  and `test/helpers/agent-harness.ts` (`seedBrainForAgent`).
