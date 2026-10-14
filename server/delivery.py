"""Durable, at-least-once notification delivery with leased attempts."""
import hashlib
import hmac
import json
import logging
import secrets
import smtplib
import time
from email.message import EmailMessage
import requests
from . import db
from .config import Config

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 6


def enqueue(channel, destination, payload, bot_id=None):
    event_id = secrets.token_hex(16)
    db.execute("INSERT INTO notification_outbox(event_id,bot_id,channel,destination,payload,created_at) "
               "VALUES(?,?,?,?,?,?)", (event_id, bot_id, channel, destination,
                                       json.dumps(payload), db.now_iso()))
    return event_id


def send(row):
    payload = json.loads(row["payload"])
    if row["channel"] == "webhook":
        payload["event_id"] = row["event_id"]
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "ChatbotStudio/2.0",
                   "X-Studio-Event-ID": row["event_id"]}
        if Config.WEBHOOK_SIGNING_SECRET:
            headers["X-Studio-Signature"] = hmac.new(
                Config.WEBHOOK_SIGNING_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = requests.post(row["destination"], data=body, headers=headers,
                                 timeout=(5, 15), allow_redirects=False)
        try:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"HTTP {response.status_code}")
        finally:
            response.close()
    else:
        msg = EmailMessage()
        msg["Subject"] = payload["subject"]
        msg["From"] = Config.SMTP_FROM
        msg["To"] = row["destination"]
        # The brand mailbox owns the alert, but the owner is CC'd on everything
        # so nothing depends on someone watching a shared inbox. Skipped when it
        # would just duplicate the To.
        cc = (Config.ALERT_CC or "").strip()
        if cc and cc.lower() != (row["destination"] or "").strip().lower():
            msg["Cc"] = cc
        msg["Message-ID"] = f"<{row['event_id']}@chatbot-studio>"
        msg.set_content(payload["body"])
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if Config.SMTP_USER:
                smtp.login(Config.SMTP_USER, Config.SMTP_PASS)
            smtp.send_message(msg)


def run_due(limit=10):
    count = 0
    for _ in range(limit):
        now = time.time()
        lease = secrets.token_hex(16)
        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM notification_outbox WHERE "
                               "status IN ('pending','sending') AND next_attempt<=? ORDER BY id LIMIT 1",
                               (now,)).fetchone()
            if row is None:
                break
            conn.execute("UPDATE notification_outbox SET status='sending', lease=?, "
                         "attempts=attempts+1, next_attempt=? WHERE id=?", (lease, now + 120, row["id"]))
        try:
            send(row)
        except Exception as exc:
            attempt = row["attempts"] + 1
            # Do not put webhook URLs, provider responses, or contact details in errors.
            error = str(exc) if str(exc).startswith("HTTP ") else type(exc).__name__
            db.execute("UPDATE notification_outbox SET status=?, error=?, next_attempt=? "
                       "WHERE id=? AND lease=?", ("failed" if attempt >= MAX_ATTEMPTS else "pending",
                         error[:200], now + min(3600, 15 * 2 ** attempt), row["id"], lease))
            log.warning("notification %s failed: %s", row["id"], error)
        else:
            db.execute("UPDATE notification_outbox SET status='delivered', error='', delivered_at=? "
                       "WHERE id=? AND lease=?", (db.now_iso(), row["id"], lease))
        count += 1
    return count
