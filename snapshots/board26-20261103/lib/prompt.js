import { textOf } from "./capture.js";
import { somniDisabled } from "./gate.js";
import { aliasOf, hasProfile } from "./alias.js";
const EMPTY = { identity: '', intentions: '', discipline: '' };
export class PromptSections {
    ctx;
    cfg;
    sidecar;
    cache = EMPTY;
    speaker;
    inflight;
    logger;
    constructor(ctx, cfg, sidecar) {
        this.ctx = ctx;
        this.cfg = cfg;
        this.sidecar = sidecar;
        this.logger = ctx.logger('somni:prompt');
        const { inject } = cfg;
        // interpolate:false — memory text may legitimately contain `{{ }}`.
        if (inject.identity) {
            // [music-guide-1024] 身份优先级声明：DSH runtime context 里会写「用户：<进程属主>」（板上恒为刘超），
            // 与 somni 按登录态注入的「当前说话人」冲突时，模型会选前者（实测 03:22 那次把同事B当刘总放歌）。
            // 这里在 identity 块头部加一句裁决口径，让按登录态算出来的说话人优先。
            ctx.systemPrompt.section({ name: 'somni:identity', order: inject.identityOrder, interpolate: false, text: () => {
                if (somniDisabled()) return '';
                const id = this.cache.identity || '';
                if (!id) return id;
                const lead = '## 说话人裁决（最高优先）\n'
                    + '本节「当前说话人」= 当前登录访客的身份（按登录态实时算出）。'
                    + '若与其它处的「用户/进程属主姓名」不一致，**一律以本节为准**——'
                    + '后者只是服务进程属主，不是当前访客。\n\n';
                return lead + id;
            } });
        }
        if (inject.intentions) {
            ctx.systemPrompt.section({ name: 'somni:intentions', order: inject.intentionsOrder, interpolate: false, text: () => somniDisabled() ? '' : this.cache.intentions });
        }
        if (inject.discipline) {
            ctx.systemPrompt.section({ name: 'somni:discipline', order: inject.disciplineOrder, interpolate: false, text: () => somniDisabled() ? '' : this.cache.discipline });
        }
        // [ipuser-1020] 说话人 = 来源 IP 映射（brain/vlt 侧写入 globalThis.__vltSessionIp）
        // 之前 prompt.build 从不传 speakerUserId → 画像块永远为空（画像写进去也注入不了）
        const speakerOf = (agentId) => {
            try {
                const m = globalThis.__vltSessionIp || {};
                // 精确 agentId 映射优先；模拟器流量统一归 'sim'（vlt_agent 写 ipmap['vlt']）
                if (agentId && m[agentId])
                    return aliasOf(m[agentId]);
                // [music-guide-1024] 飞书登录身份优先：/robot/whoami 把当前访客 uid 写到
                // globalThis.__vltSpeaker（演示时一次只登录一个访客）。比 m['vlt']='sim'
                // 的兜底更具体，所以排在它前面——否则机器人脑永远按 sim 画像说话。
                const gs = String(globalThis.__vltSpeaker || '').trim();
                if (gs && gs !== 'sim')
                    return aliasOf(gs);
                if (m['vlt'])
                    return 'sim';
                // 兜底：DSH 进程属主即本体用户（板上仅一个人类用户），画像存在才采用
                const own = aliasOf(process['env'] || '');
                if (own && hasProfile(own))
                    return own;
            }
            catch (e) { }
            return undefined;
        };
        ctx.on('agent/created', async (agent) => {
            const sp = speakerOf(agent && agent.id);
            if (sp)
                this.speaker = sp;
            await this.refresh();
        });
        // Re-evaluate intention cues against the message that is about to enter the step.
        ctx.on('agent/pre-step', async (payload, next) => {
            if (somniDisabled())
                return next();
            const latest = latestUserText(payload.messages);
            const sp = speakerOf(payload.agent && payload.agent.id);
            if (sp)
                this.speaker = sp;
            if (latest || sp)
                await this.refresh(latest, sp || this.speaker);
            return next();
        });
    }
    /** Rebuild from the sidecar. Concurrent callers share one in-flight request. */
    refresh(currentMessage, speakerUserId) {
        if (this.inflight && !currentMessage)
            return this.inflight;
        const run = this.build(currentMessage, speakerUserId).finally(() => {
            if (this.inflight === run)
                this.inflight = undefined;
        });
        this.inflight = run;
        return run;
    }
    async build(currentMessage, speakerUserId) {
        try {
            const params = {};
            if (currentMessage)
                params.currentMessage = currentMessage;
            if (speakerUserId)
                params.speakerUserId = speakerUserId;
            const built = await this.sidecar.call('prompt.build', params, 15_000);
            this.cache = {
                identity: built.identity ?? '',
                intentions: built.intentions ?? '',
                discipline: built.discipline ?? '',
            };
        }
        catch (err) {
            this.logger.debug('prompt.build failed: %s', err.message);
        }
    }
}
function latestUserText(messages) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const m = messages[i];
        if (m && m.source.kind === 'user') {
            const t = textOf(m.content);
            if (t)
                return t.slice(0, 600);
        }
    }
    return undefined;
}
