"""Provider-agnostic LLM layer.

chat_completion() and embed_texts() are the only entry points the rest of the
app uses. Adding a provider means adding one _chat_* function here.
"""
import hashlib
import json
import math
import re

import requests

from .config import Config

PROVIDERS = ("openai", "anthropic", "gemini", "openrouter")

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-2.0-flash",
    "openrouter": "anthropic/claude-haiku-4.5",
}

# Offered in the dashboard datalist (free text is still allowed). The OpenRouter
# ids were checked against https://openrouter.ai/api/v1/models; the Anthropic ids
# are the current direct-API aliases. Cheapest first within each provider.
MODEL_CHOICES = {
    "openai": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1", "gpt-4o"],
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-sonnet-5",
                  "claude-opus-4-8", "claude-opus-5"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "openrouter": ["anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-4.6",
                   "anthropic/claude-sonnet-5", "anthropic/claude-opus-4.8",
                   "anthropic/claude-opus-5", "openai/gpt-4.1-mini", "openai/gpt-4o-mini",
                   "google/gemini-2.5-flash", "meta-llama/llama-3.3-70b-instruct"],
}

# Claude families from Opus 4.7 onward (Opus 4.7/4.8, Opus 5, Sonnet 5, Fable and
# Mythos) reject sampling parameters: the direct API returns a 400 when
# `temperature` is sent, and OpenRouter silently drops it. Sonnet 5.5 and Haiku 5.5
# return the same 400 for any non-default value. Haiku 4.5 and the 4.6 family still
# accept it. OpenRouter names the same models `anthropic/claude-...` with dots
# instead of dashes, so every check normalises the id first.
_NO_SAMPLING_PREFIXES = ("claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
                         "claude-sonnet-5", "claude-haiku-5", "claude-fable", "claude-mythos")

# Models that run adaptive thinking whenever `thinking` is omitted. A chat widget
# wants low latency and predictable cost, so we ask for low effort and leave room
# in max_tokens, which caps thinking and the visible reply together.
_THINKS_BY_DEFAULT_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5",
                               "claude-fable", "claude-mythos")
_THINKING_MIN_MAX_TOKENS = 2048


def _claude_family(model):
    """'anthropic/claude-sonnet-4.5' and 'claude-sonnet-4-5' both -> 'claude-sonnet-4-5'."""
    return (model or "").split("/", 1)[-1].replace(".", "-").lower()


def accepts_temperature(model):
    return not _claude_family(model).startswith(_NO_SAMPLING_PREFIXES)


def thinks_by_default(model):
    return _claude_family(model).startswith(_THINKS_BY_DEFAULT_PREFIXES)


def _anthropic_payload(model, system, messages, temperature, max_tokens):
    payload = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if accepts_temperature(model):
        payload["temperature"] = min(temperature, 1.0)
    if thinks_by_default(model):
        payload["max_tokens"] = max(max_tokens, _THINKING_MIN_MAX_TOKENS)
        payload["output_config"] = {"effort": "low"}
    return payload


def _openai_compat_payload(model, system, messages, temperature, max_tokens):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": max_tokens,
    }
    if accepts_temperature(model):
        payload["temperature"] = temperature
    if thinks_by_default(model):
        # OpenRouter's unified reasoning parameter (verified accepted by
        # anthropic/claude-sonnet-5). Only Claude ids match, so OpenAI-direct
        # requests never carry it.
        payload["max_tokens"] = max(max_tokens, _THINKING_MIN_MAX_TOKENS)
        payload["reasoning"] = {"effort": "low"}
    return payload


OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_EMBED_URL = "https://openrouter.ai/api/v1/embeddings"

_TIMEOUT = (10, 60)


class LLMError(Exception):
    pass


class LLMBudgetError(LLMError):
    """Raised when the global daily LLM-call cap is reached. Subclasses
    LLMError so every caller degrades exactly like a provider failure."""


def _openrouter_headers(api_key):
    """OpenRouter auth + optional attribution headers."""
    headers = {"Authorization": f"Bearer {api_key}", "X-Title": "Chatbot Studio"}
    if Config.PUBLIC_BASE_URL:
        headers["HTTP-Referer"] = Config.PUBLIC_BASE_URL
    return headers


def resolve_provider(bot_provider):
    provider = (bot_provider or "").strip() or Config.DEFAULT_PROVIDER
    return provider if provider in PROVIDERS else "openai"


def resolve_model(provider, bot_model):
    model = (bot_model or "").strip()
    if model:
        return model
    if provider == Config.DEFAULT_PROVIDER and Config.DEFAULT_MODEL:
        return Config.DEFAULT_MODEL
    return DEFAULT_MODELS[provider]


