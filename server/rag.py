"""
Z.R.I.C 引擎 — RAG 知识库模块 (rag.py)
从 main.py 中抽离的 RAG 相关：切片、embedding、检索、REST API。
由 main.py 通过 app.include_router(rag_router) 挂载。
"""

import math
import json
import time
import hashlib
import html
import re
import sqlite3
import threading
from collections import OrderedDict
import fastapi
from fastapi import APIRouter, UploadFile, File, Form, Request
from pydantic import BaseModel
from .logger import get_logger
from . import ai_provider
from .auth import require_account_from_request
from .document_extraction import extract_text_document

_log = get_logger("rag")

# numpy 优先，不可用时降级为纯 Python 模式
try:
    import numpy as np
    _HAS_NUMPY = True
    _log.info("numpy 可用，RAG 检索将使用向量化加速")
except ImportError:
    _HAS_NUMPY = False
    _log.info("numpy 不可用，RAG 检索使用纯 Python 模式（pip install numpy 可加速）")

rag_router = APIRouter(tags=["RAG 知识库"])

# ---------------------------------------------------------
# 数据库连接（由 main.py 注入）
# ---------------------------------------------------------
_db_file: str = ""

# RAG 参数
RAG_CHUNK_SIZE    = 600
RAG_CHUNK_OVERLAP = 80
RAG_TOP_K         = 6
RAG_EMBEDDING_BATCH_SIZE = 16
RAG_EMBEDDING_MIN_INTERVAL = 0.35
RAG_EMBEDDING_CACHE_TTL = 600
RAG_EMBEDDING_CACHE_MAX = 512

_embedding_cache_lock = threading.RLock()
_embedding_api_lock = threading.Lock()
_embedding_cache: OrderedDict[tuple[str, str, str], tuple[float, list[float]]] = OrderedDict()
_last_embedding_call_at = 0.0

def configure_rag(db_file: str):
    """由 main.py 启动时调用，注入依赖。"""
    global _db_file
    _db_file = db_file


