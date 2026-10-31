import copy
import pytest
from fastapi.testclient import TestClient
from test_api import server
from engine import Engine, GameError
from sabotage import route_solution, route_valid, route_trace, REPAIR_TTL
from test_skeld import skeld, station
from puzzle_helpers import answer_for
from test_scans import playing, scan


def start(g, kind):
    return g.sabotage_start(g.code,g.imp,kind)


def panel(g, uid, key):
    return g.sabotage_open(g.code,uid,g.state['sabotage_codes'][key])


def fix_oxygen(g,uid,key):
    grant=panel(g,uid,key)
    return g.sabotage_fix(g.code,uid,grant|{'code':grant['puzzle']['code']})


@pytest.mark.parametrize('kind',['oxygen','reactor'])
def test_critical_deadline_wins_without_client(skeld,kind):
    g=skeld;start(g,kind);assert g.room(g.code)['sabotage']['deadline']==g.clock()+45
    g.test_clock[0]+=44.99;g.tick();assert g.room(g.code)['phase']=='running'
    g.test_clock[0]+=.01;g.tick();r=g.room(g.code)
    assert r['phase']=='ended' and r['winner']=='impostor' and r['end_reason']=='sabotage_'+kind


def test_shared_cooldown_and_roles(skeld):
    g=skeld;target='oxygen'
    with pytest.raises(GameError):g.sabotage_start(g.code,g.crew[0],target)
    start(g,'oxygen')
    with pytest.raises(GameError):start(g,'comms')
    assert fix_oxygen(g,g.crew[0],'oxygen_a')['resolved']
    with pytest.raises(GameError):start(g,'comms')
    # Dead/ejected impostors keep sabotage ability, with the same room cooldown.
    g.room(g.code)['players'][g.imp]['status']='ejected'
    g.test_clock[0]+=90;start(g,'comms');assert g.room(g.code)['sabotage']['kind']=='comms'


def test_unrelated_task_attempt_survives_and_related_task_resets(skeld):
    g=skeld;uid=g.crew[0];other=g.crew[1]
    task=g.task_open(g.code,uid,station(g,'shields'));answer=answer_for(g,task)
    related=g.task_open(g.code,other,station(g,'manifolds'));epoch=g.room(g.code)['epoch']
    start(g,'reactor')
    assert g.room(g.code)['epoch']==epoch
    assert g.room(g.code)['players'][uid]['tasks'][task['station_code']]['id']==task['id']
    assert g.task_finish(g.code,uid,task['station_code'],task['id'],task['epoch'],answer)['done']
    with pytest.raises(GameError,match='саботаж'):g.task_open(g.code,other,station(g,'manifolds'))
    with pytest.raises(GameError):g.task_finish(g.code,other,related['station_code'],related['id'],related['epoch'],{'order':list(range(1,11))})
    g.control(g.code,'host','clear_sabotage');assert g.task_open(g.code,other,station(g,'manifolds'))


def test_oxygen_bound_grants_codes_and_public_state(skeld):
    g=skeld;uid=g.crew[0];start(g,'oxygen');grant=panel(g,uid,'oxygen_a')
    public=g.view(g.code,g.crew[1]);assert 'sabotage_qrs' not in public
    assert 'codes' not in public['sabotage'] and 'access' not in public['sabotage']
    with pytest.raises(GameError):g.sabotage_fix(g.code,g.crew[1],grant|{'code':grant['puzzle']['code']})
    with pytest.raises(GameError):g.sabotage_fix(g.code,uid,grant|{'code':'bad'})
    new=panel(g,uid,'oxygen_b')
    with pytest.raises(GameError):g.sabotage_fix(g.code,uid,grant|{'code':grant['puzzle']['code']})
    assert g.sabotage_fix(g.code,uid,new|{'code':new['puzzle']['code']})['resolved']
    with pytest.raises(GameError):g.sabotage_fix(g.code,uid,new|{'code':new['puzzle']['code']})
    assert g.room(g.code)['sabotage'] is None


