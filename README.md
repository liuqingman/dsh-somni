# dsh-somni

**Sleep-consolidated long-term memory for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).**

[中文说明](./README.zh.md) · [Design notes](./docs/design.md) · MIT

`dsh-somni` gives a DSH agent a memory that works the way people's does: it
**remembers while awake** (identity and open intentions stay resident in the
system prompt, relevant memories surface on their own at action time, and the
model can recall deliberately with tools) and **consolidates while asleep**
(when the agent has been idle for a while, a "dreamer" distills the day's
session logs into episodic, semantic, prospective and procedural memory).

Everything is plain Markdown + YAML front-matter on disk, so you can read,
edit, grep and version your agent's memory.

```
┌──────────────── awake (this plugin, TypeScript) ────────────────┐
│ capture       session events → session-log/*.md                 │
│ inject        about_me · user profiles · intentions · discipline│
│ associate     latest message / tool call → 【记忆提示】 hint     │
│ tools         recall_memories · search_episodes · add_intention…│
└──────────────────────────┬──────────────────────────────────────┘
                           │ stdio JSON-RPC
┌──────────────── asleep (Python sidecar) ────────────────────────┐
│ dreamer       pending session-logs → episodes / knowledge /     │
│               skills / intentions / profile deltas              │
│ recall        hybrid keyword + vector (local ONNX) scoring      │
└─────────────────────────────────────────────────────────────────┘
```

## What the agent gets

| Memory kind | Where it lives | How it reaches the model |
|---|---|---|
| Identity — `about_me.md`, `about_user.md`, per-user profiles | `memory/` | Resident system-prompt section |
| Prospective — intentions with cue / due time | `memory/intentions/` | Resident section (due & cue-matched first); `add_intention` / `complete_intention` / `cancel_intention` / `list_intentions` tools |
| Episodic — what happened, when, how it went | `memory/episodes/` | `search_episodes`, `recall_memories`; associative hints |
| Semantic — facts, decisions, pitfalls, preferences | `memory/knowledge/` | `search_knowledge`, `recall_memories`; associative hints |
| Procedural — distilled skills (`SKILL.md`) | `skills/agent_learned_skills/` | Point `dsh-skill-filesystem` at the directory; associative hints |
| Working — the current conversation | `memory/session-log/` | `search_recent_conversation` |

Associative hints are System-1: a cue (the latest user message, or a tool name
+ arguments after it ran) is scored against knowledge / skills / episodes with a
hybrid keyword+vector score; a hint is injected only when the top hit clears an
absolute floor (`0.52`) **and** stands clear of the runner-up (`0.04` gap),
with per-session habituation so the same memory is not repeated. With the
vector layer off the hooks stay silent.

## Requirements

* DeepSeek Harness `>= 0.2.0-rc.1` (peer deps: `dsh-agent`, `dsh-session`, `dsh-llm`, `dsh-tools`, `dsh-system-prompt`, `dsh-subprocess`, `dsh-agent-default-model`, `dsh-home-paths`)
* Node `>= 22`
* Python `>= 3.9` on `PATH` (configurable). No Python dependencies for the core.
  Optional: `onnxruntime tokenizers numpy` for local embeddings, `openai` for `dream.llm: openai`.

## Install

```bash
npm i dsh-somni
# optional: vector layer (semantic recall + associative hints)
python3 -m pip install "onnxruntime>=1.16" "tokenizers>=0.15" "numpy>=1.24"
```

Then add the plugin to `~/.dsh/cordis.yml` — see [`examples/cordis.patch.yml`](./examples/cordis.patch.yml)
for every option with its default. Minimal:

```yaml
- name: dsh-somni
- name: '@deepseek-ai/dsh-skill-filesystem'
  config:
    customSkillDirs: [~/.dsh/somni/skills/agent_learned_skills]
```

