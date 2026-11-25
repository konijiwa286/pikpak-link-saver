# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Serialized official CLI/MCP access; service output never enters ordinary logs."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


_AUTH_REQUIRED = {'UNAUTHENTICATED', 'UNAUTHORIZED', 'TOKEN_EXPIRED',
                  'TOKEN_REFRESH_FAILED', 'CREDENTIALS_ERROR', 'ACCESS_DENIED'}


class PikpakGateway:
    def __init__(self, cli_path, auth_dir, proxy=''):
        self.cli_path = str(Path(cli_path).resolve())
        self.auth_dir = Path(auth_dir).resolve()
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_dir.chmod(0o700)
        self.proxy = proxy
        self._lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._command_proc = None
        self._proc = None
        self._serial = 0
        self._messages = None
        self._reader = None
        self._reader_stop = None

    def _environment(self):
        env = dict(os.environ, PIKPAK_DIR=str(self.auth_dir))
        if self.proxy:
            for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy'):
                env[key] = self.proxy
        return env

    @staticmethod
    def _read(proc, messages, stopped):
        def put(message):
            while not stopped.is_set():
                try:
                    messages.put(message, timeout=.1)
                    return
                except queue.Full:
                    continue
        try:
            while not stopped.is_set():
                line = proc.stdout.readline(16 * 1024 * 1024 + 1)
                if not line or len(line) > 16 * 1024 * 1024:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                # Progress notifications have no waiting caller and must not accumulate.
                if isinstance(message, dict) and 'id' in message and ('result' in message or 'error' in message):
                    put(message)
        except (OSError, ValueError):
            pass
        finally:
            put(None)

    def _connect(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        self._close_process()
        self._messages = queue.Queue(maxsize=16)
        self._reader_stop = threading.Event()
        with self._process_lock:
            if self._cancelled.is_set():
                raise OSError('PikPak 操作已停止')
            self._proc = subprocess.Popen(
                [self.cli_path, 'mcp', '--stdio'], env=self._environment(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1)
        self._reader = threading.Thread(target=self._read,
            args=(self._proc, self._messages, self._reader_stop), daemon=True)
        self._reader.start()
        try:
            self._rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                   'clientInfo': {'name': 'pikpak-link-saver', 'version': '1'}})
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except Exception:
            self._close_process()
            raise

    def _send(self, message):
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self._proc.stdin.flush()

    def _rpc(self, method, params):
        self._serial += 1
        serial = self._serial
        self._send({'jsonrpc': '2.0', 'id': serial, 'method': method, 'params': params})
        deadline = time.monotonic() + 120
        while True:
            try:
                message = self._messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('PikPak 响应超时') from None
            if message is None:
                raise ConnectionError('PikPak 连接已中断')
            if message.get('id') == serial:
                if message.get('error'):
                    raise RuntimeError('PikPak 请求失败')
                result = message.get('result', {})
                if not isinstance(result, dict):
                    raise ConnectionError('PikPak 返回格式无效')
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError('PikPak 响应超时')

    def call(self, tool_name, **arguments):
        with self._lock:
            try:
                self._connect()
                result = self._rpc('tools/call', {'name': tool_name, 'arguments': arguments})
                if result.get('isError'):
                    raise RuntimeError('PikPak 操作失败')
                if isinstance(result.get('structuredContent'), dict):
                    return result['structuredContent']
                text = '\n'.join(block['text'] for block in result.get('content', []) if block.get('type') == 'text')
                if not text:
                    return {}
                try:
                    data = json.loads(text)
                    if not isinstance(data, dict):
                        raise ValueError()
                    return data
                except ValueError:
                    if tool_name in ('rename', 'mv', 'rm', 'task_rm'):
                        return {'message': '操作完成'}
                    raise RuntimeError('PikPak 返回格式无效') from None
            except OSError:
                self._close_process()
                raise RuntimeError('PikPak 连接失败，操作结果待核对') from None

    @staticmethod
    def _auth_json(output):
        try:
            return json.loads(output)
        except ValueError:
            # CLI v0.5.3 may prefix account JSON with a VIP/storage warning.
            start = output.find('\n{')
            if start < 0:
                raise
            return json.loads(output[start + 1:])

    def _command(self, arguments, timeout=120):
        with self._lock:
            account_check = arguments == ['auth', 'status']
            try:
                with self._process_lock:
                    if self._cancelled.is_set():
                        raise OSError('PikPak 操作已停止')
                    proc = subprocess.Popen([self.cli_path] + arguments + ['-F', 'json'],
                        env=self._environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE if account_check else subprocess.DEVNULL,
                        text=True, encoding='utf-8')
                    self._command_proc = proc
                try:
                    output, errors = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise
                finally:
                    with self._process_lock:
                        self._command_proc = None
                if account_check:
                    try:
                        data = self._auth_json(output)
                    except ValueError:
                        data = {}
                    if not proc.returncode and isinstance(data, dict) and type(data.get('logged_in')) is bool:
                        return data
                    for raw in (output, errors):
                        try:
                            report = self._auth_json(raw)
                        except ValueError:
                            continue
                        error = report.get('error') if isinstance(report, dict) else None
                        if isinstance(error, dict) and str(error.get('code', '')).upper() in _AUTH_REQUIRED:
                            return {'logged_in': False}
                    raise RuntimeError('暂时无法检查 PikPak 连接，请检查网络或代理')
                data = json.loads(output)
                if not isinstance(data, dict):
                    raise ValueError()
                if proc.returncode:
                    # Device polling may return pending JSON with a nonzero exit.
                    if data.get('status') not in ('pending', 'authorization_pending', 'slow_down'):
                        raise RuntimeError('PikPak 命令失败')
                return data
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise RuntimeError('PikPak 命令失败，操作结果待核对') from None

    def auth_status(self):
        return self._command(['auth', 'status'])

    def auth_start(self):
        return self._command(['auth', 'login', '--method', 'device', '--no-launch-browser'])

    def auth_continue(self, device_code):
        result = self._command(['auth', 'login', '--continue', device_code])
        if result.get('status') == 'success':
            with self._lock:
                self._close_process()  # Reload the profile written by the official CLI.
        return result

    def upload(self, local_path, parent, name, checkpoint_dir):
        checkpoint = Path(checkpoint_dir)
        checkpoint.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint.chmod(0o700)
        return self._command(['upload', str(local_path), '--parent', parent, '--name', name,
                              '--checkpoint', str(checkpoint), '--watch'], timeout=86400)

    def _close_process(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        self._reader_stop.set()
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if self._reader:
            self._reader.join(timeout=2)
        if proc.stdout:
            proc.stdout.close()
        self._reader = self._messages = self._reader_stop = None

    def cancel(self):
        """Stop in-flight CLI work and reject new commands during shutdown."""
        self._cancelled.set()
        with self._process_lock:
            processes = [p for p in (self._proc, self._command_proc) if p is not None]
            for proc in processes:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
        for proc in processes:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

    def close(self):
        with self._lock:
            self._close_process()
