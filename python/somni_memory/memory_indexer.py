# -*- coding: utf-8 -*-
"""Memory Indexer — 记忆条目的向量索引（派生物，可随时删除重建）。

职责：
- EmbeddingProvider 抽象：本地 ONNX（bge-small-zh-v1.5，默认）/ OpenAI 兼容 HTTP / off
- VectorIndex：SQLite（WAL）存 embedding + 元数据，进程内 numpy 矩阵做 cosine 暴力检索
- sync()：对 knowledge / agent_learned_skills / episodes 做 hash-diff 增量同步。
  hash 只算被嵌入的文本（name/description/body），**绝不含 score/last_decay**，
  否则每轮 decay 重写 frontmatter 都会触发全量 re-embed。
- S5 预留：vec_meta 带 applies_when / not_applies_when 列；vec_links 存 related 边。
  三个字段不进 hash（改边界/改链接不 re-embed）。

数据源复用 memory_manager.load_all_entries() / load_all_episodes()，indexer 不自己
解析 frontmatter；扩展字段走 MemoryEntry.extra。

numpy / onnxruntime / tokenizers 属于可选依赖（pip install somni-memory[embed]）；
未安装时 get_index() 返回 None，全系统退回纯关键词检索。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional, Sequence

try:
    import numpy as np
except ImportError:  # 未安装 embed extra：向量层整体关闭
    np = None  # type: ignore[assignment]

logger = logging.getLogger("memory_indexer")

# bge-zh 系列：检索场景 query 侧加指令前缀，文档侧不加
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
# knowledge/skill 正文只嵌前 N 字符（bge 512 token 截断，多了也是白算）
BODY_EMBED_CHARS = 2000
# 边界/链接字段（S5 预留，S1 就入库）
BOUNDARY_FIELDS = ("applies_when", "not_applies_when")


def _mm():
    """延迟导入 memory_manager（避免循环导入：memory_manager._vec_index 也延迟导入本模块）。"""
    from . import memory_manager as mm
    return mm


# ---------------------------------------------------------------------------
# Embedding providers
# ---------------------------------------------------------------------------

class EmbeddingProvider(ABC):
    """embedding 后端抽象。provider_id 变化 → 索引全量重建。"""

    provider_id: str = "abstract"
    dim: int = 0

    @abstractmethod
    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """文档侧批量 embed。返回已 L2 归一化的向量。"""

    def embed_query(self, text: str) -> list[float]:
        """query 侧 embed（子类可加指令前缀）。"""
        return self.embed_batch([text])[0]


class LocalOnnxEmbedder(EmbeddingProvider):
    """本地 ONNX 推理（onnxruntime + tokenizers），懒加载。

    bge 官方取 CLS 向量后 L2 归一化；SOMNI_EMBED_POOLING=mean 可切均值池化。
    """

    def __init__(self, model_dir: Path, max_length: int = 512, batch_size: int = 16):
        self.model_dir = Path(model_dir)
        self.max_length = max_length
        self.batch_size = batch_size
        self.pooling = os.environ.get("SOMNI_EMBED_POOLING", "cls").lower()
        self.provider_id = f"onnx:{self.model_dir.name}"
        self.dim = self._read_dim()
        self._session = None
        self._tokenizer = None
        self._input_names: list[str] = []
        self._lock = threading.Lock()

    def _read_dim(self) -> int:
        cfg = self.model_dir / "config.json"
        try:
            return int(json.loads(cfg.read_text(encoding="utf-8")).get("hidden_size", 512))
        except Exception:
            return 512

    @staticmethod
    def model_files_present(model_dir: Path) -> bool:
        d = Path(model_dir)
        return (d / "tokenizer.json").exists() and any(
            (d / n).exists() for n in ("model.onnx", "model_quantized.onnx", "model_int8.onnx")
        )

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort  # noqa: WPS433  (懒导入：off 模式不需要该依赖)
            from tokenizers import Tokenizer

            onnx_path = None
            for n in ("model.onnx", "model_quantized.onnx", "model_int8.onnx"):
                if (self.model_dir / n).exists():
                    onnx_path = self.model_dir / n
                    break
            if onnx_path is None:
                raise FileNotFoundError(f"no onnx model under {self.model_dir}")
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(os.environ.get("SOMNI_EMBED_THREADS", "2"))
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"])
            tok = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            tok.enable_truncation(max_length=self.max_length)
            tok.enable_padding(pad_id=0, pad_token="[PAD]")
            self._input_names = [i.name for i in sess.get_inputs()]
            self._tokenizer = tok
            self._session = sess
            logger.info("[vec-index] onnx loaded: %s dim=%d pooling=%s inputs=%s",
                        onnx_path.name, self.dim, self.pooling, self._input_names)

    def _run(self, texts: Sequence[str]) -> np.ndarray:
        enc = self._tokenizer.encode_batch(list(texts))
        ids = np.asarray([e.ids for e in enc], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
        feed = {}
        for name in self._input_names:
            if name == "input_ids":
                feed[name] = ids
            elif name == "attention_mask":
                feed[name] = mask
            elif name == "token_type_ids":
                feed[name] = np.zeros_like(ids)
        out = self._session.run(None, feed)[0]  # (B, L, H) last_hidden_state
        if out.ndim == 2:  # 有些导出直接给 sentence_embedding
            vec = out
        elif self.pooling == "mean":
            m = mask[..., None].astype(np.float32)
            vec = (out * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-6, None)
        else:
            vec = out[:, 0, :]
        vec = vec.astype(np.float32)
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        return vec / np.clip(norm, 1e-12, None)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self._ensure_loaded()
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = [t if t.strip() else " " for t in texts[i:i + self.batch_size]]
            out.extend(self._run(chunk).tolist())
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_batch([BGE_QUERY_PREFIX + text])[0]


class OpenAICompatEmbedder(EmbeddingProvider):
    """POST {base_url}/embeddings（OpenAI 兼容）。失败抛异常，由上层降级。"""

    def __init__(self, base_url: str, api_key: str, model: str, batch_size: int = 25, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch_size = batch_size
        self.timeout = timeout
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0]
        self.provider_id = f"http:{model}@{host}"
        self.dim = int(os.environ.get("SOMNI_EMBED_DIM", "0"))  # 0 = 首次调用时确定

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        import urllib.request

        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = [t if t.strip() else " " for t in texts[i:i + self.batch_size]]
            payload = json.dumps({"model": self.model, "input": chunk}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/embeddings", data=payload, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            vecs = np.asarray([it["embedding"] for it in items], dtype=np.float32)
            vecs = vecs / np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
            out.extend(vecs.tolist())
        if out and not self.dim:
            self.dim = len(out[0])
        return out


_provider: Optional[EmbeddingProvider] = None
_provider_resolved = False
_provider_lock = threading.Lock()


def _default_model_dir() -> Path:
    """默认模型目录：SOMNI_EMBED_MODEL_PATH > <SOMNI_DATA_DIR>/models/bge-small-zh-v1.5。"""
    explicit = os.environ.get("SOMNI_EMBED_MODEL_PATH", "").strip()
    if explicit:
        return Path(explicit)
    data_root = Path(os.environ.get("SOMNI_DATA_DIR", str(Path.home() / ".dsh" / "somni")))
    return data_root / "models" / "bge-small-zh-v1.5"


def get_provider() -> Optional[EmbeddingProvider]:
    """按 SOMNI_EMBED_PROVIDER 选择后端；off / 初始化失败 → None（全系统退回关键词行为）。"""
    global _provider, _provider_resolved
    if _provider_resolved:
        return _provider
    with _provider_lock:
        if _provider_resolved:
            return _provider
        kind = os.environ.get("SOMNI_EMBED_PROVIDER", "local").strip().lower()
        prov: Optional[EmbeddingProvider] = None
        try:
            if kind == "off":
                logger.info("[vec-index] provider=off，向量层关闭")
            elif np is None:
                logger.warning("[vec-index] numpy 未安装（pip install somni-memory[embed]），向量层关闭")
            elif kind == "http":
                base = os.environ.get("SOMNI_EMBED_BASE_URL", "").strip()
                model = os.environ.get("SOMNI_EMBED_MODEL", "").strip()
                if base and model:
                    prov = OpenAICompatEmbedder(base, os.environ.get("SOMNI_EMBED_API_KEY", ""), model)
                else:
                    logger.warning("[vec-index] provider=http 但缺 SOMNI_EMBED_BASE_URL/MODEL，向量层关闭")
            else:
                mdir = _default_model_dir()
                if LocalOnnxEmbedder.model_files_present(mdir):
                    prov = LocalOnnxEmbedder(mdir)
                else:
                    logger.warning("[vec-index] 本地模型缺失（%s），向量层关闭；放入 model.onnx + tokenizer.json 后重启", mdir)
        except Exception:
            logger.exception("[vec-index] provider 初始化失败，向量层关闭")
            prov = None
        _provider = prov
        _provider_resolved = True
        return _provider


def set_provider(provider: Optional[EmbeddingProvider]) -> None:
    """测试/运维注入用：显式指定 provider（None = off），并重置索引单例。"""
    global _provider, _provider_resolved
    with _provider_lock:
        _provider = provider
        _provider_resolved = True
    _reset_index_singleton()


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vec_meta(
  path             TEXT PRIMARY KEY,
  entry_type       TEXT NOT NULL,
  name             TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  content_hash     TEXT NOT NULL,
  embedding        BLOB NOT NULL,
  embedder_id      TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  applies_when     TEXT NOT NULL DEFAULT '',
  not_applies_when TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vec_links(
  src_path TEXT NOT NULL,
  dst_path TEXT NOT NULL,
  PRIMARY KEY(src_path, dst_path)
);
CREATE TABLE IF NOT EXISTS index_state(
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def _entry_kind(entry, from_episodes: bool) -> str:
    """索引内统一的条目类型：knowledge / skill / episode（与 frontmatter type 无关）。"""
    if from_episodes:
        return "episode"
    if "agent_learned_skills" in Path(entry.path).parts:
        return "skill"
    return "knowledge"


def text_to_embed(entry, kind: str) -> str:
    """被嵌入的文本 = hash 的唯一输入。不含任何 decay 相关字段。"""
    if kind == "episode":
        return f"{entry.description}\n{entry.body}".strip()
    return f"{entry.name}\n{entry.description}\n{entry.body[:BODY_EMBED_CHARS]}".strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_related(raw) -> list[str]:
    """frontmatter related 字段：支持 `[a, b]` / `a, b` / list。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [p.strip().strip("'\"") for p in s.split(",") if p.strip().strip("'\"")]


