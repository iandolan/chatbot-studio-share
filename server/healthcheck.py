"""Nightly self-check for the things that fail silently.

Two outages ran for weeks without anyone noticing, and neither produced a
single error anyone saw:

* The embedding key was revoked. Every bot kept answering, just ungrounded,
  because retrieval returning nothing looks exactly like a question with no
  good answer.
* `extra_hosts` pinned the sites to a coolify-proxy IP that had changed. Every
  crawl and every weekly re-crawl raised ConnectionError, so sources quietly
  served whatever they last fetched.

What these have in common is that the failure is invisible from the outside:
the site is up, the widget loads, the bot replies. So the check does not ask
"is the service up", it asks "is the service still able to do its job":

    embeddings      can we embed at all?
    stored_vectors  does a bot's own text still retrieve itself?
    sites           can the crawler actually reach each trained host?
    sources         anything stuck in error?
    freshness       is the re-crawl scheduler still running at all?

`stored_vectors` is the one that catches a revoked key even when a fresh
embedding would succeed, because it compares new vectors against the stored
ones. If those were written by a different model, or written as zeroes during
an outage, the score collapses and this fails.

Run it from the container:

    python -m server.healthcheck            # exits non-zero when unhealthy
    python -m server.healthcheck --quiet    # only print when something is wrong
"""
import argparse
import logging
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import db, notify, rag
from .config import Config
from .crawler import CrawlError, crawl

log = logging.getLogger(__name__)

PROBE = "connectivity probe: can this service still embed text"
# A chunk's exact text, re-embedded, must match its own stored vector at
# essentially 1.0. The margin here is for float32 rounding only, nothing else:
# a different model, a different dimension or zeroed vectors all land far
# below. Do not probe with an excerpt of the chunk instead of the whole thing;
# a 40-word opener against a 1200-character chunk honestly scores around 0.67
# and would make this either noisy or useless.
SELF_MATCH_MIN = 0.95
# Only complain once a re-crawl is this many times overdue, so a scheduler that
# ran a little late is not an alert.
STALE_FACTOR = 3


class Report:
    def __init__(self):
        self.checks = OrderedDict()

    def ok(self, name, detail):
        self.checks[name] = (True, detail)

    def fail(self, name, detail):
        self.checks[name] = (False, detail)

    @property
    def healthy(self):
        return all(ok for ok, _ in self.checks.values())

    @property
    def failures(self):
        return [(n, d) for n, (ok, d) in self.checks.items() if not ok]

    def text(self):
        lines = []
        for name, (ok, detail) in self.checks.items():
            lines.append(f"[{'ok  ' if ok else 'FAIL'}] {name}: {detail}")
        return "\n".join(lines)


def _check_embeddings(report):
    try:
        vec = rag.embed_texts([PROBE])[0]
    except Exception as exc:
        report.fail("embeddings", f"{type(exc).__name__}: {exc}")
        return None
    if not vec:
        report.fail("embeddings", "provider returned an empty vector")
        return None
    report.ok("embeddings", f"{len(vec)} dims via {Config.EMBEDDING_PROVIDER}")
    return len(vec)


def _check_stored_vectors(report, dims):
    """Re-embed a stored chunk and match it against its own saved vector.

    Catches the case a plain embed cannot: new vectors fine, stored ones
    written by a different model, a different dimension, or as zeroes during
    an outage.
    """
    if not dims:
        report.fail("stored_vectors", "skipped, embeddings are down")
        return
    bots = db.query("SELECT id, name FROM bots ORDER BY id")
    bad, checked = [], 0
    for bot in bots:
        row = db.query(
            """SELECT c.content FROM chunks c WHERE c.bot_id = ? AND length(c.embedding) = ?
               ORDER BY length(c.content) DESC LIMIT 1""",
            (bot["id"], dims * 4), one=True)
        if row is None:
            n = db.query("SELECT COUNT(*) n FROM chunks WHERE bot_id = ?",
                         (bot["id"],), one=True)["n"]
            if n:
                bad.append(f"{bot['name']}: {n} chunks, none at {dims} dims")
            continue
        probe = row["content"] or ""
        if not probe.strip():
            continue
        try:
            _, best = rag.retrieve(bot["id"], probe, top_k=1)
        except Exception as exc:
            bad.append(f"{bot['name']}: {type(exc).__name__}: {exc}")
            continue
        checked += 1
        if best < SELF_MATCH_MIN:
            bad.append(f"{bot['name']}: own text scored {best:.3f} against its stored vectors")
    if bad:
        report.fail("stored_vectors", "; ".join(bad))
    else:
        report.ok("stored_vectors", f"{checked} bots matched their own text")


