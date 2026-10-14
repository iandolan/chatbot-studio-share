"""Query-embedding cache.

Embedding the question is 96-100% of a retrieval call, so repeat questions
(starter-question clicks, "pricing") should not pay for it twice.
"""
import numpy as np
import pytest

from server import rag


@pytest.fixture(autouse=True)
def _clear_cache():
    with rag._query_cache_lock:
        rag._query_cache.clear()
        rag._query_cache_stats.update(hit=0, miss=0)
    yield


def _counting_embedder(calls):
    def embed(texts):
        calls.extend(texts)
        return [[float(len(t)), 1.0, 2.0] for t in texts]
    return embed


def test_repeat_question_is_embedded_once(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "embed_texts", _counting_embedder(calls))

    first = rag._query_vector("what does it cost?")
    second = rag._query_vector("what does it cost?")

    assert calls == ["what does it cost?"], "second call should not hit the API"
    assert np.array_equal(first, second)
    assert rag.query_cache_stats()["hit"] == 1


def test_different_questions_are_embedded_separately(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "embed_texts", _counting_embedder(calls))
    rag._query_vector("alpha")
    rag._query_vector("beta")
    assert calls == ["alpha", "beta"]
    assert rag.query_cache_stats()["miss"] == 2


def test_cached_vector_is_read_only(monkeypatch):
    """A caller mutating the array in place would corrupt every later hit."""
    monkeypatch.setattr(rag, "embed_texts", _counting_embedder([]))
    vec = rag._query_vector("immutable please")
    with pytest.raises(ValueError):
        vec[0] = 99.0


def test_cache_is_bounded_and_evicts_oldest_first(monkeypatch):
    monkeypatch.setattr(rag, "embed_texts", _counting_embedder([]))
    monkeypatch.setattr(rag, "_QUERY_CACHE_MAX", 3)
    for q in ("one", "two", "three", "four"):
        rag._query_vector(q)
    assert rag.query_cache_stats()["size"] == 3
    with rag._query_cache_lock:
        cached = [k[1] for k in rag._query_cache]
    assert "one" not in cached, "oldest entry should have been evicted"
    assert cached == ["two", "three", "four"]


def test_cache_key_includes_the_model(monkeypatch):
    """Switching embedding model must not serve vectors from the old one."""
    calls = []
    monkeypatch.setattr(rag, "embed_texts", _counting_embedder(calls))
    monkeypatch.setattr(rag.Config, "EMBEDDING_MODEL", "model-a")
    rag._query_vector("same text")
    monkeypatch.setattr(rag.Config, "EMBEDDING_MODEL", "model-b")
    rag._query_vector("same text")
    assert len(calls) == 2, "a model change must invalidate the entry"


def test_retrieve_uses_the_cache(app, monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "embed_texts", _counting_embedder(calls))
    with app.app_context():
        rag.retrieve(99999, "no such bot, but the question still embeds")
        rag.retrieve(99999, "no such bot, but the question still embeds")
    assert len(calls) == 1
