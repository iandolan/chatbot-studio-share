"""Chunking, embedding storage, and retrieval over SQLite."""
import array
import re
import threading
from collections import OrderedDict

import numpy as np

from . import db
from .config import Config
from .llm import embed_texts

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
TOP_K = 5
MIN_SCORE = 0.25
GAP_SCORE = 0.30  # below this, log the question as an answer gap

# Embedding the question is effectively all of retrieval's cost: measured on
# prod it is 686-854ms of a 688-892ms call, while the SQL and the cosine pass
# over 557 chunks come to under 40ms between them. Scaling the knowledge base is
# close to free; repeating the network round trip is not. Starter-question
# clicks and common questions ("pricing") repeat constantly, so the same text is
# embedded again and again.
#
# Per worker process, so a 2x4 gunicorn gives 2 of these. Bounded, because the
# keys are visitor-supplied and must not grow without limit.
_QUERY_CACHE_MAX = 512
_query_cache = OrderedDict()
_query_cache_lock = threading.Lock()
_query_cache_stats = {"hit": 0, "miss": 0}


def _query_vector(question):
    """Embed a question, reusing a recent identical one.

    Embeddings are deterministic for a given model and input, so this returns
    the same vector the API would. The cached array is marked read-only: a
    caller that mutated it in place would corrupt every later hit.
    """
    key = (Config.EMBEDDING_MODEL, question)
    with _query_cache_lock:
        hit = _query_cache.get(key)
        if hit is not None:
            _query_cache.move_to_end(key)
            _query_cache_stats["hit"] += 1
            return hit
        _query_cache_stats["miss"] += 1
    vec = np.array(embed_texts([question])[0], dtype=np.float32)
    vec.setflags(write=False)
    with _query_cache_lock:
        _query_cache[key] = vec
        while len(_query_cache) > _QUERY_CACHE_MAX:
            _query_cache.popitem(last=False)
    return vec


def query_cache_stats():
    """{hit, miss, size} for the health check and the dashboard."""
    with _query_cache_lock:
        return dict(_query_cache_stats, size=len(_query_cache))


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks = []
    current = ""
    for para in paragraphs:
        if len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            step = size - overlap
            for start in range(0, len(para), step):
                piece = para[start:start + size]
                if len(piece) > 50 or not chunks:
                    chunks.append(piece)
        elif len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
        else:
            chunks.append(current)
            tail = current[-overlap:]
            current = f"{tail}\n\n{para}" if tail else para
    if current:
        chunks.append(current)
    return chunks


def _pack(vector):
    return array.array("f", vector).tobytes()


def _unpack(blob):
    return np.frombuffer(blob, dtype=np.float32)


def index_source(bot_id, source_id, chunks, updates=None, expected_revision=None, publish_hook=None):
    """Build outside the transaction, then atomically publish a complete version."""
    from .llm import LLMError
    source = db.query("SELECT * FROM sources WHERE id=? AND bot_id=?",
                      (source_id, bot_id), one=True)
    if source is None:
        raise ValueError("Source no longer exists")
    revision = source["revision"] if expected_revision is None else expected_revision
    vectors = embed_texts(chunks) if chunks else []
    if len(vectors) != len(chunks):
        raise LLMError("Embedding provider returned an incomplete result")
    dimensions = {len(v) for v in vectors}
    if vectors and (len(dimensions) != 1 or 0 in dimensions or
                    not all(np.isfinite(v).all() for v in vectors)):
        raise LLMError("Embedding provider returned invalid vectors")
    rows = [(bot_id, source_id, i, text, _pack(v))
            for i, (text, v) in enumerate(zip(chunks, vectors))]
    with db.transaction() as conn:
        current = conn.execute("SELECT * FROM sources WHERE id=? AND bot_id=?",
                               (source_id, bot_id)).fetchone()
        if current is None or current["revision"] != revision:
            raise ValueError("This source changed during indexing. Reload and try again.")
        if current["revision"] or current["chunk_count"]:
            conn.execute("INSERT INTO source_versions(source_id,title,content,meta,created_at) "
                         "VALUES(?,?,?,?,?)", (source_id, current["title"], current["content"],
                                               current["meta"], db.now_iso()))
        conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        conn.executemany("INSERT INTO chunks(bot_id,source_id,position,content,embedding) "
                         "VALUES(?,?,?,?,?)", rows)
        values = {"chunk_count": len(chunks), "status": "ready", "error": "",
                  "revision": revision + 1}
        values.update({k: v for k, v in (updates or {}).items()
                       if k in ("title", "content", "meta", "last_crawled_at")})
        conn.execute("UPDATE sources SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                     (*values.values(), source_id))
        if publish_hook:
            publish_hook(conn)
    return len(chunks)


def retrieve(bot_id, question, top_k=TOP_K):
    """Returns (chunks, best_score). chunks: [{content, title, score}]."""
    query_vec = _query_vector(question)
    rows = db.query(
        """SELECT c.content, c.embedding, s.title, s.type
           FROM chunks c JOIN sources s ON s.id = c.source_id
           WHERE c.bot_id = ? AND length(c.embedding) = ?""",
        (bot_id, len(query_vec) * 4),
    )
    if not rows:
        return [], 0.0
    matrix = np.vstack([_unpack(r["embedding"]) for r in rows])
    norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query_vec) or 1.0)
    norms[norms == 0] = 1.0
    scores = (matrix @ query_vec) / norms
    order = np.argsort(-scores)[:top_k]
    results = []
    for idx in order:
        score = float(scores[idx])
        if score < MIN_SCORE:
            continue
        row = rows[int(idx)]
        results.append({"content": row["content"], "title": row["title"], "score": score})
    best = float(scores[order[0]]) if len(order) else 0.0
    return results, best


def knowledge_block(chunks):
    if not chunks:
        return "(no matching knowledge found)"
    parts = []
    for chunk in chunks:
        parts.append(f"[Source: {chunk['title'] or 'untitled'}]\n{chunk['content']}")
    return "\n\n---\n\n".join(parts)
