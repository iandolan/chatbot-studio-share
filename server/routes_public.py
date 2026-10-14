"""Widget-facing API. Open (CORS *) but rate-limited; auth is the embed key."""
import json
import secrets
import time
from itsdangerous import URLSafeTimedSerializer, BadSignature
from .config import Config

from flask import Blueprint, Response, jsonify, request, stream_with_context

from . import behavior, chat, db, notify
from .security import (client_ip, rate_limited, unlock_token, valid_unlock_token,
                      verify_passphrase)

bp = Blueprint("public", __name__, url_prefix="/api/widget")

CHAT_LIMIT_PER_MIN = 20
CHAT_LIMIT_PER_HOUR = 150


@bp.before_request
def validate_public_body():
    if request.method == "POST":
        if request.content_length and request.content_length > 32000:
            return jsonify(error="Request is too large"), 413
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Send a JSON object"), 400


@bp.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Conversation-Token"
    return resp


@bp.route("/<path:_any>", methods=["OPTIONS"])
def preflight(_any):
    return "", 204


def _bot_by_key(key):
    if not key:
        return None
    return db.query("SELECT * FROM bots WHERE embed_key = ?", (key,), one=True)


@bp.get("/config")
def widget_config():
    bot = _bot_by_key(request.args.get("key", ""))
    if bot is None:
        return jsonify({"error": "Unknown bot key"}), 404
    gated = bot["mode"] == "internal_kb" and bool(bot["access_hash"])
    return jsonify({
        "name": bot["name"],
        "welcome_message": bot["welcome_message"],
        "theme_color": bot["theme_color"],
        "launcher_text": bot["launcher_text"],
        "position": bot["position"],
        "starter_questions": db.json_field(bot["starter_questions"], []),
        "lead_fields": db.json_field(bot["lead_fields"], []),
        "mode": bot["mode"],
        "gated": gated,
    })


@bp.post("/unlock")
def widget_unlock():
    """Exchange the shared passphrase for a stable per-bot unlock token that the
    widget replays on every chat call. Gates internal-KB bots."""
    body = request.get_json(silent=True) or {}
    bot = _bot_by_key(str(body.get("key", "")))
    if bot is None:
        return jsonify({"error": "Unknown bot key"}), 404
    if rate_limited(f"unlock:{client_ip()}:{bot['id']}", 10, 300):
        return jsonify({"error": "Too many attempts, please wait a moment."}), 429
    if bot["mode"] != "internal_kb" or not bot["access_hash"]:
        return jsonify({"error": "This bot isn't passphrase-gated."}), 400
    passphrase = str(body.get("passphrase", ""))
    if not verify_passphrase(passphrase, bot["access_hash"]):
        return jsonify({"ok": False, "error": "Incorrect passphrase."}), 401
    return jsonify({"ok": True, "unlock_token": unlock_token(bot["embed_key"], bot["access_hash"])})


def _gate(bot, body=None):
    body = body or {}
    token = body.get("unlock_token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if bot["mode"] == "internal_kb" and bot["access_hash"] and not valid_unlock_token(
            bot["embed_key"], token, bot["access_hash"]):
        return jsonify(error="Enter the knowledge-base passphrase to continue.", gate_required=True), 401
    return None


def _session_serializer():
    return URLSafeTimedSerializer(Config.SECRET_KEY, salt="widget-session-v2")


def _session_valid(bot, sid, token):
    try:
        value = _session_serializer().loads(token or "", max_age=30 * 86400)
        return value == {"bot": bot["id"], "session": sid}
    except (BadSignature, ValueError, TypeError):
        return False


def _session_check(bot, sid, body=None, always=False):
    token = (body or {}).get("conversation_token") or request.headers.get("X-Conversation-Token", "")
    convo = db.query("SELECT token_required FROM conversations WHERE bot_id=? AND session_id=?",
                     (bot["id"], sid), one=True)
    if (always or (convo and convo["token_required"])) and not _session_valid(bot, sid, token):
        return jsonify(error="This conversation has expired. Start a new conversation.", session_expired=True), 401
    return None


@bp.post("/session")
def widget_session():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error="Expected an object"), 400
    bot = _bot_by_key(str(body.get("key", "")))
    if bot is None:
        return jsonify(error="Unknown bot key"), 404
    error = _gate(bot, body)
    if error:
        return error
    if rate_limited(f"session:{client_ip()}:{bot['id']}", 30, 60):
        return jsonify(error="Too many requests. Try again shortly."), 429
    sid = str(body.get("session_id", ""))[:64]
    if not _session_valid(bot, sid, body.get("conversation_token")):
        sid = secrets.token_urlsafe(24)
    # Create lazily on the first message, so page views do not count as chats.
    return jsonify(session_id=sid, conversation_token=_session_serializer().dumps({"bot": bot["id"], "session": sid}))


