"""从 Python 函数签名 + Google 风格 docstring 生成 JSON-Schema 工具描述。

供 service.tools.list 使用：把 memory_manager 里给 Work Agent 用的记忆工具原样暴露给宿主
（描述取 docstring 中 Args: 之前的全部文字——recall_memories 等工具的"怎么写 query"引导都在那里）。
"""
from __future__ import annotations

import inspect
from typing import Callable

_TYPE_MAP = {int: "integer", float: "number", bool: "boolean", str: "string",
             # 模块带 from __future__ import annotations 时 signature 里拿到的是字符串
             "int": "integer", "float": "number", "bool": "boolean", "str": "string"}


def _param_docs(doc: str) -> dict[str, str]:
    """解析 Args: 段，返回 {参数名: 描述}（支持续行缩进）。"""
    out: dict[str, str] = {}
    lines = doc.splitlines()
    in_args = False
    current = ""
    for raw in lines:
        s = raw.strip()
        if s.startswith("Args:"):
            in_args = True
            continue
        if in_args and (s.startswith("Returns:") or s.startswith("Raises:")):
            break
        if not in_args or not s:
            continue
        head, sep, rest = s.partition(":")
        if sep and " " not in head.strip() and head.strip().isidentifier():
            current = head.strip()
            out[current] = rest.strip()
        elif current:
            out[current] += " " + s
    return out


def _description(doc: str) -> str:
    """docstring 中 Args:/Returns: 之前的全部文字（去缩进、保留段落换行）。"""
    text = inspect.cleandoc(doc)
    for marker in ("\nArgs:", "\nReturns:", "\nRaises:"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    return text.strip()


def schema_from_function(fn: Callable, *, name: str | None = None) -> dict:
    """返回 {"name", "description", "parameters"}（OpenAI function 参数风格的 JSON Schema）。"""
    sig = inspect.signature(fn)
    doc = fn.__doc__ or ""
    pdocs = _param_docs(doc)
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        ann = param.annotation
        ptype = _TYPE_MAP.get(ann, "string") if ann is not inspect.Parameter.empty else "string"
        spec: dict = {"type": ptype, "description": pdocs.get(pname, "")}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        elif param.default not in (None, ""):
            spec["default"] = param.default
        props[pname] = spec
    return {
        "name": name or fn.__name__,
        "description": _description(doc) or (name or fn.__name__),
        "parameters": {"type": "object", "properties": props, "required": required},
    }
