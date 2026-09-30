/**
 * dsh-somni plugin configuration.
 *
 * Follows the core-plugin convention: an `interface Config` for consumers plus a
 * runtime `Config: z<Config>` schema that fills defaults when the entry is loaded
 * from `cordis.patch.yml`.
 */
import z from '@deepseek-ai/schemastery'

export interface Config {
  /** Memory root (memory/ skills/ models/ live under it). Empty → `<dshHome>/somni`. */
  dataDir?: string
  /** Python interpreter used to launch the sidecar (≥ 3.9). */
  python?: string
  /** How long to wait for the sidecar's first `ping` before giving up. */
  sidecarStartupTimeoutMs?: number
  /** Session-log capture (what the dreamer later consolidates). */
  capture?: {
    enabled?: boolean
    /** Per-message cap written to session-log; longer text is truncated. */
    maxMessageChars?: number
  }
  /** Static system-prompt sections. */
  inject?: {
    /** about_me + user profiles. */
    identity?: boolean
    /** Prospective memory (open intentions / due reminders). */
    intentions?: boolean
    /** Recall discipline (when/how to call the memory tools). */
    discipline?: boolean
    identityOrder?: number
    intentionsOrder?: number
    disciplineOrder?: number
  }
  /** System-1 association hooks (need the vector layer; silent otherwise). */
  assoc?: {
    enabled?: boolean
    /** Cue = the latest user message, surfaced via `agent/pre-step`. */
    preStep?: boolean
    /** Cue = tool name + args, surfaced via `tools/post-execute`. */
    postExecute?: boolean
    /** Absolute floor on the hybrid score. */
    threshold?: number
    /** Confidence gap required between top-1 and top-2 before injecting one item. */
    gap?: number
    /** 1 or 2 memories per hint. */
    maxItems?: number
  }
  /** System-2 memory tools registered on `ctx.tools`. */
  tools?: {
    enabled?: boolean
  }
  /** Dreaming (sleep-agent consolidation). */
  dream?: {
    enabled?: boolean
    /** Idle time before a dream is attempted. */
    idleMs?: number
    /** Upper bound between dreams while the agent stays idle. */
    maxIntervalMs?: number
    /** Skip dreaming below this many pending session-logs. */
    minPendingLogs?: number
    /** `host` = reuse the harness model via `ctx.llm`; `openai` = the sidecar calls an OpenAI-compatible API itself. */
    llm?: 'host' | 'openai'
    openai?: {
      baseUrl?: string
      /** Name of the env var holding the key; forwarded explicitly to the sidecar. */
      apiKeyEnv?: string
      model?: string
    }
    /** Hard cap on ReAct iterations per dream. */
    maxIterations?: number
  }
  /** Embedding layer for semantic recall and association. */
  embed?: {
    provider?: 'local' | 'http' | 'off'
    /** Directory holding model.onnx + tokenizer.json. Empty → `<dataDir>/models/bge-small-zh-v1.5`. */
    modelDir?: string
    /** OpenAI-compatible `/embeddings` endpoint, used when `provider: http`. */
    baseUrl?: string
  }
  logLevel?: 'debug' | 'info' | 'warning' | 'error'
}

export const Config: z<Config> = z.object({
  dataDir: z.string().default(''),
  python: z.string().default('python3'),
  sidecarStartupTimeoutMs: z.number().default(20_000),
  capture: z.object({
    enabled: z.boolean().default(true),
    maxMessageChars: z.number().default(4000),
  }).default({}),
  inject: z.object({
    identity: z.boolean().default(true),
    intentions: z.boolean().default(true),
    discipline: z.boolean().default(true),
    identityOrder: z.number().default(100),
    intentionsOrder: z.number().default(130),
    disciplineOrder: z.number().default(1200),
  }).default({}),
  assoc: z.object({
    enabled: z.boolean().default(true),
    preStep: z.boolean().default(true),
    postExecute: z.boolean().default(true),
    threshold: z.number().default(0.52),
    gap: z.number().default(0.04),
    maxItems: z.number().default(2),
  }).default({}),
  tools: z.object({
    enabled: z.boolean().default(true),
  }).default({}),
  dream: z.object({
    enabled: z.boolean().default(true),
    idleMs: z.number().default(15 * 60_000),
    maxIntervalMs: z.number().default(6 * 60 * 60_000),
    minPendingLogs: z.number().default(1),
    llm: z.union(['host', 'openai']).default('host'),
    openai: z.object({
      baseUrl: z.string().default(''),
      apiKeyEnv: z.string().default('OPENAI_API_KEY'),
      model: z.string().default(''),
    }).default({}),
    maxIterations: z.number().default(60),
  }).default({}),
  embed: z.object({
    provider: z.union(['local', 'http', 'off']).default('local'),
    modelDir: z.string().default(''),
    baseUrl: z.string().default(''),
  }).default({}),
  logLevel: z.union(['debug', 'info', 'warning', 'error']).default('info'),
})

/** Fully-defaulted view used inside the plugin (schema guarantees every field). */
export type ResolvedConfig = {
  dataDir: string
  python: string
  sidecarStartupTimeoutMs: number
  capture: { enabled: boolean; maxMessageChars: number }
  inject: {
    identity: boolean; intentions: boolean; discipline: boolean
    identityOrder: number; intentionsOrder: number; disciplineOrder: number
  }
  assoc: { enabled: boolean; preStep: boolean; postExecute: boolean; threshold: number; gap: number; maxItems: number }
  tools: { enabled: boolean }
  dream: {
    enabled: boolean; idleMs: number; maxIntervalMs: number; minPendingLogs: number
    llm: 'host' | 'openai'; openai: { baseUrl: string; apiKeyEnv: string; model: string }; maxIterations: number
  }
  embed: { provider: 'local' | 'http' | 'off'; modelDir: string; baseUrl: string }
  logLevel: 'debug' | 'info' | 'warning' | 'error'
}
