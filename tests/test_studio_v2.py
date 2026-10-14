"""Regression tests for data loss, private access, durable work, and visitor continuity."""
import json
import time
import uuid
from unittest.mock import Mock
import pytest
from server import db, rag, jobs, delivery, crawler
from server.config import Config
from server.llm import LLMError
from server.security import rate_limited, unlock_token, valid_unlock_token


def bot(client, bot_id):
    return client.get(f'/api/bots/{bot_id}').get_json()['bot']


def seed_source(client, bot_id):
    response = client.post(f'/api/bots/{bot_id}/sources', json={
        'type':'text','title':'Hours','content':'Our office opens at nine on weekdays.'})
    assert response.status_code == 201
    return response.get_json()['source']['id']


def session(client, b):
    data = client.post('/api/widget/session', json={'key':b['embed_key']}).get_json()
    return {'key':b['embed_key'], **data}


def test_failed_index_preserves_content_and_vectors(app,client,bot_id,monkeypatch):
    sid = seed_source(client,bot_id)
    with app.app_context():
        old = dict(db.query('SELECT * FROM sources WHERE id=?',(sid,),one=True))
        chunks = [dict(c) for c in db.query('SELECT * FROM chunks WHERE source_id=?',(sid,))]
        monkeypatch.setattr(rag,'embed_texts',Mock(side_effect=LLMError('Outage')))
        with pytest.raises(LLMError):
            rag.index_source(bot_id,sid,['New content'],{'content':'New content'})
        assert dict(db.query('SELECT * FROM sources WHERE id=?',(sid,),one=True)) == old
        assert [dict(c) for c in db.query('SELECT * FROM chunks WHERE source_id=?',(sid,))] == chunks


@pytest.mark.parametrize('vectors',[[],[[float('nan')]],[[1,2],[1]]])
def test_invalid_embedding_batch_never_publishes(app,client,bot_id,monkeypatch,vectors):
    sid=seed_source(client,bot_id)
    with app.app_context():
        monkeypatch.setattr(rag,'embed_texts',lambda chunks:vectors)
        with pytest.raises(LLMError): rag.index_source(bot_id,sid,['Replacement'])
        assert db.query('SELECT count(*) n FROM chunks WHERE source_id=?',(sid,),one=True)['n'] == 1


def test_source_revision_prevents_lost_update(app,client,bot_id):
    sid=seed_source(client,bot_id)
    with app.app_context():
        with pytest.raises(ValueError): rag.index_source(bot_id,sid,['New text'],expected_revision=0)
        assert db.query('SELECT revision FROM sources WHERE id=?',(sid,),one=True)['revision'] == 1


def test_nested_transaction_rolls_back(app,bot_id):
    with app.app_context():
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute('UPDATE bots SET name=? WHERE id=?',('Should roll back',bot_id))
                with db.transaction(): delivery.enqueue('email','test@example.com',{'subject':'test','body':'test'},bot_id)
                raise RuntimeError('simulate crash')
        assert db.query('SELECT name FROM bots WHERE id=?',(bot_id,),one=True)['name'] != 'Should roll back'
        assert not db.query('SELECT 1 FROM notification_outbox WHERE bot_id=?',(bot_id,))


def test_passphrase_rotation_revokes_all_private_routes(client,bot_id):
    b=bot(client,bot_id)
    client.patch(f'/api/bots/{bot_id}',json={'mode':'internal_kb'})
    client.post(f'/api/bots/{bot_id}/passphrase',json={'passphrase':'first-password'})
    token=client.post('/api/widget/unlock',json={'key':b['embed_key'],'passphrase':'first-password'}).get_json()['unlock_token']
    client.post(f'/api/bots/{bot_id}/passphrase',json={'passphrase':'second-password'})
    payload={'key':b['embed_key'],'unlock_token':token,'session_id':'private-test','message':'hello'}
    assert client.post('/api/widget/chat',json=payload).status_code==401
    assert client.post('/api/widget/lead',json={**payload,'fields':{'name':'Test','email':'a@example.com'}}).status_code==401
    for path in ('updates','history'):
        assert client.get('/api/widget/'+path,query_string={'key':b['embed_key'],'session_id':'private-test','after':'0'},headers={'Authorization':'Bearer '+token}).status_code==401


