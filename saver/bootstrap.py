# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Per-user portable launch, detached worker and lock-owned single instance."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError
import webbrowser

from .engine import Engine
from .pikpak import PikpakGateway
from .sources import Network, Resolver
from .store import Store
from .web import APP_MARKER, Application, make_server


APP_DIR = Path(__file__).resolve().parents[1]


def data_home(environ=None):
    env = os.environ if environ is None else environ
    if env.get('PIKPAK_SAVER_HOME'):
        return Path(env['PIKPAK_SAVER_HOME']).expanduser().resolve()
    base = Path(env.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share'))
    return (base / 'pikpak-link-saver').expanduser().resolve()


def auth_home(store, directory, environ=None):
    env = os.environ if environ is None else environ
    if env.get('PIKPAK_DIR'):
        return Path(env['PIKPAK_DIR']).expanduser().resolve()
    pointer = store.private_get('auth_dir')
    if isinstance(pointer, str) and Path(pointer).expanduser().is_dir():
        return Path(pointer).expanduser().resolve()
    return Path(directory) / 'auth'


def find_cli(app_dir=APP_DIR, environ=None):
    env = os.environ if environ is None else environ
    override = env.get('PIKPAK_CLI')
    candidate = override or str(Path(app_dir) / 'bin' / 'pikpak')
    if override and '/' not in candidate:
        candidate = shutil.which(candidate, path=env.get('PATH')) or candidate
    if not override and not Path(candidate).is_file():
        candidate = shutil.which('pikpak', path=env.get('PATH')) or candidate
    path = Path(candidate).expanduser().resolve()
    return path if path.is_file() and os.access(str(path), os.X_OK) else None


def self_check(app_dir=APP_DIR, environ=None):
    checks = [(sys.version_info >= (3, 8), 'Python 3.8 或以上'),
              (platform.system() == 'Linux' and platform.machine().lower() in ('x86_64', 'amd64'), 'Linux x86_64'),
              (find_cli(app_dir, environ) is not None, 'PikPak 官方 CLI；缺失时设置 PIKPAK_CLI 或放入 bin/pikpak')]
    for ok, label in checks:
        print(('[通过] ' if ok else '[缺少] ') + label)
    return all(ok for ok, _ in checks)


def _desktop_quote(value):
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('安装路径不能包含换行或空字符')
    value = value.replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        value = value.replace(character, '\\' + character)
    # Desktop Entry unescaping precedes Exec command-line unescaping.
    return '"' + value.replace('\\', '\\\\') + '"'


def install_desktop(app_dir=APP_DIR, environ=None):
    env = os.environ if environ is None else environ
    app_dir = Path(app_dir).resolve()
    base = Path(env.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')).expanduser()
    directory = base / 'applications'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'pikpak-link-saver.desktop'
    icon = str(app_dir / 'saver' / 'static' / 'icon.svg')
    if any(c in icon for c in '\n\r\x00'):
        raise ValueError('安装路径不能包含换行或空字符')
    content = ('[Desktop Entry]\nType=Application\nName=PikPak 链接转存助手\n'
               'Comment=批量解析并分类保存公开链接\nExec=' + _desktop_quote(str(app_dir / '启动.sh')) +
               '\nIcon=' + icon.replace('\\', '\\\\') + '\nTerminal=false\nCategories=Network;FileTransfer;\nStartupNotify=false\n')
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)
    return path


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _write_instance(path, information):
    temporary = path.with_suffix('.tmp')
    fd = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(information, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def existing_instance(directory):
    try:
        info = json.loads((directory / 'instance.json').read_text(encoding='utf-8'))
        if not isinstance(info, dict) or not isinstance(info.get('url'), str):
            return None
        # Never use a profile descriptor as an arbitrary URL opener.
        from urllib.parse import urlsplit
        parts = urlsplit(info['url'])
        if (parts.scheme != 'http' or parts.hostname != '127.0.0.1' or not parts.port
                or parts.path not in ('', '/') or parts.query or parts.fragment or parts.username):
            return None
        opener = build_opener(ProxyHandler({}))
        with opener.open(info['url'].rstrip('/') + '/api/health', timeout=.5) as response:
            status = json.load(response)
        if isinstance(status, dict) and status.get('app') == APP_MARKER:
            return dict(info, stopping=bool(status.get('stopping')))
        return None
    except HTTPError as error:
        error.close()
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def stop_instance(directory):
    info = existing_instance(directory)
    if info is None:
        return False
    token = info.get('token')
    if not isinstance(token, str):
        raise RuntimeError('旧版后台不支持停止接口，请从原启动会话结束后再使用新版')
    request = Request(info['url'].rstrip('/') + '/api/stop', data=b'{}',
                      headers={'Content-Type': 'application/json', 'X-App-Token': token})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            response.read()
    except OSError as error:
        if isinstance(error, HTTPError):
            error.close()
        if existing_instance(directory):
            raise
    descriptor = directory / 'instance.json'
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            current = json.loads(descriptor.read_text(encoding='utf-8'))
            if current.get('token') != token:
                return True
        except FileNotFoundError:
            return True
        time.sleep(.1)
    raise RuntimeError('后台仍在退出，请稍后检查')


def serve(app_dir=APP_DIR):
    directory = data_home()
    _private_directory(directory)
    descriptor = directory / 'instance.json'
    fd = os.open(str(directory / 'instance.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    lock = os.fdopen(fd, 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return 0
    store = gateway = engine = server = None
    try:
        cli = find_cli(app_dir)
        if cli is None:
            raise RuntimeError('未找到 PikPak 官方 CLI，请运行 --self-check')
        store = Store(directory / 'jobs.sqlite3')
        proxy = store.settings()['proxy']
        gateway = PikpakGateway(cli, auth_home(store, directory), proxy=proxy)
        if store.private_get('auth_dir') != str(gateway.auth_dir):
            store.private_set('auth_dir', str(gateway.auth_dir))
        engine = Engine(store, Resolver(Network(proxy)), gateway, directory)
        app = Application(store, engine)
        server = make_server(app)
        port = server.server_address[1]
        info = {'url': 'http://127.0.0.1:%d/' % port, 'token': app.token}
        _write_instance(descriptor, info)
        def request_stop(signum, frame):
            app.stop_requested.set()
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        engine.start()
        server.serve_forever(poll_interval=.2)
        return 0
    finally:
        if engine:
            engine.stop()
        if gateway:
            gateway.close()
        if server:
            server.server_close()
        if store:
            store.close()
        try:
            descriptor.unlink()
        except FileNotFoundError:
            pass
        lock.close()


def open_browser(url):
    opener = shutil.which('xdg-open')
    if opener:
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        webbrowser.open(url)


def launch(app_dir=APP_DIR, browser=True):
    directory = data_home()
    _private_directory(directory)
    info = existing_instance(directory)
    if info and info.get('stopping'):
        raise RuntimeError('程序正在退出，请稍后重新启动')
    if info is None:
        if find_cli(app_dir) is None:
            raise RuntimeError('未找到 PikPak 官方 CLI；请设置 PIKPAK_CLI 或放入 bin/pikpak，并运行 --self-check')
        subprocess.Popen([sys.executable, str(Path(app_dir) / 'main.py'), '--serve'], cwd=str(app_dir),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = existing_instance(directory)
            if info:
                break
            time.sleep(.1)
        if info is None:
            raise RuntimeError('本机服务未能启动，请检查数据目录权限、CLI 与 Python，并运行 --self-check')
    if browser:
        open_browser(info['url'])
    return info['url']


def main(argv=None):
    parser = argparse.ArgumentParser(description='PikPak 链接转存助手')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--serve', action='store_true', help='在当前进程运行后台服务')
    actions.add_argument('--stop', action='store_true', help='结束当前用户的后台服务')
    actions.add_argument('--self-check', action='store_true', help='只检查依赖，不启动任务')
    actions.add_argument('--install-desktop', action='store_true', help='安装当前用户的桌面入口')
    parser.add_argument('--no-browser', action='store_true', help='启动后台并仅打印本机地址')
    args = parser.parse_args(argv)
    try:
        if args.self_check:
            return 0 if self_check() else 1
        if args.install_desktop:
            print('已安装：' + str(install_desktop()))
            return 0
        if args.serve:
            return serve()
        if args.stop:
            print('后台已结束' if stop_instance(data_home()) else '后台未运行')
            return 0
        print(launch(browser=not args.no_browser))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
