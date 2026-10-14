"""Lead notifications: webhook POST and optional SMTP email, saved to the delivery outbox."""
import json
import logging


from .config import Config

log = logging.getLogger(__name__)


def alert_recipient(bot):
    """Who an alert for this bot is addressed to.

    The bot's own address when it has one, so the brand that owns the bot gets
    its own mail, otherwise the owner's. Returns "" only when neither is set,
    which is the one case worth dropping. Config.ALERT_CC is CC'd on top of this
    by the delivery layer, so the owner sees everything either way.
    """
    return (bot["lead_alert_email"] or "").strip() or (Config.ALERT_CC or "").strip()


def notify_lead(bot, lead_data, conversation_id, page_url, lead_id=None):
    payload = {
        "event": "lead.created",
        "lead_id": lead_id,
        "bot": {"id": bot["id"], "name": bot["name"]},
        "lead": lead_data,
        "conversation_id": conversation_id,
        "page_url": page_url,
    }
    webhook_url = (bot["lead_webhook_url"] or "").strip()
    if webhook_url.startswith(("http://", "https://")):
        _post_webhook(webhook_url, payload)
    alert_email = alert_recipient(bot)
    if alert_email and Config.smtp_configured():
        _send_email(alert_email, bot["name"], lead_data, page_url, bot["id"], lead_id)


def notify_handoff(bot, conversation_id):
    """Fires when a visitor asks for a human: webhook + optional email alert."""
    from. import db  # late import to avoid cycles
    convo = db.query("SELECT page_url FROM conversations WHERE id = ?",
                     (conversation_id,), one=True)
    page_url = convo["page_url"] if convo else ""
    last_msgs = db.query(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 6",
        (conversation_id,),
    )
    transcript_tail = [
        {"role": m["role"], "content": m["content"][:300]} for m in reversed(last_msgs)
    ]
    payload = {
        "event": "handoff.requested",
        "bot": {"id": bot["id"], "name": bot["name"]},
        "conversation_id": conversation_id,
        "page_url": page_url,
        "recent_messages": transcript_tail,
    }
    webhook_url = (bot["lead_webhook_url"] or "").strip()
    if webhook_url.startswith(("http://", "https://")):
        _post_webhook(webhook_url, payload)
    alert_email = alert_recipient(bot)
    if alert_email and Config.smtp_configured():
        lines = "\n".join(f"{m['role']}: {m['content']}" for m in transcript_tail)
        _send_plain_email(alert_email, f"Visitor requested a human. {bot['name']}",
                  "A visitor asked to talk to a real person.\n\n"
                  f"Page: {page_url}\nConversation #{conversation_id}\n\n"
                  f"Recent messages:\n{lines}\n\n"
                  "Reply from the bot's Conversations tab. your message appears "
                  "live in their chat widget.\n\nChatbot Studio",
            bot_id=bot["id"],
        )


def notify_gap_digest(bot, gaps):
    """One batched 'your bot needs answers' notification, sent over the bot's
    existing channels (lead webhook and/or alert email). Returns True when at
    least one channel was queued, so callers know to mark gaps notified."""
    questions = [str(g["question"])[:200] for g in gaps]
    answer_url = f"{(Config.PUBLIC_BASE_URL or 'http://127.0.0.1:5090').rstrip('/')}/bots/{bot['id']}#training"
    queued = False

    webhook_url = (bot["lead_webhook_url"] or "").strip()
    if webhook_url.startswith(("http://", "https://")):
        payload = {
            "event": "gaps.pending",
            "bot": {"id": bot["id"], "name": bot["name"]},
            "count": len(questions),
            "questions": questions,
            "answer_at": answer_url,
        }
        _post_webhook(webhook_url, payload)
        queued = True

    alert_email = alert_recipient(bot)
    if alert_email and Config.smtp_configured():
        lines = "\n".join(f"  - {q}" for q in questions)
        _send_plain_email(alert_email,
                  f"{bot['name']}: {len(questions)} visitor question(s) need an answer",
                  "Visitors asked things the bot couldn't answer from its training:\n\n"
                  f"{lines}\n\n"
                  f"Answer them here (Training tab, Answer gaps panel):\n{answer_url}\n\n"
                  "Each answer you save becomes permanent training immediately.\n\nChatbot Studio",
            bot_id=bot["id"],
        )
        queued = True

    return queued


def _post_webhook(url, payload):
    from .delivery import enqueue
    return enqueue("webhook", url, payload, payload.get("bot", {}).get("id"))


def _send_email(to_addr, bot_name, lead_data, page_url, bot_id=None, lead_id=None):
    lines = [f"{k}: {v}" for k, v in lead_data.items()]
    if page_url:
        lines.append(f"Captured on: {page_url}")
    _send_plain_email(to_addr, f"New lead from {bot_name}",
                      "A visitor left their details:\n\n" + "\n".join(lines)
                      + "\n\nChatbot Studio", bot_id=bot_id, lead_id=lead_id)


def _send_plain_email(to_addr, subject, body, bot_id=None, lead_id=None):
    from .delivery import enqueue
    return enqueue("email", to_addr, {"subject": subject, "body": body, "lead_id": lead_id}, bot_id)


def payload_preview():
    """Shown in the dashboard so users know what their webhook will receive."""
    return json.dumps({
        "event": "lead.created",
        "bot": {"id": 1, "name": "Support Bot"},
        "lead": {"name": "Jane Doe", "email": "jane@example.com"},
        "conversation_id": 42,
        "page_url": "https://example.com/pricing",
    }, indent=2)
