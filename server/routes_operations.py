"""Workspace operations, training jobs, and the shared inbox."""
import json
from flask import Blueprint, jsonify, request
from . import db, jobs
from .config import Config
from .security import require_admin_api

bp = Blueprint('operations', __name__)


def _bot(bot_id):
    return db.query('SELECT * FROM bots WHERE id=?', (bot_id,), one=True)


def readiness(bot):
    counts = db.query("SELECT count(*) AS total, COALESCE(sum(chunk_count),0) AS chunks, "
                      "COALESCE(sum(error!=''),0) AS errors FROM sources WHERE bot_id=?",
                      (bot['id'],), one=True)
    delivery = bool(bot['lead_webhook_url'] or (bot['lead_alert_email'] and Config.smtp_configured()))
    checks = [
        {'label': 'Add business knowledge', 'done': counts['chunks'] > 0, 'tab': 'training'},
        {'label': 'Set instructions and welcome', 'done': bool(bot['system_prompt'].strip() and bot['welcome_message'].strip()), 'tab': 'behaviour'},
    ]
    if bot['mode'] == 'internal_kb':
        checks.append({'label': 'Protect the knowledge base', 'done': bool(bot['access_hash']), 'tab': 'guardrails'})
    else:
        checks.append({'label': 'Set up lead capture', 'done': bool(bot['lead_rule'].strip()), 'tab': 'leadcapture'})
        checks.append({'label': 'Connect lead notifications', 'done': delivery, 'tab': 'leadcapture'})
    return {'checks': checks, 'completed': sum(c['done'] for c in checks), 'total': len(checks),
            'sources': counts['total'], 'chunks': counts['chunks'], 'source_errors': counts['errors'],
            'configured': all(c['done'] for c in checks), 'mock_mode': Config.MOCK_LLM}


@bp.get('/api/workspace')
@require_admin_api
def workspace():
    bots = db.query('SELECT * FROM bots ORDER BY name COLLATE NOCASE')
    result = []
    for bot in bots:
        item = {'id': bot['id'], 'name': bot['name'], 'mode': bot['mode'], 'color': bot['theme_color'],
                'readiness': readiness(bot)}
        item['waiting'] = db.query("SELECT count(*) n FROM conversations WHERE bot_id=? "
                                  "AND status='needs_human' AND resolved=0", (bot['id'],), one=True)['n']
        result.append(item)
    stats = db.query("SELECT count(*) conversations, COALESCE(sum(status='needs_human' AND resolved=0),0) waiting "
                     "FROM conversations WHERE session_id NOT LIKE 'eval-%'", one=True)
    leads = db.query("SELECT count(*) n, COALESCE(sum(stage='qualified'),0) qualified FROM leads", one=True)
    deliveries = db.query("SELECT count(*) n FROM notification_outbox WHERE status='failed'", one=True)['n']
    return jsonify(bots=result, stats={**dict(stats), 'leads': leads['n'], 'qualified': leads['qualified'],
                                      'failed_deliveries': deliveries})


@bp.get('/api/bots/<int:bot_id>/readiness')
@require_admin_api
def bot_readiness(bot_id):
    bot = _bot(bot_id)
    if not bot:
        return jsonify(error='Bot not found'), 404
    return jsonify(readiness(bot))


@bp.get('/api/inbox')
@require_admin_api
def inbox():
    clauses, params = ["c.session_id NOT LIKE 'eval-%'"], []
    status = request.args.get('status', '')
    if status in ('bot', 'human', 'needs_human'):
        clauses.append('c.status=? AND c.resolved=0'); params.append(status)
    elif status == 'resolved':
        clauses.append('c.resolved=1')
    elif status != 'all':
        clauses.append('c.resolved=0')
    if request.args.get('bot_id', '').isdigit():
        clauses.append('c.bot_id=?'); params.append(int(request.args['bot_id']))
    search = request.args.get('q', '').strip()[:120]
    if search:
        clauses.append('(b.name LIKE ? OR c.page_url LIKE ? OR EXISTS '
                       '(SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.content LIKE ?))')
        params.extend(['%' + search + '%'] * 3)
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify(error='Invalid offset'), 400
    where = ' AND '.join(clauses)
    total = db.query('SELECT count(*) n FROM conversations c JOIN bots b ON b.id=c.bot_id WHERE ' + where,
                     params, one=True)['n']
    rows = db.query('''SELECT c.id,c.bot_id,b.name bot_name,c.status,c.resolved,c.assignee,
        c.page_url,c.last_message_at,
        (SELECT substr(content,1,180) FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1) preview,
        (SELECT count(*) FROM messages WHERE conversation_id=c.id AND role='user' AND id>c.reviewed_message_id) unread
        FROM conversations c JOIN bots b ON b.id=c.bot_id WHERE ''' + where + '''
        ORDER BY (c.status='needs_human') DESC,c.last_message_at DESC,c.id DESC LIMIT 40 OFFSET ?''', (*params, offset))
    return jsonify(conversations=[dict(row) for row in rows], total=total, offset=offset)


