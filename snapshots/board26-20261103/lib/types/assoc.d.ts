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
import type { Context } from '@deepseek-ai/cordis';
import { type UserMessage } from '@deepseek-ai/dsh-llm';
import type { ResolvedConfig } from './config.ts';
import type { Sidecar } from './sidecar.ts';
export declare function hintMessage(hint: string): UserMessage;
export declare class Association {
    private readonly ctx;
    private readonly cfg;
    private readonly sidecar;
    private readonly logger;
    constructor(ctx: Context, cfg: ResolvedConfig, sidecar: Sidecar);
    private query;
}
