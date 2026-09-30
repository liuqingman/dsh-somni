/**
 * Dream scheduling. A dream (sleep-agent consolidation) starts when every live
 * agent has been idle for `idleMs` and enough session-logs are pending, or —
 * steady-state — when logs have waited longer than `maxIntervalMs` and the
 * agents are idle right now. Dreams are preemptible: if any agent wakes up
 * mid-dream the sidecar is told to interrupt, so the user never waits on
 * consolidation. Open session-logs are rotated first so the day's own
 * conversation is eligible.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { ResolvedConfig } from './config.ts'
import type { Sidecar } from './sidecar.ts'
import type { PromptSections } from './prompt.ts'

interface SleepStatus {
  sleeping: boolean
  pendingLogs: number
  llm: string
}

interface SleepResult {
  status: 'deep_sleep' | 'skipped' | 'interrupted' | 'error'
  reason?: string
  summary?: string
  token_usage?: { total_tokens?: number }
}

const TICK_MS = 30_000
const DREAM_TIMEOUT_MS = 45 * 60_000

export class Dreamer {
  private lastActivity = Date.now()
  private lastDream = Date.now()
  private dreaming = false
  private timer: NodeJS.Timeout | undefined
  private readonly logger

  constructor(
    private readonly ctx: Context,
    private readonly cfg: ResolvedConfig,
    private readonly sidecar: Sidecar,
    private readonly prompt: PromptSections,
  ) {
    this.logger = ctx.logger('somni:dream')
    ctx.on('agent/status', ({ status }) => {
      this.lastActivity = Date.now()
      if (status === 'running' && this.dreaming) this.interrupt('agent woke up')
    })
    ctx.on('session/event', () => { this.lastActivity = Date.now() })
    this.timer = setInterval(() => { void this.tick() }, TICK_MS)
    this.timer.unref?.()
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = undefined
    if (this.dreaming) this.interrupt('plugin stopping')
  }

  private allIdle(): boolean {
    return this.ctx.agents.list().every((a) => a.status === 'idle')
  }

  private async tick(): Promise<void> {
    if (this.dreaming || !this.sidecar.alive || !this.allIdle()) return
    const now = Date.now()
    const quiet = now - this.lastActivity
    const sinceDream = now - this.lastDream
    if (quiet < this.cfg.dream.idleMs && sinceDream < this.cfg.dream.maxIntervalMs) return
    // Rotate open logs first so `pendingLogs` reflects today's conversation too.
    try {
      await this.sidecar.call('session.closeAll', {}, 10_000)
      const st = await this.sidecar.call<SleepStatus>('sleep.status', {}, 10_000)
      if (st.sleeping || st.pendingLogs < this.cfg.dream.minPendingLogs) return
      await this.dream(quiet >= this.cfg.dream.idleMs ? 'idle' : 'max_interval')
    } catch (err) {
      this.logger.debug('tick skipped: %s', (err as Error).message)
    }
  }

  /** Run one dream now (used by the scheduler and the `dream_now` tool). */
  async dream(trigger: string): Promise<SleepResult> {
    if (this.dreaming) return { status: 'skipped', reason: 'already dreaming' }
    this.dreaming = true
    const started = Date.now()
    this.logger.info('dream start (trigger=%s)', trigger)
    try {
      const res = await this.sidecar.call<SleepResult>('sleep.run', { trigger }, DREAM_TIMEOUT_MS)
      const secs = ((Date.now() - started) / 1000).toFixed(0)
      if (res.status === 'deep_sleep') {
        this.logger.info('dream done in %ss, tokens=%s', secs, res.token_usage?.total_tokens ?? '?')
      } else {
        this.logger.info('dream %s in %ss%s', res.status, secs, res.reason ? ` (${res.reason})` : '')
      }
      return res
    } catch (err) {
      this.logger.warn('dream failed: %s', (err as Error).message)
      return { status: 'error', reason: (err as Error).message }
    } finally {
      this.dreaming = false
      this.lastDream = Date.now()
      void this.prompt.refresh()
    }
  }

  private interrupt(reason: string): void {
    this.logger.info('interrupting dream: %s', reason)
    void this.sidecar.call('sleep.cancel', {}, 5_000).catch(() => undefined)
  }
}

/** Optional operator tool so a dream can be forced from the conversation. */
export function registerDreamTool(ctx: Context, dreamer: Dreamer): void {
  ctx.tools.register({
    name: 'dream_now',
    description: '立即触发一次记忆整理（做梦）：把待处理的会话日志提炼为情景/知识/技能/意图记忆。耗时可能数分钟，仅在用户明确要求时调用。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    isConcurrencySafe: () => false,
    timeoutMs: DREAM_TIMEOUT_MS,
    async execute() {
      const res = await dreamer.dream('manual')
      if (res.status === 'deep_sleep') return res.summary || '做梦完成。'
      return `做梦${res.status}${res.reason ? `：${res.reason}` : ''}`
    },
  })
}
