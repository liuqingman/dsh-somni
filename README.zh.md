# dsh-somni

**给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的"睡眠巩固式"长期记忆插件。**

[English](./README.md) · [设计说明](./docs/design.md) · MIT

`dsh-somni` 让 DSH agent 拥有一套接近人类的记忆机制：**醒着时记**（身份与未完成的意图常驻
system prompt；相关记忆在行动时自动浮现；模型也能主动用工具回忆），**睡着时整理**
（agent 空闲一段时间后，"做梦者"把当天的会话日志蒸馏成情景 / 语义 / 前瞻 / 程序性记忆）。

所有记忆都是磁盘上的 Markdown + YAML front-matter，可以直接读、改、grep、进版本库。

```
┌──────────────── 醒着（本插件，TypeScript）──────────────────────┐
│ 捕获     session 事件 → session-log/*.md                        │
│ 注入     about_me · 用户画像 · 意图 · 回忆纪律                    │
│ 联想     最新一句话 / 工具调用 → 【记忆提示】                      │
│ 工具     recall_memories · search_episodes · add_intention…      │
└──────────────────────────┬──────────────────────────────────────┘
                           │ stdio JSON-RPC
┌──────────────── 睡着（Python sidecar）──────────────────────────┐
│ 做梦     待处理 session-log → 情景 / 知识 / 技能 / 意图 / 画像增量 │
│ 召回     关键词 + 向量（本地 ONNX）混合打分                        │
└─────────────────────────────────────────────────────────────────┘
```

## agent 得到什么

| 记忆类型 | 落盘位置 | 如何到达模型 |
|---|---|---|
| 身份 — `about_me.md`、`about_user.md`、按用户画像 | `memory/` | 常驻 system prompt 段 |
| 前瞻 — 带触发线索 / 截止时间的意图 | `memory/intentions/` | 常驻段（到期与线索命中的优先）；`add_intention` / `complete_intention` / `cancel_intention` / `list_intentions` |
| 情景 — 发生了什么、何时、结果如何 | `memory/episodes/` | `search_episodes`、`recall_memories`；联想提示 |
| 语义 — 事实、决策、坑、偏好 | `memory/knowledge/` | `search_knowledge`、`recall_memories`；联想提示 |
| 程序性 — 蒸馏出的技能（`SKILL.md`） | `skills/agent_learned_skills/` | 交给 `dsh-skill-filesystem` 加载；联想提示 |
| 工作记忆 — 当前对话 | `memory/session-log/` | `search_recent_conversation` |

联想提示属于 System-1：以线索（用户最新一句话，或刚执行完的工具名 + 参数）对
知识 / 技能 / 情景做关键词 + 向量混合打分；只有当榜首分数过绝对门限（`0.52`）**且**
与第二名拉开差距（`0.04`）时才注入，并按会话做习惯化抑制，同一条记忆不会反复出现。
向量层关闭时，钩子保持静默。

## 依赖

* DeepSeek Harness `>= 0.2.0-rc.1`（peer：`dsh-agent`、`dsh-session`、`dsh-llm`、`dsh-tools`、`dsh-system-prompt`、`dsh-subprocess`、`dsh-agent-default-model`、`dsh-home-paths`）
* Node `>= 22`
* `PATH` 上有 Python `>= 3.9`（可配置）。核心零第三方依赖。
  可选：`onnxruntime tokenizers numpy` 启用本地向量；`openai` 用于 `dream.llm: openai`。

## 安装

```bash
npm i dsh-somni
# 可选：向量层（语义召回 + 联想提示）
python3 -m pip install "onnxruntime>=1.16" "tokenizers>=0.15" "numpy>=1.24"
```

然后在 `~/.dsh/cordis.yml` 中加入插件 —— 全部选项及默认值见
[`examples/cordis.patch.yml`](./examples/cordis.patch.yml)。最小配置：

```yaml
- name: dsh-somni
- name: '@deepseek-ai/dsh-skill-filesystem'
  config:
    customSkillDirs: [~/.dsh/somni/skills/agent_learned_skills]
```

