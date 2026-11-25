# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Loopback-only HTTP boundary with explicit public response fields."""
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

APP_MARKER = 'pikpak-link-saver'
VERSION = '1.2.1'
_STATIC = {'/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
           '/static/app.css': ('app.css', 'text/css; charset=utf-8'),
           '/static/icon.svg': ('icon.svg', 'image/svg+xml'),
           '/static/vendor/tabler.min.css': ('vendor/tabler.min.css', 'text/css; charset=utf-8')}
_ITEM_FIELDS = ('id', 'root_name', 'group_title', 'filename', 'kind', 'size', 'state',
                'error', 'bytes_done', 'method', 'attempts')
_BATCH_FIELDS = ('id', 'created_at', 'root_name', 'state', 'error', 'total', 'verified',
                 'bytes_total', 'bytes_done', 'images', 'videos')
_SUMMARY_FIELDS = ('total', 'verified', 'active', 'waiting', 'failed', 'needs_attention', 'bytes_total', 'bytes_done')
_GROUP_FIELDS = ('title', 'root_name', 'total', 'verified', 'images', 'videos',
                 'images_verified', 'videos_verified', 'bytes_total', 'bytes_done', 'needs_attention')
_SETTING_FIELDS = ('root_name', 'proxy', 'auto_start', 'local_relay', 'paused')


def _fields(row, names):
    return {key: row[key] for key in names if key in row}


def _group_id(row):
    key = (row['root_name'], row['group_key'])
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20]


def _public_item(row):
    item = _fields(row, _ITEM_FIELDS)
    item['group_id'] = _group_id(row)
    return item


def _account(row):
    public = {'connected': bool(row.get('connected')), 'name': str(row.get('name') or ''),
              'status': row.get('status') or ('connected' if row.get('connected') else 'disconnected'),
              'error': str(row.get('error') or ''), 'device': None}
    device = row.get('device')
    if isinstance(device, dict):
        public['device'] = _fields(device, ('user_code', 'verification_uri', 'expires_at'))
    return public


class WindowSessions:
    """Keep the worker alive while at least one application page is open."""
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._windows = {}
        self._lock = threading.Lock()
        self._seen = False
        self._empty_since = clock()

    def update(self, action, window_id):
        if action not in ('open', 'ping', 'close') or not isinstance(window_id, str) or not 0 < len(window_id) <= 64:
            raise ValueError('页面会话无效')
        with self._lock:
            now = self._clock()
            if action == 'open':
                self._windows[window_id] = now + 120
                self._seen, self._empty_since = True, None
            elif action == 'ping':
                if window_id in self._windows:
                    self._windows[window_id] = now + 120
            else:
                self._windows.pop(window_id, None)
                self._seen = True
                if not self._windows:
                    self._empty_since = now

    def should_stop(self):
        with self._lock:
            now = self._clock()
            self._windows = {key: expires for key, expires in self._windows.items() if expires > now}
            if self._windows:
                return False
            if self._empty_since is None:
                self._empty_since = now
            return now - self._empty_since >= (3 if self._seen else 60)


class Application:
    def __init__(self, store, engine, token=None):
        self.store, self.engine = store, engine
        self.token = token if token is not None else secrets.token_urlsafe(32)
        self.static_dir = Path(__file__).resolve().parent / 'static'
        self.windows = WindowSessions()
        self.stop_requested = threading.Event()

    def state(self):
        snapshot = self.store.snapshot()
        return {'settings': _fields(snapshot['settings'], _SETTING_FIELDS),
                'summary': _fields(snapshot['summary'], _SUMMARY_FIELDS),
                'batches': [_fields(row, _BATCH_FIELDS) for row in snapshot['batches']],
                'items': [_public_item(row) for row in snapshot['items']],
                'attention': [_public_item(row) for row in snapshot['attention']],
                'groups': [dict(_fields(row, _GROUP_FIELDS), id=_group_id(row)) for row in snapshot['groups']],
                'account': _account(self.engine.account())}


