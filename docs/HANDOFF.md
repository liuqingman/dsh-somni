# dsh-somni 工作交接（HANDOFF）

> 面向接手的开发者 / agent。目标：让你在不读历史会话的前提下，知道这个仓库是什么、
> 做到了哪一步、哪些决策不能改、下一步该做什么。
> 更细的架构说明见 [design.md](./design.md)，使用说明见 [../README.zh.md](../README.zh.md)。

## 0. 一句话

`dsh-somni` 是把 somni2 的"工作 / 睡眠"记忆系统（`skill_agent2/backend/` 下的
memory_manager / sleep_agent / memory_indexer）解耦后，封装成的**独立开源 DeepSeek Harness (DSH) 插件**。
Python 记忆核心以 sidecar 进程运行，TypeScript 负责全部 DSH 接线。**本地已全部通过测试，但尚未在真实 DSH 宿主里跑过。**

## 1. 代码路径

仓库根：`/data/liuc/agent/product-release-v1-develop/skill_agent2/dsh-somni/`

```
dsh-somni/
├── package.json                 npm 包 dsh-somni@0.1.0（ESM，main=lib/index.js）
├── tsconfig.json                tsc 5.9；allowImportingTsExtensions + rewriteRelativeImportExtensions
├── LICENSE                      MIT
├── README.md / README.zh.md     使用说明（英 / 中）
├── docs/
│   ├── design.md                设计说明：RPC 方法表、钩子选型、做梦调度、磁盘布局、衰减公式
│   └── HANDOFF.md               本文件
├── examples/cordis.patch.yml    接入 ~/.dsh/cordis.yml 的完整配置示例（含全部默认值）
├── src/                         TypeScript 插件（DSH 接线层，不含任何记忆算法）
│   ├── index.ts                 插件入口：name / inject / Config / apply；装配下面所有模块
│   ├── config.ts                schemastery Config（interface + Schema 一对，ResolvedConfig）
│   ├── sidecar.ts               拉起 python -m somni_memory；stdio ndjson JSON-RPC 客户端；崩溃退避重启
│   ├── capture.ts               session/event → 缓冲 → session.append/tool；turn/end、flush 落盘；disposed 收尾
│   ├── prompt.ts                ctx.systemPrompt.section ×3（identity/intentions/discipline），异步刷新缓存
│   ├── assoc.ts                 联想提示：agent/pre-step 追加 UserMessage；tools/post-execute additionalContexts
│   ├── tools.ts                 tools.list 拿到的 JSON schema → ctx.tools.register
│   ├── dream.ts                 做梦调度（全 agent idle + 安静计时 / 最大间隔；可抢占）+ dream_now 工具
│   └── llm-bridge.ts            反向 llm.chat：OpenAI 消息格式 ⇄ ctx.llm.stream
├── python/
│   ├── pyproject.toml           somni-memory@0.1.0，Python ≥3.9，核心零依赖；extras [embed]/[openai]
│   ├── somni_memory/
│   │   ├── __main__.py          CLI 入口（--data-dir/--llm/--embed/--assoc-*/--log-level → 环境变量）
│   │   ├── rpc.py               双向 stdio ndjson JSON-RPC 2.0；日志强制走 stderr
│   │   ├── service.py           RPC 方法表（ping/prompt.build/session.*/assoc.*/tools.*/sleep.*/index.sync/shutdown）
│   │   ├── assoc.py             System-1 联想钩子：混合分 + 门限/差距/习惯化
│   │   ├── toolspec.py          函数签名 + docstring → JSON Schema（兼容 from __future__ annotations）
│   │   ├── memory_manager.py    记忆核心（fork 自 somni2，已去 agentscope/secret_vault/access_password）
│   │   ├── sleep_agent.py       做梦 ReAct 循环（fork 自 somni2；ChatBackend 可插拔：host / openai）
│   │   ├── memory_indexer.py    向量索引（numpy/onnxruntime 可选，缺失时退化为关键词）
│   │   ├── token_utils.py
│   │   └── prompts/memory-extraction.md   做梦时的记忆抽取策略提示词
│   └── tests/
│       ├── smoke_sidecar.py     真实 stdio 起 sidecar + 假宿主应答 llm.chat，走完全部 RPC 与一次做梦
│       ├── test_sidecar.py      pytest 包装上面的冒烟
│       └── test_toolspec.py     schema 生成单测
├── tests/smoke_plugin.mjs       用假 DSH ctx 跑构建后的插件（捕获→工具→prompt→关会话→经 llm 桥完整做梦）
└── lib/                         tsc 产物（已构建，.js + types/*.d.ts）
```