def test_private_token_expiry(monkeypatch):
    token=unlock_token('pk_test','hash-one')
    assert valid_unlock_token('pk_test',token,'hash-one')
    assert not valid_unlock_token('pk_other',token,'hash-one')
    monkeypatch.setattr(Config,'ACCESS_TOKEN_TTL',-1)
    assert not valid_unlock_token('pk_test',token,'hash-one')


def test_signed_session_history_and_turn_dedup(client,bot_id,app):
    b=bot(client,bot_id); payload=session(client,b)
    message={**payload,'message':'Hello there','request_id':'turn-one'}
    first=client.post('/api/widget/chat',json=message)
    second=client.post('/api/widget/chat',json=message)
    assert first.status_code==second.status_code==200
    assert first.get_json()==second.get_json()
    params={'key':b['embed_key'],'session_id':payload['session_id']}
    assert client.get('/api/widget/history',query_string=params).status_code==401
    good=client.get('/api/widget/history',query_string=params,headers={'X-Conversation-Token':payload['conversation_token']})
    assert good.status_code==200 and len(good.get_json()['messages'])==2
    spoof={**message,'request_id':'turn-two'};spoof.pop('conversation_token')
    assert client.post('/api/widget/chat',json=spoof).status_code==401
    assert client.get('/api/widget/updates',query_string=params).status_code==401
    with app.app_context():
        assert db.query('SELECT count(*) n FROM conversations WHERE bot_id=?',(bot_id,),one=True)['n']==1


def test_stream_replay_does_not_duplicate(client,bot_id):
    payload={**session(client,bot(client,bot_id)),'message':'hello','request_id':'stream-turn'}
    r=client.post('/api/widget/chat/stream',json=payload)
    events=[json.loads(line[5:]) for line in r.get_data(as_text=True).splitlines() if line.startswith('data:')]
    assert events[-1]['done']
    replay=client.post('/api/widget/chat',json=payload)
    assert replay.get_json()['reply']==''.join(e.get('delta','') for e in events)


def test_concurrent_turn_is_rejected(app,client,bot_id):
    payload={**session(client,bot(client,bot_id)),'message':'hello','request_id':'one'}
    client.post('/api/widget/chat',json=payload)
    with app.app_context():
        cid=db.query('SELECT id FROM conversations WHERE bot_id=?',(bot_id,),one=True)['id']
        db.execute("INSERT INTO chat_turns VALUES(?,?,'running','{}',?)",(cid,'busy',time.time()))
    assert client.post('/api/widget/chat',json={**payload,'request_id':'two'}).status_code==409


def test_session_bootstrap_does_not_count_pageviews(app,client,bot_id):
    session(client,bot(client,bot_id))
    with app.app_context():
        assert not db.query('SELECT 1 FROM conversations WHERE bot_id=?',(bot_id,))


def test_training_job_versions_and_restore(app,client,bot_id):
    sid=seed_source(client,bot_id)
    queued=client.post(f'/api/bots/{bot_id}/jobs',json={'source_id':sid,'content':'Our office now opens at ten on weekdays.','title':'New hours'})
    assert queued.status_code==202
    assert client.post(f'/api/bots/{bot_id}/jobs',json={'source_id':sid,'content':'Conflicting hours update'}).status_code==409
    with app.app_context(): assert jobs.run_one()
    details=client.get(f'/api/bots/{bot_id}/sources/{sid}').get_json()
    assert 'ten' in details['source']['content']
    assert len(details['versions'])==1
    restore=client.post(f'/api/bots/{bot_id}/jobs',json={'source_id':sid,'kind':'restore','version_id':details['versions'][0]['id']})
    assert restore.status_code==202
    with app.app_context(): assert jobs.run_one()
    assert 'nine' in client.get(f'/api/bots/{bot_id}/sources/{sid}').get_json()['source']['content']


