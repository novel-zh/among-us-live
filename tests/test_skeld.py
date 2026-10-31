import concurrent.futures
import copy
import time

import pytest

from engine import Engine, GameError
from task_rules import TASKS
from puzzle_helpers import answer_for
from test_scans import playing, scan, badge
from test_api import server
from fastapi.testclient import TestClient


@pytest.fixture
def skeld(tmp_path):
    now = [time.time()]
    g = Engine(tmp_path/'game.sqlite3', clock=lambda: now[0])
    g.test_clock = now
    g.ensure_catalog()
    g.register('host', 'Ведущий')
    g.code = g.create_room('host','Skeld')['code']
    for i in range(5):
        g.register(str(i+1),'Игрок '+str(i+1)); g.join(g.code,str(i+1))
    stations=list(g.state['stations'])
    g.configure(g.code,'host',{'stations':stations,'tasks':len(stations),'discussion':10,'voting':10})
    g.start(g.code,'host')
    g.imp=next(p['id'] for p in g.room(g.code)['players'].values() if p['role']=='impostor')
    g.crew=[p['id'] for p in g.room(g.code)['players'].values() if p['role']=='crew']
    yield g
    g.db.close()


def station(g, kind):
    return next(s['code'] for s in g.state['stations'].values() if s['kind']==kind)


def finish(g, uid, code, ghost=False):
    task=g.task_open(g.code,uid,code,ghost=ghost)
    answer=answer_for(g,task)
    return g.task_finish(g.code,uid,code,task['id'],task['epoch'],answer)


def all_arrive(g):
    for uid,p in list(g.room(g.code)['players'].items()):
        if p['status']=='alive': g.meeting_join(g.code,uid)


@pytest.mark.parametrize('kind',list(TASKS))
def test_every_skeld_task_validates_each_stage_and_counts_once(skeld,kind):
    g=skeld; uid=g.crew[0]; code=station(g,kind)
    stages=TASKS[kind]['stages']
    for index in range(len(stages)):
        task=g.task_open(g.code,uid,code+('_p'+str(index+1) if len(stages)>1 else ''))
        assert task['stage']==index and task['stage_count']==len(stages)
        with pytest.raises(GameError): g.task_finish(g.code,uid,code,task['id'],task['epoch'],{'invalid':True})
        answer=answer_for(g,task)
        result=g.task_finish(g.code,uid,code,task['id'],task['epoch'],answer)
        assert result['done']==(index==len(stages)-1)
        assert result['progress']['done']==(1 if result['done'] else 0)
        with pytest.raises(GameError): g.task_finish(g.code,uid,code,task['id'],task['epoch'],answer)
    with pytest.raises(GameError,match='уже выполнено'): g.task_open(g.code,uid,code)
    assert g.view(g.code,g.imp)['me']['tasks']==[]


def test_stage_qr_enforces_order_and_catalog_is_additive(skeld):
    g=skeld;code=station(g,'wires');uid=g.crew[0]
    with pytest.raises(GameError,match='этап 1'):g.inspect_scan(g.code,uid,'s_'+code+'_p2')
    finish(g,uid,code+'_p1')
    with pytest.raises(GameError,match='этап 2'):g.task_open(g.code,uid,code+'_p1')
    assert g.task_open(g.code,uid,code)['stage']==1  # Old universal QR still works.
    before=copy.deepcopy(g.state['stations']);g.ensure_catalog()
    assert g.state['stations']==before and len(before)==19


def test_meeting_blocks_all_gameplay_resets_attempt_but_preserves_completed_stages(skeld):
    g=skeld;uid=g.crew[0];code=station(g,'wires')
    finish(g,uid,code)
    attempt=g.task_open(g.code,uid,code);answer=answer_for(g,attempt)
    g.emergency(g.code,g.crew[1]);r=g.room(g.code)
    assert r['phase']=='gathering' and r['players'][uid]['tasks'][code]['stage']==1
    assert r['players'][uid]['tasks'][code]['started'] is None
    g.test_clock[0]+=1000;g.tick();assert r['phase']=='gathering'
    for fn in (lambda:g.kill(g.code,g.imp,g.state['users'][uid]['badge']),
               lambda:g.task_open(g.code,uid,code),lambda:g.task_finish(g.code,uid,code,attempt['id'],attempt['epoch'],answer)):
        with pytest.raises(GameError):fn()
    all_arrive(g);assert g.room(g.code)['phase']=='discussion'
    g.control(g.code,'host','next_phase');g.control(g.code,'host','next_phase')
    with pytest.raises(GameError,match='устарело'):g.task_finish(g.code,uid,code,attempt['id'],attempt['epoch'],answer)
    assert finish(g,uid,code)['stage']==2


