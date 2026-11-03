# -*- coding: utf-8 -*-
"""Sleep Agent — 做梦模块（自实现 ReAct 循环版本）。

基于 DESIGN.md 设计方案实现：
- Sleep Agent 使用自实现的简化 ReAct 循环（非 AgentScope ReActAgent）
- 13 个专用工具封装 memory_manager 底层操作，LLM 自主决策记忆提取/修正/衰减
- 统一走 Deep Sleep 模式，每次做梦完整处理所有 pending session-log
- 支持可靠打断机制：通过 SleepInterruptedError 异常强制终止循环
- 使用独立的 SLEEP_MODEL（默认 qwen-plus）降低成本
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    from . import memory_manager as mm
    from .token_utils import estimate_tokens
except ImportError:
    import memory_manager as mm
    from token_utils import estimate_tokens

logger = logging.getLogger("sleep_agent")

# 空闲触发延迟（秒）
IDLE_DELAY = int(os.environ.get("SLEEP_IDLE_DELAY", "30"))

# ── Token 优化相关配置（schemes A/B） ──────────────────────────────────────
# 总开关：设为 "0" 可整体关闭 A/B/C 优化（用于对照测量或紧急回退）
TOKEN_OPT_ENABLED = os.environ.get("SLEEP_TOKEN_OPT", "1") != "0"
# 四阶段上下文共享：①②③ 段间续用同一 messages（review 始终冷启动隔离）；0=回退旧摘要链
SHARED_CTX_ENABLED = os.environ.get("SLEEP_SHARED_CTX", "1") != "0"
SLEEP_STAGE_HANDOFF_BUDGET = int(os.environ.get("SLEEP_STAGE_BUDGET_TOKENS", "12000"))
# Scheme B：messages 估算 token 超过此预算时，压缩较早的工具输出
SLEEP_CTX_BUDGET_TOKENS = int(os.environ.get("SLEEP_CTX_BUDGET_TOKENS", "24000"))
# Scheme B：压缩时始终保留最近 N 条消息不动（保护当前推理所需上下文）
SLEEP_CTX_KEEP_RECENT = int(os.environ.get("SLEEP_CTX_KEEP_RECENT", "6"))
# 工具输出短于此字符数则不值得压缩
_MIN_COMPACT_LEN = 200
# Scheme A：session-log 处理完成后替换其原始内容的占位文本
_PROCESSED_LOG_STUB = (
    "（该 session-log 已处理并标记 processed，原始内容已从上下文移除以节省 token）"
)
# Scheme B：较早工具输出被压缩后的占位文本
_COMPACTED_STUB = (
    "（较早的工具输出已压缩省略以节省上下文；如确有需要可重新调用对应工具）"
)

# Sleep Agent 状态
_sleep_lock = asyncio.Lock()
_sleep_timer: Optional[asyncio.Task] = None
_cancel_event = asyncio.Event()

# ── 动态轮询间隔（idle backoff）──────────────────────────────────────────────
# 连续空闲时逐步拉长做梦轮询间隔，减少无效检查。新消息到达时重置。
IDLE_BACKOFF_STEPS = [
    int(os.environ.get("SLEEP_IDLE_DELAY", "30")),   # 第一次：默认 30s
    300,                                              # 第二次：5 分钟
    900,                                              # 第三次：15 分钟
    1800,                                             # 之后：30 分钟
]
_idle_backoff_index: int = 0  # 当前退避级别（0=最短间隔）


def reset_idle_backoff() -> None:
    """重置空闲退避到最短间隔（有新消息时调用）。"""
    global _idle_backoff_index
    _idle_backoff_index = 0


def _advance_idle_backoff() -> None:
    """推进到下一级退避间隔。"""
    global _idle_backoff_index
    _idle_backoff_index = min(_idle_backoff_index + 1, len(IDLE_BACKOFF_STEPS) - 1)


def _current_idle_delay() -> int:
    """获取当前退避级别对应的延迟秒数。"""
    return IDLE_BACKOFF_STEPS[min(_idle_backoff_index, len(IDLE_BACKOFF_STEPS) - 1)]

# Skill 目录
SKILL_DIR = Path(os.environ.get("SKILL_ROOT_PATH", str(Path(__file__).parent / "skill")))
MEMORY_EXTRACTION_SKILL = SKILL_DIR / "base_skill" / "memory-extraction" / "SKILL.md"

# 阶段 Prompt 工坊：外置 override 文件（memory/config/stage-prompts/<stage>.md）
# 每段 _build_stage_prompt 入口现读（不缓存）——改完下一段即生效，无需重启
def _stage_prompt_dir() -> Path:
    # 支持环境变量覆盖；默认走记忆数据目录（对齐 _staging_dir() 写法，不能用 __file__ 相对解析）
    d = os.environ.get("SLEEP_STAGE_PROMPT_DIR")
    return Path(d) if d else mm._get_memory_dir() / "config" / "stage-prompts"
_STAGE_PROMPT_STAGES = ("extract", "rem", "consolidate", "review")


def _load_stage_prompt_override(stage: str) -> Optional[str]:
    """读 <stage>.md override；失败/为空/超 32KB 一律回退默认（做梦链路不可断）。"""
    if stage not in _STAGE_PROMPT_STAGES:
        return None
    fp = _stage_prompt_dir() / f"{stage}.md"
    try:
        if not fp.exists():
            return None
        text = fp.read_text(encoding="utf-8").strip()
        if not text or len(text) > 32768:
            logger.warning("stage-prompt override %s 无效（空或超32KB），回退默认", fp)
            return None
        return text
    except Exception as e:
        logger.warning("stage-prompt override %s 读取失败：%s，回退默认", fp, e)
        return None




# ---------------------------------------------------------------------------
# 四阶段真拆（方案 B）：阶段基建 —— staging 目录 / 状态机 / 每 stage 产物
# ---------------------------------------------------------------------------
# 一个 sleep agent 状态机多阶段（2026-10-17_042 共识）：同一大脑切模式，
# 每阶段换 system prompt + 工具子集，段间传产物文件。段间依赖（DAG）：
# ①提取 → ②推演(依赖①) → ③巩固(依赖①②) → ④review(占位)

STAGES = ["extract", "rem", "consolidate", "review"]
# [laneB-parallel-1022] 泳道B 与 ②③④ 并行后会同时读写同一个 state.json，
# 读-改-写没有锁会互相覆盖（并行夜实测会丢 stage 状态）。所有 state 事务走这把进程内锁。
_STAGE_STATE_LOCK = __import__("threading").Lock()

def _stage_state_tx(fn):
    """state.json 读改写原子化：同一进程内串行，避免并行泳道互相覆盖。"""
    with _STAGE_STATE_LOCK:
        return fn()

STAGE_LABELS = {
    "extract": "① Deep 提取",
    "rem": "② REM 推演",
    "consolidate": "③ 巩固",
    "review": "④ 对抗 Review",
}


def _staging_dir() -> Path:
    return mm._get_memory_dir() / "dream-staging"


def _stage_state_load(dream_id: str) -> dict:
    fp = _staging_dir() / dream_id / "state.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"dream_id": dream_id, "trigger": "", "stages": {}}


def _stage_state_save(state: dict) -> None:
    d = _staging_dir() / state["dream_id"]
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "state.json"
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(fp)


def _stage_begin(dream_id: str, stage: str, trigger: str) -> None:
    def _do():
        state = _stage_state_load(dream_id)
        state["trigger"] = trigger
        state.setdefault("start_time", datetime.now().isoformat(timespec="seconds"))
        state["stages"][stage] = {"status": "running", "start": datetime.now().isoformat(timespec="seconds")}
        for s in STAGES:
            state["stages"].setdefault(s, {"status": "pending"})
        _stage_state_save(state)
    _stage_state_tx(_do)


def _stage_end(dream_id: str, stage: str, status: str = "done", detail: str = "") -> None:
    def _do():
        state = _stage_state_load(dream_id)
        st = state["stages"].get(stage, {})
        st["status"] = status
        st["end"] = datetime.now().isoformat(timespec="seconds")
        if detail:
            st["detail"] = detail[:600]
        state["stages"][stage] = st
        state["end_time"] = datetime.now().isoformat(timespec="seconds")
        _stage_state_save(state)
    _stage_state_tx(_do)


def _stage_heartbeat(dream_id: str, stage: str, iter_no: int, max_iters: int,
                     action: str = "") -> None:
    """段内 ReAct 心跳：当前迭代/上限 + 最近动作，供前端实时显示执行到哪。轻量，失败静默。"""
    def _do():
        state = _stage_state_load(dream_id)
        st = state["stages"].setdefault(stage, {})
        st["hb"] = {"iter": iter_no, "max": max_iters, "action": action[:120],
                    "ts": datetime.now().isoformat(timespec="seconds")}
        hist = st.setdefault("hb_hist", [])
        if not hist or hist[-1].get("a") != action[:60]:
            hist.append({"a": action[:60], "i": iter_no, "t": st["hb"]["ts"]})
            del hist[:-12]
        _stage_state_save(state)
    try:
        _stage_state_tx(_do)
    except Exception:
        pass


def _stage_trace(dream_id: str, stage: str, entry: dict) -> None:
    """段内行为轨迹：LLM 回复摘要 + 工具调用与结果预览，供前端展开查看。轻量，失败静默。"""
    def _do():
        state = _stage_state_load(dream_id)
        st = state["stages"].setdefault(stage, {})
        tr = st.setdefault("trace", [])
        tr.append({k: (list(v) if k == "tools" else v) for k, v in entry.items()})
        del tr[:-40]
        _stage_state_save(state)
    try:
        _stage_state_tx(_do)
    except Exception:
        pass


def _stage_write_artifact(dream_id: str, stage: str, name: str, content: str) -> str:
    d = _staging_dir() / dream_id
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"{stage}-{name}"
    fp.write_text(content, encoding="utf-8")
    return str(fp)


def _new_dream_id() -> str:
    return f"{date.today().isoformat()}_{datetime.now().strftime('%H%M%S')}"


# ---------------------------------------------------------------------------
# 自定义异常：用于强制中断 ReAct 循环
# ---------------------------------------------------------------------------

class SleepInterruptedError(Exception):
    """Sleep Agent 被用户消息中断时抛出。"""
    def __init__(self, msg: str = "", token_usage: dict | None = None):
        super().__init__(msg)
        self.token_usage = token_usage or {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}


# ---------------------------------------------------------------------------
# Sleep Agent 专用工具函数（13 个）
# ---------------------------------------------------------------------------

def _check_cancel(fn: Callable) -> Callable:
    """装饰器：工具调用前检查 cancel_event，若已设置则 raise SleepInterruptedError。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _cancel_event.is_set():
            raise SleepInterruptedError("用户发来新消息，Sleep Agent 被中断")
        return fn(*args, **kwargs)
    return wrapper


# --- 泳道B（插件生成）缺口信号：①提取阶段专用，S1 不再自己摸 log 扫描 ---
_GAP_SIGNALS_PATH = Path("/xdata/dsh/home/somni/memory/dream-staging/gap-signals.jsonl")
_GAP_SIGNAL_CURRENT = []  # 本夜缓存：run_sleep 结束时 dispatch 给泳道B


