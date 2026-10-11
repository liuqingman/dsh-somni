/**
 * Optional tool-face gate: hot-disable every dsh-somni tool (the memory tools
 * plus `dream_now`) by flipping a value in a JSON state file — no harness
 * restart required. This lets an operator (or another plugin's enable/disable
 * UI) take the whole tool face away at runtime.
 *
 * Semantics: with `tools.gate.stateFile` configured, the file is polled for
 * changes; the value at `tools.gate.stateKey` (a dotted path into the JSON)
 * truthy → all somni tools are disposed, so their schemas disappear from
 * `ctx.tools.list` and the model can neither see nor call them; falsy (or a
 * missing file) → they are re-registered. `execute` also hard-rejects while
 * the gate is closed, covering the in-flight window where the model still
 * plans against a stale prompt right after a flip.
 *
 * Without `stateFile` the gate is inert: `register()` mounts immediately and
 * behaves exactly like a plain `ctx.tools.register()`.
 */
import { statSync, readFileSync } from 'node:fs'
import type { Context } from '@deepseek-ai/cordis'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'

export interface ToolGateOptions {
  /** JSON state file watched for plugin on/off. Empty → gate disabled. */
  stateFile: string
  /** Dotted path into the state file whose truthy value disables the tool face. */
  stateKey: string
  /** State-file poll interval in ms. */
  pollMs: number
}

function resolvePath(obj: unknown, dotted: string): unknown {
  let cur: unknown = obj
  for (const seg of dotted.split('.')) {
    if (cur === null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[seg]
  }
  return cur
}

export class ToolGate {
  private readonly logger
  /** Definitions handed to us via register(), keyed by tool name. */
  private readonly defs = new Map<string, ToolDefinition>()
  /** Live disposer per mounted tool. */
  private readonly disposers = new Map<string, () => void>()
  /** undefined = state not read yet; closed tools stay unmounted. */
  private closed: boolean | undefined
  private timer: ReturnType<typeof setInterval> | undefined
  private lastMtimeMs = 0

  constructor(
    private readonly ctx: Context,
    private readonly opts: ToolGateOptions,
  ) {
    this.logger = ctx.logger('somni:gate')
  }

  start(): void {
    this.poll()
    this.timer = setInterval(() => this.poll(), this.opts.pollMs)
    this.timer.unref?.()
  }

  async stop(): Promise<void> {
    if (this.timer) clearInterval(this.timer)
    this.timer = undefined
    this.unmountAll()
  }

  isClosed(): boolean {
    return this.closed === true
  }

  /**
   * Register a tool through the gate. Returns a disposer that unmounts the
   * tool and forgets it (the gate itself keeps mounting the rest).
   */
  register(def: ToolDefinition): () => void {
    this.defs.set(def.name, def)
    if (this.closed) return () => this.forget(def.name)
    this.mount(def)
    return () => this.forget(def.name)
  }

  private forget(name: string): void {
    this.defs.delete(name)
    this.disposers.get(name)?.()
    this.disposers.delete(name)
  }

  private mount(def: ToolDefinition): void {
    if (this.disposers.has(def.name)) return
    const gated: ToolDefinition = {
      ...def,
      // Hard reject while closed: covers the in-flight window where the model
      // still holds a stale schema right after a flip.
      execute: async (args, exec) => {
        if (this.closed) throw new Error(`somni tool '${def.name}' is disabled by gate (${this.opts.stateFile})`)
        return def.execute(args, exec)
      },
    }
    this.disposers.set(def.name, this.ctx.tools.register(gated))
  }

  private unmountAll(): void {
    for (const dispose of this.disposers.values()) {
      try { dispose() } catch { /* best effort */ }
    }
    this.disposers.clear()
  }

  private mountAll(): void {
    for (const def of this.defs.values()) this.mount(def)
  }

  private apply(): void {
    if (this.closed && this.disposers.size > 0) {
      this.unmountAll()
      this.logger.info('gate closed → %d somni tool(s) unmounted', this.defs.size)
    } else if (!this.closed && this.defs.size > 0 && this.disposers.size === 0) {
      this.mountAll()
      this.logger.info('gate open → %d somni tool(s) mounted', this.defs.size)
    }
  }

  private poll(): void {
    let mtime = 0
    try { mtime = statSync(this.opts.stateFile).mtimeMs } catch { /* missing file → open */ }
    if (mtime !== 0 && mtime === this.lastMtimeMs) return
    this.lastMtimeMs = mtime

    let wasClosed = this.closed
    if (mtime === 0) {
      this.closed = false
    } else {
      try {
        const raw: unknown = JSON.parse(readFileSync(this.opts.stateFile, 'utf8'))
        const value = resolvePath(raw, this.opts.stateKey)
        this.closed = Boolean(value)
      } catch (err) {
        // Unreadable state file: keep the last known state, warn once per change.
        this.logger.warn('gate state file unreadable, keeping current state: %s', (err as Error).message)
        return
      }
    }
    if (this.closed !== wasClosed) this.apply()
  }
}
