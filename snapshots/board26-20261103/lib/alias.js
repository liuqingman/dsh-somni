/**
 * 用户身份归并：来源 IP（或进程用户兜底）→ 统一 user_id。
 *
 * 背景：capture.js 按来源 IP 区分用户（[ipuser-1020]），但裸 IP 当 user_id 会导致
 * 同一个人换网段就变成新用户、画像各写一份。别名表放在记忆数据目录里（可热改）：
 *   <dataDir>/memory/config/user-alias.json   { "127.0.0.1": "root", "10.0.0.5": {"id":"zhangsan","name":"张三"} }
 * 没有映射时原样返回（不丢数据）。
 */
import * as nodeFs from 'node:fs';
const ALIAS_PATH = '/xdata/dsh/home/somni/memory/config/user-alias.json';
let cache = { t: -1, map: {} };
function loadMap() {
    try {
        const st = nodeFs.statSync(ALIAS_PATH);
        if (st.mtimeMs !== cache.t) {
            const raw = JSON.parse(nodeFs.readFileSync(ALIAS_PATH, 'utf8')) || {};
            cache = { t: st.mtimeMs, map: raw };
        }
    }
    catch (e) {
        cache = { t: -1, map: {} };
    }
    return cache.map;
}
export function aliasOf(userId) {
    if (!userId)
        return userId;
    const v = loadMap()[userId];
    if (typeof v === 'string' && v)
        return v;
    if (v && typeof v === 'object' && v.id)
        return String(v.id);
    return userId;
}

export function hasProfile(userId) {
    if (!userId)
        return false;
    try {
        return nodeFs.existsSync('/xdata/dsh/home/somni/memory/users/' + userId + '/about_user.md');
    }
    catch (e) {
        return false;
    }
}