@bp.get("/history")
def widget_history():
    bot = _bot_by_key(request.args.get("key", ""))
    if bot is None:
        return jsonify(error="Unknown bot key"), 404
    sid = request.args.get("session_id", "")[:64]
    error = _gate(bot) or _session_check(bot, sid, always=True)
    if error:
        return error
    if rate_limited(f"history:{client_ip()}:{bot['id']}", 30, 60):
        return jsonify(error="Too many requests"), 429
    convo = db.query("SELECT id,status FROM conversations WHERE bot_id=? AND session_id=?", (bot['id'], sid), one=True)
    if not convo:
        return jsonify(messages=[], status="bot", latest_id=0)
    rows = db.query("SELECT id,role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (convo['id'],))
    return jsonify(messages=[dict(m) for m in reversed(rows)], status=convo['status'],
                   latest_id=max((m['id'] for m in rows if m['role']=='agent'), default=0))


def _get_or_create_conversation(bot_id, session_id, page_url, token_required=False):
    now = db.now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "INSERT INTO conversations(bot_id,session_id,page_url,started_at,last_message_at,token_required) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id,session_id) DO UPDATE SET "
            "last_message_at=excluded.last_message_at,resolved=0 RETURNING id",
            (bot_id, session_id, page_url[:500], now, now, int(token_required))).fetchone()
    return row['id']


def _claim_turn(convo_id, request_id):
    with db.transaction() as conn:
        conn.execute("UPDATE chat_turns SET status='interrupted' WHERE conversation_id=? "
                     "AND status='running' AND created_at<?", (convo_id, time.time()-600))
        row = conn.execute("SELECT status,result FROM chat_turns WHERE conversation_id=? AND request_id=?",
                           (convo_id, request_id)).fetchone()
        if row:
            if row['status'] == 'done':
                return json.loads(row['result']), None
            return None, (jsonify(error="This message is still processing or was interrupted. Refresh the chat before sending again."), 409)
        active = conn.execute("SELECT 1 FROM chat_turns WHERE conversation_id=? AND status='running'", (convo_id,)).fetchone()
        if active:
            return None, (jsonify(error="Wait for the current reply before sending another message."), 409)
        conn.execute("INSERT INTO chat_turns(conversation_id,request_id,status,created_at) VALUES(?,?,'running',?)",
                     (convo_id, request_id, time.time()))
    return None, None


def _finish_turn(convo_id, request_id, result=None):
    db.execute("UPDATE chat_turns SET status=?, result=? WHERE conversation_id=? AND request_id=?",
               ('done' if result is not None else 'interrupted', json.dumps(result or {}), convo_id, request_id))


def _chat_request_checks(body):
    """Shared validation for the chat endpoints. Returns
    (bot, message, session_id, page_url, error_response)."""
    bot = _bot_by_key(str(body.get("key", "")))
    if bot is None:
        return None, "", "", "", (jsonify({"error": "Unknown bot key"}), 404)
    message = str(body.get("message", "")).strip()[:4000]
    session_id = str(body.get("session_id", "")).strip()[:64]
    page_url = str(body.get("page_url", ""))
    if not message or not session_id:
        return None, "", "", "", (jsonify({"error": "message and session_id are required"}), 400)
    error = _gate(bot, body) or _session_check(bot, session_id, body)
    if error:
        return None, "", "", "", error
    if rate_limited(f"chat:{client_ip()}:{bot['id']}", CHAT_LIMIT_PER_MIN, 60) or \
       rate_limited(f"chat-h:{session_id}:{bot['id']}", CHAT_LIMIT_PER_HOUR, 3600):
        return None, "", "", "", (jsonify({
            "reply": "You're sending messages a little fast — give me a few seconds and try again.",
            "action": None, "rate_limited": True,
        }), 429)
    return bot, message, session_id, page_url, None


def _turn_setup(body):
    bot, message, sid, page_url, error = _chat_request_checks(body)
    if error:
        return None, None, None, None, None, error
    protected = _session_valid(bot, sid, body.get("conversation_token"))
    cid = _get_or_create_conversation(bot['id'], sid, page_url, protected)
    rid = str(body.get("request_id", "")).strip()[:100] or secrets.token_hex(16)
    replay, error = _claim_turn(cid, rid)
    return bot, message, cid, rid, replay, error


@bp.post("/chat")
def widget_chat():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error="Expected an object"), 400
    bot, message, cid, rid, replay, error = _turn_setup(body)
    if error:
        return error
    if replay is not None:
        return jsonify(replay)
    try:
        result = chat.run_turn(bot, cid, message)
        public = {k: result.get(k, []) if k == 'citations' else result.get(k)
                  for k in ('reply','action','citations')}
        _finish_turn(cid, rid, public)
        return jsonify(public)
    except BaseException:
        _finish_turn(cid, rid)
        raise


