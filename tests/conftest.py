"""Shared fixtures. Forces mock AI mode + a throwaway SQLite DB before the
server package (which reads env at import time) is imported."""
import os
import pathlib
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="chatbot-studio-tests-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["MOCK_LLM"] = "1"
os.environ["EMBEDDING_PROVIDER"] = "auto"  # tests pin this per-case
os.environ["ADMIN_PASSWORD"] = ""  # open dashboard: no auth in tests
# SMTP_* has to go too, not just the model keys. The budget tests set the cap to
# 3 and then exceed it on purpose, which trips the once-a-day "budget reached"
# alert. With a developer's real .env loaded that alert was a real email to a
# real inbox, sent every time anyone ran the suite.
#
# These are set to "" rather than popped. config.py calls load_dotenv() at
# import, which runs AFTER this file and refills anything missing from the real
# .env, so popping a key simply handed it back. load_dotenv does not override a
# name that is already present, and "" counts as present, so assigning empty is
# what actually sticks. Config.smtp_configured() needs SMTP_HOST and SMTP_FROM,
# so empty strings make every send a no-op.
for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
            "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
            "ALERT_CC", "BUDGET_ALERT_EMAIL"):
    os.environ[key] = ""

from server import create_app, db  # noqa: E402  (env must be set first)


@pytest.fixture(scope="session")
def app():
    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def bot_id(client):
    """A fresh bot per test."""
    resp = client.post("/api/bots", json={"name": f"Test bot {uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 201
    return resp.get_json()["bot"]["id"]


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


def seed_conversation(app, bot_id, turns, start=None):
    """Insert a conversation with alternating messages.

    turns: list of (user_text, reply) where reply is None or a dict:
      {"content": str, "grounded": 0|1|None, "action": dict|None,
       "role": "assistant"|"agent"}
    Returns the conversation id.
    """
    import json

    start = start or days_ago(0)
    with app.app_context():
        convo_id = db.execute(
            "INSERT INTO conversations (bot_id, session_id, page_url, started_at, "
            "last_message_at) VALUES (?,?,?,?,?)",
            (bot_id, uuid.uuid4().hex[:16], "https://example.com/", iso(start), iso(start)),
        )
        ts = start
        for user_text, reply in turns:
            db.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?,?,?,?)",
                (convo_id, "user", user_text, iso(ts)),
            )
            ts += timedelta(minutes=1)
            if reply is not None:
                db.execute(
                    "INSERT INTO messages (conversation_id, role, content, action, "
                    "grounded, created_at) VALUES (?,?,?,?,?,?)",
                    (convo_id, reply.get("role", "assistant"), reply["content"],
                     json.dumps(reply["action"]) if reply.get("action") else "",
                     reply.get("grounded"), iso(ts)),
                )
                ts += timedelta(minutes=1)
        db.execute("UPDATE conversations SET last_message_at = ? WHERE id = ?",
                   (iso(ts), convo_id))
    return convo_id


GOOD_REPLY = {
    "content": "We offer a full refund within 30 days of purchase — just email "
               "support@example.com with your order number and we'll take care of it.",
    "grounded": 1,
}
WEAK_REPLY = {
    "content": "Sorry, I don't know, but the team can help if you leave your details.",
    "grounded": 0,
}