def get_db_connection():
    conn = sqlite3.connect(_db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _owner_id_from_request(request: Request) -> int:
    account = require_account_from_request(request)
    return int(account["id"])


# ---------------------------------------------------------
# 数据库表初始化
# ---------------------------------------------------------
def init_rag_tables():
    """创建 RAG 相关表（幂等）。由 main.py 的 init_db() 调用。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS rag_documents (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        source     TEXT NOT NULL DEFAULT '',
        chunk_size INTEGER NOT NULL DEFAULT 0,
        hidden     INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    # 迁移：为旧数据库添加 hidden 列
    try:
        cursor.execute("ALTER TABLE rag_documents ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    cursor.execute('''CREATE TABLE IF NOT EXISTS rag_chunks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id      INTEGER NOT NULL,
        chunk_index INTEGER NOT NULL DEFAULT 0,
        chunk_text  TEXT NOT NULL,
        embedding   TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY(doc_id) REFERENCES rag_documents(id) ON DELETE CASCADE
    )''')
    conn.commit()
    conn.close()


# ---------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------
class RagIngestRequest(BaseModel):
    title:        str
    source:       str = ""
    text:         str
    chunk_size:   int = RAG_CHUNK_SIZE
    chunk_overlap: int = RAG_CHUNK_OVERLAP
    hidden:       int = 0


class RagSearchRequest(BaseModel):
    scene_name: str
    content:    str
    top_k:      int = 0  # 0 = 使用默认值 RAG_TOP_K*2
    use_dense:  bool = False  # 管理区如需语义检索可显式开启；运行时默认不调用 embedding


# ---------------------------------------------------------
# 核心函数：切片、embedding、检索
# ---------------------------------------------------------

def sanitize_knowledge_text(text: str) -> str:
    """Convert extractor HTML/LaTeX artifacts to readable plain text for RAG."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not value:
        return ""
    value = re.sub(r"</(?:tr|p|div|li|h[1-6])\s*>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:td|th)\s*>", " | ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\$=\s*\\mathbf\{([^}]+)\}\s*=\$", r"= \1 =", value)
    value = re.sub(r"\$\\mathbf\{([^}]+)\}\$", r"\1", value)
    value = re.sub(r"\\([*_{}\[\]()#+.!-])", r"\1", value)
    value = re.sub(
        r"CLASS OF SERVICE DESIRED\s*\|.*?PATBOXIDE SHOULD CHECKCLASS OF SERVICE DISRUED\s*\|?",
        "",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"CLASS OF SERVICE DESIRED\s*\|.*?(?:DEFERRED|DISRUED)\s*(?:\|?</td?)?",
        "",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r"</?td\b[^>\n]*(?:>|$)", "", value, flags=re.I)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value

def chunk_text(text: str, max_size: int = 600, overlap: int = 0) -> list[str]:
    """
    语义切分：
    1. 优先按双换行符（\n\n）切分自然段落    ##注释掉
    2. 如果文档没有双换行，fallback 到单换行（\n）切分
    3. 段落超过 max_size 时，按中文/英文句号切分
    4. 保留标点符号，不丢失语义边界
    """
    text = sanitize_knowledge_text(text).strip()
    if not text:
        return []

    # 1. 优先按 \n\n 切分
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    # 2. 如果只得到一个段落（文档可能没有 \n\n），fallback 到 \n
    if len(paragraphs) <= 1 and len(text) > max_size:
        paragraphs = [p.strip() for p in text.split('\n') if p.strip()]

    chunks = []
    for p in paragraphs:
        if len(p) <= max_size:
            chunks.append(p)
        else:
            # 3. 按句号/问号/叹号切分（保留标点）
            sentences = re.split(r'(?<=[。！？!?])', p)
            current = ""
            for s in sentences:
                if not s.strip():
                    continue
                if len(current) + len(s) <= max_size:
                    current += s
                else:
                    if current:
                        chunks.append(current.strip())
                    current = s
            if current:
                chunks.append(current.strip())

    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            overlapped.append(tail + chunks[i])
        chunks = overlapped

    return chunks


_SPARSE_STOP_TERMS = {
    "玩家", "行动", "场景", "当前", "选择", "内容", "描述", "结果", "推演",
    "the", "and", "for", "with", "this", "that", "from", "into", "scene",
}


def _extract_sparse_terms(query_text: str, max_terms: int = 12) -> list[str]:
    """提取少量可用于 LIKE 检索的关键词，避免运行时为了召回去打 embedding。"""
    normalized = (query_text or "").lower()
    terms: list[str] = []

    def add(term: str):
        term = term.strip().lower()
        if len(term) < 2 or term in _SPARSE_STOP_TERMS:
            return
        if term not in terms:
            terms.append(term)

    for token in re.findall(r"[a-z0-9_]{3,}", normalized):
        add(token)

    for token in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        parts = [token] if len(token) <= 8 else [
            part for part in re.split(r"[，。！？；、\s的了是在和与及对向中]", token)
            if 2 <= len(part) <= 8
        ]
        for part in parts:
            add(part)
            # 中文没有天然空格，补充短 n-gram，提升无 embedding 运行时召回。
            for size in (4, 3, 2):
                if len(part) <= size:
                    continue
                for start in range(0, len(part) - size + 1):
                    add(part[start:start + size])

    return terms[:max_terms]


def _embedding_cache_scope(owner_account_id: int | None = None) -> tuple[str, str]:
    """Cache keys must follow the active provider/model, otherwise vectors may be incompatible."""
    try:
        cfg = ai_provider.get_config(owner_account_id=owner_account_id)
        provider_id = cfg.provider_id
    except Exception:
        provider_id = ""
    return provider_id, ai_provider.get_active_model("embedding", owner_account_id=owner_account_id) or ""


def _embedding_cache_key(text: str, scope: tuple[str, str]) -> tuple[str, str, str]:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return scope[0], scope[1], digest


def _embedding_cache_get(key: tuple[str, str, str]) -> list[float] | None:
    now = time.monotonic()
    with _embedding_cache_lock:
        item = _embedding_cache.get(key)
        if not item:
            return None
        ts, vec = item
        if now - ts > RAG_EMBEDDING_CACHE_TTL:
            _embedding_cache.pop(key, None)
            return None
        _embedding_cache.move_to_end(key)
        return list(vec)


def _embedding_cache_put(key: tuple[str, str, str], vec: list[float]):
    if not vec:
        return
    with _embedding_cache_lock:
        _embedding_cache[key] = (time.monotonic(), list(vec))
        _embedding_cache.move_to_end(key)
        while len(_embedding_cache) > RAG_EMBEDDING_CACHE_MAX:
            _embedding_cache.popitem(last=False)


def _wait_for_embedding_slot():
    """Serialize embedding bursts so live play does not hammer OpenAI-compatible endpoints."""
    global _last_embedding_call_at
    now = time.monotonic()
    wait_sec = max(0.0, RAG_EMBEDDING_MIN_INTERVAL - (now - _last_embedding_call_at))
    if wait_sec:
        time.sleep(wait_sec)
    _last_embedding_call_at = time.monotonic()


def get_embeddings(texts: list[str], *, owner_account_id: int | None = None) -> list[list[float]]:
    """
    调用统一 OpenAI 兼容端点获取 embedding 向量。
    带缓存、去重和 rate-limit 保护，避免运行中同一查询反复触发 embedding。
    """
    if not texts or not ai_provider.get_active_model("embedding", owner_account_id=owner_account_id):
        return []

    scope = _embedding_cache_scope(owner_account_id=owner_account_id)
    all_embeddings: list[list[float]] = [[] for _ in texts]
    missing: OrderedDict[tuple[str, str, str], dict] = OrderedDict()

    for idx, text in enumerate(texts):
        if not (text or "").strip():
            continue
        key = _embedding_cache_key(text, scope)
        cached = _embedding_cache_get(key)
        if cached is not None:
            all_embeddings[idx] = cached
            continue
        if key not in missing:
            missing[key] = {"text": text, "indices": []}
        missing[key]["indices"].append(idx)

    missing_items = list(missing.items())
    if not missing_items:
        return all_embeddings

    for i in range(0, len(missing_items), RAG_EMBEDDING_BATCH_SIZE):
        batch_items = missing_items[i:i + RAG_EMBEDDING_BATCH_SIZE]
        batch = [item["text"] for _key, item in batch_items]
        retry = 0
        while retry < 3:
            try:
                with _embedding_api_lock:
                    _wait_for_embedding_slot()
                    resp = ai_provider.embedding_create(batch, owner_account_id=owner_account_id)
                items = sorted(resp.data, key=lambda x: x.index)
                batch_embeddings: list[list[float]] = [[] for _ in batch]
                for item in items:
                    if 0 <= item.index < len(batch_embeddings):
                        batch_embeddings[item.index] = item.embedding
                for (key, meta), emb in zip(batch_items, batch_embeddings):
                    _embedding_cache_put(key, emb)
                    for target_idx in meta["indices"]:
                        all_embeddings[target_idx] = emb
                break
            except Exception as e:
                retry += 1
                if "429" in str(e) or "rate" in str(e).lower():
                    _log.debug("Embedding 429 限流，第 %d 次重试", retry)
                    time.sleep(1.0 * retry)  # 指数退避
                elif retry >= 3:
                    # 最终失败：用空向量占位
                    _log.warning("Embedding 批次最终失败（3次重试耗尽）: %s", e)
                    break
                else:
                    _log.debug("Embedding 调用异常（第 %d 次重试）: %s", retry, e)
                    time.sleep(0.5)

        # 批间冷却：防止并发速率限制
        if i + RAG_EMBEDDING_BATCH_SIZE < len(missing_items):
            time.sleep(0.3)

    return all_embeddings


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot   = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------
# 向量缓存（内存加速检索，避免每次全表 json.loads）
# ---------------------------------------------------------
class _VectorCache:
    """
    将 rag_chunks 表中的 embedding 预加载到内存。
    - numpy 可用时：存为 float32 矩阵，检索用矩阵乘法（毫秒级）
    - numpy 不可用时：存为 list[list[float]]，逐行计算（兼容模式）
    线程安全：通过 RLock 保护读写。
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._chunk_ids: list[int] = []       # chunk_id 列表，与矩阵行一一对应
        self._texts: list[str] = []           # chunk_text
        self._titles: list[str] = []          # document title
        self._doc_ids: list[int] = []         # doc_id（用于按文档删除）
        self._chunk_indexes: list[int] = []   # chunk_index
        self._matrix = None                   # numpy ndarray 或 list[list[float]]
        self._norms = None                    # 预计算的 L2 范数
        self._ready = False

    def reload(self, db_file: str = ""):
        """从数据库全量加载向量到内存。"""
        target_db = db_file or _db_file
        if not target_db:
            return
        try:
            conn = sqlite3.connect(target_db, timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT c.id, c.chunk_text, c.embedding, c.chunk_index, c.doc_id, d.title "
                "FROM rag_chunks c JOIN rag_documents d ON c.doc_id = d.id "
                "WHERE d.hidden=0 "
                "ORDER BY c.id"
            ).fetchall()
            conn.close()
        except Exception as e:
            _log.warning("向量缓存加载失败: %s", e)
            return

        ids, texts, titles, doc_ids, chunk_indexes, vectors = [], [], [], [], [], []
        for row in rows:
            try:
                emb = json.loads(row["embedding"])
                if not emb or not isinstance(emb, list):
                    continue
                ids.append(row["id"])
                texts.append(row["chunk_text"])
                titles.append(row["title"])
                doc_ids.append(row["doc_id"])
                chunk_indexes.append(row["chunk_index"])
                vectors.append(emb)
            except (json.JSONDecodeError, TypeError):
                continue

        with self._lock:
            self._chunk_ids = ids
            self._texts = texts
            self._titles = titles
            self._doc_ids = doc_ids
            self._chunk_indexes = chunk_indexes
            if _HAS_NUMPY and vectors:
                mat = np.array(vectors, dtype=np.float32)
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms == 0] = 1.0  # 避免除零
                self._matrix = mat / norms  # 预归一化，检索时只需 dot
                self._norms = norms
            else:
                self._matrix = vectors  # fallback: list[list[float]]
                self._norms = None
            self._ready = bool(ids)
            _log.info("向量缓存已加载: %d 个有效向量", len(ids))

    def search(self, query_vec: list[float], top_k: int = RAG_TOP_K,
               threshold: float = 0.3) -> list[dict]:
        """
        检索最相关的 top_k 个切片。
        返回 [{"score": float, "chunk_text": str, "title": str, "doc_id": int, "chunk_index": int}, ...]
        """
        with self._lock:
            if not self._ready or not self._chunk_ids:
                return []

            if _HAS_NUMPY and isinstance(self._matrix, np.ndarray):
                # numpy 向量化：query 归一化后矩阵乘法
                qvec = np.array(query_vec, dtype=np.float32)
                qnorm = np.linalg.norm(qvec)
                if qnorm == 0:
                    return []
                qvec = qvec / qnorm
                scores = self._matrix @ qvec  # (N,) cosine similarities
                # 取 top_k（先过滤阈值）
                mask = scores >= threshold
                if not mask.any():
                    return []
                indices = np.where(mask)[0]
                top_indices = indices[np.argsort(scores[indices])[::-1][:top_k]]
                return [
                    {
                        "score": round(float(scores[i]), 4),
                        "chunk_text": self._texts[i],
                        "title": self._titles[i],
                        "doc_id": self._doc_ids[i],
                        "chunk_index": self._chunk_indexes[i],
                    }
                    for i in top_indices
                ]
            else:
                # 纯 Python fallback
                scored = []
                for i, emb in enumerate(self._matrix):
                    score = cosine_similarity(query_vec, emb)
                    if score >= threshold:
                        scored.append((score, i))
                scored.sort(key=lambda x: x[0], reverse=True)
                return [
                    {
                        "score": round(scored[j][0], 4),
                        "chunk_text": self._texts[scored[j][1]],
                        "title": self._titles[scored[j][1]],
                        "doc_id": self._doc_ids[scored[j][1]],
                        "chunk_index": self._chunk_indexes[scored[j][1]],
                    }
                    for j in range(min(top_k, len(scored)))
                ]

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._chunk_ids)


# 全局缓存实例
_vec_cache = _VectorCache()


def refresh_vector_cache():
    """外部调用入口：刷新向量缓存。由 main.py 在加载剧本/重建 embedding 后调用。"""
    _vec_cache.reload()


# ---------------------------------------------------------
# 核心检索函数（使用向量缓存加速）
# ---------------------------------------------------------


# ---------------------------------------------------------
# 混合检索核心（关键词预匹配 + 向量语义检索）
# ---------------------------------------------------------
def _hybrid_retrieve(conn, query_text: str, top_k: int = RAG_TOP_K,
                     vec_threshold: float = 0.3,
                     allow_dense: bool = True,
                     owner_account_id: int | None = None) -> list[dict]:
    """
    混合检索：Sparse（关键词匹配）+ 可选 Dense（向量语义检索）。
    返回 [{"score": float, "chunk_text": str, "title": str, ...}, ...]
    运行时 prompt 注入默认只走 Sparse，避免每回合触发 embedding。
    """
    seen_texts = set()  # 去重用
    exact_results = []
    sparse_quota = top_k  # 玩家运行时优先使用确定命中的设定，避免不必要 embedding

    # ── 1. 关键词预匹配（Sparse）──
    normalized_query = query_text.lower()

    # 从 world_entities 提取实体名。老库/轻量测试库可能没有该表，失败时仍继续文档检索。
    try:
        entities = conn.execute(
            "SELECT name FROM world_entities WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
    except Exception as e:
        _log.debug("RAG 实体关键词检索跳过: %s", e)
        entities = []

    try:
        # 从 rag_documents 提取文档标题
        doc_titles = conn.execute(
            "SELECT title FROM rag_documents WHERE title IS NOT NULL AND title != ''"
        ).fetchall()

        # 收集命中的关键词（过滤长度 < 2 的，防止"王"匹配"国王"等误命中）
        hit_keywords = set()
        for row in entities:
            name = row["name"]
            if len(name) >= 2 and name.lower() in normalized_query:
                hit_keywords.add(name)
        for row in doc_titles:
            title = row["title"]
            if len(title) >= 2:
                title_lower = title.lower()
                if title_lower in normalized_query:
                    hit_keywords.add(title)
                else:
                    for term in _extract_sparse_terms(title, max_terms=16):
                        if term in normalized_query:
                            hit_keywords.add(term)

        # 对命中关键词执行精确查找
        if hit_keywords:
            for kw in hit_keywords:
                if len(exact_results) >= sparse_quota:
                    break
                rows = conn.execute(
                    "SELECT c.chunk_text, c.chunk_index, c.doc_id, d.title "
                    "FROM rag_chunks c JOIN rag_documents d ON c.doc_id = d.id "
                    "WHERE d.hidden=0 AND (d.title LIKE ? OR c.chunk_text LIKE ?) LIMIT 2",
                    (f"%{kw}%", f"%{kw}%")
                ).fetchall()
                for r in rows:
                    if r["chunk_text"] not in seen_texts and len(exact_results) < sparse_quota:
                        exact_results.append({
                            "score": 1.0,
                            "chunk_text": r["chunk_text"],
                            "title": f"[精确]{r['title']}",
                            "doc_id": r["doc_id"],
                            "chunk_index": r["chunk_index"],
                        })
                        seen_texts.add(r["chunk_text"])

        # 关键词兜底：没有实体/标题精确命中时，也可通过少量查询词召回公开知识。
        if len(exact_results) < sparse_quota:
            for term in _extract_sparse_terms(query_text):
                if len(exact_results) >= sparse_quota:
                    break
                rows = conn.execute(
                    "SELECT c.chunk_text, c.chunk_index, c.doc_id, d.title "
                    "FROM rag_chunks c JOIN rag_documents d ON c.doc_id = d.id "
                    "WHERE d.hidden=0 AND (d.title LIKE ? OR c.chunk_text LIKE ?) LIMIT 2",
                    (f"%{term}%", f"%{term}%")
                ).fetchall()
                for r in rows:
                    if r["chunk_text"] not in seen_texts and len(exact_results) < sparse_quota:
                        exact_results.append({
                            "score": 0.72,
                            "chunk_text": r["chunk_text"],
                            "title": f"[关键词]{r['title']}",
                            "doc_id": r["doc_id"],
                            "chunk_index": r["chunk_index"],
                        })
                        seen_texts.add(r["chunk_text"])
    except Exception as e:
        _log.debug("RAG 关键词预匹配异常（已跳过）: %s", e)

    if exact_results and (vec_threshold >= 0.3 or not allow_dense):
        return exact_results[:top_k]

    if not allow_dense:
        return exact_results[:top_k]

    # ── 2. 向量语义检索（Dense）──
    dense_quota = top_k - len(exact_results)
    vec_results = []

    if dense_quota > 0:
        try:
            query_vecs = get_embeddings([query_text], owner_account_id=owner_account_id)
        except Exception as e:
            _log.warning("RAG 检索 embedding 失败: %s", e)
            query_vecs = []

        if query_vecs and query_vecs[0]:
            query_vec = query_vecs[0]

            if _vec_cache.is_ready:
                # 内存缓存检索（多取一些，去重后截断）
                raw = _vec_cache.search(query_vec, top_k=dense_quota + len(exact_results),
                                         threshold=vec_threshold)
                for r in raw:
                    if r["chunk_text"] not in seen_texts and len(vec_results) < dense_quota:
                        vec_results.append(r)
                        seen_texts.add(r["chunk_text"])
            else:
                # 缓存未就绪：数据库全表扫描
                rows = conn.execute(
                    "SELECT c.id, c.chunk_text, c.embedding, c.chunk_index, c.doc_id, d.title "
                    "FROM rag_chunks c JOIN rag_documents d ON c.doc_id = d.id "
                    "WHERE d.hidden=0"
                ).fetchall()
                scored = []
                for row in rows:
                    try:
                        emb = json.loads(row["embedding"])
                        if not emb:
                            continue
                        score = cosine_similarity(query_vec, emb)
                        if score >= vec_threshold and row["chunk_text"] not in seen_texts:
                            scored.append({
                                "score": round(score, 4),
                                "chunk_text": row["chunk_text"],
                                "title": row["title"],
                                "doc_id": row["doc_id"],
                                "chunk_index": row["chunk_index"],
                            })
                    except Exception:
                        continue
                scored.sort(key=lambda x: x["score"], reverse=True)
                vec_results = scored[:dense_quota]

    # ── 3. 合并：精确匹配置顶，向量补充 ──
    return exact_results + vec_results


def rag_retrieve(conn, query_text: str, top_k: int = RAG_TOP_K,
                 allow_dense: bool = False,
                 owner_account_id: int | None = None) -> str:
    """
    检索最相关的 top_k 个切片，格式化为 prompt 注入文本。
    运行时默认只使用关键词/实体/标题检索，不调用 embedding。
    """
    results = _hybrid_retrieve(
        conn,
        query_text,
        top_k=top_k,
        allow_dense=allow_dense,
        owner_account_id=owner_account_id,
    )
    if not results:
        return ""
    return "\n\n".join(
        f"[来源：{r['title']}]\n{sanitize_knowledge_text(r['chunk_text'])}" for r in results
    )


# ---------------------------------------------------------
# REST API 端点
# ---------------------------------------------------------
@rag_router.get("/api/rag/documents")
def rag_list_documents(request: Request, include_hidden: int = 0):
    show_hidden = bool(include_hidden)
    if show_hidden:
        _owner_id_from_request(request)
    conn = get_db_connection()
    query = "SELECT * FROM rag_documents"
    params = ()
    if not show_hidden:
        query += " WHERE hidden=0"
    query += " ORDER BY created_at DESC"
    docs = conn.execute(query, params).fetchall()
    result = []
    for d in docs:
        chunk_count = conn.execute(
            "SELECT COUNT(*) as c FROM rag_chunks WHERE doc_id=?", (d["id"],)
        ).fetchone()["c"]
        result.append({**dict(d), "chunk_count": chunk_count})
    conn.close()
    return {"status": "success", "documents": result}


@rag_router.get("/api/rag/documents/{doc_id}")
def rag_get_document(doc_id: int):
    """读取单个知识库文档及其切片，供玩家友好的资料查看面板使用。"""
    conn = get_db_connection()
    doc = conn.execute("SELECT * FROM rag_documents WHERE id=? AND hidden=0", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return {"status": "error", "message": "文档不存在或未公开", "document": None, "chunks": []}
    chunks = [dict(r) for r in conn.execute(
        "SELECT id, chunk_index, chunk_text FROM rag_chunks WHERE doc_id=? ORDER BY chunk_index",
        (doc_id,)
    ).fetchall()]
    conn.close()
    return {"status": "success", "document": dict(doc), "chunks": chunks}


@rag_router.post("/api/rag/ingest")
def rag_ingest(req: RagIngestRequest, request: Request):
    """导入文档：切片 → embedding → 写入数据库。带 rate-limit 保护。"""
    owner_account_id = _owner_id_from_request(request)
    t0 = time.time()

    chunks = chunk_text(req.text, max_size=req.chunk_size, overlap=req.chunk_overlap)
    if not chunks:
        return {"status": "error", "message": "文本为空，无法切片"}

    conn = get_db_connection()
    cur = conn.execute(
        "INSERT INTO rag_documents (title, source, chunk_size, hidden) VALUES (?,?,?,?)",
        (req.title[:100], req.source[:200], len(chunks), 1 if req.hidden else 0)
    )
    doc_id = cur.lastrowid

    all_embeddings = get_embeddings(chunks, owner_account_id=owner_account_id)

    for idx, chunk in enumerate(chunks):
        emb = all_embeddings[idx] if idx < len(all_embeddings) else []
        conn.execute(
            "INSERT INTO rag_chunks (doc_id, chunk_index, chunk_text, embedding) VALUES (?,?,?,?)",
            (doc_id, idx, chunk, json.dumps(emb))
        )

    conn.commit()
    conn.close()

    # 导入完成后刷新向量缓存
    refresh_vector_cache()

    elapsed = round(time.time() - t0, 1)
    embedded_count = sum(1 for e in all_embeddings if e)
    return {
        "status": "success",
        "doc_id": doc_id,
        "chunk_count": len(chunks),
        "embedded": embedded_count,
        "elapsed_sec": elapsed,
    }


@rag_router.post("/api/rag/upload")
async def rag_upload(
    request:       Request,
    file:          UploadFile = File(...),
    title:         str        = Form(""),
    source:        str        = Form(""),
    chunk_size:    int        = Form(RAG_CHUNK_SIZE),
    chunk_overlap: int        = Form(RAG_CHUNK_OVERLAP),
    hidden:        int        = Form(0),
):
    """上传 TXT / Markdown / PDF / Word 文件并导入知识库。"""
    owner_account_id = _owner_id_from_request(request)
    t0 = time.time()
    filename = file.filename or "unknown"
    raw = await file.read()

    try:
        text, extraction_warnings = extract_text_document(raw, filename)
    except fastapi.HTTPException as exc:
        return {"status": "error", "message": str(exc.detail)}

    text = text.strip()
    if not text:
        detail = "文件内容为空"
        if extraction_warnings:
            detail = f"{detail}；{extraction_warnings[-1]}"
        return {"status": "error", "message": detail}

    doc_title  = (title.strip()  or filename)[:100]
    doc_source = (source.strip() or filename)[:200]

    chunks = chunk_text(text, max_size=chunk_size, overlap=chunk_overlap)
    if not chunks:
        return {"status": "error", "message": "文本切片失败"}

    conn = get_db_connection()
    cur = conn.execute(
        "INSERT INTO rag_documents (title, source, chunk_size, hidden) VALUES (?,?,?,?)",
        (doc_title, doc_source, len(chunks), 1 if hidden else 0)
    )
    doc_id = cur.lastrowid

    all_embeddings = get_embeddings(chunks, owner_account_id=owner_account_id)
    for idx, chunk in enumerate(chunks):
        emb = all_embeddings[idx] if idx < len(all_embeddings) else []
        conn.execute(
            "INSERT INTO rag_chunks (doc_id, chunk_index, chunk_text, embedding) VALUES (?,?,?,?)",
            (doc_id, idx, chunk, json.dumps(emb))
        )

    conn.commit()
    conn.close()
    refresh_vector_cache()

    elapsed = round(time.time() - t0, 1)
    embedded_count = sum(1 for e in all_embeddings if e)
    return {
        "status":      "success",
        "doc_id":      doc_id,
        "filename":    filename,
        "char_count":  len(text),
        "chunk_count": len(chunks),
        "embedded":    embedded_count,
        "elapsed_sec": elapsed,
        "warnings":    extraction_warnings,
    }


@rag_router.delete("/api/rag/documents/{doc_id}")
def rag_delete_document(doc_id: int, request: Request):
    _owner_id_from_request(request)
    conn = get_db_connection()
    conn.execute("DELETE FROM rag_chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM rag_documents WHERE id=?", (doc_id,))
    conn.commit(); conn.close()
    # 删除后刷新缓存
    refresh_vector_cache()
    return {"status": "success"}


@rag_router.patch("/api/rag/documents/{doc_id}/hidden")
def rag_toggle_hidden(doc_id: int, hidden: int, request: Request):
    _owner_id_from_request(request)
    """设置文档的隐藏状态（hidden=0 公开，hidden=1 隐藏）。"""
    conn = get_db_connection()
    conn.execute("UPDATE rag_documents SET hidden=? WHERE id=?", (1 if hidden else 0, doc_id))
    conn.commit(); conn.close()
    refresh_vector_cache()
    return {"status": "success", "doc_id": doc_id, "hidden": hidden}


@rag_router.post("/api/rag/search")
def rag_search(req: RagSearchRequest, request: Request):
    """独立检索接口，供前端测试知识库效果；默认模拟运行时的无 embedding 检索。"""
    owner_account_id = _owner_id_from_request(request) if req.use_dense else None
    query = f"{req.scene_name} {req.content}"
    conn = get_db_connection()
    effective_top_k = req.top_k if req.top_k > 0 else RAG_TOP_K * 2
    results = _hybrid_retrieve(
        conn,
        query,
        top_k=effective_top_k,
        vec_threshold=0.0 if req.use_dense else 0.3,
        allow_dense=req.use_dense,
        owner_account_id=owner_account_id,
    )
    conn.close()
    return {"status": "success", "results": results}
