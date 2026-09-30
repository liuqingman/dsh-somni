/**
 * Resident memory in the system prompt: identity (about_me + user profiles),
 * prospective memory (open intentions / due reminders) and the recall
 * discipline. Section text is synchronous in dsh-system-prompt, so we keep a
 * cache that is refreshed asynchronously — at startup, before each step that
 * admits user input (so intention cues can match the latest message), after a
 * dream, and after any tool that mutates intentions.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { UserMessage } from '@deepseek-ai/dsh-llm'
import type { ResolvedConfig } from './config.ts'
import type { Sidecar } from './sidecar.ts'
import { textOf } from './capture.ts'

interface Built {
  identity: string
  intentions: string
  discipline: string
}

const EMPTY: Built = { identity: '', intentions: '', discipline: '' }

export class PromptSections {
  private cache: Built = EMPTY
  private inflight: Promise<void> | undefined
  private readonly logger

  constructor(
    private readonly ctx: Context,
    private readonly cfg: ResolvedConfig,
    private readonly sidecar: Sidecar,
  ) {
    this.logger = ctx.logger('somni:prompt')
    const { inject } = cfg
    // interpolate:false — memory text may legitimately contain `{{ }}`.
    if (inject.identity) {
      ctx.systemPrompt.section({ name: 'somni:identity', order: inject.identityOrder, interpolate: false, text: () => this.cache.identity })
    }
    if (inject.intentions) {
      ctx.systemPrompt.section({ name: 'somni:intentions', order: inject.intentionsOrder, interpolate: false, text: () => this.cache.intentions })
    }
    if (inject.discipline) {
      ctx.systemPrompt.section({ name: 'somni:discipline', order: inject.disciplineOrder, interpolate: false, text: () => this.cache.discipline })
    }

    ctx.on('agent/created', async () => { await this.refresh() })
    // Re-evaluate intention cues against the message that is about to enter the step.
    ctx.on('agent/pre-step', async (payload, next) => {
      const latest = latestUserText(payload.messages)
      if (latest) await this.refresh(latest)
      return next()
    })
  }

  /** Rebuild from the sidecar. Concurrent callers share one in-flight request. */
  refresh(currentMessage?: string): Promise<void> {
    if (this.inflight && !currentMessage) return this.inflight
    const run: Promise<void> = this.build(currentMessage).finally(() => {
      if (this.inflight === run) this.inflight = undefined
    })
    this.inflight = run
    return run
  }

  private async build(currentMessage?: string): Promise<void> {
    try {
      const built = await this.sidecar.call<Built>('prompt.build', currentMessage ? { currentMessage } : {}, 15_000)
      this.cache = {
        identity: built.identity ?? '',
        intentions: built.intentions ?? '',
        discipline: built.discipline ?? '',
      }
    } catch (err) {
      this.logger.debug('prompt.build failed: %s', (err as Error).message)
    }
  }
}

function latestUserText(messages: readonly UserMessage[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]
    if (m && m.source.kind === 'user') {
      const t = textOf(m.content)
      if (t) return t.slice(0, 600)
    }
  }
  return undefined
}