@bp.post("/chat/stream")
def widget_chat_stream():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error="Expected an object"), 400
    bot, message, cid, rid, replay, error = _turn_setup(body)
    if error:
        return error

    def sse():
        if replay is not None:
            yield f"data: {json.dumps({'delta': replay['reply']})}\n\n"
            yield f"data: {json.dumps({'done': True, 'action': replay['action'], 'citations': replay.get('citations', [])})}\n\n"
            return
        parts, completed = [], False
        try:
            for event in chat.run_turn_stream(bot, cid, message):
                if event.get('delta'):
                    parts.append(event['delta'])
                if event.get('done'):
                    _finish_turn(cid, rid, {'reply': ''.join(parts), 'action': event.get('action'),
                                            'citations': event.get('citations', [])})
                    completed = True
                    # Provider error details stay in server logs.
                    event['error'] = ('Daily AI budget reached' if 'budget' in str(event.get('error', '')).lower()
                                      else 'Reply service unavailable' if event.get('error') else None)
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            if not completed:
                _finish_turn(cid, rid)

    return Response(stream_with_context(sse()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@bp.get("/updates")
def widget_updates():
    """Lightweight poll for live team replies + conversation status.
    First call (no `after`) returns just the high-water mark."""
    bot = _bot_by_key(request.args.get("key", ""))
    if bot is None:
        return jsonify({"error": "Unknown bot key"}), 404
    session_id = request.args.get("session_id", "")[:64]
    error = _gate(bot) or _session_check(bot, session_id)
    if error:
        return error
    if rate_limited(f"upd:{client_ip()}:{bot['id']}", 30, 60):
        return jsonify({"error": "Slow down"}), 429
    convo = db.query(
        "SELECT id, status FROM conversations WHERE bot_id = ? AND session_id = ?",
        (bot["id"], session_id), one=True,
    )
    if convo is None:
        return jsonify({"status": "bot", "latest_id": 0, "messages": []})

    after = request.args.get("after", "")
    latest = db.query(
        "SELECT COALESCE(MAX(id), 0) AS m FROM messages "
        "WHERE conversation_id = ? AND role = 'agent'", (convo["id"],), one=True,
    )["m"]
    if not after.isdigit():
        return jsonify({"status": convo["status"], "latest_id": latest, "messages": []})

    rows = db.query(
        "SELECT id, content, created_at FROM messages "
        "WHERE conversation_id = ? AND role = 'agent' AND id > ? ORDER BY id LIMIT 20",
        (convo["id"], int(after)),
    )
    return jsonify({
        "status": convo["status"],
        "latest_id": rows[-1]["id"] if rows else int(after),
        "messages": [dict(r) for r in rows],
    })


@bp.post("/lead")
def widget_lead():
    body = request.get_json(silent=True) or {}
    bot = _bot_by_key(str(body.get("key", "")))
    if bot is None:
        return jsonify({"error": "Unknown bot key"}), 404

    session_id = str(body.get("session_id", "")).strip()[:64]
    if not session_id:
        return jsonify(error="A conversation is required"), 400
    error = _gate(bot, body) or _session_check(bot, session_id, body)
    if error:
        return error
    if behavior.profile(bot)['enabled'] and 'lead_form' not in behavior.allowed(bot):
        return jsonify(error="Contact capture is disabled for this bot"), 403
    if rate_limited(f"lead:{client_ip()}:{bot['id']}", 5, 300):
        return jsonify({"error": "Too many submissions, please wait a moment."}), 429

    fields = db.json_field(bot["lead_fields"], [])
    allowed_keys = {f["key"] for f in fields if isinstance(f, dict) and f.get("key")}
    submitted = body.get("fields") or {}
    if not isinstance(submitted, dict):
        return jsonify({"error": "fields must be an object"}), 400

    clean = {}
    for field in fields:
        key = field.get("key")
        value = str(submitted.get(key, "")).strip()[:500]
        if field.get("required") and not value:
            return jsonify({"error": f"{field.get('label', key)} is required"}), 400
        if value:
            clean[key] = value
    for key, value in submitted.items():
        if key in allowed_keys and key not in clean:
            value = str(value).strip()[:500]
            if value:
                clean[key] = value
    if not clean:
        return jsonify({"error": "No details provided"}), 400

    convo_row = db.query(
        "SELECT id, page_url FROM conversations WHERE bot_id = ? AND session_id = ?",
        (bot["id"], session_id), one=True,
    )
    convo_id = convo_row["id"] if convo_row else None
    page_url = str(body.get("page_url", "")) or (convo_row["page_url"] if convo_row else "")

    with db.transaction():
        lead_id = db.execute(
            "INSERT INTO leads (bot_id, conversation_id, data, page_url, created_at) VALUES (?,?,?,?,?)",
            (bot["id"], convo_id, json.dumps(clean), page_url[:500], db.now_iso()))
        notify.notify_lead(bot, clean, convo_id, page_url, lead_id=lead_id)

    return jsonify({"ok": True, "message": "Thanks. Your details have been saved for the team."})
