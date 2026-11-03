import fs from 'node:fs';
// dsh-somni soft hot-switch gate. Reads vltctx-state.json (same file as vlt-control console API):
// disabled_plugins['dsh-somni'] = true -> hooks no-op, sidecar stays alive.
// Cache strategy: mtime-aware. statSync every call (cheap); re-read only when the
// state file changed on disk. Falls back to a 3s TTL for the mtime-unchanged case
// so a hot loop of hooks does not readFileSync each time. This removes the
// 3s staleness window right after a toggle (race fixed 2026-10-10: a hook fired
// within 3s of toggling used to observe the stale cached value and capture
// messages during the "disabled" period).
const STATE_PATH = '/xdata/dsh/robot/vlt-brain/vltctx-state.json';
const CACHE_MS = 3000;
let cacheTs = 0;
let cacheMtime = -1;
let cached = false;
export function somniDisabled() {
    const now = Date.now();
    let mtime = -1;
    try {
        mtime = fs.statSync(STATE_PATH).mtimeMs;
    } catch {
        // state file unreadable/missing -> fail-open, keep last known value for TTL
    }
    if (mtime !== -1 && mtime === cacheMtime && now - cacheTs < CACHE_MS)
        return cached;
    if (mtime === -1 && now - cacheTs < CACHE_MS)
        return cached;
    try {
        const st = JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
        cached = Boolean(st.disabled_plugins && st.disabled_plugins['dsh-somni']);
    } catch {
        cached = false;
    }
    cacheMtime = mtime;
    cacheTs = now;
    try { fs.appendFileSync('/tmp/gate-trace.log', `${new Date().toISOString()} mtime=${mtime.toFixed(1)} cachedM=${cacheMtime.toFixed(1)} -> disabled=${cached}\n`); } catch {}
    return cached;
}
