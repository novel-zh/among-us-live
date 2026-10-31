"""One-command launcher: python run.py. Python 3.11+, no Node/Docker needed."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Among Us Live — локальный сервер и Telegram-бот")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--setup", action="store_true", help="Replace the saved bot token")
    parser.add_argument("--demo", action="store_true", help="Explicit local demo without a Telegram token")
    parser.add_argument("--tunnel", action="store_true", help="Also expose the explicit demo through Cloudflare for connectivity checks")
    parser.add_argument("--no-install", action="store_true", help="Use dependencies from the current Python")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data", type=Path, help="Override the data directory")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required: https://www.python.org/downloads/")
    if args.data:
        args.data = args.data.resolve()
    os.chdir(ROOT)
    if not args.no_install:
        env = ROOT / ".venv"
        python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        marker = env / ".requirements.sha256"
        digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
        if not python.exists():
            print("Preparing a private Python environment...", flush=True)
            venv.create(env, with_pip=True)
        if not marker.exists() or marker.read_text() != digest:
            print("Installing dependencies (internet needed on first launch)...", flush=True)
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
            marker.write_text(digest)
        # A child process owns the server; Ctrl+C propagates on Windows.
        command = [str(python), str(ROOT / "run.py"), "--no-install"]
        for flag in ('no_browser', 'demo', 'tunnel', 'setup'):
            if getattr(args, flag):
                command.append('--' + flag.replace('_', '-'))
        if args.port:
            command += ["--port", str(args.port)]
        if args.data:
            command += ["--data", str(args.data.resolve())]
        try:
            return subprocess.call(command)
        except KeyboardInterrupt:
            return 0
    return launch(args)


def launch(args):
    from launcher import InstanceLock, LaunchError, ProcessOwner, Tunnel, clean, public_health, read_config, reserve_port, save_config, setup
    import uvicorn
    data = args.data or Path(os.environ.get('AMONGUS_DATA', ROOT / 'data')).resolve()
    owner = None
    sock = None
    try:
        with InstanceLock(data):
            config = read_config(data) if args.demo else setup(ROOT, data, reset=args.setup)
            sock = reserve_port(args.port or config.get('port', 8765), explicit=args.port is not None)
            port = sock.getsockname()[1]
            owner = ProcessOwner()
            tunnel = Tunnel(ROOT, owner) if not args.demo or args.tunnel else None
            public_url = tunnel.start(port) if tunnel else ''
            # Runtime overrides keep --demo isolated from the saved Telegram credentials.
            os.environ.update(AMONGUS_DATA=str(data), AMONGUS_PORT=str(port),
                              AMONGUS_MODE='demo' if args.demo else 'telegram', AMONGUS_PUBLIC_URL=public_url,
                              AMONGUS_BOOT_KEY=secrets.token_urlsafe(32), AMONGUS_INSTANCE=secrets.token_urlsafe(16))
            if not args.demo:
                config.update(port=port, public_url=public_url)
                save_config(data, config)
            import server
            if not args.demo and not server.engine.state['stations']:
                for name, kind in [('Электрощит', 'wires'), ('Считыватель карты', 'card_swipe'),
                                   ('Распределитель', 'calibration'), ('Реактор', 'sequence')]:
                    server.engine.station(name, kind)
            app_server = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=port,
                                                       proxy_headers=False, access_log=False, log_level='warning'))
            stop = threading.Event()
            failed = []
            def watch():
                deadline = time.monotonic() + 100
                public_ready = tunnel is None
                announced = False
                bot_error_printed = False
                last_probe = 0
                while not stop.wait(.5):
                    if tunnel and tunnel.process.poll() is not None:
                        failed.append('Cloudflare завершился. Перезапустите START.cmd; данные игры сохранены.')
                        app_server.should_exit = True
                        return
                    if not app_server.started:
                        continue
                    if tunnel and not public_ready and time.monotonic() - last_probe >= 3:
                        last_probe = time.monotonic()
                        public_ready = public_health(public_url, os.environ['AMONGUS_INSTANCE'])
                    if server.bot_status['state'] == 'error' and not bot_error_printed:
                        print('Telegram: ' + server.bot_status['detail'], flush=True)
                        bot_error_printed = True
                    if public_ready and (args.demo or server.bot_status['state'] == 'online') and not announced:
                        print('\n[4/4] ГОТОВО. ' + ('Демонстрация.' if args.demo else 'Telegram-бот подключён.'), flush=True)
                        if public_url:
                            print('Публичная Mini App: ' + public_url + ' (HTTPS проверен)', flush=True)
                        if args.demo:
                            link = f'http://127.0.0.1:{port}/#setup={server.BOOT_KEY}'
                            print('Локальный вход ведущего: ' + link, flush=True)
                        else:
                            link = 'https://t.me/' + config['bot_username']
                            print('Бот: ' + link + '\nНапишите /start → «Создать комнату» или «Войти по коду».', flush=True)
                        print('Оставьте это окно открытым. Остановка: Ctrl+C.\n', flush=True)
                        if not args.no_browser:
                            webbrowser.open(link)
                        announced = True
                    if not announced and time.monotonic() >= deadline:
                        failed.append('Не удалось подтвердить готовность. Проверьте интернет и .tools/cloudflare.log; затем повторите START.cmd.')
                        app_server.should_exit = True
                        return
            monitor = threading.Thread(target=watch, daemon=True)
            monitor.start()
            print('[3/4] Запускаем сервер' + (' и Telegram-бота…' if not args.demo else '…'), flush=True)
            if public_url:
                print('Адрес туннеля: ' + public_url, flush=True)
            try:
                app_server.run(sockets=[sock])
            finally:
                stop.set()
                monitor.join(timeout=6)
            if failed:
                raise LaunchError(failed[0])
            return 0
    except KeyboardInterrupt:
        return 0
    except LaunchError as exc:
        print('\nОшибка: ' + clean(exc), flush=True)
        return 1
    except OSError as exc:
        print('\nОшибка запуска: ' + clean(exc), flush=True)
        return 1
    finally:
        if owner:
            owner.close()
        if sock:
            sock.close()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
