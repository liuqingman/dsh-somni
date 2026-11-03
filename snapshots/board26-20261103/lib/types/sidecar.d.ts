import type { Context } from '@deepseek-ai/cordis';
import type { ResolvedConfig } from './config.ts';
type Json = Record<string, unknown>;
type RequestHandler = (params: Json) => Promise<unknown>;
export declare class SidecarError extends Error {
    readonly code?: number | undefined;
    readonly data?: unknown | undefined;
    constructor(message: string, code?: number | undefined, data?: unknown | undefined);
}
/** Where the bundled `python/` tree lives, both from `lib/` and from `src/`. */
export declare function pythonRoot(): string;
export interface SidecarOptions {
    /** Absolute data dir (already resolved against dshHome). */
    dataDir: string;
    /** Env entries forwarded on top of the scrubbed parent env (API keys etc.). */
    env: Record<string, string>;
}
export declare class Sidecar {
    private readonly ctx;
    private readonly cfg;
    private readonly opts;
    private handle;
    private pending;
    private handlers;
    private nextId;
    private ready;
    private stopping;
    private restarts;
    private readonly logger;
    constructor(ctx: Context, cfg: ResolvedConfig, opts: SidecarOptions);
    /** Register a handler for a sidecar → host request. */
    onRequest(method: string, handler: RequestHandler): void;
    get alive(): boolean;
    /** Spawn and wait for the first `ping`. Safe to call repeatedly. */
    start(): Promise<void>;
    /** Graceful `shutdown` then wait; falls back to SIGTERM/SIGKILL via graceMs. */
    stop(): Promise<void>;
    /** JSON-RPC request. Rejects with {@link SidecarError} on RPC error, death, or timeout. */
    call<T = unknown>(method: string, params?: Json, timeoutMs?: number): Promise<T>;
    private spawn;
    private attach;
    private onLine;
    private onReverseRequest;
    private onStderr;
    private onExit;
}
export {};