def test_ghost_cannot_repair_and_late_fix_rejected(skeld):
    g=skeld;uid=g.crew[0];start(g,'oxygen');grant=panel(g,uid,'oxygen_a')
    g.room(g.code)['players'][uid]['status']='dead'
    with pytest.raises(GameError):g.sabotage_fix(g.code,uid,grant|{'code':grant['puzzle']['code']})
    with pytest.raises(GameError):panel(g,uid,'oxygen_b')
    g.room(g.code)['players'][uid]['status']='alive';g.test_clock[0]+=45
    with pytest.raises(GameError):g.sabotage_fix(g.code,uid,grant|{'code':grant['puzzle']['code']})


def test_reactor_requires_distinct_players_continuous_holds(skeld):
    g=skeld;start(g,'reactor');a,b=g.crew[:2];ga=panel(g,a,'reactor_a');gb=panel(g,b,'reactor_b')
    for _ in range(5):
        g.reactor_hold(g.code,a,ga|{'pressed':True})
        if not g.room(g.code)['sabotage']:break
        g.reactor_hold(g.code,b,gb|{'pressed':True});g.test_clock[0]+=.6
    assert g.room(g.code)['sabotage'] is None


def test_reactor_single_player_two_tabs_and_expired_lease_do_not_complete(skeld):
    g=skeld;start(g,'reactor');a,b=g.crew[:2];ga=panel(g,a,'reactor_a');gb=panel(g,a,'reactor_b')
    with pytest.raises(GameError):g.reactor_hold(g.code,a,ga|{'pressed':True})
    g.reactor_hold(g.code,a,gb|{'pressed':True});assert len(g.room(g.code)['sabotage']['holds'])==1
    ga=panel(g,a,'reactor_a');gb=panel(g,b,'reactor_b')
    g.reactor_hold(g.code,a,ga|{'pressed':True});g.reactor_hold(g.code,b,gb|{'pressed':True})
    # No timer tick between disconnect and heartbeat: late renewal still breaks overlap.
    g.test_clock[0]+=2.1;g.reactor_hold(g.code,a,ga|{'pressed':True});g.reactor_hold(g.code,b,gb|{'pressed':True})
    assert g.room(g.code)['sabotage'] is not None
    g.test_clock[0]+=.6;g.reactor_hold(g.code,a,ga|{'pressed':False})
    assert g.room(g.code)['sabotage']['overlap_since'] is None


def test_pause_freezes_critical_timer_and_revokes_repair_grants(skeld):
    g=skeld;start(g,'reactor');grant=panel(g,g.crew[0],'reactor_a');g.reactor_hold(g.code,g.crew[0],grant|{'pressed':True})
    deadline=g.room(g.code)['sabotage']['deadline'];g.control(g.code,'host','pause');g.test_clock[0]+=100;g.tick()
    assert g.room(g.code)['phase']=='running';g.control(g.code,'host','resume')
    assert g.room(g.code)['sabotage']['deadline']==deadline+100
    assert not g.room(g.code)['sabotage']['holds']
    with pytest.raises(GameError):g.reactor_hold(g.code,g.crew[0],grant|{'pressed':True})


@pytest.mark.parametrize('kind',['comms','oxygen','reactor'])
def test_emergency_blocked_body_report_allowed(skeld,kind):
    g=skeld;victim=g.crew[0];g.room(g.code)['players'][victim]['status']='dead';start(g,kind)
    with pytest.raises(GameError):g.emergency(g.code,g.crew[1])
    g.report(g.code,g.crew[1],g.state['users'][victim]['badge'])
    assert g.room(g.code)['phase']=='gathering'
    assert bool(g.room(g.code)['sabotage'])==(kind=='comms')


