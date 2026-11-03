import path from 'node:path';
import { expandHomePath, resolveDshHome } from '@deepseek-ai/dsh-home-paths';
import { Config } from "./config.js";
import { somniDisabled } from "./gate.js";
import { Sidecar } from "./sidecar.js";
import { Capture } from "./capture.js";
import { hostChat } from "./llm-bridge.js";
import { PromptSections } from "./prompt.js";
import { Association } from "./assoc.js";
import { registerMemoryTools } from "./tools.js";
import { Dreamer, registerDreamTool } from "./dream.js";
import { stopToolGate } from "./toolgate.js";
export { Config } from "./config.js";
export const name = 'dsh-somni';
export const inject = [
    'subprocess', 'sessions', 'agents', 'llm', 'tools', 'systemPrompt', 'agentDefaultModel', 'webServer',
];
/** `<dshHome>/somni` unless overridden; `~` expanded either way. */
export function resolveDataDir(configured) {
    if (configured)
        return path.resolve(expandHomePath(configured));
    return path.join(resolveDshHome(), 'somni');
}
/** Env forwarded to the sidecar on top of the scrubbed parent env. */
function sidecarEnv(cfg) {
    const env = {};
    if (cfg.dream.llm === 'openai') {
        const key = process.env[cfg.dream.openai.apiKeyEnv];
        if (key)
            env['SLEEP_API_KEY'] = key;
        if (cfg.dream.openai.baseUrl)
            env['SLEEP_BASE_URL'] = cfg.dream.openai.baseUrl;
        if (cfg.dream.openai.model)
            env['SLEEP_MODEL'] = cfg.dream.openai.model;
    }
    env['SLEEP_MAX_ITERS'] = String(cfg.dream.maxIterations);
    if (cfg.embed.provider === 'http' && cfg.embed.baseUrl)
        env['SOMNI_EMBED_BASE_URL'] = cfg.embed.baseUrl;
    return env;
}
export const apply = (ctx, rawConfig) => {
    console.error('[SOMNI-TRACE] apply called, dream cfg:', JSON.stringify(rawConfig && rawConfig.dream || null));
    const cfg = rawConfig;
    const logger = ctx.logger('somni');
    const dataDir = resolveDataDir(cfg.dataDir);
    const sidecar = new Sidecar(ctx, cfg, { dataDir, env: sidecarEnv(cfg) });
    if (somniDisabled())
        logger.warn('dsh-somni starts DISABLED (soft hot-switch); sidecar kept alive, hooks no-op');
    if (cfg.dream.llm === 'host') {
        const chat = hostChat(ctx);
        sidecar.onRequest('llm.chat', (params) => chat(params));
    }
    const capture = cfg.capture.enabled ? new Capture(ctx, cfg, sidecar) : undefined;
    const prompt = new PromptSections(ctx, cfg, sidecar);
    if (cfg.assoc.enabled && !somniDisabled())
        new Association(ctx, cfg, sidecar);
    if (cfg.tools.enabled && !somniDisabled())
        registerMemoryTools(ctx, sidecar, prompt);
    const dreamer = cfg.dream.enabled ? new Dreamer(ctx, cfg, sidecar, prompt) : undefined;
    console.error('[SOMNI-TRACE] dreamer=', !!dreamer, 'somniDisabled=', somniDisabled());
    if (dreamer && cfg.tools.enabled)
        registerDreamTool(ctx, dreamer);
    // HTTP entry so external flows (e.g. vlt-brain sleep intent) can trigger
    // a manual dream without going through the LLM tool catalog.
    const webServer = ctx.get('webServer');
    console.error('[SOMNI-TRACE] webServer=', !!webServer);
    if (webServer) {
        ctx.effect(() => webServer.register({
            kind: 'prefix', path: '/somni',
            handler: async (req, res) => {
                const u = (req.url || '').split('?')[0];
                if (req.method === 'POST' && (u === '/dream' || u === '/dream/' || u === '/somni/dream' || u === '/somni/dream/')) {
                    if (!dreamer) {
                        res.writeHead(503, { 'content-type': 'application/json; charset=utf-8' });
                        res.end(JSON.stringify({ ok: false, error: 'dream disabled in config' }));
                        return;
                    }
                    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({ ok: true, queued: true }));
                    dreamer.dream('manual')
                        .then((r) => logger.info('manual dream via HTTP: %s', JSON.stringify(r).slice(0, 300)))
                        .catch((e) => logger.error('manual dream via HTTP failed: %s', e.message));
                    return;
                }
                /* [somni-call-1103] 查询类工具的 HTTP 入口：sim 执行器拿不到 LLM 工具面，
                   以前 recall/search 这类工具被写进 steps 只能回假成功。这里是**同步**调用口：
                   调完直接把工具返回原文回给调用方，执行器拿去 reportToBrain。 */
                if (req.method === 'POST' && (u === '/call' || u === '/call/' || u === '/somni/call' || u === '/somni/call/')) {
                    const chunks = [];
                    let total = 0;
                    for await (const c of req) { total += c.length; if (total > (1 << 20)) break; chunks.push(c); }
                    let payload = {};
                    try { payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); } catch { payload = {}; }
                    const toolName = String(payload.name || payload.tool || '').trim();
                    if (!toolName) {
                        res.writeHead(400, { 'content-type': 'application/json; charset=utf-8' });
                        res.end(JSON.stringify({ ok: false, error: 'missing name' }));
                        return;
                    }
                    const uid = process.env.USER || process.env.USERNAME || 'default';
                    try {
                        const r = await sidecar.call('tools.call', {
                            name: toolName,
                            args: payload.args || payload.parameters || {},
                            sessionId: String(payload.sessionId || ''),
                            userId: uid,
                        }, 30000);
                        const result = (r && typeof r.result === 'string') ? r.result : JSON.stringify((r && r.result) ?? '');
                        res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
                        res.end(JSON.stringify({ ok: true, tool: toolName, result: result }));
                        logger.info('somni call %s via HTTP ok (' + String(result).length + ' chars)', toolName);
                    } catch (e) {
                        res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
                        res.end(JSON.stringify({ ok: false, tool: toolName, error: String(e && e.message || e) }));
                        logger.error('somni call %s via HTTP failed: ' + e.message, toolName);
                    }
                    return;
                }
                res.writeHead(404, { 'content-type': 'application/json; charset=utf-8' });
                res.end(JSON.stringify({ error: 'not found' }));
            },
        }), 'somni-http-dream');
        console.error('[SOMNI-TRACE] somni prefix route registered');
    }
    ctx.effect(() => {
        sidecar.start()
            .then(() => prompt.refresh())
            .catch((err) => logger.error('sidecar failed to start (%s); memory features are inactive until it recovers', err.message));
        return async () => {
            stopToolGate();
            dreamer?.stop();
            await capture?.closeAll();
            await sidecar.stop();
        };
    }, 'somni sidecar');
};
