"""Token-only Telegram setup and managed Cloudflare Quick Tunnel (stdlib only)."""
import ctypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class LaunchError(RuntimeError):
    pass


TOKEN = re.compile(r'\d{5,15}:[A-Za-z0-9_-]{20,100}')
TUNNEL_URL = re.compile(r'https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?=[/\s|]|$)', re.I)
# Official release assets, pinned with SHA-256 from Cloudflare's GitHub release.
VERSION = '2026.10.2'
ASSETS = {
    ('Windows', 'amd64'): ('cloudflared-windows-amd64.exe', '12c7c887f8aaab779aef1dfa9b60673530a5adb88ecc55dd6df8f56e0b70f199'),
    ('Linux', 'amd64'): ('cloudflared-linux-amd64', '35fbdd2beb378436c7da377d7358cea8e33f9c94a6705d1d8036bf9b0d6d8005'),
    ('Linux', 'arm64'): ('cloudflared-linux-arm64', 'b7e55b4d9702c2ab5f8348f780bb1db044527fc555367714c78e0595750a4665'),
    ('Darwin', 'amd64'): ('cloudflared-darwin-amd64.tgz', 'b64efd5a2772fc124b855f60eb3d33ba1a4f2ba67b19ddc58d32b705d42b82c9'),
    ('Darwin', 'arm64'): ('cloudflared-darwin-arm64.tgz', '529a07089ac2d46762578389230fa148e5cb6ce2689200fc006956864042d17a'),
}


def clean(value):
    return TOKEN.sub('[токен скрыт]', str(value))


