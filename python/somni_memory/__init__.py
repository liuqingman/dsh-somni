"""somni_memory — 工作/睡眠双态记忆核心（dsh-somni 插件的 Python sidecar）。

模块分工：
- memory_manager : 记忆存储（knowledge / skill / episode / 画像）、衰减、检索、prompt 组装
- memory_indexer : 可选向量索引（本地 ONNX / OpenAI 兼容 HTTP），未装依赖时自动关闭
- sleep_agent    : 做梦（ReAct 循环），LLM 后端可插拔
- rpc            : stdio ndjson JSON-RPC 服务，供 TS 侧 dsh-somni 调用
"""

__version__ = "0.1.0"
