/**
 * Dream scheduling. A dream (sleep-agent consolidation) starts when every live
 * agent has been idle for `idleMs` and enough session-logs are pending, or —
 * steady-state — when logs have waited longer than `maxIntervalMs` and the
 * agents are idle right now. Dreams are preemptible: if any agent wakes up
 * mid-dream the sidecar is told to interrupt, so the user never waits on
 * consolidation. Open session-logs are rotated first so the day's own
 * conversation is eligible.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { ResolvedConfig } from './config.ts';
import type { Sidecar } from './sidecar.ts';
import type { PromptSections } from './prompt.ts';
interface SleepResult {
    status: 'deep_sleep' | 'skipped' | 'interrupted' | 'error';
    reason?: string;
    summary?: string;
    token_usage?: {
        total_tokens?: number;
    };
}
export declare class Dreamer {
    private readonly ctx;
    private readonly cfg;
    private readonly sidecar;
    private readonly prompt;
    private lastActivity;
    private lastDream;
    private dreaming;
    private timer;
    private readonly logger;
    constructor(ctx: Context, cfg: ResolvedConfig, sidecar: Sidecar, prompt: PromptSections);
    stop(): void;
    private allIdle;
    private tick;
    /** Run one dream now (used by the scheduler and the `dream_now` tool). */
    dream(trigger: string): Promise<SleepResult>;
    private interrupt;
}
/** Optional operator tool so a dream can be forced from the conversation. */
export declare function registerDreamTool(ctx: Context, dreamer: Dreamer): void;
export {};