def test_comms_route_validates_server_side_and_single_repairer(skeld):
    g=skeld;start(g,'comms');a,b=g.crew[:2];grant=panel(g,a,'comms')
    with pytest.raises(GameError,match='другой игрок'):panel(g,b,'comms')
    for directions in (None, [0]*15, [True]*16, [4]*16, grant['puzzle']['directions']):
        with pytest.raises(GameError):g.sabotage_fix(g.code,a,grant|{'directions':directions})
    assert g.sabotage_fix(g.code,a,grant|{'directions':route_solution(grant['puzzle'])})['resolved']
    assert len(g.state['sabotage_codes'])==8


def test_repair_scan_receipt_is_required_once_and_actor_bound(server):
    c,post,code,imp,crew,clock=playing(server);g=server.engine
    assert post(imp,'sabotage_start',{'kind':'oxygen'}).status_code==200
    assert post(crew[0],'sabotage_open',{}).status_code==400
    repair=scan(post,crew[0],'x_'+g.state['sabotage_codes']['oxygen_a'])['receipt']
    assert post(crew[1],'sabotage_open',{'receipt':repair}).status_code==400
    grant=post(crew[0],'sabotage_open',{'receipt':repair});assert grant.status_code==200
    assert post(crew[0],'sabotage_open',{'receipt':repair}).status_code==400
    c.close()


def test_clear_on_pause_starts_exact_cooldown_after_resume(skeld):
    g=skeld;start(g,'comms');g.control(g.code,'host','pause');g.test_clock[0]+=80
    g.control(g.code,'host','clear_sabotage');g.test_clock[0]+=20;g.control(g.code,'host','resume')
    assert g.room(g.code)['sabotage_ready']==g.clock()+90


def test_sabotage_qrs_are_stable_telegram_links_and_not_public(server):
    s=server;s.cfg.update(mode='telegram',public_url='https://first.trycloudflare.com',bot_username='our_bot')
    s.chat_bot.process('101','Хост','/create');code=s.engine.current_room('101')['code']
    c=TestClient(s.app);headers={'Authorization':'Bearer '+s.issue('101')}
    codes=s.engine.view(code,'101')['sabotage_qrs'];assert len(codes)==5 and all(q['use']=='repair' for q in codes)
    for q in codes:
        result=c.post('/api/qr',json={'payload':q['payload']},headers=headers)
        assert result.status_code==200
        assert result.json()['url']=='https://t.me/our_bot?start='+q['payload']
        assert result.json()['image'].startswith('data:image/svg+xml;base64,')
        s.cfg['public_url']='https://next.trycloudflare.com'
        message=s.chat_bot.process('101','Хост','/start '+q['payload'])[0]
        assert message['reply_markup']['inline_keyboard'][0][0]['web_app']['url']=='https://next.trycloudflare.com?start='+q['payload']
    c.close()


def test_players_on_same_network_do_not_share_reactor_rate_limit(server):
    import time
    c,post,code,imp,crew,clock=playing(server)
    server.attempts[('testclient','game:'+crew[0])]=[time.time()]*240
    assert post(crew[0],'scan',{'payload':'m_'+server.engine.state['meeting_code']}).status_code==400
    assert post(crew[1],'scan',{'payload':'m_'+server.engine.state['meeting_code']}).status_code==200
    c.close()

@pytest.mark.parametrize('kind,first,second',[('comms','comms','comms'),('oxygen','oxygen_a','oxygen_b')])
def test_exclusive_repair_claim_is_atomic(skeld,kind,first,second):
    from concurrent.futures import ThreadPoolExecutor
    g=skeld;start(g,kind)
    def claim(args):
        try:return panel(g,*args)
        except GameError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,[(g.crew[0],first),(g.crew[1],second)]))
    assert sum(r is not None for r in results)==1
    assert len(g.live_repairs(g.room(g.code)))==1