@bp.patch('/api/inbox/<int:convo_id>')
@require_admin_api
def update_inbox(convo_id):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error='Expected an object'), 400
    values = {}
    if 'assignee' in body:
        values['assignee'] = str(body['assignee']).strip()[:80]
    if 'resolved' in body:
        if not isinstance(body['resolved'], bool):
            return jsonify(error='resolved must be true or false'), 400
        values['resolved'] = int(body['resolved'])
    if body.get('read'):
        latest = db.query('SELECT COALESCE(max(id),0) n FROM messages WHERE conversation_id=?',
                          (convo_id,), one=True)['n']
        try:
            values['reviewed_message_id'] = min(latest, max(0, int(body.get('read_up_to', latest))))
        except (ValueError, TypeError):
            return jsonify(error='Invalid read cursor'),400
    if not values:
        return jsonify(error='No changes supplied'), 400
    changed = db.execute_rowcount('UPDATE conversations SET ' + ','.join('reviewed_message_id=max(reviewed_message_id,?)' if k=='reviewed_message_id' else k+'=?' for k in values) + ' WHERE id=?',
                                  (*values.values(), convo_id))
    return (jsonify(ok=True), 200) if changed else (jsonify(error='Conversation not found'), 404)


@bp.get('/api/deliveries')
@require_admin_api
def deliveries():
    rows = db.query('''SELECT o.id,o.event_id,o.bot_id,b.name bot_name,o.channel,o.status,o.attempts,
        o.error,o.created_at,o.delivered_at FROM notification_outbox o LEFT JOIN bots b ON b.id=o.bot_id
        ORDER BY CASE o.status WHEN 'failed' THEN 0 WHEN 'pending' THEN 1 WHEN 'sending' THEN 2 ELSE 3 END,
        o.id DESC LIMIT 100''')
    return jsonify(deliveries=[dict(row) for row in rows])


@bp.post('/api/deliveries/<int:delivery_id>/retry')
@require_admin_api
def retry_delivery(delivery_id):
    changed = db.execute_rowcount("UPDATE notification_outbox SET status='pending',attempts=0,next_attempt=0,error='' "
                                  "WHERE id=? AND status='failed'", (delivery_id,))
    if not changed:
        return jsonify(error='Only failed deliveries can be retried'), 409
    return jsonify(ok=True)


@bp.get('/api/bots/<int:bot_id>/jobs')
@require_admin_api
def list_jobs(bot_id):
    rows = db.query('SELECT id,source_id,kind,status,error,created_at,updated_at FROM work_jobs WHERE bot_id=? ORDER BY id DESC LIMIT 30', (bot_id,))
    return jsonify(jobs=[dict(row) for row in rows])


