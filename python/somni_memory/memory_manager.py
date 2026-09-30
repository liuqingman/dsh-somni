# -*- coding: utf-8 -*-
"""Memory Manager — Somni 记忆核心模块（Work-Sleep 架构）。

职责：
- 加载/保存记忆条目（knowledge/ 下的 SKILL.md）
- 构建带记忆的 system prompt 片段（identity / intentions / 回忆纪律）
- session-log 实时写入
- stability/score 衰减与淘汰

目录约定（可用环境变量覆盖）：
- MEMORY_ROOT_PATH  记忆根目录，默认 ~/.dsh/somni/memory
- SKILL_ROOT_PATH   技能根目录，默认 ~/.dsh/somni/skills
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("memory_manager")

def _default_data_root() -> Path:
    return Path(os.environ.get("SOMNI_DATA_DIR", str(Path.home() / ".dsh" / "somni")))


def _get_memory_dir() -> Path:
    return Path(os.environ.get("MEMORY_ROOT_PATH", str(_default_data_root() / "memory")))


# 保留模块级属性兼容性（通过 __getattr__ 延迟求值）
def __getattr__(name: str):
    _dir_map = {
        "MEMORY_DIR": _get_memory_dir,
        "KNOWLEDGE_DIR": lambda: _get_memory_dir() / "knowledge",
        "SESSION_LOG_DIR": lambda: _get_memory_dir() / "session-log",
        "DREAM_LOG_DIR": lambda: _get_memory_dir() / "dream-log",
        "ARCHIVED_DIR": lambda: _get_memory_dir() / "archived",
        "EPISODES_DIR": lambda: _get_memory_dir() / "episodes",
    }
    if name in _dir_map:
        return _dir_map[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 单个 session-log 文件大小上限（调小：避免单个日志过大，做梦时反复重发推高 token）
SESSION_LOG_MAX_BYTES = 24 * 1024
# 单条消息文本写入上限（超出截断+标注）：避免 [agent]/[user] 巨型正文撑大日志
SESSION_LOG_MSG_MAX_CHARS = 4000

# ── 记忆衰减曲线 ───────────────────────────────────────────────────────────
# 语义记忆（knowledge/skill）：慢衰减，越用越牢
SEMANTIC_DECAY_BASE = 0.1
# 情景记忆（episode）：快衰减，不再被提及则较快淡忘（约 1~2 周淡出）
EPISODE_DECAY_BASE = 0.25
# salience（重要度）→ 情景记忆初始 score
SALIENCE_INITIAL_SCORE = {"low": 0.4, "mid": 0.6, "medium": 0.6, "high": 0.8}


# ---------------------------------------------------------------------------
# PlatformAdapter — 平台适配点
# ---------------------------------------------------------------------------

@dataclass
class PlatformAdapter:
    sanitize_name: Callable[[str], str] = field(default_factory=lambda: lambda name: name)
    safe_move: Optional[Callable[[str, str], None]] = None
    atomic_write: Optional[Callable[[Path, str], None]] = None


_platform = PlatformAdapter()


def set_platform_adapter(adapter: PlatformAdapter) -> None:
    global _platform
    _platform = adapter


# ---------------------------------------------------------------------------
# Frontmatter 解析（纯手写，不依赖 pyyaml）
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter，返回 (meta_dict, body)。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    # 找到第二个独立行 "---" 作为 frontmatter 结束标记
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx < 0:
        return {}, text
    meta_raw = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()
    meta = {}
    # 不应被解析为数字的字段
    string_fields = {"name", "description", "created", "last_used", "last_decay", "type",
                     "related", "applies_when", "not_applies_when"}
    for line in meta_raw.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1].replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        if key in string_fields:
            meta[key] = val
        elif val.replace(".", "", 1).replace("-", "", 1).isdigit() and val.count("-") <= 1 and val.count(".") <= 1:
            meta[key] = float(val) if "." in val else int(val)
        else:
            meta[key] = val
    return meta, body


def _dump_frontmatter(meta: dict, body: str) -> str:
    """将 meta dict + body 序列化为 SKILL.md 格式。"""
    lines = ["---"]
    _need_quote_chars = {":", '"', "'", "#", "[", "{", "*", ">", "|", "---", "\n"}
    for k, v in meta.items():
        if isinstance(v, str):
            needs_quote = any(c in v for c in _need_quote_chars) or v.startswith("---")
            if needs_quote:
                escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                lines.append(f'{k}: "{escaped}"')
            else:
                lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MemoryEntry — 单条记忆
# ---------------------------------------------------------------------------

@dataclass
class MemoryEntry:
    name: str
    description: str
    entry_type: str  # user | experience | identity
    stability: int = 0
    score: float = 0.5
    created: str = ""
    last_used: str = ""
    last_decay: str = ""
    body: str = ""
    path: Path = field(default_factory=Path)
    # 非标准 frontmatter 字段（如 related / applies_when / not_applies_when），
    # 原样保留并在 save() 时回写，避免 decay 重写 frontmatter 时丢失
    extra: dict = field(default_factory=dict)

    _STD_KEYS = frozenset({"name", "description", "type", "stability", "score",
                           "created", "last_used", "last_decay"})

    @classmethod
    def load(cls, skill_md_path: Path) -> "MemoryEntry":
        text = skill_md_path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        return cls(
            extra={k: v for k, v in meta.items() if k not in cls._STD_KEYS},
            name=meta.get("name", skill_md_path.parent.name),
            description=meta.get("description", ""),
            entry_type=meta.get("type", "experience"),
            stability=int(meta.get("stability", 0)),
            score=float(meta.get("score", 0.5)),
            created=str(meta.get("created", "")),
            last_used=str(meta.get("last_used", "")),
            last_decay=str(meta.get("last_decay", "")),
            body=body,
            path=skill_md_path,
        )

    def save(self) -> None:
        meta = {
            "name": self.name,
            "description": self.description,
            "type": self.entry_type,
            "stability": self.stability,
            "score": round(self.score, 3),
            "created": self.created or str(date.today()),
            "last_used": self.last_used or str(date.today()),
            "last_decay": self.last_decay or str(date.today()),
        }
        for k, v in self.extra.items():
            if k not in meta:
                meta[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = _dump_frontmatter(meta, self.body)
        if _platform.atomic_write:
            _platform.atomic_write(self.path, content)
        else:
            self.path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 记忆加载
# ---------------------------------------------------------------------------

def _get_skill_dir() -> Path:
    return Path(os.environ.get("SKILL_ROOT_PATH", str(_default_data_root() / "skills")))


def load_all_entries() -> list[MemoryEntry]:
    """加载 knowledge/ 和 agent_learned_skills/ 下所有记忆条目。"""
    entries = []
    dirs = [
        _get_memory_dir() / "knowledge",
        _get_skill_dir() / "agent_learned_skills",
    ]
    for d in dirs:
        if not d.exists():
            continue
        for md in sorted(d.rglob("SKILL.md")):
            try:
                entries.append(MemoryEntry.load(md))
            except Exception:
                continue
    return entries


def load_identity_file(filename: str) -> str:
    """加载 about_me.md 或 about_user.md 的正文。"""
    fp = _get_memory_dir() / filename
    if not fp.exists():
        return ""
    _, body = _parse_frontmatter(fp.read_text(encoding="utf-8"))
    return body.strip()


# ---------------------------------------------------------------------------
# 构建带记忆的 system prompt
# ---------------------------------------------------------------------------

def _has_identity_data(filename: str) -> bool:
    """检查 identity 文件是否已包含有效数据（通过 frontmatter 的 has_data 字段或内容长度判断）。"""
    fp = _get_memory_dir() / filename
    if not fp.exists():
        return False
    text = fp.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    if "has_data" in meta:
        v = meta["has_data"]
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return bool(v)
    return bool(body.strip()) and len(body.strip()) > 20


def build_identity_block(*, speaker_user_id: Optional[str] = None, participant_user_ids=()) -> str:
    """常驻身份块：关于我 + 用户画像（+ 可选的知识目录）。不含前瞻记忆与回忆纪律。

    多用户：传入 speaker_user_id / participant_user_ids 时注入对应用户画像；
    不传时不注入用户画像。返回可能为空串。
    """
    parts: list[str] = []

    # about_me
    about_me = load_identity_file("about_me.md")
    if about_me:
        parts.append(f"## 关于我\n{about_me}")

    # 用户画像（多用户模式）
    if speaker_user_id or participant_user_ids:
        users_block = _build_users_block(speaker_user_id, participant_user_ids)
        if users_block:
            parts.append(users_block.strip())

    # knowledge descriptions（默认关闭：知识改由 recall_memories / 联想钩子按需检索）
    # 类脑原理：大脑在特定场景下优先激活高频/高相关知识，不是所有知识同时活跃
    if os.environ.get("SOMNI_INJECT_KNOWLEDGE_LIST", "0") == "1":
        entries = [e for e in load_all_entries() if e.entry_type != "episode"]
        if entries:
            # 按 score 降序：常用知识更可能在当前场景相关
            entries.sort(key=lambda e: e.score, reverse=True)
            # 限制注入数量：最多 50 条描述，避免 token 爆炸
            entries = entries[:50]
            desc_lines = [f"- {e.name}: {e.description}" for e in entries]
            parts.append("## 我的知识与技能\n" + "\n".join(desc_lines))

    return "\n\n".join(parts)


def build_recall_discipline() -> str:
    """回忆纪律（元记忆/知晓感 + query 构造 + 未命中重试 + 工具分工 + 两级检索）。"""
    return (
        "## 🧠 回忆纪律\n"
        "你的记忆不在上下文里，在记忆库里；回答前先确认信息来源。如果信息不在当前对话上下文中，"
        "你**必须**先检索再回答。**绝对禁止**在未检索的情况下回复\"不记得\"\"没有记录\"\"不知道\"。\n\n"
        "**触发条件（满足任一即检索）：**\n"
        "- 用户提到你当前上下文中不存在的具体信息，或用回忆性语气（\"...来着？\" \"...是什么？\" \"几个？\" \"哪个？\"）\n"
        "- 用户用代词/泛指引用：\"那个/这个/我们的/之前的/上次的\"\n"
        "- 用户问团队/项目/人物/数字/配置等具体事实，或问\"有没有做过/遇到过类似的\"\n"
        "- 你对答案有任何不确定性；或问\"最新版本/当前状态\"等时效性信息（对话里可能有更新的值覆盖旧知识）\n\n"
        "**工具分工：**\n"
        "- `recall_memories`：按**意思**回忆经验/事实/技能/往事（knowledge/skill/episode），不知道用什么词时首选\n"
        "- `search_episodes`：按**时间/标签**筛\"何时做过什么\"（支持 since/until/tags）\n"
        "- `search_recent_conversation`：按**关键词**取对话**原话**（谁说了什么、具体数字、原始报错）\n\n"
        "**query 怎么写（重要）：**先在心里做指代消解，再写成一句描述对象+场景的话，不要只丢代词或单个词。\n"
        "- ❌ recall_memories(query=\"上次\") / recall_memories(query=\"那个\")\n"
        "- ✅ recall_memories(query=\"上次和刘超讨论的模型切换与网关配置操作\")\n"
        "- ✅ recall_memories(query=\"断流时网关返回 503 no_available_workers 的处理经验\")\n"
        "- search_recent_conversation 相反要**短关键词**：\"团队有几个人来着？\" → query=\"团队\"；"
        "\"那个服务器IP是什么？\" → query=\"服务器\"\n\n"
        "**没命中怎么办：**返回里带 top1 分数；没结果或分数很低（<0.30）时**换措辞再试**，最多 3 次"
        "（换同义词、补上对象/场景/报错原文、换另一个工具）。三次都没有，才可以说找不到，"
        "并说明你搜了什么。\n\n"
        "**两级检索：**recall_memories/search_episodes 命中 episode 时会给出 when/source 锚点——"
        "需要当时的原话或细节，就拿锚点里的对象名/日期当关键词再调 search_recent_conversation。"
    )


def build_memory_prompt(base_prompt: str, *, speaker_user_id: Optional[str] = None,
                        participant_user_ids=()) -> str:
    """在基础 prompt 后追加完整记忆上下文（identity + intentions + 回忆纪律）。

    单体宿主用；DSH 插件侧按块分别注入（build_identity_block / build_intentions_block /
    build_recall_discipline），以便各块独立开关与排序。
    """
    parts = [base_prompt]
    identity = build_identity_block(speaker_user_id=speaker_user_id,
                                    participant_user_ids=participant_user_ids)
    if identity:
        parts.append("\n\n" + identity)
    # 前瞻记忆：到期/临近的未竟事宜需主动注入（限量），供 agent 主动提醒
    intentions_block = build_intentions_block()
    if intentions_block:
        parts.append(intentions_block)
    parts.append("\n\n" + build_recall_discipline())
    return "".join(parts)


# ---------------------------------------------------------------------------
# Stability / Score 操作
# ---------------------------------------------------------------------------

def record_usage(entry: MemoryEntry) -> None:
    """记录一次使用：stability 和 score 增长，但同一天多次使用递减收益。

    类脑原理：间隔重复（spaced repetition）比集中重复有效。
    同一天多次使用的边际收益递减。
    """
    today_str = str(date.today())
    if entry.last_used == today_str:
        # 同天重复使用：stability 不增，score 仅+0.05（递减收益）
        entry.score = min(entry.score + 0.05, 1.0)
    else:
        # 新的一天使用：正常增长（间隔重复效果好）
        entry.stability += 1
        entry.score = min(entry.score + 0.2, 1.0)
    entry.last_used = today_str
    entry.save()


def apply_decay_all() -> None:
    """对所有记忆执行基于 stability 的差异化衰减（时间差计算）。

    语义记忆（knowledge/agent_learned_skills）用慢衰减曲线（base 0.1，越用越牢）；
    情景记忆（episodes，type=episode）用快衰减曲线（base 0.25，不再被提及则较快淡忘）。
    """
    today = date.today()
    _decay_entries(load_all_entries(), SEMANTIC_DECAY_BASE, today)
    _decay_entries(load_all_episodes(), EPISODE_DECAY_BASE, today)


def _decay_entries(entries: list["MemoryEntry"], base: float, today: date) -> None:
    """对给定条目按指定基础日衰减率做时间差衰减并保存。

    类脑原理：闪光灯记忆效应——高重要度(salience)的记忆衰减更慢。
    salience=high 时衰减率减半，模拟情感加固效应。
    """
    for entry in entries:
        last_decay_date = today
        if entry.last_decay:
            try:
                last_decay_date = date.fromisoformat(entry.last_decay)
            except ValueError:
                last_decay_date = today
        days_elapsed = (today - last_decay_date).days
        if days_elapsed <= 0:
            continue
        # 闪光灯记忆：高 salience 的条目衰减减慢
        effective_base = base
        if entry.entry_type == "episode":
            salience = _episode_field(entry, "salience")
            if salience == "high":
                effective_base = base * 0.4  # 衰减大幅减慢
            elif salience in ("mid", "medium"):
                effective_base = base * 0.7  # 衰减适度减慢
        daily_decay = 1 - (effective_base / (1 + entry.stability * 0.5))
        decay_factor = daily_decay ** days_elapsed
        entry.score = entry.score * decay_factor
        entry.last_decay = str(today)
        entry.save()


def run_retirement() -> list[str]:
    """淘汰 score < 0.2 的条目（语义 + 情景），返回被淘汰的名称列表。"""
    retired = []
    entries = load_all_entries()

    # 语义条目 score < 0.2 淘汰
    for entry in entries:
        if entry.score < 0.2:
            _archive_entry(entry)
            retired.append(entry.name)

    # 语义条目总数 > 300 强制淘汰最低分（同等低分优先淘汰更久未活跃的）
    remaining = [e for e in load_all_entries() if e.name not in retired]
    if len(remaining) > 300:
        remaining.sort(key=lambda e: (e.score, getattr(e, 'last_active', '') or ''))
        for entry in remaining[: len(remaining) - 300]:
            _archive_entry(entry)
            retired.append(entry.name)

    # 情景条目 score < 0.2 淘汰（归档到 episodes/archived）
    for ep in load_all_episodes():
        if ep.score < 0.2:
            _archive_episode(ep)
            retired.append(ep.name)

    return retired


# 前瞻记忆自动过期天数（超过此天数未活动的 intention 自动归档）
INTENTION_STALE_DAYS = 30


def archive_stale_intentions(stale_days: int = INTENTION_STALE_DAYS) -> list[str]:
    """自动归档超过 stale_days 天未活动的前瞻记忆（intentions）。

    判定逻辑：以 last_used 或 created 中较新者为「最后活跃日」，
    若距今超过 stale_days 天则归档，reason 标记为 "auto_expired"。

    Returns:
        被归档的 intention id 列表。
    """
    today = date.today()
    archived_ids: list[str] = []
    for entry in load_all_intentions():
        # 取最后活跃日：last_used 和 created 中较新者
        last_active = None
        for dt_str in (entry.last_used, entry.created):
            if dt_str:
                try:
                    d = date.fromisoformat(dt_str[:10])
                    if last_active is None or d > last_active:
                        last_active = d
                except ValueError:
                    continue
        if last_active is None:
            # 无法判断日期，跳过
            continue
        if (today - last_active).days > stale_days:
            _archive_intention(entry, "auto_expired")
            archived_ids.append(entry.name)
    return archived_ids


def _archive_entry(entry: MemoryEntry) -> None:
    """将条目移入 archived/。"""
    archived_dir = _get_memory_dir() / "archived"
    dest = archived_dir / entry.path.parent.name
    if dest.exists():
        shutil.rmtree(dest)
    archived_dir.mkdir(parents=True, exist_ok=True)
    if _platform.safe_move:
        _platform.safe_move(str(entry.path.parent), str(dest))
    else:
        shutil.move(str(entry.path.parent), str(dest))


# ---------------------------------------------------------------------------
# 情景记忆（Episodic Memory）
# ---------------------------------------------------------------------------

def _episodes_dir() -> Path:
    return _get_memory_dir() / "episodes"


def load_all_episodes() -> list[MemoryEntry]:
    """加载 episodes/ 下所有情景记忆条目（跳过 archived/）。"""
    d = _episodes_dir()
    out: list[MemoryEntry] = []
    if not d.exists():
        return out
    for md in sorted(d.rglob("SKILL.md")):
        try:
            if "archived" in md.relative_to(d).parts:
                continue
        except ValueError:
            continue
        try:
            out.append(MemoryEntry.load(md))
        except Exception:
            continue
    return out


def _format_episode_body(when: str, actors: str, outcome: str, objects: str,
                         tools: str, tags: str, salience: str, source: str) -> str:
    """把情景记忆的结构化字段序列化进 body（MemoryEntry.save 只写固定 meta 键）。"""
    lines = [
        f"- when: {when}",
        f"- actors: {actors}",
        f"- outcome: {outcome}",
    ]
    if objects:
        lines.append(f"- objects: {objects}")
    if tools:
        lines.append(f"- tools: {tools}")
    if tags:
        lines.append(f"- tags: {tags}")
    lines.append(f"- salience: {salience}")
    if source:
        lines.append(f"- source: {source}")
    return "\n".join(lines)


def record_episode(what: str, when: str = "", actors: str = "user, agent",
                   outcome: str = "unknown", objects: str = "", tools: str = "",
                   tags: str = "", salience: str = "low", source: str = "") -> MemoryEntry:
    """记录一条情景记忆（事件流水）。初始 score 由 salience 决定，快衰减。"""
    salience_key = (salience or "low").strip().lower()
    score = SALIENCE_INITIAL_SCORE.get(salience_key, 0.4)
    today = date.today().isoformat()
    when = when or datetime.now().isoformat(timespec="seconds")

    base_name = _platform.sanitize_name(source.strip()) if source.strip() else \
        f"ep-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d = _episodes_dir()
    name = base_name
    seq = 1
    while (d / name / "SKILL.md").exists():
        seq += 1
        name = f"{base_name}-{seq}"

    body = _format_episode_body(when, actors, outcome, objects, tools, tags, salience_key, source)
    entry = MemoryEntry(
        name=name,
        description=what.strip(),
        entry_type="episode",
        stability=0,
        score=score,
        created=today,
        last_used=today,
        last_decay=today,
        body=body,
        path=d / name / "SKILL.md",
    )
    entry.save()
    return entry


def _archive_episode(entry: MemoryEntry) -> None:
    """将情景记忆移入 episodes/archived/。"""
    archived_dir = _episodes_dir() / "archived"
    dest = archived_dir / entry.path.parent.name
    if dest.exists():
        shutil.rmtree(dest)
    archived_dir.mkdir(parents=True, exist_ok=True)
    if _platform.safe_move:
        _platform.safe_move(str(entry.path.parent), str(dest))
    else:
        shutil.move(str(entry.path.parent), str(dest))


def _episode_field(entry: MemoryEntry, key: str) -> str:
    """从 episode body 中取出某个结构化字段值（如 when/tags）。"""
    prefix = f"- {key}:"
    for line in entry.body.splitlines():
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def search_episodes(query: str = "", since: str = "", until: str = "",
                    tags: str = "", limit: int = 20) -> str:
    """检索情景记忆（事件流水）。回忆"何时做过什么"时使用。

    Args:
        query: 关键词，匹配事件摘要与详情（为空则不按关键词过滤）。
        since: 起始日期 YYYY-MM-DD（含），为空不限。
        until: 截止日期 YYYY-MM-DD（含），为空不限。
        tags: 标签关键词，匹配事件标签（为空不限）。
        limit: 返回条数上限（默认 20）。

    Returns:
        匹配的情景记忆列表文本（按时间倒序，新→旧）。
    """
    def _to_date(s: str):
        s = (s or "").strip()[:10]
        try:
            return date.fromisoformat(s)
        except Exception:
            return None

    since_d, until_d = _to_date(since), _to_date(until)
    q = (query or "").strip().lower()
    tag_q = (tags or "").strip().lower()
    try:
        limit = max(1, int(limit or 20))
    except (TypeError, ValueError):
        limit = 20

    # 有 query 时相关性交给 semantic_search（向量+关键词混合；索引不可用自动降级关键词）。
    # 多取一些候选再做日期/标签过滤，避免过滤后不足 limit。
    sem_rel: dict = {}
    if q:
        try:
            for r in semantic_search(q, types=("episode",), limit=min(20, max(limit * 3, 10))):
                sem_rel[r["path"]] = r["score"]
        except Exception:
            logger.debug("search_episodes: semantic_search 失败，降级关键词", exc_info=True)
            sem_rel = {}

    results = []
    for e in load_all_episodes():
        relevance = 0.0
        if q:
            if sem_rel:
                relevance = sem_rel.get(str(e.path.resolve()), 0.0)
            else:
                hay = (e.description + " " + e.body).lower()
                # 分词匹配：复用 _tokenize_query，任一 token 命中即匹配
                tokens = _tokenize_query(q)
                if q in hay:
                    relevance = 100  # 完整匹配最高分
                elif tokens:
                    relevance = sum(1 for t in tokens if len(t) >= 2 and t in hay)
            if relevance <= 0:
                continue
        if tag_q and tag_q not in _episode_field(e, "tags").lower():
            continue
        ed = _to_date(e.created)
        if since_d and ed and ed < since_d:
            continue
        if until_d and ed and ed > until_d:
            continue
        results.append((e, relevance))

    # 按相关性降序（有 query 时），再按时间倒序
    if q:
        results.sort(key=lambda x: (-x[1], x[0].created), reverse=False)
        results.sort(key=lambda x: x[1], reverse=True)
    else:
        results.sort(key=lambda x: (x[0].created, x[0].name), reverse=True)
    results = results[:limit]
    if not results:
        return ("没有匹配的情景记忆。可换一种措辞（补上对象/场景/报错原文）重试，"
                "或用 recall_memories 跨 knowledge/skill/episode 语义回忆。")

    lines = [f"共找到 {len(results)} 条情景记忆（按{'相关性' if q else '时间倒序'}排序）："]
    for e, rel in results:
        when = _episode_field(e, "when") or e.created
        outcome = _episode_field(e, "outcome")
        etags = _episode_field(e, "tags")
        source = _episode_field(e, "source")
        meta = " | ".join(x for x in [f"when={when}", f"outcome={outcome}" if outcome else "",
                                      f"tags={etags}" if etags else "",
                                      f"source={source}" if source else "",
                                      f"score={e.score:.2f}"] if x)
        lines.append(f"- {e.description}（{meta}）")
        # 记忆再巩固：被成功检索的 episode score 微增（回忆强化记忆）
        if q and rel > 0:
            e.score = min(e.score + 0.03, 1.0)
            e.last_used = str(date.today())
            e.save()
    if q and any(_episode_field(e, "source") for e, _ in results):
        lines.append("（source=会话日志 id；要看当时原话，用 search_recent_conversation 按关键词搜对话记录）")
    return "\n".join(lines)


def search_knowledge(query: str = "", entry_type: str = "", limit: int = 10) -> str:
    """检索知识记忆（沉淀的经验、事实、流程结论，knowledge/ 目录）。
    回答涉及"之前/上次/具体配置/服务器/历史结论/团队人物"等问题前先查这里。
    注意：技能（skill）用法请看系统提示中的 Agent Skills 列表；回忆"何时做过什么"用 search_episodes。

    Args:
        query: 关键词，匹配记忆名称、描述与正文（空则按 score 降序返回高分条目）。
        entry_type: 过滤类型（如 experience），空则不限。
        limit: 返回条数上限（默认 10）。

    Returns:
        匹配的知识记忆（含正文摘要），按相关性/score 排序。
    """
    q = (query or "").strip().lower()
    et = (entry_type or "").strip().lower()
    try:
        limit = max(1, min(int(limit or 10), 20))
    except (TypeError, ValueError):
        limit = 10

    entries = [e for e in load_all_entries()
               if "knowledge" in str(e.path.parent) or "knowledge" in str(e.path)]
    # 只取 knowledge/ 目录条目；技能记忆（agent_learned_skills）已常驻注入，不重复
    entries = [e for e in entries if "agent_learned_skills" not in str(e.path)]
    if et:
        entries = [e for e in entries if e.entry_type.lower() == et]

    results = []
    if q:
        tokens = _tokenize_query(q)
        for e in entries:
            name_l = e.name.lower()
            desc_l = e.description.lower()
            body_l = e.body.lower()
            rel = 0
            for t in tokens:
                if len(t) < 2:
                    continue
                if t in name_l:
                    rel += 3
                if t in desc_l:
                    rel += 2
                if t in body_l:
                    rel += 1
            if rel > 0:
                results.append((e, rel))
        results.sort(key=lambda x: (-x[1], -x[0].score))
    else:
        results = [(e, 0) for e in sorted(entries, key=lambda x: -x.score)]

    results = results[:limit]
    if not results:
        return "没有匹配的知识记忆。可尝试其他关键词，或用 search_episodes 检索情景记忆。"

    today = str(date.today())
    lines = [f"共找到 {len(results)} 条知识记忆（{'按相关性' if q else '按 score'}排序）："]
    for e, rel in results:
        # 正文摘要：单条截断 ~2000 字符，完整内容可 view_text_file 读取
        body = e.body.strip()
        if len(body) > 2000:
            body = body[:2000] + f"\n…（已截断，完整内容用 view_text_file 读 {e.path}）"
        lines.append(f"\n### {e.name}（type={e.entry_type}, score={e.score:.2f}）\n{e.description}\n\n{body}")
        # 记忆再巩固：被检索命中的 knowledge 条目涨分（复用 record_usage 的间隔重复逻辑）
        if q:
            try:
                record_usage(e)
            except Exception:
                pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 语义检索（向量 + 关键词混合）
#
# 向量层是派生物（memory_indexer），缺失/异常时自动降级为纯关键词，结果与旧行为一致。
# 归一化常数针对 bge-small-zh-v1.5 实测：无关 query 的 cosine ≈0.40（噪声底），
# 真命中 0.60~0.66，0.70 视为饱和。换 embedding provider 必须重校。
# ---------------------------------------------------------------------------

_COS_FLOOR = float(os.environ.get("SOMNI_COS_FLOOR", "0.40"))
_COS_SPAN = float(os.environ.get("SOMNI_COS_SPAN", "0.30"))
# 混合权重：向量 0.6 / 关键词 0.4 / 字面全命中额外 +0.2
_HYBRID_W_VEC, _HYBRID_W_KW, _HYBRID_LITERAL_BOOST = 0.6, 0.4, 0.2
# S5：1-hop related 邻居并入候选时的分数折减
_LINK_SPREAD_DECAY = 0.6


def _vec_index():
    """取向量索引单例；provider=off / 依赖缺失 / 初始化失败 → None（降级关键词）。"""
    try:
        from . import memory_indexer as _mi
    except ImportError:
        return None
    try:
        return _mi.get_index()
    except Exception:
        return None


def _entry_kind(e: MemoryEntry) -> str:
    """episode / skill / knowledge —— 与 memory_indexer 的分类保持一致。"""
    if e.entry_type == "episode":
        return "episode"
    parts = e.path.parts if e.path else ()
    if "episodes" in parts:
        return "episode"
    if "agent_learned_skills" in parts:
        return "skill"
    return "knowledge"


def _normalize_types(types) -> tuple:
    if not types:
        return ()
    if isinstance(types, str):
        types = types.replace("，", ",").split(",")
    return tuple(t.strip().lower() for t in types if t and t.strip())


def _primary_tokens(q: str) -> list:
    """query 的"整词"切分（按分隔符 + 中英文边界），不含 _tokenize_query 补的 2-gram 碎片。"""
    out = []
    for part in re.split(r'[\s,/，、。？！：；\-_]+', q):
        for seg in re.findall(r'[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+', part):
            seg = seg.strip()
            if seg and seg not in out:
                out.append(seg)
    return out


def _keyword_relevance(e: MemoryEntry, tokens: list, q: str, primary=None) -> tuple:
    """(加权关键词分, 是否字面全命中, 是否强证据)。权重沿用 search_knowledge：name 3 / desc 2 / body 1。

    强证据 = 字面全命中，或命中了一个"整词"（非中文标识符/英文/数字 ≥3 字符，或中文整段 ≥2 字）。
    仅靠 2-gram 碎片（如 "今天"/"怎么"）命中不算强证据——混合模式下用于过滤关键词噪声。
    """
    name_l, desc_l, body_l = e.name.lower(), e.description.lower(), e.body.lower()
    hay = name_l + "\n" + desc_l + "\n" + body_l
    rel = 0
    for t in tokens:
        if len(t) < 2:
            continue
        if t in name_l:
            rel += 3
        if t in desc_l:
            rel += 2
        if t in body_l:
            rel += 1
    literal = bool(q) and q in hay
    strong = literal
    if not strong and rel > 0:
        for t in (primary or ()):
            if t not in hay:
                continue
            is_cjk = bool(re.fullmatch(r'[\u4e00-\u9fff]+', t))
            if (is_cjk and len(t) >= 2) or (not is_cjk and len(t) >= 3):
                strong = True
                break
    return rel, literal, strong


def _episode_anchor(e: MemoryEntry) -> dict:
    """情景记忆的时间锚点：date（YYYY-MM-DD）+ source（session-log id），供二级检索取原话。"""
    when = _episode_field(e, "when") or e.created or ""
    return {"date": when[:10], "source": _episode_field(e, "source")}


def semantic_search(query: str, types=(), limit: int = 5) -> list:
    """混合检索 knowledge / skill / episode，返回按 score 降序的 dict 列表。

    每项：{path, name, entry_type, score, cos, kw, description, body_snippet,
           applies_when, not_applies_when, anchor: {date, source}, entry}
    - score = 0.6*clip((cos-FLOOR)/SPAN) + 0.4*kw/kw_max (+0.2 字面全命中)
    - 向量层不可用 → score = kw/kw_max (+boost)，排序与纯关键词一致
    - SOMNI_LINK_SPREAD=1 且索引可用时：命中项的 related 邻居以 score*0.6 并入候选
    """
    q = (query or "").strip()
    q_l = q.lower()
    if not q:
        return []
    try:
        limit = max(1, min(int(limit or 5), 20))
    except (TypeError, ValueError):
        limit = 5
    tset = _normalize_types(types)

    by_path: dict = {}
    # 只加载会用到的来源：钩子每轮两次只查 knowledge/skill，跳过 200+ 条 episode 的磁盘扫描省 ~100ms
    pool = []
    if not tset or set(tset) & {"knowledge", "skill"}:
        pool += load_all_entries()
    if not tset or "episode" in tset:
        pool += load_all_episodes()
    for e in pool:
        if tset and _entry_kind(e) not in tset:
            continue
        by_path[str(e.path.resolve())] = e
    if not by_path:
        return []

    cos_by_path: dict = {}
    idx = _vec_index()
    if idx is not None:
        try:
            for r in idx.search(q, k=max(limit * 3, 15), types=tset):
                cos_by_path[r["path"]] = float(r["cos"])
        except Exception:
            logger.debug("semantic_search: 向量检索失败，降级关键词", exc_info=True)
            cos_by_path = {}
    hybrid = bool(cos_by_path)

    # 关键词层：混合模式只认"整词"（标识符/英文/数字/空格分开的中文词），语义由向量层负责，
    # 2-gram 碎片（"计算"/"时间"）只会把无关条目抬高；降级模式保留碎片以换召回（等价旧行为）
    primary = _primary_tokens(q_l)
    kw_tokens = primary if hybrid else _tokenize_query(q_l)
    kw_raw: dict = {}
    literal_hit: dict = {}
    strong_hit: dict = {}
    for p, e in by_path.items():
        rel, lit, strong = _keyword_relevance(e, kw_tokens, q_l, primary)
        if rel > 0:
            kw_raw[p] = rel
            literal_hit[p] = lit
            strong_hit[p] = strong
    kw_max = max(kw_raw.values()) if kw_raw else 0

    # 关键词归一化：降级模式用相对值（top 命中 = 1，排序等价旧关键词检索）；
    # 混合模式用 max(kw_max, 3*整词数) 作分母，避免"只碰到一个词"被放大到满分
    kw_cap = kw_max if not hybrid else max(kw_max, 3 * max(1, len(primary)))

    def _score(p: str) -> float:
        s = 0.0
        cos = cos_by_path.get(p)
        if cos is not None:
            vn = (cos - _COS_FLOOR) / _COS_SPAN if _COS_SPAN > 0 else 0.0
            s += _HYBRID_W_VEC * max(0.0, min(1.0, vn))
        if kw_cap and p in kw_raw:
            kwn = min(1.0, kw_raw[p] / kw_cap)
            s += (_HYBRID_W_KW * kwn) if hybrid else kwn
            if literal_hit.get(p):
                s += _HYBRID_LITERAL_BOOST
        return s

    if hybrid:
        # 混合模式：候选 = 向量分高于噪声底的 ∪ 有强关键词证据的；
        # 向量 top-k 总会返回 k 条，落在噪声底以下且只有碎片命中的不算候选
        candidates = {p for p, c in cos_by_path.items() if p in by_path and c > _COS_FLOOR}
        candidates |= {p for p in kw_raw if strong_hit.get(p)}
    else:
        candidates = set(kw_raw)
    scored: dict = {p: s for p in candidates for s in (_score(p),) if s > 0}

    # S5：1-hop 链接扩散（只对向量索引可用的情况生效，邻居分数折减且不覆盖已有更高分）
    if idx is not None and scored and os.environ.get("SOMNI_LINK_SPREAD", "1") == "1":
        try:
            seeds = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
            for p, s in seeds:
                for nb in idx.neighbors(p):
                    if nb in by_path and nb not in scored:
                        scored[nb] = s * _LINK_SPREAD_DECAY
        except Exception:
            logger.debug("semantic_search: 链接扩散失败（忽略）", exc_info=True)

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], -by_path[kv[0]].score))[:limit]
    out = []
    for p, s in ranked:
        e = by_path[p]
        meta = None
        if idx is not None:
            try:
                meta = idx.get_meta(p)
            except Exception:
                meta = None
        body = e.body.strip()
        out.append({
            "path": p,
            "name": e.name,
            "entry_type": _entry_kind(e),
            "score": round(s, 4),
            "cos": round(cos_by_path[p], 4) if p in cos_by_path else None,
            "kw": kw_raw.get(p, 0),
            "description": e.description,
            "body_snippet": body[:600] + ("…" if len(body) > 600 else ""),
            "applies_when": (meta or {}).get("applies_when") or e.extra.get("applies_when", ""),
            "not_applies_when": (meta or {}).get("not_applies_when") or e.extra.get("not_applies_when", ""),
            "anchor": _episode_anchor(e) if _entry_kind(e) == "episode" else {},
            "entry": e,
        })
    return out


def recall_memories(query: str, types: str = "", limit: int = 5) -> str:
    """按语义回忆记忆（知识 knowledge / 技能 skill / 情景 episode），比关键词更能抓到"意思相近"的条目。

    适用："上次那个网关报错怎么处理的""有没有做过类似的事""这类问题我有经验吗"。
    query 请写成一句描述性的话（先把"那个/上次"等指代换成具体对象），不要只丢裸关键词或代词。
    没命中时换 1~3 种措辞重试；回忆"何时做过什么"且命中 episode 后，可用其 date/source 锚点
    调 search_recent_conversation 取当时原话。

    Args:
        query: 描述性的一句话，例如 "断流时网关返回 503 no_available_workers 的处理经验"。
        types: 限定类型，逗号分隔（knowledge / skill / episode），空则全部。
        limit: 返回条数上限（默认 5，最多 20）。

    Returns:
        按相关度排序的记忆列表（含分数、适用/不适用边界、摘录）；空结果时返回重试引导。
    """
    q = (query or "").strip()
    if not q:
        return "query 为空。请用一句描述性的话说明要回忆的内容（先把指代换成具体对象）。"
    try:
        results = semantic_search(q, types=types, limit=limit)
    except Exception as ex:
        logger.exception("recall_memories 失败")
        return f"记忆检索出错：{ex}"
    if not results:
        return (f"没有找到与「{q}」相关的记忆（top1=0.00）。\n"
                "这不代表没有：换一种措辞再试 1~3 次（换同义词、补上对象/场景/报错原文），"
                "或改用 search_recent_conversation 直接搜对话记录。都没有时再说明搜过什么。")

    top1 = results[0]["score"]
    lines = [f"共 {len(results)} 条相关记忆（top1={top1:.2f}；<0.30 视为弱相关，建议换措辞重试）："]
    for r in results:
        e: MemoryEntry = r["entry"]
        head = f"\n[{r['entry_type']}|score={r['score']:.2f}] {r['name']} — {r['description']}"
        if r["entry_type"] == "episode":
            a = r["anchor"]
            when = _episode_field(e, "when") or e.created
            outcome = _episode_field(e, "outcome")
            head += f"\n  when={when}" + (f" outcome={outcome}" if outcome else "")
            if a.get("source"):
                head += f" source={a['source']}（可用 search_recent_conversation 取原话）"
        lines.append(head)
        if r["applies_when"] or r["not_applies_when"]:
            lines.append("  适用：" + (r["applies_when"] or "—") + "  不适用：" + (r["not_applies_when"] or "—"))
        if r["body_snippet"] and r["entry_type"] != "episode":
            snippet = r["body_snippet"].replace("\n", "\n  ")
            lines.append(f"  摘录：{snippet}\n  （完整内容用 view_text_file 读 {r['path']}）")
        # 记忆再巩固：被回忆命中即强化（间隔重复；episode 沿用 search_episodes 的微增）
        try:
            if r["entry_type"] == "episode":
                e.score = min(e.score + 0.03, 1.0)
                e.last_used = str(date.today())
                e.save()
            else:
                record_usage(e)
        except Exception:
            pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 前瞻记忆（Prospective Memory）— 未竟事宜 / 将来要做的事
#
# 脑科学对齐：
# - 线索/到期触发的被动召回是大脑的原生机制（本模块即此）；闹钟是外接假肢（暂不做）。
# - 蔡格尼克效应 / 意图优越效应：未完成意图保持高激活、**不随时间衰减**，
#   直到完成或主动放弃才清除（移入 intentions/archived/）。
# - 临期监控升级：截止日临近（默认 3 天内）才开始在会话中提起，过早不打扰。
# ---------------------------------------------------------------------------

# 临期监控窗口（天）：due 在未来这么多天内才开始主动提起
INTENTION_MONITOR_WINDOW_DAYS = 3
# 各类注入上限，避免撑爆 prompt
_INTENTION_INJECT_CAPS = {"due": 10, "upcoming": 5, "someday": 5}


def _intentions_dir() -> Path:
    return _get_memory_dir() / "intentions"


def load_all_intentions() -> list[MemoryEntry]:
    """加载 intentions/ 下所有未竟事宜（跳过 archived/，即未完成的）。"""
    d = _intentions_dir()
    out: list[MemoryEntry] = []
    if not d.exists():
        return out
    for md in sorted(d.rglob("INTENT.md")):
        try:
            if "archived" in md.relative_to(d).parts:
                continue
        except ValueError:
            continue
        try:
            out.append(MemoryEntry.load(md))
        except Exception:
            continue
    return out


def _intention_field(entry: MemoryEntry, key: str) -> str:
    prefix = f"- {key}:"
    for line in entry.body.splitlines():
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def add_intention(what: str, due: str = "", cue: str = "", priority: str = "mid",
                  source: str = "") -> str:
    """记录一条前瞻记忆（未竟事宜 / 将来要做的事）。不随时间遗忘，完成或取消才清除。

    Args:
        what: 要做/要提醒的事（一句话）。
        due: 截止/计划日期 YYYY-MM-DD（可空，表示"将来某时"无固定日期）。
        cue: 触发线索关键词，逗号分隔（如"登录,bug"），用于相关话题出现时主动提起。
        priority: 优先级 low/mid/high。
        source: 来源 session-log / episode id（可回溯）。

    Returns:
        记录结果。
    """
    what = (what or "").strip()
    if not what:
        return "错误：what 不能为空。"
    pr = (priority or "mid").strip().lower()
    pr = "mid" if pr in ("medium", "") else pr
    if pr not in ("low", "mid", "high"):
        pr = "mid"
    due = (due or "").strip()
    if due:
        try:
            date.fromisoformat(due[:10])
        except ValueError:
            return f"错误：due 日期格式应为 YYYY-MM-DD，收到 '{due}'。"

    today = date.today().isoformat()
    d = _intentions_dir()
    base = f"pi-{date.today().strftime('%Y%m%d')}"
    seq = 1
    name = f"{base}-{seq:03d}"
    while (d / name / "INTENT.md").exists():
        seq += 1
        name = f"{base}-{seq:03d}"

    body_lines = []
    if due:
        body_lines.append(f"- due: {due[:10]}")
    if cue:
        body_lines.append(f"- cue: {cue}")
    body_lines.append(f"- priority: {pr}")
    body_lines.append(f"- created: {today}")
    if source:
        body_lines.append(f"- source: {source}")

    entry = MemoryEntry(
        name=name, description=what, entry_type="intention",
        stability=0, score=1.0, created=today, last_used="", last_decay=today,
        body="\n".join(body_lines), path=d / name / "INTENT.md",
    )
    entry.save()
    due_str = f"，截止 {due[:10]}" if due else "（无固定日期）"
    return f"已记下未竟事宜：[{name}] {what}{due_str}（优先级 {pr}）"


def _archive_intention(entry: MemoryEntry, reason: str) -> None:
    archived_dir = _intentions_dir() / "archived"
    dest = archived_dir / entry.path.parent.name
    if dest.exists():
        shutil.rmtree(dest)
    archived_dir.mkdir(parents=True, exist_ok=True)
    # 在 body 末尾标注结束原因，便于回溯（归档后不再参与召回）
    try:
        entry.body = (entry.body or "") + f"\n- closed: {date.today().isoformat()} ({reason})"
        entry.save()
    except Exception:
        pass
    if _platform.safe_move:
        _platform.safe_move(str(entry.path.parent), str(dest))
    else:
        shutil.move(str(entry.path.parent), str(dest))


def complete_intention(intention_id: str) -> str:
    """将一条未竟事宜标记为已完成并清除（完成即不再提醒，蔡格尼克效应）。

    Args:
        intention_id: 未竟事宜 id（形如 pi-20260607-001，可用 list_intentions 查看）。

    Returns:
        处理结果。
    """
    fp = _intentions_dir() / intention_id / "INTENT.md"
    if not fp.exists():
        return f"错误：未找到未竟事宜 '{intention_id}'（可能已完成/取消）。"
    e = MemoryEntry.load(fp)
    _archive_intention(e, "completed")
    return f"已完成并清除：[{intention_id}] {e.description}"


def cancel_intention(intention_id: str) -> str:
    """取消一条未竟事宜（用户放弃，不再提醒）。

    Args:
        intention_id: 未竟事宜 id（形如 pi-20260607-001）。

    Returns:
        处理结果。
    """
    fp = _intentions_dir() / intention_id / "INTENT.md"
    if not fp.exists():
        return f"错误：未找到未竟事宜 '{intention_id}'。"
    e = MemoryEntry.load(fp)
    _archive_intention(e, "cancelled")
    return f"已取消：[{intention_id}] {e.description}"


def list_intentions() -> str:
    """列出所有未完成的待办/未竟事宜（前瞻记忆）。

    Returns:
        未竟事宜列表（按截止日期排序）。
    """
    items = load_all_intentions()
    if not items:
        return "当前没有未竟事宜。"
    items.sort(key=lambda e: (_intention_field(e, "due") or "9999-99-99", e.name))
    lines = [f"未竟事宜（共 {len(items)} 条）："]
    for e in items:
        due = _intention_field(e, "due")
        pr = _intention_field(e, "priority") or "mid"
        due_part = f"截止 {due}" if due else "无固定日期"
        lines.append(f"- [{e.name}] {e.description}（{due_part}，{pr}）")
    return "\n".join(lines)


def get_cue_triggered_intentions(message: str) -> list[MemoryEntry]:
    """根据当前消息内容，返回被线索触发的未竟事宜（按优先级排序）。

    类脑原理：前瞻记忆的线索触发——看到相关话题自动想起待办。
    例如用户提到"老王"，触发 cue 含"老王"的 intention。
    """
    if not message:
        return []
    msg_lower = message.lower()
    triggered = []
    _priority_order = {"high": 0, "mid": 1, "low": 2}
    for e in load_all_intentions():
        cue_str = _intention_field(e, "cue")
        if not cue_str:
            continue
        cues = [c.strip().lower() for c in cue_str.split(",") if c.strip()]
        if any(c in msg_lower for c in cues):
            triggered.append(e)
    triggered.sort(key=lambda e: _priority_order.get(_intention_field(e, "priority"), 1))
    return triggered


def build_intentions_block(today: "date | None" = None, current_message: str = "") -> str:
    """构建注入 prompt 的「待办与提醒」块（前瞻记忆的被动召回）。

    分三类：到期/逾期（主动提醒）、临近（监控窗口内）、无固定日期（someday）。
    截止日在监控窗口之外的不注入（过早不打扰，模拟大脑临期才监控升级）。
    """
    today = today or date.today()
    items = load_all_intentions()
    if not items:
        return ""

    due_now, upcoming, someday = [], [], []
    for e in items:
        d = _intention_field(e, "due")
        dd = None
        if d:
            try:
                dd = date.fromisoformat(d[:10])
            except ValueError:
                dd = None
        if dd is None:
            someday.append((e, None))
        elif dd <= today:
            due_now.append((e, dd))
        elif (dd - today).days <= INTENTION_MONITOR_WINDOW_DAYS:
            upcoming.append((e, dd))
        # else: 截止日尚远 → 暂不注入

    if not (due_now or upcoming or someday):
        # 即使无时间触发，也检查线索触发
        if not current_message:
            return ""
        cue_triggered = get_cue_triggered_intentions(current_message)
        if not cue_triggered:
            return ""
        lines = [
            "\n\n## 📌 待办与提醒（前瞻记忆）",
            "以下事项因当前话题线索被触发，请在合适时机自然地提醒用户：",
        ]
        for e in cue_triggered[:3]:
            lines.append(f"- 💡 线索触发 [{e.name}] {e.description}")
        return "\n".join(lines)

    due_now.sort(key=lambda x: x[1])
    upcoming.sort(key=lambda x: x[1])
    lines = [
        "\n\n## 📌 待办与提醒（前瞻记忆）",
        "以下是用户记下的未竟事宜。**到期/逾期**项请在本轮对话合适时机主动提醒用户；"
        "用户确认完成后调用 `complete_intention` 清除；同一条不要反复唠叨。",
    ]
    for e, dd in due_now[: _INTENTION_INJECT_CAPS["due"]]:
        lines.append(f"- ⏰ 到期 [{e.name}] {e.description}（截止 {dd}）")
    for e, dd in upcoming[: _INTENTION_INJECT_CAPS["upcoming"]]:
        lines.append(f"- 📅 临近 [{e.name}] {e.description}（截止 {dd}）")
    # someday 项：匹配当前话题线索的优先显示并标注
    cue_names = set()
    if current_message:
        cue_names = {e.name for e in get_cue_triggered_intentions(current_message)}
    for e, _ in someday[: _INTENTION_INJECT_CAPS["someday"]]:
        if e.name in cue_names:
            lines.append(f"- 💡 线索触发 [{e.name}] {e.description}")
        else:
            lines.append(f"- • 待办 [{e.name}] {e.description}")

    # 线索触发：当前话题匹配到的额外 intentions（已有 due 但不在时间窗口内的）
    if current_message:
        listed_names = {e.name for e, _ in due_now + upcoming + someday}
        cue_triggered = [e for e in get_cue_triggered_intentions(current_message)
                         if e.name not in listed_names]
        for e in cue_triggered[:3]:
            lines.append(f"- 💡 线索触发 [{e.name}] {e.description}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 多用户画像（Multi-user Profiles）— memory/users/<dir>/about_user.md
#
# 团队信任域：长期记忆/画像全局共享；按 user_id 索引。单用户场景用 "default"。
# ---------------------------------------------------------------------------

DEFAULT_USER_ID = "default"


def _users_dir() -> Path:
    return _get_memory_dir() / "users"


def _user_dir(user_id: str) -> Path:
    return _users_dir() / _platform.sanitize_name(user_id or DEFAULT_USER_ID)


def _user_profile_path(user_id: str) -> Path:
    return _user_dir(user_id) / "about_user.md"


def _legacy_about_user_path() -> Path:
    return _get_memory_dir() / "about_user.md"


def get_user_profile(user_id: str = DEFAULT_USER_ID) -> Optional[dict]:
    """读取某用户画像，返回 {user_id, meta, body}；不存在返回 None。

    default 用户兼容旧的 memory/about_user.md（未迁移时也能读到）。
    """
    fp = _user_profile_path(user_id)
    if not fp.exists():
        if user_id == DEFAULT_USER_ID and _legacy_about_user_path().exists():
            meta, body = _parse_frontmatter(_legacy_about_user_path().read_text(encoding="utf-8"))
            return {"user_id": DEFAULT_USER_ID, "meta": meta, "body": body}
        return None
    meta, body = _parse_frontmatter(fp.read_text(encoding="utf-8"))
    return {"user_id": user_id, "meta": meta, "body": body}


def load_user_profile_body(user_id: str = DEFAULT_USER_ID) -> str:
    """加载某用户的行为画像正文（注入用）。"""
    p = get_user_profile(user_id)
    return (p["body"].strip() if p else "")


def _write_user_profile(user_id: str, meta: dict, body: str) -> None:
    fp = _user_profile_path(user_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(_dump_frontmatter(meta, body), encoding="utf-8")


def upsert_user_identity(user_id: str, *, name: str = "", open_id: str = "",
                         union_id: str = "", department: str = "",
                         is_owner: Optional[bool] = None) -> None:
    """写入/更新用户的硬身份（由通道在收到消息时调用，幂等）。保留已有行为画像 body。"""
    prof = get_user_profile(user_id)
    meta = dict(prof["meta"]) if prof else {}
    body = prof["body"] if prof else ""
    today = str(date.today())
    meta["user_id"] = user_id
    meta["type"] = "identity"
    if name:
        meta["name"] = name
    meta.setdefault("name", name or user_id)
    if open_id:
        meta["open_id"] = open_id
    if union_id:
        meta["union_id"] = union_id
    if department:
        meta["department"] = department
    if is_owner is not None:
        meta["is_owner"] = "true" if is_owner else "false"
    meta.setdefault("first_seen", today)
    meta["last_seen"] = today
    if name or open_id or department:
        meta["identity_synced"] = today
    _write_user_profile(user_id, meta, body)


def update_about_user(content: str, user_id: str = DEFAULT_USER_ID) -> str:
    """更新某用户的行为画像正文（由 Sleep Agent 做梦时调用）。保留硬身份 frontmatter。"""
    prof = get_user_profile(user_id)
    meta = dict(prof["meta"]) if prof else {}
    meta.setdefault("user_id", user_id)
    meta.setdefault("name", meta.get("name") or user_id)
    meta["type"] = "identity"
    meta["updated"] = str(date.today())
    _write_user_profile(user_id, meta, content)
    return f"已更新用户画像：{meta.get('name', user_id)}（{len(content)} 字符）"


def list_users() -> list[dict]:
    """列出所有用户画像（供 API/前端）。含旧 about_user.md 作为 default 兼容。"""
    out: list[dict] = []
    d = _users_dir()
    seen = set()
    if d.exists():
        for sub in sorted(d.iterdir()):
            fp = sub / "about_user.md"
            if not fp.exists():
                continue
            try:
                meta, _ = _parse_frontmatter(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            uid = str(meta.get("user_id") or sub.name)
            seen.add(uid)
            out.append(_user_summary(uid, meta))
    # 兼容：未迁移的旧 about_user.md 作为 default
    if DEFAULT_USER_ID not in seen and _legacy_about_user_path().exists():
        meta, _ = _parse_frontmatter(_legacy_about_user_path().read_text(encoding="utf-8"))
        out.append(_user_summary(DEFAULT_USER_ID, meta))
    # 主人置顶，其后按 last_seen 倒序
    out.sort(key=lambda u: (not u["is_owner"], u.get("last_seen", "")), reverse=False)
    return out


def _user_summary(user_id: str, meta: dict) -> dict:
    return {
        "user_id": user_id,
        "name": str(meta.get("name") or user_id),
        "department": str(meta.get("department", "")),
        "role": str(meta.get("role", "")),
        "is_owner": str(meta.get("is_owner", "")).lower() == "true",
        "open_id": str(meta.get("open_id", "")),
        "first_seen": str(meta.get("first_seen", "")),
        "last_seen": str(meta.get("last_seen", "")),
    }


def _build_users_block(speaker_user_id: Optional[str], participant_user_ids) -> str:
    """构建注入 prompt 的用户画像段（当前说话人较详细 + 参与者简要）。"""
    parts: list[str] = []
    speaker = get_user_profile(speaker_user_id) if speaker_user_id else None
    if speaker:
        name = speaker["meta"].get("name", speaker_user_id)
        dept = speaker["meta"].get("department", "")
        head = f"## 当前说话人\n姓名：{name}" + (f"（{dept}）" if dept else "")
        if speaker["body"].strip():
            head += "\n" + speaker["body"].strip()
        parts.append(head)
    others = [u for u in (participant_user_ids or []) if u and u != speaker_user_id]
    lines = []
    for uid in others[:10]:
        p = get_user_profile(uid)
        if not p:
            continue
        nm = p["meta"].get("name", uid)
        first_line = next((ln for ln in p["body"].splitlines() if ln.strip()), "")
        lines.append(f"- {nm}：{first_line[:80]}")
    if lines:
        parts.append("## 团队成员（近期参与者）\n" + "\n".join(lines))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def build_user_context(speaker_user_id: Optional[str] = None, participant_user_ids=()) -> str:
    """返回可直接拼到 system prompt 末尾的「当前说话人 + 团队成员」画像块（可能为空）。"""
    return _build_users_block(speaker_user_id, participant_user_ids)


# ---------------------------------------------------------------------------
# SessionLogWriter — 实时写入 session-log
# ---------------------------------------------------------------------------

class SessionLogWriter:
    """管理单次会话的 session-log 写入。"""

    def __init__(self, user_id: str = "default", chat_id: Optional[str] = None,
                 chat_type: Optional[str] = None):
        self._user_id = user_id or "default"
        self._chat_id = chat_id
        self._chat_type = chat_type
        self._file: Optional[Path] = None
        self._seq = 1
        self._msg_count = 0
        self._start_time = datetime.now()
        self._open_new_file()

    def _open_new_file(self) -> None:
        session_log_dir = _get_memory_dir() / "session-log"
        session_log_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        # 找到当天最大序号（包括已归档的文件）
        existing = list(session_log_dir.glob(f"{today}_*.md"))
        archived_dir = session_log_dir / "archived"
        if archived_dir.exists():
            existing.extend(archived_dir.glob(f"{today}_*.md"))
        if existing:
            nums = []
            for f in existing:
                try:
                    nums.append(int(f.stem.split("_")[-1]))
                except ValueError:
                    pass
            self._seq = max(nums, default=0) + 1
        self._file = session_log_dir / f"{today}_{self._seq:03d}.md"
        self._start_time = datetime.now()
        self._msg_count = 0
        # 写 frontmatter
        header = (
            f"---\n"
            f"session_id: {today}_{self._seq:03d}\n"
            f"user: {self._user_id}\n"
        )
        if self._chat_id:
            header += f"chat_id: {self._chat_id}\n"
        if self._chat_type:
            header += f"chat_type: {self._chat_type}\n"
        header += (
            f"start_time: {self._start_time.isoformat(timespec='seconds')}\n"
            f"status: pending\n"
            f"---\n\n"
        )
        self._file.write_text(header, encoding="utf-8")

    def append(self, role: str, text: str, speaker: str = "") -> None:
        """追加一条消息到 session-log。speaker 用于群聊标注说话人。"""
        if not self._file:
            return
        # 检查文件大小，超限则切分
        if self._file.exists() and self._file.stat().st_size > SESSION_LOG_MAX_BYTES:
            self._seq += 1
            self._open_new_file()

        ts = datetime.now().isoformat(timespec="seconds")
        tag = f"{role}:{speaker}" if speaker else role
        # 单条文本封顶，避免巨型正文撑大日志（做梦时会反复重发推高 token）
        if text and len(text) > SESSION_LOG_MSG_MAX_CHARS:
            text = text[:SESSION_LOG_MSG_MAX_CHARS] + f"\n...(截断，原文 {len(text)} 字符)"
        line = f"[{tag}] {ts}\n{text}\n\n"
        with open(self._file, "a", encoding="utf-8") as f:
            f.write(line)
        self._msg_count += 1

    def append_tool(self, call: str, result: str, reason: str = "") -> None:
        """追加一条工具调用摘要记录到 session-log（[tool] 块，含 reason/call/result）。

        设计依据：DESIGN.md §5.4 工具调用记录规范。
        - reason 由 Work Agent 在调用时实时生成（来自 Agent 的推理意图）
        - call 为工具名 + 参数摘要（一行）
        - result 为执行结果摘要（一行，关键信息），不存原始全量输出

        Args:
            call: 工具调用摘要，如 `exec-shell("npm run migrate")`。
            result: 结果摘要（首 200 字符 + 关键状态码/错误信息）。
            reason: 调用原因与理由（为空时也会写入空行，保持格式稳定）。
        """
        if not self._file:
            return
        if self._file.exists() and self._file.stat().st_size > SESSION_LOG_MAX_BYTES:
            self._seq += 1
            self._open_new_file()

        ts = datetime.now().isoformat(timespec="seconds")

        def _summarize(text: str, limit: int = 200) -> str:
            text = (text or "").strip().replace("\n", " ⏎ ")
            if limit and len(text) > limit:
                text = text[:limit] + "...(截断)"
            return text

        block_lines = [f"[tool] {ts}"]
        if reason:
            block_lines.append(f"reason: {_summarize(reason, 0)}")
        block_lines.append(f"call: {_summarize(call, 0)}")
        block_lines.append(f"result: {_summarize(result, 300)}")
        block_lines.append("")
        block_lines.append("")
        with open(self._file, "a", encoding="utf-8") as f:
            f.write("\n".join(block_lines))
        self._msg_count += 1

    def close(self) -> Optional[Path]:
        """关闭当前 session-log，更新 end_time 和 message_count。返回文件路径。"""
        if not self._file or not self._file.exists():
            return None
        content = self._file.read_text(encoding="utf-8")
        end_time = datetime.now().isoformat(timespec="seconds")
        # 插入 end_time 和 message_count
        content = content.replace(
            "status: pending",
            f"end_time: {end_time}\nmessage_count: {self._msg_count}\nstatus: pending",
            1,
        )
        self._file.write_text(content, encoding="utf-8")
        path = self._file
        self._file = None
        return path


def get_pending_logs() -> list[Path]:
    """获取所有未处理的 session-log 文件。

    不变量：直接位于 session-log/ 下（未移入 archived/）且未被显式标记
    status: processed 的日志，都视为待处理 pending——包括没有 frontmatter 的
    旧日志（否则它们会变成"孤儿"：永远不被做梦消费、也不归档，造成记忆泄漏）。
    """
    logs = []
    session_log_dir = _get_memory_dir() / "session-log"
    if not session_log_dir.exists():
        return logs
    for f in sorted(session_log_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(text)
            if meta.get("status") != "processed":
                logs.append(f)
        except Exception:
            continue
    return logs


def mark_log_processed(log_path: Path) -> None:
    """将 session-log 标记为 processed 并移入 archived/。"""
    if not log_path.exists():
        return
    content = log_path.read_text(encoding="utf-8")
    content = content.replace("status: pending", "status: processed", 1)
    if _platform.atomic_write:
        _platform.atomic_write(log_path, content)
    else:
        log_path.write_text(content, encoding="utf-8")
    archived_dir = (_get_memory_dir() / "session-log") / "archived"
    archived_dir.mkdir(parents=True, exist_ok=True)
    if _platform.safe_move:
        _platform.safe_move(str(log_path), str(archived_dir / log_path.name))
    else:
        shutil.move(str(log_path), str(archived_dir / log_path.name))


# ---------------------------------------------------------------------------
# Conversation Recall — contextvars 绑定 + 对话回忆工具
# ---------------------------------------------------------------------------

_current_chat_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    '_current_chat_id', default='')
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    '_current_user_id', default='')


def search_recent_conversation(query: str = "", limit: int = 20) -> str:
    """搜索当前会话的历史对话记录。

    当用户提到"之前/刚才/你说过的"而你没有印象时，调用此工具取回上下文。

    Args:
        query: 搜索关键词（为空时返回最近的对话轮次）。
        limit: 最多返回的对话轮次数（默认20）。

    Returns:
        按时间正序的对话转录文本，仅包含用户和助手的对话。
    """
    chat_id = _current_chat_id.get()
    if not chat_id:
        return "（无法确定当前会话，请稍后重试）"

    char_budget = int(os.environ.get("CONV_SEARCH_CHAR_BUDGET", "12000"))

    # 全局搜索：搜索所有会话的 session-log（不限 chat_id）
    turns = _collect_all_turns(query, limit)
    if not turns:
        return "（没有找到相关的对话记录）"

    # 格式化输出
    lines = []
    total_chars = 0
    if query and turns:
        today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines.append(f"（当前时间：{today_str}。以下为匹配「{query}」的对话记录，按时间正序排列，最后一条是最新的）")
    for ts, role, text in turns:
        role_label = "用户" if "user" in role else "你"
        line = f"[{ts}] {role_label}：{text}"
        if total_chars + len(line) > char_budget:
            lines.insert(0 if not query else 1, "（更早内容已省略）")
            break
        lines.append(line)
        total_chars += len(line)
    return "\n".join(lines)


def _tokenize_query(query: str) -> list[str]:
    """将搜索 query 分词为多个 token，支持中英文混合。

    策略：
    1. 先按标点/空格分割
    2. 对每个片段，在中文与非中文（英文/数字）边界处切分
    3. 对纯中文片段超过 3 字的，额外生成 2-gram 子串提高召回率
    """
    # 第一步：按常见分隔符切分
    parts = re.split(r'[\s,/，、。？！：；\-_]+', query)
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        # 第二步：在中文与非中文边界切分
        # 匹配连续中文 or 连续非中文
        segments = re.findall(r'[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+', part)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            tokens.append(seg)
            # 第三步：纯中文且 > 3字，生成 bigram 提高部分匹配召回
            if len(seg) > 3 and re.fullmatch(r'[\u4e00-\u9fff]+', seg):
                for k in range(len(seg) - 1):
                    tokens.append(seg[k:k+2])
    # 去重保序
    seen = set()
    out = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _collect_all_turns(query: str, limit: int) -> list[tuple[str, str, str]]:
    """从所有 session-log 中收集匹配的对话轮次（全局搜索）。"""
    session_log_dir = _get_memory_dir() / "session-log"
    if not session_log_dir.exists():
        return []

    files: list[Path] = []
    for f in session_log_dir.glob("*.md"):
        files.append(f)
    archived_dir = session_log_dir / "archived"
    if archived_dir.exists():
        for f in archived_dir.glob("*.md"):
            files.append(f)
    files.sort(key=lambda f: f.name)

    all_turns: list[tuple[str, str, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _parse_frontmatter(text)
        turns = _parse_conversation_turns(body)
        all_turns.extend(turns)

    if not all_turns:
        return []

    if not query:
        return all_turns[-limit:]

    query_lower = query.lower()
    tokens = _tokenize_query(query_lower)
    scored_indices: list[tuple[int, int]] = []
    for i, (_, _, text) in enumerate(all_turns):
        text_lower = text.lower()
        if query_lower in text_lower:
            scored_indices.append((i, 100))
            continue
        if tokens:
            hit_count = sum(1 for t in tokens if len(t) >= 2 and t in text_lower)
            if hit_count > 0:
                scored_indices.append((i, hit_count))

    if not scored_indices:
        return []

    scored_indices.sort(key=lambda x: (x[1], x[0]), reverse=True)
    core_limit = max(1, limit * 2 // 3)
    matched_indices = set()
    for idx, _ in scored_indices[:core_limit]:
        for j in range(max(0, idx - 1), min(len(all_turns), idx + 2)):
            matched_indices.add(j)
    result = [all_turns[i] for i in sorted(matched_indices)]
    return result[-limit:]


def _collect_user_turns(user_id: str, exclude_chat_id: str, query: str, limit: int) -> list[tuple[str, str, str]]:
    """跨会话搜索：从所有会话中收集匹配的对话轮次（排除当前 chat）。"""
    session_log_dir = _get_memory_dir() / "session-log"
    if not session_log_dir.exists():
        return []

    files: list[Path] = []
    for f in session_log_dir.glob("*.md"):
        files.append(f)
    archived_dir = session_log_dir / "archived"
    if archived_dir.exists():
        for f in archived_dir.glob("*.md"):
            files.append(f)
    files.sort(key=lambda f: f.name)

    all_turns: list[tuple[str, str, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, body = _parse_frontmatter(text)
        if meta.get("chat_id") == exclude_chat_id:
            continue
        turns = _parse_conversation_turns(body)
        all_turns.extend(turns)

    if not all_turns or not query:
        return []

    query_lower = query.lower()
    tokens = _tokenize_query(query_lower)
    scored_indices: list[tuple[int, int]] = []
    for i, (_, _, text) in enumerate(all_turns):
        text_lower = text.lower()
        if query_lower in text_lower:
            scored_indices.append((i, 100))
            continue
        if tokens:
            hit_count = sum(1 for t in tokens if len(t) >= 2 and t in text_lower)
            if hit_count > 0:
                scored_indices.append((i, hit_count))

    if not scored_indices:
        return []

    scored_indices.sort(key=lambda x: (x[1], x[0]), reverse=True)
    core_limit = max(1, limit * 2 // 3)
    matched_indices = set()
    for idx, _ in scored_indices[:core_limit]:
        for j in range(max(0, idx - 1), min(len(all_turns), idx + 2)):
            matched_indices.add(j)
    result = [all_turns[i] for i in sorted(matched_indices)]
    return result[-limit:]


def _collect_chat_turns(chat_id: str, query: str, limit: int) -> list[tuple[str, str, str]]:
    """从 session-log 中收集指定 chat_id 的对话轮次。

    Returns: [(timestamp, role, text), ...] 按时间正序。
    """
    session_log_dir = _get_memory_dir() / "session-log"
    if not session_log_dir.exists():
        return []

    # 收集所有相关文件（pending + archived），按文件名排序
    files: list[Path] = []
    for f in session_log_dir.glob("*.md"):
        files.append(f)
    archived_dir = session_log_dir / "archived"
    if archived_dir.exists():
        for f in archived_dir.glob("*.md"):
            files.append(f)
    files.sort(key=lambda f: f.name)

    # 过滤属于当前 chat_id 的文件并解析
    all_turns: list[tuple[str, str, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, body = _parse_frontmatter(text)
        if meta.get("chat_id") != chat_id:
            continue
        turns = _parse_conversation_turns(body)
        all_turns.extend(turns)

    if not all_turns:
        return []

    # 根据 query 过滤或截取
    if query:
        query_lower = query.lower()
        tokens = _tokenize_query(query_lower)
        # 计算每轮的匹配得分（命中 token 越多越相关）
        scored_indices: list[tuple[int, int]] = []  # (index, score)
        for i, (_, _, text) in enumerate(all_turns):
            text_lower = text.lower()
            # 完整 query 命中得分最高
            if query_lower in text_lower:
                scored_indices.append((i, 100))
                continue
            # 按 token 命中数评分
            if tokens:
                hit_count = sum(1 for t in tokens if len(t) >= 2 and t in text_lower)
                if hit_count > 0:
                    scored_indices.append((i, hit_count))

        if not scored_indices:
            return []

        # 按 score 降序，同分时按 index 降序（最新优先）
        scored_indices.sort(key=lambda x: (x[1], x[0]), reverse=True)
        # 取 top N 核心匹配轮（limit 的 2/3），然后扩展上下文
        core_limit = max(1, limit * 2 // 3)
        matched_indices = set()
        for idx, _ in scored_indices[:core_limit]:
            for j in range(max(0, idx - 1), min(len(all_turns), idx + 2)):
                matched_indices.add(j)
        result = [all_turns[i] for i in sorted(matched_indices)]
        return result[-limit:]
    else:
        return all_turns[-limit:]


def _parse_conversation_turns(body: str) -> list[tuple[str, str, str]]:
    """解析 session-log body 中的对话轮次，只保留 [user]/[agent] 块。

    Returns: [(timestamp, role, text), ...]
    """
    turns: list[tuple[str, str, str]] = []
    # 匹配所有块头：[role] ts 或 [role:speaker] ts（包括 tool）
    all_blocks = re.compile(r'^\[([^\]]+)\]\s+(\S+)', re.MULTILINE)
    matches = list(all_blocks.finditer(body))
    for i, m in enumerate(matches):
        tag = m.group(1)  # e.g. "user", "agent", "user:张三", "tool"
        role = tag.split(":")[0]
        if role not in ("user", "agent"):
            continue
        ts = m.group(2)
        # 提取到下一个块开头或文件末尾
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if text:
            turns.append((ts, role, text))
    return turns