def emit_gap_signal(gap: str, signal_type: str = "intent-no-tool",
                    evidence: str = "", fail_context: str = "",
                    fail_count: int = 1, expect_plugin_id: str = "",
                    gap_key: str = "") -> str:
    """记录一条能力缺口信号，供充电期插件生成流水线（泳道B）消费。

    仅当日志中出现「想做的事没做成」且归因是缺工具/缺环境适配时调用；
    纯事实类问题（在哪/是什么）、一次性闲聊、定义性问题不要调用。
    本夜无缺口就不调用（宁缺勿滥，不要为了生成插件而编造缺口）。

    Args:
        gap: 缺口一句话描述，只写「缺什么/什么事没做成」，禁止写解决方案或工具名。
        signal_type: 信号类型，intent-no-tool（想做的事没有对应工具）/ tool-fail（工具执行失败）/ user-correct（用户纠偏）。
        evidence: 日志中的原始证据摘录（时间+发生了什么，字面量抄录）。
        fail_context: 失败上下文（仅 tool-fail 需要：报错内容/失败环境因素）。
        fail_count: 同类失败出现次数，默认 1。
        expect_plugin_id: 期望的插件标识（kebab-case，如 vlt-offpeak-charge），可留空由流水线命名。
        gap_key: 缺口归并键（kebab-case，如 patrol-schedule）。同一件事换个说法也必须用同一个
            key——插件生成按此键跨夜累计，key 不稳会导致同一缺口被反复当成新缺口。留空则按文本兜底哈希。

    Returns:
        确认信息字符串。
    """
    sig = {
        "gap": (gap or "").strip()[:300],
        "signalType": signal_type if signal_type in ("intent-no-tool", "tool-fail", "user-correct") else "intent-no-tool",
        "sessionEvidence": (evidence or "").strip()[:500],
        "failContext": (fail_context or "").strip()[:300],
        "failCount": max(1, int(fail_count or 1)),
        "expectPluginId": (expect_plugin_id or "").strip()[:60],
        "gapKey": (gap_key or "").strip()[:60],
        "emittedAt": datetime.now().isoformat(timespec="seconds"),
    }
    if not sig["gap"]:
        return "错误：gap 不能为空"
    try:
        _GAP_SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_GAP_SIGNALS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(sig, ensure_ascii=False) + "\n")
        _GAP_SIGNAL_CURRENT.append(sig)
        # 并入跨夜台账：按 gapKey 归并，判断是否够阈值进入插件生成
        try:
            try:
                from . import gap_ledger
            except Exception:
                import gap_ledger
            ent = gap_ledger.record(sig)
            sig["gapKeyResolved"] = ent["key"]
            passed, reason = gap_ledger.evaluate(ent)
            verdict = "已够阈值，将进入插件生成" if passed else "累计中，暂不生成"
            hint = ""
            if ent["key"].startswith("auto-"):
                hint = "；⚠️未给 gap_key，已按文本兜底（同义换说法会算成新缺口，请补稳定 key）"
            return "缺口信号已记录（共 %d 条；key=%s，%s：%s）%s" % (
                len(_GAP_SIGNAL_CURRENT), ent["key"], verdict, reason, hint)
        except Exception as e:
            logger.warning("缺口台账写入失败（已忽略）: %s", e)
        return "缺口信号已记录（共 %d 条，做梦结束后交插件生成流水线路由）" % len(_GAP_SIGNAL_CURRENT)
    except Exception as e:
        return f"错误：缺口信号落盘失败 {e}"


def read_session_log(path: str) -> str:
    """读取指定 session-log 文件的完整内容。

    Args:
        path: session-log 文件的完整路径。

    Returns:
        文件的文本内容。
    """
    fp = Path(path)
    if not fp.exists():
        return f"错误：文件不存在 {path}"
    return fp.read_text(encoding="utf-8")


def _iter_skill_files() -> list[tuple[Path, str]]:
    """遍历 SKILL_DIR 下所有 SKILL.md 文件，返回 (路径, 所属子目录名) 列表。

    跳过 archived/ 与 base_skill/memory-extraction（Sleep Agent 自身策略文件）。
    """
    out: list[tuple[Path, str]] = []
    if not SKILL_DIR.exists():
        return out
    for md in sorted(SKILL_DIR.rglob("SKILL.md")):
        try:
            rel = md.relative_to(SKILL_DIR)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        sub_dir = parts[0]
        # 跳过归档目录和 Sleep Agent 自身策略文件
        if sub_dir == "archived":
            continue
        if sub_dir == "base_skill" and len(parts) >= 2 and parts[1] == "memory-extraction":
            continue
        out.append((md, sub_dir))
    return out


def _locate_item(name: str) -> tuple[Optional[Path], Optional[str]]:
    """根据 name 自动定位条目所在路径和来源（knowledge 或某个 skill 子目录）。

    返回 (SKILL.md 路径, 来源标识)；找不到时返回 (None, None)。
    """
    knowledge_fp = mm.KNOWLEDGE_DIR / name / "SKILL.md"
    if knowledge_fp.exists():
        return knowledge_fp, "knowledge"
    for md, sub_dir in _iter_skill_files():
        if md.parent.name == name:
            return md, sub_dir
    return None, None


def list_items(scope: str = "all") -> str:
    """列出 knowledge 或 skill 条目的 name + description + 所在目录。

    Args:
        scope: 范围，'knowledge' / 'skills' / 'all'（默认）。

    Returns:
        条目列表文本，每行包含 name、所在目录、stability、score、description。
    """
    scope = (scope or "all").strip().lower()
    if scope not in ("knowledge", "skills", "all"):
        return f"错误：scope 仅支持 'knowledge' / 'skills' / 'all'，收到 '{scope}'"

    lines: list[str] = []

    if scope in ("knowledge", "all"):
        entries = mm.load_all_entries()
        for e in entries:
            body_preview = (e.body or "")[:80].replace("\n", " ").strip()
            if len(e.body or "") > 80:
                body_preview += "..."
            lines.append(
                f"- [knowledge] name: {e.name} | type: {e.entry_type} "
                f"| stability: {e.stability} | score: {e.score:.3f} "
                f"| description: {e.description}"
                f" | body_preview: {body_preview}"
            )

    if scope in ("skills", "all"):
        for md, sub_dir in _iter_skill_files():
            try:
                meta, sk_body = mm._parse_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            sk_name = str(meta.get("name") or md.parent.name)
            sk_desc = str(meta.get("description") or "")
            stab = meta.get("stability", "—")
            sc = meta.get("score", "—")
            sc_str = f"{sc:.3f}" if isinstance(sc, (int, float)) else str(sc)
            body_preview = sk_body[:80].replace("\n", " ").strip()
            if len(sk_body) > 80:
                body_preview += "..."
            lines.append(
                f"- [skill/{sub_dir}] name: {sk_name} | stability: {stab} "
                f"| score: {sc_str} | description: {sk_desc}"
                f" | body_preview: {body_preview}"
            )

    if not lines:
        return "当前没有任何条目。"
    return "\n".join(lines)


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_H1_RE = re.compile(r"^#\s+\S", re.M)


def _ensure_zh_h1(body: str, description: str = "", name: str = "") -> str:
    """保证条目 body 第一处有一级标题（面板展示名）。

    - body 已有 `# 标题` → 原样返回（仅在缺少中文且 description 是中文时不动，避免擅改）
    - body 无一级标题 → 用 description 首句补一行 `# ...`
    - description 也没有 → 用 name 兜底
    """
    b = (body or "").lstrip("\n")
    if _H1_RE.search(b):
        return body
    cand = (description or "").strip()
    if cand:
        cand = re.split(r"[。；;\n]", cand, 1)[0].strip()
    if not cand:
        cand = (name or "").strip() or "未命名条目"
    cand = cand[:40]
    return f"# {cand}\n\n" + b


def read_item(name: str) -> str:
    """读取某条 knowledge 或 skill 的完整内容。系统根据 name 自动定位目录。

    Args:
        name: 条目的 name（目录名/slug）。

    Returns:
        该条目 SKILL.md 的完整内容（含 frontmatter）。
    """
    fp, _ = _locate_item(name)
    if fp is None:
        return f"错误：条目 '{name}' 不存在（已检索 knowledge 和所有 skill 子目录）。"
    return fp.read_text(encoding="utf-8")


def create_knowledge(name: str, entry_type: str, description: str, body: str,
                     verified: bool = True) -> str:
    """新建记忆条目。初始 stability=0；verified 时 score=0.5，未验证时 score=0.3 并标注待验证。

    Args:
        name: 条目名称（slug 格式，如 deploy-gotcha）。
        entry_type: 类型，'experience' 或 'user'。
        description: 一句话描述（用于索引和检索）。
        body: 详细内容（markdown 格式）。
        verified: 该结论/修复是否已在会话中被验证。若执行报错/会话未解决/用户未确认，
                  传 verified=false：初始分更低、正文顶部标注「待验证」，待后续确认后再提分。

    Returns:
        创建结果。
    """
    name = mm._platform.sanitize_name(name)
    fp = mm.KNOWLEDGE_DIR / name / "SKILL.md"
    if fp.exists():
        return f"错误：记忆条目 '{name}' 已存在。请使用 update_knowledge 更新。"
    score = 0.5 if verified else 0.3
    body = _ensure_zh_h1(body or "", description, name)
    if not verified:
        body = "> ⚠️ 待验证：本结论在会话中未成功验证，待后续确认。\n\n" + (body or "")
    entry = mm.MemoryEntry(
        name=name,
        description=description,
        entry_type=entry_type,
        stability=0,
        score=score,
        created=str(date.today()),
        last_used="",
        body=body,
        path=fp,
    )
    entry.save()
    suffix = "" if verified else "，⚠️未验证"
    return f"已创建记忆条目：{name}（type={entry_type}, score={score}{suffix}）"


def update_knowledge(name: str, description: str = "", body: str = "",
                     stability_delta: int = 0, score_delta: float = 0.0) -> str:
    """更新已有记忆条目的内容、stability 或 score。

    Args:
        name: 要更新的条目名称。
        description: 新的 description（为空则保持不变）。
        body: 新的正文内容（为空则保持不变）。
        stability_delta: stability 增量（如 +1 表示巩固一次）。
        score_delta: score 增量（如 +0.2 表示正向反馈）。

    Returns:
        更新结果。
    """
    fp = mm.KNOWLEDGE_DIR / name / "SKILL.md"
    if not fp.exists():
        return f"错误：记忆条目 '{name}' 不存在。"
    entry = mm.MemoryEntry.load(fp)
    if description:
        entry.description = description
    if body:
        entry.body = _ensure_zh_h1(body, entry.description or description, name)
    entry.stability = max(0, entry.stability + stability_delta)
    entry.score = max(0.0, min(1.0, entry.score + score_delta))
    if stability_delta > 0:
        entry.last_used = str(date.today())
    entry.save()
    return (
        f"已更新 '{name}'：stability={entry.stability}, score={entry.score:.3f}"
        + (f", description 已更新" if description else "")
        + (f", 正文已更新" if body else "")
    )


def archive_item(name: str) -> str:
    """将 knowledge 或 skill 条目移入 archived/，系统自动判断来源目录。

    淘汰、强负向反馈或合并去重后归档时使用。

    Args:
        name: 要归档的条目名称（slug）。

    Returns:
        归档结果。
    """
    fp, source = _locate_item(name)
    if fp is None:
        return f"错误：条目 '{name}' 不存在。"

    if source == "knowledge":
        entry = mm.MemoryEntry.load(fp)
        mm._archive_entry(entry)
        return f"已归档 knowledge 条目：{name}"

    # skill 归档：移入 SKILL_DIR / "archived" / <sub_dir> / <name>
    src_dir = fp.parent
    archived_root = SKILL_DIR / "archived" / source
    archived_root.mkdir(parents=True, exist_ok=True)
    dest = archived_root / src_dir.name
    if dest.exists():
        shutil.rmtree(dest)
    if mm._platform.safe_move:
        mm._platform.safe_move(str(src_dir), str(dest))
    else:
        shutil.move(str(src_dir), str(dest))
    return f"已归档 skill 条目：{source}/{name}"


def create_skill(name: str, description: str, body: str, verified: bool = True) -> str:
    """新建技能到 skill/agent_learned_skills/ 目录。用于操作类经验的产出。

    Args:
        name: 技能名称（slug 格式）。
        description: 一句话描述。
        body: 技能的完整内容（SKILL.md 正文，markdown 格式）。
        verified: 该流程是否已在会话中被验证跑通。若执行报错/会话未解决/用户未确认，
                  传 verified=false：初始分更低、正文顶部标注「待验证」，待后续确认后再提分。

    Returns:
        创建结果。
    """
    name = mm._platform.sanitize_name(name)
    skill_path = SKILL_DIR / "agent_learned_skills" / name / "SKILL.md"
    if skill_path.exists():
        return f"错误：技能 'agent_learned_skills/{name}' 已存在。请使用 update_skill 更新。"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    score = 0.5 if verified else 0.3
    body = _ensure_zh_h1(body or "", description, name)
    if not verified:
        body = "> ⚠️ 待验证：本流程在会话中未成功验证，待后续确认。\n\n" + (body or "")
    content = (
        f"---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        f"stability: 0\n"
        f"score: {score}\n"
        f"created: {today}\n"
        f"last_used: {today}\n"
        f"last_decay: {today}\n"
        f"---\n\n"
        f"{body}"
    )
    if mm._platform.atomic_write:
        mm._platform.atomic_write(skill_path, content)
    else:
        skill_path.write_text(content, encoding="utf-8")
    suffix = "" if verified else "，⚠️未验证"
    return f"已创建技能：agent_learned_skills/{name}（stability=0, score={score}{suffix}）"