@bp.post('/api/bots/<int:bot_id>/jobs')
@require_admin_api
def create_job(bot_id):
    if not _bot(bot_id):
        return jsonify(error='Bot not found'), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error='Expected an object'), 400
    source_id = body.get('source_id')
    kind = body.get('kind', 'index')
    if kind not in ('index', 'recrawl', 'restore'):
        return jsonify(error='Unknown training operation'), 400
    payload = {}
    if body.get("gap_id"):
        payload["gap_id"] = body["gap_id"]
    if source_id is not None:
        source = db.query('SELECT * FROM sources WHERE id=? AND bot_id=?', (source_id, bot_id), one=True)
        if not source:
            return jsonify(error='Source not found'), 404
        payload['revision'] = source['revision']
        if kind == 'recrawl' and source['type'] != 'url':
            return jsonify(error='Only website sources can be recrawled'), 400
        if kind == 'restore':
            version = db.query('SELECT title,content,meta FROM source_versions WHERE id=? AND source_id=?',
                               (body.get('version_id'), source_id), one=True)
            if not version:
                return jsonify(error='Version not found'), 404
            payload.update(dict(version))
        else:
            if 'content' in body:
                content = str(body['content']).strip()
                if not 10 <= len(content) <= 500_000:
                    return jsonify(error='Content must contain 10 to 500,000 characters'), 400
                payload['content'] = content
            if 'title' in body:
                payload['title'] = str(body['title']).strip()[:200]
        job_id = jobs.enqueue(bot_id, source_id, kind, payload)
    else:
        stype = body.get('type', 'text')
        if stype not in ('text', 'qa', 'url'):
            return jsonify(error='Choose text, Q&A, or a website'), 400
        content = str(body.get('content', '')).strip()
        title = str(body.get('title', '')).strip()[:200]
        meta = {}
        hours = 0
        if stype == 'url':
            from urllib.parse import urlparse
            url = str(body.get('url', '')).strip()
            if urlparse(url).scheme not in ('http', 'https') or not urlparse(url).hostname:
                return jsonify(error='Enter a complete website address'), 400
            try:
                pages = max(1, min(15, int(body.get('max_pages', 5))))
                hours = int(body.get('recrawl_hours', 0))
            except (ValueError, TypeError):
                return jsonify(error='Invalid crawl settings'), 400
            if hours not in (0,24,168):
                return jsonify(error='Invalid recrawl schedule'), 400
            meta = {'url': url, 'max_pages': pages}; title = title or url; kind = 'recrawl'
        elif stype == 'qa':
            question, answer = str(body.get('question', '')).strip(), str(body.get('answer', '')).strip()
            if not question or not answer:
                return jsonify(error='Add both a question and an answer'), 400
            content = f'Q: {question}\nA: {answer}'; title = question[:200]
        if stype != 'url' and not 10 <= len(content) <= 500_000:
            return jsonify(error='Content must contain 10 to 500,000 characters'), 400
        with db.transaction():
            source_id = db.execute('INSERT INTO sources(bot_id,type,title,content,meta,status,recrawl_hours,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                   (bot_id, stype, title or 'Pasted text', content, json.dumps(meta), 'pending', hours, db.now_iso()))
            job_id = jobs.enqueue(bot_id, source_id, kind, payload)
    if not job_id:
        return jsonify(error='This source already has a queued or running job'), 409
    return jsonify(job_id=job_id, source_id=source_id), 202


@bp.get('/api/bots/<int:bot_id>/sources/<int:source_id>')
@require_admin_api
def source_details(bot_id, source_id):
    source = db.query('SELECT * FROM sources WHERE id=? AND bot_id=?', (source_id, bot_id), one=True)
    if not source:
        return jsonify(error='Source not found'), 404
    versions = db.query('SELECT id,title,created_at FROM source_versions WHERE source_id=? ORDER BY id DESC LIMIT 20', (source_id,))
    return jsonify(source=dict(source), versions=[dict(v) for v in versions])


@bp.patch('/api/bots/<int:bot_id>/leads/<int:lead_id>')
@require_admin_api
def update_lead(bot_id, lead_id):
    body = request.get_json(silent=True) or {}
    stage = body.get('stage', 'new')
    if stage not in ('new', 'contacted', 'qualified', 'booked', 'closed'):
        return jsonify(error='Invalid lead stage'), 400
    row = db.query('SELECT notes FROM leads WHERE id=? AND bot_id=?', (lead_id,bot_id),one=True)
    if not row:
        return jsonify(error='Lead not found'),404
    changed = db.execute_rowcount('UPDATE leads SET stage=?,notes=? WHERE bot_id=? AND id=?',
                                  (stage, str(body.get('notes', row['notes']))[:4000], bot_id, lead_id))
    return (jsonify(ok=True), 200) if changed else (jsonify(error='Lead not found'), 404)


@bp.delete('/api/bots/<int:bot_id>/leads/<int:lead_id>')
@require_admin_api
def delete_lead(bot_id, lead_id):
    with db.transaction():
        changed = db.execute_rowcount('DELETE FROM leads WHERE bot_id=? AND id=?', (bot_id, lead_id))
        db.execute("DELETE FROM notification_outbox WHERE bot_id=? AND json_extract(payload,'$.lead_id')=?", (bot_id, lead_id))
    return (jsonify(ok=True), 200) if changed else (jsonify(error='Lead not found'), 404)


@bp.post('/api/bots/<int:bot_id>/jobs/<int:job_id>/retry')
@require_admin_api
def retry_job(bot_id, job_id):
    row = db.query("SELECT * FROM work_jobs WHERE id=? AND bot_id=? AND status='failed'", (job_id,bot_id),one=True)
    if not row:
        return jsonify(error='Failed job not found'),404
    payload = db.json_field(row['payload'],{})
    source = db.query('SELECT revision FROM sources WHERE id=?',(row['source_id'],),one=True)
    if not source:
        return jsonify(error='Source no longer exists'),404
    if 'revision' in payload and payload['revision'] != source['revision']:
        return jsonify(error='Source has changed. Open it and submit a fresh update.'),409
    queued = jobs.enqueue(bot_id,row['source_id'],row['kind'],payload)
    if not queued:
        return jsonify(error='This source already has an active job'),409
    return jsonify(job_id=queued),202
