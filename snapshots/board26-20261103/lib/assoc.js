import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { somniDisabled } from "./gate.js";
import { markInjected, textOf } from "./capture.js";
const ASSOC_TIMEOUT_MS = 4_000;
export function hintMessage(hint) {
    const msg = createUserMessage({ content: [{ type: 'text', text: hint }], source: { kind: 'user' } });
    markInjected(msg);
    return msg;
}
export class Association {
    ctx;
    cfg;
    sidecar;
    logger;
    constructor(ctx, cfg, sidecar) {
        this.ctx = ctx;
        this.cfg = cfg;
        this.sidecar = sidecar;
        this.logger = ctx.logger('somni:assoc');
        if (cfg.assoc.preStep) {
            ctx.on('agent/pre-step', async (payload, next) => {
                const decision = await next();
                if (decision.kind !== 'enter')
                    return decision;
                const cue = latestUserCue(decision.messages);
                if (!cue)
                    return decision;
                const hint = await this.query('assoc.thought', { sessionId: payload.agent.id, cue, iterNo: payload.step });
                if (!hint)
                    return decision;
                return { ...decision, messages: [...decision.messages, hintMessage(hint)] };
            });
        }
        if (cfg.assoc.postExecute) {
            ctx.on('tools/post-execute', async (exec, result, next) => {
                const decision = await next();
                if (decision.kind !== 'accept' || result.isError || !exec.agent)
                    return decision;
                const hint = await this.query('assoc.action', {
                    sessionId: exec.agent.id, toolName: exec.name, args: exec.arguments ?? {},
                });
                if (!hint)
                    return decision;
                return { ...decision, additionalContexts: [...(decision.additionalContexts ?? []), hintMessage(hint)] };
            });
        }
        ctx.on('session/disposed', (session) => {
            void this.sidecar.call('assoc.reset', { sessionId: session.id }, ASSOC_TIMEOUT_MS).catch(() => undefined);
        });
    }
    async query(method, params) {
        if (somniDisabled())
            return '';
        if (!this.sidecar.alive)
            return '';
        try {
            const r = await this.sidecar.call(method, params, ASSOC_TIMEOUT_MS);
            if (r.hint)
                this.logger.debug('%s → %d item(s)', method, r.items.length);
            return r.hint ?? '';
        }
        catch (err) {
            // Association is best-effort; never delay the step for it.
            this.logger.debug('%s skipped: %s', method, err.message);
            return '';
        }
    }
}
function latestUserCue(messages) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const m = messages[i];
        if (m && m.source.kind === 'user') {
            const t = textOf(m.content);
            if (t)
                return t;
        }
    }
    return undefined;
}