@pytest.mark.parametrize('kind,key',[('comms','comms'),('oxygen','oxygen_a'),('reactor','reactor_a')])
def test_repair_lease_release_and_heartbeat(skeld,kind,key):
    g=skeld;start(g,kind);a,b=g.crew[:2];grant=panel(g,a,key)
    assert g.view(g.code,b)['sabotage']['busy'][key]
    g.test_clock[0]+=8;g.repair_heartbeat(g.code,a,grant)
    g.test_clock[0]+=8
    with pytest.raises(GameError,match='другой игрок'):panel(g,b,key)
    g.repair_release(g.code,b,grant)  # Another actor cannot release the owner's lease.
    with pytest.raises(GameError):panel(g,b,key)
    g.repair_release(g.code,a,grant)
    assert not g.view(g.code,b)['sabotage']['busy'][key]
    replacement=panel(g,b,key)
    with pytest.raises(GameError):g.repair_heartbeat(g.code,a,grant)
    assert g.view(g.code,b)['sabotage']['mine']['panel']==key
    assert replacement['ticket']!=grant['ticket']


def test_oxygen_panels_are_one_exclusive_job_and_one_completion_clears_both(skeld):
    g=skeld;start(g,'oxygen');a,b=g.crew[:2];grant=panel(g,a,'oxygen_a')
    for key in ('oxygen_a','oxygen_b'):
        with pytest.raises(GameError):panel(g,b,key)
    g.sabotage_fix(g.code,a,grant|{'code':grant['puzzle']['code']})
    assert g.view(g.code,b)['sabotage'] is None
    with pytest.raises(GameError):panel(g,b,'oxygen_b')


def test_expired_repair_cannot_renew_or_submit_after_another_claim(skeld):
    g=skeld;start(g,'comms');a,b=g.crew[:2];old=panel(g,a,'comms')
    g.test_clock[0]+=REPAIR_TTL+.01
    assert not g.view(g.code,b)['sabotage']['busy']['comms']
    new=panel(g,b,'comms')
    with pytest.raises(GameError):g.repair_heartbeat(g.code,a,old)
    with pytest.raises(GameError):g.sabotage_fix(g.code,a,old|{'directions':route_solution(old['puzzle'])})
    g.repair_release(g.code,a,old)
    assert g.sabotage_fix(g.code,b,new|{'directions':route_solution(new['puzzle'])})['resolved']


def test_same_actor_new_tab_invalidates_old_grant_but_old_release_is_safe(skeld):
    g=skeld;start(g,'comms');uid=g.crew[0];old=panel(g,uid,'comms');new=panel(g,uid,'comms')
    g.repair_release(g.code,uid,old)
    with pytest.raises(GameError):g.sabotage_fix(g.code,uid,old|{'directions':route_solution(old['puzzle'])})
    assert g.repair_heartbeat(g.code,uid,new)
    assert g.sabotage_fix(g.code,uid,new|{'directions':route_solution(new['puzzle'])})['resolved']


def test_reactor_allows_two_people_but_never_two_on_one_sensor(skeld):
    g=skeld;start(g,'reactor');a,b,c=g.crew[:3];ga=panel(g,a,'reactor_a');gb=panel(g,b,'reactor_b')
    with pytest.raises(GameError):panel(g,c,'reactor_a')
    with pytest.raises(GameError):panel(g,c,'reactor_b')
    g.reactor_hold(g.code,a,ga|{'pressed':True});g.reactor_hold(g.code,b,gb|{'pressed':True})
    g.repair_release(g.code,a,ga)
    assert 'reactor_a' not in g.room(g.code)['sabotage']['holds']
    assert g.room(g.code)['sabotage']['overlap_since'] is None
    assert panel(g,c,'reactor_a')


def test_death_and_body_report_release_noncritical_repairs(skeld):
    g=skeld;g.test_clock[0]+=61;start(g,'comms');a,b=g.crew[:2];grant=panel(g,a,'comms')
    g.kill(g.code,g.imp,g.state['users'][a]['badge'])
    assert not g.room(g.code)['sabotage']['access']
    replacement=panel(g,b,'comms');g.report(g.code,b,g.state['users'][a]['badge'])
    assert g.room(g.code)['sabotage']['kind']=='comms' and not g.room(g.code)['sabotage']['access']


