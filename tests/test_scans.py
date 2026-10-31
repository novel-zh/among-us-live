import time

from fastapi.testclient import TestClient

# Reuse the isolated application fixture without importing a production database.
from test_api import server
from puzzle_helpers import answer_for


def playing(s):
    g = s.engine
    clock = [time.time()]
    g.clock = lambda: clock[0]
    g.test_clock = clock
    g.register('host', 'Ведущий')
    code = g.create_room('host', 'Сканирование')['code']
    for n in range(1, 6):
        uid = f'demo_{n}'
        g.register(uid, 'Игрок ' + str(n))
        g.join(code, uid)
    stations = [g.station('Станция ' + str(n), k)['code'] for n, k in enumerate(('wires', 'sequence', 'tuning'))]
    g.configure(code, 'host', {'stations': stations, 'tasks': 2, 'cooldown': 5})
    g.start(code, 'host')
    clock[0] += 6
    players = list(g.room(code)['players'].values())
    imp = next(p['id'] for p in players if p['role'] == 'impostor')
    crew = [p['id'] for p in players if p['role'] == 'crew']
    c = TestClient(s.app)
    def post(uid, action, body):
        return c.post(f'/api/rooms/{code}/{action}', json=body,
                      headers={'Authorization': 'Bearer ' + s.issue(uid, False, True)})
    return c, post, code, imp, crew, clock


def badge(s, uid):
    return s.engine.state['users'][uid]['badge']


def scan(post, uid, payload):
    result = post(uid, 'scan', {'payload': payload})
    assert result.status_code == 200, result.text
    return result.json()


def test_old_shortcuts_require_scan(server):
    c, post, code, imp, crew, _ = playing(server)
    for action, data in (('kill', {'badge': badge(server, crew[0])}), ('report', {'badge': badge(server, crew[0])}), ('emergency', {}),
                         ('task_open', {'station': next(iter(server.engine.room(code)['players'][crew[0]]['tasks']))})):
        res = post(imp if action == 'kill' else crew[0], action, data)
        assert res.status_code == 400
        assert 'отсканируйте' in res.json()['error']
    assert server.engine.room(code)['players'][crew[0]]['status'] == 'alive'
    assert server.engine.room(code)['phase'] == 'running'
    c.close()


def test_meeting_station_qr_required_and_not_exposed(server):
    c, post, code, _, crew, _ = playing(server)
    view = server.engine.view(code, crew[0])
    assert 'meeting_code' not in view
    scanned = scan(post, crew[0], 'm_' + server.engine.state['meeting_code'])
    assert scanned['actions'] == ['emergency']
    assert post(crew[0], 'emergency', {'receipt': scanned['receipt']}).status_code == 200
    assert server.engine.room(code)['phase'] == 'gathering'
    assert post(crew[0], 'emergency', {'receipt': scanned['receipt']}).status_code == 400
    c.close()


def test_body_requires_its_badge_then_confirmation(server):
    c, post, code, imp, crew, _ = playing(server)
    target, observer = crew[:2]
    alive = scan(post, observer, 'b_' + badge(server, target))
    assert alive['actions'] == []
    assert post(observer, 'report', {'receipt': alive['receipt']}).status_code == 400
    kill = scan(post, imp, 'b_' + badge(server, target))
    assert post(imp, 'kill', {'receipt': kill['receipt']}).status_code == 200
    body = scan(post, observer, 'b_' + badge(server, target))
    assert body['kind'] == 'body' and body['actions'] == ['report']
    # Merely scanning does not publicly report it or begin a meeting.
    assert server.engine.room(code)['phase'] == 'running'
    assert post(observer, 'report', {'receipt': body['receipt']}).status_code == 200
    assert server.engine.room(code)['meeting']['body'] == target
    c.close()


def test_receipt_bound_to_actor_action_target_and_one_use(server):
    c, post, code, imp, crew, _ = playing(server)
    receipt = scan(post, imp, 'b_' + badge(server, crew[0]))['receipt']
    assert post(crew[1], 'kill', {'receipt': receipt}).status_code == 400
    assert post(imp, 'report', {'receipt': receipt}).status_code == 400
    # Client cannot swap the QR target by supplying a different badge.
    assert post(imp, 'kill', {'receipt': receipt, 'badge': badge(server, crew[1])}).status_code == 200
    ps = server.engine.room(code)['players']
    assert ps[crew[0]]['status'] == 'dead' and ps[crew[1]]['status'] == 'alive'
    assert post(imp, 'kill', {'receipt': receipt}).status_code == 400
    c.close()


def test_expired_and_superseded_scans_rejected(server):
    c, post, code, imp, crew, _ = playing(server)
    old = scan(post, imp, 'b_' + badge(server, crew[0]))['receipt']
    current = scan(post, imp, 'b_' + badge(server, crew[1]))['receipt']
    assert post(imp, 'kill', {'receipt': old}).status_code == 400
    server.scan_receipts[current]['expires'] = time.time() - 1
    assert post(imp, 'kill', {'receipt': current}).status_code == 400
    assert all(p['status'] == 'alive' for p in server.engine.room(code)['players'].values())
    c.close()


