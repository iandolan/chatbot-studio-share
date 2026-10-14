"""Persistent training jobs shared by web workers."""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from . import db, rag
from .crawler import crawl

log = logging.getLogger(__name__)


def enqueue(bot_id, source_id, kind, payload):
    import sqlite3
    now = db.now_iso()
    try:
        with db.transaction():
            return db.execute("INSERT INTO work_jobs(bot_id,source_id,kind,payload,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?)", (bot_id, source_id, kind, json.dumps(payload), now, now))
    except sqlite3.IntegrityError:
        return None


def run_one():
    lease = secrets.token_hex(16)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM work_jobs WHERE status='queued' OR "
                           "(status='running' AND updated_at < ?) ORDER BY id LIMIT 1", (cutoff,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE work_jobs SET status='running', lease=?, updated_at=? WHERE id=?",
                     (lease, db.now_iso(), row["id"]))
    try:
        source = db.query("SELECT * FROM sources WHERE id=? AND bot_id=?",
                          (row["source_id"], row["bot_id"]), one=True)
        if source is None:
            raise ValueError("Source was deleted")
        payload = db.json_field(row["payload"], {})
        revision = payload.get("revision", source["revision"])
        if row["kind"] == "recrawl":
            meta = db.json_field(source["meta"], {})
            result = crawl(meta.get("url", ""), meta.get("max_pages", 5))
            meta["pages"] = result["pages"]
            updates = {"title": result["title"][:200], "content": result["content"][:500_000],
                       "meta": json.dumps(meta), "last_crawled_at": db.now_iso()}
        else:
            updates = {k: payload[k] for k in ("title", "content", "meta") if k in payload}
        content = updates.get("content", source["content"])
        if not content.strip():
            raise ValueError("Source has no readable content")
        chunks = [content] if source["type"] == "qa" else rag.chunk_text(content)
        def publish(conn):
            changed = conn.execute("UPDATE work_jobs SET status='succeeded', error='', updated_at=? "
                "WHERE id=? AND lease=? AND status='running'", (db.now_iso(), row["id"], lease)).rowcount
            if not changed:
                raise ValueError("This job was reclaimed by another worker")
            if payload.get("gap_id"):
                conn.execute("DELETE FROM gaps WHERE id=? AND bot_id=?", (payload["gap_id"], row["bot_id"]))
        rag.index_source(row["bot_id"], source["id"], chunks, updates, revision, publish_hook=publish)
    except Exception as exc:
        log.exception("training job %s failed", row["id"])
        message = str(exc)[:500]
        with db.transaction():
            changed = db.execute_rowcount("UPDATE work_jobs SET status='failed', error=?, updated_at=? "
                                           "WHERE id=? AND lease=?", (message, db.now_iso(), row["id"], lease))
            if changed:
                db.execute("UPDATE sources SET error=?, status=CASE WHEN chunk_count>0 THEN 'ready' "
                           "ELSE 'error' END WHERE id=?", (message, row["source_id"]))
    return True