本地向量需要把 `BAAI/bge-small-zh-v1.5`（或同结构的句向量模型）的 `model.onnx` +
`tokenizer.json` 放到 `~/.dsh/somni/models/bge-small-zh-v1.5/`，或设置 `embed.modelDir`。
没有模型时插件只提示一次，退回纯关键词模式。

## 配置

| 键 | 默认 | 含义 |
|---|---|---|
| `dataDir` | `<DSH_HOME>/somni` | `memory/`、`skills/`、`models/` 所在根目录 |
| `python` | `python3` | sidecar 解释器 |
| `capture.enabled` / `maxMessageChars` | `true` / `4000` | 写 session-log；单条消息上限 |
| `inject.identity` / `intentions` / `discipline` | `true` | 常驻 prompt 段（order 100 / 130 / 1200） |
| `assoc.enabled` / `preStep` / `postExecute` | `true` | 对最新消息 / 工具执行后做联想 |
| `assoc.threshold` / `gap` / `maxItems` | `0.52` / `0.04` / `2` | 置信门限 |
| `tools.enabled` | `true` | 注册记忆工具（含 `dream_now`） |
| `dream.enabled` | `true` | 定时巩固 |
| `dream.idleMs` / `maxIntervalMs` / `minPendingLogs` | `15 min` / `6 h` / `1` | 安静这么久后开始；空闲时至少这么久做一次 |
| `dream.llm` | `host` | `host` = 走宿主 `ctx.llm`；`openai` = sidecar 直连 OpenAI 兼容接口（`dream.openai.{baseUrl,apiKeyEnv,model}`） |
| `dream.maxIterations` | `60` | 每次做梦的 ReAct 步数上限 |
| `embed.provider` | `local` | `local`（ONNX）· `http`（OpenAI 兼容 `/embeddings`，`embed.baseUrl`）· `off` |
| `logLevel` | `info` | sidecar 日志级别（转发到宿主 logger） |

做梦可被抢占：任何 agent 在梦中开始新一轮，sidecar 会被立刻叫停，用户从不等待巩固。
`dream_now` 工具可在对话里强制做一次。

## 与 DSH 的接合点

* **捕获** — `session/event` → 缓冲 → 在 `session/flush` / `turn/end` 时落盘；
  `session/disposed` 收尾日志。跳过子 agent 会话；插件自己注入的提示不会被回写。
* **Prompt** — `ctx.systemPrompt.section` × 3，带缓存；在 `agent/created`、每次放行用户输入的
  step 前（让意图线索能匹配到来消息）、做梦后、意图工具后刷新。
* **联想** — `agent/pre-step`（往放行的消息批里追加一条提示 `UserMessage`）与
  `tools/post-execute`（`additionalContexts`）。这是仅有的两个决策类型能携带上下文的钩子；
  `tools/pre-execute` 不能，因此未用。
* **工具** — schema 由 Python 函数的 docstring 生成，启动时注册到 `ctx.tools`。
* **做梦** — `ctx.agents.list()` 全空闲 + 安静计时器；sidecar 的 `sleep.run` 驱动 ReAct 循环，
  其中的 LLM 调用通过 RPC 反向请求 `llm.chat`，由 `ctx.llm.stream` +
  `ctx.agentDefaultModel.currentSelection()` 提供。
* **Sidecar** — 由 `ctx.subprocess.spawn` 拉起（环境已清洗，只转发配置里点名的 key），
  崩溃后退避重启，随插件优雅关闭。

## 开发

```bash
npm install
npm run typecheck
npm test            # python: pytest（单测 + 真实 stdio sidecar 冒烟）· ts: 构建 + 假宿主 e2e
```

`tests/smoke_plugin.mjs` 用一个最小的假宿主 context 跑构建后的插件，依次走过
捕获 → 工具 → prompt 刷新 → 会话关闭 → 经宿主 LLM 桥完成一次完整做梦。
`PYTHON=python3.11 node tests/smoke_plugin.mjs` 可指定解释器。

目录：`src/`（插件）、`python/somni_memory/`（记忆核心 + sidecar）、`python/tests/`、
`examples/`、`docs/`。

## 来源

记忆核心是为 *somni* 飞书 agent 构建的"工作 / 睡眠"记忆系统的解耦 fork —— 同样的磁盘格式、
衰减曲线与巩固策略，去掉了平台相关部分。

## 许可

MIT
