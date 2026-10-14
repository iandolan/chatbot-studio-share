"""Off-topic (troll/nuisance) messages must never enter the answer-gap queue.

The bot still replies politely, and the transcript is still stored — only the
owner's training/notification queue is protected.
"""
import pytest

from server import chat


class _Bot(dict):
    """dict that also supports row-style access used by chat.py."""


@pytest.fixture()
def bot():
    return _Bot(id=99, name="Test Bot", fallback_message="I don't have that yet.")


def _prep(grounded, message="how far is santa"):
    return {"user_message": message, "grounded": grounded, "best_score": 0.05}


def test_off_topic_action_blocks_gap_logging(bot, monkeypatch):
    inserted = []
    monkeypatch.setattr(chat.db, "execute", lambda *a, **k: inserted.append(a))
    monkeypatch.setattr(chat.db, "query", lambda *a, **k: None)

    chat._finalize_gaps(bot, _prep(grounded=0), 1, "This chat is about our services.",
                        {"type": "off_topic"})
    assert inserted == [], "off-topic message must not create a gap"


def test_real_unanswered_question_still_logs_a_gap(bot, monkeypatch):
    inserted = []
    monkeypatch.setattr(chat.db, "execute", lambda *a, **k: inserted.append(a))
    monkeypatch.setattr(chat.db, "query", lambda *a, **k: None)

    chat._finalize_gaps(bot, _prep(grounded=0, message="do you offer weekend support?"),
                        1, "I don't have that yet.", None)
    assert len(inserted) == 1, "genuine unanswered question must still be logged"
    assert "do you offer weekend support?" in inserted[0][1]


def test_off_topic_is_hidden_from_the_widget():
    assert chat._public_action({"type": "off_topic"}) is None
    assert chat._public_action({"type": "lead_form"}) == {"type": "lead_form"}
    assert chat._public_action(None) is None


def test_off_topic_action_validates_without_a_redirect_allowlist():
    assert chat._validate_action({"type": "off_topic"}, [], True) == {"type": "off_topic"}


def test_off_topic_parsed_from_a_trailing_action_line():
    reply, action = chat._parse_reply(
        'That one is outside what I can help with.\nACTION: {"type":"off_topic"}', [], True)
    assert action == {"type": "off_topic"}
    assert "ACTION" not in reply


# --- short questions are not evidence of a gap -------------------------------

def test_one_word_question_with_a_weak_score_is_not_logged_on_score_alone(bot, monkeypatch):
    """The one-word "Pricing" case.

    It scored 0.247, under the gap threshold, purely because one word embeds
    badly against any chunk. The bot answered it well. Logging it taught the
    owner that the alerts were noise.
    """
    inserted = []
    started = []
    monkeypatch.setattr(chat.db, "execute", lambda *a, **k: inserted.append(a))
    monkeypatch.setattr(chat.db, "query", lambda *a, **k: None)
    monkeypatch.setattr(chat.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: started.append(k)})())

    chat._finalize_gaps(bot, _prep(grounded=0, message="Pricing"), 1,
                        "Pricing depends on scope, so we work it out on the call.", None)

    assert inserted == [], "a one-word question must not be logged on score alone"
    assert started, "it should fall through to the reply-vs-fallback check instead"


def test_a_short_question_the_bot_really_failed_is_still_caught(bot, monkeypatch):
    """Falling through is not the same as being dropped: the fallback check
    still logs it when the bot actually gave up."""
    inserted = []
    monkeypatch.setattr(chat.db, "_connect", lambda: _FakeConn(inserted))
    monkeypatch.setattr(chat, "embed_texts", lambda texts: [[1.0, 0.0]] * len(texts))
    monkeypatch.setattr(chat, "FALLBACK_SIM_THRESHOLD", 0.5)

    chat._maybe_log_fallback_gap(dict(bot), 1, "Pricing", bot["fallback_message"], 0.247)
    assert len(inserted) == 1
    assert inserted[0][1][2] == "Pricing"


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("INSERT"):
            self.sink.append((sql, params))
        return type("C", (), {"fetchone": lambda s: None})()

    def commit(self):
        pass

    def close(self):
        pass