def test_lights_upgrade_preserves_printed_codes_and_converts_active_game(skeld,tmp_path):
    import json
    g=skeld;start(g,'comms');saved=copy.deepcopy(g.state)
    old_start=saved['sabotage_codes'].pop('start_comms');old_repair=saved['sabotage_codes'].pop('comms')
    saved['sabotage_codes'].update(start_lights=old_start,lights=old_repair)
    s=saved['rooms'][g.code]['sabotage'];s.update(kind='lights',switches=[0,1,0,1,0]);s.pop('route')
    import sqlite3
    legacy=tmp_path/'legacy.sqlite3';db=sqlite3.connect(legacy);g.db.backup(db);
    db.execute('UPDATE state SET data=? WHERE id=1',(json.dumps(saved),));db.commit();db.close()
    restored=Engine(legacy,clock=g.clock)
    assert restored.state['sabotage_codes']['start_comms']==old_start
    assert restored.state['sabotage_codes']['comms']==old_repair
    assert len(restored.sabotage_qrs())==5
    assert restored.room(g.code)['sabotage']['kind']=='comms'
    assert restored.inspect_scan(g.code,g.crew[0],'x_'+old_repair)['actions']==['sabotage_open']
    restored.db.close()


def test_comms_blocks_upload_but_preserves_wire_task(skeld):
    g=skeld;uid=g.crew[0];task=g.task_open(g.code,uid,station(g,'wires'));answer=answer_for(g,task)
    start(g,'comms')
    assert g.task_finish(g.code,uid,task['station_code'],task['id'],task['epoch'],answer)['stage']==1
    with pytest.raises(GameError,match='саботаж'):g.task_open(g.code,uid,station(g,'upload_data'))


def test_api_repair_lease_checks_again_after_scan_and_supports_release(server):
    c,post,code,imp,crew,clock=playing(server);g=server.engine
    assert post(imp,'sabotage_start',{'kind':'comms'}).status_code==200
    payload='x_'+g.state['sabotage_codes']['comms'];a,b=crew[:2]
    ra=scan(post,a,payload)['receipt'];rb=scan(post,b,payload)['receipt']
    ga=post(a,'sabotage_open',{'receipt':ra}).json()
    assert post(b,'sabotage_open',{'receipt':rb}).status_code==400
    assert post(b,'repair_heartbeat',ga).status_code==400
    assert post(a,'repair_heartbeat',ga).status_code==200
    assert post(b,'repair_release',ga).status_code==200
    assert post(b,'scan',{'payload':payload}).status_code==400
    assert post(a,'repair_release',ga).status_code==200
    assert post(b,'scan',{'payload':payload}).status_code==200
    c.close()


def test_undo_old_light_event_restores_comms_without_stale_owners(skeld):
    import json
    g=skeld;start(g,'comms');panel(g,g.crew[0],'comms');g.control(g.code,'host','clear_sabotage')
    row=g.db.execute('SELECT id,before_room FROM events WHERE room=? ORDER BY id DESC LIMIT 1',(g.code,)).fetchone()
    old=json.loads(row[1]);old['sabotage'].update(kind='lights',switches=[0,1,0,1,0]);old['sabotage'].pop('route')
    g.db.execute('UPDATE events SET before_room=? WHERE id=?',(json.dumps(old),row[0]));g.db.commit()
    g.undo(g.code,'host');r=g.room(g.code)
    assert r['sabotage']['kind']=='comms' and r['paused_at'] is not None
    assert not r['sabotage']['access'] and not r['sabotage']['holds']

