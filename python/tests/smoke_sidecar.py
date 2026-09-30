"""端到端冒烟：真实 stdio 管道拉起 sidecar，走一遍 ping / session / tools / prompt / sleep。

sleep.run 用一个假宿主 LLM 应答 llm.chat（先调 mark_log_processed + write_dream_log，再收尾），
验证反向桥 + ReAct 循环 + 文件落盘全链路。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]


class Host:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self._id = 0
        self._pending: dict = {}
        self.llm_calls = 0
        self._reader_task = asyncio.create_task(self._read())

    async def _read(self):
        assert self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if "method" in msg:
                await self._handle_reverse(msg)
            else:
                fut = self._pending.pop(msg["id"], None)
                if fut:
                    fut.set_result(msg)

    async def _handle_reverse(self, msg):
        assert msg["method"] == "llm.chat", msg
        self.llm_calls += 1
        messages = msg["params"]["messages"]
        tool_names = [t["function"]["name"] for t in msg["params"]["tools"]]
        assert "mark_log_processed" in tool_names and "write_dream_log" in tool_names
        # 第 1 轮：读日志；第 2 轮：标记处理；第 3 轮：写 dream-log；第 4 轮：收尾文本
        pending_path = None
        for m in messages:
            if m["role"] == "user" and "session-log" in (m.get("content") or ""):
                for tok in m["content"].split():
                    if tok.endswith(".md") and "/session-log/" in tok:
                        pending_path = tok.strip("`,;")
        step = self.llm_calls
        if step == 1:
            reply = self._tool("read_session_log", {"path": pending_path})
        elif step == 2:
            reply = self._tool("mark_log_processed", {"path": pending_path})
        elif step == 3:
            reply = self._tool("write_dream_log", {"content": "---\ndream_id: smoke\n---\n\n## 提取结果\n（冒烟）"})
        else:
            reply = {"role": "assistant", "content": "## 🌙 睡梦总结\n\n**处理日志**: 1 个 session-log\n"}
        await self._send({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"message": reply, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}})

    def _tool(self, name, args):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": f"call_{self.llm_calls}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}

    async def _send(self, obj):
        assert self.proc.stdin
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, method, params=None, timeout=60):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]


async def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="somni-smoke-"))
    env = {**os.environ, "SOMNI_DATA_DIR": str(data_dir), "SOMNI_EMBED_PROVIDER": "off",
           "PYTHONPATH": str(ROOT)}
    proc = await asyncio.create_subprocess_exec(
        PY, "-m", "somni_memory", "--llm", "host", "--log-level", "warning",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=sys.stderr, env=env, cwd=str(ROOT),
    )
    host = Host(proc)
    try:
        pong = await host.call("ping")
        assert pong["llm"] == "host" and pong["pendingLogs"] == 0, pong
        print("ping ok:", pong)

        sid = "sess-1"
        await host.call("session.append", {"sessionId": sid, "userId": "u1", "role": "user", "text": "帮我看下 nginx 502"})
        await host.call("session.append", {"sessionId": sid, "role": "agent", "text": "先看 upstream 日志"})
        await host.call("session.tool", {"sessionId": sid, "call": "exec-shell(tail error.log)", "result": "upstream timed out", "reason": "看错误"})
        closed = await host.call("session.close", {"sessionId": sid})
        assert closed["path"] and Path(closed["path"]).exists(), closed
        body = Path(closed["path"]).read_text(encoding="utf-8")
        assert "status: pending" in body and "[tool]" in body and "nginx 502" in body
        print("session ok:", closed["path"])

        tools = (await host.call("tools.list"))["tools"]
        names = {t["name"] for t in tools}
        assert {"recall_memories", "search_episodes", "search_recent_conversation", "add_intention"} <= names, names
        lim = next(t for t in tools if t["name"] == "recall_memories")["parameters"]["properties"]["limit"]
        assert lim["type"] == "integer", lim
        print("tools.list ok:", sorted(names))

        r = await host.call("tools.call", {"name": "add_intention", "args": {"what": "复查 nginx 超时", "cue": "nginx,502"}})
        assert "pi-" in r["result"], r
        r = await host.call("tools.call", {"name": "list_intentions", "args": {}})
        assert "nginx" in r["result"]
        r = await host.call("tools.call", {"name": "search_recent_conversation", "args": {"query": "nginx"}, "sessionId": sid})
        assert "502" in r["result"], r
        print("tools.call ok")

        p = await host.call("prompt.build", {"currentMessage": "nginx 又 502 了"})
        assert "回忆纪律" in p["discipline"] and "nginx" in p["intentions"], p
        print("prompt.build ok (intentions cue fired)")

        a = await host.call("assoc.thought", {"sessionId": sid, "cue": "nginx upstream timed out 怎么排查"})
        assert a["items"] == [] and a["hint"] == ""  # 向量层 off → 不联想
        print("assoc ok (vector off → silent)")

        st = await host.call("sleep.status")
        assert st["pendingLogs"] == 1 and not st["sleeping"], st
        res = await host.call("sleep.run", {"trigger": "smoke"}, timeout=120)
        print("sleep.run:", {k: v for k, v in res.items() if k != "summary"})
        assert res.get("status") == "deep_sleep", res
        assert host.llm_calls >= 4, host.llm_calls
        assert not (data_dir / "memory" / "session-log").glob("*.md") or \
            not list((data_dir / "memory" / "session-log").glob("*.md")), "log not archived"
        assert list((data_dir / "memory" / "dream-log").glob("*.md")), "dream-log missing"
        print("sleep ok: llm_calls =", host.llm_calls)

        await host.call("shutdown")
        await asyncio.wait_for(proc.wait(), 10)
        assert proc.returncode == 0, proc.returncode
        print("ALL OK")
        return 0
    finally:
        if proc.returncode is None:
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
