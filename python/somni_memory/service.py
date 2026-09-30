"""SomniService —— sidecar 对外的方法集合（挂到 Rpc.handlers）。

方法一览（TS → Python）：
  ping                      → {version, dataDir, llm, vector, pendingLogs}
  prompt.build              {speakerUserId?, participantUserIds?, currentMessage?}
                            → {identity, intentions, discipline}
  session.append            {sessionId, userId?, chatType?, role, text, speaker?}
  session.tool              {sessionId, userId?, call, result, reason?}
  session.close             {sessionId} → {path}
  session.closeAll          → {closed: n}
  assoc.thought             {sessionId, cue, iterNo?} → {items: [...], hint}
  assoc.action              {sessionId, toolName, args, iterNo?} → {items, hint}
  assoc.reset               {sessionId}
  tools.list                → {tools: [{name, description, parameters}]}
  tools.call                {name, args, sessionId?, userId?} → {result: str}
  sleep.run                 {trigger?} → run_sleep 返回的 dict（长任务；期间可能反向调 llm.chat）
  sleep.cancel              → {ok}
  sleep.status              → {sleeping, pendingLogs, llm}
  index.sync                {reason?} → {ok}
  shutdown                  → {ok}

反向（Python → TS）：
  llm.chat                  {messages, tools} → {message, usage}
                            仅 llm 模式为 "host" 时使用；messages/tools/message 均为 OpenAI Chat 格式
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from . import __version__
from . import assoc as assoc_mod
from . import memory_manager as mm
from . import sleep_agent
from .rpc import INVALID_PARAMS, Rpc, RpcError
from .toolspec import schema_from_function

logger = logging.getLogger("somni.service")

# 暴露给 Work Agent 的记忆工具（System-2：模型显式调用）
WORK_TOOLS = [
    mm.recall_memories,
    mm.search_episodes,
    mm.search_recent_conversation,
    mm.search_knowledge,
    mm.add_intention,
    mm.complete_intention,
    mm.cancel_intention,
    mm.list_intentions,
]

# llm.chat 反向调用的单次超时：做梦一轮推理可能很长（大 session-log + thinking model）
HOST_LLM_TIMEOUT = 600.0


def _req(params: dict, key: str) -> Any:
    v = params.get(key)
    if v is None or v == "":
        raise RpcError(INVALID_PARAMS, f"missing param: {key}")
    return v


class SomniService:
    def __init__(self, rpc: Rpc, *, llm_mode: str = "host",
                 assoc_threshold: float = 0.52, assoc_gap: float = 0.04, assoc_max_items: int = 2) -> None:
        self.rpc = rpc
        self.llm_mode = llm_mode
        self._assoc_cfg = dict(threshold=assoc_threshold, gap=assoc_gap, max_items=assoc_max_items)
        self._sessions: dict[str, mm.SessionLogWriter] = {}
        self._hooks: dict[str, assoc_mod.AssociationHook] = {}
        self._shutdown = asyncio.Event()
        if llm_mode == "host":
            sleep_agent.set_chat_backend(self._host_chat)
        self._register()

    # ── 注册 ────────────────────────────────────────────────────────────
    def _register(self) -> None:
        h = self.rpc.handlers
        h["ping"] = self.ping
        h["prompt.build"] = self.prompt_build
        h["session.append"] = self.session_append
        h["session.tool"] = self.session_tool
        h["session.close"] = self.session_close
        h["session.closeAll"] = self.session_close_all
        h["assoc.thought"] = self.assoc_thought
        h["assoc.action"] = self.assoc_action
        h["assoc.reset"] = self.assoc_reset
        h["tools.list"] = self.tools_list
        h["tools.call"] = self.tools_call
        h["sleep.run"] = self.sleep_run
        h["sleep.cancel"] = self.sleep_cancel
        h["sleep.status"] = self.sleep_status
        h["index.sync"] = self.index_sync
        h["shutdown"] = self.shutdown

    @property
    def shutdown_requested(self) -> asyncio.Event:
        return self._shutdown

    # ── 基础 ────────────────────────────────────────────────────────────
    async def ping(self, params: dict) -> dict:
        pending = await asyncio.to_thread(mm.get_pending_logs)
        return {
            "version": __version__,
            "dataDir": str(mm._default_data_root()),
            "memoryDir": str(mm._get_memory_dir()),
            "skillDir": str(mm._get_skill_dir()),
            "llm": self.llm_mode if sleep_agent.llm_available() else "none",
            "vector": assoc_mod.vector_layer_ready(),
            "pendingLogs": len(pending),
        }

    async def prompt_build(self, params: dict) -> dict:
        speaker = params.get("speakerUserId") or None
        participants = tuple(params.get("participantUserIds") or ())
        current = params.get("currentMessage") or ""

        def _build() -> dict:
            return {
                "identity": mm.build_identity_block(speaker_user_id=speaker, participant_user_ids=participants),
                "intentions": mm.build_intentions_block(current_message=current),
                "discipline": mm.build_recall_discipline(),
            }
        return await asyncio.to_thread(_build)

    # ── session-log 捕获 ───────────────────────────────────────────────
    def _writer(self, params: dict) -> mm.SessionLogWriter:
        sid = str(_req(params, "sessionId"))
        w = self._sessions.get(sid)
        if w is None:
            w = mm.SessionLogWriter(
                user_id=str(params.get("userId") or "default"),
                chat_id=sid,
                chat_type=params.get("chatType") or None,
            )
            self._sessions[sid] = w
            logger.info("session-log opened for %s", sid)
        return w

    async def session_append(self, params: dict) -> dict:
        role = str(_req(params, "role"))
        text = str(params.get("text") or "")
        if not text.strip():
            return {"ok": True, "skipped": "empty"}
        w = self._writer(params)
        await asyncio.to_thread(w.append, role, text, str(params.get("speaker") or ""))
        return {"ok": True}

    async def session_tool(self, params: dict) -> dict:
        w = self._writer(params)
        await asyncio.to_thread(w.append_tool, str(params.get("call") or ""),
                                str(params.get("result") or ""), str(params.get("reason") or ""))
        return {"ok": True}

    async def session_close(self, params: dict) -> dict:
        sid = str(_req(params, "sessionId"))
        w = self._sessions.pop(sid, None)
        self._hooks.pop(sid, None)
        if w is None:
            return {"path": None}
        path = await asyncio.to_thread(w.close)
        return {"path": str(path) if path else None}

    async def session_close_all(self, params: dict) -> dict:
        n = 0
        for sid in list(self._sessions):
            await self.session_close({"sessionId": sid})
            n += 1
        return {"closed": n}

    # ── System-1 联想 ──────────────────────────────────────────────────
    def _hook(self, params: dict) -> assoc_mod.AssociationHook:
        sid = str(params.get("sessionId") or "_")
        h = self._hooks.get(sid)
        if h is None:
            h = assoc_mod.AssociationHook(**self._assoc_cfg)
            self._hooks[sid] = h
        return h

    async def assoc_thought(self, params: dict) -> dict:
        hook = self._hook(params)
        items = await asyncio.to_thread(hook.associate, str(params.get("cue") or ""), "thought",
                                        int(params.get("iterNo") or 0))
        return {"items": items, "hint": assoc_mod.format_hint(items) if items else ""}

    async def assoc_action(self, params: dict) -> dict:
        name = str(params.get("toolName") or "")
        if not name or name in assoc_mod.SKIP_TOOLS:
            return {"items": [], "hint": ""}
        hook = self._hook(params)
        cue = assoc_mod.action_cue(name, params.get("args") or {})
        items = await asyncio.to_thread(hook.associate, cue, "action", int(params.get("iterNo") or 0))
        return {"items": items, "hint": assoc_mod.format_hint(items) if items else ""}

    async def assoc_reset(self, params: dict) -> dict:
        self._hook(params).reset()
        return {"ok": True}

    # ── System-2 工具 ──────────────────────────────────────────────────
    async def tools_list(self, params: dict) -> dict:
        return {"tools": [schema_from_function(fn) for fn in WORK_TOOLS]}

    async def tools_call(self, params: dict) -> dict:
        name = str(_req(params, "name"))
        fn = next((f for f in WORK_TOOLS if f.__name__ == name), None)
        if fn is None:
            raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")
        args = params.get("args") or {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "args must be an object")

        def _run() -> str:
            # search_recent_conversation 等依赖 contextvars 拿当前会话/用户
            mm._current_chat_id.set(str(params.get("sessionId") or ""))
            mm._current_user_id.set(str(params.get("userId") or ""))
            return str(fn(**args))
        try:
            result = await asyncio.to_thread(_run)
        except TypeError as e:  # 参数名不匹配
            raise RpcError(INVALID_PARAMS, f"{name}: {e}") from e
        return {"result": result}

    # ── 做梦 ────────────────────────────────────────────────────────────
    async def _host_chat(self, messages: list, tools: list) -> tuple:
        """ChatBackend：把一轮推理转给宿主（TS 侧 ctx.llm）。"""
        res = await self.rpc.call("llm.chat", {"messages": messages, "tools": tools}, timeout=HOST_LLM_TIMEOUT)
        if not isinstance(res, dict) or not isinstance(res.get("message"), dict):
            raise RuntimeError("llm.chat: host returned malformed result")
        return res["message"], res.get("usage") or {}

    async def sleep_run(self, params: dict) -> dict:
        trigger = str(params.get("trigger") or "host")
        result = await sleep_agent.run_sleep(trigger)
        # run_sleep 内部吞掉了 SleepInterruptedError 以外的异常并返回 status=error；这里只做转发
        return result

    async def sleep_cancel(self, params: dict) -> dict:
        await sleep_agent.cancel_sleep()
        return {"ok": True}

    async def sleep_status(self, params: dict) -> dict:
        pending = await asyncio.to_thread(mm.get_pending_logs)
        return {
            "sleeping": sleep_agent._sleep_lock.locked(),
            "pendingLogs": len(pending),
            "llm": self.llm_mode if sleep_agent.llm_available() else "none",
        }

    async def index_sync(self, params: dict) -> dict:
        from . import memory_indexer
        await asyncio.to_thread(memory_indexer.sync_quietly, str(params.get("reason") or "host"))
        return {"ok": True}

    async def shutdown(self, params: dict) -> dict:
        await self.session_close_all({})
        self._shutdown.set()
        return {"ok": True}
