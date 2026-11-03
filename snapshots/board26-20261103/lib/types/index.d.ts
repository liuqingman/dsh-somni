import type { Context } from '@deepseek-ai/cordis';
import { Config } from './config.ts';
export { Config } from './config.ts';
export type { ResolvedConfig } from './config.ts';
export declare const name = "dsh-somni";
export declare const inject: readonly ["subprocess", "sessions", "agents", "llm", "tools", "systemPrompt", "agentDefaultModel"];
/** `<dshHome>/somni` unless overridden; `~` expanded either way. */
export declare function resolveDataDir(configured: string): string;
export declare const apply: (ctx: Context, rawConfig: Config) => void;
