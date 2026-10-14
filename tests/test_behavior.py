import json
import pytest
from server import behavior, chat, db, screen
from conftest import seed_conversation


@pytest.mark.parametrize('name', list(behavior.TEMPLATES))
def test_templates_roundtrip(client, bot_id, name):
    config = behavior.template(name)
    response = client.patch(f'/api/bots/{bot_id}', json={'behavior_config': config})
    assert response.status_code == 200
    assert client.get(f'/api/bots/{bot_id}').json['bot']['behavior_config'] == config


@pytest.mark.parametrize('patch', [ {'enabled':'yes'}, {'template':[]}, {'response_length':None}, {'questions':['x']*9}, {'allowed_actions':['refund']}, {'boundaries':'no'}, {'extra':True} ])
def test_invalid_config_rejected(client, bot_id, patch):
    cfg = {**behavior.template('general'), **patch}
    assert client.patch(f'/api/bots/{bot_id}', json={'behavior_config':cfg}).status_code == 400


def test_preview_does_not_save(client, bot_id):
    cfg = behavior.template('personal_injury')
    r = client.post(f'/api/bots/{bot_id}/behavior-preview', json={'behavior_config':cfg})
    assert r.status_code == 200
    assert 'lead_form' not in r.json['actions']
    assert r.json['warnings']
    assert not client.get(f'/api/bots/{bot_id}').json['bot']['behavior_config']['enabled']


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('kind', ['lead_form', 'handoff', 'redirect'])
def test_denied_actions_never_execute(app, client, bot_id, monkeypatch, stream, kind):
    cfg = behavior.template('general'); cfg['allowed_actions'] = []
    client.patch(f'/api/bots/{bot_id}', json={'behavior_config':cfg, 'lead_rule':'On request'})
    convo_id = seed_conversation(app, bot_id, [])
    text = 'Here is help.\nACTION: '+json.dumps({'type':kind,'url':'https://example.com/help'})
    monkeypatch.setattr(screen, 'screen', lambda *a:None)
    monkeypatch.setattr(chat, '_finalize_gaps', lambda *a:None)
    monkeypatch.setattr(chat, 'chat_completion', lambda *a, **kw:{'text':text,'tokens_in':0,'tokens_out':0})
    monkeypatch.setattr(chat, '_split_stream', lambda *a:iter([('text','Here is help.'),('action_raw',text.split('\n')[1]),('usage',(0,0))]))
    monkeypatch.setattr(chat, 'chat_completion_stream', lambda *a, **kw:iter([]))
    with app.app_context():
        bot = db.query('SELECT * FROM bots WHERE id=?',(bot_id,),one=True)
        db.execute('INSERT INTO redirects(bot_id,title,url,description,mode) VALUES(?,?,?,?,?)',(bot_id,'Help','https://example.com/help','','button'))
        result = list(chat.run_turn_stream(bot,convo_id,'Help'))[-1] if stream else chat.run_turn(bot,convo_id,'Help')
        assert result['action'] is None
        assert db.query('SELECT status FROM conversations WHERE id=?',(convo_id,),one=True)['status']=='bot'
        assert not db.query('SELECT * FROM notification_outbox WHERE bot_id=?',(bot_id,))


def test_profile_prompt_and_state(client, bot_id):
    bot = client.get(f'/api/bots/{bot_id}').json['bot']
    bot['behavior_config'] = behavior.template('personal_injury')
    bot['lead_rule'] = 'On request'
    assert 'lead_form' in behavior.allowed(bot)
    assert 'lead_form' not in behavior.allowed(bot, submitted=True)
    assert 'handoff' not in behavior.allowed(bot, status='needs_human')
    prompt = chat.build_system_prompt(bot, [], [], False, 'bot')
    assert 'Do not give legal advice' in prompt
    assert 'one at a time' in prompt


def test_screen_supports_database_row(app, bot_id, monkeypatch):
    monkeypatch.setattr(screen.jev, 'available', lambda:True)
    seen = {}
    def decide(state, questions, **kwargs):
        seen.update(kwargs);seen.update(state)
        return None
    monkeypatch.setattr(screen.jev, 'decide', decide)
    with app.app_context():
        row=db.query('SELECT * FROM bots WHERE id=?',(bot_id,),one=True)
        assert screen.screen(row,'Hello') is None
    assert seen['timeout']==0.5
    assert seen['assistant_is_for']


def test_disabled_capture_rejects_direct_submission(client, bot_id):
    cfg = behavior.template('general'); cfg['lead_capture']='off'
    bot=client.patch(f'/api/bots/{bot_id}',json={'behavior_config':cfg}).json['bot']
    session=client.post('/api/widget/session',json={'key':bot['embed_key']}).json
    response=client.post('/api/widget/lead',json={**session,'key':bot['embed_key'],'fields':{}})
    assert response.status_code==403


def test_migration_repeat_preserves_profile(app, client, bot_id):
    cfg=behavior.template('ecommerce')
    client.patch(f'/api/bots/{bot_id}',json={'behavior_config':cfg})
    with app.app_context():
        db.init_db(); db.init_db()
    assert client.get(f'/api/bots/{bot_id}').json['bot']['behavior_config']==cfg