def make_server(app, port=0):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'PikPakSaver'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def handle(self):
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                # Closing a tab can interrupt either headers or the response body.
                pass

        def log_message(self, format, *args):
            # Request paths may contain source parameters; never put them in logs.
            pass

        def reply(self, status, data, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, write=False):
            hosts = self.headers.get_all('Host', [])
            origins = self.headers.get_all('Origin', [])
            port = self.server.server_address[1]
            valid_hosts = ('127.0.0.1:%d' % port, 'localhost:%d' % port)
            valid = len(hosts) == 1 and hosts[0] in valid_hosts
            valid = valid and len(origins) <= 1 and (not origins or origins[0] == 'http://' + hosts[0])
            window_id = self.headers.get('X-Window-Id')
            if window_id:
                valid = valid and len(window_id) <= 64
            if write or window_id:
                tokens = self.headers.get_all('X-App-Token', [])
                valid = valid and len(tokens) == 1 and secrets.compare_digest(tokens[0].encode('utf-8'), app.token.encode('utf-8'))
            if not valid:
                self.reply(403, {'error': '请求来源或本机令牌无效，请从启动入口重新打开'})
            return valid

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            if path == '/api/health':
                self.reply(200, {'app': APP_MARKER, 'version': VERSION, 'stopping': self.server.stopping})
            elif path == '/api/state':
                try:
                    window_id = self.headers.get('X-Window-Id')
                    if window_id:
                        app.windows.update('ping', window_id)
                    state = app.state()
                except Exception:
                    self.reply(500, {'error': '读取任务状态失败，请稍后重试'})
                else:
                    self.reply(200, state)
            elif path == '/':
                template = (app.static_dir / 'index.html').read_text(encoding='utf-8')
                document = template.replace('__APP_TOKEN__', html.escape(app.token, quote=True))
                self.reply(200, document.encode('utf-8'), 'text/html; charset=utf-8')
            elif path in _STATIC:
                name, content_type = _STATIC[path]
                target = (app.static_dir / name).resolve()
                if app.static_dir not in target.parents or not target.is_file():
                    self.reply(404, {'error': '页面不存在'})
                    return
                self.reply(200, target.read_bytes(), content_type)
            else:
                self.reply(404, {'error': '页面不存在'})

        def do_POST(self):
            if not self.allowed(write=True):
                return
            routes = ('/api/jobs', '/api/settings', '/api/retry', '/api/auth/start', '/api/window', '/api/stop')
            if self.path not in routes:
                self.reply(404, {'error': '接口不存在'})
                return
            status = 200
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('请求长度无效')
                length = int(lengths[0])
                if length < 0 or length > 2 * 1024 * 1024:
                    status, result = 413, {'error': '输入过大，请分批粘贴'}
                elif self.headers.get_content_type() != 'application/json':
                    status, result = 415, {'error': '请求必须使用 JSON'}
                else:
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(payload, dict):
                        raise ValueError('输入必须为 JSON 对象')
                    if self.path == '/api/jobs':
                        if set(payload) - {'text', 'root_name'}:
                            raise ValueError('转存字段无效')
                        if 'root_name' in payload and not isinstance(payload['root_name'], str):
                            raise ValueError('目标目录必须为文本')
                        batch_id = app.store.new_batch(payload.get('text'), payload.get('root_name'))
                        status, result = 201, {'batch_id': batch_id}
                    elif self.path == '/api/settings':
                        app.store.set_settings(payload)
                        result = {'settings': _fields(app.store.settings(), _SETTING_FIELDS)}
                    elif self.path == '/api/retry':
                        if set(payload) - {'batch_id'} or ('batch_id' in payload and not isinstance(payload['batch_id'], str)):
                            raise ValueError('批次字段无效')
                        app.store.retry_failed(payload.get('batch_id'))
                        result = {'ok': True}
                    elif self.path == '/api/window':
                        if set(payload) != {'action'}:
                            raise ValueError('页面操作无效')
                        app.windows.update(payload['action'], self.headers.get('X-Window-Id'))
                        result = {'ok': True}
                    else:
                        if payload:
                            raise ValueError('此操作不接受附加参数')
                        if self.path == '/api/stop':
                            app.stop_requested.set()
                            result = {'ok': True}
                        else:
                            result = _account(app.engine.start_auth())
            except (ValueError, TypeError) as error:
                status, result = 400, {'error': str(error) or '输入无效'}
            except Exception:
                status, result = 500, {'error': '操作暂时不可用，请稍后重试'}
            self.reply(status, result)

    class Server(ThreadingHTTPServer):
        stopping = False

        def service_actions(self):
            if not self.stopping and (app.stop_requested.is_set() or app.windows.should_stop()):
                self.stopping = True
                threading.Thread(target=self.shutdown, daemon=True).start()

    return Server(('127.0.0.1', port), Handler)