def _raise_for_status(resp):
    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            body = resp.json()
            detail = (
                body.get("error", {}).get("message")
                or body.get("error", {}).get("status")
                or detail
            )
        except ValueError:
            pass
        raise LLMError(f"Provider returned {resp.status_code}: {detail}")


def _http_post(url, headers, payload):
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise LLMError(f"Network error calling provider: {exc}") from exc
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError as exc:
        raise LLMError("Provider returned a non-JSON response") from exc


def _http_post_stream(url, headers, payload):
    """POST with stream=True; yields SSE `data:` payload strings."""
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=(10, 120), stream=True)
    except requests.RequestException as exc:
        raise LLMError(f"Network error calling provider: {exc}") from exc
    _raise_for_status(resp)
    try:
        # Decode bytes as UTF-8 ourselves: SSE responses rarely declare a
        # charset, and requests would otherwise fall back to Latin-1, mangling
        # any non-ASCII the model streams (em dashes, accents, emoji).
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    yield data
    except requests.RequestException as exc:
        raise LLMError(f"Stream dropped: {exc}") from exc
    finally:
        resp.close()


# ---------- chat ----------

def chat_completion(provider, api_key, model, system, messages, temperature=0.4,
                    max_tokens=1024, json_mode=False):
    """messages: [{role: user|assistant, content: str}]. Returns
    {text, tokens_in, tokens_out}."""
    if Config.MOCK_LLM:
        return _chat_mock(system, messages)
    if not api_key:
        raise LLMError(
            f"No API key configured for provider '{provider}'. Set it in .env "
            "or give this bot its own key in the API key tab."
        )
    _register_llm_call()
    if provider == "openai":
        return _chat_openai(api_key, model, system, messages, temperature, max_tokens, json_mode)
    if provider == "openrouter":
        return _chat_openrouter(api_key, model, system, messages, temperature, max_tokens, json_mode)
    if provider == "anthropic":
        return _chat_anthropic(api_key, model, system, messages, temperature, max_tokens)
    if provider == "gemini":
        return _chat_gemini(api_key, model, system, messages, temperature, max_tokens, json_mode)
    raise LLMError(f"Unknown provider '{provider}'")


def _register_llm_call():
    """Count this chat completion against the global daily cap; raise
    LLMBudgetError (an LLMError, so callers degrade gracefully) when spent."""
    from . import budget  # local import: budget uses db, keep llm import-light
    if not budget.try_register_call():
        raise LLMBudgetError(
            f"Global daily LLM budget reached ({Config.GLOBAL_DAILY_LLM_CALLS} "
            "calls today, UTC). Bots reply with their fallback message until "
            "midnight UTC. Raise GLOBAL_DAILY_LLM_CALLS in .env to lift the cap."
        )


def _chat_openai_compat(url, headers, model, system, messages, temperature,
                        max_tokens, json_mode, label):
    """OpenAI chat-completions request/response — shared by OpenAI and
    OpenRouter (OpenRouter is OpenAI-compatible; only URL/headers differ)."""
    payload = _openai_compat_payload(model, system, messages, temperature, max_tokens)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data = _http_post(url, headers, payload)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected {label} response shape") from exc
    usage = data.get("usage", {}) or {}
    return {
        "text": text,
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }


def _chat_openai(api_key, model, system, messages, temperature, max_tokens, json_mode):
    return _chat_openai_compat(
        OPENAI_CHAT_URL, {"Authorization": f"Bearer {api_key}"},
        model, system, messages, temperature, max_tokens, json_mode, "OpenAI")


def _chat_openrouter(api_key, model, system, messages, temperature, max_tokens, json_mode):
    return _chat_openai_compat(
        OPENROUTER_CHAT_URL, _openrouter_headers(api_key),
        model, system, messages, temperature, max_tokens, json_mode, "OpenRouter")


def _chat_anthropic(api_key, model, system, messages, temperature, max_tokens):
    payload = _anthropic_payload(model, system, messages, temperature, max_tokens)
    data = _http_post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        payload,
    )
    try:
        text = "".join(block.get("text", "") for block in data["content"])
    except (KeyError, TypeError) as exc:
        raise LLMError("Unexpected Anthropic response shape") from exc
    usage = data.get("usage", {})
    return {
        "text": text,
        "tokens_in": usage.get("input_tokens", 0),
        "tokens_out": usage.get("output_tokens", 0),
    }