def test_training_failure_can_be_retried(app,client,bot_id,monkeypatch):
    sid=seed_source(client,bot_id)
    job=client.post(f'/api/bots/{bot_id}/jobs',json={'source_id':sid,'content':'Updated training content'}).get_json()['job_id']
    original=rag.embed_texts
    monkeypatch.setattr(rag,'embed_texts',Mock(side_effect=LLMError('Temporary outage')))
    with app.app_context(): jobs.run_one()
    details=client.get(f'/api/bots/{bot_id}/sources/{sid}').get_json()['source']
    assert details['status']=='ready' and 'nine' in details['content']
    monkeypatch.setattr(rag,'embed_texts',original)
    assert client.post(f'/api/bots/{bot_id}/jobs/{job}/retry').status_code==202
    with app.app_context(): jobs.run_one()
    assert client.get(f'/api/bots/{bot_id}/sources/{sid}').get_json()['source']['content']=='Updated training content'


def test_delivery_retries_and_preserves_event_id(app,bot_id,monkeypatch):
    sent=[]
    def fail(row):
        sent.append(row['event_id']);raise RuntimeError('HTTP 500')
    monkeypatch.setattr(delivery,'send',fail)
    with app.app_context():
        db.execute('DELETE FROM notification_outbox')
        event=delivery.enqueue('webhook','https://example.com',{'event':'test'},bot_id)
        assert delivery.run_due()==1
        row=db.query('SELECT * FROM notification_outbox WHERE event_id=?',(event,),one=True)
        assert row['status']=='pending' and row['attempts']==1 and row['next_attempt']>time.time()
        db.execute('UPDATE notification_outbox SET next_attempt=0 WHERE event_id=?',(event,))
        monkeypatch.setattr(delivery,'send',lambda row:sent.append(row['event_id']))
        delivery.run_due()
        row=db.query('SELECT * FROM notification_outbox WHERE event_id=?',(event,),one=True)
        assert row['status']=='delivered' and sent==[event,event]


def test_webhook_http_errors_are_not_success(monkeypatch):
    monkeypatch.setattr(delivery.requests,'post',lambda *args,**kwargs:Mock(status_code=500))
    with pytest.raises(RuntimeError,match='HTTP 500'):
        delivery.send({'payload':'{}','channel':'webhook','event_id':'event','destination':'https://example.com'})


def test_lead_and_notification_are_atomic(app,client,bot_id,monkeypatch):
    b=bot(client,bot_id)
    client.patch(f'/api/bots/{bot_id}',json={'lead_webhook_url':'https://example.com/hook'})
    monkeypatch.setattr(delivery,'enqueue',Mock(side_effect=RuntimeError('Disk error')))
    with pytest.raises(RuntimeError):
        client.post('/api/widget/lead',json={'key':b['embed_key'],'session_id':'test','fields':{'name':'Test','email':'test@example.com'}})
    with app.app_context(): assert not db.query('SELECT 1 FROM leads WHERE bot_id=?',(bot_id,))


def test_rate_limits_shared_across_contexts(app):
    key=uuid.uuid4().hex
    with app.app_context(): assert not rate_limited(key,1,60)
    with app.app_context(): assert rate_limited(key,1,60)


def test_auth_csrf_and_safe_login_redirect(client,monkeypatch):
    monkeypatch.setattr(Config,'ADMIN_PASSWORD','test-password')
    assert client.get('/api/workspace').status_code==401
    assert client.post('/login?next=https://example.com',data={'password':'test-password'}).location=='/'
    assert client.post('/api/bots',json={'name':'Blocked'}).status_code==403
    with client.session_transaction() as sess: token=sess['csrf_token']
    assert client.post('/api/bots',json={'name':'Allowed'},headers={'X-CSRF-Token':token}).status_code==201


def test_private_network_blocked_even_in_mock_mode(monkeypatch):
    monkeypatch.setattr(Config,'CRAWLER_ALLOW_PRIVATE',False)
    monkeypatch.setattr(Config,'CRAWLER_PRIVATE_ALLOWLIST',frozenset())
    monkeypatch.setattr(crawler.socket,'getaddrinfo',lambda *args:[(None,None,None,None,('127.0.0.1',0))])
    with pytest.raises(crawler.CrawlError): crawler._check_url('http://internal.example')


def test_crawler_checks_redirect_before_second_connection(monkeypatch):
    monkeypatch.setattr(crawler,'_resolve',lambda host:['93.184.216.34'] if host=='example.com' else (_ for _ in ()).throw(crawler.CrawlError('Private target')))
    response=Mock(status=302,headers={'Location':'http://internal.example/'})
    pool=Mock();pool.urlopen.return_value=response
    factory=Mock(return_value=pool)
    monkeypatch.setattr(crawler.urllib3,'HTTPConnectionPool',factory)
    with pytest.raises(crawler.CrawlError):crawler._fetch_http('http://example.com','example.com')
    assert factory.call_count==1


