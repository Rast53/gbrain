import type { Recipe } from '../types.ts';

/**
 * OpenCode Zen gateway (opencode.ai/zen/go/v1). OpenAI-compatible
 * /chat/completions endpoint proxying the DeepSeek V4 family under its own
 * auth (OPENCODE_GO_API_KEY).
 *
 * OpenCode Go requires a stable per-conversation session id in
 * `x-opencode-session` (their routing/abuse layer; a request without it
 * fails with `MissingSessionID` — verified live 2026-10-08) and asks
 * clients to identify themselves with their own user agent. Both ride on
 * `resolveDefaultHeaders` — the attribution-header mechanism OpenRouter
 * already uses for its referer/title. The session id comes from
 * OPENCODE_GO_SESSION_ID when set, with a constant fallback so the recipe
 * still works without the env.
 *
 * Local-production glue until upstream GBrain carries a native recipe; keep
 * it registered in the local patch registry like moonshot.ts.
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
  resolveDefaultHeaders(env) {
    return {
      'x-opencode-session': (env.OPENCODE_GO_SESSION_ID ?? '').trim() || 'gbrain-raclaw',
      'User-Agent': 'gbrain-raclaw/0.60 (+https://gbrain.ragpt.ru)',
    };
  },
  touchpoints: {
    chat: {
      models: ['deepseek-v4.1-flash', 'deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-flash'],
      supports_tools: true,
      supports_subagent_loop: true,
      supports_prompt_cache: true,
      // Thinking is on by default for the DeepSeek V4 family (same models the
      // `deepseek` recipe declares it for); reasoning bills as output.
      thinking_by_default: true,
      max_context_tokens: 1000000,
    },
  },
  setup_hint: 'Set OPENCODE_GO_API_KEY, then use `opencode-go:deepseek-v4.1-flash`.',
};
