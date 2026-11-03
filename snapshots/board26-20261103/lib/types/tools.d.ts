/**
 * System-2 memory tools: the sidecar's `tools.list` is mirrored onto
 * `ctx.tools` so the model can deliberately recall episodes / knowledge,
 * search the current conversation, and manage intentions. Schemas come from
 * Python docstrings (see `somni_memory/toolspec.py`); the TS side only
 * forwards calls and refreshes the resident prompt after intention changes.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { Sidecar } from './sidecar.ts';
import type { PromptSections } from './prompt.ts';
export declare function registerMemoryTools(ctx: Context, sidecar: Sidecar, prompt: PromptSections): void;