def test_crawler_download_limit(monkeypatch):
    monkeypatch.setattr(crawler,'_resolve',lambda host:['93.184.216.34'])
    response=Mock(status=200,headers={'Content-Type':'text/html'})
    response.stream.return_value=[b'x'*1_000_001,b'x'*1_000_001]
    pool=Mock();pool.urlopen.return_value=response
    monkeypatch.setattr(crawler.urllib3,'HTTPConnectionPool',lambda **kwargs:pool)
    with pytest.raises(crawler.CrawlError,match='download limit'):crawler._fetch_http('http://example.com','example.com')
    response.close.assert_called_once()


def test_inbox_assignment_resolution_and_read_state(app,client,bot_id):
    payload={**session(client,bot(client,bot_id)),'message':'Hello'}
    client.post('/api/widget/chat',json=payload)
    rows=client.get('/api/inbox',query_string={'bot_id':bot_id}).get_json()['conversations']
    assert rows[0]['unread']==1
    cid=rows[0]['id']
    assert client.patch(f'/api/inbox/{cid}',json={'read':True,'assignee':'Alex','resolved':True}).status_code==200
    assert client.get('/api/inbox',query_string={'bot_id':bot_id}).get_json()['conversations']==[]
    resolved=client.get('/api/inbox',query_string={'bot_id':bot_id,'status':'resolved'}).get_json()['conversations'][0]
    assert resolved['assignee']=='Alex' and resolved['unread']==0
    client.post('/api/widget/chat',json={**payload,'message':'One more question'})
    assert client.get('/api/inbox',query_string={'bot_id':bot_id}).get_json()['conversations']


def test_new_routes_and_pages(client,bot_id):
    assert client.get('/').status_code==200
    assert client.get(f'/bots/{bot_id}').status_code==200
    assert client.get('/healthz').get_json()['status']=='ok'
    assert client.get('/api/workspace').status_code==200
    assert client.get(f'/api/bots/{bot_id}/readiness').get_json()['configured'] is False
    assert client.get('/api/deliveries').status_code==200


def test_first_update_of_legacy_source_keeps_original_version(app,client,bot_id):
    sid=seed_source(client,bot_id)
    with app.app_context():
        db.execute('UPDATE sources SET revision=0 WHERE id=?',(sid,))
        rag.index_source(bot_id,sid,['Replacement text'],{'content':'Replacement text'})
        version=db.query('SELECT content FROM source_versions WHERE source_id=?',(sid,),one=True)
        assert 'nine' in version['content']


def test_publish_callback_failure_rolls_back_index(app,client,bot_id):
    sid=seed_source(client,bot_id)
    def fail(conn): raise ValueError('lease lost')
    with app.app_context():
        with pytest.raises(ValueError,match='lease lost'):
            rag.index_source(bot_id,sid,['Replacement text'],{'content':'Replacement text'},publish_hook=fail)
        assert 'nine' in db.query('SELECT content FROM sources WHERE id=?',(sid,),one=True)['content']
        assert 'nine' in db.query('SELECT content FROM chunks WHERE source_id=?',(sid,),one=True)['content']


def test_existing_admin_session_receives_csrf_token(client,monkeypatch):
    monkeypatch.setattr(Config,'ADMIN_PASSWORD','password')
    with client.session_transaction() as sess:
        sess.clear();sess['admin']=True
    assert client.get('/').status_code==200
    with client.session_transaction() as sess: assert sess['csrf_token']


def test_background_file_upload(app,client,bot_id):
    import io
    response=client.post(f'/api/bots/{bot_id}/sources/upload?background=1',data={'file':(io.BytesIO(b'Office hours are nine to five each weekday.'),'hours.txt')})
    assert response.status_code==202
    with app.app_context():jobs.run_one()
    details=client.get(f"/api/bots/{bot_id}/sources/{response.get_json()['source_id']}").get_json()
    assert details['source']['status']=='ready' and details['source']['chunk_count']>0


