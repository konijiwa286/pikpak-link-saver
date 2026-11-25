# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Public source discovery. No account credentials or script execution."""
import ast
from collections import deque
import html
from html.entities import html5
from html.parser import HTMLParser
import json
import mimetypes
import re
import unicodedata
from urllib.parse import parse_qs, parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener


_RESERVED = str.maketrans('\\/:*?"<>|', '＼／：＊？＂＜＞｜')


def safe_name(value):
    value = ''.join(c for c in str(value) if unicodedata.category(c) != 'Cc')
    value = value.translate(_RESERVED).strip()
    value = re.sub(r'\.+$', lambda m: '．' * len(m[0]), value)
    return value or '未命名'


def extract_links(text):
    # Semicolon-free names such as &not= are raw query keys, not entities.
    text = re.sub(r'&(?:#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);',
                  lambda match: html.unescape(match[0])
                  if match[0][1] == '#' or match[0][1:] in html5 else match[0], text)
    text = re.sub(r'\\+&', '&', text)
    result = []
    seen = set()
    for match in re.finditer(r'https?://[^\s<>"\'\[\]）]+', text):
        url = match[0]
        while url.endswith(')') and url.count(')') > url.count('('):
            url = url[:-1]
        url = urldefrag(url)[0]
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


class Network:
    def __init__(self, proxy='', stop_event=None, max_metadata_bytes=16 * 1024 * 1024):
        if type(max_metadata_bytes) is not int or max_metadata_bytes <= 0:
            raise ValueError('元数据大小限制必须为正整数')
        if proxy and urlsplit(proxy).scheme not in ('http', 'https'):
            raise ValueError('代理必须使用 HTTP 或 HTTPS')
        self.opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy}) if proxy else ProxyHandler())
        self.stop_event = stop_event
        self.max_metadata_bytes = max_metadata_bytes

    def _open(self, url, method='GET', json_body=None, headers=None):
        if self.stop_event is not None and self.stop_event.is_set():
            raise OSError('来源请求已停止')
        request_headers = {'User-Agent': 'Mozilla/5.0 PikPak-Link-Saver/1.0'}
        request_headers.update(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        return self.opener.open(Request(url, data=body, headers=request_headers, method=method), timeout=45)

    def text(self, url, method='GET', json_body=None):
        with self._open(url, method, json_body) as response:
            data = response.read(self.max_metadata_bytes + 1)
            if len(data) > self.max_metadata_bytes:
                raise ValueError('来源页面超过元数据大小限制，请拆分来源')
            return data.decode(response.headers.get_content_charset() or 'utf-8')

    def json(self, url, method='GET', json_body=None):
        return json.loads(self.text(url, method, json_body))

    def open_binary(self, url, start=0):
        if start < 0:
            raise ValueError('下载偏移不能为负数')
        return self._open(url, headers={'Range': 'bytes=%d-' % start} if start else {})


class _Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.title = []
        self.heading = []
        self._tag = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag in ('h1', 'title'):
            self._tag = tag
        if tag == 'a':
            self.links.extend(value for key, value in attrs if key == 'href' and value)

    def handle_endtag(self, tag):
        if tag == self._tag:
            self._tag = None

    def handle_data(self, data):
        if self._tag == 'h1':
            self.heading.append(data)
        elif self._tag == 'title':
            self.title.append(data)


class _Literal:
    """Small JavaScript data-literal reader; rejects executable expressions."""
    token = re.compile(r'\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|-?\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|[\[\]{},:])', re.S)

    def __init__(self, text):
        self.text = text
        self.pos = 0

    def take(self):
        match = self.token.match(self.text, self.pos)
        if not match:
            raise ValueError('来源包含无效的数据字面量')
        self.pos = match.end()
        return match[1]

    def value(self, token=None):
        token = self.take() if token is None else token
        if token in ('[', '{'):
            is_object = token == '{'
            end = '}' if is_object else ']'
            result = {} if is_object else []
            token = self.take()
            while token != end:
                if is_object:
                    key = self.string(token) if token[0] in '\'"' else token
                    if not re.fullmatch(r'[A-Za-z_$][\w$]*', key) and token[0] not in '\'"':
                        raise ValueError('无效字段名')
                    if self.take() != ':':
                        raise ValueError('来源字段缺少冒号')
                    result[key] = self.value()
                else:
                    result.append(self.value(token))
                token = self.take()
                if token == end:
                    break
                if token != ',':
                    raise ValueError('来源包含非数据表达式')
                token = self.take()
            return result
        if token[0] in '\'"':
            return self.string(token)
        if token in ('null', 'true', 'false'):
            return {'null': None, 'true': True, 'false': False}[token]
        if re.fullmatch(r'-?\d+(?:\.\d+)?', token):
            return float(token) if '.' in token else int(token)
        raise ValueError('来源包含非数据表达式')

    @staticmethod
    def string(token):
        if token.startswith('"'):
            return json.loads(token)
        return ast.literal_eval(token.replace('\\/', '/'))


def _query(url, **changes):
    parts = urlsplit(url)
    pairs = []
    remaining = dict(changes)
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        pairs.append((key, str(remaining.pop(key)) if key in remaining else value))
    pairs.extend((k, str(v)) for k, v in remaining.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ''))


