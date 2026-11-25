# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Durable, bounded transfer scheduling with exact remote verification."""
import copy
import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time

from .sources import Network, extract_links


MAX_CLOUD_ATTEMPTS = 3


def _complete(value):
    return str(value).upper() in ('COMPLETE', 'PHASE_TYPE_COMPLETE', 'COMPLETE (PHASE_TYPE_COMPLETE)')


def _failed(value):
    return str(value).upper() in ('ERROR', 'PHASE_TYPE_ERROR', 'ERROR (PHASE_TYPE_ERROR)')


def _live(file):
    return file.get('trashed') in (False, 'false', None, '')


def _size(file):
    try:
        return int(file['size'])
    except (KeyError, ValueError, TypeError):
        return -1


class Engine:
    def __init__(self, store, resolver, gateway, data_dir, max_active=20):
        self.store, self.resolver, self.gateway = store, resolver, gateway
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_active = max(1, int(max_active))
        self._lock = threading.RLock()
        self._account_lock = threading.RLock()
        self._public = {'connected': False, 'status': 'checking', 'name': '', 'error': '', 'device': None}
        self._identity_ok = False
        self._auth_checked = 0
        self._auth_interval = 60
        self._device = None
        self._proxy = store.settings()['proxy']
        self._stop = threading.Event()
        if isinstance(resolver.network, Network):
            resolver.network.stop_event = self._stop
        self._cleanup_at = 0
        self._thread = None
        self.store.recover()

    def account(self):
        with self._account_lock:
            return copy.deepcopy(self._public)

    def _public_update(self, **fields):
        with self._account_lock:
            self._public.update(fields)

    def start_auth(self):
        with self._lock:
            # A retry must reuse a valid session, not create another device grant.
            self._refresh_account(force=True)
            if self._identity_ok or self.account()['status'] == 'error':
                return self.account()
            try:
                response = self.gateway.auth_start()
                if response.get('device_code'):
                    now = time.time()
                    expires = now + max(1, int(response.get('expires_in', 300)))
                    interval = max(2, int(response.get('interval', 5)))
                    self._device = {'code': response['device_code'], 'expires': expires,
                                    'interval': interval, 'next_poll': now + interval}
                    self._public_update(status='disconnected', error='', device={
                        'user_code': str(response.get('user_code', '')),
                        'verification_uri': str(response.get('verification_uri', '')),
                        'expires_at': expires})
                else:
                    self._auth_checked = 0
                    self._refresh_account()
            except Exception:
                self._public_update(status='error', error='设备授权暂时不可用，请检查网络后重试')
            return self.account()

    def _refresh_account(self, force=False):
        now = time.time()
        if self._device:
            if now >= self._device['expires']:
                self._device = None
                self._public_update(device=None, error='设备授权已过期，请重新授权')
            elif now >= self._device['next_poll']:
                self._device['next_poll'] = now + self._device['interval']
                try:
                    result = self.gateway.auth_continue(self._device['code'])
                    if result.get('status') == 'success':
                        self._device = None
                        self._public_update(device=None)
                        self._auth_checked = 0
                    elif result.get('status') in ('expired_token', 'access_denied'):
                        self._device = None
                        self._public_update(device=None, error='设备授权未完成，请重新授权')
                    elif result.get('status') == 'slow_down':
                        self._device['interval'] += 5
                except Exception:
                    self._public_update(error='设备授权状态暂时不可用')
        if not force and self._auth_checked and now - self._auth_checked < self._auth_interval:
            return
        self._auth_checked = now
        self._auth_interval = 60
        self._identity_ok = False
        try:
            status = self.gateway.auth_status()
            connected = bool(status.get('logged_in'))
            identity = str(status.get('user_id') or '')
            name = str(status.get('name') or '') if connected else ''
            saved = self.store.private_get('account_id')
            if connected and identity:
                if saved and saved != identity:
                    self._public_update(connected=True, status='error', name=name, error='账户已更换，现有任务已停止，请恢复原账户')
                    return
                if not saved:
                    self.store.private_set('account_id', identity)
                self._identity_ok = True
                self._device = None
                self._public_update(device=None)
            self._public_update(connected=connected, name=name,
                                status='connected' if self._identity_ok else 'error' if connected else 'disconnected',
                                error='暂时无法识别账户身份，请重试连接' if connected and not identity else '')
        except Exception:
            self._auth_interval = 10
            self._public_update(status='error', error='暂时无法检查连接，请检查网络或代理；程序会自动重试')

    def _pages(self, tool, key, **arguments):
        token, seen = '', set()
        while True:
            result = self.gateway.call(tool, token=token, limit=100, **arguments)
            if not isinstance(result, dict) or not isinstance(result.get(key), list):
                raise RuntimeError('远端列表格式无效')
            for row in result[key]:
                if not isinstance(row, dict):
                    raise RuntimeError('远端列表项目格式无效')
                yield row
            token = result.get('next_page_token') or ''
            if not token:
                break
            if token in seen:
                raise RuntimeError('远端分页未完成')
            seen.add(token)

    def _files(self, parent):
        return self._pages('ls', 'files', parent_id=parent, raw=True)

    def _mkdir(self, parent, name):
        matches = [file for file in self._files(parent) if _live(file) and file.get('name') == name]
        folders = [file for file in matches if file.get('kind') in ('folder', 'drive#folder')]
        if len(folders) == 1 and len(matches) == 1:
            return folders[0]['id']
        if matches:
            raise RuntimeError('目标目录名称存在冲突')
        result = self.gateway.call('mkdir', name=name, parent=parent)
        if not result.get('id'):
            raise RuntimeError('目录创建结果待核对')
        return result['id']

    def _parent(self, item):
        root, group = item['root_name'], item['group_key']
        cached = self.store.folder(root, group) or {}
        if not cached.get('root_id'):
            id = self._mkdir('', root)
            self.store.save_folder(root, group, root_id=id)
            cached['root_id'] = id
        if not cached.get('group_id'):
            # Distinct logical groups sharing a title must keep separate copies.
            title = self.store.claim_group_name(root, group, item['group_title'])
            id = self._mkdir(cached['root_id'], title)
            self.store.save_folder(root, group, group_id=id)
            cached['group_id'] = id
        for field, name in (('images_id', '图片'), ('videos_id', '视频')):
            if not cached.get(field):
                id = self._mkdir(cached['group_id'], name)
                self.store.save_folder(root, group, **{field: id})
                cached[field] = id
        return cached['images_id' if item['kind'] == 'image' else 'videos_id']

    def _discover(self):
        batch = self.store.queued_batch()
        if not batch:
            return
        self.store.mark_batch(batch['id'], 'discovering')
        errors = []
        links = extract_links(batch['text'])
        for index, link in enumerate(links, 1):
            if self._stop.is_set():
                self.store.mark_batch(batch['id'], 'queued')
                return
            try:
                group = self.resolver.resolve(link)
                self.store.ingest(batch['id'], [group])
                if group.get('warnings'):
                    errors.append('第 %d 个来源有未能解析的内容' % index)
            except Exception:
                errors.append('第 %d 个来源解析失败，请重试或检查链接' % index)
        if self._stop.is_set():
            self.store.mark_batch(batch['id'], 'queued')
        elif self.store.has_items(batch_id=batch['id']):
            self.store.mark_batch(batch['id'], 'active', error='；'.join(errors))
        else:
            self.store.mark_batch(batch['id'], 'error', error='；'.join(errors) or '来源没有可识别的文件')

    def _verified(self, item, file):
        return (_live(file) and file.get('name') == item['filename']
                and file.get('parent_id') == item['parent_id']
                and _size(file) == item['size'] and _complete(file.get('phase'))
                and file.get('kind') not in ('folder', 'drive#folder'))

    def _accept(self, item, file):
        self.store.update_item(item['id'], state='verified', file_id=file['id'],
                               bytes_done=item['size'], verified_at=time.time(), error='', audit=file.get('audit'))
        self._clean_local_stage(item['id'])
        self._clean_staging_folders(item)

    def _clean_local_stage(self, item_id):
        root = self.data_dir / 'staging'
        stage = root / item_id
        if not re.fullmatch(r'[0-9a-f]{32}', item_id) or root.is_symlink() or stage.is_symlink():
            return
        try:
            if stage.is_dir():
                shutil.rmtree(str(stage))
            root.rmdir()
        except OSError:
            # Retry after a verified commit, including after a crash before cleanup.
            pass

    def _clean_verified_stages(self):
        root = self.data_dir / 'staging'
        if root.is_dir() and not root.is_symlink():
            for stage in root.iterdir():
                if re.fullmatch(r'[0-9a-f]{32}', stage.name) and self.store.is_verified(stage.name):
                    self._clean_local_stage(stage.name)

    def _clean_staging_folders(self, item):
        parents = {attempt.get('parent_id') for attempt in item['remote_attempts']}
        for id in parents - {item['parent_id'], '', None}:
            try:
                folder = self.gateway.call('get', id=id, raw=True)
                owned_name = (folder.get('name', '').startswith('.链接转存重试-' + item['id'] + '-')
                              or folder.get('name') == '.链接转存中转-' + item['id'])
                if (_live(folder) and owned_name and folder.get('kind') in ('folder', 'drive#folder')
                        and folder.get('parent_id') == item['parent_id']
                        and next(self._files(id), None) is None):
                    self.gateway.call('rm', ids=[id])
            except Exception:
                continue

    @staticmethod
    def _owned_failure(item, file):
        return any(attempt.get('status') == 'error' and attempt.get('file_id') == file.get('id')
                   and attempt.get('parent_id') == file.get('parent_id')
                   and attempt.get('url') and attempt['url'] == file.get('params', {}).get('url')
                   and not _complete(file.get('phase')) for attempt in item['remote_attempts'])

    def _clean_failed(self, item):
        for attempt in item['remote_attempts']:
            if attempt.get('status') != 'error' or not attempt.get('file_id'):
                continue
            try:
                file = self.gateway.call('get', id=attempt['file_id'], raw=True)
                if _live(file) and self._owned_failure(item, file):
                    self.gateway.call('rm', ids=[file['id']])
                if attempt.get('task_id'):
                    self.gateway.call('task_rm', ids=[attempt['task_id']], **{'delete-files': False})
            except Exception:
                # Verification is retained even when cleanup is unavailable.
                continue

    def _history(self, item, **fields):
        history = list(item['remote_attempts'])
        if history:
            history[-1] = dict(history[-1], **fields)
            self.store.update_item(item['id'], remote_attempts=history)
        return history

    def _submit(self, item):
        if self._stop.is_set():
            return
        try:
            parent = item['parent_id'] or self._parent(item)
            self.store.update_item(item['id'], parent_id=parent)
            item = dict(item, parent_id=parent)
            matches = [file for file in self._files(parent) if _live(file) and file.get('name') == item['filename']
                       and not self._owned_failure(item, file)]
            if matches:
                if len(matches) == 1:
                    file = self.gateway.call('get', id=matches[0]['id'], raw=True)
                    if self._verified(item, file):
                        self._accept(item, file)
                        return
                self.store.update_item(item['id'], state='error', error='目标目录存在同名文件，未覆盖')
                return
            url = self.resolver.download_url(item['source'])
        except Exception:
            self.store.update_item(item['id'], state='error', error='来源地址或目标目录不可用，请检查后重试')
            return
        remote_parent = parent
        if item['remote_attempts']:
            try:
                remote_parent = self._mkdir(parent, '.链接转存重试-%s-%d' % (item['id'], len(item['remote_attempts']) + 1))
            except Exception:
                self.store.update_item(item['id'], state='error', error='重试目录不可用，请检查后重试')
                return
        if self._stop.is_set() or self.store.settings()['paused']:
            return
        history = list(item['remote_attempts'])
        history.append({'url': url, 'parent_id': remote_parent, 'submitted_at': time.time(), 'method': 'cloud'})
        self.store.update_item(item['id'], state='creating', submitted_url=url,
                               submitted_at=time.time(), attempts=item['attempts'] + 1,
                               method='cloud', remote_attempts=history, task_id='', file_id='', error='')
        try:
            result = self.gateway.call('add_link', url=url, parent=remote_parent)
            if not result.get('task_id'):
                raise RuntimeError('提交结果待核对')
            history[-1].update(task_id=result['task_id'], file_id=result.get('file_id', ''))
            self.store.update_item(item['id'], state='submitted', task_id=result['task_id'],
                                   file_id=result.get('file_id', ''), remote_attempts=history)
        except Exception:
            self.store.update_item(item['id'], state='unknown', error='云端提交结果不明，正在核对，未重复提交')

    def _check(self, item):
        try:
            file_id = item['file_id']
            if item['task_id']:
                task = self.gateway.call('task_get', id=item['task_id'])
                file_id = task.get('file_id') or file_id
                if file_id:
                    self.store.update_item(item['id'], file_id=file_id)
                    item = dict(item, file_id=file_id)
                if _failed(task.get('status', task.get('phase'))):
                    history = self._history(item, status='error', file_id=file_id)
                    if item['attempts'] < MAX_CLOUD_ATTEMPTS:
                        state = 'new'
                    else:
                        state = 'relay' if self.store.settings()['local_relay'] else 'error'
                    self.store.update_item(item['id'], state=state, task_id='', file_id='',
                                           error='云端下载失败' if state == 'error' else '', remote_attempts=history)
                    return
                if not _complete(task.get('status', task.get('phase'))):
                    return
            if not file_id:
                return
            file = self.gateway.call('get', id=file_id, raw=True)
            self.store.update_item(item['id'], audit=file.get('audit'))
            if not _live(file):
                self.store.update_item(item['id'], state='error', error='远端文件已删除')
                return
            if not _complete(file.get('phase')):
                return
            if _size(file) != item['size']:
                self.store.update_item(item['id'], state='error', error='远端文件字节数不匹配，未确认保存')
                return
            if file.get('name') != item['filename']:
                conflicts = [f for f in self._files(file.get('parent_id', '')) if _live(f)
                             and f.get('name') == item['filename'] and f.get('id') != file_id]
                if conflicts:
                    self.store.update_item(item['id'], state='error', error='目标目录存在同名文件，未覆盖')
                    return
                self.gateway.call('rename', id=file_id, name=item['filename'])
                file = self.gateway.call('get', id=file_id, raw=True)
            # Verify the complete file under its exact name before removing any
            # failed artifact belonging to earlier attempts of this same item.
            staged = dict(item, parent_id=file.get('parent_id'))
            if not self._verified(staged, file):
                return
            self._clean_failed(item)
            if file.get('parent_id') != item['parent_id']:
                conflicts = [f for f in self._files(item['parent_id']) if _live(f)
                             and f.get('name') == item['filename'] and f.get('id') != file_id]
                if conflicts:
                    self.store.update_item(item['id'], state='error', error='目标目录存在同名文件，未覆盖')
                    return
                self.gateway.call('mv', id=file_id, target=item['parent_id'])
                file = self.gateway.call('get', id=file_id, raw=True)
            if self._verified(item, file):
                self._accept(item, file)
        except Exception:
            self.store.update_item(item['id'], error='远端状态暂时无法核对，将继续检查')

    def _reconcile(self, item):
        try:
            if item['task_id'] or item['file_id']:
                self.store.update_item(item['id'], state='submitted')
                self._check(item)
                return
            if not item['parent_id']:
                return
            remote_parent = item['remote_attempts'][-1].get('parent_id', item['parent_id']) if item['remote_attempts'] else item['parent_id']
            files = self._files(remote_parent)
            if item['method'] == 'relay':
                named = [f for f in files if _live(f) and f.get('name') == item['filename']
                         and f.get('kind') not in ('folder', 'drive#folder')]
                if len(named) == 1 and _size(named[0]) == item['size'] and _complete(named[0].get('phase')):
                    self.store.update_item(item['id'], state='submitted', file_id=named[0]['id'], error='')
                    self._history(item, file_id=named[0]['id'])
                    return
                stage = self.data_dir / 'staging' / item['id']
                checkpoint = stage / 'checkpoints'
                resumable = (checkpoint.is_dir() and any(p.is_file() and not p.is_symlink()
                             for p in checkpoint.rglob('*')))
                if (resumable and len(named) <= 1 and not self.store.settings()['paused']
                        and (not named or _size(named[0]) == item['size'])
                        and self._local_valid(stage / 'payload.part', item)):
                    # The official CLI owns checkpoint interpretation. Re-enter
                    # exactly the same upload, with the same path/parent/name.
                    self._upload(item, stage / 'payload.part', remote_parent,
                                 list(item['remote_attempts']))
                else:
                    self.store.update_item(item['id'], error='上传结果仍待确认，缺少可安全续传的文件或检查点')
                return
            candidates = {f['id']: f for f in files if _live(f)
                          and item['submitted_url'] and f.get('params', {}).get('url') == item['submitted_url']}
            tasks = {}
            for task in self._pages('task_ls', 'tasks', type='offline', phase='pending,running,error,complete'):
                detail = self.gateway.call('task_get', id=task['id'])
                id = detail.get('file_id')
                if not id:
                    continue
                file = self.gateway.call('get', id=id, raw=True)
                if (_live(file) and file.get('parent_id') == remote_parent
                        and item['submitted_url'] and file.get('params', {}).get('url') == item['submitted_url']):
                    candidates[id] = file
                    tasks[id] = task['id']
            if len(candidates) == 1:
                id = next(iter(candidates))
                task_id = tasks.get(id, '')
                self.store.update_item(item['id'], state='submitted', file_id=id, task_id=task_id, error='')
                self._history(item, file_id=id, task_id=task_id)
            else:
                self.store.update_item(item['id'], error='提交结果仍无法唯一确认，未重复提交')
        except Exception:
            self.store.update_item(item['id'], error='提交结果暂时无法核对，未重复提交')

    def _relay(self, item):
        stage = self.data_dir / 'staging' / item['id']
        path = stage / 'payload.part'
        try:
            if stage.parent.is_symlink() or stage.is_symlink() or path.is_symlink():
                raise ValueError('中转目录不能是符号链接')
            stage.mkdir(parents=True, exist_ok=True, mode=0o700)
            stage.chmod(0o700)
            if not path.exists() and item['size'] == 0:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            current = path.stat().st_size if path.exists() else 0
            if current > item['size']:
                path.unlink()
                raise ValueError('本地文件大小无效')
            if current < item['size']:
                url = self.resolver.download_url(item['source'])
                with self.resolver.network.open_binary(url, start=current) as response:
                    status = getattr(response, 'status', 200)
                    response_end = item['size']
                    if status == 206:
                        value = response.headers.get('Content-Range', '')
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', value)
                        if (not match or int(match[1]) != current or int(match[3]) != item['size']
                                or not current <= int(match[2]) < item['size']):
                            raise ValueError('续传范围无效')
                        response_end = int(match[2]) + 1
                    elif status == 200:
                        current = 0
                    else:
                        raise ValueError('下载响应无效')
                    with path.open('ab' if current else 'wb') as out:
                        os.chmod(str(path), 0o600)
                        last_progress = time.monotonic()
                        while not self._stop.is_set():
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            current += len(chunk)
                            if current > response_end:
                                raise ValueError('响应内容超出声明范围')
                            out.write(chunk)
                            if time.monotonic() - last_progress >= 1:
                                self.store.update_item(item['id'], bytes_done=current)
                                last_progress = time.monotonic()
                        out.flush()
                        os.fsync(out.fileno())
                    self.store.update_item(item['id'], bytes_done=current)
                    if not self._stop.is_set() and current != response_end:
                        raise OSError('下载响应未完成，可以续传')
                if self._stop.is_set():
                    return
            if path.stat().st_size != item['size']:
                raise OSError('下载中断，可以续传')
            if not self._local_valid(path, item):
                path.unlink()
                raise ValueError('源文件校验不匹配')
            if self._stop.is_set() or self.store.settings()['paused']:
                return
            # An isolated owned upload folder avoids names from failed cloud tasks.
            key = 'relay_folder:' + item['id']
            remote = self.store.private_get(key)
            if not remote:
                remote = self._mkdir(item['parent_id'], '.链接转存中转-' + item['id'])
                self.store.private_set(key, remote)
            history = list(item['remote_attempts'])
            history.append({'method': 'relay', 'parent_id': remote, 'submitted_at': time.time()})
            self._upload(item, path, remote, history)
        except ValueError:
            self.store.update_item(item['id'], state='error', error='本地文件大小、续传范围或 MD5 校验失败')
        except Exception:
            # No upload has been started yet. Keep the owned partial for resume.
            self.store.update_item(item['id'], state='relay', error='本机下载暂时中断，将继续续传')

    @staticmethod
    def _local_valid(path, item):
        if not path.is_file() or path.stat().st_size != item['size']:
            return False
        if item.get('md5'):
            digest = hashlib.md5()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            return digest.hexdigest().lower() == item['md5'].lower()
        return True

    def _upload(self, item, path, remote, history):
        if self._stop.is_set() or self.store.settings()['paused']:
            return
        self.store.update_item(item['id'], state='creating', method='relay', task_id='', file_id='',
                               submitted_url='', submitted_at=time.time(),
                               relay_attempts=item['relay_attempts'] + 1, remote_attempts=history)
        try:
            result = self.gateway.upload(path, remote, item['filename'], path.parent / 'checkpoints')
            if not result.get('file_id'):
                raise RuntimeError('上传结果待核对')
            history[-1]['file_id'] = result['file_id']
            self.store.update_item(item['id'], state='submitted', file_id=result['file_id'],
                                   remote_attempts=history, error='')
        except Exception:
            self.store.update_item(item['id'], state='unknown', error='上传结果不明，正在核对，未重复上传')

    def tick(self):
        with self._lock:
            if time.monotonic() >= self._cleanup_at:
                self._cleanup_at = time.monotonic() + 60
                self._clean_verified_stages()
            proxy = self.store.settings()['proxy']
            if proxy != self._proxy:
                self.gateway.close()
                self.gateway.proxy = proxy
                self.resolver.network = Network(proxy, stop_event=self._stop)
                self._proxy = proxy
                self._auth_checked = 0
            working = self.store.has_items(['new', 'relay', 'unknown', 'submitted'])
            self._refresh_account(force=working and self._identity_ok)
            self._discover()
            if self._stop.is_set():
                return
            # Discovery can take minutes; recheck if the cached identity has expired.
            self._refresh_account()
            if not self._identity_ok:
                return
            for item in self.store.iter_items(['unknown', 'submitted']):
                if self._stop.is_set():
                    return
                if item['state'] == 'unknown':
                    self._reconcile(item)
                else:
                    self._check(item)
            if self._stop.is_set() or self.store.settings()['paused']:
                return
            active, relay_active = self.store.active_counts()
            slots = max(0, self.max_active - active)
            if not slots:
                return
            # Stage at most one local file each tick, serialized with other work.
            relays = self.store.list_items('relay', limit=1)
            if (relays and self.store.settings()['local_relay']
                    and not relay_active):
                self._relay(relays[0])
                slots -= 1
            for item in self.store.list_items('new', limit=slots):
                if self._stop.is_set() or self.store.settings()['paused']:
                    break
                self._submit(item)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self._public_update(error='后台处理暂时失败，将继续检查')
            self._stop.wait(2)

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='pikpak-saver', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self.gateway.cancel()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=50)
