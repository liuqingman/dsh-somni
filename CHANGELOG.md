# Changelog

## [0.2.0-board26] — 2026-11-03

板端 172.16.0.26 运行副本回灌上游仓库，首次把板端与开源主线对齐。

### Added
- **sleep 四阶段流水线**（S1 提取 / S2 审视 / S3 写入 / S4 收尾），替换原单 ReAct 循环
- `gap_ledger.py`：跨夜能力缺口台账，同一缺口跨够天数才允许进插件生成（阈值分档：安全 1 天 / 用户纠偏 2 天 / 普通 3 天）
- 睡眠纪律 prompt 块：用户说「睡一觉」必须调 `dream_now`，白天不主动做梦
- 场所记忆提取规则：`[场所记忆]` 标记必须建 `place-<id>` 知识条目且带坐标
- 会话日志按 userId 切档：前端切登录身份后重开 session-log
- `snapshots/board26-20261103/`：板端 lib 构建产物快照（手工改过、无 TS 源码）
- `PATCHES.md`：板端本地补丁清单

### Fixed
- `sleep_agent.py` 丢失 `from __future__ import annotations`，导致 py3.8/3.9 无法 import（`dict | None` 在运行时求值）
- `registerDreamTool` 少传 `gate` 参数，`tsc` 报 TS2554（contents API 碎 commit 漏改）
- manual 触发做梦被 agent 唤醒误打断

### Known issues
- `python/tests/smoke_sidecar.py` 的假宿主 LLM 只应答 4 轮，四阶段流水线轮数变多后 `sleep.run` 超时（测试需跟上，非产品缺陷）
- `src/toolgate.ts`（上游通用版）与板端 `gate.js` + `toolgate.js` 双份实现，待收敛
