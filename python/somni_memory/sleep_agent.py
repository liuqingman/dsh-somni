# -*- coding: utf-8 -*-
"""Sleep Agent — 做梦模块（自实现 ReAct 循环）。

- Sleep Agent 使用自实现的简化 ReAct 循环，不依赖任何 agent 框架
- 一组专用工具封装 memory_manager 底层操作，LLM 自主决策记忆提取/修正/衰减
- 统一走 Deep Sleep 模式，每次做梦完整处理所有 pending session-log
- 支持可靠打断机制：通过 SleepInterruptedError 异常强制终止循环
- LLM 后端可插拔（set_chat_backend）：默认 OpenAI 兼容接口，宿主（如 DSH 插件）
  可注入自己的 chat 函数，让做梦复用宿主的模型与凭证
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import memory_manager as mm
from .token_utils import estimate_tokens

logger = logging.getLogger("sleep_agent")

# 空闲触发延迟（秒）
IDLE_DELAY = int(os.environ.get("SLEEP_IDLE_DELAY", "30"))

# ── Token 优化相关配置（schemes A/B） ──────────────────────────────────────
# 总开关：设为 "0" 可整体关闭 A/B/C 优化（用于对照测量或紧急回退）
TOKEN_OPT_ENABLED = os.environ.get("SLEEP_TOKEN_OPT", "1") != "0"
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

# Skill 目录（与 memory_manager 共用同一解析：SKILL_ROOT_PATH > <SOMNI_DATA_DIR>/skills）
SKILL_DIR = mm._get_skill_dir()

# 做梦策略文件：优先用户在 skill 目录下的覆写版，否则用包内自带的默认版
_BUNDLED_STRATEGY = Path(__file__).parent / "prompts" / "memory-extraction.md"


def _resolve_strategy_file() -> Path:
    override = SKILL_DIR / "base_skill" / "memory-extraction" / "SKILL.md"
    return override if override.exists() else _BUNDLED_STRATEGY


MEMORY_EXTRACTION_SKILL = _resolve_strategy_file()

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
        entry.body = body
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
    new_body = body if body else old_body
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
        return (f"拒绝写入：about_me 是「清醒时的 Work Agent」的自我认知，"
                f"检测到睡眠角色描述 {_hits}。做梦/提取/衰减是后台维护机制，不是 Work Agent 的人格——"
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
                 如 channel:user_xxx）；不确定就用 default。

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


# 所有 Sleep 专用工具（包装 cancel 检查）
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
]

SLEEP_TOOLS = [_check_cancel(fn) for fn in SLEEP_TOOLS_RAW]


# ---------------------------------------------------------------------------
# 工具注册表（供 ReAct 循环调用）
# ---------------------------------------------------------------------------

def _build_tools_schema() -> list[dict]:
    """从工具函数的 docstring 和 annotations 构建 OpenAI function 格式的工具描述。

    描述只取 docstring 首行（做梦系统提示已包含完整工作流，工具描述保持简短省 token）。
    """
    from .toolspec import schema_from_function
    tools = []
    for fn in SLEEP_TOOLS:
        spec = schema_from_function(fn)
        first_line = (fn.__doc__ or "").strip().split("\n", 1)[0]
        tools.append({
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": first_line or spec["name"],
                "parameters": spec["parameters"],
            },
        })
    return tools


def _get_tool_by_name(name: str) -> Optional[Callable]:
    """根据名称查找工具函数。"""
    for fn in SLEEP_TOOLS:
        if fn.__name__ == name:
            return fn
    return None


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
        "1. 使用 `review_dream_history` 回顾近期 dream-log（无 path 列出列表，有 path 读取内容），评估过去的提取效果（策略自适应）\n"
        "2. 使用 `read_session_log` 严格按列表顺序从上到下（时间旧→新）逐个读取待处理的 session-log，处理完一个再处理下一个，不跳跃\n"
        "3. 使用 `list_items(scope=\"all\")` 查看已有 knowledge 和 skill，用于去重、合并和整合判断\n"
        "4. 识别反馈信号（正向/负向）：\n"
        "   - 知识条目：`update_knowledge` 修正/巩固，强负向用 `archive_item` 归档\n"
        "   - 已有同主题 skill：`update_skill` 整合增强（agent_learned_skills/ 可覆写；其他目录仅追加/修正）\n"
        "5. 提取新记忆 → `create_knowledge`\n"
        "6. 提取新操作类技能 → `create_skill`（写入 agent_learned_skills/）\n"
        "7. 识别用户画像信息 → `update_about_user` 更新用户画像（多用户：按 session-log frontmatter 的 "
        "`user` 字段把信息更新到对应用户，传 user_id；不同用户的画像不要混在一起）\n"
        "8. 识别 Agent 自我认知信息（性格、能力、角色定位等）→ `update_about_me` 更新自我画像\n"
        "   ⚠️ **身份分离铁律（防做梦反噬清醒身份）**：about_me 写的是「清醒时的 Work Agent」——"
        "用户的工程搭子。你（Sleep Agent）的职责（做梦/提取/衰减/记忆整理）只是后台维护机制，"
        "**不是 Work Agent 的人格**：严禁把「记忆管理助手/做梦/提取记忆」等睡眠角色写进 about_me；"
        "严禁编造源日志中不存在的身份经历（如「用户误称我为X」——源日志无此事即是编造）。"
        "自我认知只能来自对话中真实发生的身份讨论；无依据时不更新 about_me（宁缺勿编）。\n"
        "9. 跨条目阅读详情 → `read_item`（自动定位 knowledge 或 skill 目录）\n"
        "10. **效率审视**：审视对话中的 tool_call 链，发现可合并/可脚本化/可精简的优化点，"
        "用 `update_skill` 追加到对应 skill 的 `## 效率优化` section（详见做梦策略中「效率审视」章节）\n"
        "11. 使用 `decay_all_knowledge` 执行全量衰减和淘汰\n"
        "12. 使用 `mark_log_processed` 标记每个已处理的 session-log\n"
        "13. 使用 `write_dream_log` 记录本次做梦的完整过程（按设计格式）\n\n"
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
# LLM 后端（可插拔）
#
# ChatBackend(messages, tools) -> (assistant_message_dict, usage_dict)
#   assistant_message_dict: OpenAI 格式 {role, content, tool_calls?, reasoning_content?}
#   usage_dict: {"prompt_tokens": N, "completion_tokens": M, "cached_tokens": C}
# 默认后端走 OpenAI 兼容接口（环境变量 SLEEP_BASE_URL / SLEEP_API_KEY / SLEEP_MODEL，
# 回退 OPENAI_BASE_URL / OPENAI_API_KEY）；宿主可用 set_chat_backend() 注入自己的实现。
# ---------------------------------------------------------------------------

ChatBackend = Callable[[list, list], Awaitable[tuple]]

_chat_backend: Optional[ChatBackend] = None


def set_chat_backend(fn: Optional[ChatBackend]) -> None:
    """注入 LLM chat 实现（None = 恢复默认 OpenAI 兼容后端）。"""
    global _chat_backend
    _chat_backend = fn


def _resolve_openai_config() -> Optional[dict]:
    """从环境变量解析默认后端配置；缺 key 或 model 返回 None。"""
    api_key = os.environ.get("SLEEP_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    base_url = os.environ.get("SLEEP_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = os.environ.get("SLEEP_MODEL") or os.environ.get("OPENAI_MODEL") or ""
    if not api_key or not model:
        return None
    return {"api_key": api_key, "base_url": base_url, "model": model}


async def _openai_chat(messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
    """默认后端：OpenAI 兼容 Chat Completions。"""
    cfg = _resolve_openai_config()
    if cfg is None:
        raise RuntimeError("no_llm_backend: set SLEEP_API_KEY/SLEEP_MODEL or inject one via set_chat_backend()")
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    response = await client.chat.completions.create(
        model=cfg["model"],
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
    # 提取 prefix-cache 命中 token（可观测）。
    # DeepSeek 返回 prompt_cache_hit_tokens；OpenAI 系返回 prompt_tokens_details.cached_tokens。
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


async def _call_chat(messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
    fn = _chat_backend or _openai_chat
    message, usage = await fn(messages, tools)
    usage = dict(usage or {})
    usage.setdefault("prompt_tokens", 0)
    usage.setdefault("completion_tokens", 0)
    usage.setdefault("cached_tokens", 0)
    return message, usage


def llm_available() -> bool:
    """是否存在可用的 LLM 后端（已注入，或环境变量配全）。"""
    return _chat_backend is not None or _resolve_openai_config() is not None


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


async def _run_react_loop(messages: list[dict], tools_schema: list[dict], max_iters: int) -> dict:
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

        response, usage = await _call_chat(messages, tools_schema)
        total_prompt += usage.get("prompt_tokens", 0)
        total_completion += usage.get("completion_tokens", 0)
        total_cached += usage.get("cached_tokens", 0)

        tool_calls = response.get("tool_calls")
        if not tool_calls:
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

            tool_fn = _get_tool_by_name(func_name)
            if tool_fn is None:
                tool_result = f"错误：未知工具 '{func_name}'"
            else:
                try:
                    tool_result = tool_fn(**func_args)
                except SleepInterruptedError:
                    raise SleepInterruptedError("工具执行中检测到中断信号", _usage_snapshot())
                except Exception as e:
                    tool_result = f"工具执行错误：{e}"

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

async def _run_deep_sleep(logs: list[Path], trigger: str) -> dict:
    """运行 Sleep Agent 的自实现 ReAct 循环。"""

    # 1. 确认 LLM 后端可用（已注入或环境变量配全），否则直接返回，避免静默失败
    if not llm_available():
        return {"status": "error", "reason": "no_llm_backend"}

    # 2. 构建工具 schema
    tools_schema = _build_tools_schema()

    # 3. 构建 messages
    sys_prompt = _build_sleep_system_prompt()
    log_paths_str = "\n".join(f"- {str(log)}" for log in logs)

    # 预加载最近一条 dream-log 摘要，辅助策略自适应
    recent_dream_hint = ""
    if mm.DREAM_LOG_DIR.exists():
        dream_files = sorted(mm.DREAM_LOG_DIR.glob("*.md"), reverse=True)
        if dream_files:
            try:
                last_dream = dream_files[0].read_text(encoding="utf-8")
                if len(last_dream) > 1000:
                    last_dream = last_dream[:1000] + "\n...(截断)"
                recent_dream_hint = (
                    f"\n\n## 最近一次 dream-log（供策略自适应参考）\n"
                    f"文件：{dream_files[0].name}\n```\n{last_dream}\n```"
                )
            except Exception:
                pass

    user_msg = (
        f"以下是待处理的 session-log 文件（共 {len(logs)} 个，已按时间旧→新排列），"
        f"触发方式：{trigger}，当前时间：{datetime.now().isoformat(timespec='seconds')}。\n"
        f"请严格按以下列表顺序从上到下逐个处理，不要跳跃或乱序。\n\n"
        f"{log_paths_str}"
        f"{recent_dream_hint}"
    )

    messages: list[dict] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]

    # 4. 动态计算 max_iters（SLEEP_MAX_ITERS 为上限）
    max_iters = max(30, len(logs) * 10 + 15)
    cap = int(os.environ.get("SLEEP_MAX_ITERS", "0") or 0)
    if cap > 0:
        max_iters = min(max_iters, cap)

    # 5. 启动 ReAct 循环
    try:
        react_result = await _run_react_loop(messages, tools_schema, max_iters)
        result_text = react_result["text"]
        token_usage = react_result["token_usage"]
        token_usage["total_tokens"] = token_usage["prompt_tokens"] + token_usage["completion_tokens"]
        logger.info("Sleep Agent Deep Sleep 完成, token: %s", token_usage)
        return {"status": "deep_sleep", "summary": result_text, "token_usage": token_usage}
    except SleepInterruptedError as e:
        logger.info("Sleep Agent 被中断: %s", e)
        tu = e.token_usage
        tu["total_tokens"] = tu["prompt_tokens"] + tu["completion_tokens"]
        return {"status": "interrupted", "reason": str(e), "token_usage": tu}


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

# 单次做梦最大处理 log 数量，避免长时间运行被中断概率增大
MAX_LOGS_PER_SLEEP = int(os.environ.get("SLEEP_MAX_LOGS", "5"))


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


def _vec_sync_after_sleep() -> None:
    from . import memory_indexer
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
