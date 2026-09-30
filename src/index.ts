/**
 * dsh-somni — sleep-consolidated long-term memory for DeepSeek Harness.
 *
 * Wake phase (this process): capture the session stream, keep identity /
 * intentions resident in the system prompt, surface associative hints at
 * action time, and expose the memory tools. Sleep phase (Python sidecar):
 * consolidate session-logs into episodes / knowledge / skills / intentions.
 */
// Side-effect type imports: pull in the `Context` / `Events` augmentations of every service we touch.
import type {} from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import type {} from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-subprocess'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-tools'

import path from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import { expandHomePath, resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { Config, type ResolvedConfig } from './config.ts'
import { Sidecar } from './sidecar.ts'
import { Capture } from './capture.ts'
import { hostChat, type ChatParams } from './llm-bridge.ts'
import { PromptSections } from './prompt.ts'
import { Association } from './assoc.ts'
import { registerMemoryTools } from './tools.ts'
import { Dreamer, registerDreamTool } from './dream.ts'

export { Config } from './config.ts'
export type { ResolvedConfig } from './config.ts'

export const name = 'dsh-somni'

export const inject = {
  required: ['subprocess', 'sessions', 'agents', 'llm', 'tools', 'systemPrompt', 'agentDefaultModel'],
} as const

/** `<dshHome>/somni` unless overridden; `~` expanded either way. */
export function resolveDataDir(configured: string): string {
  if (configured) return path.resolve(expandHomePath(configured))
  return path.join(resolveDshHome(), 'somni')
}

/** Env forwarded to the sidecar on top of the scrubbed parent env. */
function sidecarEnv(cfg: ResolvedConfig): Record<string, string> {
  const env: Record<string, string> = {}
  if (cfg.dream.llm === 'openai') {
    const key = process.env[cfg.dream.openai.apiKeyEnv]
    if (key) env['SLEEP_API_KEY'] = key
    if (cfg.dream.openai.baseUrl) env['SLEEP_BASE_URL'] = cfg.dream.openai.baseUrl
    if (cfg.dream.openai.model) env['SLEEP_MODEL'] = cfg.dream.openai.model
  }
  env['SLEEP_MAX_ITERS'] = String(cfg.dream.maxIterations)
  if (cfg.embed.provider === 'http' && cfg.embed.baseUrl) env['SOMNI_EMBED_BASE_URL'] = cfg.embed.baseUrl
  return env
}

export const apply = (ctx: Context, rawConfig: Config) => {
  const cfg = rawConfig as ResolvedConfig
  const logger = ctx.logger('somni')
  const dataDir = resolveDataDir(cfg.dataDir)

  const sidecar = new Sidecar(ctx, cfg, { dataDir, env: sidecarEnv(cfg) })
  if (cfg.dream.llm === 'host') {
    const chat = hostChat(ctx)
    sidecar.onRequest('llm.chat', (params) => chat(params as unknown as ChatParams))
  }

  const capture = cfg.capture.enabled ? new Capture(ctx, cfg, sidecar) : undefined
  const prompt = new PromptSections(ctx, cfg, sidecar)
  if (cfg.assoc.enabled) new Association(ctx, cfg, sidecar)
  if (cfg.tools.enabled) registerMemoryTools(ctx, sidecar, prompt)
  const dreamer = cfg.dream.enabled ? new Dreamer(ctx, cfg, sidecar, prompt) : undefined
  if (dreamer && cfg.tools.enabled) registerDreamTool(ctx, dreamer)

  ctx.effect(() => {
    sidecar.start()
      .then(() => prompt.refresh())
      .catch((err: Error) => logger.error('sidecar failed to start (%s); memory features are inactive until it recovers', err.message))
    return async () => {
      dreamer?.stop()
      await capture?.closeAll()
      await sidecar.stop()
    }
  }, 'somni sidecar')
}