def update_skill(name: str, description: str = "", body: str = "",
                 merge_note: str = "",
                 stability_delta: int = 0, score_delta: float = 0.0) -> str:
    """更新已有 skill。对非 agent_learned_skills/ 目录采用 Merge 模式（仅追加/修正）。

    Args:
        name: 要更新的 skill 名称（slug）。
        description: 新的 description（为空保持不变）。
        body: 新的正文。对 agent_learned_skills/ 可整体覆写；对其他目录必须是
              在原 body 基础上追加/整合后的全文，工具层会校验长度未减少。
        merge_note: 整合说明，记入 dream-log 用（不写入 skill 文件本身）。
        stability_delta: stability 增量（如 +1 表示巩固）。
        score_delta: score 增量（如 +0.1 表示做梦巩固）。

    Returns:
        更新结果。
    """
    fp, source = _locate_item(name)
    if fp is None:
        return f"错误：skill '{name}' 不存在。"
    if source == "knowledge":
        return f"错误：'{name}' 是 knowledge 条目，请使用 update_knowledge。"

    text = fp.read_text(encoding="utf-8")
    meta, old_body = mm._parse_frontmatter(text)

    # auto_merge: false 逃生口
    auto_merge_val = meta.get("auto_merge")
    if isinstance(auto_merge_val, str) and auto_merge_val.lower() in ("false", "0", "no"):
        return (f"错误：skill '{name}' 设置了 auto_merge: false，"
                f"禁止 Sleep Agent 自动整合。请改为在 knowledge/ 中存储补充信息。")

    # Merge 模式校验：非 agent_learned_skills/ 目录的 skill body 不允许缩短（避免删除原有内容）
    new_body = _ensure_zh_h1(body, description or meta.get("description", ""), name) if body else old_body
    if body and source != "agent_learned_skills":
        if len(body.strip()) < len(old_body.strip()):
            return (f"错误：'{source}/{name}' 不允许重写全文（Merge 模式仅允许追加/修正）。"
                    f"请在原 body 基础上追加新实践经验。")

    if description:
        meta["description"] = description

    today = date.today().isoformat()

    # stability/score 调整
    try:
        old_stab = int(meta.get("stability", 0))
    except (TypeError, ValueError):
        old_stab = 0
    try:
        old_score = float(meta.get("score", 0.5))
    except (TypeError, ValueError):
        old_score = 0.5

    meta["stability"] = max(0, old_stab + stability_delta)
    meta["score"] = round(max(0.0, min(1.0, old_score + score_delta)), 3)
    if stability_delta > 0:
        meta["last_used"] = today
    meta.setdefault("last_decay", today)

    full = mm._dump_frontmatter(meta, new_body)
    if mm._platform.atomic_write:
        mm._platform.atomic_write(fp, full)
    else:
        fp.write_text(full, encoding="utf-8")

    note_part = f"，merge_note: {merge_note}" if merge_note else ""
    return (f"已更新 '{source}/{name}'：stability={meta['stability']}, "
            f"score={meta['score']:.3f}{note_part}")


def mark_log_processed(path: str) -> str:
    """标记 session-log 为 processed 并移入 archived/。

    Args:
        path: session-log 文件的完整路径。

    Returns:
        处理结果。
    """
    fp = Path(path)
    if not fp.exists():
        return f"错误：文件不存在 {path}"
    mm.mark_log_processed(fp)
    return f"已标记 {fp.name} 为 processed"


def write_dream_log(content: str) -> str:
    """写入 dream-log 记录本次做梦过程。

    Args:
        content: dream-log 的完整 markdown 内容（含 frontmatter）。

    Returns:
        写入结果。
    """
    # 格式校验：必须包含 frontmatter
    stripped = content.strip()
    if not stripped.startswith("---"):
        return ("错误：dream-log 内容缺少 frontmatter。"
                "请以 '---' 开头，包含 dream_id、trigger、start_time、end_time、processed_logs 等字段，"
                "再以 '---' 结束 frontmatter 后写入正文。")
    # 检查 frontmatter 闭合
    second_sep = stripped.find("---", 3)
    if second_sep == -1:
        return "错误：dream-log frontmatter 未闭合（缺少第二个 '---'）。请补全 frontmatter。"
    # 四阶段版校验：frontmatter 必须含 dream_id 与 processed_logs（防 LLM 自创格式写成第二份假日志）
    fm = stripped[3:second_sep]
    if "dream_id:" not in fm or "processed_logs:" not in fm:
        return ("错误：dream-log frontmatter 缺少 dream_id 或 processed_logs 字段。"
                "请严格按系统提示里的格式模板重写（frontmatter: dream_id/trigger/start_time/"
                "end_time/processed_logs），每夜只写一份。")

    mm.DREAM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    existing = list(mm.DREAM_LOG_DIR.glob(f"{today}_dream_*.md"))
    seq = len(existing) + 1
    fp = mm.DREAM_LOG_DIR / f"{today}_dream_{seq:03d}.md"
    fp.write_text(content, encoding="utf-8")
    return f"已写入 dream-log：{fp.name}"


def decay_all_knowledge() -> str:
    """执行全量衰减 + 淘汰。基于 stability 差异化衰减所有记忆条目的 score，并淘汰 score < 0.2 的条目。
    同时自动归档超过 30 天未活动的前瞻记忆（intentions）。

    Returns:
        衰减和淘汰结果。
    """
    mm.apply_decay_all()
    retired = mm.run_retirement()
    stale_intentions = mm.archive_stale_intentions()
    entries = mm.load_all_entries()
    summary_lines = [f"衰减完成，当前共 {len(entries)} 条记忆。"]
    if retired:
        summary_lines.append(f"淘汰了 {len(retired)} 条：{', '.join(retired)}")
    else:
        summary_lines.append("无条目被淘汰。")
    if stale_intentions:
        summary_lines.append(f"自动归档了 {len(stale_intentions)} 条过期前瞻记忆：{', '.join(stale_intentions)}")
    return "\n".join(summary_lines)


def review_dream_history(path: str = "") -> str:
    """查阅做梦历史。无 path 时列出近期 dream-log 列表，有 path 时返回指定文件内容。

    Args:
        path: dream-log 文件路径。为空则列出最近 10 个文件，非空则读取该文件内容。

    Returns:
        无 path：dream-log 文件路径列表；
        有 path：该文件的完整内容。
    """
    if path:
        fp = Path(path)
        if not fp.exists():
            return f"错误：文件不存在 {path}"
        return fp.read_text(encoding="utf-8")

    if not mm.DREAM_LOG_DIR.exists():
        return "当前没有任何 dream-log。"
    logs = sorted(mm.DREAM_LOG_DIR.glob("*.md"), reverse=True)
    if not logs:
        return "当前没有任何 dream-log。"
    lines = [f"近期 dream-log（共 {len(logs)} 个，显示最近 10 个）："]
    for log in logs[:10]:
        lines.append(f"- {log}")
    return "\n".join(lines)


def update_about_me(content: str) -> str:
    """更新 about_me.md 的内容（Agent 自我认知画像）。

    Args:
        content: 新的 about_me.md 正文内容（不含 frontmatter）。

    Returns:
        更新结果。
    """
    # ⚠️ 防做梦反噬清醒身份（2026-09-26 事故：about_me 被覆写成 Sleep Agent 岗位说明书）
    # 人做梦可以微调自我认知（反思/成长），但睡眠角色本身不能成为清醒人格，且严禁编造身份经历。
    _forbidden = ("记忆管理助手", "Sleep Agent", "睡眠进程", "从 session-log 中", "提取、巩固、修正、淘汰记忆")
    _hits = [k for k in _forbidden if k in content]
    if _hits:
        return (f"拒绝写入：about_me 是「清醒时 Work Agent somni」的自我认知，"
                f"检测到睡眠角色描述 {_hits}。做梦/提取/衰减是后台维护机制，不是 somni 的人格——"
                f"如需反思成长（如新的工作习惯、沟通原则），请重写内容去除睡眠角色描述后再提交。")
    fp = mm.MEMORY_DIR / "about_me.md"
    meta = {
        "name": "about_me",
        "description": "Agent 的自我认知和个性画像",
        "type": "identity",
        "has_data": "true",
        "updated": str(date.today()),
    }
    full_content = mm._dump_frontmatter(meta, content)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(full_content, encoding="utf-8")
    return f"已更新 about_me.md（{len(content)} 字符）"


def update_about_user(content: str, user_id: str = "default") -> str:
    """更新指定用户的画像（用户偏好/习惯/角色）。

    多用户：team 私有助理场景下，按 session-log 的 user 分别维护各成员画像，不要混在一起。
    user_id 为空或 default 时更新默认用户（单用户/WS 场景，兼容旧行为）。

    Args:
        content: 新的用户画像正文内容（不含 frontmatter）。
        user_id: 该画像所属用户 id（来自正在处理的 session-log frontmatter 的 user 字段，
                 如 feishu:ou_xxx）；不确定就用 default。

    Returns:
        更新结果。
    """
    return mm.update_about_user(content, user_id=user_id or "default")


def record_episode(what: str, outcome: str = "unknown", objects: str = "",
                   tools: str = "", tags: str = "", salience: str = "low",
                   when: str = "", source: str = "") -> str:
    """记录一条情景记忆（事件流水：何时/谁/对什么/做了什么/结果如何）。

    用于把「实际发生过的事」记下来——即使它不值得提取为长期知识。情景记忆会随时间
    快速衰减，不再被提及则自然遗忘。判断标准比知识提取宽：只要发生了一件实质的事
    （任务/请求/决策/事故/排查）就默认记录；纯一次性查询（"今天几号"）或纯闲聊可跳过；
    拿不准就记成 salience=low（很快会自己淡忘，漏记则永久丢失）。

    Args:
        what: 事件一句话摘要（发生了什么）。
        outcome: 结果，success / failed / unknown。
        objects: 涉及的对象/产物（文件、CVE 编号、目标机等），逗号分隔。
        tools: 用到的工具/技能链，逗号分隔。
        tags: 主题标签，逗号分隔（如 内核CVE,数据分析），便于日后检索。
        salience: 重要度 low/mid/high，决定初始记忆强度与遗忘快慢。
        when: 事件时间或时间段（为空则用当前时间）。
        source: 来源 session-log id（便于回溯原文）。

    Returns:
        记录结果。
    """
    e = mm.record_episode(what=what, when=when, outcome=outcome, objects=objects,
                          tools=tools, tags=tags, salience=salience, source=source)
    return f"已记录情景记忆：{e.name}（salience={salience}, score={e.score:.2f}）"


# ---------------------------------------------------------------------------
# 推演猜想（反事实通道）：挂载 / 核销（验证并段 / 证伪删段）
# ---------------------------------------------------------------------------

_SPEC_MARK_RE = re.compile(r"<!--spec:(?P<id>[A-Za-z0-9_.-]+)\s+status=(?P<status>\w+)(?:\s+date=(?P<date>[\d-]+))?-->")
_SPEC_SECTION = "## 推演猜想（未验证）"


def _split_spec_sections(body: str):
    """把 skill body 切成 (普通正文, [(id, status, date, section_text)])。段边界=下一个 '## ' 或文末。"""
    lines = body.split("\n")
    normal, specs, cur = [], [], None  # cur=(id,status,date,[lines])
    for ln in lines:
        m = _SPEC_MARK_RE.search(ln)
        if m is not None and cur is None:
            cur = (m.group("id"), m.group("status"), m.group("date") or "", [ln])
            continue
        if cur is not None:
            if ln.startswith("## ") and _SPEC_SECTION not in ln:
                specs.append(cur)
                cur = None
                normal.append(ln)
            else:
                cur[3].append(ln)
                if ln.strip() and not ln.startswith((" ", "-", "|", ">")) and cur[3][-1] is ln and False:
                    pass
        else:
            normal.append(ln)
    if cur is not None:
        specs.append(cur)
    return "\n".join(normal).rstrip() + "\n", specs