def test_arrivals_exclude_dead_then_reveal_them_as_nonvoters(skeld):
    g=skeld;dead=g.crew[0];g.test_clock[0]+=60
    g.kill(g.code,g.imp,g.state['users'][dead]['badge'])
    g.emergency(g.code,g.crew[1]);r=g.room(g.code)
    view=g.view(g.code,g.crew[1])
    assert view['meeting']['required']==4 and view['meeting']['participants']==[]
    g.meeting_join(g.code,g.crew[1]);g.meeting_join(g.code,g.crew[1])
    view=g.view(g.code,g.crew[1]);assert view['meeting']['arrived_count']==1
    assert [p['id'] for p in view['meeting']['participants']]==[g.crew[1]]
    with pytest.raises(GameError):g.meeting_join(g.code,dead)
    with pytest.raises(GameError):g.vote(g.code,g.crew[1],r['meeting']['id'],g.imp)
    for uid,p in r['players'].items():
        if p['status']=='alive' and uid!=g.crew[1]:g.meeting_join(g.code,uid)
    assert g.room(g.code)['phase']=='discussion'
    view=g.view(g.code,g.crew[1]);assert len(view['meeting']['participants'])==5
    assert next(p for p in view['meeting']['participants'] if p['id']==dead)['status']=='dead'
    g.control(g.code,'host','next_phase')
    with pytest.raises(GameError):g.vote(g.code,g.crew[1],r['meeting']['id'],dead)
    with pytest.raises(GameError):g.vote(g.code,dead,r['meeting']['id'],'skip')


def test_only_killed_crew_can_open_without_qr_and_cooldown_survives_reload(skeld):
    g=skeld;uid=g.crew[0];a=station(g,'manifolds');b=station(g,'shields')
    with pytest.raises(GameError,match='только убитым'):g.task_open(g.code,uid,a,ghost=True)
    with pytest.raises(GameError,match='Предатели'):g.task_open(g.code,g.imp,a,ghost=True)
    alive_attempt=g.task_open(g.code,uid,a)
    g.test_clock[0]+=60;g.kill(g.code,g.imp,g.state['users'][uid]['badge'])
    with pytest.raises(GameError,match='устарело'):g.task_finish(g.code,uid,a,alive_attempt['id'],alive_attempt['epoch'],list(range(1,11)))
    result=finish(g,uid,a,ghost=True);assert result['ghost_ready']==g.clock()+60
    g.test_clock[0]+=59.9
    with pytest.raises(GameError,match='минуту'):g.task_open(g.code,uid,b,ghost=True)
    path=g.db.execute('PRAGMA database_list').fetchone()[2]
    loaded=Engine(path,clock=g.clock)
    try:
        with pytest.raises(GameError):loaded.task_open(g.code,uid,b,ghost=True)
    finally:loaded.db.close()
    g.test_clock[0]+=.1
    assert finish(g,uid,b,ghost=True)['done']


def test_ghost_cannot_finish_preopened_parallel_attempt_or_skip_cooldown(skeld):
    g=skeld;uid=g.crew[0];g.test_clock[0]+=60
    g.kill(g.code,g.imp,g.state['users'][uid]['badge'])
    a=station(g,'manifolds');b=station(g,'shields')
    stale=g.task_open(g.code,uid,a,ghost=True)
    current=g.task_open(g.code,uid,b,ghost=True)
    g.task_finish(g.code,uid,b,current['id'],current['epoch'],{'sectors':[1]*7})
    with pytest.raises(GameError):g.task_finish(g.code,uid,a,stale['id'],stale['epoch'],list(range(1,11)))
    assert g.progress(g.room(g.code))['done']==1


def test_minute_kill_cooldown_and_pause_freezes_ghost_timer(skeld):
    g=skeld;uid=g.crew[0];badge_value=g.state['users'][uid]['badge']
    assert g.room(g.code)['settings']['cooldown']==60
    g.test_clock[0]+=59
    with pytest.raises(GameError):g.kill(g.code,g.imp,badge_value)
    g.test_clock[0]+=1;g.kill(g.code,g.imp,badge_value)
    assert g.room(g.code)['players'][g.imp]['kill_ready']==g.clock()+60
    finish(g,uid,station(g,'shields'),ghost=True)
    g.control(g.code,'host','pause');g.test_clock[0]+=180;g.control(g.code,'host','resume')
    assert g.view(g.code,uid)['me']['ghost_ready']==g.clock()+60