规模：TS 约 1.2k 行，Python 约 4.9k 行（其中 ~3.8k 是 fork 的核心）。

**不要动的地方**：`skill_agent2/backend/` 是 somni2 线上代码，本插件是 fork 而非引用，两边独立演进。

## 2. 已完成的工作（按阶段）

| 阶段 | 内容 | 状态 |
|---|---|---|
| p01 调研 | 对照 `node_modules/@deepseek-ai/dsh-*/lib/types/*.d.ts` 逐个核实插件约定（`name/inject/Config/apply`、事件签名、决策类型、`ctx.subprocess` 语义） | ✅ |
| p02 骨架 | package.json / tsconfig / LICENSE / README 骨架 | ✅ |
| p03 Python 解耦 | 去 agentscope `ToolResponse`/`_as_tool`、secret_vault、access_password、dashscope 原生分支、飞书文案；数据根改为 `SOMNI_DATA_DIR` → `~/.dsh/somni`；numpy 可选 | ✅ |
| p04 Sidecar | rpc.py / service.py / assoc.py / toolspec.py / `__main__.py`；反向 `llm.chat` | ✅ |
| p05 TS 捕获 | sidecar.ts + capture.ts | ✅ |
| p06 TS 注入/联想/工具 | prompt.ts + assoc.ts + tools.ts + llm-bridge.ts | ✅ |
| p07 TS 做梦 | dream.ts（不走 `agent.runMaintenance`，可抢占） | ✅ |
| p08 测试 | pytest 3 passed（python3.10）；`npm run typecheck` 0 错误；`npm run test:ts` ALL OK；`npm pack --dry-run` 35 文件 | ✅ |
| p09 文档 | README 中英、docs/design.md、examples/cordis.patch.yml | ✅ |

## 3. 关键决策（已由用户确认，不要推翻）

- **命名**：npm `dsh-somni`；Python 包 `somni_memory`（PyPI `somni-memory`）；数据目录 `~/.dsh/somni`（`SOMNI_DATA_DIR`）；`SOMNI_*` 环境变量名保留；`source.kind: 'somni'`；自定义事件前缀 `somni/`。
- **许可**：MIT。不能用 `@deepseek-ai` scope。
- **架构**：Python 核心作 sidecar，stdio ndjson JSON-RPC 双向；TS 只做接线，不含记忆算法。
- **LLM**：只支持 OpenAI 兼容接口（`dream.llm: openai`）+ 宿主反向桥（`dream.llm: host`，默认，无需自带 key）。dashscope 原生分支已删。
- **做梦调度**：不用 `agent.runMaintenance`（会阻塞新一轮输入）；改为全 agent idle + 安静 `idleMs`（默认 15 min）或 `maxIntervalMs`（默认 6 h）触发，`agent/status running` 时 `sleep.cancel` 抢占。
- **注入点**：只用 `agent/pre-step`（`{kind:'enter', messages}`）和 `tools/post-execute`（`additionalContexts`）。`tools/pre-execute` 的决策类型无法携带上下文，**不可用**。
- **联想门控**：阈值 0.52 / 差距 0.04 / 最多 2 条；按会话习惯化；向量层不可用时钩子静默。
- **默认向量层**：本地 ONNX（`bge-small-zh-v1.5`），因当前没有可用的远程 embedding 端点。

## 4. 必须遵守的不变量

