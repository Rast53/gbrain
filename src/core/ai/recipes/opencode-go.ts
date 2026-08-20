import type { Recipe } from '../types.ts';

/**
 * OpenCode Zen gateway (opencode.ai/zen/go/v1). OpenAI-compatible /chat/completions
 * endpoint proxying DeepSeek V4 family under its own auth (OPENCODE_GO_API_KEY).
 * Local-production glue until upstream GBrain carries a native recipe; keep it
 * registered in the local patch registry like moonshot.ts.
 */
export const opencodeGo: Recipe = {
  id: 'opencode-go',
  name: 'OpenCode Zen (DeepSeek V4 via opencode.ai)',
  tier: 'openai-compat',
  implementation: 'openai-compatible',
  base_url_default: 'https://opencode.ai/zen/go/v1',
  auth_env: {
    required: ['OPENCODE_GO_API_KEY'],
    setup_url: 'https://opencode.ai',
  },
  touchpoints: {
    chat: {
      models: ['deepseek-v4-pro', 'deepseek-v4-flash'],
      supports_tools: true,
      supports_subagent_loop: true,
      supports_prompt_cache: false,
      max_context_tokens: 1000000,
    },
  },
  setup_hint: 'Set OPENCODE_GO_API_KEY, then use `opencode-go:deepseek-v4-pro`.',
};
