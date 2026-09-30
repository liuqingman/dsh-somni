"""stdio ndjson JSON-RPC 2.0 —— 双向传输层。

- 每行一个 JSON 对象；stdin 收、stdout 发（因此进程内任何东西都不能往 stdout print，
  日志一律走 stderr）
- 同时扮演 server（TS → Python：`ping` / `session.*` / `tools.*` / `sleep.*` …）
  和 client（Python → TS：做梦时的 `llm.chat` 反向调用）
- 每个入站请求跑在独立 task 里，长任务（sleep.run）不会阻塞其它调用
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import traceback
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger("somni.rpc")

Handler = Callable[[dict], Union[Any, Awaitable[Any]]]

# 单行上限：一个 session-log 追加或一次 llm.chat 的 messages 可能有几百 KB
_LINE_LIMIT = 32 * 1024 * 1024

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    """handler 主动抛出，会原样映射成 JSON-RPC error。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        d: dict = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


class Rpc:
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = asyncio.Event()

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def _open_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_LINE_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        w_transport, w_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer  # type: ignore[attr-defined]
        )
        self._reader = reader
        self._writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)

    async def serve(self) -> None:
        """读 stdin 直到 EOF；EOF 视为宿主退出，取消所有在跑的 handler。"""
        await self._open_stdio()
        assert self._reader is not None
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    logger.error("rpc: 单行超过 %d 字节，丢弃", _LINE_LIMIT)
                    continue
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                self._dispatch_line(line)
        finally:
            self._closed.set()
            for t in list(self._tasks):
                t.cancel()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("rpc closed"))
            self._pending.clear()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # ── 入站 ────────────────────────────────────────────────────────────
    def _dispatch_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            self._spawn(self._send({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": PARSE_ERROR, "message": f"parse error: {e}"}}))
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            self._spawn(self._handle_request(msg))
        elif "id" in msg:
            self._resolve_response(msg)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {"_": params}
        handler = self.handlers.get(method or "")
        if handler is None:
            if req_id is not None:
                await self._send_error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")
            return
        try:
            result = handler(params)
            if inspect.isawaitable(result):
                result = await result
            if req_id is not None:
                await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except asyncio.CancelledError:
            if req_id is not None and not self.closed:
                await self._send_error(req_id, INTERNAL_ERROR, "cancelled")
            raise
        except RpcError as e:
            if req_id is not None:
                await self._send({"jsonrpc": "2.0", "id": req_id, "error": e.to_dict()})
        except Exception as e:  # noqa: BLE001
            logger.exception("rpc: handler %s 抛出异常", method)
            if req_id is not None:
                await self._send_error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}",
                                       data={"traceback": traceback.format_exc(limit=8)})

    def _resolve_response(self, msg: dict) -> None:
        fut = self._pending.pop(str(msg.get("id")), None)
        if fut is None or fut.done():
            return
        if "error" in msg and msg["error"] is not None:
            err = msg["error"] or {}
            fut.set_exception(RpcError(int(err.get("code", INTERNAL_ERROR)),
                                       str(err.get("message", "remote error")), err.get("data")))
        else:
            fut.set_result(msg.get("result"))

    # ── 出站 ────────────────────────────────────────────────────────────
    async def _send(self, obj: dict) -> None:
        if self._writer is None or self.closed:
            return
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            self._writer.write(data)
            try:
                await self._writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                self._closed.set()

    async def _send_error(self, req_id: Any, code: int, message: str, data: Any = None) -> None:
        await self._send({"jsonrpc": "2.0", "id": req_id, "error": RpcError(code, message, data).to_dict()})

    async def call(self, method: str, params: Optional[dict] = None, *, timeout: Optional[float] = None) -> Any:
        """反向调用宿主（TS 侧）。id 带 py- 前缀避免与宿主发起的请求撞号。"""
        if self.closed:
            raise ConnectionError("rpc closed")
        self._next_id += 1
        req_id = f"py-{self._next_id}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        try:
            return await (asyncio.wait_for(fut, timeout) if timeout else fut)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})
