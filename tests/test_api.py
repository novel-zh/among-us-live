import hashlib
import hmac
import importlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AMONGUS_DATA", str(tmp_path))
    monkeypatch.setenv("AMONGUS_BOOT_KEY", "test-owner-key")
    sys.modules.pop("server", None)
    s = importlib.import_module("server")
    yield s
    s.engine.db.close()


def auth(client, path, body):
    r = client.post('/api/auth/'+path, json=body)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def signed(token, stamp=None, uid=123):
    data = {"auth_date": str(int(time.time()) if stamp is None else stamp), "user": json.dumps({"id": uid, "first_name": "Тест"}, ensure_ascii=False), "query_id": "AAE"}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_telegram_signature_expiry_and_tampering(server):
    token = "12345:some-secret"
    assert server.validate_init_data(signed(token), token)["id"] == 123
    from engine import GameError
    for value in (signed(token)+'&user=duplicate', signed(token).replace('AAE','BBE'), signed(token, int(time.time())-4000), signed(token, int(time.time())+100)):
        with pytest.raises(GameError):
            server.validate_init_data(value, token)


def test_remote_cannot_use_demo_or_owner(server):
    with TestClient(server.app, client=("198.51.100.9", 1000)) as c:
        assert c.post('/api/auth/demo', json={"id":"demo_1"}).status_code == 400
        assert c.post('/api/auth/local', json={"key":"test-owner-key"}).status_code == 400
    with TestClient(server.app) as c:
        assert c.post('/api/auth/demo', json={"id":"demo_1"}, headers={"host":"public.example.com"}).status_code == 400
        assert c.post('/api/auth/local', json={"key":"test-owner-key"}, headers={"x-forwarded-for":"198.51.100.1"}).status_code == 400


def test_api_access_qr_and_configuration(server):
    with TestClient(server.app) as c:
        assert c.get('/').status_code == 200
        assert c.get('/static/app.js').status_code == 200
        assert c.get('/api/me').status_code == 401
        owner = auth(c, 'local', {"key":"test-owner-key"})
        player = auth(c, 'demo', {"id":"demo_1"})
        assert c.post('/api/rooms', json={"name":"Чужая"}, headers=player).status_code == 400
        r = c.post('/api/rooms', json={"name":"Наша игра"}, headers=owner).json()
        code = r['code']
        assert c.get('/api/rooms/'+code, headers=player).status_code == 400
        assert c.post('/api/join', json={"code":code}, headers=player).status_code == 200
        assert c.get('/api/rooms/'+code, headers=player).json()['admin'] is False
        st = c.post('/api/stations', json={"name":"Реактор", "kind":"wires"}, headers=owner).json()
        qr = c.post('/api/qr', json={"payload":"s_"+st['code']}, headers=owner).json()
        assert qr['url'].endswith('start=s_'+st['code'])
        assert qr['image'].startswith('data:image/svg+xml;base64,')
        assert c.post('/api/qr', json={"payload":"s_"+st['code']}, headers=player).status_code == 400
        assert c.post('/api/rooms/'+code+'/control', json={"action":"end"}, headers=player).status_code == 400
        settings = c.get('/api/settings', headers=owner).json()
        assert 'bot_token' not in settings
        assert c.get('/api/settings', headers=player).status_code == 400
        assert c.get('/api/me', headers=owner).headers['cache-control'] == 'no-store'


def test_demo_disabled_in_telegram_mode_and_qr_stability(server):
    server.cfg.update(mode='telegram', bot_token='12345:fake-secret', bot_username='our_test_bot', app_short_name='play', public_url='https://one.example.com')
    # No lifespan: avoid contacting Telegram in this deterministic test.
    c = TestClient(server.app)
    assert c.post('/api/auth/demo', json={"id":"demo_1"}).status_code == 400
    first = server.deep_link('s_abc123')
    server.cfg['public_url'] = 'https://another.example.com'
    assert server.deep_link('s_abc123') == first == 'https://t.me/our_test_bot?start=s_abc123'
    server.cfg['admin_ids'] = ['123']
    owner = auth(c, 'telegram', {'initData':signed(server.cfg['bot_token'])})
    assert c.get('/api/me', headers=owner).json()['host']
    server.cfg['admin_ids'] = []
    player = auth(c, 'telegram', {'initData':signed(server.cfg['bot_token'], uid=321)})
    assert not c.get('/api/me', headers=player).json()['host']
    c.close()


def test_same_station_code_for_multiple_sessions(server):
    c = TestClient(server.app)
    owner = auth(c, 'local', {"key":"test-owner-key"})
    st = c.post('/api/stations', json={"name":"Связь", "kind":"tuning"}, headers=owner).json()
    a = c.post('/api/qr', json={"payload":"s_"+st['code']}, headers=owner).json()
    c.post('/api/rooms', json={"name":"Первая"}, headers=owner)
    c.post('/api/rooms', json={"name":"Вторая"}, headers=owner)
    b = c.post('/api/qr', json={"payload":"s_"+st['code']}, headers=owner).json()
    assert a == b
    c.close()
