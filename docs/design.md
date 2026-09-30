# dsh-somni — design notes

This document explains *why* the plugin is shaped the way it is. For usage see
the [README](../README.md).

## 1. Two halves, one process boundary

```
DSH (Node)                                 sidecar (Python)
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ src/index.ts     wiring      │            │ __main__.py   argv → env      │
│ src/sidecar.ts   RPC client  │◀─ stdio ──▶│ rpc.py        ndjson JSON-RPC │
│ src/capture.ts   sessions    │  ndjson    │ service.py    method table    │
│ src/prompt.ts    sections    │            │ memory_manager.py  core       │
│ src/assoc.ts     hints       │            │ assoc.py      System-1        │
│ src/tools.ts     ctx.tools   │            │ sleep_agent.py  dreamer       │
│ src/dream.ts     scheduler   │            │ memory_indexer.py  vectors    │
│ src/llm-bridge.ts ctx.llm    │            │ toolspec.py   docstring→schema│
└──────────────────────────────┘            └──────────────────────────────┘
```

The memory core (decay curves, Markdown/YAML store, hybrid scoring, the dream
ReAct loop) is Python and was already battle-tested in the *somni* agent. The
harness is TypeScript. Rather than port ~2k lines and lose parity, the plugin
keeps the core as a **sidecar** and puts everything harness-specific (hooks,
prompt sections, tool registration, scheduling) in TypeScript.

Transport is **newline-delimited JSON-RPC 2.0 over stdin/stdout**, in both
directions. Reasons:

* `ctx.subprocess.spawn` gives us pipes, a scrubbed env, grace-period
  termination and lifecycle tracking for free — no ports, no sockets, nothing
  to leak when the plugin is unloaded.
* Both sides need to *call* the other: the host drives memory operations; the
  sidecar, while dreaming, calls back for LLM completions (`llm.chat`). One
  duplex line protocol handles both.
* The sidecar's stdout is the channel, so **all Python logging goes to stderr**
  and is re-emitted through `ctx.logger('somni:py')` at the matching level.

### 1.1 Method table

Host → sidecar:

| Method | Params | Returns | Notes |
|---|---|---|---|
| `ping` | – | `{version, dataDir, memoryDir, skillDir, llm, vector, pendingLogs}` | Used as the readiness probe at startup |
| `prompt.build` | `{speakerUserId?, participantUserIds?, currentMessage?}` | `{identity, intentions, discipline}` | `currentMessage` lets cue-based intentions match the incoming text |
| `session.append` | `{sessionId, userId?, chatType?, role, text, speaker?}` | `{ok}` | Opens the session-log lazily |
| `session.tool` | `{sessionId, call, result, reason?}` | `{ok}` | One line per tool call |
| `session.close` | `{sessionId}` | `{path}` | Finalises the `.md`, `status: pending` |
| `session.closeAll` | – | `{closed}` | Before a dream, so the dreamer sees everything |
| `assoc.thought` | `{sessionId, cue, iterNo}` | `{items, hint}` | System-1 on the latest user message |
| `assoc.action` | `{sessionId, toolName, args, iterNo}` | `{items, hint}` | System-1 after a tool ran; memory tools themselves are skipped |
| `assoc.reset` | `{sessionId}` | `{ok}` | Clears habituation state |
| `tools.list` | – | `{tools: ToolSchema[]}` | Schemas generated from docstrings |
| `tools.call` | `{name, args, sessionId?, userId?}` | `{result}` | Sets contextvars so `search_recent_conversation` knows the session |
| `sleep.run` | `{trigger}` | `{status, …}` | `deep_sleep · skipped · interrupted · error` |
| `sleep.cancel` | – | `{ok}` | Cooperative: the dreamer checks a flag between tool calls |
| `sleep.status` | – | `{sleeping, pendingLogs, llm}` | |
| `index.sync` | `{reason}` | `{ok}` | Rebuild vector index for changed files |
| `shutdown` | – | `{ok}` | Closes logs, exits the serve loop |

Sidecar → host:

| Method | Params | Returns |
|---|---|---|
| `llm.chat` | `{messages: OpenAI-style[], tools: OpenAI-style[]}` | `{message: {role, content, reasoning_content?, tool_calls?}, usage: {prompt_tokens, completion_tokens, cached_tokens?}}` |

The wire format for `llm.chat` is deliberately the OpenAI chat-completions
shape: `sleep_agent.py` already spoke it, so `dream.llm: openai` (sidecar calls
an API directly) and `dream.llm: host` (sidecar calls the harness) are the same
code path with a different `ChatBackend`.

### 1.2 Lifecycle and failure

* `Sidecar.start()` is memoised: spawn → wait for `ping` (configurable timeout)
  → resolve. Everyone who needs the sidecar awaits `start()`; nothing races the
  process.