1. **sidecar 的 stdout 只能走协议**。Python 侧任何 `print()`/logger 到 stdout 都会破坏 ndjson 流；日志全部走 stderr（`__main__._setup_logging` 已强制）。
2. **`ctx.systemPrompt.section` 的 `text` 回调是同步的**。动态内容只能读 `prompt.ts` 的异步刷新缓存（刷新时机：sidecar 启动、`agent/created`、放行用户输入的 `pre-step`、做梦后、意图工具后）。
3. **`ctx.subprocess.spawn` 会清洗子进程环境**（凭证形状的变量、`DSH_*`）。sidecar 需要的 key 必须在 `index.ts#sidecarEnv` 里显式转发。
4. **工具 schema 来自 Python**（`toolspec.py` → `tools.list`），TS 原样注册。新增记忆工具只需改 `service.py#WORK_TOOLS`，不必改 TS。
5. **`from __future__ import annotations` 会把 `inspect.signature` 的注解变成字符串**。toolspec 已按字符串名分派类型；新增 Python 工具函数时注解写 `str/int/bool/float`，不要用 `Optional[...]` 等复杂类型。
6. 插件自己注入的提示消息（`assoc.ts#hintMessage`）通过 `capture.ts#markInjected` 排除，避免被回写进 session-log 再被做梦。

## 5. 本机开发环境

- `/usr/bin/python3` 是 3.8，**无法导入**本包；一律用 `python3.10`。
- `npm install --legacy-peer-deps`（`@deepseek-ai` rc 版本的 peer 范围很窄）。
- 命令：
  ```bash
  cd /data/liuc/agent/product-release-v1-develop/skill_agent2/dsh-somni
  npm run typecheck
  npm run build                         # → lib/
  PYTHON=python3.10 npm run test:ts     # 构建 + tests/smoke_plugin.mjs
  (cd python && python3.10 -m pytest tests -q)
  npm pack --dry-run
  ```
- 冒烟测试把 `SOMNI_DATA_DIR` 指到 `/tmp/somni-smoke*`，不会污染 `~/.dsh/somni`。
- 本机**没有安装真实 DSH 宿主**，也没有 onnxruntime（向量层在测试中为关键词退化模式）。

## 6. 未完成 / 建议的下一步

按优先级：

1. **真实 DSH 接入验证**（最重要，目前只有假宿主 e2e）
   - 装一个 DSH（≥ 0.2.0-rc.1），把本包 `npm link` 或安装到 DSH 能解析的 `node_modules`，按 `examples/cordis.patch.yml` 写入 `~/.dsh/cordis.yml`，重启 `dsh`。
   - 验证清单：日志出现 `somni:` 前缀且 `ping` 成功；system prompt 含 identity/intentions 段；对话能调 `recall_memories`/`add_intention`；`session/disposed` 后 `~/.dsh/somni/memory/session-log/` 有 `status: pending` 文件；`dream_now` 或空闲 15 min 后 `dream-log/` 出现文件且 session-log 移入 `archived/`；梦中发消息能打断（日志 `interrupted`）。
   - 重点核对假宿主可能掩盖的差异：`agent/pre-step` 的 `this: Scoped<Agent>` 与 `payload.agent` 取值、`session/event` 里 `header.origin`/`source.kind` 的真实字段、`ctx.agentDefaultModel.currentSelection()` 返回结构、`ctx.llm.stream` 的 `block-end` 块形状。
2. **本地开发接入示例**：给 `examples/` 补一份 `npm link` / 路径引用的说明。
3. **向量层实测**：安装 `onnxruntime tokenizers numpy`，放入 `bge-small-zh-v1.5` 的 `model.onnx + tokenizer.json`，确认联想提示真的会触发（目前测试里 `assoc hint appended: false` 是预期的退化行为）。
4. **仓库独立化**：目前 `dsh-somni/` 在 `skill_agent2` git 里是 **untracked**，没有任何 commit。需要拆成独立仓库、`git init`、补 `.gitignore`（`node_modules/`、`lib/`、`__pycache__/`、`*.egg-info`）、决定 `lib/` 是否入库。
5. **发布**：npm `dsh-somni`（名字已确认可用）、PyPI `somni-memory`（未查重）。发布前跑一遍 `npm pack --dry-run` 核对文件表。
6. **可选增强**：CI（GitHub Actions 跑 pytest + test:ts）；`index.sync` 目前只在做梦后/启动时调用，是否需要监听文件变化；多 DSH 实例共用 `dataDir` 的锁（当前明确为非目标）。

## 7. 参考

- 上游：https://github.com/deepseek-ai/deepseek-harness
- somni2 原始实现（只读参考）：`skill_agent2/backend/memory_manager.py`、`sleep_agent.py`、`memory_indexer.py`
- 相关设计文档：`skill_agent2/docs/memory-architecture-design.md`、`sleep-agent-enhancement-design.md`、`prospective-memory-design.md`