def _chat_gemini(api_key, model, system, messages, temperature, max_tokens, json_mode):
    contents = [
        {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    data = _http_post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": api_key},
        payload,
    )
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("Unexpected Gemini response shape") from exc
    usage = data.get("usageMetadata", {})
    return {
        "text": text,
        "tokens_in": usage.get("promptTokenCount", 0),
        "tokens_out": usage.get("candidatesTokenCount", 0),
    }


# ---------- streaming chat ----------

def chat_completion_stream(provider, api_key, model, system, messages,
                           temperature=0.4, max_tokens=1024):
    """Generator yielding ("delta", text) tuples, then a final
    ("usage", (tokens_in, tokens_out)). Raises LLMError before the first
    delta if the request itself fails."""
    if Config.MOCK_LLM:
        yield from _mock_stream(system, messages)
        return
    if not api_key:
        raise LLMError(
            f"No API key configured for provider '{provider}'. Set it in .env "
            "or give this bot its own key in the API key tab."
        )
    _register_llm_call()
    if provider == "openai":
        yield from _stream_openai(api_key, model, system, messages, temperature, max_tokens)
    elif provider == "openrouter":
        yield from _stream_openrouter(api_key, model, system, messages, temperature, max_tokens)
    elif provider == "anthropic":
        yield from _stream_anthropic(api_key, model, system, messages, temperature, max_tokens)
    elif provider == "gemini":
        yield from _stream_gemini(api_key, model, system, messages, temperature, max_tokens)
    else:
        raise LLMError(f"Unknown provider '{provider}'")


def _stream_openai(api_key, model, system, messages, temperature, max_tokens):
    yield from _stream_openai_compat(
        OPENAI_CHAT_URL, {"Authorization": f"Bearer {api_key}"},
        model, system, messages, temperature, max_tokens)


def _stream_openrouter(api_key, model, system, messages, temperature, max_tokens):
    # OpenRouter streams SSE in the OpenAI chat-completions format, so the
    # OpenAI streaming parser is reused as-is (only URL/headers differ).
    yield from _stream_openai_compat(
        OPENROUTER_CHAT_URL, _openrouter_headers(api_key),
        model, system, messages, temperature, max_tokens)


def _stream_openai_compat(url, headers, model, system, messages, temperature, max_tokens):
    payload = _openai_compat_payload(model, system, messages, temperature, max_tokens)
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    tokens_in = tokens_out = 0
    for data in _http_post_stream(url, headers, payload):
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        usage = chunk.get("usage")
        if usage:
            tokens_in = usage.get("prompt_tokens", 0)
            tokens_out = usage.get("completion_tokens", 0)
        choices = chunk.get("choices") or []
        if choices:
            delta = (choices[0].get("delta") or {}).get("content")
            if delta:
                yield ("delta", delta)
    yield ("usage", (tokens_in, tokens_out))


def _stream_anthropic(api_key, model, system, messages, temperature, max_tokens):
    payload = _anthropic_payload(model, system, messages, temperature, max_tokens)
    payload["stream"] = True
    tokens_in = tokens_out = 0
    for data in _http_post_stream(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, payload):
        try:
            event = json.loads(data)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "message_start":
            tokens_in = event.get("message", {}).get("usage", {}).get("input_tokens", 0)
        elif kind == "content_block_delta":
            text = event.get("delta", {}).get("text")
            if text:
                yield ("delta", text)
        elif kind == "message_delta":
            tokens_out = event.get("usage", {}).get("output_tokens", tokens_out)
        elif kind == "error":
            raise LLMError(f"Anthropic stream error: {event.get('error', {}).get('message', '')}")
    yield ("usage", (tokens_in, tokens_out))


def _stream_gemini(api_key, model, system, messages, temperature, max_tokens):
    contents = [
        {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    tokens_in = tokens_out = 0
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:streamGenerateContent?alt=sse")
    for data in _http_post_stream(url, {"x-goog-api-key": api_key}, payload):
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        usage = chunk.get("usageMetadata")
        if usage:
            tokens_in = usage.get("promptTokenCount", tokens_in)
            tokens_out = usage.get("candidatesTokenCount", tokens_out)
        candidates = chunk.get("candidates") or []
        if candidates:
            for part in (candidates[0].get("content") or {}).get("parts", []):
                if part.get("text"):
                    yield ("delta", part["text"])
    yield ("usage", (tokens_in, tokens_out))


def _mock_stream(system, messages):
    result = _chat_mock(system, messages)
    words = result["text"].split(" ")
    for start in range(0, len(words), 4):
        yield ("delta", " ".join(words[start:start + 4]) + (" " if start + 4 < len(words) else ""))
    yield ("usage", (0, 0))


# ---------- embeddings (OpenAI direct, OpenRouter, or deterministic mock) ----------

EMBED_DIM_MOCK = 256


def _embedding_route():
    """(url, headers, model) for embeddings.

    EMBEDDING_PROVIDER pins this: "openrouter", "openai", or "auto" (default:
    direct OpenAI key if set, else OpenRouter). Pinning matters because a key
    that is present but revoked still wins under "auto" and fails every call.
    Both routes serve the same upstream model and return interchangeable
    vectors (verified: identical text scores 1.0000 across the two), so the
    provider can be switched without re-embedding anything already stored.
    """
    provider = Config.EMBEDDING_PROVIDER

    def _openai():
        if not Config.OPENAI_API_KEY:
            raise LLMError("EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set.")
        return ("https://api.openai.com/v1/embeddings",
                {"Authorization": f"Bearer {Config.OPENAI_API_KEY}"},
                Config.EMBEDDING_MODEL)

    def _openrouter():
        if not Config.OPENROUTER_API_KEY:
            raise LLMError("EMBEDDING_PROVIDER=openrouter but OPENROUTER_API_KEY is not set.")
        model = Config.EMBEDDING_MODEL
        if "/" not in model:
            model = f"openai/{model}"
        return (OPENROUTER_EMBED_URL, _openrouter_headers(Config.OPENROUTER_API_KEY), model)

    if provider == "openrouter":
        return _openrouter()
    if provider == "openai":
        return _openai()
    if Config.OPENAI_API_KEY:
        return _openai()
    if Config.OPENROUTER_API_KEY:
        return _openrouter()
    raise LLMError(
        "Embeddings need OPENROUTER_API_KEY or OPENAI_API_KEY in .env (training and "
        "retrieval embed text even when a bot chats through another provider)."
    )


def embed_texts(texts):
    """Returns a list of float vectors, one per input text."""
    if Config.MOCK_LLM:
        return [_mock_embedding(t) for t in texts]
    url, headers, model = _embedding_route()
    vectors = []
    for start in range(0, len(texts), 96):
        batch = texts[start:start + 96]
        data = _http_post(url, headers, {"model": model, "input": batch})
        try:
            items = sorted(data["data"], key=lambda d: d["index"])
            vectors.extend(item["embedding"] for item in items)
        except (KeyError, TypeError) as exc:
            raise LLMError("Unexpected embeddings response shape") from exc
    return vectors


_token_re = re.compile(r"[a-z0-9']+")


def _mock_embedding(text):
    """Hashed bag-of-words vector — similar texts genuinely score higher, so
    retrieval, gap logging and the demo all behave sensibly without API keys."""
    vec = [0.0] * EMBED_DIM_MOCK
    for token in _token_re.findall(text.lower()):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % EMBED_DIM_MOCK
        sign = 1.0 if digest[4] % 2 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _chat_mock(system, messages):
    """Deterministic assistant used in mock mode. Speaks the same trailing
    ACTION-line protocol as the real prompt so the whole pipeline works."""
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    lower = last_user.lower()

    knowledge = ""
    marker = "# KNOWLEDGE"
    if marker in system:
        knowledge = system.split(marker, 1)[1][:4000]

    redirects = []
    for match in re.finditer(r'\{"id": (\d+), "title": "(.*?)", "url": "(.*?)"', system):
        redirects.append({"title": match.group(2), "url": match.group(3)})

    action = None
    if "# HUMAN HANDOFF" in system and any(
        w in lower for w in ("human", "real person", "agent", "someone from", "talk to a person")
    ):
        action = {"type": "handoff"}
    if action is None:
        for r in redirects:
            title_tokens = [t for t in _token_re.findall(r["title"].lower()) if len(t) > 3]
            if any(t in lower for t in title_tokens):
                action = {"type": "redirect", "url": r["url"], "auto": False, "label": r["title"]}
                break
    if action is None and "# LEAD CAPTURE" in system and any(
        w in lower for w in ("contact", "quote", "demo", "call me", "reach out", "sign up", "pricing")
    ):
        action = {"type": "lead_form"}

    snippet = ""
    for token in _token_re.findall(lower):
        if len(token) > 3 and token in knowledge.lower():
            i = knowledge.lower().index(token)
            start = max(0, knowledge.rfind("\n", 0, i) + 1)
            end = knowledge.find("\n", i)
            snippet = knowledge[start:end if end != -1 else len(knowledge)].strip()[:300]
            break

    if action and action["type"] == "handoff":
        reply = "Of course — I've flagged this conversation for the team, and they'll pick it up right here."
    elif snippet:
        reply = f"{snippet}\n\n_(Mock AI mode — answered from your training data. Add an API key in .env for real answers.)_"
    else:
        reply = ("I'm running in **mock AI mode** (no API key configured). I echo your training "
                 "data when a question matches it — try asking about something you've added in "
                 "the Training tab.")
    text = reply if action is None else f"{reply}\nACTION: {json.dumps(action)}"
    return {"text": text, "tokens_in": 0, "tokens_out": 0}


def extract_json(text):
    """Best-effort: parse the first balanced JSON object found in text."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None
