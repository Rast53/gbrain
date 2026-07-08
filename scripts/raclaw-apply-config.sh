#!/usr/bin/env bash
#
# raclaw-apply-config.sh — idempotent bootstrap of Raclaw-specific gbrain config.
#
# Applies the deployment-specific gbrain `config` table values that Raclaw prod
# requires on top of upstream gbrain defaults. Upstream defaults assume
# Anthropic-only subagent runtime; Raclaw runs DeepSeek via the gateway-native
# tool loop and needs several non-default settings.
#
# Run this script AFTER every gbrain upgrade / fresh deploy / DB recreation.
# It is safe to re-run: it upserts each key (last-write-wins to the documented
# value). Runtime-state keys (sync.last_commit, version, dream.synthesize
# .last_completion_ts) are intentionally NOT touched.
#
# Usage:
#   ./scripts/raclaw-apply-config.sh [--dry-run]
#
# Env:
#   GBRAIN_BIN   gbrain binary path   (default: /root/.bun/bin/gbrain)
#   GBRAIN_HOME  gbrain home dir      (default: /opt/gbrain/home)
#
# Exit codes: 0 ok, 2 missing gbrain binary, 3 config apply failed.
#
set -euo pipefail

GBRAIN_BIN="${GBRAIN_BIN:-/root/.bun/bin/gbrain}"
GBRAIN_HOME="${GBRAIN_HOME:-/opt/gbrain/home}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 64;;
  esac
done

export GBRAIN_HOME

if [[ ! -x "$GBRAIN_BIN" ]]; then
  echo "ERROR: gbrain binary not found at $GBRAIN_BIN" >&2
  echo "Set GBRAIN_BIN to the correct path." >&2
  exit 2
fi

# Each row: <config-key>\t<value>\t<reason>
# Order matters only for readability. All values are raclaw deployment-specific
# and intentionally diverge from upstream defaults.
read -r -d '' SETTINGS <<'EOF' || true
agent.use_gateway_loop	true	REQUIRED for non-Anthropic subagent models (deepseek). Without this, dream.synthesize subagent jobs fail with "non-Anthropic but agent.use_gateway_loop is not enabled". See incident 2026-07-08.
models.dream.synthesize	deepseek/deepseek-v4-pro	Dream-cycle transcript synthesis. DeepSeek chosen for cost; requires agent.use_gateway_loop=true.
models.dream.synthesize_verdict	deepseek/deepseek-v4-flash	Dream-cycle synthesis verdict pass (faster/cheaper model).
models.think	cpa.raclaw:gpt-5.5	Raclaw think tier routed via CPA (cli-proxy-api) relay.
embedding_model	openrouter:openai/text-embedding-3-small	Embedding provider. Paired with embedding_dimensions=1536.
embedding_dimensions	1536	Embedding vector dimension for text-embedding-3-small.
search.mode	conservative	Raclaw search mode default.
chunk_strategy	semantic	Chunking strategy.
schema_pack	raclaw-ops	Custom schema pack with server/service/project/device/repository/source/policy types.
mcp.publish_advisor	true	Expose advisor diagnostics over MCP.
mcp.publish_skills	true	Expose skills catalog over MCP.
cycle.conversation_facts_backfill.enabled	true	Enable conversation facts backfill phase.
EOF

apply_setting() {
  local key="$1" value="$2" reason="$3"
  local current
  current="$("$GBRAIN_BIN" config get "$key" 2>/dev/null || echo "")"
  if [[ "$current" == "$value" ]]; then
    printf '  OK  %-45s already set\n' "$key"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  --> %-45s would set %q (now: %q)\n' "$key" "$value" "$current"
    return 0
  fi
  # agent.use_gateway_loop is not in the validator's known-keys schema; --force
  # is required to bypass the "Unknown config key" guard. Other keys accept
  # normal set.
  if "$GBRAIN_BIN" config set "$key" "$value" --force >/dev/null 2>&1; then
    printf '  SET %-45s -> %s\n' "$key" "$value"
  else
    printf '  ERR %-45s FAILED\n' "$key" >&2
    return 1
  fi
}

if [[ $DRY_RUN -eq 1 ]]; then
  echo "raclaw-apply-config: DRY-RUN preview of Raclaw deployment config"
else
  echo "raclaw-apply-config: applying Raclaw deployment config"
fi
echo "  binary: $GBRAIN_BIN"
echo "  home:   $GBRAIN_HOME"
echo

FAILED=0
while IFS=$'\t' read -r key value reason; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  if ! apply_setting "$key" "$value" "$reason"; then
    FAILED=$((FAILED+1))
  fi
done <<< "$SETTINGS"

echo
if [[ $FAILED -gt 0 ]]; then
  echo "ERROR: $FAILED setting(s) failed to apply." >&2
  exit 3
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "Dry-run complete. Re-run without --dry-run to apply."
else
  echo "All Raclaw settings applied. If gbrain-worker is running, it reads"
  echo "config per-job — no restart strictly required, but for a clean state:"
  echo "  systemctl restart gbrain-worker"
fi