@pytest.mark.parametrize('kind',['comms','oxygen','reactor'])
def test_all_sabotages_start_by_button_without_qr_and_legacy_qr_is_retired(server,kind):
    c,post,code,imp,crew,clock=playing(server);g=server.engine
    assert post(crew[0],'sabotage_start',{'kind':kind}).status_code==400
    result=post(imp,'sabotage_start',{'kind':kind});assert result.status_code==200,result.text
    assert g.room(code)['sabotage']['kind']==kind
    assert post(imp,'sabotage_start',{'kind':'comms'}).status_code==400
    legacy='x_'+g.state['sabotage_codes']['start_'+kind]
    assert 'кноп' in post(imp,'scan',{'payload':legacy}).json()['error']
    assert 'кноп' in c.post('/api/qr',json={'payload':legacy},headers={'Authorization':'Bearer '+server.issue('host',True,True)}).json()['error']
    c.close()


def test_sabotage_button_still_checks_pause_meetings_membership_and_kind(server):
    c,post,code,imp,crew,clock=playing(server);g=server.engine
    for invalid in (None,[],{},'doors'):
        assert post(imp,'sabotage_start',{'kind':invalid}).status_code==400
    assert post('host','sabotage_start',{'kind':'comms'}).status_code==400
    g.control(code,'host','pause')
    assert post(imp,'sabotage_start',{'kind':'comms'}).status_code==400
    g.control(code,'host','resume');g.emergency(code,crew[0])
    for _ in range(3):
        assert post(imp,'sabotage_start',{'kind':'comms'}).status_code==400
        g.control(code,'host','next_phase')
    assert g.room(code)['sabotage'] is None
    c.close()


@pytest.mark.parametrize('stage',[0,1])
@pytest.mark.parametrize('ghost',[False,True])
def test_comms_blocks_both_data_stages_for_living_and_dead_players(skeld,stage,ghost):
    g=skeld;uid=g.crew[0];st=station(g,'upload_data')
    if stage:
        first=g.task_open(g.code,uid,st);answer=answer_for(g,first)
        g.task_finish(g.code,uid,st,first['id'],first['epoch'],answer)
    if ghost:g.room(g.code)['players'][uid]['status']='dead'
    task=g.task_open(g.code,uid,st,ghost=ghost);answer=answer_for(g,task)
    start(g,'comms')
    with pytest.raises(GameError,match='саботаж'):g.task_open(g.code,uid,st,ghost=ghost)
    with pytest.raises(GameError,match='саботаж'):g.task_finish(g.code,uid,st,task['id'],task['epoch'],answer)
    assert g.room(g.code)['players'][uid]['tasks'][st]['stage']==stage
    assert not g.room(g.code)['players'][uid]['tasks'][st]['done']
    g.control(g.code,'host','clear_sabotage')
    reopened=g.task_open(g.code,uid,st,ghost=ghost);assert reopened['stage']==stage

@pytest.mark.parametrize('key',['oxygen_a','oxygen_b'])
def test_any_one_oxygen_panel_fully_resolves_and_cancels_deadline(skeld,key):
    g=skeld;start(g,'oxygen');g.test_clock[0]+=20
    assert fix_oxygen(g,g.crew[0],key)['resolved']
    assert g.room(g.code)['sabotage'] is None
    assert g.room(g.code)['sabotage_ready']==g.clock()+90
    g.test_clock[0]+=30;g.tick();assert g.room(g.code)['phase']=='running'
    for panel_key in ('oxygen_a','oxygen_b'):
        with pytest.raises(GameError):panel(g,g.crew[1],panel_key)


def test_comms_rejects_diagonal_claim_even_when_arrow_values_have_valid_route(skeld):
    g=skeld;start(g,'comms');uid=g.crew[0];grant=panel(g,uid,'comms');answer=route_solution(grant['puzzle'])
    with pytest.raises(GameError,match='Диагональ'):
        g.sabotage_fix(g.code,uid,grant|{'directions':answer,'path':[12,9,6,3]})
    assert g.room(g.code)['sabotage'] is not None
    path=route_trace(grant['puzzle'],answer)
    assert g.sabotage_fix(g.code,uid,grant|{'directions':answer,'path':path})['resolved']


