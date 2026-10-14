"""Dashboard API + pages. Everything here requires admin (when a password is set)."""
import csv
import io
import json
import re

from flask import Blueprint, Response, jsonify, render_template, request

from . import behavior, budget, db, faq_miner, notify, presets, rag
from .chat import TONE_PRESETS, bot_credentials
from .config import Config
from .crawler import CrawlError, crawl
from .extract import ALLOWED_EXTENSIONS, ExtractError, extract_file
from .llm import (DEFAULT_MODELS, LLMError, MODEL_CHOICES, PROVIDERS,
                  chat_completion, resolve_model, resolve_provider)
from .security import (decrypt_secret, encrypt_secret, hash_passphrase, key_hint,
                       new_embed_key, require_admin_api, require_admin_page)

bp = Blueprint("admin", __name__)

TONES = tuple(TONE_PRESETS.keys())


def _base_url():
    return (Config.PUBLIC_BASE_URL or request.url_root).rstrip("/")


def _bot_or_404(bot_id):
    return db.query("SELECT * FROM bots WHERE id = ?", (bot_id,), one=True)


def _bot_public(bot):
    """Bot dict for the dashboard; never includes the raw API key or passphrase hash."""
    data = dict(bot)
    data.pop("api_key_encrypted", None)
    data.pop("access_hash", None)
    data["api_key_set"] = bool(bot["api_key_encrypted"])
    data["access_gated"] = bool(bot["access_hash"])
    data["starter_questions"] = db.json_field(bot["starter_questions"], [])
    data["lead_fields"] = db.json_field(bot["lead_fields"], [])
    data["guardrails"] = db.json_field(bot["guardrails"], [])
    data["behavior_config"] = behavior.profile(bot)
    data["resolved_provider"] = resolve_provider(bot["provider"])
    data["resolved_model"] = resolve_model(data["resolved_provider"], bot["model"])
    return data


# ---------- pages ----------

@bp.get("/")
@require_admin_page
def dashboard_page():
    return render_template("dashboard.html", mock_mode=Config.MOCK_LLM,
                           budget=budget.status())


