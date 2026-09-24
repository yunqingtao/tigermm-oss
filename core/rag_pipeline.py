
"""
RAGPipeline — document chunking, embedding, and semantic retrieval.
Built on MemoryVectorStore. Adds split→embed→store→recall→rerank pipeline.
"""
import re, logging
from typing import Optional

logger = logging.getLogger("core.rag_pipeline")


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    if not text or len(text) < chunk_size:
        return [{"index": 0, "text": text, "char_start": 0, "char_end": len(text)}]

    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        # Try to break at sentence boundary (。！？\n)
        if end < len(text):
            for sep in ['。', '！', '？', '\n\n', '\n', '. ', '! ', '? ']:
                pos = text.rfind(sep, start + chunk_size // 2, end)
                if pos > start:
                    end = pos + 1
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append({
                "index": idx,
                "text": chunk,
                "char_start": start,
                "char_end": end,
            })
            idx += 1
        start = end - overlap if end < len(text) else len(text)

    return chunks


def chunk_file(filepath: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """Read and chunk a file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
        chunks = chunk_text(text, chunk_size, overlap)
        for c in chunks:
            c["source"] = filepath
        logger.info("RAG: %s → %d chunks", filepath, len(chunks))
        return chunks
    except Exception as e:
        logger.warning("RAG chunk_file error: %s", e)
        return []


def rerank(query: str, results: list[dict], top_k: int = 3) -> list[dict]:
    """Simple keyword-based reranking on top of vector similarity."""
    if not results:
        return results

    query_terms = set(query.lower().split())
    for r in results:
        content = r.get("content", "").lower()
        # Bonus for exact keyword matches
        kw_score = sum(1 for t in query_terms if t in content) / max(len(query_terms), 1)
        r["keyword_score"] = round(kw_score, 3)
        r["combined_score"] = round(r.get("similarity", 0) * 0.7 + kw_score * 0.3, 3)

    results.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    return results[:top_k]


async def ingest_file(filepath: str, store) -> int:
    """Full pipeline: chunk → embed → store."""
    chunks = chunk_file(filepath)
    if not chunks:
        return 0

    count = 0
    for c in chunks:
        await store.store_with_embedding(
            session_id=f"rag:{filepath}",
            turn_index=c["index"],
            role="document",
            content=c["text"],
            metadata={"source": filepath, "char_start": c["char_start"], "char_end": c["char_end"]},
        )
        count += 1

    logger.info("RAG: ingested %s → %d chunks", filepath, count)
    return count


def recall_with_context(store, query: str, top_k: int = 5) -> str:
    """Recall + format as context for model injection."""
    results = store.recall(query, top_k=top_k, min_similarity=0.3)
    if not results:
        return ""

    reranked = rerank(query, results, top_k=min(top_k, len(results)))
    lines = ["# Retrieved Context (RAG)"]
    for i, r in enumerate(reranked, 1):
        lines.append(f"\n## Chunk {i} (similarity: {r.get('similarity', 0):.2f}, source: {r.get('metadata', {}).get('source', 'unknown')})")
        lines.append(r.get("content", "")[:1000])
    return "\n".join(lines)