* A crash rejects every in-flight request with `SidecarError`, then restarts
  with backoff (`2 s × n`, at most 3 times). During restart, callers see errors
  and degrade: prompt sections keep their last text, hints stay silent, tools
  return an error string.
* `stop()` sends `shutdown`, waits up to 5 s, then lets `ctx.subprocess`
  terminate. The disposer returned from `ctx.effect` orders it: stop dreamer →
  close captured sessions → stop sidecar.
* The env passed to the sidecar is *only* what the config names: `SLEEP_*`
  when `dream.llm: openai`, `SOMNI_EMBED_BASE_URL` when `embed.provider: http`,
  plus `PYTHONPATH` / `PYTHONUNBUFFERED` / `PYTHONIOENCODING`.

## 2. Hook choices

DSH exposes several waterfall hooks. The plugin uses exactly the ones whose
decision type can carry content into the conversation:

| Hook | Decision type | Used for |
|---|---|---|
| `agent/pre-step` | `{kind:'enter', messages}` | Refresh prompt with the incoming text, then append an associative hint as one more `UserMessage` |
| `tools/post-execute` | `{kind:'accept', additionalContexts?: UserMessage[]}` | Hint triggered by the tool that just ran |
| `tools/pre-execute` | `{kind:'accept'} \| {kind:'reject'}` | **Not used** — no field for context; a hint here would have to reject the call or be dropped |

Both hooks call `next()` first and only decorate an *enter/accept* decision, so
another plugin's reject wins and the hint is never attached to a rejected step.

Hints are `UserMessage`s with `source.kind === 'user'` because that is the only
source the request builder accepts for ad-hoc context. To avoid the hint being
captured back into the session-log (and dreamed on, and hinted again),
`capture.ts` keeps a `WeakSet`-like set of injected message ids and skips them
on `user/message`.

### 2.1 Prompt sections

Three `ctx.systemPrompt.section` registrations with `interpolate: false`
(memory text may legitimately contain `{{…}}`):

| Section | Order | Content |
|---|---|---|
| `somni:identity` | 100 | `about_me.md`, `about_user.md`, the speaker's profile |
| `somni:intentions` | 130 | Due intentions, cue-matched intentions, then the rest |
| `somni:discipline` | 1200 | Short "when to recall / when not to" rules |

Sections return cached text; the cache is refreshed on `agent/created`, in
`agent/pre-step` when user input is admitted (so cue matching sees the latest
message), after any intention tool, and at the end of every dream. Refreshes
share one in-flight promise so a burst of steps costs one RPC. Empty sections
are dropped by DSH, so a fresh install adds nothing to the prompt.

### 2.2 Tools

`tools.list` is fetched once after the sidecar is up; each schema becomes a
`ToolDefinition` whose `execute` is a thin `tools.call`. Two details:

* `isConcurrencySafe` is `false` for `add/complete/cancel_intention` (they
  rewrite files under `memory/intentions/`) and `true` for the read-only
  searches.
* Output is a single text block; the Python side already renders Markdown
  intended for the model.

`dream_now` is registered separately in `dream.ts` so it can share the
`Dreamer`'s in-flight state and preemption logic.

## 3. Dreaming

### 3.1 Why not `agent.runMaintenance`

`runMaintenance` runs a task while holding an agent's inbox, which is right for
a few seconds of housekeeping and wrong for a consolidation pass that may take
minutes of LLM calls. The plugin instead:

1. Tracks `lastActivity` from `agent/status` and `session/event`.
2. Every 30 s: if no dream is running, the sidecar is alive, **every** agent in
   `ctx.agents.list()` is `idle`, and either `quiet ≥ dream.idleMs` or
   `sinceLastDream ≥ dream.maxIntervalMs`, it calls `session.closeAll`, checks
   `sleep.status.pendingLogs ≥ dream.minPendingLogs`, and starts `sleep.run`.
3. If any agent flips to `running` mid-dream, it sends `sleep.cancel`. The
   Python loop checks the flag between tool calls and returns
   `status: interrupted`; processed logs stay processed, unprocessed ones wait
   for the next dream.

The user therefore never waits on a dream, and a dream never observes a
half-written session-log.

### 3.2 What a dream does

`sleep.run` drives a ReAct loop (`SLEEP_MAX_ITERS`, default 60) over the
pending `session-log/*.md` with these tools: `read_session_log`, `list_items`,
`read_item`, `create_knowledge`, `update_knowledge`, `create_skill`,
`update_skill`, `archive_item`, `record_episode`, `mark_log_processed`,
`write_dream_log`, `decay_all_knowledge`, `review_dream_history`,
`update_about_me`, `update_about_user`. The extraction guidance lives in
`python/somni_memory/prompts/memory-extraction.md`.

Every tool is wrapped with a cancellation check; `mark_log_processed` moves
the log to `archived/` so a partial dream is safe to resume.

### 3.3 Host LLM bridge

`llm-bridge.ts` converts OpenAI-shaped requests to `ctx.llm.stream`:

* `system` → `GenerateOptions.system`; `user` → `RequestUserInput`;
  `assistant` → `createAssistantMessage` with reasoning / text / tool-call
  blocks; `tool` → `createToolResultMessage`.
* `{type:'function', function:{name, description, parameters}}` →
  `ToolSchema`.
* Provider/model come from `ctx.agentDefaultModel.currentSelection()` so the
  dreamer uses whatever the user has selected in the harness.
* Chunks are assembled from `block-end` blocks with a text/tool-call-delta
  fallback; `usage` maps to `prompt_tokens / completion_tokens / cached_tokens`.

`dream.llm: openai` bypasses the bridge for deployments that want a cheaper or
separate model for consolidation.

## 4. Memory model on disk

```
<dataDir>/
  memory/
    about_me.md  about_user.md  users/<id>/about_user.md
    knowledge/*.md        semantic   (type: fact|decision|pitfall|preference…)
    episodes/*.md         episodic   (salience: high|mid|low)
    intentions/*.md       prospective (cue, due, priority, status)
    session-log/*.md      working    (status: pending → processed)
    dream-log/*.md        the dreamer's own diary
    archived/             retired or processed items
  skills/agent_learned_skills/<name>/SKILL.md   procedural
  models/bge-small-zh-v1.5/{model.onnx,tokenizer.json}
```

Every entry is Markdown with YAML front-matter carrying `score`, `stability`,
`created`, `last_used`, `last_decay`. Unknown front-matter keys are preserved
on rewrite.

### 4.1 Decay and retirement

Applied once per dream by `decay_all_knowledge`:

```
daily_decay = 1 − base / (1 + stability × 0.5)
score      *= daily_decay ^ days_since_last_decay
```

* `base` = `0.1` for semantic entries, `0.25` for episodes; episodes with
  `salience: high` use `base × 0.4`, `mid` uses `base × 0.7`.
* `stability` grows when an entry is recalled or re-confirmed, flattening the
  curve — the spaced-repetition intuition.
* Entries with `score < 0.2` are moved to `archived/`.

### 4.2 Recall scoring

`recall_memories` / associative hints use one scorer: keyword overlap on
name + description + tags, plus cosine similarity from the vector index when
available, weighted by `score`, with one hop of link spreading
(`_LINK_SPREAD_DECAY = 0.6`) across `related:` links. The vector index is
rebuilt incrementally (`index.sync`) and persisted under `dataDir`.

### 4.3 Associative gating

Candidates are the scored rows with `score ≥ assoc.threshold` (`0.52`) that
have **not** already been hinted in this session (per-session habituation —
the same memory is shown once per conversation, reset on `session/disposed`).
Then:

* one candidate, or the top two separated by `≥ assoc.gap` (`0.04`) → inject
  the top one;
* exactly two candidates too close to separate → inject both (if
  `assoc.maxItems ≥ 2`; they are equally relevant);
* three or more too close to separate → inject nothing — the cue is ambiguous
  and guessing costs more than staying silent.

Chosen items are formatted into one `【记忆提示】` block. Without a vector layer, `assoc.py` reports
`vector_layer_ready() == false` and both hooks return empty — keyword-only
scores are too noisy to inject unprompted.

## 5. Capture semantics

`capture.ts` turns `session/event` into session-log lines:

| Session event | Log entry |
|---|---|
| `user/message` (source `user`, not injected) | `user:` text |
| `assistant/message` | `agent:` text |
| `tool/call` | remembered by `callId` as `name(args)` |
| `tool/result` | `tool:` `call → result` or `[error] name: reason` |
| `turn/end`, `session/flush` | drain buffer to the sidecar |
| `session/disposed` | `session.close` (skipped if nothing was captured) |

Sessions with `header.origin === 'subagent'` are ignored entirely — their
content is summarised in the parent's tool result anyway. Per-message text is
capped at `capture.maxMessageChars`.

## 6. Testing strategy

* `python/tests/test_toolspec.py` — docstring → JSON Schema generator.
* `python/tests/test_sidecar.py` — the real `python -m somni_memory` over
  stdio with a fake host answering `llm.chat`; exercises every RPC method and a
  full dream.
* `tests/smoke_plugin.mjs` — the *built* plugin against a minimal fake harness
  `ctx` (`on/effect/logger/subprocess/systemPrompt/tools/agents/llm`);
  exercises hook wiring, section refresh, tool registration, capture →
  session-log on disk, and a dream that round-trips through the host-LLM
  bridge.

Neither test needs network or a model: embeddings fall back to keyword mode,
and the fake LLM yields a scripted tool-call sequence.

## 7. Non-goals

* No cross-process locking on `dataDir`: one harness instance per data
  directory.
* No migration of an existing somni deployment's `memory/` (the format is the
  same, so copying the directory works, but nothing automates it).
* No UI; the files are the UI.