def _find_spec_block(body: str, spec_id: str):
    """返回 (start_idx, end_idx, mark_line_idx) 或 None。按 <!--spec:id 定位段。"""
    lines = body.split("\n")
    start = end = mark = None
    for i, ln in enumerate(lines):
        m = _SPEC_MARK_RE.search(ln)
        if m and m.group("id") == spec_id:
            mark = i
            # 段头 = 向上吞并紧邻的「## 推演猜想（未验证）」标题与空行
            s = i
            while s > 0 and (not lines[s - 1].strip() or lines[s - 1].strip() == _SPEC_SECTION):
                if lines[s - 1].strip() == _SPEC_SECTION and s - 1 > 0 and lines[s - 2].strip() and not lines[s - 2].startswith("#"):
                    pass
                s -= 1
                if lines[s].strip() == _SPEC_SECTION:
                    break
            start = s
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and _SPEC_MARK_RE.search(lines[j]) is None:
            end = j
            break
    return start, end, mark


def append_speculation(skill: str, basis: str, benefit: str, verify_action: str) -> str:
    """挂载推演猜想（反事实通道）。往 skill 追加「推演猜想（未验证）」段，机器生成 spec 标记。

    Args:
        skill: 目标 skill 名称（slug）。
        basis: 推演依据（来自 log 中的失败事件）。
        benefit: 预期收益。
        verify_action: 验证动作（可判定真假的证据要求，写不出不挂）。

    Returns:
        挂载结果。
    """
    import uuid as _uuid
    if not (basis or "").strip() or not (verify_action or "").strip():
        return "错误：依据与验证动作不能为空；无可判定验证动作的猜想不挂。"
    fp, source = _locate_item(skill)
    if fp is None:
        return f"错误：skill '{skill}' 不存在。"
    if source == "knowledge":
        return f"错误：'{skill}' 是 knowledge 条目，猜想只挂 skill。"
    text = fp.read_text(encoding="utf-8")
    meta, body = mm._parse_frontmatter(text)
    if _find_spec_block(body, "*") :
        pass
    # 幂等：同 skill 已有 pending 猜想则拒绝重复挂
    lines = body.split("\n")
    for ln in lines:
        m = _SPEC_MARK_RE.search(ln)
        if m and m.group("status") == "pending":
            return f"错误：'{skill}' 已有 pending 猜想（{m.group('id')}），核销后再挂新。"
    spec_id = f"spec-{date.today().strftime('%Y%m%d')}-{_uuid.uuid4().hex[:6]}"
    section = (
        f"\n{_SPEC_SECTION}\n\n"
        f"<!--spec:{spec_id} status=pending date={date.today().isoformat()}-->\n\n"
        f"- 推演依据：{basis.strip()}\n"
        f"- 预期收益：{benefit.strip()}\n"
        f"- 验证动作：{verify_action.strip()}\n"
    )
    new_body = body.rstrip() + "\n" + section
    meta.setdefault("stability", 0)
    full = mm._dump_frontmatter(meta, new_body)
    if mm._platform.atomic_write:
        mm._platform.atomic_write(fp, full)
    else:
        fp.write_text(full, encoding="utf-8")
    return f"已挂载猜想 {spec_id} → {source}/{skill}（pending）。核销用 mark_speculation_verified / remove_speculation。"


def mark_speculation_verified(skill: str, spec_id: str, evidence: str = "") -> str:
    """验证核销：猜想并入正式段（去标记），stability +1。

    Args:
        skill: skill 名称。
        spec_id: 猜想 id（<!--spec:id --> 的 id）。
        evidence: 支撑证据（来自 log 的 tool_call 链/结果，写入段首摘要）。

    Returns:
        核销结果。
    """
    return _resolve_speculation(skill, spec_id, verified=True, evidence=evidence)


def remove_speculation(skill: str, spec_id: str, reason: str = "") -> str:
    """证伪核销：删除猜想段；reason 有信息量时压一行教训入正式段。

    Args:
        skill: skill 名称。
        spec_id: 猜想 id。
        reason: 证伪原因（可空；非空则沉淀一行教训）。

    Returns:
        核销结果。
    """
    return _resolve_speculation(skill, spec_id, verified=False, evidence=reason)


def _resolve_speculation(skill: str, spec_id: str, verified: bool, evidence: str) -> str:
    fp, source = _locate_item(skill)
    if fp is None:
        return f"错误：skill '{skill}' 不存在。"
    text = fp.read_text(encoding="utf-8")
    meta, body = mm._parse_frontmatter(text)
    hit = _find_spec_block(body, spec_id)
    if hit is None:
        return f"错误：'{skill}' 中找不到猜想 {spec_id}。"
    start, end, _mark = hit
    lines = body.split("\n")
    block = lines[start:end]
    if verified:
        merged = []
        for ln in block:
            if _SPEC_MARK_RE.search(ln) or ln.strip() == _SPEC_SECTION or not ln.strip():
                continue
            ln = re.sub(r"^- ", "", ln)
            merged.append(ln)
        summary = f"（已验证 {date.today().isoformat()}：" + (evidence or "log 证据判定成立").strip() + "）"
        replacement = ["## 实践要点（推演已验证）", summary] + merged
        lines[start:end] = replacement
        try:
            meta["stability"] = int(meta.get("stability", 0)) + 1
        except (TypeError, ValueError):
            meta["stability"] = 1
        action = "并段去标记，stability +1"
    else:
        lines[start:end] = []
        # 清掉残留的空「## 推演猜想（未验证）」标题
        while _SPEC_SECTION in lines:
            k = lines.index(_SPEC_SECTION)
            nxt = next((l for l in lines[k+1:] if l.strip()), "")
            if nxt.startswith("## "):
                lines.pop(k)
                while k < len(lines) and not lines[k].strip():
                    lines.pop(k)
            else:
                break
        if (evidence or "").strip():
            lesson = f"\n## 教训\n\n- 推猜想 {spec_id} 证伪：{evidence.strip()}\n"
            lines.append(lesson.strip("\n").split("\n")[-1] and "")
            lines = [l for l in lines if l != ""]
            lines.append("")
            lines.append("## 教训")
            lines.append("")
            lines.append(f"- 猜想 {spec_id} 证伪：{evidence.strip()}")
        action = "删段" + ("，沉淀教训" if (evidence or "").strip() else "")
    new_body = "\n".join(lines).rstrip() + "\n"
    full = mm._dump_frontmatter(meta, new_body)
    if mm._platform.atomic_write:
        mm._platform.atomic_write(fp, full)
    else:
        fp.write_text(full, encoding="utf-8")
    return f"已核销 {spec_id}（{skill}）：{action}。"


# 所有 Sleep 专用工具（包装 cancel 检查）— 与 DESIGN.md §8.2 对齐
SLEEP_TOOLS_RAW = [
    read_session_log,
    list_items,
    read_item,
    create_knowledge,
    update_knowledge,
    create_skill,
    update_skill,
    archive_item,
    record_episode,
    mark_log_processed,
    write_dream_log,
    decay_all_knowledge,
    review_dream_history,
    update_about_me,
    update_about_user,
    append_speculation,
    mark_speculation_verified,
    remove_speculation,
    emit_gap_signal,
]

SLEEP_TOOLS = [_check_cancel(fn) for fn in SLEEP_TOOLS_RAW]


# ---------------------------------------------------------------------------
# 工具注册表（供 ReAct 循环调用）
# ---------------------------------------------------------------------------

def _build_tools_schema() -> list[dict]:
    """从工具函数的 docstring 和 annotations 构建 JSON Schema 格式的工具描述。"""
    import inspect
    tools = []
    for fn in SLEEP_TOOLS:
        sig = inspect.signature(fn)
        params = {}
        required = []
        for pname, param in sig.parameters.items():
            ptype = "string"
            annotation = param.annotation
            if annotation == int:
                ptype = "integer"
            elif annotation == float:
                ptype = "number"

            param_desc = ""
            if fn.__doc__:
                for line in fn.__doc__.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(f"{pname}:"):
                        param_desc = stripped.split(":", 1)[1].strip()
                        break

            params[pname] = {"type": ptype, "description": param_desc}
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        doc_lines = (fn.__doc__ or "").strip().split("\n")
        description = doc_lines[0] if doc_lines else fn.__name__

        tools.append({
            "type": "function",
            "function": {
                "name": fn.__name__,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": required,
                },
            },
        })
    return tools


def _get_tool_by_name(name: str) -> Optional[Callable]:
    """根据名称查找工具函数。"""
    for fn in SLEEP_TOOLS:
        if fn.__name__ == name:
            return fn
    return None


# ── 阶段工具子集（四阶段真拆：每阶段只见自己的工具）─────────────────────────
_STAGE_TOOLS = {
    "extract": [
        "read_session_log", "list_items", "read_item",
        "create_knowledge", "update_knowledge", "create_skill", "update_skill",
        "archive_item", "record_episode", "update_about_me", "update_about_user",
        "mark_log_processed", "emit_gap_signal",
    ],
    "rem": ["read_session_log", "list_items", "read_item", "review_dream_history",
            "append_speculation"],
    "consolidate": ["list_items", "read_item", "decay_all_knowledge",
                    "mark_speculation_verified", "remove_speculation", "write_dream_log"],
    "review": ["list_items", "read_item", "review_dream_history"],
}


def _stage_tools_schema(stage: str) -> list[dict]:
    allowed = set(_STAGE_TOOLS.get(stage, [fn.__name__ for fn in SLEEP_TOOLS]))
    return [t for t in _build_tools_schema()
            if t["function"]["name"] in allowed]


def _get_stage_tool(stage: str, name: str) -> Optional[Callable]:
    """阶段内工具查找：不在该阶段白名单里的调用直接拒绝（防跨阶段乱用）。"""
    if name not in _STAGE_TOOLS.get(stage, set()):
        return None
    return _get_tool_by_name(name)


# ---------------------------------------------------------------------------
# Sleep Agent System Prompt 构建
# ---------------------------------------------------------------------------