class VectorIndex:
    """SQLite 持久化 + 进程内 numpy 矩阵缓存。所有公共方法线程安全。"""

    def __init__(self, provider: EmbeddingProvider, db_path: Optional[Path] = None):
        self.provider = provider
        self.db_path = Path(db_path) if db_path else (_mm()._get_memory_dir() / ".vec_index" / "index.sqlite")
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        # 缓存：paths / meta 行 / 归一化矩阵
        self._cache_paths: list[str] = []
        self._cache_meta: list[dict] = []
        self._cache_mat: Optional[np.ndarray] = None
        self._cache_links: dict[str, list[str]] = {}

    # ── 存储 ────────────────────────────────────────────────────────────────
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def _state(self, key: str) -> Optional[str]:
        row = self._db().execute("SELECT value FROM index_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_state(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO index_state(key,value) VALUES(?,?)", (key, value))

    def _invalidate_cache(self) -> None:
        self._cache_paths, self._cache_meta, self._cache_mat, self._cache_links = [], [], None, {}

    def _load_cache(self) -> None:
        if self._cache_mat is not None:
            return
        rows = self._db().execute(
            "SELECT path, entry_type, name, description, embedding, applies_when, not_applies_when "
            "FROM vec_meta WHERE embedder_id=? ORDER BY path", (self.provider.provider_id,)
        ).fetchall()
        paths, meta, vecs = [], [], []
        for p, et, name, desc, blob, aw, naw in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            paths.append(p)
            meta.append({"path": p, "entry_type": et, "name": name, "description": desc,
                         "applies_when": aw, "not_applies_when": naw})
            vecs.append(v)
        links: dict[str, list[str]] = {}
        for s, d in self._db().execute("SELECT src_path, dst_path FROM vec_links").fetchall():
            links.setdefault(s, []).append(d)
        self._cache_paths, self._cache_meta, self._cache_links = paths, meta, links
        self._cache_mat = np.vstack(vecs) if vecs else np.zeros((0, self.provider.dim or 1), dtype=np.float32)

    # ── 同步 ────────────────────────────────────────────────────────────────
    def _collect_sources(self) -> tuple[list[tuple], dict[str, str]]:
        """返回 [(path, kind, entry, text, hash)] 与 name→path 映射（供 related 解析）。"""
        mm = _mm()
        items: list[tuple] = []
        name_to_path: dict[str, str] = {}
        for from_ep, loader in ((False, mm.load_all_entries), (True, mm.load_all_episodes)):
            for e in loader():
                kind = _entry_kind(e, from_ep)
                p = str(Path(e.path).resolve())
                txt = text_to_embed(e, kind)
                items.append((p, kind, e, txt, content_hash(txt)))
                name_to_path.setdefault(e.name, p)
                name_to_path.setdefault(Path(e.path).parent.name, p)
        return items, name_to_path

    def sync(self, full: bool = False) -> dict:
        """hash-diff 增量同步。任何异常吞掉记日志，返回统计。"""
        t0 = time.time()
        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0, "failed": 0,
                 "links": 0, "rebuilt": False, "elapsed_ms": 0}
        with self._lock:
            try:
                conn = self._db()
                pid = self.provider.provider_id
                if full or (self._state("embedder_id") not in (None, pid)):
                    conn.execute("DELETE FROM vec_meta")
                    conn.execute("DELETE FROM vec_links")
                    conn.commit()
                    stats["rebuilt"] = True
                    logger.info("[vec-index] embedder_id 变更或强制重建 → 清表 (now=%s)", pid)

                items, name_to_path = self._collect_sources()
                existing = {p: h for p, h in conn.execute("SELECT path, content_hash FROM vec_meta").fetchall()}
                src_paths = {it[0] for it in items}

                to_embed = [it for it in items if existing.get(it[0]) != it[4]]
                stats["skipped"] = len(items) - len(to_embed)
                removed = [p for p in existing if p not in src_paths]

                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                # 逐批 embed；单批失败只计 failed，不影响其余
                B = 32
                for i in range(0, len(to_embed), B):
                    batch = to_embed[i:i + B]
                    try:
                        vecs = self.provider.embed_batch([b[3] for b in batch])
                    except Exception:
                        logger.exception("[vec-index] embed 批次失败 (%d 条)", len(batch))
                        stats["failed"] += len(batch)
                        continue
                    for (p, kind, e, _txt, h), v in zip(batch, vecs):
                        extra = getattr(e, "extra", {}) or {}
                        conn.execute(
                            "INSERT OR REPLACE INTO vec_meta(path,entry_type,name,description,content_hash,"
                            "embedding,embedder_id,updated_at,applies_when,not_applies_when) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (p, kind, e.name, e.description, h,
                             np.asarray(v, dtype=np.float32).tobytes(), pid, now,
                             str(extra.get("applies_when", "") or ""), str(extra.get("not_applies_when", "") or "")),
                        )
                        if p in existing:
                            stats["updated"] += 1
                        else:
                            stats["added"] += 1
                    conn.commit()

                # 边界字段不进 hash → 未 re-embed 的条目也要刷新这两列（便宜：纯元数据 UPDATE）
                for p, kind, e, _txt, h in items:
                    if existing.get(p) == h:
                        extra = getattr(e, "extra", {}) or {}
                        conn.execute(
                            "UPDATE vec_meta SET applies_when=?, not_applies_when=? WHERE path=? AND "
                            "(applies_when<>? OR not_applies_when<>?)",
                            (str(extra.get("applies_when", "") or ""), str(extra.get("not_applies_when", "") or ""),
                             p, str(extra.get("applies_when", "") or ""), str(extra.get("not_applies_when", "") or "")),
                        )

                if removed:
                    conn.executemany("DELETE FROM vec_meta WHERE path=?", [(p,) for p in removed])
                    stats["removed"] = len(removed)

                # related 边：全量重写（数量小，简单可靠）；双向各一行
                conn.execute("DELETE FROM vec_links")
                pairs: set[tuple[str, str]] = set()
                for p, kind, e, _txt, h in items:
                    extra = getattr(e, "extra", {}) or {}
                    for ref in _parse_related(extra.get("related")):
                        dst = name_to_path.get(ref)
                        if dst is None or dst == p:
                            if dst is None:
                                logger.info("[vec-index] related 目标不存在，忽略: %s → %s", e.name, ref)
                            continue
                        pairs.add((p, dst))
                        pairs.add((dst, p))
                if pairs:
                    conn.executemany("INSERT OR IGNORE INTO vec_links(src_path,dst_path) VALUES(?,?)", sorted(pairs))
                stats["links"] = len(pairs)

                self._set_state(conn, "embedder_id", pid)
                self._set_state(conn, "last_sync", now)
                self._set_state(conn, "last_stats", json.dumps(stats, ensure_ascii=False))
                conn.commit()
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                self._invalidate_cache()
            except Exception:
                logger.exception("[vec-index] sync 失败")
                stats["failed"] = max(stats["failed"], 1)
        stats["elapsed_ms"] = int((time.time() - t0) * 1000)
        logger.info("[vec-index] sync +%d ~%d -%d skip=%d fail=%d links=%d %s(%dms)",
                    stats["added"], stats["updated"], stats["removed"], stats["skipped"],
                    stats["failed"], stats["links"], "REBUILT " if stats["rebuilt"] else "", stats["elapsed_ms"])
        return stats

    # ── 检索 ────────────────────────────────────────────────────────────────
    def search(self, query: str, k: int = 15, types: Iterable[str] = ()) -> list[dict]:
        """embed query → cosine top-k。返回 [{path,name,entry_type,cos,description,applies_when,not_applies_when}]。"""
        if not query or not query.strip():
            return []
        vec = self.provider.embed_query(query.strip())
        return self.search_by_vector(vec, k=k, types=types)

    def search_by_vector(self, vec: Sequence[float], k: int = 15, types: Iterable[str] = ()) -> list[dict]:
        """供钩子复用（cue 已 embed 时）。types 为空 = 不过滤。"""
        with self._lock:
            self._load_cache()
            mat, meta = self._cache_mat, self._cache_meta
            if mat is None or mat.shape[0] == 0:
                return []
            q = np.asarray(vec, dtype=np.float32)
            if q.shape[0] != mat.shape[1]:
                logger.warning("[vec-index] query dim=%d != index dim=%d，跳过", q.shape[0], mat.shape[1])
                return []
            q = q / max(float(np.linalg.norm(q)), 1e-12)
            sims = mat @ q
            tset = set(types) if types else None
            idx = np.argsort(-sims)
            out: list[dict] = []
            for i in idx:
                m = meta[int(i)]
                if tset and m["entry_type"] not in tset:
                    continue
                item = dict(m)
                item["cos"] = float(sims[int(i)])
                out.append(item)
                if len(out) >= k:
                    break
            return out

    def neighbors(self, path: str) -> list[str]:
        """1-hop related 邻居（S5 扩散用；S1 只提供数据）。"""
        with self._lock:
            self._load_cache()
            return list(self._cache_links.get(str(path), []))

    def get_meta(self, path: str) -> Optional[dict]:
        with self._lock:
            self._load_cache()
            for m in self._cache_meta:
                if m["path"] == str(path):
                    return dict(m)
            return None

    # ── 状态 ────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            conn = self._db()
            rows = conn.execute("SELECT entry_type, COUNT(*) FROM vec_meta GROUP BY entry_type").fetchall()
            links = conn.execute("SELECT COUNT(*) FROM vec_links").fetchone()[0]
            size = sum(p.stat().st_size for p in self.db_path.parent.glob(self.db_path.name + "*") if p.exists())
            last_stats = self._state("last_stats")
            return {
                "provider_id": self.provider.provider_id,
                "dim": self.provider.dim,
                "db_path": str(self.db_path),
                "db_size_bytes": size,
                "rows": {et: n for et, n in rows},
                "total": sum(n for _, n in rows),
                "links": links,
                "embedder_id": self._state("embedder_id"),
                "last_sync": self._state("last_sync"),
                "last_stats": json.loads(last_stats) if last_stats else None,
            }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
            self._invalidate_cache()


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_index: Optional[VectorIndex] = None
_index_resolved = False
_index_lock = threading.Lock()


