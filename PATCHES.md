# 板端（board26）本地补丁清单

这些改动**只存在于板 172.16.0.26 的运行副本**，历史上是在 `lib/*.js` 构建产物上手工改的，
没有对应 TS 源码。本文件 + `snapshots/board26-20261103/` 一起构成板端现状的版本留档。

> 原则：上游 `src/*.ts` 保持通用（开源可用），板端专属逻辑走本清单描述的方式重打；
> 重新构建（`npm run build`）会覆盖 `lib/`，届时这些改动必须按本清单补回。

## 已回写进 TS 源码的部分
| 改动 | 上游落点 |
| --- | --- |
| manual 做梦不被 agent 唤醒打断（`manualDream`） | `src/dream.ts` |
| sleep 四阶段流水线 / 睡眠纪律块 / 场所记忆提取规则 / 会话用户切换 | `python/somni_memory/*` |
| 跨夜缺口台账 `gap_ledger.py` | `python/somni_memory/gap_ledger.py` |

## 仅板端存在、尚未回写 TS 的部分
### 1. `lib/gate.js` —— 软热开关
读 `/xdata/dsh/robot/vlt-brain/vltctx-state.json` 的 `disabled_plugins[dsh-somni]`，
为真时所有钩子 no-op、sidecar 保活。mtime 感知缓存（3s TTL 兜底）。
上游对应物是 `src/toolgate.ts`（配置驱动、通用版），但板端这版**路径写死**、且
额外挂了 `capture`/`assoc`/`prompt`/`dream` 的 no-op 判定。

### 2. `lib/alias.js` —— 用户身份归并
`<dataDir>/memory/config/user-alias.json` 把裸 IP / 进程名映射成统一 user_id，
并提供 `hasProfile()`。板端专属（演示身份体系）。

### 3. `lib/toolgate.js` —— 工具面总闸（板端变体）
与上游 `src/toolgate.ts` 语义相同，但 gate 来源换成 `gate.js` 的 `somniDisabled()`，
5s watcher 翻转挂载/摘除。

### 4. `lib/{capture,prompt,index,dream,tools,assoc,sidecar}.js` 手改点
- `capture.js`：按 `globalThis.__vltSessionIp` / `__vltSpeaker` 判定说话人（登录身份优先于 sim 兜底），再过 `aliasOf()`
- `prompt.js`：identity 块加「说话人裁决（最高优先）」抬头；`build()` 传 `speakerUserId`（此前从不传 → 画像块永远为空）
- `index.js`：注入 `webServer`；注册 `/somni/dream`（HTTP 触发做梦）与 `/somni/call`（同步工具调用口，给拿不到 LLM 工具面的 sim 执行器用）
- `dream.js`：只有真人插话（`user/message` 且非 `[RETRY]`）才打断做梦
- `sidecar.js`：子进程 stderr 非 WARNING 行按 info 打（原 debug 看不到）
- `tools.js` / `assoc.js`：gate 判定接入

## 待办
把 1~4 里通用的部分（说话人注入、HTTP 触发口、stderr 日志级别）提成
配置驱动的上游实现，板端专属的（vltctx 路径、alias 表）走配置项，消除双份代码。