For local embeddings put `model.onnx` + `tokenizer.json` of
`BAAI/bge-small-zh-v1.5` (or any sentence-embedding model with the same
layout) under `~/.dsh/somni/models/bge-small-zh-v1.5/`, or set
`embed.modelDir`. Without a model the plugin logs once and runs keyword-only.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `dataDir` | `<DSH_HOME>/somni` | `memory/`, `skills/`, `models/` live here |
| `python` | `python3` | Interpreter for the sidecar |
| `capture.enabled` / `maxMessageChars` | `true` / `4000` | Write session-logs; per-message cap |
| `inject.identity` / `intentions` / `discipline` | `true` | Resident prompt sections (orders 100 / 130 / 1200) |
| `assoc.enabled` / `preStep` / `postExecute` | `true` | Associative hints on the latest message / after tool calls |
| `assoc.threshold` / `gap` / `maxItems` | `0.52` / `0.04` / `2` | Confidence gate |
| `tools.enabled` | `true` | Register the memory tools (+ `dream_now`) |
| `dream.enabled` | `true` | Scheduled consolidation |
| `dream.idleMs` / `maxIntervalMs` / `minPendingLogs` | `15 min` / `6 h` / `1` | Start after this much quiet, or at least this often while idle |
| `dream.llm` | `host` | `host` = harness model via `ctx.llm`; `openai` = sidecar calls an OpenAI-compatible API (`dream.openai.{baseUrl,apiKeyEnv,model}`) |
| `dream.maxIterations` | `60` | ReAct step cap per dream |
| `embed.provider` | `local` | `local` (ONNX) · `http` (OpenAI-compatible `/embeddings`, `embed.baseUrl`) · `off` |
| `logLevel` | `info` | Sidecar log level (forwarded to the harness logger) |

Dreams are preemptible: if any agent starts a turn mid-dream the sidecar is
told to stop, so the user never waits on consolidation. The `dream_now` tool
forces one from the conversation.

## How it plugs into DSH

* **Capture** — `session/event` → buffered → drained on `session/flush` /
  `turn/end`; `session/disposed` closes the log. Sub-agent sessions are skipped;
  the plugin's own injected hints are never echoed back.
* **Prompt** — `ctx.systemPrompt.section` × 3, cached and refreshed on
  `agent/created`, before each step that admits user input (so intention cues
  match the incoming message), after dreams and after intention tools.
* **Association** — `agent/pre-step` (appends a hint `UserMessage` to the
  admitted batch) and `tools/post-execute` (`additionalContexts`). These are
  the two hooks whose decision types can carry context; `tools/pre-execute`
  cannot and is not used.
* **Tools** — schemas are generated from the Python functions' docstrings and
  registered on `ctx.tools` at startup.
* **Dreaming** — `ctx.agents.list()` idle check + quiet timer; the sidecar's
  `sleep.run` drives a ReAct loop whose LLM calls come back over the RPC link
  as `llm.chat` and are served by `ctx.llm.stream` with
  `ctx.agentDefaultModel.currentSelection()`.
* **Sidecar** — spawned via `ctx.subprocess.spawn` (scrubbed env; only the
  keys the config names are forwarded), restarted with backoff on crash,
  shut down gracefully with the plugin.

## Development

```bash
npm install
npm run typecheck
npm test            # python: pytest (unit + real-stdio sidecar smoke) · ts: build + fake-harness e2e
```

`tests/smoke_plugin.mjs` runs the built plugin against a minimal fake harness
context and walks capture → tools → prompt refresh → session close → a full
dream through the host-LLM bridge. `PYTHON=python3.11 node tests/smoke_plugin.mjs`
to pick an interpreter.

Layout: `src/` (plugin), `python/somni_memory/` (memory core + sidecar),
`python/tests/`, `examples/`, `docs/`.

## Origin

The memory core is a decoupled fork of the "work / sleep" memory system built
for the *somni* Feishu agent — same on-disk format, decay curves and
consolidation strategy, minus the platform-specific parts.

## License

MIT