def _build_sleep_system_prompt() -> str:
    """构建 Sleep Agent 的 system prompt。"""
    role_prompt = (
        "你是「记忆整理助手」，负责在 Agent 空闲时从对话日志（session-log）中提取有价值的记忆和技能。\n"
        "你的工作方式类似人脑做梦——从白天的经历中整理、提取、巩固、修正和遗忘记忆。\n\n"
        "你拥有一组专用工具来完成记忆管理操作。请通过工具调用来执行所有操作，自主决策提取什么、如何合并、怎么修正。\n\n"
    )

    strategy = ""
    if MEMORY_EXTRACTION_SKILL.exists():
        text = MEMORY_EXTRACTION_SKILL.read_text(encoding="utf-8")
        _, body = mm._parse_frontmatter(text)
        strategy = f"## 做梦策略\n\n{body}\n\n"

    # Scheme C：不再在 system prompt 中内嵌全量记忆列表（避免与 list_items 重复，
    # 且该列表会随 system prompt 每一轮重发，是 token 大头）。仅保留计数，
    # 引导 Agent 在需要时调用 list_items 获取最新、完整（含 skill）的列表。
    entries = mm.load_all_entries()
    if TOKEN_OPT_ENABLED:
        memory_list = (
            f"## 当前已有记忆\n\n"
            f"系统中已有约 {len(entries)} 条 knowledge 记忆（另有若干 skill）。"
            f"请在去重/合并判断前调用 `list_items(scope=\"all\")` 获取完整、最新的条目列表"
            f"（含 knowledge 与 skill 的 name/stability/score/description）。\n\n"
        )
    elif entries:
        lines = [f"- {e.name} (type={e.entry_type}, stability={e.stability}, score={e.score:.3f}): {e.description}"
                 for e in entries]
        memory_list = f"## 当前已有记忆（共 {len(entries)} 条）\n\n" + "\n".join(lines) + "\n\n"
    else:
        memory_list = "## 当前已有记忆\n\n（无已有记忆条目）\n\n"

    workflow = (
        "## 你的工作流程\n\n"
        "1. 使用 `read_session_log` 严格按列表顺序从上到下（时间旧→新）逐个读取待处理的 session-log，处理完一个再处理下一个，不跳跃\n"
        "2. 使用 `list_items(scope=\"all\")` 查看已有 knowledge 和 skill，用于去重、合并和整合判断\n"
        "3. 识别反馈信号（正向/负向）：\n"
        "   - 知识条目：`update_knowledge` 修正/巩固，强负向用 `archive_item` 归档\n"
        "   - 已有同主题 skill：`update_skill` 整合增强（agent_learned_skills/ 可覆写；其他目录仅追加/修正）\n"
        "4. 提取新记忆 → `create_knowledge`\n"
        "5. 提取新操作类技能 → `create_skill`（写入 agent_learned_skills/）\n"
        "6. 识别用户画像信息 → `update_about_user` 更新用户画像（多用户：按 session-log frontmatter 的 "
        "`user` 字段把信息更新到对应用户，传 user_id；不同用户的画像不要混在一起）\n"
        "7. 识别 Agent 自我认知信息（性格、能力、角色定位等）→ `update_about_me` 更新自我画像\n"
        "   ⚠️ **身份分离铁律（防做梦反噬清醒身份）**：about_me 写的是「清醒时的 Work Agent somni」——"
        "用户的工程搭子。你（Sleep Agent）的职责（做梦/提取/衰减/记忆整理）只是后台维护机制，"
        "**不是 somni 的人格**：严禁把「记忆管理助手/做梦/提取记忆」等睡眠角色写进 about_me；"
        "严禁编造源日志中不存在的身份经历（如「用户误称我为X」——源日志无此事即是编造）。"
        "自我认知只能来自对话中真实发生的身份讨论；无依据时不更新 about_me（宁缺勿编）。\n"
        "8. 跨条目阅读详情 → `read_item`（自动定位 knowledge 或 skill 目录）\n"
        "9. **skill 自进化**（仅限本 log 实际涉及的 skill，快判快走不展开长文）：\n"
        "   10a 效率审视：tool_call 链有可合并/脚本化点 → `update_skill` 追加「效率优化」段。\n"
        "   10b 失败推演：log 中环境/模型能力缺口类失败（非定义性缺口/tool-bug）→ `append_speculation` "
        "挂猜想（推演依据/预期收益/验证动作三件套）；无可判定验证动作的不挂；每夜至多 2 条，宁缺勿滥。\n"
        "   10c 猜想核销：已有 pending 猜想且本 log 有确凿证据 → 验证用 `mark_speculation_verified`"
        "（并段去标记，stability+1）、证伪用 `remove_speculation`（删段留一行教训）；证据不足不动。"
        "只认 log 证据，无依据不判。\n"
        "   顺序 10a→10b→10c。\n"
        "10. 使用 `decay_all_knowledge` 执行全量衰减和淘汰\n"
        "11. 使用 `mark_log_processed` 标记每个已处理的 session-log\n"
        "12. 使用 `write_dream_log` 记录本次做梦的完整过程（按设计格式）\n\n"
        "## 情景记忆（处理每个 session-log 时都要判断）\n\n"
        "除了上面的知识/技能提取，处理每个 session-log 时还要判断是否用 `record_episode` "
        "记录一条「情景记忆」（事件流水：何时/谁/对什么/做了什么/结果）。这是独立于知识提取的一步：\n"
        "- 知识提取很严格（只记长期有价值的偏好/事实/教训/可复用流程）；情景记忆很宽松——"
        "只要发生了一件实质的事（任务/请求/排查/决策/事故/交付），就默认记录，哪怕它是一次性的。\n"
        "- 只有纯一次性查询（如「今天几号」「帮我算个数」）或纯闲聊才跳过。\n"
        "- 拿不准就记，salience 给 low：情景记忆会快速衰减、不再被提及就自然遗忘；漏记则永久丢失，"
        "代价不对称，所以倾向于记录。\n"
        "- salience：low（一般任务/查询）/ mid（有后续价值的任务）/ high（事故、关键决策、重要交付）。\n"
        "- record_episode 的 source 传该 session-log 的 id（如 2026-06-02_012），便于回溯原文。\n\n"
        "## dream-log 格式要求\n\n"
        "写入 dream-log 时，请使用以下 markdown 格式（含 frontmatter）：\n"
        "```\n"
        "---\n"
        "dream_id: {日期}_dream_{序号}\n"
        "trigger: {触发方式}\n"
        "start_time: {开始时间}\n"
        "end_time: {结束时间}\n"
        "processed_logs:\n"
        "  - {log文件名}\n"
        "---\n\n"
        "## 提取结果\n\n"
        "### 新增记忆\n"
        "| 名称 | 类型 | 来源 session-log | 提取依据 |\n"
        "| ... |\n\n"
        "### 更新条目\n"
        "| 名称 | 变更 | stability 变化 | 原因 |\n"
        "| ... |\n\n"
        "### 淘汰条目\n"
        "| 名称 | stability | score | 淘汰原因 |\n"
        "| ... |\n\n"
        "### 反馈识别与处理\n"
        "| 条目 | 信号类型 | 依据 | 处理 |\n"
        "| ... |\n\n"
        "### 推演\n"
        "挂载: 猜想id|skill|验证动作 / 核销: 猜想id|结论|证据log|动作\n\n"
        "### 策略自适应记录\n"
        "本次调整：...\n\n"
        "## Sleep Agent 推理过程摘要\n"
        "1. ...\n"
        "```\n\n"
        "## 注意事项\n\n"
        "- 每处理完一个 session-log 就立即调用 mark_log_processed 标记，保证打断时不丢进度\n"
        "- 💰 **省 token（处理大日志时尤其重要）**：读完一个 session-log 后，先在脑中/一句话里记下"
        "「要从它提取/更新什么」，**立刻调用 mark_log_processed**（系统会把该日志原文从上下文移除，"
        "避免它在后续每一轮被反复重发），**之后再**做 create_knowledge/update_knowledge/update_skill/"
        "record_episode 等提取动作。顺序务必是「读 → 记要点 → mark_log_processed → 再提取」，"
        "不要把大段原文一直留在上下文里反复搬运。\n"
        "- 🔒 **字面量保真**：路径、命令、配置键、错误码、端口、版本号、IP、数值这类字面量必须与 session-log "
        "原文**逐字一致**，禁止凭印象改写（如把 /etc/zqd/zqd.toml 写成 /etc/zq/zqd.conf）。"
        "「记要点」那一步就要把要用到的字面量原样抄下来，因为 mark_log_processed 之后原文就不在上下文里了；"
        "拿不准的字面量宁可不写，也不要编一个像的。\n"
        "- 负向反馈：修正条目内容（从对话中提取正确版本），stability 和 score 不变。"
        "修正时 body 只保留正确的当前值，完全替换旧内容，不附带旧值注释或变更历史（旧值自然遗忘，变更过程记录在 dream-log 中）\n"
        "- 正向反馈：stability +1, score +0.2\n"
        "- 做梦巩固（记忆在 session-log 中被引用/验证/提及，但不属于明确正向反馈）：stability +1, score +0.1\n"
        "- 强负向反馈（用户要求删除）：直接 archive_item\n"
        "- 不提取无关第三方的个人隐私数据（手机号、身份证号等），但允许提取主人的密码、token、凭证（本系统为个人私人助理）\n"
        "- 操作类经验（有完整步骤、可复用的流程）产出到 skill/agent_learned_skills/，认知类（偏好/事实/教训）产出到 knowledge/\n"
        "- 用户画像信息（角色、偏好、习惯等）使用 update_about_user 更新，不分散到多个 knowledge 条目\n"
        "- ⚠️ **去重（最重要）**：新建 create_knowledge 或 create_skill 之前，必须先对照 list_items 输出检查是否已有同主题条目（包括名称不同但功能/主题相同的）。"
        "如果已有相关条目，使用 update_knowledge 或 update_skill 整合新信息，而非新建重复条目。"
        "判断标准：description 或 body_preview 中涉及相同操作流程、相同知识点、相同工具链的，视为同主题\n\n"
        "## 最终输出要求\n\n"
        "所有工具调用完成后，你的最终回复必须是一段结构化的「睡梦总结」，格式如下：\n\n"
        "## 🌙 睡梦总结\n\n"
        "**处理日志**: N 个 session-log\n"
        "**Token 消耗**: （由系统注入，你写 0 即可）\n\n"
        "### 记忆变更\n"
        "- ✨ 新增: `name` — 描述\n"
        "- 🔄 更新: `name` — 变更说明\n"
        "- 🗑️ 淘汰: `name` — 原因\n"
        "（无变更写「无」）\n\n"
        "### 用户画像更新\n"
        "- 变更描述（无变更写「无」）\n\n"
        "### 自我认知更新\n"
        "- 变更描述（无变更写「无」）\n"
    )

    return role_prompt + strategy + memory_list + workflow


# ---------------------------------------------------------------------------
# 自实现 ReAct 循环 — DashScope API 直接调用
# ---------------------------------------------------------------------------

# ── LLM 后端可插拔（与 1010 版对齐）：宿主可注入，默认直连后端 ──
ChatBackend = Callable[[list, list], Awaitable[tuple]]

_chat_backend: Optional[ChatBackend] = None


def set_chat_backend(fn: Optional[ChatBackend]) -> None:
    """注入 LLM chat 实现（None = 恢复默认直连后端）。"""
    global _chat_backend
    _chat_backend = fn


def llm_available() -> bool:
    """是否存在可用的 LLM 后端（已注入，或有配置）。"""
    if _chat_backend is not None:
        return True
    return bool(os.environ.get("DASHSCOPE_API_KEY")) or bool(os.environ.get("SLEEP_MODEL"))


async def _call_dashscope_chat(messages: list[dict], tools: list[dict],
                               model_name: str, api_key: str,
                               base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1") -> tuple[dict, dict]:
    """调用 LLM Chat API（兼容 OpenAI 格式）。

    Returns:
        tuple[dict, dict]: (message_dict, usage_dict)
        usage_dict 格式: {"prompt_tokens": N, "completion_tokens": M}

    SDK 选择优先级：SLEEP_SDK_BACKEND 环境变量 > 自动检测。
    """
    if _chat_backend is not None:
        message, usage = await _chat_backend(messages, tools)
        usage = dict(usage or {})
        usage.setdefault("prompt_tokens", 0)
        usage.setdefault("completion_tokens", 0)
        usage.setdefault("cached_tokens", 0)
        return message, usage

    backend = os.environ.get("SLEEP_SDK_BACKEND", "").strip().lower()

    use_openai = False
    if backend == "openai":
        use_openai = True
    elif backend == "dashscope":
        use_openai = False
    else:
        try:
            from openai import AsyncOpenAI as _AOI  # noqa: F401
            use_openai = True
        except ImportError:
            use_openai = False

    if not use_openai:
        from dashscope import Generation
        response = await asyncio.to_thread(
            Generation.call,
            model=model_name,
            messages=messages,
            tools=tools,
            result_format="message",
        )
        if response.status_code != 200:
            raise RuntimeError(f"DashScope API 错误: {response.code} - {response.message}")
        # DashScope 原生 SDK: usage 可能是 dict 或对象
        raw_usage = getattr(response, "usage", None) or {}
        if isinstance(raw_usage, dict):
            details = raw_usage.get("prompt_tokens_details") or {}
            cached = (details.get("cached_tokens", 0) if isinstance(details, dict) else 0) \
                or raw_usage.get("prompt_cache_hit_tokens", 0) or 0
            usage = {
                "prompt_tokens": raw_usage.get("input_tokens", 0),
                "completion_tokens": raw_usage.get("output_tokens", 0),
                "cached_tokens": cached,
            }
        else:
            usage = {
                "prompt_tokens": getattr(raw_usage, "input_tokens", 0),
                "completion_tokens": getattr(raw_usage, "output_tokens", 0),
                "cached_tokens": getattr(raw_usage, "prompt_cache_hit_tokens", 0) or 0,
            }
        return response.output.choices[0].message, usage

    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
    )
    response = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        tools=tools if tools else None,
    )
    choice = response.choices[0]
    result = {"role": "assistant", "content": choice.message.content or ""}
    # 保留 reasoning_content 以便下轮回传（DeepSeek thinking model 要求）
    reasoning = getattr(choice.message, "reasoning_content", None)
    if reasoning:
        result["reasoning_content"] = reasoning
    if choice.message.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in choice.message.tool_calls
        ]
    # OpenAI SDK: response.usage 是对象。提取 prefix-cache 命中 token（scheme D 可观测）。
    # DeepSeek 返回 prompt_cache_hit_tokens；OpenAI/通义返回 prompt_tokens_details.cached_tokens。
    cached_tokens = 0
    if response.usage:
        details = getattr(response.usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", None) or 0
        if not cached_tokens:
            cached_tokens = getattr(response.usage, "prompt_cache_hit_tokens", 0) or 0
    usage = {
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        "cached_tokens": cached_tokens,
    }
    return result, usage


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """估算整个 messages 数组的 token 数（含 content 与 tool_calls 参数）。"""
    total = 0
    for m in messages:
        c = m.get("content")
        if c:
            total += estimate_tokens(str(c))
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {}) or {}
            total += estimate_tokens(str(fn.get("name", "")) + str(fn.get("arguments", "")))
    return total


