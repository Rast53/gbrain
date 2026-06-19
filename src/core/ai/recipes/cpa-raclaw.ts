import type { Recipe } from "../types.ts";

/**
 * Raclaw CliproxyAPI — OpenAI-compatible chat endpoint.
 *
 * This is the user's private CPA gateway, exposed at https://cpa.raclaw.ru/v1.
 * Use model strings like `cpa.raclaw:gpt-5.5` so operational config doesn't
 * misleadingly look like direct OpenRouter traffic.
 */
export const cpaRaclaw: Recipe = {
  id: "cpa.raclaw",
  name: "Raclaw CliproxyAPI",
  tier: "openai-compat",
  implementation: "openai-compatible",
  base_url_default: "https://cpa.raclaw.ru/v1",
  auth_env: {
    required: ["CPA_RACLAW_API_KEY"],
    optional: ["CPA_RACLAW_BASE_URL"],
    setup_url: "https://cpa.raclaw.ru",
  },
  touchpoints: {
    chat: {
      models: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
      supports_tools: true,
      supports_subagent_loop: false,
      supports_prompt_cache: false,
      max_context_tokens: 200000,
      price_last_verified: "2026-06-01",
    },
  },
  setup_hint:
    "Set CPA_RACLAW_API_KEY and use cpa.raclaw:<model>, e.g. cpa.raclaw:gpt-5.5.",
};
