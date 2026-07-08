# Raclaw gbrain Runbook

Operational procedures for the Raclaw production gbrain deployment. This
covers the **deployment-specific** layer on top of upstream gbrain — anything
that diverges from upstream defaults and must survive upgrades.

- **Host:** `tw-msk-server` (`100.108.115.59` via Tailscale)
- **Service:** `gbrain-worker.service`, `gbrain-mcp.service`
- **Repo:** `/opt/gbrain/repo` → `Rast53/gbrain` (fork of `garrytan/gbrain`)
- **Binary:** `/root/.bun/bin/gbrain` (v0.42.55.0 at time of writing)
- **Home:** `/opt/gbrain/home`
- **DB:** `gbrain-db` container (PostgreSQL 16 + pgvector), persistent volume

## What makes Raclaw different from upstream

Upstream gbrain assumes an **Anthropic-only** subagent runtime: `claude-*`
models via the Anthropic Messages API tool-loop. Raclaw diverges in three ways:

1. **Subagent model is DeepSeek**, not Anthropic. Requires the gateway-native
   tool loop (`agent.use_gateway_loop=true`). Without it, every subagent job
   fails with: `non-Anthropic but agent.use_gateway_loop is not enabled`.
2. **Custom schema pack** (`raclaw-ops`) with entity types `server`, `service`,
   `project`, `device`, `repository`, `source`, `policy` — reflected in
   onboard checks and dashboard entity coverage.
3. **Model routing** via CPA (cli-proxy-api) relay and OpenRouter embeddings,
   not direct provider APIs.

These settings live in the gbrain `config` table (Postgres), **not** in code.
They survive binary upgrades but are lost on a fresh DB.

## Upgrade procedure

When upgrading gbrain (new binary / rebase on upstream):

```bash
ssh root@100.108.115.59

# 1. (Optional) snapshot current config before touching anything
docker exec gbrain-db psql -U gbrain -d gbrain \
  -c "COPY (SELECT key,value FROM config ORDER BY key) TO STDOUT" \
  > /opt/gbrain/backups/config-$(date +%Y%m%d).tsv

# 2. Do the upgrade (replace binary, restart services) per upstream release notes.

# 3. Re-apply Raclaw deployment config — idempotent, safe to re-run.
cd /opt/gbrain/repo
./scripts/raclaw-apply-config.sh           # dry-run first to preview
./scripts/raclaw-apply-config.sh           # then apply for real

# 4. Restart worker so it picks up config cleanly (handler reads per-job,
#    but a restart flushes any in-flight state).
systemctl restart gbrain-worker

# 5. Verify with a test subagent.
gbrain jobs submit subagent --data '{"model":"deepseek/deepseek-v4-pro","max_turns":3,"prompt":"ping"}'
gbrain jobs list --status completed | head
```

## Known recurring failure: gateway loop not enabled

**Symptom:** `autopilot-cycle` jobs time out at 30 min; subagent jobs go
`dead` with:
```
resolved model "deepseek/deepseek-v4-pro" is non-Anthropic but
agent.use_gateway_loop is not enabled
```

**Root cause:** `agent.use_gateway_loop` missing or `false` in the `config`
table. Happens on fresh DB or if an upstream default reset leaks through.

**Fix:** `./scripts/raclaw-apply-config.sh` (sets it to `true`), or manually:
```bash
gbrain config set agent.use_gateway_loop true --force
```
The `--force` is required because the key is not in the config validator's
known-keys schema (the validator warns "Nothing in gbrain reads this" —
incorrect; `src/core/minions/handlers/subagent.ts:225` reads it via
`engine.getConfig`).

## Config inventory

All keys set by `raclaw-apply-config.sh`. Runtime-state keys (version,
sync.last_commit, dream.synthesize.last_completion_ts) are intentionally
**not** managed by the script.

| Key | Value | Why |
|-----|-------|-----|
| `agent.use_gateway_loop` | `true` | Enables non-Anthropic subagent models |
| `models.dream.synthesize` | `deepseek/deepseek-v4-pro` | Cost-efficient transcript synthesis |
| `models.dream.synthesize_verdict` | `deepseek/deepseek-v4-flash` | Verdict pass |
| `models.think` | `cpa.raclaw:gpt-5.5` | Think tier via CPA relay |
| `embedding_model` | `openrouter:openai/text-embedding-3-small` | Embeddings |
| `embedding_dimensions` | `1536` | Paired with the model above |
| `search.mode` | `conservative` | Search strictness |
| `chunk_strategy` | `semantic` | Chunking |
| `schema_pack` | `raclaw-ops` | Custom entity types |
| `mcp.publish_advisor` | `true` | Advisor diagnostics over MCP |
| `mcp.publish_skills` | `true` | Skills catalog over MCP |
| `cycle.conversation_facts_backfill.enabled` | `true` | Facts backfill phase |

## Related code patches (committed separately)

These Raclaw patches live in the fork on top of upstream and are rebased
during upgrades — they are **not** managed by the config script:

- `src/commands/serve-http.ts` — MCP `/initialize` endpoint with OAuth2 metadata
- `src/core/import-file.ts` — defensive `parsed.title` type guard
- `src/core/onboard/checks.ts`, `pglite-engine.ts`, `postgres-engine.ts` —
  expanded entity type set for dashboard coverage