def _compact_history(messages: list[dict], budget: int = SLEEP_CTX_BUDGET_TOKENS,
                     keep_recent: int = SLEEP_CTX_KEEP_RECENT) -> int:
    """Scheme B：当上下文超预算时，压缩较早的 tool 输出。

    仅替换 tool 消息的 content（不删除消息），从而保持 assistant.tool_calls 与
    其后续 tool 消息的配对关系不被破坏。始终保留最近 keep_recent 条消息不动。

    Returns:
        被压缩的消息条数。
    """
    if _estimate_messages_tokens(messages) <= budget:
        return 0
    n = len(messages)
    compacted = 0
    skip_contents = {_COMPACTED_STUB, _PROCESSED_LOG_STUB}
    for i in range(max(0, n - keep_recent)):
        m = messages[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content") or ""
        if len(content) <= _MIN_COMPACT_LEN or content in skip_contents:
            continue
        m["content"] = _COMPACTED_STUB
        compacted += 1
        if _estimate_messages_tokens(messages) <= budget:
            break
    return compacted


async def _run_react_loop(messages: list[dict], tools_schema: list[dict],
                          model_name: str, api_key: str, max_iters: int,
                          base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          stage: str = "", dream_id: str = "") -> dict:
    """自实现 ReAct 循环，每次工具调用前检查 cancel_event。

    Token 优化：
    - Scheme A：session-log 经 mark_log_processed 后，将其 read_session_log 的原始
      输出从历史中替换为占位文本（处理完即不再需要全文，避免每轮重发 64KB 级内容）。
    - Scheme B：每轮结束后，若上下文估算超预算，压缩较早的工具输出（保留消息结构，
      仅替换 content，确保 assistant.tool_calls 与 tool 消息配对不被破坏）。
    - Scheme D：累计 prefix-cache 命中 token，便于观测缓存收益。

    Returns:
        dict: {"text": 最终回复文本, "token_usage": {"prompt_tokens": N,
               "completion_tokens": M, "cached_tokens": C}}

    Raises:
        SleepInterruptedError: 当 cancel_event 被设置时。
    """
    total_prompt = 0
    total_completion = 0
    total_cached = 0

    # Scheme A：记录每个 session-log 的 read_session_log 输出在 messages 中的下标
    log_msg_index: dict[str, int] = {}

    def _usage_snapshot() -> dict:
        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "cached_tokens": total_cached,
        }

    for i in range(max_iters):
        if _cancel_event.is_set():
            raise SleepInterruptedError("循环开始前检测到中断信号", _usage_snapshot())

        response, usage = await _call_dashscope_chat(messages, tools_schema, model_name, api_key, base_url)
        total_prompt += usage.get("prompt_tokens", 0)
        total_completion += usage.get("completion_tokens", 0)
        total_cached += usage.get("cached_tokens", 0)

        tool_calls = response.get("tool_calls")
        _trace = None
        if dream_id:
            _c = (response.get("content") or "").strip()
            _rs = (response.get("reasoning_content") or "").strip()
            _trace = {"i": i + 1,
                      "llm": _c[:120] or (("…" + _rs[-100:]) if _rs else ""),
                      "tools": [],
                      "t": datetime.now().isoformat(timespec="seconds")}
        if not tool_calls:
            if dream_id and _trace is not None:
                _trace["final"] = (response.get("content") or "")[:300]
                _stage_trace(dream_id, stage, _trace)
            return {"text": response.get("content", ""), "token_usage": _usage_snapshot()}

        messages.append(response)

        for tc in tool_calls:
            if _cancel_event.is_set():
                raise SleepInterruptedError("工具调用前检测到中断信号", _usage_snapshot())

            func_name = tc["function"]["name"]
            func_args_str = tc["function"]["arguments"]

            try:
                func_args = json.loads(func_args_str) if func_args_str else {}
            except json.JSONDecodeError:
                func_args = {}

            if dream_id:
                _brief = func_name
                _p = ""
                try:
                    _p = func_args.get("path") or func_args.get("name") or func_args.get("gap") or ""
                except Exception:
                    _p = ""
                if _p:
                    _brief += "(" + str(_p).split("/")[-1][:40] + ")"
                _stage_heartbeat(dream_id, stage, i + 1, max_iters, _brief)

            tool_fn = _get_stage_tool(stage, func_name) if stage else _get_tool_by_name(func_name)
            if tool_fn is None:
                tool_result = f"错误：未知工具 '{func_name}'"
            else:
                try:
                    tool_result = tool_fn(**func_args)
                except SleepInterruptedError:
                    raise SleepInterruptedError("工具执行中检测到中断信号", _usage_snapshot())
                except Exception as e:
                    tool_result = f"工具执行错误：{e}"
            if _trace is not None:
                _trace["tools"].append({"n": func_name, "a": (func_args_str or "")[:100], "r": str(tool_result)[:200]})
                _stage_trace(dream_id, stage, _trace)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": str(tool_result),
            })
            tool_msg_idx = len(messages) - 1

            # Scheme A：维护 read_session_log → 消息下标映射；处理完成即剔除原始内容
            if TOKEN_OPT_ENABLED and func_name == "read_session_log":
                p = func_args.get("path")
                if p:
                    log_msg_index[str(Path(p))] = tool_msg_idx
            elif TOKEN_OPT_ENABLED and func_name == "mark_log_processed":
                p = func_args.get("path")
                if p and str(tool_result).startswith("已标记"):
                    idx = log_msg_index.get(str(Path(p)))
                    if idx is not None and 0 <= idx < len(messages):
                        messages[idx]["content"] = _PROCESSED_LOG_STUB

        # Scheme B：本轮结束后按预算压缩历史
        if TOKEN_OPT_ENABLED:
            n_compacted = _compact_history(messages)
            if n_compacted:
                logger.debug("ReAct 循环第 %d 轮：压缩了 %d 条较早工具输出", i + 1, n_compacted)

        logger.debug("ReAct 循环第 %d 轮完成", i + 1)

    return {"text": "（已达到最大迭代次数，做梦结束）", "token_usage": _usage_snapshot()}


# ---------------------------------------------------------------------------
# Deep Sleep — 创建临时 Sleep Agent 执行多步推理
# ---------------------------------------------------------------------------

def _build_stage_prompt(stage: str, logs: list[Path], trigger: str,
                        extraction_summary: str = "") -> str:
    """构建各阶段 system prompt。策略正文复用 memory-extraction skill。"""
    # Prompt 工坊 override：现读外置文件，有则整体替换内置默认（含策略正文），无则走内置
    _ov = _load_stage_prompt_override(stage)
    if _ov is not None:
        return _build_stage_prompt_filled(stage, _ov, logs, trigger, extraction_summary)
    strategy = ""
    if MEMORY_EXTRACTION_SKILL.exists():
        t = MEMORY_EXTRACTION_SKILL.read_text(encoding="utf-8")
        _, body = mm._parse_frontmatter(t)
        strategy = f"## 做梦策略\n\n{body}\n\n"

    now = datetime.now().isoformat(timespec="seconds")

    if stage == "extract":
        return (
            "你是「记忆整理助手」，处于做梦的【① Deep 提取】阶段。\n"
            "职责：逐个读取待处理 session-log，提取/更新记忆、记录情景、更新画像，"
            "并 mark_log_processed。\n\n"
            "## 本阶段纪律\n"
            "- 只做提取与更新：create/update_knowledge、create/update_skill、"
            "archive_item、record_episode、update_about_me/user、mark_log_processed\n"
            "- 不要调用 write_dream_log / decay_all_knowledge / append_speculation——"
            "它们属于后续阶段，本阶段工具栏里没有\n"
            "- 省 token 纪律：读 → 记要点（字面量原样抄）→ mark_log_processed → 再提取\n"
            "- 字面量保真：路径/命令/端口/版本号与原文逐字一致，拿不准宁可不写\n"
            "- 去重：新建前先 list_items 查同主题条目，有则 update 不 create\n"
            "- 情景记忆：实质性事件默认 record_episode（salience low），纯查询/闲聊跳过\n"
            "- 缺口上报：日志里出现「想做的事没做成」且归因是缺工具/缺环境适配时，"
            "用 emit_gap_signal 记录（gap 只描述缺什么，禁止写解决方案）；"
            "纯事实类问题（在哪/是什么）不要上报；本夜没有就不上报，宁缺勿滥\n"
            "- ⚠️ **gap_key 必须稳定（决定插件该不该造）**：每条缺口都要给 gap_key"
            "（kebab-case，2~4 个词，如 patrol-schedule / daily-broadcast / dream-trigger）；"
            "先 list 本会话已有缺口与近期台账口径，**同一件事换个说法必须用同一个 key**——"
            "上游按 key 跨夜累计，凑够阈值才允许进插件生成（安全类 1 日、用户纠偏 2 日、普通 3 日）。"
            "同一夜重复报同一件事只算 1 次；不要为了让某个 key 达标而拆成多条\n"
            "- ⚠️ **身份分离铁律（防做梦反噬清醒身份）**：about_me 写的是「清醒时的 Work Agent somni」——"
            "用户的工程搭子。你（Sleep Agent）的职责（做梦/提取/记忆整理）只是后台维护机制，"
            "**不是 somni 的人格**：严禁把「记忆管理助手/做梦/提取记忆」等睡眠角色写进 about_me；"
            "严禁编造源日志中不存在的身份经历。自我认知只能来自对话中真实发生的身份讨论；"
            "无依据时不更新 about_me（宁缺勿编）\n"
            "- 隐私：不提取无关第三方的个人隐私数据（手机号、身份证号等），"
            "但允许提取主人的密码、token、凭证（本系统为个人私人助理）\n"
            "- 反馈识别：负向反馈=修正条目内容（body 只留正确当前值，完全替换旧值，stability/score 不变）；"
            "正向反馈=stability+1, score+0.2；做梦巩固（被引用/提及但非明确反馈）=stability+1, score+0.1；"
            "强负向（用户要求删除）=archive_item\n"
            "- 画像分流：用户画像信息用 update_about_user 更新（多用户按 session-log frontmatter 的 "
            "user 字段传 user_id，不同用户的画像不混），不分散进多个 knowledge 条目\n"
            "- 产出归类：操作类经验（有完整步骤、可复用流程）→ create_skill（agent_learned_skills/）；"
            "认知类（偏好/事实/教训）→ create_knowledge\n"
            "- 效率审视（10a）：本 log 的 tool_call 链有可合并/脚本化点 → update_skill 追加「效率优化」段"
            "（仅限本 log 实际涉及的 skill，快判快走）\n\n"
            f"触发方式：{trigger}，当前时间：{now}\n\n" + strategy
        )

    if stage == "rem":
        return (
            "你是「记忆整理助手」，处于做梦的【② REM 推演】阶段（反事实通道）。\n"
            "职责：基于本轮提取的成果，对「环境/模型能力缺口类失败」做反事实推演，"
            "把可验证的猜想挂载到对应 skill。\n\n"
            "## 本阶段纪律\n"
            "- 只用 append_speculation 挂猜想（推演依据/预期收益/验证动作三件套）\n"
            "- 每夜至多 2 条，宁缺勿滥；无可判定验证动作的不挂\n"
            "- 不挂定义性缺口/tool-bug 类失败（那些不是环境或能力问题）\n"
            "- 只挂①阶段实际新增/更新的 skill 相关猜想；没有合适的就不挂\n"
            "- 效率优化类（10a）属①阶段职责，本阶段不做；只做反事实推演挂猜想\n"
            "- 禁止在本阶段做记忆增删改\n\n"
            "## ①阶段提取摘要\n" + (extraction_summary or "（无摘要，按 list_items 自查）") + "\n\n"
            f"触发方式：{trigger}，当前时间：{now}\n\n" + strategy
        )

    if stage == "consolidate":
        return (
            "你是「记忆整理助手」，处于做梦的【③ 巩固】阶段。\n"
            "职责：全量衰减淘汰 → 猜想核销（仅限本夜日志有确凿证据的）→ 写 dream-log。\n\n"
            "## 本阶段纪律\n"
            "- 先 decay_all_knowledge 执行全量衰减与淘汰\n"
            "- 再核销：本夜 session-log 有确凿证据的 pending 猜想，验证用 "
            "mark_speculation_verified，证伪用 remove_speculation；证据不足不动\n"
            "- 最后 write_dream_log 记录本次做梦全程（含①提取、②推演、③巩固三段）\n"
            "- ⚠️ 每夜只写一份 dream-log：frontmatter 必须含 dream_id/trigger/start_time/end_time/"
            "processed_logs 字段，正文用「## 提取结果」等既定小节；禁止自创 date:/type:/phase: 头\n"
            "- dream-log 格式模板：\n"
            "```\n"
            "---\n"
            "dream_id: {日期}_dream_{序号}\n"
            "trigger: {触发方式}\n"
            "start_time: {开始时间}\n"
            "end_time: {结束时间}\n"
            "processed_logs:\n"
            "  - {log文件名}\n"
            "---\n\n"
            "## 提取结果\n\n"
            "### 新增记忆\n"
            "| 名称 | 类型 | 来源 session-log | 提取依据 |\n"
            "### 更新条目\n"
            "| 名称 | 变更 | stability 变化 | 原因 |\n"
            "### 淘汰条目\n"
            "| 名称 | stability | score | 淘汰原因 |\n"
            "### 反馈识别与处理\n"
            "| 条目 | 信号类型 | 依据 | 处理 |\n"
            "### 推演\n"
            "挂载: 猜想id|skill|验证动作 / 核销: 猜想id|结论|证据log|动作\n"
            "### 策略自适应记录\n"
            "本次调整：...\n\n"
            "## Sleep Agent 推理过程摘要\n"
            "1. ...\n"
            "```\n"
            "- 禁止再做记忆内容增删（核销除外）\n\n"
            "## ①阶段提取摘要\n" + (extraction_summary or "（无摘要）") + "\n\n"
            f"触发方式：{trigger}，当前时间：{now}\n\n" + strategy
        )

    # review：占位阶段（第二批单独评审），当前只做只读确认
    return (
        "你是「记忆整理助手」，处于做梦的【④ 对抗 Review】阶段（占位）。\n"
        "本阶段为后续对抗式评审预留：当前只需复核①~③产物无异常后，"
        "用一句话回复「REVIEW PASS」即可。不要做任何写操作。\n"
    )