def _trained_hosts():
    hosts = OrderedDict()
    import json
    for row in db.query("SELECT meta FROM sources WHERE type = 'url'"):
        try:
            url = json.loads(row["meta"] or "{}").get("url", "")
        except ValueError:
            continue
        host = urlparse(url).hostname
        if host and host not in hosts:
            hosts[host] = url
    return hosts


def _check_sites(report):
    """Fetch one real page per trained host.

    This is the stale-proxy-IP check. Nothing else notices, because a failed
    re-crawl just leaves the previous content in place.
    """
    hosts = _trained_hosts()
    if not hosts:
        report.ok("sites", "no crawled sources")
        return
    broken = []
    for host, url in hosts.items():
        try:
            crawl(url, 1)
        except (CrawlError, ValueError) as exc:
            broken.append(f"{host} ({str(exc)[:90]})")
        except Exception as exc:  # noqa: BLE001 - a health check must not raise
            broken.append(f"{host} ({type(exc).__name__})")
    if broken:
        report.fail("sites", f"{len(broken)} of {len(hosts)} unreachable: " + "; ".join(broken))
    else:
        report.ok("sites", f"all {len(hosts)} hosts reachable")


def _check_sources(report):
    rows = db.query(
        """SELECT b.name, s.id, s.error FROM sources s JOIN bots b ON b.id = s.bot_id
           WHERE s.status = 'error' ORDER BY s.id""")
    if rows:
        report.fail("sources", "; ".join(
            f"{r['name']} #{r['id']}: {(r['error'] or '')[:70]}" for r in rows[:8]))
    else:
        report.ok("sources", "no sources in error")


def _check_freshness(report):
    """Has the scheduler stopped running?

    This is NOT a "last successful crawl" check, and must not be read as one.
    scheduler.run_due_recrawls() stamps last_crawled_at when it CLAIMS a source,
    before the crawl runs, so a failed re-crawl still looks fresh here. Failed
    re-crawls are caught by _check_sources (they flip status to 'error') and by
    _check_sites (it fetches each host for real). What only this check catches
    is the scheduler thread dying: then nothing is claimed, and every scheduled
    source drifts past STALE_FACTOR times its interval.
    """
    now = datetime.now(timezone.utc)
    stale = []
    rows = db.query(
        """SELECT b.name, s.id, s.recrawl_hours, s.last_crawled_at
           FROM sources s JOIN bots b ON b.id = s.bot_id
           WHERE s.type = 'url' AND s.recrawl_hours > 0""")
    for r in rows:
        stamp = (r["last_crawled_at"] or "").strip()
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when > timedelta(hours=r["recrawl_hours"] * STALE_FACTOR):
            stale.append(f"{r['name']} #{r['id']} ({(now - when).days}d)")
    if stale:
        report.fail("freshness",
                    f"scheduler may have stopped: {len(stale)} sources not attempted in "
                    f">{STALE_FACTOR}x their interval: " + "; ".join(stale[:8]))
    else:
        report.ok("freshness", f"scheduler active, {len(rows)} scheduled sources attempted on time")


def run():
    report = Report()
    dims = _check_embeddings(report)
    _check_stored_vectors(report, dims)
    _check_sites(report)
    _check_sources(report)
    _check_freshness(report)
    return report


def _notify(report):
    to_addr = (Config.BUDGET_ALERT_EMAIL or Config.ALERT_CC or "").strip()
    if not to_addr or not Config.smtp_configured():
        log.warning("health check unhealthy but no SMTP/recipient configured")
        return False
    names = ", ".join(n for n, _ in report.failures)
    notify._send_plain_email(
        to_addr,
        f"Chatbot Studio health check FAILED: {names}",
        "The nightly self-check found something wrong.\n\n"
        f"{report.text()}\n\n"
        "These checks exist because this class of fault is invisible from the\n"
        "outside: the widget still loads and the bots still reply, they just\n"
        "stop being grounded in your content.\n\nChatbot Studio",
    )
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chatbot Studio health check")
    parser.add_argument("--quiet", action="store_true",
                        help="print nothing when everything passes")
    parser.add_argument("--no-email", action="store_true",
                        help="report only, do not send the alert")
    args = parser.parse_args(argv)

    from . import create_app
    app = create_app()
    with app.app_context():
        report = run()
        if not report.healthy:
            print(report.text(), file=sys.stderr)
            if not args.no_email:
                _notify(report)
            return 1
        if not args.quiet:
            print(report.text())
        return 0


if __name__ == "__main__":
    sys.exit(main())