@pytest.mark.parametrize('start,end,directions',[
    (3,4,[1]*16),  # right across the edge must not wrap to the next row
    (4,3,[3]*16),  # left across the edge must not wrap to the previous row
    (12,3,[4]*16), # no diagonal direction values
    (12,3,[0,0,0,0,0,0,0,0,2,0,0,0,0,0,0,0]), # 12 -> 8 -> 12 is a loop
])
def test_route_rejects_wraps_diagonal_values_and_cycles(start,end,directions):
    assert route_trace({'size':4,'start':start,'end':end},directions) is None


def test_phone_route_preview_and_server_agree_on_steps_and_results():
    import json,random,subprocess,shutil
    if not shutil.which('node'):pytest.skip('Node.js is optional and only needed for preview/server agreement tests')
    from pathlib import Path
    rng=random.Random(904);cases=[]
    for size in (2,3,4,6):
        for _ in range(24):
            start,end=rng.sample(range(size*size),2)
            board={'size':size,'start':start,'end':end,'directions':[rng.randrange(4) for _ in range(size*size)]}
            cases.append({'board':board,'directions':board['directions']})
            cases.append({'board':board,'directions':route_solution(board)})
    for start,end,directions in [(3,4,[1]*16),(4,3,[3]*16),(12,3,[4]*16),(12,3,[.5]*16)]:
        cases.append({'board':{'size':4,'start':start,'end':end},'directions':directions})
    path=Path(__file__).resolve().parents[1]/'static'/'route-rules.js'
    script="const fs=require('fs'),vm=require('vm');const ctx={window:{}};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);const cases=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(cases.map(c=>ctx.window.SabotageRoute.trace(c.board,c.directions))));"
    result=subprocess.run(['node','-e',script,str(path)],input=json.dumps(cases),capture_output=True,text=True,check=True)
    preview=json.loads(result.stdout)
    for case,shown in zip(cases,preview):
        expected=route_trace(case['board'],case['directions'])
        assert shown['complete']==(expected is not None)
        if expected is not None:assert shown['path']==expected
        for a,b in zip(shown['path'],shown['path'][1:]):
            ar,ac=divmod(a,case['board']['size']);br,bc=divmod(b,case['board']['size'])
            assert abs(ar-br)+abs(ac-bc)==1


def test_upgrade_clears_old_oxygen_with_one_panel_already_fixed(skeld,tmp_path):
    import sqlite3
    g=skeld;start(g,'oxygen');g.room(g.code)['sabotage']['fixed']=['oxygen_a'];g._save();g.db.commit()
    path=tmp_path/'old-oxygen.sqlite3';db=sqlite3.connect(path);g.db.backup(db);db.close()
    restored=Engine(path,clock=g.clock)
    assert restored.room(g.code)['sabotage'] is None
    assert restored.room(g.code)['sabotage_ready']==g.clock()+90
    restored.db.close()


def test_undo_old_partial_oxygen_uses_one_panel_rule_without_extending_cooldown(skeld):
    import json
    g=skeld;start(g,'oxygen');g.control(g.code,'host','clear_sabotage')
    row=g.db.execute('SELECT id,before_room FROM events WHERE room=? ORDER BY id DESC LIMIT 1',(g.code,)).fetchone()
    old=json.loads(row[1]);old['sabotage']['fixed']=['oxygen_b']
    g.db.execute('UPDATE events SET before_room=? WHERE id=?',(json.dumps(old),row[0]));g.db.commit()
    g.test_clock[0]+=30;g.undo(g.code,'host');r=g.room(g.code)
    assert r['sabotage'] is None and r['paused_at']==g.clock()
    assert r['sabotage_ready']==g.clock()+90