def _reset_index_singleton() -> None:
    global _index, _index_resolved
    with _index_lock:
        if _index is not None:
            try:
                _index.close()
            except Exception:
                pass
        _index = None
        _index_resolved = False


def get_index() -> Optional[VectorIndex]:
    """进程级单例；provider 为 None（off/初始化失败）时返回 None，调用点静默跳过。"""
    global _index, _index_resolved
    if _index_resolved:
        return _index
    with _index_lock:
        if _index_resolved:
            return _index
        prov = get_provider()
        _index = VectorIndex(prov) if prov is not None else None
        _index_resolved = True
        return _index


def reset() -> None:
    """测试用：重置 provider 与 index 单例（下次 get_* 重新按 env 解析）。"""
    global _provider, _provider_resolved
    with _provider_lock:
        _provider = None
        _provider_resolved = False
    _reset_index_singleton()


def sync_quietly(reason: str = "") -> Optional[dict]:
    """挂钩用一行接口：get_index() 为 None 直接返回；任何异常吞掉。"""
    try:
        idx = get_index()
        if idx is None:
            return None
        stats = idx.sync()
        if reason:
            logger.info("[vec-index] sync 触发源=%s", reason)
        return stats
    except Exception:
        logger.exception("[vec-index] sync_quietly 失败 (%s)", reason)
        return None
