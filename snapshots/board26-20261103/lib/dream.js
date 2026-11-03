import { somniDisabled } from "./gate.js";
import { textOf } from './capture.js';
import { gatedRegister } from "./toolgate.js";
const TICK_MS = 30_000;
const DREAM_TIMEOUT_MS = 45 * 60_000;
export class Dreamer {
    ctx;
    cfg;
    sidecar;
    prompt;
    lastActivity = Date.now();
    lastDream = Date.now();
    dreaming = false;
    manualDream = false;
    timer;
    logger;
    constructor(ctx, cfg, sidecar, prompt) {
        this.ctx = ctx;
        this.cfg = cfg;
        this.sidecar = sidecar;
        this.prompt = prompt;
        this.logger = ctx.logger('somni:dream');
        // 只有「真人插话」才让做梦让路。原实现里 agent/status=running 就中断，
        // 导致 robot-brain 侧任何一次自愈/反馈都会把梦打断（实测 iter 13/65 被砍）。
        ctx.on('agent/status', () => { this.lastActivity = Date.now(); });
        ctx.on('session/event', (_session, event) => {
            this.lastActivity = Date.now();
            if (!this.dreaming || !event || event.type !== 'user/message')
                return;
            const data = event.data || {};
            const src = data.source || {};
            if (src.kind !== 'user')
                return;
            const text = textOf(data.content || []);
            // harness 自愈重试/系统提示不算人插话，否则一次格式重试就能把梦砍掉
            if (!text || text.startsWith('[RETRY]') || text.includes('不是合法的可执行规划'))
                return;
            this.interrupt('user message');
        });
        this.timer = setInterval(() => { void this.tick(); }, TICK_MS);
        this.timer.unref?.();
    }
    stop() {
        if (this.timer)
            clearInterval(this.timer);
        this.timer = undefined;
        if (this.dreaming)
            this.interrupt('plugin stopping');
    }
    allIdle() {
        return this.ctx.agents.list().every((a) => a.status === 'idle');
    }
    async tick() {
        if (somniDisabled() || this.dreaming || !this.sidecar.alive || !this.allIdle())
            return;
        const now = Date.now();
        const quiet = now - this.lastActivity;
        const sinceDream = now - this.lastDream;
        if (quiet < this.cfg.dream.idleMs && sinceDream < this.cfg.dream.maxIntervalMs)
            return;
        // Rotate open logs first so `pendingLogs` reflects today's conversation too.
        try {
            await this.sidecar.call('session.closeAll', {}, 10_000);
            const st = await this.sidecar.call('sleep.status', {}, 10_000);
            if (st.sleeping || st.pendingLogs < this.cfg.dream.minPendingLogs)
                return;
            await this.dream(quiet >= this.cfg.dream.idleMs ? 'idle' : 'max_interval');
        }
        catch (err) {
            this.logger.debug('tick skipped: %s', err.message);
        }
    }
    /** Run one dream now (used by the scheduler and the `dream_now` tool). */
    async dream(trigger) {
        console.error('[DREAM-TRACE] dream() called trigger=%s dreaming=%s', trigger, this.dreaming);
        if (somniDisabled())
            return { status: 'skipped', reason: 'dsh-somni disabled (soft hot-switch)' };
        if (this.dreaming)
            return { status: 'skipped', reason: 'already dreaming' };
        this.dreaming = true;
        this.manualDream = trigger === 'manual';
        // fix2: manual dream must close open sessions first, otherwise freshly
        // chatted content never becomes pending and the dream is a no-op.
        try {
            await this.sidecar.call('session.closeAll', {}, 10_000);
        } catch (err) {
            this.logger.warn('manual dream closeAll failed: %s', err.message);
        }
        const started = Date.now();
        this.logger.info('dream start (trigger=%s)', trigger);
        try {
                const res = await this.sidecar.call('sleep.run', { trigger }, DREAM_TIMEOUT_MS);
            const secs = ((Date.now() - started) / 1000).toFixed(0);
            if (res.status === 'deep_sleep') {
                this.logger.info('dream done in %ss, tokens=%s', secs, res.token_usage?.total_tokens ?? '?');
            }
            else {
                this.logger.info('dream %s in %ss%s', res.status, secs, res.reason ? ` (${res.reason})` : '');
            }
            return res;
        }
        catch (err) {
            this.logger.warn('dream failed: %s', err.message);
            return { status: 'error', reason: err.message };
        }
        finally {
            this.dreaming = false;
            this.manualDream = false;
            this.lastDream = Date.now();
            void this.prompt.refresh();
        }
    }
    interrupt(reason) {
        this.logger.info('interrupting dream: %s', reason);
        void this.sidecar.call('sleep.cancel', {}, 5_000).catch(() => undefined);
    }
}
/** Optional operator tool so a dream can be forced from the conversation. */
export function registerDreamTool(ctx, dreamer) {
    gatedRegister(ctx, {
        name: 'dream_now',
        description: '立即触发一轮自我迭代（记忆整理）：把待处理的会话日志提炼为情景/知识/技能/意图记忆。耗时可能数分钟，仅在用户明确要求时调用。',
        parameters: { type: 'object', properties: {}, additionalProperties: false },
        output: {
            schema: { type: 'string' },
            render: (_args, value) => [{ type: 'text', text: String(value) }],
        },
        isConcurrencySafe: () => false,
        timeoutMs: DREAM_TIMEOUT_MS,
        async execute() {
            if (somniDisabled())
                return 'dsh-somni is currently disabled (soft hot-switch); dream tool is unavailable.';
            void dreamer.dream('manual').then((res) => { this.logger.info('manual dream finished: %s', res.status); }).catch(() => undefined);
            return '已触发自我迭代（后台进行中），期间有新消息会自动唤醒。';
        },
    });
}