@bp.get("/bots/<int:bot_id>")
@require_admin_page
def builder_page(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return render_template("dashboard.html", mock_mode=Config.MOCK_LLM), 404
    return render_template(
        "builder.html",
        bot_id=bot_id,
        bot_name=bot["name"],
        mock_mode=Config.MOCK_LLM,
        budget=budget.status(),
        base_url=_base_url(),
        tones=TONES,
        providers=PROVIDERS,
        model_choices=MODEL_CHOICES,
        default_models=DEFAULT_MODELS,
        default_provider=Config.DEFAULT_PROVIDER,
        smtp_configured=Config.smtp_configured(),
        webhook_preview=notify.payload_preview(),
    )


# ---------- bots ----------

@bp.get("/api/bots")
@require_admin_api
def list_bots():
    rows = db.query("""
        SELECT b.*,
            (SELECT COUNT(*) FROM sources s WHERE s.bot_id = b.id) AS source_count,
            (SELECT COUNT(*) FROM conversations c WHERE c.bot_id = b.id) AS conversation_count,
            (SELECT COUNT(*) FROM leads l WHERE l.bot_id = b.id) AS lead_count,
            (SELECT COUNT(*) FROM gaps g WHERE g.bot_id = b.id) AS gap_count
        FROM bots b ORDER BY b.created_at DESC
    """)
    bots = []
    for row in rows:
        bot = _bot_public(row)
        bot["source_count"] = row["source_count"]
        bot["conversation_count"] = row["conversation_count"]
        bot["lead_count"] = row["lead_count"]
        bot["gap_count"] = row["gap_count"]
        bots.append(bot)
    return jsonify({"bots": bots, "mock_mode": Config.MOCK_LLM,
                    "budget": budget.status()})


@bp.get("/api/presets")
@require_admin_api
def list_presets_api():
    return jsonify({"presets": presets.list_presets()})


# Preset config keys that are stored as JSON text columns on the bot.
_PRESET_JSON_COLS = ("starter_questions", "lead_fields", "guardrails")


def _apply_preset(bot_id, slug):
    """Overwrite a freshly-created bot's fields from a preset definition."""
    preset = presets.get_preset(slug)
    if preset is None:
        return
    cfg = preset["config"]
    cols, params = ["preset = ?"], [slug]
    for key, value in cfg.items():
        if key == "name":
            continue  # keep the caller-provided name
        if key in _PRESET_JSON_COLS:
            value = json.dumps(value)
        cols.append(f"{key} = ?")
        params.append(value)
    params.append(bot_id)
    db.execute(f"UPDATE bots SET {', '.join(cols)} WHERE id = ?", params)


@bp.post("/api/bots")
@require_admin_api
def create_bot():
    body = request.get_json(silent=True) or {}
    slug = str(body.get("preset", "")).strip()
    preset = presets.get_preset(slug) if slug else None
    if slug and preset is None:
        return jsonify({"error": f"Unknown preset '{slug}'"}), 400
    # Name defaults to the preset's name when the user leaves it blank.
    name = str(body.get("name", "")).strip()[:80] or (preset["config"]["name"] if preset else "")
    if not name:
        return jsonify({"error": "Name is required"}), 400
    now = db.now_iso()
    bot_id = db.execute(
        "INSERT INTO bots (name, embed_key, created_at, updated_at) VALUES (?,?,?,?)",
        (name, new_embed_key(), now, now),
    )
    if preset:
        _apply_preset(bot_id, slug)
    return jsonify({"bot": _bot_public(_bot_or_404(bot_id))}), 201


@bp.get("/api/bots/<int:bot_id>")
@require_admin_api
def get_bot(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    return jsonify({"bot": _bot_public(bot)})


_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

# field -> validator (returns cleaned value or raises ValueError)
def _v_str(max_len):
    return lambda v: str(v)[:max_len]

def _v_choice(options):
    def check(v):
        if v not in options:
            raise ValueError(f"must be one of {', '.join(options)}")
        return v
    return check

def _v_temperature(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("must be a number")
    return min(max(f, 0.0), 1.0)

def _v_color(v):
    v = str(v).strip()
    if not _HEX_COLOR.match(v):
        raise ValueError("must be a hex color like #4f46e5")
    return v

def _v_starter_questions(v):
    if not isinstance(v, list):
        raise ValueError("must be a list")
    cleaned = [str(q).strip()[:120] for q in v if str(q).strip()]
    return json.dumps(cleaned[:6])

def _v_lead_fields(v):
    if not isinstance(v, list):
        raise ValueError("must be a list")
    cleaned = []
    seen = set()
    for item in v:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()[:60]
        if not label:
            continue
        key = re.sub(r"[^a-z0-9_]+", "_", str(item.get("key") or label).lower()).strip("_") or "field"
        base, n = key, 2
        while key in seen:
            key, n = f"{base}_{n}", n + 1
        seen.add(key)
        ftype = item.get("type") if item.get("type") in ("text", "email", "tel", "textarea") else "text"
        cleaned.append({"key": key, "label": label, "type": ftype,
                        "required": bool(item.get("required"))})
    return json.dumps(cleaned[:8])

def _v_provider(v):
    v = str(v).strip()
    if v and v not in PROVIDERS:
        raise ValueError(f"must be empty or one of {', '.join(PROVIDERS)}")
    return v

def _v_guardrails(v):
    if not isinstance(v, list):
        raise ValueError("must be a list")
    cleaned = [str(g).strip()[:300] for g in v if str(g).strip()]
    return json.dumps(cleaned[:20])

EDITABLE_FIELDS = {
    "behavior_config": lambda v: json.dumps(behavior.validate(v)),
    "name": _v_str(80),
    "system_prompt": _v_str(8000),
    "tone": _v_choice(TONES),
    "welcome_message": _v_str(500),
    "fallback_message": _v_str(500),
    "provider": _v_provider,
    "model": _v_str(80),
    "temperature": _v_temperature,
    "theme_color": _v_color,
    "launcher_text": _v_str(40),
    "position": _v_choice(("left", "right")),
    "starter_questions": _v_starter_questions,
    "lead_fields": _v_lead_fields,
    "lead_rule": _v_str(1000),
    "lead_webhook_url": _v_str(500),
    "lead_alert_email": _v_str(200),
    "handoff_enabled": lambda v: 1 if v else 0,
    "mode": _v_choice(("public", "internal_kb")),
    "cite_sources": lambda v: 1 if v else 0,
    "guardrails": _v_guardrails,
}


@bp.patch("/api/bots/<int:bot_id>")
@require_admin_api
def update_bot(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    updates, params = [], []
    for field, value in body.items():
        validator = EDITABLE_FIELDS.get(field)
        if validator is None:
            continue
        try:
            cleaned = validator(value)
        except ValueError as exc:
            return jsonify({"error": f"{field}: {exc}"}), 400
        updates.append(f"{field} = ?")
        params.append(cleaned)
    if not updates:
        return jsonify({"error": "No editable fields in request"}), 400
    if "name" in body and not str(body["name"]).strip():
        return jsonify({"error": "Name can't be empty"}), 400
    params.extend([db.now_iso(), bot_id])
    db.execute(f"UPDATE bots SET {', '.join(updates)}, updated_at = ? WHERE id = ?", params)
    return jsonify({"bot": _bot_public(_bot_or_404(bot_id))})


@bp.delete("/api/bots/<int:bot_id>")
@require_admin_api
def delete_bot(bot_id):
    db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
    return jsonify({"ok": True})


@bp.post("/api/bots/<int:bot_id>/rotate-key")
@require_admin_api
def rotate_embed_key(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    db.execute("UPDATE bots SET embed_key = ?, updated_at = ? WHERE id = ?",
               (new_embed_key(), db.now_iso(), bot_id))
    return jsonify({"bot": _bot_public(_bot_or_404(bot_id))})


# ---------- per-bot API key ----------

@bp.post("/api/bots/<int:bot_id>/apikey")
@require_admin_api
def set_api_key(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    api_key = str(body.get("api_key", "")).strip()
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400
    db.execute(
        "UPDATE bots SET api_key_encrypted = ?, api_key_hint = ?, updated_at = ? WHERE id = ?",
        (encrypt_secret(api_key), key_hint(api_key), db.now_iso(), bot_id),
    )
    return jsonify({"ok": True, "api_key_hint": key_hint(api_key)})


# ---------- internal-KB access passphrase ----------

@bp.post("/api/bots/<int:bot_id>/passphrase")
@require_admin_api
def set_passphrase(bot_id):
    if _bot_or_404(bot_id) is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    passphrase = str(body.get("passphrase", "")).strip()
    if len(passphrase) < 4:
        return jsonify({"error": "Passphrase must be at least 4 characters"}), 400
    db.execute("UPDATE bots SET access_hash = ?, updated_at = ? WHERE id = ?",
               (hash_passphrase(passphrase), db.now_iso(), bot_id))
    return jsonify({"ok": True, "access_gated": True})


@bp.delete("/api/bots/<int:bot_id>/passphrase")
@require_admin_api
def clear_passphrase(bot_id):
    db.execute("UPDATE bots SET access_hash = '', updated_at = ? WHERE id = ?",
               (db.now_iso(), bot_id))
    return jsonify({"ok": True, "access_gated": False})


@bp.delete("/api/bots/<int:bot_id>/apikey")
@require_admin_api
def clear_api_key(bot_id):
    db.execute(
        "UPDATE bots SET api_key_encrypted = '', api_key_hint = '', updated_at = ? WHERE id = ?",
        (db.now_iso(), bot_id),
    )
    return jsonify({"ok": True})


@bp.post("/api/bots/<int:bot_id>/apikey/test")
@require_admin_api
def test_api_key(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    if Config.MOCK_LLM:
        return jsonify({"ok": True, "detail": "Mock AI mode is on — no real call was made. "
                        "Set a provider key in .env to leave mock mode."})
    provider, api_key, model = bot_credentials(bot)
    if not api_key:
        return jsonify({"ok": False, "detail": f"No key available for provider '{provider}'."})
    try:
        chat_completion(provider, api_key, model, "You are a health check.",
                        [{"role": "user", "content": "Reply with the word: ok"}],
                        temperature=0, max_tokens=8)
    except LLMError as exc:
        return jsonify({"ok": False, "detail": str(exc)})
    source = "this bot's own key" if bot["api_key_encrypted"] else "the platform key"
    return jsonify({"ok": True, "detail": f"{provider} / {model} responded using {source}."})


# ---------- training sources ----------

SOURCE_COLS = ("id, type, title, status, error, chunk_count, meta, recrawl_hours, "
               "last_crawled_at, created_at")


@bp.get("/api/bots/<int:bot_id>/sources")
@require_admin_api
def list_sources(bot_id):
    rows = db.query(
        f"SELECT {SOURCE_COLS} FROM sources WHERE bot_id = ? ORDER BY id DESC", (bot_id,))
    return jsonify({"sources": [dict(r) for r in rows]})


def _index_new_source(bot_id, stype, title, content, meta=None, recrawl_hours=0):
    now = db.now_iso()
    source_id = db.execute(
        "INSERT INTO sources (bot_id, type, title, content, meta, recrawl_hours, "
        "last_crawled_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (bot_id, stype, title[:200], content[:500_000], json.dumps(meta or {}),
         recrawl_hours, now if stype == "url" else "", now),
    )
    try:
        chunks = ([content] if stype == "qa" else rag.chunk_text(content))
        rag.index_source(bot_id, source_id, chunks)
        return source_id, None
    except LLMError as exc:
        db.execute("UPDATE sources SET status = 'error', error = ? WHERE id = ?",
                   (str(exc)[:500], source_id))
        return source_id, str(exc)


@bp.post("/api/bots/<int:bot_id>/sources")
@require_admin_api
def add_source(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    stype = body.get("type")

    if stype == "text":
        title = str(body.get("title", "")).strip() or "Pasted text"
        content = str(body.get("content", "")).strip()
        if len(content) < 10:
            return jsonify({"error": "Add at least a sentence of content"}), 400
        source_id, err = _index_new_source(bot_id, "text", title, content)

    elif stype == "qa":
        question = str(body.get("question", "")).strip()
        answer_text = str(body.get("answer", "")).strip()
        if not question or not answer_text:
            return jsonify({"error": "Both question and answer are required"}), 400
        content = f"Q: {question}\nA: {answer_text}"
        source_id, err = _index_new_source(bot_id, "qa", question[:200], content)

    elif stype == "url":
        url = str(body.get("url", "")).strip()
        max_pages = body.get("max_pages", 5)
        try:
            recrawl_hours = int(body.get("recrawl_hours", 0) or 0)
        except (TypeError, ValueError):
            recrawl_hours = 0
        if recrawl_hours not in (0, 24, 168):
            recrawl_hours = 0
        if not url:
            return jsonify({"error": "URL is required"}), 400
        try:
            result = crawl(url, max_pages)
        except (CrawlError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        source_id, err = _index_new_source(
            bot_id, "url", result["title"], result["content"],
            meta={"url": url, "max_pages": max_pages, "pages": result["pages"]},
            recrawl_hours=recrawl_hours,
        )

    else:
        return jsonify({"error": "type must be text, qa or url"}), 400

    row = db.query(f"SELECT {SOURCE_COLS} FROM sources WHERE id = ?", (source_id,), one=True)
    status_code = 201 if not err else 207
    return jsonify({"source": dict(row), "error": err}), status_code


@bp.post("/api/bots/<int:bot_id>/sources/upload")
@require_admin_api
def upload_source(bot_id):
    if _bot_or_404(bot_id) is None:
        return jsonify({"error": "Bot not found"}), 404
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Attach a file (PDF, DOCX, TXT or MD)"}), 400
    try:
        title, text = extract_file(upload.filename, upload.read())
    except ExtractError as exc:
        return jsonify({"error": str(exc)}), 400
    if request.args.get("background") == "1":
        from . import jobs
        with db.transaction():
            source_id = db.execute("INSERT INTO sources(bot_id,type,title,content,meta,status,created_at) "
                "VALUES(?,?,?,?,?,'pending',?)", (bot_id, "file", title[:200], text[:500_000],
                    json.dumps({"filename": upload.filename, "chars": len(text)}), db.now_iso()))
            job_id = jobs.enqueue(bot_id, source_id, "index", {})
        return jsonify(job_id=job_id, source_id=source_id), 202
    source_id, err = _index_new_source(
        bot_id, "file", title, text,
        meta={"filename": upload.filename, "chars": len(text)},
    )
    row = db.query(f"SELECT {SOURCE_COLS} FROM sources WHERE id = ?", (source_id,), one=True)
    return jsonify({"source": dict(row), "error": err}), (201 if not err else 207)


@bp.patch("/api/bots/<int:bot_id>/sources/<int:source_id>")
@require_admin_api
def update_source(bot_id, source_id):
    row = db.query("SELECT id, type FROM sources WHERE id = ? AND bot_id = ?",
                   (source_id, bot_id), one=True)
    if row is None:
        return jsonify({"error": "Source not found"}), 404
    body = request.get_json(silent=True) or {}
    if "recrawl_hours" in body:
        if row["type"] != "url":
            return jsonify({"error": "Only URL sources can re-crawl"}), 400
        try:
            hours = int(body["recrawl_hours"])
        except (TypeError, ValueError):
            return jsonify({"error": "recrawl_hours must be a number"}), 400
        if hours not in (0, 24, 168):
            return jsonify({"error": "recrawl_hours must be 0, 24 or 168"}), 400
        db.execute("UPDATE sources SET recrawl_hours = ? WHERE id = ?", (hours, source_id))
    updated = db.query(f"SELECT {SOURCE_COLS} FROM sources WHERE id = ?", (source_id,), one=True)
    return jsonify({"source": dict(updated)})


@bp.post("/api/bots/<int:bot_id>/sources/<int:source_id>/recrawl")
@require_admin_api
def recrawl_source(bot_id, source_id):
    row = db.query("SELECT * FROM sources WHERE id = ? AND bot_id = ? AND type = 'url'",
                   (source_id, bot_id), one=True)
    if row is None:
        return jsonify({"error": "URL source not found"}), 404
    meta = db.json_field(row["meta"], {})
    try:
        result = crawl(meta.get("url", ""), meta.get("max_pages", 5))
    except (CrawlError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    meta["pages"] = result["pages"]
    try:
        rag.index_source(bot_id, source_id, rag.chunk_text(result["content"]), updates={
            "title": result["title"][:200], "content": result["content"][:500_000],
            "meta": json.dumps(meta), "last_crawled_at": db.now_iso(),
        }, expected_revision=row["revision"])
    except (LLMError, ValueError) as exc:
        db.execute("UPDATE sources SET error = ? WHERE id = ?", (str(exc)[:500], source_id))
        return jsonify({"error": str(exc)}), 502
    row = db.query(f"SELECT {SOURCE_COLS} FROM sources WHERE id = ?", (source_id,), one=True)
    return jsonify({"source": dict(row)})


@bp.delete("/api/bots/<int:bot_id>/sources/<int:source_id>")
@require_admin_api
def delete_source(bot_id, source_id):
    db.execute("DELETE FROM sources WHERE id = ? AND bot_id = ?", (source_id, bot_id))
    return jsonify({"ok": True})


# ---------- answer gaps ----------

@bp.get("/api/bots/<int:bot_id>/gaps")
@require_admin_api
def list_gaps(bot_id):
    rows = db.query(
        "SELECT id, question, best_score, created_at FROM gaps "
        "WHERE bot_id = ? ORDER BY id DESC LIMIT 50", (bot_id,))
    return jsonify({"gaps": [dict(r) for r in rows]})


@bp.delete("/api/bots/<int:bot_id>/gaps/<int:gap_id>")
@require_admin_api
def delete_gap(bot_id, gap_id):
    db.execute("DELETE FROM gaps WHERE id = ? AND bot_id = ?", (gap_id, bot_id))
    return jsonify({"ok": True})


# ---------- FAQ miner (what people ask) ----------

FAQ_ENTRY_COLS = "id, question, answer, source_id, cluster_key, created_at"


def _suppress_questions(bot_id, norms, reason):
    now = db.now_iso()
    rows = [(bot_id, n, reason, now) for n in norms if n]
    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO faq_suppressed (bot_id, question_norm, reason, created_at) "
            "VALUES (?,?,?,?)", rows)


@bp.get("/api/bots/<int:bot_id>/faq/clusters")
@require_admin_api
def faq_clusters(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    clusters = faq_miner.mine(bot_id, fallback_message=bot["fallback_message"])
    return jsonify({"clusters": clusters})


@bp.post("/api/bots/<int:bot_id>/faq/draft")
@require_admin_api
def faq_draft(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    question = str(body.get("question", "")).strip()[:500]
    if not question:
        return jsonify({"error": "question is required"}), 400
    best_answer = str(body.get("best_answer", ""))[:4000]
    try:
        count = max(1, int(body.get("count", 1)))
    except (TypeError, ValueError):
        count = 1
    result = faq_miner.draft_answer(bot, question, best_answer=best_answer, count=count)
    return jsonify(result)


@bp.post("/api/bots/<int:bot_id>/faq/approve")
@require_admin_api
def faq_approve(bot_id):
    """Approve a mined Q&A into the bot's training data (as a regular Q&A
    source) and into the exportable website FAQ list."""
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    question = str(body.get("question", "")).strip()[:500]
    answer = str(body.get("answer", "")).strip()[:8000]
    if not question or not answer:
        return jsonify({"error": "Both question and answer are required"}), 400
    if answer.lower().startswith("todo"):
        return jsonify({"error": "Write the answer first — it still starts with TODO"}), 400

    # Reuse the existing Q&A training-source mechanism (same as the Training tab).
    source_id, err = _index_new_source(
        bot_id, "qa", question[:200], f"Q: {question}\nA: {answer}")

    cluster_key = faq_miner.normalize_question(question)
    entry_id = db.execute(
        "INSERT INTO faq_entries (bot_id, question, answer, source_id, cluster_key, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (bot_id, question, answer, source_id, cluster_key, db.now_iso()),
    )
    members = body.get("members_norm") or []
    norms = {cluster_key} | {str(m).strip()[:500] for m in members if str(m).strip()}
    _suppress_questions(bot_id, norms, "approved")

    entry = db.query(f"SELECT {FAQ_ENTRY_COLS} FROM faq_entries WHERE id = ?",
                     (entry_id,), one=True)
    return jsonify({"entry": dict(entry), "error": err}), (201 if not err else 207)


@bp.post("/api/bots/<int:bot_id>/faq/dismiss")
@require_admin_api
def faq_dismiss(bot_id):
    if _bot_or_404(bot_id) is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    members = body.get("members_norm") or []
    norms = {str(m).strip()[:500] for m in members if str(m).strip()}
    if not norms:
        return jsonify({"error": "members_norm is required"}), 400
    _suppress_questions(bot_id, norms, "dismissed")
    return jsonify({"ok": True})


@bp.get("/api/bots/<int:bot_id>/faq/entries")
@require_admin_api
def faq_entries(bot_id):
    rows = db.query(
        f"SELECT {FAQ_ENTRY_COLS} FROM faq_entries WHERE bot_id = ? ORDER BY id DESC",
        (bot_id,))
    return jsonify({"entries": [dict(r) for r in rows]})


@bp.delete("/api/bots/<int:bot_id>/faq/entries/<int:entry_id>")
@require_admin_api
def delete_faq_entry(bot_id, entry_id):
    """Removes the entry from the website-FAQ export. The Q&A training source
    it created stays (delete that from the Training tab if needed)."""
    db.execute("DELETE FROM faq_entries WHERE id = ? AND bot_id = ?", (entry_id, bot_id))
    return jsonify({"ok": True})


@bp.get("/api/bots/<int:bot_id>/faq/export")
@require_admin_api
def faq_export(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    rows = db.query(
        "SELECT question, answer FROM faq_entries WHERE bot_id = ? ORDER BY id",
        (bot_id,))
    entries = [dict(r) for r in rows]
    fmt = request.args.get("format", "html")
    if fmt == "markdown":
        return Response(faq_miner.export_markdown(entries),
                        mimetype="text/markdown; charset=utf-8")
    return Response(faq_miner.export_html(bot["name"], entries),
                    mimetype="text/plain; charset=utf-8")


# ---------- redirects ----------

@bp.get("/api/bots/<int:bot_id>/redirects")
@require_admin_api
def list_redirects(bot_id):
    rows = db.query("SELECT * FROM redirects WHERE bot_id = ? ORDER BY id", (bot_id,))
    return jsonify({"redirects": [dict(r) for r in rows]})


@bp.post("/api/bots/<int:bot_id>/redirects")
@require_admin_api
def add_redirect(bot_id):
    if _bot_or_404(bot_id) is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True) or {}
    title = str(body.get("title", "")).strip()[:120]
    url = str(body.get("url", "")).strip()[:500]
    description = str(body.get("description", "")).strip()[:300]
    mode = body.get("mode") if body.get("mode") in ("button", "auto") else "button"
    if not title or not url:
        return jsonify({"error": "Title and URL are required"}), 400
    if not (url.startswith(("http://", "https://", "/"))):
        return jsonify({"error": "URL must be absolute (https://…) or site-relative (/pricing)"}), 400
    redirect_id = db.execute(
        "INSERT INTO redirects (bot_id, title, url, description, mode) VALUES (?,?,?,?,?)",
        (bot_id, title, url, description, mode),
    )
    row = db.query("SELECT * FROM redirects WHERE id = ?", (redirect_id,), one=True)
    return jsonify({"redirect": dict(row)}), 201


@bp.delete("/api/bots/<int:bot_id>/redirects/<int:redirect_id>")
@require_admin_api
def delete_redirect(bot_id, redirect_id):
    db.execute("DELETE FROM redirects WHERE id = ? AND bot_id = ?", (redirect_id, bot_id))
    return jsonify({"ok": True})


# ---------- leads ----------

@bp.get("/api/bots/<int:bot_id>/leads")
@require_admin_api
def list_leads(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    rows = db.query(
        "SELECT id, conversation_id, data, page_url, created_at, stage, notes FROM leads "
        "WHERE bot_id = ? ORDER BY id DESC", (bot_id,))
    leads = [{**dict(r), "data": db.json_field(r["data"], {})} for r in rows]

    if request.args.get("format") == "csv":
        keys = [f["key"] for f in db.json_field(bot["lead_fields"], []) if f.get("key")]
        extra = sorted({k for lead in leads for k in lead["data"]} - set(keys))
        headers = keys + extra + ["page_url", "conversation_id", "created_at"]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for lead in leads:
            def safe_cell(value):
                value = str(value)
                return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
            writer.writerow(
                [safe_cell(lead["data"].get(k, "")) for k in keys + extra]
                + [lead["page_url"], lead["conversation_id"] or "", lead["created_at"]]
            )
        return Response(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=leads-bot{bot_id}-{db.now_iso()[:10]}.csv"},
        )
    return jsonify({"leads": leads})


# ---------- conversations ----------

@bp.get("/api/bots/<int:bot_id>/conversations")
@require_admin_api
def list_conversations(bot_id):
    rows = db.query("""
        SELECT c.id, c.session_id, c.page_url, c.status, c.started_at, c.last_message_at,
            (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
            (SELECT content FROM messages m WHERE m.conversation_id = c.id AND m.role = 'user'
             ORDER BY m.id LIMIT 1) AS first_message,
            (SELECT COUNT(*) FROM leads l WHERE l.conversation_id = c.id) AS lead_count
        FROM conversations c WHERE c.bot_id = ?
        ORDER BY c.last_message_at DESC LIMIT 200
    """, (bot_id,))
    return jsonify({"conversations": [dict(r) for r in rows]})


@bp.get("/api/bots/<int:bot_id>/conversations/<int:convo_id>")
@require_admin_api
def get_conversation(bot_id, convo_id):
    convo = db.query("SELECT * FROM conversations WHERE id = ? AND bot_id = ?",
                     (convo_id, bot_id), one=True)
    if convo is None:
        return jsonify({"error": "Conversation not found"}), 404
    messages = db.query(
        "SELECT id, role, content, action, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id", (convo_id,))
    return jsonify({
        "conversation": dict(convo),
        "messages": [
            {**dict(m), "action": json.loads(m["action"]) if m["action"] else None}
            for m in messages
        ],
    })


@bp.delete("/api/bots/<int:bot_id>/conversations/<int:convo_id>")
@require_admin_api
def delete_conversation(bot_id, convo_id):
    db.execute("DELETE FROM conversations WHERE id = ? AND bot_id = ?", (convo_id, bot_id))
    return jsonify({"ok": True})


@bp.post("/api/bots/<int:bot_id>/conversations/<int:convo_id>/reply")
@require_admin_api
def reply_to_conversation(bot_id, convo_id):
    """Human takeover: your reply appears live in the visitor's widget and
    pauses the AI for this conversation until you hand it back."""
    convo = db.query("SELECT id FROM conversations WHERE id = ? AND bot_id = ?",
                     (convo_id, bot_id), one=True)
    if convo is None:
        return jsonify({"error": "Conversation not found"}), 404
    body = request.get_json(silent=True) or {}
    message = str(body.get("message", "")).strip()[:4000]
    if not message:
        return jsonify({"error": "Message is required"}), 400
    now = db.now_iso()
    with db.transaction():
        db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                   (convo_id, "agent", message, now))
        db.execute("UPDATE conversations SET status='human',resolved=0,last_message_at=? WHERE id=?", (now,convo_id))
    return jsonify({"ok": True, "status": "human"})


@bp.post("/api/bots/<int:bot_id>/conversations/<int:convo_id>/status")
@require_admin_api
def set_conversation_status(bot_id, convo_id):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("bot", "needs_human", "human"):
        return jsonify({"error": "status must be bot, needs_human or human"}), 400
    changed = db.execute_rowcount(
        "UPDATE conversations SET status = ? WHERE id = ? AND bot_id = ?",
        (status, convo_id, bot_id),
    )
    if not changed:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify({"ok": True, "status": status})


# ---------- analytics ----------

@bp.get("/api/bots/<int:bot_id>/analytics")
@require_admin_api
def analytics(bot_id):
    if _bot_or_404(bot_id) is None:
        return jsonify({"error": "Bot not found"}), 404
    cutoff_30 = db.query("SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-30 days') AS d", one=True)["d"]
    cutoff_14 = db.query("SELECT date('now', '-13 days') AS d", one=True)["d"]

    convos_30 = db.query(
        "SELECT COUNT(*) AS n FROM conversations WHERE bot_id = ? AND started_at >= ?",
        (bot_id, cutoff_30), one=True)["n"]
    user_msgs_30 = db.query(
        "SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.bot_id = ? AND m.role = 'user' AND m.created_at >= ?",
        (bot_id, cutoff_30), one=True)["n"]
    leads_30 = db.query(
        "SELECT COUNT(*) AS n FROM leads WHERE bot_id = ? AND created_at >= ?",
        (bot_id, cutoff_30), one=True)["n"]
    gaps_30 = db.query(
        "SELECT COUNT(*) AS n FROM gaps WHERE bot_id = ? AND created_at >= ?",
        (bot_id, cutoff_30), one=True)["n"]
    handoffs_30 = db.query(
        "SELECT COUNT(DISTINCT m.conversation_id) AS n FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.bot_id = ? AND m.action LIKE '%\"handoff\"%' AND m.created_at >= ?",
        (bot_id, cutoff_30), one=True)["n"]
    grounded = db.query(
        "SELECT COUNT(*) AS total, COALESCE(SUM(m.grounded), 0) AS grounded "
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.bot_id = ? AND m.role = 'assistant' AND m.grounded IS NOT NULL "
        "AND m.created_at >= ?", (bot_id, cutoff_30), one=True)

    def per_day(sql, params):
        return {r["d"]: r["n"] for r in db.query(sql, params)}

    msgs_daily = per_day(
        "SELECT date(m.created_at) AS d, COUNT(*) AS n FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.bot_id = ? AND m.role = 'user' AND date(m.created_at) >= ? GROUP BY d",
        (bot_id, cutoff_14))
    leads_daily = per_day(
        "SELECT date(created_at) AS d, COUNT(*) AS n FROM leads "
        "WHERE bot_id = ? AND date(created_at) >= ? GROUP BY d",
        (bot_id, cutoff_14))
    days = db.query(
        "WITH RECURSIVE seq(d) AS (SELECT ? UNION ALL SELECT date(d, '+1 day') FROM seq "
        "WHERE d < date('now')) SELECT d FROM seq", (cutoff_14,))

    converted = db.query("SELECT count(*) n FROM conversations c WHERE c.bot_id=? AND c.started_at>=? "
        "AND EXISTS(SELECT 1 FROM leads l WHERE l.conversation_id=c.id)", (bot_id,cutoff_30),one=True)["n"]
    answer_rate = (grounded["grounded"] / grounded["total"]) if grounded["total"] else None
    return jsonify({
        "last_30_days": {
            "conversations": convos_30,
            "user_messages": user_msgs_30,
            "leads": leads_30,
            "gaps": gaps_30,
            "handoffs": handoffs_30,
            "lead_conversion": (converted / convos_30) if convos_30 else None,
            "answer_rate": answer_rate,
        },
        "daily": [
            {"date": r["d"], "messages": msgs_daily.get(r["d"], 0),
             "leads": leads_daily.get(r["d"], 0)}
            for r in days
        ],
    })


# ---------- usage ----------

@bp.get("/api/bots/<int:bot_id>/usage")
@require_admin_api
def usage(bot_id):
    def totals(where_extra="", params=()):
        row = db.query(f"""
            SELECT COUNT(*) AS messages,
                   COALESCE(SUM(m.tokens_in), 0) AS tokens_in,
                   COALESCE(SUM(m.tokens_out), 0) AS tokens_out
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE c.bot_id = ? AND m.role = 'assistant' {where_extra}
        """, (bot_id, *params), one=True)
        return dict(row)
    cutoff = db.query("SELECT datetime('now', '-30 days') AS d", one=True)["d"]
    return jsonify({
        "all_time": totals(),
        "last_30_days": totals("AND m.created_at >= ?", (cutoff,)),
    })


@bp.get("/api/behavior-templates")
@require_admin_api
def behavior_templates():
    return jsonify({"templates": [{"id": name, "label": item["label"],
                    "config": behavior.template(name), "scenarios": item["scenarios"]}
                   for name, item in behavior.TEMPLATES.items()]})


@bp.post("/api/bots/<int:bot_id>/behavior-preview")
@require_admin_api
def behavior_preview(bot_id):
    bot = _bot_or_404(bot_id)
    if bot is None:
        return jsonify({"error": "Bot not found"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected an object"}), 400
    try:
        cfg = behavior.validate(body.get("behavior_config", {}))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    candidate = dict(bot)
    candidate["behavior_config"] = json.dumps(cfg)
    actions = behavior.allowed(candidate)
    warnings = []
    if 'lead_form' in cfg['allowed_actions'] and not bot['lead_rule'].strip():
        warnings.append('Contact forms need a lead capture rule in the Lead capture tab.')
    if 'handoff' in cfg['allowed_actions'] and not bot['handoff_enabled']:
        warnings.append('Human handoff is disabled in Behaviour and tone.')
    if 'redirect' in actions and not db.query('SELECT id FROM redirects WHERE bot_id = ?', (bot_id,)):
        actions.discard('redirect')
        warnings.append('Page links need an approved page in the Redirects tab.')
    return jsonify({"enabled": cfg['enabled'], "actions": sorted(actions),
                    "warnings": warnings, "scenarios": behavior.TEMPLATES[cfg['template']]['scenarios'],
                    "summary": cfg, "note": "Configuration preview only. Use the test chat to evaluate generated answers."})
