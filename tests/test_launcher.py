import json
from pathlib import Path
import socket
import sys
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher as l

TEST_TOKEN = '123456789:' + 'a' * 35


def test_first_setup_needs_only_token_preserves_game_settings_and_hides_secret(tmp_path, monkeypatch, capsys):
    data = tmp_path / 'data'
    l.save_config(data, {'mode': 'demo', 'admin_ids': ['101'], 'port': 8765, 'public_url': 'https://old.trycloudflare.com'})
    (tmp_path / '.env').write_text('BOT_TOKEN=' + TEST_TOKEN, 'utf-8')
    monkeypatch.delenv('BOT_TOKEN', raising=False)
    monkeypatch.setattr(l, 'get_me', lambda token: 'among_test_bot' if token == TEST_TOKEN else pytest.fail('Wrong token'))
    config = l.setup(tmp_path, data)
    assert config['mode'] == 'telegram' and config['bot_username'] == 'among_test_bot'
    assert config['public_url'] == '' and config['admin_ids'] == ['101']
    assert l.read_config(data) == config
    assert TEST_TOKEN not in capsys.readouterr().out


def test_token_prompt_is_saved_and_reset_can_replace_saved_token(tmp_path, monkeypatch):
    monkeypatch.delenv('BOT_TOKEN', raising=False)
    tokens = iter([TEST_TOKEN, '987654321:' + 'b'*35])
    monkeypatch.setattr(l.getpass, 'getpass', lambda prompt: next(tokens))
    monkeypatch.setattr(l, 'get_me', lambda token: 'among_test_bot')
    data = tmp_path / 'data'
    assert l.setup(tmp_path, data)['bot_token'] == TEST_TOKEN
    assert l.setup(tmp_path, data)['bot_token'] == TEST_TOKEN  # No second prompt.
    assert l.setup(tmp_path, data, reset=True)['bot_token'].startswith('987654321:')


def test_invalid_token_does_not_replace_existing_config(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    l.save_config(data, {'bot_token': TEST_TOKEN, 'admin_ids': ['55']})
    monkeypatch.setenv('BOT_TOKEN', 'not-a-token')
    with pytest.raises(l.LaunchError, match='формат'):
        l.setup(tmp_path, data)
    assert l.read_config(data)['bot_token'] == TEST_TOKEN


def test_network_errors_never_expose_telegram_token(monkeypatch):
    def rejected(*args, **kw):
        raise HTTPError('https://api.telegram.org/bot'+TEST_TOKEN+'/getMe', 401, 'Unauthorized', {}, None)
    monkeypatch.setattr(l, 'urlopen', rejected)
    with pytest.raises(l.LaunchError) as error:
        l.get_me(TEST_TOKEN)
    assert TEST_TOKEN not in str(error.value) and 'отклонил' in str(error.value)
    assert TEST_TOKEN not in l.clean('url=bot' + TEST_TOKEN + '/getMe')


def test_download_rejects_corrupted_cloudflared_before_execution(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(l.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(l.platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(l, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'not an executable'))
    with pytest.raises(l.LaunchError, match='сумма'):
        l.ensure_cloudflared(tmp_path)
    assert not list((tmp_path / '.tools').glob('*.exe'))
    assert not list((tmp_path / '.tools').glob('*.download'))


def test_port_reservation_does_not_steal_other_projects_port():
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0)); taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(l.LaunchError, match='занят'):
            l.reserve_port(port, explicit=True)
        reserved = l.reserve_port(port)
        try:
            assert reserved.getsockname()[1] != port
        finally:
            reserved.close()


def test_double_start_is_blocked_and_lock_is_released_after_shutdown(tmp_path):
    with l.InstanceLock(tmp_path):
        with pytest.raises(l.LaunchError, match='уже запущена'):
            with l.InstanceLock(tmp_path):
                pytest.fail('Second server acquired lock')
    with l.InstanceLock(tmp_path):
        pass


def test_tunnel_url_parser_rejects_similar_domain():
    assert not l.TUNNEL_URL.search('https://a.trycloudflare.com.evil.test')
    assert l.TUNNEL_URL.search('| https://one-two.trycloudflare.com |')[0] == 'https://one-two.trycloudflare.com'


def test_public_readiness_requires_this_instance(monkeypatch):
    import io
    monkeypatch.setattr(l, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps({'ok': True, 'instance': 'other'}).encode()))
    assert l.public_health('https://test.trycloudflare.com', 'mine') is False
    monkeypatch.setattr(l, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps({'ok': True, 'instance': 'mine'}).encode()))
    assert l.public_health('https://test.trycloudflare.com', 'mine') is True


def test_owned_child_is_stopped_and_does_not_receive_bot_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TEST_TOKEN)
    monkeypatch.setenv('AMONGUS_BOOT_KEY', 'private-owner-key')
    owner = l.ProcessOwner()
    try:
        child = owner.start([sys.executable, '-u', '-c',
                             'import os,time; print(bool(os.getenv("BOT_TOKEN") or os.getenv("AMONGUS_BOOT_KEY")),flush=True); time.sleep(30)'], tmp_path)
        assert child.stdout.readline().strip() == 'False'
    finally:
        owner.close()
    assert child.poll() is not None