def test_scan_rechecks_pause_and_changed_state(server):
    c, post, code, imp, crew, _ = playing(server)
    receipt = scan(post, imp, 'b_' + badge(server, crew[0]))['receipt']
    server.engine.control(code, 'host', 'pause')
    assert post(imp, 'kill', {'receipt': receipt}).status_code == 400
    assert post(crew[0], 'scan', {'payload': 'm_' + server.engine.state['meeting_code']}).status_code == 400
    server.engine.control(code, 'host', 'resume')
    meeting = scan(post, crew[0], 'm_' + server.engine.state['meeting_code'])['receipt']
    assert post(crew[0], 'emergency', {'receipt': meeting}).status_code == 200
    assert post(imp, 'kill', {'receipt': receipt}).status_code == 400
    c.close()


def test_task_opens_after_scan_and_cannot_reuse_receipt(server):
    c, post, code, _, crew, _ = playing(server)
    uid = crew[0]
    station = next(iter(server.engine.room(code)['players'][uid]['tasks']))
    scanned = scan(post, uid, 's_' + station)
    result = post(uid, 'task_open', {'receipt': scanned['receipt']})
    assert result.status_code == 200
    task = result.json()
    assert task['station']['code'] == station
    assert post(uid, 'task_open', {'receipt': scanned['receipt']}).status_code == 400
    answer = answer_for(server.engine, task)
    assert post(uid, 'task_finish', {'station': station, 'id': task['id'], 'epoch': task['epoch'], 'answer': answer}).status_code == 200
    while not server.engine.room(code)['players'][uid]['tasks'][station]['done']:
        receipt = scan(post, uid, 's_' + station)['receipt']
        task = post(uid, 'task_open', {'receipt': receipt}).json()
        answer = answer_for(server.engine, task)
        assert post(uid, 'task_finish', {'station': station, 'id': task['id'], 'epoch': task['epoch'], 'answer': answer}).status_code == 200
    assert post(uid, 'scan', {'payload': 's_' + station}).status_code == 400
    c.close()


def test_wrong_room_and_unknown_qr_and_ghost(server):
    c, post, code, imp, crew, _ = playing(server)
    for payload in ('m_fakefake', 'b_doesnotexist', 'j_123456', None, ['s_bad']):
        assert post(crew[0], 'scan', {'payload': payload}).status_code == 400
    receipt = scan(post, imp, 'b_' + badge(server, crew[0]))['receipt']
    other = server.engine.create_room('host', 'Другая')['code']
    auth = {'Authorization': 'Bearer ' + server.issue(imp, False, True)}
    assert c.post(f'/api/rooms/{other}/kill', json={'receipt':receipt}, headers=auth).status_code == 400
    assert post(imp, 'kill', {'receipt':receipt}).status_code == 200
    assert post(crew[0], 'scan', {'payload':'m_'+server.engine.state['meeting_code']}).status_code == 400
    c.close()


def test_meeting_qr_persistent_and_printable_only_by_admin(server):
    c, post, code, _, crew, _ = playing(server)
    payload = 'm_' + server.engine.state['meeting_code']
    owner = {'Authorization':'Bearer '+server.issue('host',True,True)}
    player = {'Authorization':'Bearer '+server.issue(crew[0],False,True)}
    qr = c.post('/api/qr',json={'payload':payload},headers=owner)
    assert qr.status_code == 200 and 'start=m_' in qr.json()['url']
    assert c.post('/api/qr',json={'payload':payload},headers=player).status_code == 400
    from engine import Engine
    other = Engine(server.DATA / 'game.sqlite3')
    assert other.state['meeting_code'] == server.engine.state['meeting_code']
    other.db.close()
    c.close()


def test_demo_object_menu_disabled_in_telegram_and_remote(server):
    c, post, _, _, crew, _ = playing(server)
    assert post(crew[0], 'demo_objects', {}).status_code == 200
    server.cfg['mode'] = 'telegram'
    assert post(crew[0], 'demo_objects', {}).status_code == 400
    c.close()


def test_impostor_cannot_scan_or_submit_another_players_task(server):
    c, post, code, imp, crew, _ = playing(server)
    station = next(iter(server.engine.room(code)['players'][crew[0]]['tasks']))
    result = post(imp, 'scan', {'payload':'s_'+station})
    assert result.status_code == 400 and 'Предатели' in result.json()['error']
    receipt = scan(post, crew[0], 's_'+station)['receipt']
    opened = post(crew[0], 'task_open', {'receipt':receipt}).json()
    answer = answer_for(server.engine, opened)
    rejected = post(imp, 'task_finish', {'station':station, 'id':opened['id'], 'epoch':opened['epoch'], 'answer':answer})
    assert rejected.status_code == 400 and 'Предатели' in rejected.json()['error']
    assert not server.engine.view(code,imp)['me']['tasks']
    c.close()
