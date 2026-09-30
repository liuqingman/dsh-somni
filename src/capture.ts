/**
 * Session-log capture: mirrors the durable session stream into the sidecar's
 * `session-log/` so the dreamer has raw material to consolidate.
 *
 * Events are buffered per session and drained on `session/flush` (awaited by
 * the store), on turn end, and on disposal. Nothing here touches the model
 * context; capture is write-only.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { ContentBlock, ToolResultMessage, UserMessage } from '@deepseek-ai/dsh-llm'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import type { ResolvedConfig } from './config.ts'
import type { Sidecar } from './sidecar.ts'

type Entry =
  | { kind: 'append'; role: 'user' | 'agent'; text: string }
  | { kind: 'tool'; call: string; result: string }

interface SessionBuffer {
  entries: Entry[]
  /** callId → "name(args)" awaiting its result. */
  calls: Map<string, string>
  draining: Promise<void>
  /** Total captured messages (user+agent); zero-length sessions are never closed on disk. */
  captured: number
}

/** Message ids the plugin itself injected (memory hints) — never echoed back into session-log. */
const injectedIds = new Set<string>()
export function markInjected(message: UserMessage): void {
  injectedIds.add(message.id)
}

export function textOf(content: readonly ContentBlock[]): string {
  const parts: string[] = []
  for (const block of content) {
    if (block.type === 'text') parts.push(block.text)
    else if (block.type === 'image') parts.push('[image]')
    else if (block.type === 'file') parts.push('[file]')
  }
  return parts.join('\n').trim()
}

function clip(text: string, max: number): string {
  if (text.length <= max) return text
  return `${text.slice(0, max)}\n…（截断，原长 ${text.length} 字）`
}

function compactArgs(raw: string, max = 300): string {
  const s = raw.replace(/\s+/g, ' ').trim()
  return s.length > max ? `${s.slice(0, max)}…` : s
}

export class Capture {
  private buffers = new Map<string, SessionBuffer>()
  private readonly userId = process.env['USER'] || process.env['USERNAME'] || 'default'
  private readonly logger

  constructor(
    private readonly ctx: Context,
    private readonly cfg: ResolvedConfig,
    private readonly sidecar: Sidecar,
  ) {
    this.logger = ctx.logger('somni:capture')
    ctx.on('session/event', (session, event) => this.onEvent(session, event))
    ctx.on('session/flush', (session) => this.flush(session.id))
    ctx.on('session/disposed', (session) => { void this.close(session.id) })
  }

  /** Flush every buffer and close every open log (plugin teardown). */
  async closeAll(): Promise<void> {
    await Promise.all([...this.buffers.keys()].map((id) => this.close(id)))
  }

  private buffer(id: string): SessionBuffer {
    let b = this.buffers.get(id)
    if (!b) {
      b = { entries: [], calls: new Map(), draining: Promise.resolve(), captured: 0 }
      this.buffers.set(id, b)
    }
    return b
  }

  private onEvent(session: Session, event: SessionEvent): void {
    // Sub-agent transcripts are noisy and their outcome surfaces in the parent as a tool result.
    if (session.header.origin === 'subagent') return
    const max = this.cfg.capture.maxMessageChars
    switch (event.type) {
      case 'user/message': {
        const msg = event.data
        if (injectedIds.has(msg.id)) { injectedIds.delete(msg.id); return }
        if (msg.source.kind !== 'user') return
        const text = textOf(msg.content)
        if (text) this.push(session.id, { kind: 'append', role: 'user', text: clip(text, max) })
        return
      }
      case 'assistant/message': {
        const text = textOf(event.data.message.content)
        if (text) this.push(session.id, { kind: 'append', role: 'agent', text: clip(text, max) })
        return
      }
      case 'tool/call': {
        const { callId, name, arguments: args } = event.data
        this.buffer(session.id).calls.set(callId, `${name}(${compactArgs(args)})`)
        return
      }
      case 'tool/result': {
        const msg: ToolResultMessage = event.data.message
        const b = this.buffer(session.id)
        const call = b.calls.get(msg.toolCallId) ?? `${msg.toolCallId}`
        b.calls.delete(msg.toolCallId)
        const err = event.data.error
        const body = textOf(msg.content) || (err ? `${err.name}: ${err.reason ?? err.code}` : '')
        const result = msg.isError ? `[error] ${body}` : body
        this.push(session.id, { kind: 'tool', call, result: clip(result, Math.min(max, 1500)) })
        return
      }
      case 'turn/end':
        void this.flush(session.id)
        return
      default:
        return
    }
  }

  private push(id: string, entry: Entry): void {
    const b = this.buffer(id)
    b.entries.push(entry)
    if (entry.kind === 'append') b.captured += 1
    // Long tool loops shouldn't sit in memory until turn end.
    if (b.entries.length >= 32) void this.flush(id)
  }

  /** Drain one session's buffer into the sidecar, preserving order. Safe to call concurrently. */
  flush(id: string): Promise<void> {
    const b = this.buffers.get(id)
    if (!b || b.entries.length === 0) return Promise.resolve()
    b.draining = b.draining.then(() => this.drain(id, b))
    return b.draining
  }

  private async drain(id: string, b: SessionBuffer): Promise<void> {
    const batch = b.entries.splice(0)
    for (const e of batch) {
      try {
        if (e.kind === 'append') {
          await this.sidecar.call('session.append', {
            sessionId: id, userId: this.userId, chatType: 'dsh', role: e.role, text: e.text,
          })
        } else {
          await this.sidecar.call('session.tool', { sessionId: id, call: e.call, result: e.result })
        }
      } catch (err) {
        this.logger.warn('capture dropped %d entries for %s: %s', batch.length, id, (err as Error).message)
        return
      }
    }
  }

  private async close(id: string): Promise<void> {
    const b = this.buffers.get(id)
    if (!b) return
    await this.flush(id)
    this.buffers.delete(id)
    if (b.captured === 0) return
    try {
      const r = await this.sidecar.call<{ path: string | null }>('session.close', { sessionId: id })
      if (r.path) this.logger.debug('closed %s → %s', id, r.path)
    } catch (err) {
      this.logger.warn('session.close failed for %s: %s', id, (err as Error).message)
    }
  }
}
