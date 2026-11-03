"""缺口信号跨夜台账 + 晋升阈值（泳道B 插件生成的闸门）。

问题：工作 agent 一夜能报出好几个能力缺口，若每条都直接触发插件生成，
      会不断消耗 LLM 且造出一堆用不上的一次性插件。
做法：缺口按 gapKey 归并 —— 同一缺口要"跨够天数"才允许进入生成流程。
      阈值按信号强度分档（安全类 1 天，用户亲口纠偏 2 天，普通 3 天）。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

LEDGER_PATH = Path("/xdata/dsh/home/somni/memory/dream-staging/gap-ledger.json")

# 允许进入插件生成的档位阈值：窗口天数 + 需命中的不同日期数
THRESHOLDS = {
    "safety": {"windowDays": 30, "distinctDays": 1},
    "user-correct": {"windowDays": 14, "distinctDays": 2},
    "default": {"windowDays": 14, "distinctDays": 3},
}

# 安全/不可逆类缺口：出现一次就该做（等三轮黄花菜都凉了）
_SAFETY_PAT = re.compile(
    r"急停|紧急停|安全|碰撞|越界|门闸|闸门|坠落|低电量|自保|求救|保护|"
    r"estop|e-stop|emergency|safety|collision|failsafe",
    re.IGNORECASE,
)

# 已进入/已完成生成流程的状态：同类缺口不再重复触发
_TERMINAL = {"building", "built", "probation", "promoted", "frozen"}
_BUILD_COOLDOWN_DAYS = 7  # 生成失败后冷却，避免同一缺口每夜烧 LLM


# 主题簇：同一类缺口不同措辞 → 归一到一个规范 key（防"同义换说法算新缺口"）
_TOPIC_CANON = [
    # 充电位选择依据：桩故障/该避哪个/该换到哪 —— 全是同一件事
    (re.compile(r"(charger|charge|充电)[a-z-]*(select|choice|target|switch|fault|pile|basis|avoid)|"
                r"(select|switch)[a-z-]*charger|桩[^，。]*?(故障|选择|换桩|该去|该避)"),
     "charger-target-selection"),
]


def _canonical_key(k: str, gap: str = "") -> str:
    """把同主题不同措辞归一到规范 key。命中主题簇则用规范 key，否则原样返回。"""
    for pat, canon in _TOPIC_CANON:
        if pat.search(k) or (gap and pat.search(gap)):
            return canon
    return k


def normalize_key(raw_key: str = "", gap: str = "", expect_plugin_id: str = "") -> str:
    """把缺口归并成稳定 key：LLM 显式给的 > 期望插件 id > 文本兜底哈希。

    ⚠️ 已知弱点（2026-10-22 实测）：LLM 每次对同一件事给的 key 措辞会变
    （charge-pile-connect-fail / charger-selection-basis / charger-fault-target-switch
    其实是同一个缺口），于是同一缺口被当成新缺口反复计数、永远到不了阈值。
    这里加一层主题别名归并：key 命中同一主题簇就归一到一个规范 key。
    """
    for cand in (raw_key, expect_plugin_id):
        k = (cand or "").strip().lower()
        if k:
            k = re.sub(r"[^a-z0-9]+", "-", k).strip("-")
            if k:
                return _canonical_key(k[:60], gap)
    toks = re.findall(r"[a-zA-Z][a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}", gap or "")
    toks = sorted({t.lower() for t in toks})
    if not toks:
        return "auto-empty"
    return "auto-" + hashlib.sha1("|".join(toks).encode("utf-8")).hexdigest()[:10]


def classify(gap: str, signal_type: str) -> str:
    """定档：安全类优先，其次用户纠偏，其余普通。"""
    if _SAFETY_PAT.search(gap or ""):
        return "safety"
    if signal_type == "user-correct":
        return "user-correct"
    return "default"


def _bump(m: dict, day: str) -> dict:
    m = dict(m or {})
    m[day] = int(m.get(day, 0)) + 1
    return m


def _load() -> dict:
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "gaps": {}}


def _save(data: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(LEDGER_PATH)


def record(sig: dict, dream_id: str = "") -> dict:
    """把一条缺口信号并入台账，返回该缺口的台账条目。"""
    data = _load()
    gaps = data.setdefault("gaps", {})
    key = normalize_key(sig.get("gapKey", ""), sig.get("gap", ""), sig.get("expectPluginId", ""))
    today = datetime.now().strftime("%Y-%m-%d")
    tier = classify(sig.get("gap", ""), sig.get("signalType", "intent-no-tool"))

    ent = gaps.get(key) or {
        "key": key,
        "firstSeen": today,
        "dates": [],
        "occurrences": 0,
        "status": "observing",
        "builds": [],
    }
    dates = [d for d in ent.get("dates", []) if d != today]
    dates.append(today)
    ent.update({
        "dates": sorted(set(dates)),
        "occurrences": int(ent.get("occurrences", 0)) + 1,
        "occByDate": _bump(ent.get("occByDate") or {}, today),
        "lastSeen": today,
        "latestGap": (sig.get("gap") or "")[:300],
        "signalType": sig.get("signalType", "intent-no-tool"),
        "expectPluginId": sig.get("expectPluginId", "") or ent.get("expectPluginId", ""),
        "tier": tier,
    })
    if dream_id and dream_id not in ent.get("seenInDreams", [])[-10:]:
        ent.setdefault("seenInDreams", []).append(dream_id)
    gaps[key] = ent
    _save(data)
    return ent


def evaluate(ent: dict) -> tuple[bool, str]:
    """按档位阈值判定该缺口能否进入插件生成。

    返回 (是否放行, 原因)。
    """
    status = ent.get("status", "observing")
    if status in _TERMINAL:
        return False, "status=%s，同类插件已在流程中/已存在" % status

    tier = ent.get("tier") or "default"
    th = THRESHOLDS.get(tier, THRESHOLDS["default"])
    cutoff = (datetime.now() - timedelta(days=th["windowDays"])).strftime("%Y-%m-%d")
    days = [d for d in ent.get("dates", []) if d >= cutoff]

    # 失败冷却：刚建过且失败，先别重复烧
    last = ent.get("lastBuildAt")
    if status == "failed" and last:
        if last >= (datetime.now() - timedelta(days=_BUILD_COOLDOWN_DAYS)).strftime("%Y-%m-%d"):
            return False, "上次生成失败(%s)，冷却 %d 天中" % (last, _BUILD_COOLDOWN_DAYS)

    need = th["distinctDays"]
    if len(days) >= need:
        return True, "%s 档命中 %d 个不同日期(需 %d 日/%d 天窗口)" % (tier, len(days), need, th["windowDays"])

    # 补充通道：同一日内被反复撞到（
    # 用户口径"连续 3 次"的场景）——单日 occurrences >= 3 也放行，
    # 避免白天连着撞三次墙却要等三天。
    occ_in_window = sum(v for d, v in (ent.get("occByDate") or {}).items() if d >= cutoff)
    if occ_in_window >= 3:
        return True, "%s 档窗口内累计 %d 次（高频通道，>=3 次）" % (tier, occ_in_window)
    return False, "%s 档仅 %d 个不同日期/%d 次，需 %d 日或单日 3 次（%d 天窗口）" % (
        tier, len(days), occ_in_window, need, th["windowDays"])


def mark_building(key: str, dream_id: str = "") -> None:
    data = _load()
    ent = data.setdefault("gaps", {}).get(key)
    if not ent:
        return
    ent["status"] = "building"
    ent["lastBuildAt"] = datetime.now().strftime("%Y-%m-%d")
    ent.setdefault("builds", []).append({
        "dreamId": dream_id,
        "at": datetime.now().isoformat(timespec="seconds"),
        "status": "running",
    })
    _save(data)


def mark_build_result(key: str, ok: bool, run_id: str = "", detail: str = "") -> None:
    data = _load()
    ent = data.setdefault("gaps", {}).get(key)
    if not ent:
        return
    ent["status"] = "built" if ok else "failed"
    builds = ent.setdefault("builds", [])
    if builds:
        builds[-1].update({
            "status": "ok" if ok else "fail",
            "runId": run_id,
            "detail": (detail or "")[:200],
        })
    _save(data)


def summary() -> dict:
    """台账概览，供前端面板/排障用。"""
    data = _load()
    gaps = data.get("gaps", {})
    return {
        "total": len(gaps),
        "byStatus": {s: sum(1 for g in gaps.values() if g.get("status") == s)
                     for s in ("observing", "building", "built", "probation", "promoted", "frozen", "failed")},
        "gaps": gaps,
    }
