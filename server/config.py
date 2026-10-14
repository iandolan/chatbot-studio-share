import os
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _flag(name):
    return _env(name).lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = _env("SECRET_KEY") or "dev-secret-change-me"
    PRODUCTION = _flag("PRODUCTION")
    TRUSTED_PROXY_HOPS = int(_env("TRUSTED_PROXY_HOPS", "0"))
    ACCESS_TOKEN_TTL = int(_env("ACCESS_TOKEN_TTL", "43200"))
    ADMIN_PASSWORD = _env("ADMIN_PASSWORD")  # empty = open dashboard (dev mode)

    OPENAI_API_KEY = _env("OPENAI_API_KEY")
    ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
    GEMINI_API_KEY = _env("GEMINI_API_KEY")
    OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")

    DEFAULT_PROVIDER = _env("DEFAULT_PROVIDER", "openrouter")
    DEFAULT_MODEL = _env("DEFAULT_MODEL")  # empty = provider's built-in default
    EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "text-embedding-3-small")
    # Which provider serves embeddings: "openrouter", "openai", or "auto"
    # (auto = direct OpenAI key if present, else OpenRouter). Pin it so a
    # stale or revoked key on the other provider cannot silently take over.
    EMBEDDING_PROVIDER = _env("EMBEDDING_PROVIDER", "auto").strip().lower()

    DATABASE_PATH = _env("DATABASE_PATH") or str(BASE_DIR / "data" / "chatbot_studio.db")
    PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL")  # e.g. https://bots.example.com (used in embed snippets)

    # Mock AI mode: no external calls, deterministic embeddings + canned chat.
    # Auto-enabled when no provider key is configured, so the app works out of the box.
    MOCK_LLM = _flag("MOCK_LLM") or not (
        _env("OPENAI_API_KEY") or _env("ANTHROPIC_API_KEY") or _env("GEMINI_API_KEY")
        or _env("OPENROUTER_API_KEY")
    )

    # Global daily LLM-call cap across ALL bots (runaway-spend protection).
    # 0 / unset = no cap (previous behavior). Counted per UTC day; chat
    # completions only — embeddings and mock mode are never counted.
    try:
        GLOBAL_DAILY_LLM_CALLS = int(_env("GLOBAL_DAILY_LLM_CALLS", "0") or 0)
    except ValueError:
        GLOBAL_DAILY_LLM_CALLS = 0

    # Where the once-per-day budget-reached alert goes (needs SMTP configured).
    BUDGET_ALERT_EMAIL = _env("BUDGET_ALERT_EMAIL", "")

    # Always CC'd on every outbound alert (leads, handoffs, gap digests, budget,
    # health). Per-bot alert addresses go to the brand that owns the bot, and a
    # brand mailbox nobody watches is how a handoff request sat unanswered for
    # 13 days. The owner's primary inbox is the backstop. Blank disables it, and
    # an address equal to the To is dropped rather than duplicated.
    ALERT_CC = _env("ALERT_CC", "")

    # Optional SMTP for lead email alerts
    WEBHOOK_SIGNING_SECRET = _env("WEBHOOK_SIGNING_SECRET")
    SMTP_HOST = _env("SMTP_HOST")
    SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
    SMTP_USER = _env("SMTP_USER")
    SMTP_PASS = _env("SMTP_PASS")
    SMTP_FROM = _env("SMTP_FROM") or _env("SMTP_USER")

    CRAWLER_ALLOW_PRIVATE = _flag("CRAWLER_ALLOW_PRIVATE")

    # Hostnames the crawler may fetch even when they resolve to private IPs —
    # for sites hosted on this same box behind a hairpin-NAT/extra_hosts
    # mapping. Comma-separated, exact hostname match.
    CRAWLER_PRIVATE_ALLOWLIST = frozenset(
        h.strip().lower() for h in _env("CRAWLER_PRIVATE_ALLOWLIST").split(",") if h.strip()
    )

    @classmethod
    def platform_key(cls, provider):
        return {
            "openai": cls.OPENAI_API_KEY,
            "anthropic": cls.ANTHROPIC_API_KEY,
            "gemini": cls.GEMINI_API_KEY,
            "openrouter": cls.OPENROUTER_API_KEY,
        }.get(provider, "")

    @classmethod
    def smtp_configured(cls):
        return bool(cls.SMTP_HOST and cls.SMTP_FROM)