def _build_stage_prompt_filled(stage: str, body: str, logs: list[Path], trigger: str,
                               extraction_summary: str = "") -> str:
    """override prompt 整体替换：body 即最终 system prompt，只补注入槽位。

    槽位约定（写 override 时自行嵌入，缺省不注入）：
      {strategy}  — memory-extraction skill 策略正文
      {logs}      — 本夜待处理 session-log 列表（仅 extract 段有意义）
      {summary}   — ①阶段提取摘要（rem/consolidate 用）
      {trigger}   — 触发方式；{now} — 当前时间
    """
    strategy = ""
    if MEMORY_EXTRACTION_SKILL.exists():
        try:
            t = MEMORY_EXTRACTION_SKILL.read_text(encoding="utf-8")
            _, sbody = mm._parse_frontmatter(t)
            strategy = f"## 做梦策略\n\n{sbody}\n\n"
        except Exception as e:
            logger.warning("读做梦策略失败：%s", e)
    log_paths_str = "\n".join(f"- {log}" for log in logs) if stage == "extract" else ""
    now = datetime.now().isoformat(timespec="seconds")
    out = body
    for k, v in {"strategy": strategy, "logs": log_paths_str,
                 "summary": extraction_summary or "（无摘要）",
                 "trigger": trigger, "now": now}.items():
        out = out.replace("{" + k + "}", v)
    return out


