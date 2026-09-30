/**
 * Python sidecar lifecycle + stdio ndjson JSON-RPC 2.0 client.
 *
 * The sidecar owns every byte of memory state; this module only spawns it,
 * multiplexes requests over its stdin/stdout, forwards its stderr to the
 * harness logger, and restarts it with backoff if it dies mid-flight.
 * Reverse requests (sidecar → host, e.g. `llm.chat`) are dispatched to
 * handlers registered with {@link Sidecar.onRequest}.
 */
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import type { SubprocessHandle } from '@deepseek-ai/dsh-subprocess'
import type { ResolvedConfig } from './config.ts'

type Json = Record<string, unknown>
type RequestHandler = (params: Json) => Promise<unknown>

interface Pending {
  resolve(value: unknown): void
  reject(err: Error): void
  timer: NodeJS.Timeout | undefined
}

export class SidecarError extends Error {
  constructor(message: string, readonly code?: number, readonly data?: unknown) {
    super(message)
    this.name = 'SidecarError'
  }
}

/** Where the bundled `python/` tree lives, both from `lib/` and from `src/`. */
export function pythonRoot(): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'python')
}

const MAX_RESTARTS = 3
const SHUTDOWN_GRACE_MS = 5_000
const DEFAULT_CALL_TIMEOUT_MS = 60_000

export interface SidecarOptions {
  /** Absolute data dir (already resolved against dshHome). */
  dataDir: string
  /** Env entries forwarded on top of the scrubbed parent env (API keys etc.). */
  env: Record<string, string>
}

export class Sidecar {
  private handle: SubprocessHandle | undefined
  private pending = new Map<number, Pending>()
  private handlers = new Map<string, RequestHandler>()
  private nextId = 1
  private ready: Promise<void> | undefined
  private stopping = false
  private restarts = 0
  private readonly logger

  constructor(
    private readonly ctx: Context,
    private readonly cfg: ResolvedConfig,
    private readonly opts: SidecarOptions,
  ) {
    this.logger = ctx.logger('somni:sidecar')
  }

  /** Register a handler for a sidecar → host request. */
  onRequest(method: string, handler: RequestHandler): void {
    this.handlers.set(method, handler)
  }

  get alive(): boolean {
    return this.handle !== undefined
  }

  /** Spawn and wait for the first `ping`. Safe to call repeatedly. */
  start(): Promise<void> {
    this.ready ??= this.spawn()
    return this.ready
  }

  /** Graceful `shutdown` then wait; falls back to SIGTERM/SIGKILL via graceMs. */
  async stop(): Promise<void> {
    this.stopping = true
    const handle = this.handle
    if (!handle) return
    try {
      await this.call('shutdown', {}, SHUTDOWN_GRACE_MS)
    } catch {
      // sidecar may already be gone; terminate below
    }
    const exited = await Promise.race([
      handle.done.then(() => true),
      new Promise<boolean>((r) => setTimeout(() => r(false), SHUTDOWN_GRACE_MS)),
    ])
    if (!exited) handle.terminate()
    await handle.done
  }

