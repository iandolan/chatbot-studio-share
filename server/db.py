import json
import pathlib
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

from flask import g

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    embed_key TEXT UNIQUE NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT 'friendly',
    welcome_message TEXT NOT NULL DEFAULT 'Hi there! How can I help you today?',
    fallback_message TEXT NOT NULL DEFAULT 'Sorry, I don''t have that information yet, but the team can help if you leave your details.',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    temperature REAL NOT NULL DEFAULT 0.4,
    theme_color TEXT NOT NULL DEFAULT '#4f46e5',
    launcher_text TEXT NOT NULL DEFAULT 'Chat with us',
    position TEXT NOT NULL DEFAULT 'right',
    starter_questions TEXT NOT NULL DEFAULT '[]',
    lead_fields TEXT NOT NULL DEFAULT '[{"key":"name","label":"Name","type":"text","required":true},{"key":"email","label":"Email","type":"email","required":true}]',
    lead_rule TEXT NOT NULL DEFAULT '',
    lead_webhook_url TEXT NOT NULL DEFAULT '',
    lead_alert_email TEXT NOT NULL DEFAULT '',
    api_key_encrypted TEXT NOT NULL DEFAULT '',
    api_key_hint TEXT NOT NULL DEFAULT '',
    handoff_enabled INTEGER NOT NULL DEFAULT 1,
    mode TEXT NOT NULL DEFAULT 'public',        -- public (embeddable widget) | internal_kb (passphrase-gated)
    access_hash TEXT NOT NULL DEFAULT '',       -- hashed shared passphrase for internal_kb bots
    cite_sources INTEGER NOT NULL DEFAULT 0,    -- surface retrieval citations under each answer
    guardrails TEXT NOT NULL DEFAULT '[]',      -- JSON list of hard refusal topics re-asserted every turn
    preset TEXT NOT NULL DEFAULT '',            -- slug of the preset this bot was created from (provenance)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    type TEXT NOT NULL,               -- text | qa | url | file
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ready',   -- ready | error
    error TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    recrawl_hours INTEGER NOT NULL DEFAULT 0,   -- 0 = off (url sources only)
    last_crawled_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_bot ON sources(bot_id);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_bot ON chunks(bot_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    page_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'bot',   -- bot | needs_human | human
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    UNIQUE(bot_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_convos_bot ON conversations(bot_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,               -- user | assistant | agent (human team member)
    content TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',  -- JSON of widget action, if any
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    grounded INTEGER,                 -- assistant msgs: 1 grounded, 0 gap, NULL n/a
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    data TEXT NOT NULL DEFAULT '{}',
    page_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_bot ON leads(bot_id);

CREATE TABLE IF NOT EXISTS redirects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'button'   -- button | auto
);
CREATE INDEX IF NOT EXISTS idx_redirects_bot ON redirects(bot_id);

CREATE TABLE IF NOT EXISTS faq_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,  -- qa source created on approval
    cluster_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faq_entries_bot ON faq_entries(bot_id);

CREATE TABLE IF NOT EXISTS faq_suppressed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    question_norm TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'dismissed',   -- dismissed | approved
    created_at TEXT NOT NULL,
    UNIQUE(bot_id, question_norm)
);
CREATE INDEX IF NOT EXISTS idx_faq_suppressed_bot ON faq_suppressed(bot_id);

-- Global (cross-bot) LLM-call counter, one row per UTC day. Backs the
-- GLOBAL_DAILY_LLM_CALLS runaway-spend cap; day is the PK so the per-message
-- budget check is a single indexed upsert.
CREATE TABLE IF NOT EXISTS llm_usage_daily (
    day TEXT PRIMARY KEY,             -- UTC date, YYYY-MM-DD
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    conversation_id INTEGER,
    question TEXT NOT NULL,
    best_score REAL NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gaps_bot ON gaps(bot_id);
CREATE TABLE IF NOT EXISTS source_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 title TEXT NOT NULL, content TEXT NOT NULL, meta TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
 source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, lease TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_source_job ON work_jobs(source_id)
 WHERE status IN ('queued','running') AND source_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS notification_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE,
 bot_id INTEGER REFERENCES bots(id) ON DELETE CASCADE,
 channel TEXT NOT NULL, destination TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, lease TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_delivery_due ON notification_outbox(status, next_attempt);
CREATE TABLE IF NOT EXISTS rate_limits (
 key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_turns (
 conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL, PRIMARY KEY(conversation_id, request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_turn ON chat_turns(conversation_id)
 WHERE status='running';

"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    path = pathlib.Path(Config.DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added after first release; applied idempotently for existing DBs.
MIGRATIONS = [
    "ALTER TABLE bots ADD COLUMN behavior_config TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE bots ADD COLUMN handoff_enabled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE sources ADD COLUMN recrawl_hours INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sources ADD COLUMN last_crawled_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN status TEXT NOT NULL DEFAULT 'bot'",
    "ALTER TABLE messages ADD COLUMN grounded INTEGER",
    "ALTER TABLE bots ADD COLUMN mode TEXT NOT NULL DEFAULT 'public'",
    "ALTER TABLE bots ADD COLUMN access_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE bots ADD COLUMN cite_sources INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bots ADD COLUMN guardrails TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE bots ADD COLUMN preset TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE gaps ADD COLUMN notified INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sources ADD COLUMN last_attempt_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN assignee TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN reviewed_message_id INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN token_required INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE leads ADD COLUMN stage TEXT NOT NULL DEFAULT 'new'",
    "ALTER TABLE leads ADD COLUMN notes TEXT NOT NULL DEFAULT ''",

]


def init_db():
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.commit()
    finally:
        conn.close()


def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    if not g.get("transaction_depth", 0):
        db.commit()
    lastrowid = cur.lastrowid
    cur.close()
    return lastrowid


def execute_rowcount(sql, params=()):
    """Like execute() but returns the affected-row count (for optimistic claims)."""
    db = get_db()
    cur = db.execute(sql, params)
    if not g.get("transaction_depth", 0):
        db.commit()
    rowcount = cur.rowcount
    cur.close()
    return rowcount


def executemany(sql, seq):
    db = get_db()
    db.executemany(sql, seq)
    if not g.get("transaction_depth", 0):
        db.commit()


def row_to_dict(row):
    return dict(row) if row is not None else None


def json_field(value, default):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


@contextmanager
def transaction():
    """Nested writes share one commit; errors roll back the complete operation."""
    conn = get_db()
    depth = g.get("transaction_depth", 0)
    name = f"studio_tx_{depth}"
    if depth:
        conn.execute(f"SAVEPOINT {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
    g.transaction_depth = depth + 1
    try:
        yield conn
        if depth:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.commit()
    except BaseException:
        if depth:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.rollback()
        raise
    finally:
        g.transaction_depth = depth
