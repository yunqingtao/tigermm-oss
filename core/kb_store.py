"""
MemoryVectorStore — 文档知识库向量存储
======================================
纯 stdlib 实现（urllib + sqlite3 + math），零第三方依赖。

- 切块: 复用 core/rag_pipeline.chunk_text (句边界感知)
- 向量: ollama bge-m3 (/api/embed), 1024 维
- 存储: data/memory_vectors.db 的 kb_chunks 表 (与 mem_vectors 对话向量隔离)
- 检索: 纯 Python 余弦相似度, 千级 chunk 毫秒级

用法:
    store = MemoryVectorStore()
    store.add_document(source="产品手册.pdf", text="...")
    hits = store.search("保修期多久", top_k=3)
"""
import json
import logging
import math
import sqlite3
import time
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/embed"

# 🔴 2026-08-24: Ollama 未开时 embedding 必失败, 每次对话 RAG 预检都 print 刷屏。
# 改 logger.debug — 连接拒绝是降级场景(返回空结果), 用户无需看到。
logger = logging.getLogger("kb_store")
EMBED_MODEL = "bge-m3"
DIM = 1024

# kb_chunks 表 — 与 mem_vectors(对话向量) 隔离, 加性设计不动旧数据
DDL = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,          -- 文件名/来源
    chunk_idx   INTEGER,                -- 切片序号
    content     TEXT NOT NULL,          -- 切片文本
    meta        TEXT DEFAULT '{}',      -- JSON 附加元数据 (分类/标签等)
    embedding   BLOB NOT NULL,          -- float32 二进制
    created_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_source ON kb_chunks(source);
"""


def _embed(text: str) -> list[float]:
    """调 ollama bge-m3 生成向量。"""
    body = json.dumps({"model": EMBED_MODEL, "input": text}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    emb = data["embeddings"][0]
    return emb


def _pack(vec: list[float]) -> bytes:
    """float32 列表 → 二进制。struct 无第三方依赖。"""
    import struct
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    import struct
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度。"""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom > 0 else 0.0


class MemoryVectorStore:
    """文档知识库向量存储。"""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "memory_vectors.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.executescript(DDL)
        conn.commit()
        conn.close()

    # ── 写入 ──────────────────────────────────────────────
    def add_chunk(self, source: str, chunk_idx: int, text: str, meta: dict | None = None) -> bool:
        """嵌入并存入单个切片。失败返回 False 不中断批量。"""
        try:
            vec = _embed(text)
        except Exception as e:
            logger.debug("embed 失败 %s#%s: %s", source, chunk_idx, e)
            return False
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "INSERT INTO kb_chunks (source, chunk_idx, content, meta, embedding, created_at) VALUES (?,?,?,?,?,?)",
            (source, chunk_idx, text, json.dumps(meta or {}, ensure_ascii=False), _pack(vec), time.time()),
        )
        conn.commit()
        conn.close()
        return True

    def add_document(self, source: str, text: str, chunk_size: int = 500, overlap: int = 50,
                     meta: dict | None = None) -> int:
        """整篇文档: 切块 → 逐块嵌入入库。返回成功切片数。"""
        from core.rag_pipeline import chunk_text
        chunks = chunk_text(text, chunk_size, overlap)
        ok = 0
        for c in chunks:
            if c["text"] and len(c["text"].strip()) >= 5:  # 过滤空/过短切片
                if self.add_chunk(source, c["index"], c["text"], meta):
                    ok += 1
        return ok

    # ── 检索 ──────────────────────────────────────────────
    def search(self, query: str, top_k: int = 5, min_similarity: float = 0.25) -> list[dict]:
        """向量检索 + 关键词加分重排。"""
        try:
            qvec = _embed(query)
        except Exception as e:
            logger.debug("query embed 失败: %s", e)
            return []

        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute(
            "SELECT id, source, chunk_idx, content, meta, embedding FROM kb_chunks"
        ).fetchall()
        conn.close()

        if not rows:
            return []

        # 纯 Python 余弦: 千级 chunk 够快
        scored = []
        for rid, src, cidx, content, meta_json, blob in rows:
            vec = _unpack(blob)
            sim = _cosine(qvec, vec)
            if sim < min_similarity:
                continue
            scored.append({"id": rid, "source": src, "chunk_idx": cidx,
                           "content": content, "similarity": round(sim, 4)})

        # 关键词加分重排 (复用 rag_pipeline.rerank 逻辑)
        query_terms = [t for t in query.lower().split() if len(t) > 1]
        if query_terms:
            for r in scored:
                content = r["content"].lower()
                kw = sum(1 for t in query_terms if t in content) / len(query_terms)
                r["combined"] = round(r["similarity"] * 0.7 + kw * 0.3, 4)
        else:
            for r in scored:
                r["combined"] = r["similarity"]
        scored.sort(key=lambda x: x["combined"], reverse=True)
        return scored[:top_k]

    def format_context(self, hits: list[dict]) -> str:
        """检索结果 → 模型注入上下文。"""
        if not hits:
            return ""
        lines = ["# 知识库检索结果"]
        for i, h in enumerate(hits, 1):
            lines.append(f"\n## [{i}] 来源: {h['source']} (切片{h['chunk_idx']}, 相似度{h['similarity']})")
            lines.append(h["content"][:800])
        return "\n".join(lines)

    # ── 管理 ──────────────────────────────────────────────
    def count(self) -> int:
        conn = sqlite3.connect(str(self._db_path))
        n = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        conn.close()
        return n

    def list_sources(self) -> list[dict]:
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute(
            "SELECT source, COUNT(*), MAX(created_at) FROM kb_chunks GROUP BY source ORDER BY MAX(created_at) DESC"
        ).fetchall()
        conn.close()
        return [{"source": s, "chunks": c, "time": t} for s, c, t in rows]

    def delete_source(self, source: str) -> int:
        conn = sqlite3.connect(str(self._db_path))
        n = conn.execute("DELETE FROM kb_chunks WHERE source=?", (source,)).rowcount
        conn.commit()
        conn.close()
        return n


if __name__ == "__main__":
    # 自测
    s = MemoryVectorStore()
    print("chunks:", s.count())
    if s.count() == 0:
        n = s.add_document("测试文档.txt", "TMM是虎哥的本地AI助手。知识库功能让AI基于文档回答问题。保修期是一年。")
        print("imported:", n)
    for h in s.search("保修期多久"):
        print(h["source"], h["similarity"], h["content"][:40])
