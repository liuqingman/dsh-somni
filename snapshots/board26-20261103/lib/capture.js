import { somniDisabled } from "./gate.js";
import { aliasOf } from "./alias.js";
/** Message ids the plugin itself injected (memory hints) — never echoed back into session-log. */
const injectedIds = new Set();
export function markInjected(message) {
    injectedIds.add(message.id);
}
export function textOf(content) {
    const parts = [];
    for (const block of content) {
        if (block.type === 'text')
            parts.push(block.text);
        else if (block.type === 'image')
            parts.push('[image]');
        else if (block.type === 'file')
            parts.push('[file]');
    }
    return parts.join('\n').trim();
}
function clip(text, max) {
    if (text.length <= max)
        return text;
    return `${text.slice(0, max)}\n…（截断，原长 ${text.length} 字）`;
}
function compactArgs(raw, max = 300) {
    const s = raw.replace(/\s+/g, ' ').trim();
    return s.length > max ? `${s.slice(0, max)}…` : s;
}
export class Capture {
    ctx;
    cfg;
    sidecar;
    buffers = new Map();
    userId = process.env['USER'] || process.env['USERNAME'] || 'default';
    logger;
    constructor(ctx, cfg, sidecar) {
        this.ctx = ctx;
        this.cfg = cfg;
        this.sidecar = sidecar;
        this.logger = ctx.logger('somni:capture');
        ctx.on('session/event', (session, event) => this.onEvent(session, event));
        ctx.on('session/flush', (session) => this.flush(session.id));
        ctx.on('session/disposed', (session) => { void this.close(session.id); });
    }
    /** Flush every buffer and close every open log (plugin teardown). */
    async closeAll() {
        await Promise.all([...this.buffers.keys()].map((id) => this.close(id)));
    }
    buffer(id) {
        let b = this.buffers.get(id);
        if (!b) {
            b = { entries: [], calls: new Map(), draining: Promise.resolve(), captured: 0 };
            this.buffers.set(id, b);
        }
        return b;
    }
    onEvent(session, event) {
        if (somniDisabled())
            return;
        // Sub-agent transcripts are noisy and their outcome surfaces in the parent as a tool result.
        if (session.header.origin === 'subagent')
            return;
        const max = this.cfg.capture.maxMessageChars;
        switch (event.type) {
            case 'user/message': {
                const msg = event.data;
                if (injectedIds.has(msg.id)) {
                    injectedIds.delete(msg.id);
                    return;
                }
                if (msg.source.kind !== 'user')
                    return;
                const text = textOf(msg.content);
                if (text)
                    this.push(session.id, { kind: 'append', role: 'user', text: clip(text, max) });
                return;
            }
            case 'assistant/message': {
                const text = textOf(event.data.message.content);
                if (text)
                    this.push(session.id, { kind: 'append', role: 'agent', text: clip(text, max) });
                return;
            }
            case 'tool/call': {
                const { callId, name, arguments: args } = event.data;
                this.buffer(session.id).calls.set(callId, `${name}(${compactArgs(args)})`);
                return;
            }
            case 'tool/result': {
                const msg = event.data.message;
                const b = this.buffer(session.id);
                // DSH puts the call id on message.source.callId (see dsh-llm createToolResultMessage);
                // fall back to the legacy flat field for forward-compat.
                const callId = msg.source?.callId ?? msg.toolCallId;
                const call = b.calls.get(callId) ?? `${callId}`;
                b.calls.delete(callId);
                const err = event.data.error;
                const body = textOf(msg.content) || (err ? `${err.name}: ${err.reason ?? err.code}` : '');
                const result = msg.isError ? `[error] ${body}` : body;
                this.push(session.id, { kind: 'tool', call, result: clip(result, Math.min(max, 1500)) });
                return;
            }
            case 'turn/end':
                void this.flush(session.id);
                return;
            default:
                return;
        }
    }
    push(id, entry) {
        const b = this.buffer(id);
        b.entries.push(entry);
        if (entry.kind === 'append')
            b.captured += 1;
        // Long tool loops shouldn't sit in memory until turn end.
        if (b.entries.length >= 32)
            void this.flush(id);
    }
    /** Drain one session's buffer into the sidecar, preserving order. Safe to call concurrently. */
    flush(id) {
        const b = this.buffers.get(id);
        if (!b || b.entries.length === 0)
            return Promise.resolve();
        b.draining = b.draining.then(() => this.drain(id, b));
        return b.draining;
    }
    async drain(id, b) {
        const batch = b.entries.splice(0);
        for (const e of batch) {
            try {
                if (e.kind === 'append') {
                    // [ipuser-1020] 按 sessionId 查来源 IP 区分用户；查不到回落原 userId
                    let uid = this.userId;
                    try {
                        const ipmap = globalThis.__vltSessionIp || {};
                        // [uid-speaker-1029] 登录身份优先：/robot/whoami 与 /robot/chat 会把当前
                        // 登录访客 uid 写进 __vltSpeaker（飞书 open_id）。前端 DSH 默认页的会话 id
                        // 与 brain agent id 不同，ipmap 精确匹配不到，旧逻辑会一路回落到
                        // ipmap['vlt']='sim'，把刘超在前端的操作记进 sim 画像。故提到 sim 兜底之前。
                        const spk = String(globalThis.__vltSpeaker || '').trim();
                        if (ipmap[id]) uid = ipmap[id];
                        else if (spk && spk !== 'sim') uid = spk;
                        else if (ipmap['vlt']) uid = 'sim';
                        else {
                            const ag = this.ctx && this.ctx.sessions ? this.ctx.sessions.get(id) : null;
                            if (ag && ag.id && ipmap[ag.id]) uid = ipmap[ag.id];
                        }
                    } catch (e) {}
                    uid = aliasOf(uid);   // 裸 IP → 统一 user_id（memory/config/user-alias.json）
                    await this.sidecar.call('session.append', {
                        sessionId: id, userId: uid, chatType: 'dsh', role: e.role, text: e.text,
                    });
                }
                else {
                    await this.sidecar.call('session.tool', { sessionId: id, call: e.call, result: e.result });
                }
            }
            catch (err) {
                this.logger.warn('capture dropped %d entries for %s: %s', batch.length, id, err.message);
                return;
            }
        }
    }
    async close(id) {
        const b = this.buffers.get(id);
        if (!b)
            return;
        await this.flush(id);
        this.buffers.delete(id);
        if (b.captured === 0)
            return;
        try {
            const r = await this.sidecar.call('session.close', { sessionId: id });
            if (r.path)
                this.logger.debug('closed %s → %s', id, r.path);
        }
        catch (err) {
            this.logger.warn('session.close failed for %s: %s', id, err.message);
        }
    }
}
