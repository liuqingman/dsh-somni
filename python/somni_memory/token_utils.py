# -*- coding: utf-8 -*-
"""Token estimation utilities.

Provides a lightweight, dependency-free token counter:
- CJK characters (Chinese/Japanese/Korean) → 2 tokens per character
- Everything else (Latin, digits, punctuation, spaces) → 1 token per 4 characters

This is an *estimate*. For precise counts, use the model API ``usage`` field
(which DashScope returns in the last streaming chunk).
"""
import re
from typing import Any, Union

# CJK Unified Ideographs + Extension A + Compatibility + Kana + Bopomofo + Hangul
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff"   # CJK Unified Ideographs
    r"\u3400-\u4dbf"    # CJK Extension A
    r"\uf900-\ufaff"    # CJK Compatibility Ideographs
    r"\u3040-\u30ff"    # Hiragana + Katakana
    r"\u3100-\u312f"    # Bopomofo
    r"\uac00-\ud7af]",  # Hangul Syllables
)


def estimate_tokens(text: str) -> int:
    """Estimate token count for *text*.

    Args:
        text (`str`):
            The input text to estimate.

    Returns:
        `int`:
            Estimated token count.
    """
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub("", text)
    non_cjk_tokens = max(1, len(non_cjk) // 4) if non_cjk.strip() else 0
    return cjk_count * 2 + non_cjk_tokens


def extract_text_from_content(content: Any) -> str:
    """Extract plain text from AgentScope ``Msg.content``.

    ``content`` may be a plain ``str``, a ``list`` of block dicts, or a
    single block dict.  This helper normalises all forms into a flat string
    suitable for :func:`estimate_tokens`.

    Args:
        content (`Any`):
            The ``Msg.content`` value (str / list / dict).

    Returns:
        `str`:
            Concatenated plain text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", b.get("content", ""))
            if isinstance(b, dict)
            else str(b)
            for b in content
        )
    if isinstance(content, dict):
        return content.get("text", content.get("content", ""))
    return ""
