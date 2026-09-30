/**
 * System-1 associative recall: a memory hint surfaces on its own when the
 * current cue (latest user message, or a just-executed tool call) scores
 * confidently against consolidated knowledge / skills / episodes.
 *
 * Two verified injection points (see dsh-tools `PostToolDecision` and
 * dsh-agent `PreStepDecision`): `agent/pre-step` may replace the admitted
 * message batch, and `tools/post-execute` may attach `additionalContexts`.
 * `tools/pre-execute` cannot carry context, so it is not used.
 * The sidecar does the scoring, gating and habituation; this side only turns
 * the hint into a `UserMessage` that capture ignores.
 */
import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage, type UserMessage } from '@deepseek-ai/dsh-llm'
import type { ResolvedConfig } from './config.ts'
import type { Sidecar } from './sidecar.ts'
import { markInjected, textOf } from './capture.ts'

interface AssocReply {
  items: unknown[]
  hint: string
}

const ASSOC_TIMEOUT_MS = 4_000

export function hintMessage(hint: string): UserMessage {
  const msg = createUserMessage({ content: [{ type: 'text', text: hint }], source: { kind: 'user' } })
  markInjected(msg)
  return msg
}

export class Association {
  private readonly logger

  constructor(
    private readonly ctx: Context,
    private readonly cfg: ResolvedConfig,
    private readonly sidecar: Sidecar,
  ) {
    this.logger = ctx.logger('somni:assoc')

    if (cfg.assoc.preStep) {
      ctx.on('agent/pre-step', async (payload, next) => {
        const decision = await next()
        if (decision.kind !== 'enter') return decision
        const cue = latestUserCue(decision.messages)
        if (!cue) return decision
        const hint = await this.query('assoc.thought', { sessionId: payload.agent.id, cue, iterNo: payload.step })
        if (!hint) return decision
        return { ...decision, messages: [...decision.messages, hintMessage(hint)] }
      })
    }

    if (cfg.assoc.postExecute) {
      ctx.on('tools/post-execute', async (exec, result, next) => {
        const decision = await next()
        if (decision.kind !== 'accept' || result.isError || !exec.agent) return decision
        const hint = await this.query('assoc.action', {
          sessionId: exec.agent.id, toolName: exec.name, args: exec.arguments ?? {},
        })
        if (!hint) return decision
        return { ...decision, additionalContexts: [...(decision.additionalContexts ?? []), hintMessage(hint)] }
      })
    }

    ctx.on('session/disposed', (session) => {
      void this.sidecar.call('assoc.reset', { sessionId: session.id }, ASSOC_TIMEOUT_MS).catch(() => undefined)
    })
  }

  private async query(method: 'assoc.thought' | 'assoc.action', params: Record<string, unknown>): Promise<string> {
    if (!this.sidecar.alive) return ''
    try {
      const r = await this.sidecar.call<AssocReply>(method, params, ASSOC_TIMEOUT_MS)
      if (r.hint) this.logger.debug('%s → %d item(s)', method, r.items.length)
      return r.hint ?? ''
    } catch (err) {
      // Association is best-effort; never delay the step for it.
      this.logger.debug('%s skipped: %s', method, (err as Error).message)
      return ''
    }
  }
}

function latestUserCue(messages: readonly UserMessage[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]
    if (m && m.source.kind === 'user') {
      const t = textOf(m.content)
      if (t) return t
    }
  }
  return undefined
}
