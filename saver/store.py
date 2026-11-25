# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Private, thread-safe SQLite job history shared by the worker and local UI."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit

from .sources import extract_links, safe_name


_DEFAULTS = {'root_name': '链接转存', 'proxy': '', 'auto_start': True,
             'local_relay': False, 'paused': False}
_STATES = {'new', 'creating', 'submitted', 'verified', 'error', 'unknown', 'relay'}
_MUTABLE = {'state', 'attempts', 'relay_attempts', 'task_id', 'file_id', 'parent_id',
            'submitted_url', 'submitted_at', 'error', 'bytes_done', 'method',
            'remote_attempts', 'verified_at', 'audit'}
_FOLDER_FIELDS = {'root_id', 'group_id', 'group_name', 'images_id', 'videos_id'}
_PUBLIC_ITEM_COLUMNS = ('id,root_name,group_key,group_title,filename,kind,size,'
                        'state,error,bytes_done,method,attempts')


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _root_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('目标根目录不能为空')
    return safe_name(value)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=30, check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA foreign_keys=ON')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS private (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, text TEXT NOT NULL,
                root_name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, root_name TEXT NOT NULL,
                group_key TEXT NOT NULL, group_title TEXT NOT NULL,
                filename TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
                mime TEXT NOT NULL, md5 TEXT, source TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'new', attempts INTEGER NOT NULL DEFAULT 0,
                relay_attempts INTEGER NOT NULL DEFAULT 0, task_id TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '',
                submitted_url TEXT NOT NULL DEFAULT '', submitted_at REAL,
                error TEXT NOT NULL DEFAULT '', bytes_done INTEGER NOT NULL DEFAULT 0,
                method TEXT NOT NULL DEFAULT '', remote_attempts TEXT NOT NULL DEFAULT '[]',
                verified_at REAL, audit TEXT NOT NULL DEFAULT 'null',
                UNIQUE(key, group_key, root_name)
            );
            CREATE TABLE IF NOT EXISTS batch_items (
                batch_id TEXT NOT NULL REFERENCES batches(id),
                item_id TEXT NOT NULL REFERENCES items(id),
                PRIMARY KEY(batch_id, item_id)
            );
            CREATE INDEX IF NOT EXISTS items_state ON items(state);
            CREATE INDEX IF NOT EXISTS batches_queued ON batches(state) WHERE state='queued';
            CREATE TABLE IF NOT EXISTS folders (
                root_name TEXT NOT NULL, group_key TEXT NOT NULL,
                root_id TEXT NOT NULL DEFAULT '', group_id TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '', images_id TEXT NOT NULL DEFAULT '',
                videos_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(root_name, group_key)
            );
        ''')

    @contextmanager
    def _transaction(self, write=True):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _settings(self):
        result = dict(_DEFAULTS)
        result.update({row['key']: json.loads(row['value']) for row in
                       self._db.execute('SELECT key, value FROM settings')})
        return result

    def settings(self):
        with self._lock:
            return self._settings()

    def set_settings(self, patch):
        if not isinstance(patch, dict) or set(patch) - set(_DEFAULTS):
            raise ValueError('设置字段无效')
        values = dict(patch)
        for key, value in values.items():
            if key == 'root_name':
                values[key] = _root_name(value)
            elif key == 'proxy':
                if not isinstance(value, str):
                    raise ValueError('代理必须使用 HTTP 或 HTTPS')
                try:
                    parts = urlsplit(value)
                    valid = not value or (parts.scheme in ('http', 'https') and parts.hostname
                                          and parts.username is None and parts.password is None
                                          and parts.port != 0 and not any(c.isspace() for c in value))
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError('代理必须使用有效的 HTTP 或 HTTPS 地址')
            elif type(value) is not bool:
                raise ValueError('开关设置必须为布尔值')
        with self._transaction():
            self._db.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                                 [(key, _json(value)) for key, value in values.items()])

    def private_get(self, key, default=None):
        with self._lock:
            row = self._db.execute('SELECT value FROM private WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def private_set(self, key, value):
        encoded = _json(value)
        with self._transaction():
            self._db.execute('INSERT OR REPLACE INTO private(key,value) VALUES (?,?)', (key, encoded))

    def new_batch(self, text, root_name=None):
        if not isinstance(text, str):
            raise ValueError('批量输入必须为文本')
        links = extract_links(text)
        if not links:
            raise ValueError('没有可识别的链接')
        with self._transaction():
            root = _root_name(root_name if root_name is not None else self._settings()['root_name'])
            batch_id = uuid.uuid4().hex
            self._db.execute('INSERT INTO batches(id,created_at,text,root_name) VALUES (?,?,?,?)',
                             (batch_id, time.time(), '\n'.join(links), root))
            return batch_id

    def queued_batch(self):
        with self._lock:
            row = self._db.execute("SELECT * FROM batches WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            return dict(row) if row else None

    def mark_batch(self, id, state, error=''):
        with self._transaction():
            cursor = self._db.execute('UPDATE batches SET state=?, error=? WHERE id=?', (state, error, id))
            if not cursor.rowcount:
                raise KeyError(id)

    @staticmethod
    def _filename(name, key, existing):
        name = safe_name(name)
        if name not in existing:
            existing.add(name)
            return name
        stem, extension = os.path.splitext(name)
        suffix = hashlib.sha256(key.encode('utf-8')).hexdigest()[:10]
        candidate = '%s [%s]%s' % (stem, suffix, extension)
        number = 2
        while candidate in existing:
            candidate = '%s [%s-%d]%s' % (stem, suffix, number, extension)
            number += 1
        existing.add(candidate)
        return candidate

    def ingest(self, batch_id, groups):
        with self._transaction():
            batch = self._db.execute('SELECT root_name FROM batches WHERE id=?', (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            root = batch['root_name']
            for group in groups:
                group_key = group['key']
                title = safe_name(group['title'])
                names = {}
                for item in group['items']:
                    if type(item['size']) is not int or item['size'] < 0:
                        raise ValueError('源文件大小无效')
                    row = self._db.execute('SELECT id FROM items WHERE key=? AND group_key=? AND root_name=?',
                                           (item['key'], group_key, root)).fetchone()
                    if row:
                        item_id = row['id']
                    else:
                        item_id = uuid.uuid4().hex
                        supported = item['kind'] in ('image', 'video')
                        kind = item['kind'] if supported else 'other'
                        if kind not in names:
                            names[kind] = {r['filename'] for r in self._db.execute(
                                'SELECT filename FROM items WHERE root_name=? AND group_key=? AND kind=?',
                                (root, group_key, kind))}
                        filename = self._filename(item['name'], item['key'], names[kind])
                        self._db.execute('''INSERT INTO items
                            (id,key,root_name,group_key,group_title,filename,kind,size,mime,md5,source,state,error)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (item_id, item['key'], root, group_key, title, filename, kind, item['size'],
                             item['mime'], item.get('md5'), _json(item['source']),
                             'new' if supported else 'error', '' if supported else '不支持的文件类型：' + filename))
                    self._db.execute('INSERT OR IGNORE INTO batch_items(batch_id,item_id) VALUES (?,?)',
                                     (batch_id, item_id))
            count = self._db.execute('SELECT COUNT(*) FROM batch_items WHERE batch_id=?', (batch_id,)).fetchone()[0]
            if not count:
                self._db.execute("UPDATE batches SET state='error',error='来源没有可识别的文件' WHERE id=?", (batch_id,))

    @staticmethod
    def _item(row):
        item = dict(row)
        for key in ('source', 'remote_attempts', 'audit'):
            item[key] = json.loads(item[key])
        return item

    @staticmethod
    def _where(statuses=None, batch_id=None):
        conditions = []
        arguments = []
        if statuses is not None:
            statuses = [statuses] if isinstance(statuses, str) else list(statuses)
            if not statuses:
                return ' WHERE 0', []
            conditions.append('i.state IN (%s)' % ','.join('?' for _ in statuses))
            arguments.extend(statuses)
        if batch_id is not None:
            conditions.append('EXISTS (SELECT 1 FROM batch_items b WHERE b.item_id=i.id AND b.batch_id=?)')
            arguments.append(batch_id)
        return (' WHERE ' + ' AND '.join(conditions) if conditions else ''), arguments

    def list_items(self, statuses=None, limit=1000, batch_id=None):
        if type(limit) is not int or limit < 0:
            raise ValueError('列表数量无效')
        where, arguments = self._where(statuses, batch_id)
        with self._lock:
            return [self._item(row) for row in self._db.execute(
                'SELECT i.* FROM items i' + where + ' ORDER BY i.rowid LIMIT ?', arguments + [limit])]

    def has_items(self, statuses=None, batch_id=None):
        where, arguments = self._where(statuses, batch_id)
        with self._lock:
            return self._db.execute('SELECT 1 FROM items i' + where + ' LIMIT 1', arguments).fetchone() is not None

    def active_counts(self):
        with self._lock:
            row = self._db.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(method='relay'),0) AS relay FROM items
                WHERE state IN ('creating','unknown','submitted')""").fetchone()
            return row['total'], row['relay']

    def is_verified(self, item_id):
        with self._lock:
            return self._db.execute("SELECT 1 FROM items WHERE id=? AND state='verified'",
                                    (item_id,)).fetchone() is not None

    def iter_items(self, statuses, chunk_size=100):
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError('分块数量无效')
        where, arguments = self._where(statuses)
        last = 0
        while True:
            with self._lock:
                rows = list(self._db.execute('SELECT i.rowid AS cursor_row,i.* FROM items i' +
                    where + (' AND ' if where else ' WHERE ') +
                    'i.rowid>? ORDER BY i.rowid LIMIT ?', arguments + [last, chunk_size]))
            if not rows:
                return
            for row in rows:
                last = row['cursor_row']
                item = self._item(row)
                del item['cursor_row']
                yield item

    def update_item(self, id, **fields):
        if set(fields) - _MUTABLE:
            raise ValueError('任务字段无效')
        if 'state' in fields and fields['state'] not in _STATES:
            raise ValueError('任务状态无效')
        values = dict(fields)
        for key in ('attempts', 'relay_attempts', 'bytes_done'):
            if key in values and (type(values[key]) is not int or values[key] < 0):
                raise ValueError('任务计数无效')
        if 'remote_attempts' in values and not isinstance(values['remote_attempts'], list):
            raise ValueError('远端尝试记录必须为列表')
        for key in ('remote_attempts', 'audit'):
            if key in values:
                values[key] = _json(values[key])
        if not values:
            return
        with self._transaction():
            query = 'UPDATE items SET %s WHERE id=?' % ','.join(key + '=?' for key in values)
            cursor = self._db.execute(query, list(values.values()) + [id])
            if not cursor.rowcount:
                raise KeyError(id)

    def folder(self, root_name, group_key):
        with self._lock:
            row = self._db.execute('SELECT * FROM folders WHERE root_name=? AND group_key=?',
                                   (root_name, group_key)).fetchone()
            return dict(row) if row else None

    def claim_group_name(self, root_name, group_key, title):
        with self._transaction():
            row = self._db.execute('SELECT group_name FROM folders WHERE root_name=? AND group_key=?',
                                   (root_name, group_key)).fetchone()
            # Older releases persisted every claimed name here before creating its remote folder.
            self._db.execute('DELETE FROM private WHERE key=?', ('folder_names:' + root_name,))
            if row and row['group_name']:
                return row['group_name']
            title = safe_name(title)
            name = title
            suffix = hashlib.sha256(group_key.encode()).hexdigest()[:10]
            number = 1
            while self._db.execute('SELECT 1 FROM folders WHERE root_name=? AND group_name=? AND group_key<>? LIMIT 1',
                                   (root_name, name, group_key)).fetchone():
                name = '%s [%s%s]' % (title, suffix, '' if number == 1 else '-' + str(number))
                number += 1
            self._db.execute('INSERT OR IGNORE INTO folders(root_name,group_key) VALUES (?,?)',
                             (root_name, group_key))
            self._db.execute('UPDATE folders SET group_name=? WHERE root_name=? AND group_key=?',
                             (name, root_name, group_key))
            return name

    def save_folder(self, root_name, group_key, **fields):
        if set(fields) - _FOLDER_FIELDS:
            raise ValueError('目录字段无效')
        with self._transaction():
            self._db.execute('INSERT OR IGNORE INTO folders(root_name,group_key) VALUES (?,?)',
                             (root_name, group_key))
            if fields:
                query = 'UPDATE folders SET %s WHERE root_name=? AND group_key=?' % ','.join(key + '=?' for key in fields)
                self._db.execute(query, list(fields.values()) + [root_name, group_key])

    def recover(self):
        with self._transaction():
            self._db.execute("UPDATE items SET state='unknown' WHERE state='creating'")
            self._db.execute("UPDATE batches SET state='queued' WHERE state IN ('discovering','parsing')")

    def retry_failed(self, batch_id=None):
        with self._transaction():
            batch_filter = ''
            arguments = []
            if batch_id is not None:
                batch_filter = ' AND id IN (SELECT item_id FROM batch_items WHERE batch_id=?)'
                arguments.append(batch_id)
            self._db.execute("""UPDATE items SET state='new',attempts=0,relay_attempts=0,
                task_id='',file_id='',submitted_url='',submitted_at=NULL,error='',bytes_done=0,
                method='',verified_at=NULL WHERE state='error' AND kind IN ('image','video')""" + batch_filter,
                arguments)
            self._db.execute("UPDATE batches SET state='queued',error='' WHERE error<>''" +
                             (' AND id=?' if batch_id is not None else ''), arguments)

    def snapshot(self):
        # Aggregate in SQLite; private source/history blobs never enter the UI snapshot.
        with self._transaction(write=False):
            summary = dict(self._db.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(state='verified'),0) AS verified,
                COALESCE(SUM(state IN ('new','creating','submitted','relay')),0) AS active,
                COALESCE(SUM(state='new'),0) AS waiting,
                COALESCE(SUM(state='error'),0) AS failed,
                COALESCE(SUM(state IN ('unknown','error')),0) AS needs_attention,
                COALESCE(SUM(size),0) AS bytes_total, COALESCE(SUM(bytes_done),0) AS bytes_done
                FROM items""").fetchone())
            batches = []
            for row in self._db.execute("""SELECT b.id,b.created_at,b.root_name,b.state,b.error,
                COUNT(i.id) AS total, COALESCE(SUM(i.state='verified'),0) AS verified,
                COALESCE(SUM(i.state IN ('unknown','error')),0) AS needs_attention,
                COALESCE(SUM(i.size),0) AS bytes_total, COALESCE(SUM(i.bytes_done),0) AS bytes_done,
                COALESCE(SUM(i.kind='image'),0) AS images, COALESCE(SUM(i.kind='video'),0) AS videos
                FROM batches b LEFT JOIN batch_items bi ON bi.batch_id=b.id
                LEFT JOIN items i ON i.id=bi.item_id GROUP BY b.id ORDER BY b.rowid DESC"""):
                batch = dict(row)
                if batch['total'] and (batch['error'] or batch['needs_attention']):
                    batch['state'] = 'needs_attention'
                elif (batch['total'] and batch['verified'] == batch['total'] and not batch['error']
                      and batch['state'] not in ('queued', 'discovering', 'parsing')):
                    batch['state'] = 'completed'
                batches.append(batch)
            groups = [dict(row) for row in self._db.execute("""SELECT g.*,i.group_title AS title FROM (
                SELECT root_name,group_key,MIN(rowid) AS first_row,COUNT(*) AS total,
                SUM(state='verified') AS verified, SUM(kind='image') AS images, SUM(kind='video') AS videos,
                SUM(kind='image' AND state='verified') AS images_verified,
                SUM(kind='video' AND state='verified') AS videos_verified,
                SUM(state IN ('unknown','error')) AS needs_attention,
                SUM(size) AS bytes_total,SUM(bytes_done) AS bytes_done
                FROM items GROUP BY root_name,group_key) g
                JOIN items i ON i.rowid=g.first_row ORDER BY g.first_row""")]
            recent = [dict(row) for row in self._db.execute(
                'SELECT ' + _PUBLIC_ITEM_COLUMNS + ' FROM items ORDER BY rowid DESC LIMIT 100')]
            attention = [dict(row) for row in self._db.execute('SELECT ' + _PUBLIC_ITEM_COLUMNS +
                " FROM items WHERE state IN ('error','unknown') ORDER BY rowid")]
            return {'settings': self._settings(), 'summary': summary,
                    'batches': batches, 'items': recent, 'groups': groups, 'attention': attention}

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
