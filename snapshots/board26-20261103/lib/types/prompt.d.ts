/**
 * Resident memory in the system prompt: identity (about_me + user profiles),
 * prospective memory (open intentions / due reminders) and the recall
 * discipline. Section text is synchronous in dsh-system-prompt, so we keep a
 * cache that is refreshed asynchronously — at startup, before each step that
 * admits user input (so intention cues can match the latest message), after a
 * dream, and after any tool that mutates intentions.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { ResolvedConfig } from './config.ts';
import type { Sidecar } from './sidecar.ts';
export declare class PromptSections {
    private readonly ctx;
    private readonly cfg;
    private readonly sidecar;
    private cache;
    private inflight;
    private readonly logger;
    constructor(ctx: Context, cfg: ResolvedConfig, sidecar: Sidecar);
    /** Rebuild from the sidecar. Concurrent callers share one in-flight request. */
    refresh(currentMessage?: string): Promise<void>;
    private build;
}