def test_invalid_public_body_rejected(client):
    for endpoint in ('chat','chat/stream','lead','session','unlock'):
        assert client.post('/api/widget/'+endpoint,json=['invalid']).status_code==400
        assert client.post('/api/widget/'+endpoint,json={'padding':'x'*33000}).status_code==413


def test_expired_worker_lease_can_resume(app,client,bot_id):
    sid=seed_source(client,bot_id)
    with app.app_context():
        jid=jobs.enqueue(bot_id,sid,'index',{'content':'An updated source after worker restart.'})
        db.execute("UPDATE work_jobs SET status='running',updated_at='2000-01-01T00:00:00+00:00',lease='dead' WHERE id=?",(jid,))
        assert jobs.run_one()
        assert db.query('SELECT status FROM work_jobs WHERE id=?',(jid,),one=True)['status']=='succeeded'


def test_delivery_terminal_failure_and_manual_retry(app,client,bot_id,monkeypatch):
    monkeypatch.setattr(delivery,'send',Mock(side_effect=RuntimeError('HTTP 503')))
    with app.app_context():
        db.execute('DELETE FROM notification_outbox')
        event=delivery.enqueue('webhook','https://example.com',{},bot_id)
        db.execute('UPDATE notification_outbox SET attempts=5 WHERE event_id=?',(event,))
        delivery.run_due()
        row=db.query('SELECT * FROM notification_outbox WHERE event_id=?',(event,),one=True)
        assert row['status']=='failed'
    assert client.post(f"/api/deliveries/{row['id']}/retry").status_code==200
    with app.app_context(): assert db.query('SELECT attempts FROM notification_outbox WHERE id=?',(row['id'],),one=True)['attempts']==0


def test_source_cannot_be_edited_through_another_bot(client,bot_id):
    sid=seed_source(client,bot_id)
    other=client.post('/api/bots',json={'name':'Other bot'}).get_json()['bot']['id']
    assert client.get(f'/api/bots/{other}/sources/{sid}').status_code==404
    assert client.post(f'/api/bots/{other}/jobs',json={'source_id':sid,'content':'Cannot edit another bot source'}).status_code==404


def test_lead_deletion_clears_saved_notification_payloads(app,client,bot_id,monkeypatch):
    b=bot(client,bot_id)
    client.patch(f'/api/bots/{bot_id}',json={'lead_webhook_url':'https://example.com'})
    assert client.post('/api/widget/lead',json={'key':b['embed_key'],'session_id':'lead-test','fields':{'name':'Test','email':'test@example.com'}}).status_code==200
    with app.app_context():
        lead=db.query('SELECT id FROM leads WHERE bot_id=?',(bot_id,),one=True)['id']
        assert db.query('SELECT 1 FROM notification_outbox WHERE bot_id=?',(bot_id,))
    assert client.delete(f'/api/bots/{bot_id}/leads/{lead}').status_code==200
    with app.app_context():assert not db.query('SELECT 1 FROM notification_outbox WHERE bot_id=?',(bot_id,))


def test_read_cursor_does_not_hide_unseen_messages(app,client,bot_id):
    payload={**session(client,bot(client,bot_id)),'message':'first'}
    client.post('/api/widget/chat',json=payload)
    with app.app_context():
        cid=db.query('SELECT id FROM conversations WHERE bot_id=?',(bot_id,),one=True)['id']
        cursor=db.query('SELECT max(id) n FROM messages WHERE conversation_id=?',(cid,),one=True)['n']
    client.post('/api/widget/chat',json={**payload,'message':'second'})
    client.patch(f'/api/inbox/{cid}',json={'read':True,'read_up_to':cursor})
    row=client.get('/api/inbox',query_string={'bot_id':bot_id}).get_json()['conversations'][0]
    assert row['unread']==1
    client.patch(f'/api/inbox/{cid}',json={'read':True,'read_up_to':0})
    assert client.get('/api/inbox',query_string={'bot_id':bot_id}).get_json()['conversations'][0]['unread']==1


def test_crawler_accepts_only_www_canonical_alias():
    assert crawler._same_site('www.example.com','example.com')
    assert crawler._same_site('example.com','www.example.com')
    assert not crawler._same_site('attacker-example.com','example.com')
    assert not crawler._same_site('private.example.com','example.com')
