import copy
import pytest
from test_api import server, signed
from engine import Engine, GameError
from fastapi.testclient import TestClient


@pytest.fixture
def bots(tmp_path):
    clock=[1000000.0];g=Engine(tmp_path/'bots.sqlite3',clock=lambda:clock[0]);g.test_clock=clock
    g.register('123','Хост');g.code=g.create_room('123','Тест')['code'];g.join(g.code,'123')
    g.ensure_catalog();g.add_bots(g.code,'123',4)
    stations=list(g.state['stations']);g.configure(g.code,'123',{'stations':stations,'tasks':len(stations),'discussion':0,'voting':10})
    g.start(g.code,'123')
    g.imp=next(pid for pid,p in g.room(g.code)['players'].items() if p['role']=='impostor')
    g.botids=[pid for pid,p in g.room(g.code)['players'].items() if p.get('bot')]
    yield g
    g.db.close()


def test_host_adds_only_in_lobby_no_human_impersonation_and_limits(bots):
    g=bots
    with pytest.raises(GameError):g.add_bots(g.code,'123',1)
    with pytest.raises(GameError):g.bot_manual(g.code,'123','123')
    with pytest.raises(GameError):g.bot_manual(g.code,g.botids[0],g.botids[1])
    assert all(not row[0].startswith('bot_') for row in g.db.execute('SELECT uid FROM outbox'))
    view=g.view(g.code,'123');assert view['test_mode'] and sum(p['bot'] for p in view['players'])==4
    code=g.create_room('123','Другая')['code']
    for invalid in (0,11,True,'4'):
        with pytest.raises(GameError):g.add_bots(code,'123',invalid)
    for _ in range(4):g.add_bots(code,'123',10)
    with pytest.raises(GameError):g.add_bots(code,'123',1)


def test_bot_auto_arrives_and_votes_skip_manual_bot_waits(bots):
    g=bots;manual=g.botids[0];g.bot_manual(g.code,'123',manual)
    g.emergency(g.code,'123');g.meeting_join(g.code,'123');g.test_clock[0]+=3;g.tick()
    r=g.room(g.code);assert r['phase']=='gathering' and manual not in r['meeting']['arrived']
    assert all(pid in r['meeting']['arrived'] for pid in g.botids[1:])
    g.meeting_join(g.code,manual);g.tick();assert g.room(g.code)['phase']=='voting'
    g.tick();assert all(g.room(g.code)['meeting']['votes'][pid]=='skip' for pid in g.botids[1:])
    assert manual not in g.room(g.code)['meeting']['votes']


def test_bot_tasks_follow_role_and_ghost_cooldown(bots):
    g=bots;uid=next(pid for pid in g.botids if g.room(g.code)['players'][pid]['role']=='crew')
    if g.imp in g.botids:
        with pytest.raises(GameError):g.bot_action(g.code,'123',g.imp,'task')
    g.room(g.code)['players'][uid]['status']='dead';g.bot_action(g.code,'123',uid,'task')
    assert g.room(g.code)['players'][uid]['ghost_ready']==g.clock()+60
    with pytest.raises(GameError):g.bot_action(g.code,'123',uid,'task')
    g.test_clock[0]+=60;g.bot_action(g.code,'123',uid,'task')
    assert g.progress(g.room(g.code))['done']==2


@pytest.mark.parametrize('kind',['comms','oxygen','reactor'])
def test_bot_repair_helper_uses_normal_rules(bots,kind):
    g=bots;g.sabotage_start(g.code,g.imp,kind)
    a,b=g.botids[:2];g.bot_action(g.code,'123',a,'repair')
    if kind=='reactor':
        grant=g.sabotage_open(g.code,b,g.state['sabotage_codes']['reactor_b'])
        for _ in range(5):
            result=g.reactor_hold(g.code,b,grant|{'pressed':True})
            if result['resolved']:break
            g.test_clock[0]+=.6;g.tick()
            if not g.room(g.code)['sabotage']:break
    assert g.room(g.code)['sabotage'] is None


