"""Global daily LLM-call cap (GLOBAL_DAILY_LLM_CALLS): increment + enforcement
mechanics, midnight-UTC reset, zero-means-off, mock-mode/embeddings exemption,
widget degradation (non-streaming + streaming), and the dashboard banner."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server import budget, db, llm
from server.config import Config

CHAT_RESPONSE = {
    "choices": [{"message": {"content": "A real provider reply"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}
EMBED_RESPONSE = {"data": [{"index": 0, "embedding": [0.5, 0.5]}]}


@pytest.fixture(autouse=True)
def clean_counter(app):
    with app.app_context():
        db.execute("DELETE FROM llm_usage_daily")
    yield


@pytest.fixture()
def fake_provider(monkeypatch):
    """Real (non-mock) mode with OpenRouter as platform provider and all HTTP
    replaced by canned responses — no network, no real keys."""
    monkeypatch.setattr(Config, "MOCK_LLM", False)
    monkeypatch.setattr(Config, "DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "sk-or-fake")
    calls = []

    def fake_post(url, headers, payload):
        calls.append(url)
        return EMBED_RESPONSE if url.endswith("/embeddings") else CHAT_RESPONSE

    def fake_stream(url, headers, payload):
        calls.append(url)
        yield json.dumps({"choices": [{"delta": {"content": "A real provider reply"}}]})
        yield json.dumps({"choices": [],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    monkeypatch.setattr(llm, "_http_post", fake_post)
    monkeypatch.setattr(llm, "_http_post_stream", fake_stream)
    return calls


def _counter(app, day=None):
    with app.app_context():
        row = db.query("SELECT calls FROM llm_usage_daily WHERE day = ?",
                       (day or budget.utc_day(),), one=True)
        return row["calls"] if row else 0


def _embed_key(client, bot_id):
    return client.get(f"/api/bots/{bot_id}").get_json()["bot"]["embed_key"]


def _chat(client, key, message="hello there"):
    return client.post("/api/widget/chat", json={
        "key": key, "message": message, "session_id": uuid.uuid4().hex[:12]})


# ---------- counter mechanics ----------

def test_increments_then_enforces(app, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 3)
    with app.app_context():
        results = [budget.try_register_call() for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert _counter(app) == 3  # denied calls don't inflate the count
    with app.app_context():
        assert budget.status() == {"cap": 3, "calls": 3, "reached": True}


def test_zero_or_unset_means_no_cap(app, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 0)
    with app.app_context():
        assert all(budget.try_register_call() for _ in range(50))
        assert budget.status() == {"cap": 0, "calls": 0, "reached": False}
    assert _counter(app) == 0  # cap off: nothing is even written


def test_resets_at_utc_midnight(app, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 2)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    with app.app_context():
        # Yesterday was fully spent…
        db.execute("INSERT INTO llm_usage_daily (day, calls) VALUES (?, ?)", (yesterday, 2))
        # …but the new UTC day starts from zero.
        assert budget.try_register_call() is True
    assert _counter(app) == 1
    assert _counter(app, yesterday) == 2


# ---------- widget degradation ----------

def test_widget_chat_degrades_to_fallback_when_cap_hit(app, client, bot_id,
                                                       fake_provider, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 3)
    bot = client.get(f"/api/bots/{bot_id}").get_json()["bot"]
    key = bot["embed_key"]

    for _ in range(3):
        resp = _chat(client, key)
        assert resp.status_code == 200
        assert resp.get_json()["reply"] == "A real provider reply"

    over = _chat(client, key).get_json()
    assert over["reply"] == bot["fallback_message"]  # graceful, mock/error-style
    assert _counter(app) == 3
    chat_calls = [u for u in fake_provider if not u.endswith("/embeddings")]
    assert len(chat_calls) == 3  # no HTTP once the budget is spent


def test_streaming_counts_and_degrades(app, client, bot_id, fake_provider, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 1)
    bot = client.get(f"/api/bots/{bot_id}").get_json()["bot"]

    def stream_events(resp):
        return [json.loads(line[5:]) for line in
                resp.get_data(as_text=True).splitlines() if line.startswith("data:")]

    resp = client.post("/api/widget/chat/stream", json={
        "key": bot["embed_key"], "message": "hi", "session_id": uuid.uuid4().hex[:12]})
    events = stream_events(resp)
    assert "".join(e.get("delta", "") for e in events[:-1]) == "A real provider reply"
    assert _counter(app) == 1

    resp = client.post("/api/widget/chat/stream", json={
        "key": bot["embed_key"], "message": "hi again", "session_id": uuid.uuid4().hex[:12]})
    events = stream_events(resp)
    assert events[0]["delta"] == bot["fallback_message"]
    assert events[-1]["done"] is True
    assert "budget" in (events[-1]["error"] or "")
    assert _counter(app) == 1


def test_mock_mode_is_never_counted(app, client, bot_id, monkeypatch):
    # Config.MOCK_LLM is True in the test env — the cap must not tick.
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 2)
    key = _embed_key(client, bot_id)
    for _ in range(4):
        assert _chat(client, key).status_code == 200
    assert _counter(app) == 0


def test_embeddings_are_not_counted(app, fake_provider, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 5)
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "sk-openai-embed")
    with app.app_context():
        llm.embed_texts(["some training text"])
    assert _counter(app) == 0


# ---------- dashboard banner ----------

def test_banner_shows_when_budget_reached(app, client, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 3)
    with app.app_context():
        db.execute("INSERT INTO llm_usage_daily (day, calls) VALUES (?, ?)",
                   (budget.utc_day(), 3))
    html = client.get("/").get_data(as_text=True)
    assert "Daily AI budget reached" in html
    assert "3 of 3" in html

    api = client.get("/api/bots").get_json()
    assert api["budget"] == {"cap": 3, "calls": 3, "reached": True}


def test_no_banner_under_budget_or_with_cap_off(app, client, monkeypatch):
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 3)
    with app.app_context():
        db.execute("INSERT INTO llm_usage_daily (day, calls) VALUES (?, ?)",
                   (budget.utc_day(), 1))
    assert "Daily AI budget reached" not in client.get("/").get_data(as_text=True)

    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 0)
    assert "Daily AI budget reached" not in client.get("/").get_data(as_text=True)
