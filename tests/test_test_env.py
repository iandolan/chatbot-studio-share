"""Guards on the test environment itself.

The budget tests deliberately blow through the cap, which trips the once-a-day
"budget reached" alert. When a developer's real .env was in play that alert was
a real email to a real inbox, sent on every run of the suite. conftest clears
the SMTP settings; these assertions make sure it keeps working, because the
failure mode is silent and lands in someone's inbox rather than in a test log.
"""
import smtplib

import pytest

from server import notify
from server.config import Config


def test_smtp_is_not_configured_under_test():
    assert not Config.smtp_configured(), (
        "Tests can send real email. conftest must clear SMTP_HOST/SMTP_FROM, and "
        "must set them to '' rather than popping them, because config.load_dotenv() "
        "refills anything missing from the real .env."
    )


def test_no_live_provider_keys_are_visible_to_tests():
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        assert not getattr(Config, name), f"{name} leaked into the test environment"


def test_sending_mail_during_tests_never_opens_a_connection(app, monkeypatch):
    """Belt and braces: if something bypasses the config guard, make it loud."""
    def boom(*a, **k):
        raise AssertionError("a test tried to open an SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    with app.app_context():
        # Queues to the outbox; the send is a no-op without SMTP configured.
        notify._send_plain_email("nobody@example.test", "subject", "body")
        from server import delivery
        delivery.run_due()


def test_alert_recipient_falls_back_to_the_owner_cc(monkeypatch):
    """A bot with no address of its own still reaches someone."""
    monkeypatch.setattr(Config, "ALERT_CC", "owner@example.test")
    assert notify.alert_recipient({"lead_alert_email": ""}) == "owner@example.test"
    assert notify.alert_recipient({"lead_alert_email": None}) == "owner@example.test"
    assert notify.alert_recipient({"lead_alert_email": "brand@example.test"}) == "brand@example.test"


def test_alert_recipient_is_empty_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(Config, "ALERT_CC", "")
    assert notify.alert_recipient({"lead_alert_email": ""}) == ""