async def _run_stage(stage: str, logs: list[Path], trigger: str, dream_id: str,
                     model_name: str, api_key: str, base_url: str,
                     extraction_summary: str = "",
                     shared_messages: list[dict] | None = None,
                     rem_summary: str = "",
                     consolidate_summary: str = "") -> dict:
    """运行单个阶段：独立 system prompt + 工具子集 + 上下文，产物落 staging。

    SHARED_CTX_ENABLED 时，rem/consolidate 可传入 shared_messages 续用①的
    对话上下文（段间交接靠追加 user 消息）；review 永远冷启动（对抗隔离红线）。
    """
    _stage_begin(dream_id, stage, trigger)
    try:
        tools_schema = _stage_tools_schema(stage)
        sys_prompt = _build_stage_prompt(stage, logs, trigger, extraction_summary)

        if stage == "extract":
            log_paths_str = "\n".join(f"- {str(log)}" for log in logs)
            user_msg = (
                f"以下是待处理的 session-log 文件（共 {len(logs)} 个，已按时间旧→新排列）。\n"
                f"请严格按列表顺序从上到下逐个处理，不要跳跃或乱序。\n\n{log_paths_str}"
            )
            max_iters = max(30, len(logs) * 10 + 15)
        elif stage == "rem":
            user_msg = (
                "①阶段已完成提取。请审阅已有 skill（list_items），对属于环境/模型能力缺口类"
                "失败且本夜日志有依据的点做反事实推演，用 append_speculation 挂猜想（至多 2 条）。"
                "无合适对象则直接回复「本夜无猜想可挂」。"
            )
            max_iters = 12
        elif stage == "consolidate":
            user_msg = (
                "①②阶段已完成。请依序执行：decay_all_knowledge → "
                "（有确凿证据才）核销猜想 → write_dream_log（含①②③全程）。"
            )
            # v3：consolidate 不续用共享历史（prefill 大头），冷启动只带①②摘要
            # 修 2026-11-03：原来只拼②摘要，①摘要丢失 → ③断言「①无摘要」→ review 必 FAIL
            if extraction_summary:
                user_msg += "\n\n## ①阶段提取产物摘要\n" + extraction_summary[:800]
            if rem_summary:
                user_msg += "\n\n## ②阶段推演产物摘要\n" + rem_summary[:800]
            max_iters = 15
        else:  # review
            # 2026-10-28 修：原实现只传①摘要，②③产物缺失 → 复核必然判 FAIL；
            # 现在把三段摘要按序拼进 user 消息（review 冷启动，不续用共享历史）
            user_msg = (
                "复核①②③阶段产物，无异常则回复「REVIEW PASS」。\n\n"
                "## ①阶段提取摘要\n" + (extraction_summary or "（无摘要）") + "\n\n"
                "## ②阶段推演产物摘要\n" + ((rem_summary or "（无摘要）")[:800]) + "\n\n"
                "## ③阶段巩固产物摘要\n" + ((consolidate_summary or "（无摘要）")[:800])
            )
            max_iters = 4

        # v3：仅 rem 续用共享历史；consolidate 冷启动（省 prefill）
        use_shared = (SHARED_CTX_ENABLED and shared_messages is not None
                      and stage == "rem")
        if use_shared:
            # 段间续用：不重发 system，阶段纪律走交接 user 消息（工具子集仍按段切换）
            handoff = user_msg
            _ov = _load_stage_prompt_override(stage)
            if _ov is not None:
                handoff += "\n\n## 本阶段补充纪律（外置 override）\n" + _ov.strip()
            if stage == "rem":
                handoff += "\n（①阶段过程与提取结果已在上方对话上下文中，无需 list_items 全量自查）"
            shared_messages.append({"role": "user", "content": handoff})
            messages = shared_messages
            # 段间压缩： tighter 预算，防 extract 长历史拖累后续段
            try:
                _compact_history(messages, budget=SLEEP_STAGE_HANDOFF_BUDGET, keep_recent=4)
            except Exception:
                pass
        else:
            # 冷启动：extract 首段 / review（永远隔离）/ 关闭共享开关时的旧行为
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ]
        react = await _run_react_loop(messages, tools_schema, model_name, api_key,
                                      max_iters, base_url, stage=stage, dream_id=dream_id)
        # 阶段产物落盘
        art_name = {"extract": "extraction.md", "rem": "speculation.md",
                    "consolidate": "dream.md", "review": "review.md"}.get(stage, "out.md")
        _stage_write_artifact(dream_id, stage, art_name, react.get("text") or "")
        _stage_end(dream_id, stage, "done", (react.get("text") or "")[:600])
        return react
    except SleepInterruptedError:
        _stage_end(dream_id, stage, "failed", "interrupted")
        raise
    except Exception as e:
        logger.exception("stage %s failed", stage)
        _stage_end(dream_id, stage, "failed", str(e)[:600])
        return {"text": "", "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}}


async def _run_deep_sleep(logs: list[Path], trigger: str) -> dict:
    """四阶段真拆调度：①提取 → ②REM 推演 → ③巩固 → ④对抗 Review（占位）。

    一个 sleep agent 状态机多阶段：同一段代码里的四个串行 ReAct 循环，
    每段独立 system prompt + 工具子集 + 消息历史，段间传产物文件。
    产物落 memory/dream-staging/<dream_id>/（state.json + 各段 md）。
    """
    # 1. 加载 API 配置：优先从 vault 读取（与 Work Agent 一致），fallback 到变量
    api_key = None
    base_url = (os.environ.get("SLEEP_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model_name = os.environ.get("SLEEP_MODEL", "")

    try:
        import secret_vault
        vault = secret_vault.get_vault()
        base_url = vault.get_base_url() or base_url
        if not model_name:
            model_name = vault.get_model() or "deepseek-v4-flash"
        api_key = secret_vault.resolve_api_key()
    except Exception:
        pass

    if not api_key:
        api_key = os.environ.get("SLEEP_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        key_file = Path(os.path.expanduser("~/.dashscope/api_key"))
        if key_file.exists():
            api_key = key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        try:
            import secret_vault as _sv
            api_key = _sv.BUILTIN_API_KEY
        except Exception:
            pass

    if not api_key and _chat_backend is None:
        return {"status": "error", "reason": "no_api_key"}

    if not model_name:
        model_name = (os.environ.get("SLEEP_MODEL") or os.environ.get("OPENAI_MODEL")
                      or os.environ.get("DASHSCOPE_MODEL", "qwen-plus"))

    dream_id = _new_dream_id()
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    interrupted = None
    extraction_summary = ""
    rem_summary_txt = ""
    consolidate_summary_txt = ""
    # ①②③ 共享上下文（SHARED_CTX_ENABLED=0 或②③显式不传时仍走旧冷启动路径）
    shared_messages: list[dict] | None = [] if SHARED_CTX_ENABLED else None

    # ① Deep 提取（核心阶段；失败则整夜失败，不硬撑后续段）
    try:
        r1 = await _run_stage("extract", logs, trigger, dream_id,
                              model_name, api_key, base_url,
                              shared_messages=shared_messages)
        for k in total_usage:
            total_usage[k] += r1.get("token_usage", {}).get(k, 0)
        extraction_summary = (r1.get("text") or "")[:1200]
    except SleepInterruptedError as e:
        interrupted = e

    # [laneB-direct-1022 → 2026-10-28 改回] 默认跑全 ①提取→②REM→③巩固→④Review 四阶段；
    # 演示想快点进泳道B 插件生成时设 SLEEP_ONLY_EXTRACT=1 才掐掉 ②③④。
    _only_extract = os.environ.get("SLEEP_ONLY_EXTRACT", "0").strip().lower() in ("1", "true", "yes", "on")
    if _only_extract:
        for _sk in ("rem", "consolidate", "review"):
            try:
                _stage_begin(dream_id, _sk, trigger)
                _stage_end(dream_id, _sk, "skipped", "已关闭（SLEEP_ONLY_EXTRACT=1）：只跑①提取 + 泳道B 插件生成")
            except Exception:
                pass
    else:
        # ② REM 推演（依赖①；①被中断则跳过）
        if interrupted is None:
            try:
                r2 = await _run_stage("rem", logs, trigger, dream_id,
                                      model_name, api_key, base_url,
                                      extraction_summary=extraction_summary,
                                      shared_messages=shared_messages)
                for k in total_usage:
                    total_usage[k] += r2.get("token_usage", {}).get(k, 0)
                rem_summary_txt = (r2.get("text") or "")
            except SleepInterruptedError as e:
                interrupted = e

        # ③ 巩固（依赖①②；即使②失败也要做衰减+写 dream-log，保证不丢账）
        if interrupted is None:
            try:
                r3 = await _run_stage("consolidate", logs, trigger, dream_id,
                                      model_name, api_key, base_url,
                                      extraction_summary=extraction_summary,
                                      rem_summary=rem_summary_txt)
                for k in total_usage:
                    total_usage[k] += r3.get("token_usage", {}).get(k, 0)
                consolidate_summary_txt = (r3.get("text") or "")
            except SleepInterruptedError as e:
                interrupted = e

        # ④ 对抗 Review（占位复核；失败不阻断）
        if interrupted is None:
            try:
                r4 = await _run_stage("review", logs, trigger, dream_id,
                                      model_name, api_key, base_url,
                                      extraction_summary=extraction_summary,
                                      rem_summary=rem_summary_txt,
                                      consolidate_summary=consolidate_summary_txt)
                for k in total_usage:
                    total_usage[k] += r4.get("token_usage", {}).get(k, 0)
            except SleepInterruptedError as e:
                interrupted = e

    if interrupted is not None:
        logger.info("Sleep Agent 被中断: %s", interrupted)
        tu = dict(total_usage)
        tu["total_tokens"] = tu["prompt_tokens"] + tu["completion_tokens"]
        return {"status": "interrupted", "reason": str(interrupted), "token_usage": tu}

    # 缺口信号只由①emit_gap_signal 产出 → 这里直接交给插件生成七阶段（后台线程，不阻塞收尾）
    try:
        _dispatch_lane_b(dream_id)
    except Exception:
        logger.exception("泳道B dispatch 失败（已忽略）")

    logger.info("Sleep Agent ①提取完成 → 泳道B 插件生成已派发 (%s), token: %s", dream_id, total_usage)
    total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]
    return {
        "status": "deep_sleep",
        "dream_id": dream_id,
        "summary": extraction_summary or "（提取阶段无输出）",
        "token_usage": total_usage,
    }


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

# 单次做梦最大处理 log 数量，避免长时间运行被中断概率增大
MAX_LOGS_PER_SLEEP = int(os.environ.get("SLEEP_MAX_LOGS", "30"))


async def run_sleep(trigger: str = "idle_timeout") -> dict:
    """执行一次 Sleep（做梦）。统一走 Deep Sleep，分批处理 pending session-log。

    当 pending logs 超过 MAX_LOGS_PER_SLEEP 时，只处理最旧的 N 个（时间正序，旧→新），
    剩余的留待下次做梦处理，保证时间连续性和记忆覆盖正确性。
    """
    if _sleep_lock.locked():
        logger.info("Sleep Agent 已在运行，跳过")
        return {"status": "skipped", "reason": "already_running"}

    result = await _run_sleep_locked(trigger)
    # 做梦有实际产出后，在锁外同步向量索引（hash-diff 增量；provider=off 时为 no-op）。
    # 放在锁外：sync 是纯派生物重建，不该阻塞下一轮做梦；异常全部吞掉不影响返回值。
    if result.get("status") != "skipped":
        try:
            await asyncio.to_thread(_vec_sync_after_sleep)
        except Exception:
            logger.exception("Sleep Agent: 向量索引同步失败（已忽略）")
    return result


# --- 泳道B 调度：缺口信号 → gen-plugin.sh 七阶段（确定性 LLM 不参与路由，路由由 S1 collect_gap 做；S6 审视是唯一独立 LLM 审视会话）---
_LANE_B_TIMEOUT = 3600  # 秒，七阶段含 LLM 生成与 VETO 重写，兜底上限
# 泳道B 全局单飞：多个梦同时结束会各自起 worker 抢 fuyao 网关（实测多路并发即超时/断流），
# 这里整条泳道串行化；抢不到锁的夜直接标 skipped，等下一夜或人工补跑，不硬挤。
_LANE_B_LOCK = __import__("threading").Lock()


def _dispatch_lane_b(dream_id: str) -> None:
    """本夜缺口信号逐条喂插件生成流水线；无信号时记 skipped 不跑。"""
    import subprocess
    from threading import Thread

    signals = list(_GAP_SIGNAL_CURRENT)
    _GAP_SIGNAL_CURRENT.clear()
    state_path = Path("/xdata/dsh/home/somni/memory/dream-staging") / dream_id / "state.json"
    lane_b = {
        "signalCount": len(signals),
        "runs": [],
        "startedAt": datetime.now().isoformat(timespec="seconds"),
    }

    def _update_state():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {"dream_id": dream_id, "stages": {}}
        state["laneB"] = lane_b
        try:
            tmp = state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(state_path)
        except Exception:
            pass

    if not signals:
        lane_b["status"] = "skipped"
        lane_b["detail"] = "本夜无缺口信号，泳道B未触发"
        lane_b["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        _update_state()
        return

    if not _LANE_B_LOCK.acquire(blocking=False):
        lane_b["status"] = "skipped"
        lane_b["detail"] = "另一夜的泳道B仍在生成，本夜排队跳过（避免并发挤爆 LLM 网关）"
        lane_b["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        _update_state()
        logger.info("泳道B 已有 run 在跑，%s 跳过", dream_id)
        return

    lane_b["status"] = "running"

    def _worker():
      try:
        try:
            from . import gap_ledger  # 包内相对导入（裸 import 会 ModuleNotFoundError）
        except Exception:
            try:
                import gap_ledger
            except Exception:
                gap_ledger = None

        for i, sig in enumerate(signals):
            gkey = ""
            entry = {"signal": (sig.get("gap") or "")[:80], "gapKey": "",
                     "status": "building", "reason": ""}
            lane_b["runs"].append(entry)
            _update_state()

            # 准入闸门：同一缺口要累计够天数/次数才允许进入生成，避免 nightly 造一次性插件
            if gap_ledger is not None:
                try:
                    ent = gap_ledger.record(sig, dream_id)
                    gkey = ent["key"]
                    entry["gapKey"] = gkey
                    passed, why = gap_ledger.evaluate(ent)
                    if not passed:
                        entry["status"] = "gated"
                        entry["reason"] = why
                        _update_state()
                        logger.info("泳道B 闸门拦下 %s: %s", gkey, why)
                        continue
                    gap_ledger.mark_building(gkey, dream_id)
                    entry["reason"] = why
                    _update_state()
                except Exception as e:
                    logger.warning("泳道B 闸门异常，放行: %s", e)
            proc = None
            try:
                inp = {
                    "trigger": "做梦泳道B（%s ①提取缺口信号）" % dream_id,
                    "gap": sig.get("gap") or "",
                    "signalType": sig.get("signalType") or "intent-no-tool",
                    "sessionEvidence": sig.get("sessionEvidence") or "(none)",
                    "failContext": sig.get("failContext", ""),
                    "failCount": sig.get("failCount", 1),
                    "expectPluginId": sig.get("expectPluginId") or "unused-laneb-%s-%d" % (dream_id, i),
                }
                in_path = Path("/tmp/laneb-input-%s-%d.json" % (dream_id, i))
                in_path.write_text(json.dumps(inp, ensure_ascii=False, indent=2), encoding="utf-8")
                proc = subprocess.run(
                    ["bash", "/xdata/dsh/plugingen/v2/bin/gen-plugin.sh", str(in_path)],
                    capture_output=True, text=True, timeout=_LANE_B_TIMEOUT,
                )
                m = re.search(r"\[(\d{8}-\d{6})-[^\]]+\] pipeline", proc.stdout or "")
                run_id = m.group(0) if False else None
                # run 目录名 = RUN_ID-PLUGIN_ID，从 generated 目录取最新即可（下面扫）
            except Exception as e:
                logger.exception("泳道B run %d 异常: %s", i, e)
                entry["status"] = "error"
                entry["reason"] = "%s: %s" % (type(e).__name__, str(e)[:200])
                _update_state()
            # 从 generated 目录反查本次 run（trigger 含 dream_id 的最新目录）
            try:
                gen_root = Path("/xdata/dsh/market/generated")
                cands = [d for d in gen_root.iterdir()
                         if d.is_dir() and (d / "manifest.json").exists()]
                cands.sort(key=lambda d: d.stat().st_mtime, reverse=True)
                rid = None
                for d in cands[:3]:
                    try:
                        mf = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                        if dream_id in (mf.get("trigger") or ""):
                            rid = d.name
                            break
                    except Exception:
                        continue
                if rid:
                    entry["run"] = rid
                    entry["status"] = "built"
                elif entry.get("status") != "error":
                    entry["status"] = "no-run-dir"
                    entry["reason"] = ((proc.stdout or "") + (proc.stderr or ""))[-300:] if proc else ""
                _update_state()
                if gap_ledger is not None and gkey:
                    ok = bool(rid)
                    gap_ledger.mark_build_result(gkey, ok, rid or "", "" if ok else "no run dir")
            except Exception as e:
                if gap_ledger is not None and gkey:
                    try:
                        gap_ledger.mark_build_result(gkey, False, "", str(e))
                    except Exception:
                        pass
        lane_b["status"] = "done"
        lane_b["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        lane_b["runCount"] = len([r for r in lane_b["runs"] if r.get("status") != "gated"])
        _update_state()
      finally:
        try:
            _LANE_B_LOCK.release()
        except Exception:
            pass

    Thread(target=_worker, daemon=True, name="lane-b-dispatch-%s" % dream_id).start()


def _vec_sync_after_sleep() -> None:
    try:
        import memory_indexer  # type: ignore
    except ImportError:
        from backend import memory_indexer  # type: ignore
    memory_indexer.sync_quietly("after_sleep")


async def _run_sleep_locked(trigger: str) -> dict:
    async with _sleep_lock:
        _cancel_event.clear()
        logs = await asyncio.to_thread(mm.get_pending_logs)
        if not logs:
            return {"status": "skipped", "reason": "no_pending_logs"}

        # 预过滤空日志：frontmatter 之后无实际对话内容的直接标记 processed，不传给 LLM
        non_empty_logs = []
        for log in logs:
            try:
                content = log.read_text(encoding="utf-8")
                _, body = mm._parse_frontmatter(content)
                if body.strip():
                    non_empty_logs.append(log)
                else:
                    mm.mark_log_processed(log)
                    logger.info("Sleep Agent: 跳过空日志 %s", log.name)
            except Exception:
                non_empty_logs.append(log)  # 解析失败的保留给 LLM 处理
        logs = non_empty_logs
        if not logs:
            return {"status": "skipped", "reason": "no_pending_logs (all empty)"}

        total = len(logs)
        if total > MAX_LOGS_PER_SLEEP:
            logs = logs[:MAX_LOGS_PER_SLEEP]
            logger.info(
                "Sleep Agent: pending logs 共 %d 个，本次处理最旧的 %d 个（时间正序）",
                total, MAX_LOGS_PER_SLEEP,
            )

        logger.info("Sleep Agent: Deep Sleep 模式，处理 %d 个 log", len(logs))
        # 有实际工作 → 重置退避（系统活跃）
        reset_idle_backoff()
        return await _run_deep_sleep(logs, trigger)


def schedule_sleep(loop: Optional[asyncio.AbstractEventLoop] = None,
                   delay: Optional[int] = None) -> None:
    """安排一次延迟 Sleep（空闲触发）。

    delay 为空时使用动态退避间隔（连续空闲时逐步拉长）。
    显式传入 delay 时使用指定值（不影响退避状态）。
    """
    global _sleep_timer

    if _sleep_timer and not _sleep_timer.done():
        _sleep_timer.cancel()

    _delay = delay if delay is not None else _current_idle_delay()
    _delay = max(1, int(_delay))

    async def _delayed():
        await asyncio.sleep(_delay)
        result = await run_sleep("idle_timeout")
        # 空闲退避：无 pending logs 时推进退避级别，下次间隔更长
        if result.get("reason") == "no_pending_logs" or result.get("reason") == "no_pending_logs (all empty)":
            _advance_idle_backoff()

    _loop = loop or asyncio.get_running_loop()
    _sleep_timer = _loop.create_task(_delayed())


async def cancel_sleep() -> None:
    """取消或中断 Sleep Agent（用户重新连接时）。同时重置空闲退避。"""
    global _sleep_timer

    if _sleep_timer and not _sleep_timer.done():
        _sleep_timer.cancel()
        _sleep_timer = None

    # 新消息到达 → 重置退避，下次做梦用最短间隔
    reset_idle_backoff()

    _cancel_event.set()
    logger.info("Sleep Agent cancel_event 已设置，将在下次工具调用时中断")
