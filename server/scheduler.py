"""Background re-crawl scheduler.

A daemon thread wakes periodically and re-crawls URL sources whose
recrawl_hours interval has elapsed. Claims are optimistic UPDATEs on
last_crawled_at, so overlapping workers can't double-crawl a source.
Disable with RUN_SCHEDULER=0 (e.g. in one-off scripts)."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, notify, rag
from .crawler import CrawlError, crawl
from .llm import LLMError

log = logging.getLogger(__name__)

# A gap must sit unnotified this long before the digest fires, so several
# questions from one visitor session batch into a single notification.
GAP_DIGEST_MIN_AGE_MIN = max(0, int(os.environ.get("GAP_DIGEST_MIN_AGE_MINUTES", "60") or 60))

_started = False


def start(app):
    global _started
    if _started or os.environ.get("RUN_SCHEDULER", "1") == "0":
        return
    _started = True
    interval_min = max(5, int(os.environ.get("SCHEDULER_INTERVAL_MINUTES", "30") or 30))
    thread = threading.Thread(target=_loop, args=(app, interval_min),
                              name="recrawl-scheduler", daemon=True)
    thread.start()
    threading.Thread(target=_work_loop, args=(app, "training"), daemon=True, name="training-worker").start()
    threading.Thread(target=_work_loop, args=(app, "delivery"), daemon=True, name="delivery-worker").start()
    log.info("re-crawl scheduler running (checks every %s min)", interval_min)


def _loop(app, interval_min):
    next_maintenance = 0
    while True:
        time.sleep(5)
        if time.monotonic() < next_maintenance:
            continue
        next_maintenance = time.monotonic() + interval_min * 60
        try:
            with app.app_context():
                run_due_recrawls()
        except Exception:
            log.exception("re-crawl cycle failed")
        try:
            with app.app_context():
                run_gap_digests()
        except Exception:
            log.exception("gap-digest cycle failed")


def _work_loop(app, kind):
    while True:
        try:
            with app.app_context():
                if kind == "training":
                    from .jobs import run_one
                    run_one()
                else:
                    from .delivery import run_due
                    run_due(limit=10)
        except Exception:
            log.exception("%s worker failed", kind)
        time.sleep(3)


def run_gap_digests(min_age_minutes=None):
    """Notify owners about unanswered visitor questions, batched per bot.
    The atomic claim (UPDATE ... RETURNING) makes this safe under multiple
    gunicorn workers — only one worker wins each bot's batch. Gaps are
    released back when no channel (webhook/email) is configured, so a
    backlog still delivers once a channel is set up."""
    age = GAP_DIGEST_MIN_AGE_MIN if min_age_minutes is None else min_age_minutes
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=age)).isoformat(timespec="seconds")
    bot_rows = db.query(
        "SELECT DISTINCT bot_id FROM gaps WHERE notified = 0 AND created_at <= ?", (cutoff,))
    sent = 0
    for row in bot_rows:
        bot = db.query("SELECT * FROM bots WHERE id = ?", (row["bot_id"],), one=True)
        if bot is None:
            continue
        with db.transaction() as conn:
            claimed = [dict(g) for g in conn.execute(
                "UPDATE gaps SET notified=1 WHERE bot_id=? AND notified=0 "
                "AND created_at<=? RETURNING id, question", (row["bot_id"], cutoff)).fetchall()]
            if not claimed:
                continue
            if notify.notify_gap_digest(bot, claimed):
                sent += 1
            else:
                conn.executemany("UPDATE gaps SET notified=0 WHERE id=?", [(g["id"],) for g in claimed])
    return sent


def run_due_recrawls():
    """One scheduler cycle. Also callable directly (tests, manual kick)."""
    now = datetime.now(timezone.utc)
    rows = db.query(
        "SELECT id, bot_id, meta, recrawl_hours, last_crawled_at, last_attempt_at FROM sources "
        "WHERE type = 'url' AND recrawl_hours > 0"
    )
    ran = 0
    for row in rows:
        due = True
        if row["last_crawled_at"]:
            try:
                last = datetime.fromisoformat(row["last_crawled_at"])
                due = now >= last + timedelta(hours=row["recrawl_hours"])
            except ValueError:
                due = True
        if row["last_attempt_at"]:
            try:
                due = due and now >= datetime.fromisoformat(row["last_attempt_at"]) + timedelta(minutes=30)
            except ValueError:
                pass
        if not due:
            continue
        from . import jobs
        job = jobs.enqueue(row["bot_id"], row["id"], "recrawl", {})
        if job:
            db.execute("UPDATE sources SET last_attempt_at=? WHERE id=?", (db.now_iso(), row["id"]))
            ran += 1
    if ran:
        log.info("re-crawled %s source(s)", ran)
    return ran


def _recrawl_source(row):
    meta = db.json_field(row["meta"], {})
    url = meta.get("url", "")
    try:
        result = crawl(url, meta.get("max_pages", 5))
        meta["pages"] = result["pages"]
        rag.index_source(row["bot_id"], row["id"], rag.chunk_text(result["content"]), updates={
            "title": result["title"][:200], "content": result["content"][:500_000],
            "meta": json.dumps(meta), "last_crawled_at": db.now_iso(),
        })
        log.info("re-crawled source %s (%s)", row["id"], url)
    except (CrawlError, LLMError, ValueError) as exc:
        db.execute("UPDATE sources SET status = 'error', error = ? WHERE id = ?",
                   (f"Scheduled re-crawl failed: {exc}"[:500], row["id"]))
        log.warning("scheduled re-crawl of source %s (%s) failed: %s", row["id"], url, exc)
