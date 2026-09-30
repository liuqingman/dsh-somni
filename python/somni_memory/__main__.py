"""python -m somni_memory —— 以 stdio JSON-RPC sidecar 方式启动。

用法（由 dsh-somni 的 TS 侧拉起，也可手工调试）：
    python -m somni_memory --data-dir ~/.dsh/somni --llm host --log-level info

所有目录/开关参数都在 import 记忆模块之前写入环境变量，因为 memory_manager /
sleep_agent 在导入期就会解析 SOMNI_DATA_DIR 等。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="somni-memory", description="dsh-somni Python sidecar (stdio JSON-RPC)")
    p.add_argument("--data-dir", default=os.environ.get("SOMNI_DATA_DIR", ""),
                   help="数据根目录（memory/ skills/ models/ 都在其下），默认 ~/.dsh/somni")
    p.add_argument("--llm", choices=("host", "openai"), default="host",
                   help="做梦用的 LLM：host=反向调用宿主 ctx.llm；openai=用 SLEEP_*/OPENAI_* 环境变量直连")
    p.add_argument("--embed", choices=("local", "http", "off"), default=None,
                   help="向量层 provider（写入 SOMNI_EMBED_PROVIDER）")
    p.add_argument("--embed-model-dir", default=None, help="本地 ONNX 模型目录（写入 SOMNI_EMBED_MODEL_PATH）")
    p.add_argument("--assoc-threshold", type=float, default=0.52)
    p.add_argument("--assoc-gap", type=float, default=0.04)
    p.add_argument("--assoc-max-items", type=int, default=2)
    p.add_argument("--log-level", default=os.environ.get("SOMNI_LOG_LEVEL", "info"))
    return p.parse_args(argv)


def _apply_env(args: argparse.Namespace) -> None:
    if args.data_dir:
        os.environ["SOMNI_DATA_DIR"] = str(Path(args.data_dir).expanduser())
    if args.embed:
        os.environ["SOMNI_EMBED_PROVIDER"] = args.embed
    if args.embed_model_dir:
        os.environ["SOMNI_EMBED_MODEL_PATH"] = str(Path(args.embed_model_dir).expanduser())


def _setup_logging(level: str) -> None:
    # stdout 是 RPC 通道，日志只能走 stderr
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _amain(args: argparse.Namespace) -> int:
    from .rpc import Rpc
    from .service import SomniService

    rpc = Rpc()
    service = SomniService(
        rpc, llm_mode=args.llm,
        assoc_threshold=args.assoc_threshold, assoc_gap=args.assoc_gap, assoc_max_items=args.assoc_max_items,
    )

    loop = asyncio.get_running_loop()
    serve_task = loop.create_task(rpc.serve())
    stop = loop.create_task(service.shutdown_requested.wait())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.shutdown_requested.set)
        except (NotImplementedError, RuntimeError):
            pass

    done, _ = await asyncio.wait({serve_task, stop}, return_when=asyncio.FIRST_COMPLETED)
    if stop in done:
        # 主动关闭：先把在写的 session-log 收尾，再停止读 stdin
        await service.session_close_all({})
        serve_task.cancel()
    else:
        # stdin EOF：宿主没了，尽量收尾（写盘是本地操作，不依赖宿主）
        try:
            await service.session_close_all({})
        except Exception:  # noqa: BLE001
            pass
    for t in (serve_task, stop):
        if not t.done():
            t.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _apply_env(args)
    _setup_logging(args.log_level)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