def test_sample_wait_cannot_be_skipped_and_survives_meeting(skeld):
    g=skeld;uid=g.crew[0];code=station(g,'inspect_sample')
    finish(g,uid,code);task=g.task_open(g.code,uid,code)
    with pytest.raises(GameError,match='исследуется'):g.task_finish(g.code,uid,code,task['id'],task['epoch'],{'choice':task['puzzle']['anomaly']})
    ready=task['ready_at'];g.emergency(g.code,uid)
    assert g.room(g.code)['players'][uid]['tasks'][code]['ready_at']==ready
    all_arrive(g);g.control(g.code,'host','cancel_meeting')
    g.test_clock[0]=ready;assert finish(g,uid,code)['done']


def test_meeting_arrival_requires_new_bound_scan_receipt(server):
    c,post,code,imp,crew,clock=playing(server)
    payload='m_'+server.engine.state['meeting_code']
    call=scan(post,crew[0],payload)['receipt']
    assert post(crew[0],'emergency',{'receipt':call}).status_code==200
    assert post(crew[0],'meeting_join',{'receipt':call}).status_code==400
    assert post(crew[0],'meeting_join',{}).status_code==400
    arrival=scan(post,crew[0],payload)
    assert arrival['actions']==['meeting_join']
    assert post(crew[1],'meeting_join',{'receipt':arrival['receipt']}).status_code==400
    assert post(crew[0],'meeting_join',{'receipt':arrival['receipt']}).status_code==200
    assert post(crew[0],'meeting_join',{'receipt':arrival['receipt']}).status_code==400
    assert post(imp,'kill',{'badge':badge(server,crew[1])}).status_code==400
    for uid in [imp]+crew[1:]:
        receipt=scan(post,uid,payload)['receipt']
        assert post(uid,'meeting_join',{'receipt':receipt}).status_code==200
    assert server.engine.room(code)['phase']=='discussion'
    c.close()


def test_live_player_and_impostor_cannot_use_http_ghost_shortcut(server):
    c,post,code,imp,crew,clock=playing(server)
    st=next(iter(server.engine.room(code)['players'][crew[0]]['tasks']))
    assert post(crew[0],'ghost_task_open',{'station':st}).status_code==400
    assert post(imp,'ghost_task_open',{'station':st}).status_code==400
    server.engine.kill(code,imp,badge(server,crew[0]))
    assert post(crew[0],'ghost_task_open',{'station':st}).status_code==200
    c.close()


def test_printable_stage_qr_is_stable_and_validates_stage_range(server):
    s=server;c=TestClient(s.app)
    owner={'Authorization':'Bearer '+s.issue('host',True,True)}
    fuel=next(st['code'] for st in s.engine.state['stations'].values() if st['kind']=='fuel_engines')
    for stage in range(1,5):
        result=c.post('/api/qr',headers=owner,json={'payload':f's_{fuel}_p{stage}'})
        assert result.status_code==200 and result.json()['url'].endswith(f'start=s_{fuel}_p{stage}')
    assert c.post('/api/qr',headers=owner,json={'payload':f's_{fuel}_p5'}).status_code==400
    s.cfg.update(mode='telegram',bot_username='skeld_test_bot',public_url='https://first.example.com')
    first=s.deep_link(f's_{fuel}_p4');s.cfg['public_url']='https://second.example.com'
    assert s.deep_link(f's_{fuel}_p4')==first
    c.close()


def test_upgrade_preserves_existing_room_badges_and_single_panel_round(skeld):
    g=skeld;code=g.code;uid=g.crew[0];wire=station(g,'wires')
    old_badge=g.state['users'][uid]['badge']
    old_attempt=g.room(code)['players'][uid]['tasks'][wire]['id']
    g.state.pop('schema_version')
    r=g.room(code);r['settings']['cooldown']=30
    for p in r['players'].values():
        p.pop('ghost_ready',None);p.pop('death_id',None)
        for task in p['tasks'].values():task.pop('stage',None);task.pop('stage_count',None)
    g._save();g.db.commit()
    loaded=Engine(g.db.execute('PRAGMA database_list').fetchone()[2],clock=g.clock)
    try:
        assert loaded.room(code)['phase']=='running'
        assert loaded.room(code)['settings']['cooldown']==60
        assert loaded.state['users'][uid]['badge']==old_badge
        assert loaded.room(code)['players'][uid]['tasks'][wire]['id']==old_attempt
        assert loaded.room(code)['players'][uid]['tasks'][wire]['stage_count']==1
        loaded.ensure_catalog()
        assert len(loaded.state['stations'])==19
    finally:loaded.db.close()
