"""OpenRouter as a first-class provider: registry membership, request
construction (URL, auth + attribution headers, payload), streaming-parser
reuse, model-name defaulting, embeddings routing (OpenAI direct preferred,
OpenRouter fallback), and per-model request shaping (sampling params, effort).
All HTTP is mocked — no real provider calls."""
import json

import pytest

from server import llm
from server.config import Config

CHAT_RESPONSE = {
    "choices": [{"message": {"content": "Hello from OpenRouter"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}
EMBED_RESPONSE = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}

MESSAGES = [{"role": "user", "content": "Hi"}]


@pytest.fixture()
def real_mode(monkeypatch):
    """Leave mock AI mode without touching the network (HTTP is patched
    per-test) and keep the daily cap out of the way."""
    monkeypatch.setattr(Config, "MOCK_LLM", False)
    monkeypatch.setattr(Config, "GLOBAL_DAILY_LLM_CALLS", 0)


@pytest.fixture()
def http_calls(monkeypatch):
    """Record llm._http_post calls; return the canned chat response."""
    calls = []

    def fake_post(url, headers, payload):
        calls.append({"url": url, "headers": headers, "payload": payload})
        return EMBED_RESPONSE if url.endswith("/embeddings") else CHAT_RESPONSE

    monkeypatch.setattr(llm, "_http_post", fake_post)
    return calls


# ---------- registry + model defaulting ----------

def test_openrouter_registered():
    assert "openrouter" in llm.PROVIDERS
    assert llm.resolve_provider("openrouter") == "openrouter"
    assert llm.DEFAULT_MODELS["openrouter"] == "anthropic/claude-haiku-4.5"
    assert llm.MODEL_CHOICES["openrouter"]


def test_existing_provider_fallback_unchanged():
    # Unknown/blank providers still fall back exactly as before.
    assert llm.resolve_provider("") == Config.DEFAULT_PROVIDER
    assert llm.resolve_provider("not-a-provider") == "openai"


def test_model_defaults_to_prefixed_haiku(monkeypatch):
    monkeypatch.setattr(Config, "DEFAULT_PROVIDER", "openai")
    monkeypatch.setattr(Config, "DEFAULT_MODEL", "")
    assert llm.resolve_model("openrouter", "") == "anthropic/claude-haiku-4.5"


def test_env_default_model_wins_for_default_provider(monkeypatch):
    monkeypatch.setattr(Config, "DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setattr(Config, "DEFAULT_MODEL", "openai/gpt-4o-mini")
    assert llm.resolve_model("openrouter", "") == "openai/gpt-4o-mini"
    # A bot's explicit model always wins.
    assert llm.resolve_model("openrouter", "meta-llama/llama-3.3-70b-instruct") \
        == "meta-llama/llama-3.3-70b-instruct"


# ---------- request construction ----------

ANTHROPIC_RESPONSE = {
    "content": [{"type": "text", "text": "Hello from Anthropic"}],
    "usage": {"input_tokens": 12, "output_tokens": 5},
}


@pytest.mark.parametrize("model,expects_temperature", [
    ("claude-haiku-4-5-20251001", True),   # Haiku 4.5 still accepts sampling params
    ("claude-sonnet-5", False),            # 400 if temperature is sent
    ("claude-opus-4-8", False),
    ("claude-sonnet-5-5", False),          # 400 for any non-default value
    ("claude-haiku-5-5", False),
])
def test_anthropic_temperature_only_where_accepted(real_mode, monkeypatch, model,
                                                   expects_temperature):
    calls = []

    def fake_post(url, headers, payload):
        calls.append(payload)
        return ANTHROPIC_RESPONSE

    monkeypatch.setattr(llm, "_http_post", fake_post)
    llm.chat_completion("anthropic", "sk-ant-test", model, "sys", MESSAGES, temperature=0.3)
    assert ("temperature" in calls[0]) is expects_temperature


@pytest.mark.parametrize("model,expects_temperature", [
    ("anthropic/claude-haiku-4.5", True),
    ("anthropic/claude-sonnet-5.5", False),
    ("anthropic/claude-haiku-5.5", False),
    ("openai/gpt-4o-mini", True),
])
def test_openrouter_temperature_only_where_accepted(real_mode, http_calls, model,
                                                    expects_temperature):
    llm.chat_completion("openrouter", "sk-or-test", model, "sys", MESSAGES, temperature=0.3)
    assert ("temperature" in http_calls[0]["payload"]) is expects_temperature


def test_openrouter_chat_request(real_mode, http_calls, monkeypatch):
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://bots.example.com")
    result = llm.chat_completion("openrouter", "sk-or-test", "anthropic/claude-haiku-4.5",
                                 "You are helpful.", MESSAGES, temperature=0.3)
    assert len(http_calls) == 1
    call = http_calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-or-test"
    assert call["headers"]["X-Title"] == "Chatbot Studio"
    assert call["headers"]["HTTP-Referer"] == "https://bots.example.com"
    payload = call["payload"]
    assert payload["model"] == "anthropic/claude-haiku-4.5"
    assert payload["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert payload["messages"][1:] == MESSAGES
    assert payload["temperature"] == 0.3
    # Response parsed in the OpenAI shape.
    assert result == {"text": "Hello from OpenRouter", "tokens_in": 12, "tokens_out": 5}


def test_openrouter_referer_omitted_without_public_base_url(real_mode, http_calls, monkeypatch):
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "")
    llm.chat_completion("openrouter", "sk-or-test", "openai/gpt-4o-mini", "sys", MESSAGES)
    assert "HTTP-Referer" not in http_calls[0]["headers"]
    assert http_calls[0]["headers"]["X-Title"] == "Chatbot Studio"


def test_openai_chat_request_unchanged(real_mode, http_calls):
    llm.chat_completion("openai", "sk-test", "gpt-4o-mini", "sys", MESSAGES)
    call = http_calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"] == {"Authorization": "Bearer sk-test"}


# ---------- streaming (reuses the OpenAI SSE parser) ----------

def test_openrouter_streaming(real_mode, monkeypatch):
    calls = []

    def fake_stream(url, headers, payload):
        calls.append({"url": url, "headers": headers, "payload": payload})
        yield json.dumps({"choices": [{"delta": {"content": "Hel"}}]})
        yield json.dumps({"choices": [{"delta": {"content": "lo"}}]})
        yield json.dumps({"choices": [],
                          "usage": {"prompt_tokens": 7, "completion_tokens": 2}})

    monkeypatch.setattr(llm, "_http_post_stream", fake_stream)
    events = list(llm.chat_completion_stream(
        "openrouter", "sk-or-test", "anthropic/claude-haiku-4.5", "sys", MESSAGES))

    call = calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-or-test"
    assert call["payload"]["stream"] is True
    assert events == [("delta", "Hel"), ("delta", "lo"), ("usage", (7, 2))]


# ---------- embeddings: OpenAI direct preferred, OpenRouter fallback ----------

def test_embeddings_prefer_openai_when_both_keys_exist(real_mode, http_calls, monkeypatch):
    monkeypatch.setattr(Config, "DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "sk-openai-embed")
    vectors = llm.embed_texts(["hello world"])
    call = http_calls[0]
    assert call["url"] == "https://api.openai.com/v1/embeddings"
    assert call["headers"]["Authorization"] == "Bearer sk-openai-embed"
    assert call["payload"]["model"] == Config.EMBEDDING_MODEL
    assert vectors == [[0.1, 0.2, 0.3]]


def test_embeddings_fall_back_to_openrouter(real_mode, http_calls, monkeypatch):
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(Config, "EMBEDDING_MODEL", "text-embedding-3-small")
    vectors = llm.embed_texts(["hello world"])
    call = http_calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/embeddings"
    assert call["headers"]["Authorization"] == "Bearer sk-or-test"
    # OpenRouter needs the vendor-prefixed id for the same model.
    assert call["payload"]["model"] == "openai/text-embedding-3-small"
    assert vectors == [[0.1, 0.2, 0.3]]


def test_embeddings_error_names_both_keys(real_mode, monkeypatch):
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "")
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY or OPENAI_API_KEY"):
        llm.embed_texts(["hello"])


# ---------- model quirks: sampling params and default thinking ----------

@pytest.mark.parametrize("model,accepts", [
    ("anthropic/claude-haiku-4.5", True),
    ("anthropic/claude-sonnet-4.6", True),
    ("anthropic/claude-sonnet-5", False),
    ("anthropic/claude-opus-4.8", False),
    ("claude-opus-5", False),
    ("claude-sonnet-5-5", False),
    ("claude-haiku-5-5", False),
    ("anthropic/claude-haiku-5.5", False),
    ("openai/gpt-4o-mini", True),
])
def test_accepts_temperature_handles_both_id_styles(model, accepts):
    assert llm.accepts_temperature(model) is accepts


@pytest.mark.parametrize("model,thinks", [
    ("anthropic/claude-haiku-4.5", False),
    ("claude-opus-4-8", False),            # thinks only when asked to
    ("anthropic/claude-sonnet-5", True),
    ("claude-haiku-5-5", True),
    ("anthropic/claude-haiku-5.5", True),
])
def test_thinks_by_default_handles_both_id_styles(model, thinks):
    assert llm.thinks_by_default(model) is thinks


def test_openrouter_sonnet5_request_shape(real_mode, http_calls):
    llm.chat_completion("openrouter", "sk-or-test", "anthropic/claude-sonnet-5",
                        "sys", MESSAGES, temperature=0.3, max_tokens=1024)
    payload = http_calls[0]["payload"]
    assert "temperature" not in payload
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_tokens"] >= 2048


def test_openrouter_haiku_request_keeps_temperature_and_no_reasoning(real_mode, http_calls):
    llm.chat_completion("openrouter", "sk-or-test", "anthropic/claude-haiku-4.5",
                        "sys", MESSAGES, temperature=0.3, max_tokens=1024)
    payload = http_calls[0]["payload"]
    assert payload["temperature"] == 0.3
    assert "reasoning" not in payload
    assert payload["max_tokens"] == 1024


def test_anthropic_direct_sonnet5_asks_for_low_effort(real_mode, monkeypatch):
    calls = []

    def fake_post(url, headers, payload):
        calls.append(payload)
        return ANTHROPIC_RESPONSE

    monkeypatch.setattr(llm, "_http_post", fake_post)
    llm.chat_completion("anthropic", "sk-ant-test", "claude-sonnet-5", "sys", MESSAGES,
                        temperature=0.3, max_tokens=1024)
    assert calls[0]["output_config"] == {"effort": "low"}
    assert calls[0]["max_tokens"] >= 2048
    assert "temperature" not in calls[0]


def test_anthropic_direct_haiku55_asks_for_low_effort(real_mode, monkeypatch):
    calls = []

    def fake_post(url, headers, payload):
        calls.append(payload)
        return ANTHROPIC_RESPONSE

    monkeypatch.setattr(llm, "_http_post", fake_post)
    llm.chat_completion("anthropic", "sk-ant-test", "claude-haiku-5-5", "sys", MESSAGES,
                        temperature=0.3, max_tokens=1024)
    assert calls[0]["output_config"] == {"effort": "low"}
    assert calls[0]["max_tokens"] >= 2048
    assert "temperature" not in calls[0]


# ---------- embeddings: explicit EMBEDDING_PROVIDER pin ----------

def test_embedding_provider_pin_forces_openrouter_over_a_live_openai_key(
        real_mode, http_calls, monkeypatch):
    """The reason this exists: a *revoked but present* OpenAI key still wins
    under "auto" and 401s every call, which silently kills retrieval."""
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "sk-revoked-but-present")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "sk-or-live")
    monkeypatch.setattr(Config, "EMBEDDING_MODEL", "text-embedding-3-small")

    llm.embed_texts(["hello world"])

    call = http_calls[-1]
    assert call["url"] == "https://openrouter.ai/api/v1/embeddings"
    assert call["payload"]["model"] == "openai/text-embedding-3-small"
    assert "sk-revoked-but-present" not in str(call["headers"])


def test_embedding_provider_pin_openai(real_mode, http_calls, monkeypatch):
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "sk-or-live")

    llm.embed_texts(["hello world"])

    assert http_calls[-1]["url"] == "https://api.openai.com/v1/embeddings"


def test_embedding_provider_pin_without_its_key_is_a_clear_error(real_mode, monkeypatch):
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "sk-openai")

    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.embed_texts(["hello world"])
