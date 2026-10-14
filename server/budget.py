"""Global daily LLM-call budget — runaway-spend protection for public bots.

One SQLite row per UTC day (`llm_usage_daily`, day is the primary key), so the
per-message check is a single indexed upsert. The cap covers real chat
completions across ALL bots; embeddings and mock mode are never counted.
GLOBAL_DAILY_LLM_CALLS <= 0 (or unset) disables the cap entirely.
"""
import logging
from datetime import datetime, timezone

from . import db
from .config import Config

log = logging.getLogger(__name__)


def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def try_register_call():
    """Count one chat completion against today's global budget.

    Returns True when the call may proceed. Atomic: the upsert only
    increments while under the cap, so exactly GLOBAL_DAILY_LLM_CALLS calls
    are allowed per UTC day even across threads. At midnight UTC the day key
    changes and the counter starts fresh.
    """
    cap = Config.GLOBAL_DAILY_LLM_CALLS
    if cap <= 0:
        return True
    changed = db.execute_rowcount(
        "INSERT INTO llm_usage_daily (day, calls) VALUES (?, 1) "
        "ON CONFLICT(day) DO UPDATE SET calls = calls + 1 WHERE calls < ?",
        (utc_day(), cap),
    )
    if changed:
        return True
    log.warning(
        "GLOBAL DAILY LLM BUDGET REACHED (%d calls, UTC day %s) — refusing chat "
        "completion; bots serve fallback replies until midnight UTC.",
        cap, utc_day(),
    )
    _alert_once(cap)
    return False


def _alert_once(cap):
    """Email the owner the first time the budget trips each UTC day.

    Same atomic-dedupe pattern as the counter (INSERT OR IGNORE on the day's
    primary key), and best-effort throughout: alerting can never break a chat
    request, and the email itself already sends on notify's error-swallowing
    path."""
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS llm_cap_alerts (day TEXT PRIMARY KEY)")
        if not db.execute_rowcount(
                "INSERT OR IGNORE INTO llm_cap_alerts (day) VALUES (?)",
                (utc_day(),)):
            return
        if not Config.smtp_configured():
            return
        from .notify import _send_plain_email
        _send_plain_email(
            Config.BUDGET_ALERT_EMAIL,
            f"LLM daily budget reached ({cap} calls) - Chatbot Studio",
            f"GLOBAL_DAILY_LLM_CALLS of {cap} was reached for today (UTC).\n"
            "All bots serve their fallback reply until midnight UTC.\n\n"
            "To raise it: edit GLOBAL_DAILY_LLM_CALLS in "
            "the app's .env on the server and run "
            "'docker compose up -d'.\n\n- Chatbot Studio",
        )
    except Exception as exc:
        log.warning("budget alert email skipped: %s", exc)


def status():
    """{cap, calls, reached} for today — used by the dashboard banner."""
    cap = Config.GLOBAL_DAILY_LLM_CALLS
    calls = 0
    if cap > 0:
        row = db.query("SELECT calls FROM llm_usage_daily WHERE day = ?",
                       (utc_day(),), one=True)
        calls = row["calls"] if row else 0
    return {"cap": cap, "calls": calls, "reached": cap > 0 and calls >= cap}
