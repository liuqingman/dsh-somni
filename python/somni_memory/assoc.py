"""System-1 记忆联想钩子（从 somni2 somni_core.AssociationHook 移植，去掉框架依赖）。

把"当前在想什么/要做什么"当 cue，走 memory_manager.semantic_search 查 knowledge/skill
top-3，过绝对阈值 + 置信间隙两道门才注入；同一条记忆一个会话只提示一次（习惯化）。

实测（bge-small-zh-v1.5，346 条）：纯 cosine 对 shell 命令类 cue 不可分（ls/cat/docker
全 0.63~0.69），混合分噪声底 ≈0.45~0.48、真命中 0.55~0.80，故默认阈值取混合分 0.52。
向量层不可用时 semantic_search 退化成纯关键词，钩子场景噪声太大 → 直接不联想。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import memory_manager as mm

logger = logging.getLogger("somni.assoc")

TOPK = 3
CUE_MAX = 600
MIN_CUE = 8

# 这些工具本身就是在查/读记忆，再联想一遍只会重复
SKIP_TOOLS = frozenset({
    "recall_memories", "search_knowledge", "search_episodes", "search_skills",
    "search_recent_conversation", "list_intentions", "add_intention",
    "complete_intention", "cancel_intention",
})


def vector_layer_ready() -> bool:
    try:
        return mm._vec_index() is not None
    except Exception:
        return False


def action_cue(tool_name: str, args: Optional[dict]) -> str:
    """动作 cue：shell 类工具只取命令串（工具名会把所有 shell 知识都拉高，不带），
    其余取 '工具名: 紧凑 JSON 参数'。"""
    args = args or {}
    for key in ("command", "cmd", "query", "url", "file_path", "path", "pattern"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        s = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(args)
    return f"{tool_name}: {s}" if s and s != "{}" else tool_name


class AssociationHook:
    def __init__(self, *, threshold: float = 0.52, gap: float = 0.04, max_items: int = 2) -> None:
        self.threshold = float(threshold)
        self.gap = float(gap)
        self.max_items = max(1, int(max_items))
        self._recent: dict[str, int] = {}  # path → 首次注入的轮次
        self.stats = {"fired": 0, "injected": 0, "below": 0, "gap_reject": 0,
                      "habituated": 0, "errors": 0}

    def reset(self) -> None:
        self._recent.clear()

    def associate(self, cue: str, kind: str = "thought", iter_no: int = 0) -> list[dict]:
        """返回被选中的条目（可能为空）。每项含 name/description/path/score/applies_when/not_applies_when。"""
        cue = (cue or "").strip()[:CUE_MAX]
        if len(cue) < MIN_CUE or not vector_layer_ready():
            return []
        t0 = time.perf_counter()
        try:
            rows = mm.semantic_search(cue, types=("knowledge", "skill"), limit=TOPK)
        except Exception:
            self.stats["errors"] += 1
            logger.debug("[assoc] %s 检索异常", kind, exc_info=True)
            return []
        ms = (time.perf_counter() - t0) * 1000
        self.stats["fired"] += 1

        def _sc(r: dict) -> float:
            return float(r.get("score", r.get("cos", 0)) or 0)

        passed = [r for r in rows if _sc(r) >= self.threshold]
        habituated = [r for r in passed if r["path"] in self._recent]
        cands = [r for r in passed if r["path"] not in self._recent]

        chosen: list[dict] = []
        if not cands:
            if habituated:
                self.stats["habituated"] += 1
                decision = f"habituated({habituated[0]['name']})"
            else:
                self.stats["below"] += 1
                decision = "below"
        elif len(cands) == 1 or _sc(cands[0]) - _sc(cands[1]) >= self.gap:
            chosen = cands[:1]
            decision = "inject1"
        elif len(cands) == 2 and self.max_items >= 2:
            chosen = cands[:2]  # 两条同等相关，都给
            decision = "inject2"
        else:
            self.stats["gap_reject"] += 1  # 三条以上分不清，不猜
            decision = "gap_reject"

        logger.info("[assoc] %s iter=%d %s top=[%s] %.0fms cue=%r",
                    kind, iter_no, decision, ",".join(f"{_sc(r):.2f}" for r in rows), ms, cue[:80])
        if not chosen:
            return []
        self.stats["injected"] += len(chosen)
        for r in chosen:
            self._recent[r["path"]] = iter_no
        return [
            {
                "name": r.get("name", ""),
                "description": (r.get("description") or "").strip(),
                "path": str(r.get("path", "")),
                "type": r.get("entry_type", ""),
                "score": round(_sc(r), 3),
                "applies_when": r.get("applies_when") or "",
                "not_applies_when": r.get("not_applies_when") or "",
            }
            for r in chosen
        ]


def format_hint(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        s = f"【记忆提示】{r.get('name', '')}: {r.get('description', '')}"
        if r.get("applies_when"):
            s += f"｜适用：{r['applies_when']}"
        if r.get("not_applies_when"):
            s += f"｜不适用：{r['not_applies_when']}"
        if r.get("path"):
            s += f"（详见 {r['path']}）"
        lines.append(s)
    return "\n".join(lines)
