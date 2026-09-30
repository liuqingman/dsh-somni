"""toolspec：从 docstring + 签名生成 JSON Schema（含 `from __future__ import annotations` 的字符串注解）。"""
from __future__ import annotations

from somni_memory.toolspec import schema_from_function


def _sample(query: str, types: str = "", limit: int = 5, strict: bool = False, weight: float = 0.5) -> str:
    """检索记忆。

    第二行不该进 description。

    Args:
        query: 检索词
        types: 逗号分隔的类型过滤
        limit: 返回条数
        strict: 是否严格匹配
        weight: 权重

    Returns:
        文本
    """
    return query


def test_schema_shape_and_types():
    spec = schema_from_function(_sample)
    assert spec["name"] == "_sample"
    assert spec["description"].startswith("检索记忆。")
    assert "Returns" not in spec["description"] and "Args" not in spec["description"]
    props = spec["parameters"]["properties"]
    assert props["query"]["type"] == "string" and props["query"]["description"] == "检索词"
    assert props["limit"]["type"] == "integer" and props["limit"]["default"] == 5
    assert props["strict"]["type"] == "boolean"
    assert props["weight"]["type"] == "number"
    assert spec["parameters"]["required"] == ["query"]
    # 空字符串默认不写入 default，避免模型误传 ""
    assert "default" not in props["types"]


def test_name_override():
    assert schema_from_function(_sample, name="recall")["name"] == "recall"
