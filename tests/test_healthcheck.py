"""The nightly self-check, tested against the two outages that prompted it.

Both ran for weeks in production without producing a visible error, so these
tests reproduce the fault rather than the symptom.
"""
import array

import pytest

from server import db, healthcheck, rag
from server.crawler import CrawlError


@pytest.fixture(autouse=True)
def _isolate(app):
    """The health check looks at every bot, and the app fixture is session
    scoped, so one test's zeroed vectors would fail the next test's run."""
    with app.app_context():
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM sources")
    yield


@pytest.fixture
def trained_bot(app, bot_id):
    """A bot with one crawled source and a stored chunk vector."""
    with app.app_context():
        sid = db.execute(
            "INSERT INTO sources (bot_id, type, title, content, meta, status, created_at, "
            "last_crawled_at, recrawl_hours) VALUES (?,?,?,?,?,?,?,?,?)",
            (bot_id, "url", "Home", "We deploy private AI for law firms.",
             '{"url": "https://example.test/", "max_pages": 1}', "ready",
             db.now_iso(), db.now_iso(), 168))
        rag.index_source(bot_id, sid, ["We deploy private AI for law firms."])
        yield bot_id


def _run(app):
    with app.app_context():
        return healthcheck.run()


def test_healthy_system_reports_everything_ok(app, trained_bot, monkeypatch):
    monkeypatch.setattr(healthcheck, "crawl", lambda url, n: {"pages": [url]})
    report = _run(app)
    assert report.healthy, report.text()


def test_a_dead_embedding_provider_is_caught(app, trained_bot, monkeypatch):
    """The revoked-key outage: bots keep replying, just ungrounded."""
    def dead(_texts):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(healthcheck.rag, "embed_texts", dead)
    report = _run(app)
    assert not report.healthy
    names = dict(report.failures)
    assert "embeddings" in names
    assert "401" in names["embeddings"]


def test_unreachable_sites_are_caught(app, trained_bot, monkeypatch):
    """The stale coolify-proxy IP: every re-crawl raised ConnectionError and
    sources silently kept serving old content."""
    def unreachable(url, _n):
        raise CrawlError(f"Couldn't fetch any readable pages from {url} (ConnectionError)")

    monkeypatch.setattr(healthcheck, "crawl", unreachable)
    report = _run(app)
    assert not report.healthy
    names = dict(report.failures)
    assert "sites" in names
    assert "example.test" in names["sites"]


def test_stored_vectors_written_by_a_different_model_are_caught(app, trained_bot, monkeypatch):
    """A fresh embed can succeed while every stored vector is still garbage,
    which is exactly what an outage leaves behind."""
    monkeypatch.setattr(healthcheck, "crawl", lambda url, n: {"pages": [url]})
    with app.app_context():
        dims = len(rag.embed_texts(["x"])[0])
        junk = array.array("f", [0.0] * dims).tobytes()
        db.execute("UPDATE chunks SET embedding = ? WHERE bot_id = ?", (junk, trained_bot))
    report = _run(app)
    assert not report.healthy
    assert "stored_vectors" in dict(report.failures)


def test_a_dimension_mismatch_is_caught(app, trained_bot, monkeypatch):
    """Chunks that no longer match the live model's dimension are invisible to
    retrieve(), which filters on length, so they must be reported here."""
    monkeypatch.setattr(healthcheck, "crawl", lambda url, n: {"pages": [url]})
    with app.app_context():
        short = array.array("f", [0.1, 0.2, 0.3]).tobytes()
        db.execute("UPDATE chunks SET embedding = ? WHERE bot_id = ?", (short, trained_bot))
    report = _run(app)
    assert not report.healthy
    assert "none at" in dict(report.failures)["stored_vectors"]


def test_errored_sources_are_reported(app, trained_bot, monkeypatch):
    monkeypatch.setattr(healthcheck, "crawl", lambda url, n: {"pages": [url]})
    with app.app_context():
        db.execute("UPDATE sources SET status = 'error', error = 'boom' WHERE bot_id = ?",
                   (trained_bot,))
    report = _run(app)
    assert not report.healthy
    assert "boom" in dict(report.failures)["sources"]


def test_long_overdue_recrawls_are_reported(app, trained_bot, monkeypatch):
    monkeypatch.setattr(healthcheck, "crawl", lambda url, n: {"pages": [url]})
    with app.app_context():
        db.execute("UPDATE sources SET last_crawled_at = ? WHERE bot_id = ?",
                   ("2020-01-01T00:00:00+00:00", trained_bot))
    report = _run(app)
    assert not report.healthy
    assert "not attempted" in dict(report.failures)["freshness"]


def test_a_slightly_late_recrawl_is_not_an_alert(app, trained_bot, monkeypatch):
    """A scheduler running late must not page anyone."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(healthcheck, "crawl", lambda url, n: {"pages": [url]})
    with app.app_context():
        late = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()  # 168h + a bit
        db.execute("UPDATE sources SET last_crawled_at = ? WHERE bot_id = ?", (late, trained_bot))
    report = _run(app)
    assert report.healthy, report.text()


def test_exit_code_is_non_zero_when_unhealthy(app, trained_bot, monkeypatch):
    monkeypatch.setattr(healthcheck, "crawl",
                        lambda url, n: (_ for _ in ()).throw(CrawlError("nope")))
    assert healthcheck.main(["--no-email", "--quiet"]) == 1