def _kind(mime):
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/'):
        return 'video'
    return 'other'


def _group(key, title, items):
    result = []
    seen = set()
    for item in items:
        if item['key'] not in seen:
            seen.add(item['key'])
            result.append(item)
    warnings = ['不支持的文件类型：' + i['name'] for i in result if i['kind'] == 'other']
    if not result:
        raise ValueError('来源没有可识别的文件')
    return {'key': key, 'title': safe_name(title), 'warnings': warnings, 'items': result}


class Resolver:
    def __init__(self, network=None):
        self.network = network if network is not None else Network()

    def resolve(self, url):
        url = urldefrag(url)[0]
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        if self._bunkr(url):
            return self._album(url)
        if host in ('balbums.st', 'www.balbums.st'):
            return self._search(url)
        if host in ('disk.yandex.com', 'disk.yandex.ru', 'yadi.sk') and parts.path:
            return self._yandex(url)
        raise ValueError('不支持的链接：' + url)

    @staticmethod
    def _bunkr(url):
        parts = urlsplit(url)
        return bool(re.fullmatch(r'(?:www\.)?bunkr\.[a-z.]+', parts.hostname or '') and re.fullmatch(r'/a/[\w-]+/?', parts.path))

    def _album(self, url):
        album_id = urlsplit(url).path.rstrip('/').split('/')[-1]
        first = _query(url, advanced=1)
        pending = deque([first])
        scheduled = {first}
        items = []
        title = album_id
        while pending:
            page_url = pending.popleft()
            text = self.network.text(page_url)
            page = _Page(text)
            title = ''.join(page.heading).strip() or ''.join(page.title).split('|')[0].strip() or title
            match = re.search(r'\b(?:window\.)?albumFiles\s*=\s*', text)
            if not match:
                raise ValueError('无法解析 Bunkr 合集，可能存在访问验证')
            files = _Literal(text[match.end():]).value()
            if not isinstance(files, list):
                raise ValueError('Bunkr 文件列表无效')
            for file in files:
                if not isinstance(file, dict) or not all(k in file for k in ('id', 'original', 'size', 'slug')):
                    raise ValueError('Bunkr 文件元数据不完整')
                mime = file.get('type') or mimetypes.guess_type(file['original'])[0] or 'application/octet-stream'
                items.append({'key': 'bunkr:' + str(file['id']), 'name': safe_name(file['original']),
                              'size': int(file['size']), 'kind': _kind(mime), 'mime': mime, 'md5': file.get('md5'),
                              'source': {'provider': 'bunkr', 'id': file['id'], 'slug': file['slug']}})
            for link in page.links:
                target = urljoin(page_url, link)
                if urlsplit(target).netloc == urlsplit(url).netloc and urlsplit(target).path == urlsplit(url).path and 'page' in parse_qs(urlsplit(target).query):
                    target = _query(target, advanced=1)
                    if target not in scheduled:
                        scheduled.add(target)
                        pending.append(target)
        return _group('bunkr:' + album_id, title, items)

    @staticmethod
    def _index_valid(text):
        lowered = text.lower()
        if any(marker in lowered for marker in ('just a moment', 'verify you are human', 'cf-chl-', 'access denied')):
            return False
        return bool(re.search(r'PAGE\s+\d+\s*/\s*\d+', text, re.I) or re.search(r'https?://bunkr\.[^\s/]+/a/', text))

    def _index(self, url):
        try:
            text = self.network.text(url)
        except (OSError, ValueError):
            text = ''
        if not self._index_valid(text):
            text = self.network.text('https://r.jina.ai/' + url)
        if not self._index_valid(text):
            raise ValueError('无法解析搜索页，可能存在访问验证或来源错误')
        return text

    def _search(self, url):
        parts = urlsplit(url)
        title = parse_qs(parts.query, keep_blank_values=True).get('search', [''])[0]
        if not title.strip():
            raise ValueError('Balbums 链接必须包含非空的搜索条件')
        pending = deque([url])
        first_page = parse_qs(parts.query).get('page', ['1'])[0]
        scheduled = {str(int(first_page)) if first_page.isdigit() else first_page}
        known_pages = 0
        albums = []
        seen_albums = set()
        def enqueue(number):
            number = str(int(number))
            if number not in scheduled:
                scheduled.add(number)
                pending.append(_query(url, page=number))
        while pending:
            page_url = pending.popleft()
            text = self._index(page_url)
            page = _Page(text)
            links = [urljoin(page_url, x) for x in page.links] + extract_links(text)
            for link in links:
                if self._bunkr(link):
                    canonical = urlunsplit((*urlsplit(link)[:3], '', ''))
                    if canonical not in seen_albums:
                        seen_albums.add(canonical)
                        albums.append(canonical)
                elif urlsplit(link).netloc == urlsplit(url).netloc:
                    page_number = parse_qs(urlsplit(link).query).get('page', [])
                    if page_number and page_number[0].isdigit():
                        enqueue(page_number[0])
            count = re.search(r'PAGE\s+\d+\s*/\s*(\d+)', html.unescape(text), re.I)
            if count:
                total_pages = int(count[1])
                if total_pages > 10000:
                    raise ValueError('搜索页数量过大，请缩小搜索范围')
                for n in range(known_pages + 1, total_pages + 1):
                    enqueue(n)
                known_pages = max(known_pages, total_pages)
        items = []
        for album in albums:
            items.extend(self._album(album)['items'])
        identity_query = sorted((key, value) for key, value in
                                parse_qsl(parts.query, keep_blank_values=True)
                                if key not in ('page', 'per', 'sort'))
        identity = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(identity_query), ''))
        return _group('balbums:' + identity, title, items)

    def _yandex(self, public_key):
        endpoint = 'https://cloud-api.yandex.net/v1/disk/public/resources'
        items = []
        seen_dirs = set()

        def directory(path=None):
            if path in seen_dirs:
                return
            seen_dirs.add(path)
            offset = 0
            title = None
            while True:
                query = {'public_key': public_key, 'limit': 100, 'offset': offset}
                if path is not None:
                    query['path'] = path
                resource = self.network.json(endpoint + '?' + urlencode(query))
                title = resource.get('name', title)
                if resource.get('type') == 'file':
                    add_file(resource)
                    return title
                if resource.get('type') != 'dir' or '_embedded' not in resource:
                    raise ValueError('Yandex 来源元数据无效')
                embedded = resource['_embedded']
                children = embedded.get('items', [])
                for child in children:
                    if child.get('type') == 'dir':
                        directory(child['path'])
                    elif child.get('type') == 'file':
                        add_file(child)
                    else:
                        raise ValueError('Yandex 项目类型无效')
                offset += len(children)
                if offset >= int(embedded['total']):
                    return title
                if not children:
                    raise ValueError('Yandex 分页不完整')

        def add_file(file):
            mime = file.get('mime_type') or mimetypes.guess_type(file['name'])[0] or 'application/octet-stream'
            path = file.get('path', '/')
            items.append({'key': 'yandex:' + public_key + ':' + path, 'name': safe_name(file['name']),
                          'size': int(file['size']), 'mime': mime, 'kind': _kind(mime), 'md5': file.get('md5'),
                          'source': {'provider': 'yandex', 'public_key': public_key, 'path': path}})

        title = directory()
        return _group('yandex:' + public_key, title or 'Yandex', items)

    def download_url(self, source):
        if source.get('provider') == 'bunkr':
            metadata = self.network.json('https://dl.bunkr.cr/api/_001_v2', method='POST', json_body={'id': str(source['id'])})
            path = metadata['path']
            signature = self.network.json('https://glb-apisign.cdn.cr/sign?' + urlencode({'path': path}))
            return metadata['mediafiles'].rstrip('/') + '/' + path.lstrip('/') + '?' + urlencode({
                'n': metadata['original'], 'token': signature['token'], 'ex': signature['ex']})
        if source.get('provider') == 'yandex':
            query = urlencode({'public_key': source['public_key'], 'path': source['path']})
            return self.network.json('https://cloud-api.yandex.net/v1/disk/public/resources/download?' + query)['href']
        raise ValueError('不支持的下载来源')
