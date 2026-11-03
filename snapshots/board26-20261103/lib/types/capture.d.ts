/**
 * Session-log capture: mirrors the durable session stream into the sidecar's
 * `session-log/` so the dreamer has raw material to consolidate.
 *
 * Events are buffered per session and drained on `session/flush` (awaited by
 * the store), on turn end, and on disposal. Nothing here touches the model
 * context; capture is write-only.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { ContentBlock, UserMessage } from '@deepseek-ai/dsh-llm';
import type { ResolvedConfig } from './config.ts';
import type { Sidecar } from './sidecar.ts';
export declare function markInjected(message: UserMessage): void;
export declare function textOf(content: readonly ContentBlock[]): string;
export declare class Capture {
    private readonly ctx;
    private readonly cfg;
    private readonly sidecar;
    private buffers;
    private readonly userId;
    private readonly logger;
    constructor(ctx: Context, cfg: ResolvedConfig, sidecar: Sidecar);
    /** Flush every buffer and close every open log (plugin teardown). */
    closeAll(): Promise<void>;
    private buffer;
    private onEvent;
    private push;
    /** Drain one session's buffer into the sidecar, preserving order. Safe to call concurrently. */
    flush(id: string): Promise<void>;
    private drain;
    private close;
}
