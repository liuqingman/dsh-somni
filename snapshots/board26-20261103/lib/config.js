/**
 * dsh-somni plugin configuration.
 *
 * Follows the core-plugin convention: an `interface Config` for consumers plus a
 * runtime `Config: z<Config>` schema that fills defaults when the entry is loaded
 * from `cordis.patch.yml`.
 */
import z from '@deepseek-ai/schemastery';
export const Config = z.object({
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
});
