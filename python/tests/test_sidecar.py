"""pytest 入口：把端到端冒烟（真实 stdio sidecar + 假宿主 LLM）纳入测试集。"""
from __future__ import annotations

import asyncio

from smoke_sidecar import main as smoke_main


def test_sidecar_end_to_end():
    assert asyncio.run(smoke_main()) == 0