def test_codes_bots_and_deadlines_persist_but_sensor_holds_do_not(bots,tmp_path):
    g=bots;g.sabotage_start(g.code,g.imp,'reactor')
    grant=g.sabotage_open(g.code,g.botids[0],g.state['sabotage_codes']['reactor_a']);g.reactor_hold(g.code,g.botids[0],grant|{'pressed':True})
    saved=copy.deepcopy(g.state);restored=Engine(tmp_path/'bots.sqlite3',clock=g.clock)
    assert restored.state['sabotage_codes']==saved['sabotage_codes']
    r=restored.room(g.code);assert r['sabotage']['deadline']==saved['rooms'][g.code]['sabotage']['deadline']
    assert r['test_mode'] and r['players'][g.botids[0]]['bot']
    assert not r['sabotage']['holds'] and not r['sabotage']['access']
    restored.db.close()


def test_actual_telegram_host_can_add_and_control_bots_with_scoped_session(server):
    s=server;s.cfg.update(mode='telegram',bot_token='123:test-token',public_url='https://test.example',bot_username='testbot')
    c=TestClient(s.app);host_token=c.post('/api/auth/telegram',json={'initData':signed(s.cfg['bot_token'],uid=123)}).json()['token']
    h={'Authorization':'Bearer '+host_token};g=s.engine;code=g.create_room('123','Тест')['code'];g.join(code,'123');g.ensure_catalog()
    assert c.post(f'/api/rooms/{code}/add_bots',json={'count':4},headers=h).status_code==200
    pid=next(pid for pid in g.room(code)['players'] if pid.startswith('bot_'))
    other=g.create_room('123','Другая')['code']
    res=c.post(f'/api/rooms/{code}/bot_session',json={'player':pid},headers=h);assert res.status_code==200
    b={'Authorization':'Bearer '+res.json()['token']}
    assert c.get('/api/me',headers=b).json()['test_controller']
    assert c.get(f'/api/rooms/{code}',headers=b).status_code==200
    for path,body in [(f'rooms/{code}/add_bots',{'count':1}),(f'rooms/{code}/control',{'action':'end'}),(f'rooms/{code}/bot_session',{'player':'123'}),(f'rooms/{other}/scan',{'payload':'bad'}),('demo/chat',{'text':'/create'}),('stations',{'name':'x','kind':'wires'})]:
        assert c.post('/api/'+path,json=body,headers=b).status_code==400
    assert c.get('/api/settings',headers=b).status_code==400
    assert c.post(f'/api/rooms/{code}/bot_session',json={'player':'123'},headers=h).status_code==400
    assert c.post(f'/api/rooms/{code}/test_objects',json={},headers=b).status_code==200
    assert not g.room(code)['players'][pid]['auto']
    c.close()


def test_test_bot_session_is_revoked_with_admin_rights(server):
    s=server;g=s.engine;g.register('owner','Owner');g.register('helper','Helper');code=g.create_room('owner','Тест')['code'];g.add_bots(code,'owner',1)
    g.room(code)['admins'].append('helper');pid=next(iter(g.room(code)['players']))
    c=TestClient(s.app);h={'Authorization':'Bearer '+s.issue('helper')}
    result=c.post(f'/api/rooms/{code}/bot_session',headers=h,json={'player':pid});assert result.status_code==200
    b={'Authorization':'Bearer '+result.json()['token']};g.room(code)['admins'].remove('helper')
    assert c.get(f'/api/rooms/{code}',headers=b).status_code==400;c.close()

def test_room_cooldown_cannot_be_bypassed_by_second_impostor(bots):
    g=bots;other=next(pid for pid in g.botids if pid!=g.imp)
    g.room(g.code)['players'][other]['role']='impostor';g.room(g.code)['players'][other]['tasks']={}
    g.sabotage_start(g.code,g.imp,'comms')
    with pytest.raises(GameError):g.sabotage_start(g.code,other,'reactor')
    g.control(g.code,'123','clear_sabotage')
    with pytest.raises(GameError):g.sabotage_start(g.code,other,'oxygen')
    g.test_clock[0]+=90;g.sabotage_start(g.code,other,'reactor')
    assert g.room(g.code)['sabotage']['kind']=='reactor'

@pytest.mark.parametrize('kind,key',[('comms','comms'),('oxygen','oxygen_a')])
def test_host_bot_helper_cannot_take_an_occupied_single_player_repair(bots,kind,key):
    g=bots;g.sabotage_start(g.code,g.imp,kind)
    grant=g.sabotage_open(g.code,'123',g.state['sabotage_codes'][key])
    with pytest.raises(GameError,match='другой игрок'):g.bot_action(g.code,'123',g.botids[0],'repair')
    g.repair_release(g.code,'123',grant);g.bot_action(g.code,'123',g.botids[0],'repair')
    assert g.room(g.code)['sabotage'] is None