def read_env(path):
    result = {}
    if path.exists():
        for line in path.read_text('utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def read_config(data):
    path = data / 'config.json'
    try:
        result = json.loads(path.read_text('utf-8')) if path.exists() else {}
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (OSError, ValueError):
        raise LaunchError('Не удалось прочитать data/config.json. Сохраните его копию и исправьте JSON.') from None


def save_config(data, config):
    data.mkdir(parents=True, exist_ok=True)
    path = data / 'config.json'
    temp = data / ('config-' + secrets.token_hex(6) + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def get_me(token):
    # Never propagate exceptions containing a Telegram URL: it contains the token.
    try:
        request = Request('https://api.telegram.org/bot' + token + '/getMe', data=b'{}',
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
        if not result.get('ok') or not result.get('result', {}).get('is_bot'):
            raise LaunchError('Telegram не подтвердил токен бота.')
        return result['result']['username']
    except HTTPError as exc:
        if exc.code in (401, 404):
            raise LaunchError('Telegram отклонил токен. Проверьте BOT_TOKEN в .env или запустите START.cmd --setup.') from None
        raise LaunchError('Telegram временно недоступен (HTTP ' + str(exc.code) + '). Повторите запуск.') from None
    except (OSError, ValueError, KeyError):
        raise LaunchError('Нет связи с api.telegram.org. Проверьте интернет и повторите запуск.') from None


def setup(root, data, *, reset=False):
    config = read_config(data)
    token = (os.environ.get('BOT_TOKEN') or read_env(root / '.env').get('BOT_TOKEN')
             or config.get('bot_token') or '').strip()
    if reset or not token:
        print('Создайте бота командой /newbot у @BotFather.\nВставьте его токен сюда (ввод скрыт).', flush=True)
        try:
            token = getpass.getpass('BOT_TOKEN: ').strip()
        except (EOFError, OSError):
            raise LaunchError('Впишите BOT_TOKEN в файл .env рядом с START.cmd и повторите запуск.') from None
    if not TOKEN.fullmatch(token):
        raise LaunchError('Неверный формат токена. Впишите BOT_TOKEN в .env или запустите START.cmd --setup.')
    print('[1/4] Проверяем токен Telegram…', flush=True)
    username = get_me(token)
    # Keep saved rooms/settings, replace demo mode and stale tunnel URL automatically.
    config.update(mode='telegram', bot_token=token, bot_username=username, public_url='')
    config.setdefault('admin_ids', [])
    save_config(data, config)
    return config


class InstanceLock:
    """OS releases this lock even if the console is closed or the process crashes."""
    def __init__(self, data):
        self.path = data / 'server.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise LaunchError('Эта игра уже запущена. Используйте открытое окно или сначала закройте его.') from None
        return self

    def __exit__(self, *args):
        self.file.close()


def reserve_port(preferred=8765, explicit=False):
    if type(preferred) is not int or not 1024 <= preferred <= 65535:
        raise LaunchError('Порт должен быть от 1024 до 65535.')
    for port in ([preferred] if explicit else [preferred, 0]):
        sock = socket.socket()
        try:
            if os.name == 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(('127.0.0.1', port))
            sock.listen(128)
            return sock
        except OSError:
            sock.close()
    raise LaunchError('Указанный порт занят. Закройте другой экземпляр или задайте START.cmd --port 9000.')


def ensure_cloudflared(root):
    machine = platform.machine().lower()
    arch = 'arm64' if machine in ('arm64', 'aarch64') else 'amd64'
    asset = ASSETS.get((platform.system(), arch))
    if not asset:
        raise LaunchError('Эта система не поддерживается автоматической установкой Cloudflare.')
    name, checksum = asset
    tools = root / '.tools'
    tools.mkdir(exist_ok=True)
    downloaded = tools / name
    if not downloaded.exists() or hashlib.sha256(downloaded.read_bytes()).hexdigest() != checksum:
        print('[2/4] Скачиваем Cloudflare с официального GitHub (один раз)…', flush=True)
        temp = tools / (name + '.download')
        url = f'https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{name}'
        try:
            with urlopen(Request(url, headers={'User-Agent': 'AmongUsLive/0.4'}), timeout=60) as response, temp.open('wb') as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            if hashlib.sha256(temp.read_bytes()).hexdigest() != checksum:
                raise LaunchError('Контрольная сумма Cloudflare не совпала. Повторите запуск; файл не запущен.')
            os.replace(temp, downloaded)
        except OSError:
            raise LaunchError('Не удалось скачать Cloudflare. Проверьте доступ к github.com и повторите START.cmd.') from None
        finally:
            temp.unlink(missing_ok=True)
    executable = downloaded
    if name.endswith('.tgz'):
        import tarfile
        executable = tools / ('cloudflared-' + VERSION)
        with tarfile.open(downloaded) as archive:
            entry = next((m for m in archive.getmembers() if Path(m.name).name == 'cloudflared' and m.isfile()), None)
            if entry is None:
                raise LaunchError('В архиве Cloudflare нет программы cloudflared.')
            executable.write_bytes(archive.extractfile(entry).read())
    executable.chmod(0o700)
    return executable


class ProcessOwner:
    """A Windows job kills the tunnel if the user closes the console with X."""
    def __init__(self):
        self.children, self.job = [], None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64), ('Flags', w.DWORD),
                        ('MinWS', ctypes.c_size_t), ('MaxWS', ctypes.c_size_t), ('Processes', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', ctypes.c_size_t), ('JobMemory', ctypes.c_size_t),
                        ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes, self.kernel.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.job = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.job or not self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise LaunchError('Windows не разрешила управление туннелем. Запустите START.cmd из обычной папки пользователя.')

    def start(self, command, root):
        env = dict(os.environ)
        # Cloudflared has no reason to inherit bot credentials or local owner keys.
        for key in ('BOT_TOKEN', 'AMONGUS_BOT_TOKEN', 'AMONGUS_BOOT_KEY'):
            env.pop(key, None)
        p = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.children.append(p)
        if self.job and not self.kernel.AssignProcessToJobObject(self.job, int(p._handle)):
            self.close()
            raise LaunchError('Windows не разрешила управление процессом Cloudflare.')
        return p

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        for process in self.children:
            self.stop(process)


class Tunnel:
    def __init__(self, root, owner):
        self.root, self.owner = root, owner
        self.process, self.url = None, None
        self.log_path = root / '.tools' / 'cloudflare.log'

    def start(self, port, timeout=50):
        cloud = ensure_cloudflared(self.root)
        config = self.root / '.tools' / 'quick-tunnel.json'
        config.write_text('{}\n', encoding='utf-8')  # Ignore another project's ~/.cloudflared config.
        print('[2/4] Подключаем Cloudflare Tunnel…', flush=True)
        for protocol in ('http2', 'quic'):
            events = queue.Queue(maxsize=512)
            self.process = self.owner.start([str(cloud), 'tunnel', '--no-autoupdate', '--config', str(config),
                                            '--url', f'http://127.0.0.1:{port}', '--protocol', protocol], self.root)
            def read_output(process=self.process, output=events):
                with self.log_path.open('a', encoding='utf-8') as log:
                    for line in process.stdout:
                        log.write(clean(line)); log.flush()
                        try:
                            output.put_nowait(line)
                        except queue.Full:
                            pass
            threading.Thread(target=read_output, daemon=True).start()
            end, url, connected = time.monotonic() + timeout, None, False
            while time.monotonic() < end:
                if self.process.poll() is not None:
                    break
                try:
                    line = events.get(timeout=.2)
                except queue.Empty:
                    continue
                match = TUNNEL_URL.search(line)
                url = match[0].lower() if match else url
                connected = connected or 'Registered tunnel connection' in line
                if url and connected:
                    self.url = url
                    return url
            self.owner.stop(self.process)
            print('Cloudflare ' + protocol + ' не подключён.' + (' Пробуем QUIC…' if protocol == 'http2' else ''), flush=True)
        raise LaunchError('Cloudflare не создал туннель. Проверьте интернет и .tools/cloudflare.log, затем повторите START.cmd.')


def public_health(url, instance):
    try:
        with urlopen(url + '/api/health', timeout=5) as response:
            value = json.load(response)
        return value.get('ok') is True and value.get('instance') == instance
    except (OSError, ValueError):
        return False