  /** JSON-RPC request. Rejects with {@link SidecarError} on RPC error, death, or timeout. */
  async call<T = unknown>(method: string, params: Json = {}, timeoutMs = DEFAULT_CALL_TIMEOUT_MS): Promise<T> {
    if (method !== 'shutdown' && method !== 'ping') await this.start()
    const handle = this.handle
    if (!handle?.stdin) throw new SidecarError('somni sidecar is not running')
    const id = this.nextId++
    const line = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'
    return new Promise<T>((resolve, reject) => {
      const timer = timeoutMs > 0
        ? setTimeout(() => {
          this.pending.delete(id)
          reject(new SidecarError(`somni sidecar: ${method} timed out after ${timeoutMs}ms`))
        }, timeoutMs)
        : undefined
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer })
      handle.stdin!.write(line, (err) => {
        if (err) {
          this.pending.delete(id)
          if (timer) clearTimeout(timer)
          reject(new SidecarError(`somni sidecar: write failed: ${err.message}`))
        }
      })
    })
  }

  private async spawn(): Promise<void> {
    const root = pythonRoot()
    const { cfg } = this
    const argv = [
      cfg.python, '-m', 'somni_memory',
      '--data-dir', this.opts.dataDir,
      '--llm', cfg.dream.llm,
      '--embed', cfg.embed.provider,
      '--assoc-threshold', String(cfg.assoc.threshold),
      '--assoc-gap', String(cfg.assoc.gap),
      '--assoc-max-items', String(cfg.assoc.maxItems),
      '--log-level', cfg.logLevel,
    ]
    if (cfg.embed.modelDir) argv.push('--embed-model-dir', cfg.embed.modelDir)

    const handle = this.ctx.subprocess.spawn({
      argv,
      cwd: root,
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' },
      graceMs: SHUTDOWN_GRACE_MS,
      env: {
        ...this.opts.env,
        PYTHONPATH: root,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
      },
    })
    this.handle = handle
    this.attach(handle)
    this.logger.debug('spawned pid=%d argv=%o', handle.pid, argv)

    try {
      const pong = await this.call<Json>('ping', {}, cfg.sidecarStartupTimeoutMs)
      this.restarts = 0
      this.logger.info('ready: llm=%s vector=%s pendingLogs=%s dataDir=%s',
        pong['llm'], pong['vector'], pong['pendingLogs'], pong['dataDir'])
    } catch (err) {
      handle.terminate()
      throw err
    }
  }

  private attach(handle: SubprocessHandle): void {
    if (handle.stdout) {
      const rl = createInterface({ input: handle.stdout, crlfDelay: Infinity })
      rl.on('line', (line) => this.onLine(line))
    }
    if (handle.stderr) {
      const rl = createInterface({ input: handle.stderr, crlfDelay: Infinity })
      rl.on('line', (line) => this.onStderr(line))
    }
    void handle.done.then((outcome) => this.onExit(handle, outcome.exitCode, outcome.signal))
  }

  private onLine(line: string): void {
    if (!line.trim()) return
    let msg: Json
    try {
      msg = JSON.parse(line) as Json
    } catch {
      this.logger.warn('non-JSON line on sidecar stdout: %s', line.slice(0, 200))
      return
    }
    if (typeof msg['method'] === 'string') {
      void this.onReverseRequest(msg)
      return
    }
    const id = msg['id']
    if (typeof id !== 'number') return
    const p = this.pending.get(id)
    if (!p) return
    this.pending.delete(id)
    if (p.timer) clearTimeout(p.timer)
    const error = msg['error'] as { code?: number; message?: string; data?: unknown } | undefined
    if (error) p.reject(new SidecarError(`somni sidecar: ${error.message ?? 'error'}`, error.code, error.data))
    else p.resolve(msg['result'])
  }

  private async onReverseRequest(msg: Json): Promise<void> {
    const method = msg['method'] as string
    const id = msg['id']
    const handler = this.handlers.get(method)
    let reply: Json
    if (!handler) {
      reply = { jsonrpc: '2.0', id, error: { code: -32601, message: `host has no handler for ${method}` } }
    } else {
      try {
        const result = await handler((msg['params'] as Json) ?? {})
        reply = { jsonrpc: '2.0', id, result }
      } catch (err) {
        const e = err as Error
        this.logger.warn('%s handler failed: %s', method, e.stack ?? e.message)
        reply = { jsonrpc: '2.0', id, error: { code: -32000, message: e.message ?? String(err) } }
      }
    }
    if (id === undefined || id === null) return // notification
    this.handle?.stdin?.write(JSON.stringify(reply) + '\n')
  }

  private onStderr(line: string): void {
    // Python logging format: "<ts> <LEVEL> <name>: <msg>"
    if (/ (ERROR|CRITICAL) /.test(line)) this.logger.error('%s', line)
    else if (/ WARNING /.test(line)) this.logger.warn('%s', line)
    else this.logger.debug('%s', line)
  }

  private onExit(handle: SubprocessHandle, exitCode: number | null, signal: NodeJS.Signals | null): void {
    if (this.handle !== handle) return
    this.handle = undefined
    this.ready = undefined
    const err = new SidecarError(`somni sidecar exited (code=${exitCode} signal=${signal})`)
    for (const p of this.pending.values()) {
      if (p.timer) clearTimeout(p.timer)
      p.reject(err)
    }
    this.pending.clear()
    if (this.stopping) return
    if (this.restarts >= MAX_RESTARTS) {
      this.logger.error('%s; giving up after %d restarts', err.message, this.restarts)
      return
    }
    this.restarts += 1
    const delay = 2_000 * this.restarts
    this.logger.warn('%s; restarting in %dms (%d/%d)', err.message, delay, this.restarts, MAX_RESTARTS)
    setTimeout(() => {
      if (this.stopping) return
      this.start().catch((e: Error) => this.logger.error('restart failed: %s', e.message))
    }, delay)
  }
}
