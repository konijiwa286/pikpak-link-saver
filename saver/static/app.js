/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (C) 2026 konijiwa286
 */
'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="app-token"]').content;
  const windowId = crypto.randomUUID();
  const labels = {new: '等待提交', creating: '正在提交', submitted: '云端下载中', verified: '已核对完成',
    error: '失败', unknown: '提交结果待核对', relay: '本机中转中', queued: '等待解析',
    discovering: '正在解析', parsing: '正在解析', active: '进行中', completed: '已完成', needs_attention: '需要处理'};
  let state = null;
  let view = 'groups';
  let submitting = false;
  let pasteTimer;
  let pendingPaste = false;
  let inputRevision = 0;
  let renderKey = '';
  let search = '';
  let connectionError = '';
  let renderLimit = 100;
  let polling = null;
  let refreshTimer;
  let refreshing = false;
  let pageActive = true;
  let quitting = false;
  let windowOpened = false;

  const node = (tag, className = '', text = '') => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== '') element.textContent = String(text);
    return element;
  };
  const icon = name => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    element.setAttribute('class', 'icon');
    element.setAttribute('aria-hidden', 'true');
    use.setAttribute('href', '#i-' + name);
    element.append(use);
    return element;
  };
  function focusInput() {
    selectNavigation('overview');
    $('links').scrollIntoView({block: 'center'});
    $('links').focus({preventScroll: true});
  }
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const dark = theme === 'dark';
    $('theme-icon').setAttribute('href', dark ? '#i-sun' : '#i-moon');
    $('theme-toggle').setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
  }
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  let savedTheme;
  try { savedTheme = localStorage.getItem('pikpak-theme'); } catch (_) { /* Storage can be disabled. */ }
  setTheme(['light', 'dark'].includes(savedTheme) ? savedTheme : systemTheme.matches ? 'dark' : 'light');
  $('theme-toggle').addEventListener('click', () => {
    savedTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(savedTheme);
    try { localStorage.setItem('pikpak-theme', savedTheme); } catch (_) { /* Keep the in-page choice. */ }
  });
  systemTheme.addEventListener('change', () => {
    if (!['light', 'dark'].includes(savedTheme)) setTheme(systemTheme.matches ? 'dark' : 'light');
  });
  const bytes = value => {
    let number = Math.max(0, Number(value) || 0);
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let unit = 0;
    while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit++; }
    return (unit ? number.toFixed(1) : Math.round(number)) + ' ' + units[unit];
  };
  const say = (text, error = false) => {
    $('message').textContent = text;
    $('message').className = 'alert ' + (error ? 'alert-danger' : 'alert-success');
    $('message').hidden = false;
  };
  async function api(path, body) {
    const options = {cache: 'no-store', credentials: 'omit',
      headers: {'X-App-Token': token, 'X-Window-Id': windowId}};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const controller = new AbortController();
    options.signal = controller.signal;
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      let response;
      try { response = await fetch(path, options); }
      catch (error) {
        throw new Error(error.name === 'AbortError' ? '本机服务响应超时，请稍后重试。' : '无法连接本机服务，请通过启动入口重新打开。');
      }
      let result;
      try { result = await response.json(); }
      catch (_) { throw new Error('本机服务返回了无效响应，请稍后重试。'); }
      if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('本机服务返回了无效响应，请稍后重试。');
      if (!response.ok) throw new Error(result.error || '操作失败，请稍后重试。');
      return result;
    } finally { clearTimeout(timeout); }
  }
  async function action(button, operation) {
    button.disabled = true;
    try { await operation(); }
    catch (error) { say(error.message, true); }
    finally { button.disabled = quitting; }
  }
  function badge(status) {
    const color = status === 'verified' || status === 'completed' ? 'green' :
      ['error', 'unknown', 'needs_attention'].includes(status) ? 'orange' : 'blue';
    return node('span', 'badge bg-' + color + '-lt', labels[status] || '等待处理');
  }
  function empty(title, text, buttonText, operation) {
    const result = node('div', 'empty-state');
    const graphic = node('div', 'empty-icon');
    graphic.append(icon(view === 'attention' && !search ? 'check' : 'folder'));
    result.append(graphic, node('strong', '', title), node('span', '', text));
    if (buttonText) {
      const button = node('button', 'text-button', buttonText);
      button.type = 'button';
      button.addEventListener('click', operation);
      result.append(button);
    }
    return result;
  }
  function progress(complete, total) {
    const element = node('progress');
    element.max = Math.max(1, total);
    element.value = complete;
    element.setAttribute('aria-label', '已核对 ' + complete + ' / ' + total + ' 个文件');
    return element;
  }
  function errorNote(text) { return node('div', 'task-error', text); }
  function fileRow(item) {
    const row = node('div', 'file-row');
    const name = node('div', 'file-name');
    name.append(node('span', '', item.filename));
    if (item.error) name.append(node('div', 'text-secondary', item.error));
    const info = node('div', 'file-info');
    info.append(badge(item.state), node('span', 'text-secondary', bytes(item.bytes_done) + ' / ' + bytes(item.size)));
    row.append(name, info);
    return row;
  }
  function groupCard(group, openDetails) {
    const row = node('article', 'task-row');
    const top = node('div', 'task-top');
    const title = node('div');
    title.append(node('div', 'task-title', group.title), node('div', 'task-subtitle', '目标目录：' + group.root_name + ' / ' + group.title));
    const status = group.needs_attention ? 'needs_attention' : group.verified === group.total ? 'completed' : 'active';
    top.append(title, badge(status));
    const meta = node('div', 'task-meta');
    meta.append(node('span', '', '图片 ' + group.images_verified + ' / ' + group.images + ' · 视频 ' + group.videos_verified + ' / ' + group.videos),
      node('span', '', '已处理 ' + bytes(group.bytes_done) + ' / ' + bytes(group.bytes_total)));
    row.append(top, progress(group.verified, group.total), meta);
    const detail = node('details');
    detail.dataset.group = group.id;
    detail.open = openDetails.has(group.id);
    detail.append(node('summary', '', '最近文件'));
    let loaded = false;
    const loadFiles = () => {
      if (loaded || !detail.open) return;
      loaded = true;
      const recent = state.items.filter(item => item.group_id === group.id);
      recent.forEach(item => detail.append(fileRow(item)));
      if (!recent.length) detail.append(node('p', 'form-hint', '该合集文件已超出最近 100 项窗口。'));
    };
    detail.addEventListener('toggle', loadFiles);
    loadFiles();
    row.append(detail);
    return row;
  }
  function batchCard(batch) {
    const row = node('article', 'task-row');
    const top = node('div', 'task-top');
    const date = new Date(batch.created_at * 1000).toLocaleString('zh-CN', {hour12: false});
    top.append(node('div', 'task-title', batch.root_name + ' · ' + date), badge(batch.state));
    row.append(top);
    if (batch.total) {
      row.append(progress(batch.verified, batch.total), node('div', 'task-meta', '已核对 ' + batch.verified + ' / ' + batch.total + ' · 图片 ' + batch.images + ' · 视频 ' + batch.videos));
    } else row.append(node('p', 'form-hint', batch.error ? '来源解析未完成。' : '正在等待后台解析；文件总数尚未确定。'));
    if (batch.error) {
      row.append(errorNote(batch.error));
      const retry = node('button', 'btn btn-sm mt-2', '重新解析此批次');
      retry.type = 'button';
      retry.addEventListener('click', () => action(retry, async () => { await api('/api/retry', {batch_id: batch.id}); await poll(true); }));
      row.append(retry);
    }
    return row;
  }
  function renderTasks(force = false) {
    if (!state) return;
    const errors = state.batches.filter(batch => batch.error);
    const records = view === 'batches' ? state.batches : errors.concat(view === 'groups' ? state.groups : state.attention);
    const matches = row => [row.title, row.root_name, row.filename, row.group_title, row.error,
      row.created_at ? new Date(row.created_at * 1000).toLocaleString('zh-CN', {hour12: false}) : '']
      .some(value => String(value || '').toLocaleLowerCase().includes(search));
    const filtered = search ? records.filter(matches) : records;
    const visible = filtered.slice(0, renderLimit);
    const key = JSON.stringify([view, search, filtered.length, visible, view === 'groups' ? state.items : null]);
    if (!force && key === renderKey) return;
    renderKey = key;
    const openDetails = new Set([...$('task-list').querySelectorAll('details[open]')].map(item => item.dataset.group));
    const fragment = document.createDocumentFragment();
    visible.forEach(item => {
      if ('created_at' in item) fragment.append(batchCard(item));
      else if (view === 'groups') fragment.append(groupCard(item, openDetails));
      else {
        const row = node('article', 'task-row');
        row.append(node('div', 'task-subtitle', item.root_name + ' / ' + item.group_title), fileRow(item));
        if (item.state === 'unknown') row.append(errorNote('等待远端核对；“重试失败项”不会重复提交此文件。'));
        fragment.append(row);
      }
    });
    if (!visible.length) {
      if (search) fragment.append(empty('没有找到匹配的任务', '试试其他关键词，或清空搜索查看当前视图的全部任务。', '清空搜索', () => {
        $('task-search').value = '';
        search = '';
        renderLimit = 100;
        renderTasks(true);
        $('task-search').focus();
      }));
      else if (view === 'attention') fragment.append(empty('暂无待处理项目', '失败和待核对的项目会显示在这里。'));
      else fragment.append(empty(state.batches.length ? '正在解析链接' : '暂无任务',
        '粘贴链接开始转存。', '添加链接 →', focusInput));
    }
    $('task-list').replaceChildren(fragment);
    $('task-pagination').hidden = filtered.length <= 100;
    $('task-visible-count').textContent = '显示 ' + visible.length + ' / ' + filtered.length + ' 项';
    $('load-more').hidden = visible.length >= filtered.length;
  }
  function renderAccount(account) {
    const status = account.status || (account.connected ? 'connected' : 'disconnected');
    const checking = status === 'checking';
    const retrying = status === 'error';
    $('account-name').textContent = checking ? '正在恢复连接…' : account.connected ? (account.name || '已连接') : retrying ? '等待连接恢复' : '未连接';
    $('account-error').textContent = account.error || (checking ? '正在检查已保存的授权，无需重复连接。' : account.connected ? '' : '首次授权后，后续启动会自动恢复连接。');
    $('account-error').hidden = !$('account-error').textContent;
    $('connection').textContent = account.error ? '需处理' : '已连接';
    $('connection').hidden = !account.connected;
    $('connection').className = 'badge ' + (account.connected && !account.error ? 'bg-green-lt' : 'bg-orange-lt');
    $('auth-start').textContent = account.device ? '重新获取设备码' : retrying ? '重试连接' : account.connected ? '重新授权' : '连接 PikPak';
    $('auth-start').hidden = checking || (account.connected && !account.error && !account.device);
    const device = account.device;
    $('device').hidden = !device;
    if (device) {
      $('device-code').textContent = device.user_code || '';
      const remaining = Math.max(0, Math.ceil(device.expires_at - Date.now() / 1000));
      $('device-expiry').textContent = remaining ? '设备码约 ' + Math.ceil(remaining / 60) + ' 分钟后过期；完成授权后自动继续。' : '设备码已过期，请重新获取。';
      let url;
      try { url = new URL(device.verification_uri); } catch (_) { url = null; }
      if (url && url.protocol === 'https:' && !url.username && !url.password) {
        $('device-url').href = url.href;
        $('device-url').hidden = false;
      } else { $('device-url').removeAttribute('href'); $('device-url').hidden = true; }
    }
  }
  async function loadState() {
    const next = await api('/api/state');
    if (!pageActive || quitting) return state;
    const firstLoad = !state;
    if (firstLoad) {
      $('root-name').value = next.settings.root_name;
      $('auto-start').checked = next.settings.auto_start;
      $('proxy').value = next.settings.proxy;
      $('local-relay').checked = next.settings.local_relay;
      ['root-name', 'auto-start', 'proxy', 'local-relay', 'save-settings', 'pause'].forEach(id => { $(id).disabled = false; });
    }
    state = next;
    $('service-dot').className = 'status-dot online';
    $('service-label').textContent = '运行中';
    if (connectionError && $('message').textContent === connectionError) $('message').hidden = true;
    connectionError = '';
    const summary = state.summary;
    $('count-total').textContent = summary.total;
    $('count-active').textContent = Math.max(0, summary.active - summary.waiting);
    $('count-waiting').textContent = summary.waiting;
    $('count-verified').textContent = summary.verified;
    $('count-attention').textContent = summary.needs_attention + state.batches.filter(batch => batch.error).length;
    $('nav-total').textContent = summary.total;
    $('nav-attention-count').textContent = $('count-attention').textContent;
    $('nav-attention-count').classList.toggle('has-items', Number($('count-attention').textContent) > 0);
    $('groups-count').textContent = state.groups.length;
    $('batches-count').textContent = state.batches.length;
    $('attention-count').textContent = $('count-attention').textContent;
    $('bytes-progress').textContent = '已处理 ' + bytes(summary.bytes_done) + ' / ' + bytes(summary.bytes_total);
    $('pause').textContent = state.settings.paused ? '继续新提交' : '暂停新提交';
    $('pause-note').hidden = !state.settings.paused;
    $('retry').disabled = !summary.failed && !state.batches.some(batch => batch.error);
    renderAccount(state.account);
    renderTasks();
    if (firstLoad) inputHint();
    if (firstLoad && pendingPaste && !pasteTimer) {
      if ($('auto-start').checked) submit();
      else pendingPaste = false;
    }
    return state;
  }
  async function poll(fresh = false) {
    if (fresh && polling) await polling.catch(() => {});
    if (!polling) polling = loadState().finally(() => { polling = null; });
    return polling;
  }
  function inputHint() {
    const text = $('links').value;
    const matches = text.replace(/&amp;/gi, '&').replace(/\\&/g, '&').match(/https?:\/\/[^\s<>"）)]+/g) || [];
    const count = new Set(matches.map(value => value.split('#')[0])).size;
    $('input-state').textContent = text.trim() ? '检测到 ' + count + ' 个候选链接' : '等待粘贴链接';
    $('start').disabled = !state || submitting || !text.trim() || quitting;
    $('clear-links').hidden = !text;
  }
  async function submit() {
    clearTimeout(pasteTimer);
    pasteTimer = null;
    if (submitting || !$('links').value.trim()) return;
    submitting = true;
    pendingPaste = false;
    const revision = inputRevision;
    const text = $('links').value;
    await action($('start'), async () => {
      if (!state) await poll();
      await api('/api/jobs', {text, root_name: $('root-name').value});
      if (inputRevision === revision) $('links').value = '';
      inputHint();
      say('已加入本机队列，后台将解析并转存。');
      await poll(true);
    });
    submitting = false;
    inputHint();
    if (pendingPaste && !pasteTimer && $('auto-start').checked) submit();
  }
  $('start').addEventListener('click', submit);
  $('links').addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      submit();
    }
  });
  $('clear-links').addEventListener('click', () => {
    clearTimeout(pasteTimer);
    pasteTimer = null;
    pendingPaste = false;
    inputRevision++;
    $('links').value = '';
    inputHint();
    $('links').focus();
  });
  $('links').addEventListener('input', () => { inputRevision++; inputHint(); });
  $('links').addEventListener('paste', () => {
    pendingPaste = false;
    clearTimeout(pasteTimer);
    pasteTimer = setTimeout(() => {
      pasteTimer = null;
      if (!state) {
        pendingPaste = true;
        return;
      }
      if ($('auto-start').checked) {
        pendingPaste = true;
        submit();
      }
    }, 500);
  });
  $('auto-start').addEventListener('change', () => {
    const previous = !$('auto-start').checked;
    if (!$('auto-start').checked) {
      clearTimeout(pasteTimer);
      pasteTimer = null;
      pendingPaste = false;
    }
    action($('auto-start'), async () => {
      try { await saveSettings({auto_start: $('auto-start').checked}); }
      catch (error) { $('auto-start').checked = previous; throw error; }
    });
  });
  async function saveSettings(patch) {
    const result = await api('/api/settings', patch);
    Object.keys(patch).forEach(key => { state.settings[key] = result.settings[key]; });
    if ('root_name' in patch) $('root-name').value = result.settings.root_name;
  }
  $('root-name').addEventListener('change', () => action($('root-name'), async () => {
    await saveSettings({root_name: $('root-name').value});
  }));
  $('pause').addEventListener('click', () => action($('pause'), async () => {
    if (!state) return;
    await api('/api/settings', {paused: !state.settings.paused}); await poll(true);
  }));
  $('retry').addEventListener('click', () => action($('retry'), async () => {
    await api('/api/retry', {}); say('已重试明确失败的项目；提交结果不明的项目继续核对。'); await poll(true);
  }));
  $('auth-start').addEventListener('click', () => action($('auth-start'), async () => {
    renderAccount(await api('/api/auth/start', {})); await poll(true);
  }));
  $('copy-code').addEventListener('click', () => action($('copy-code'), async () => {
    try { await navigator.clipboard.writeText($('device-code').textContent); say('设备码已复制。'); }
    catch (_) { say('无法访问剪贴板，请选中设备码后手动复制。', true); }
  }));
  $('settings-form').addEventListener('submit', event => {
    event.preventDefault();
    action($('save-settings'), async () => {
      const proxy = $('proxy').value.trim();
      await saveSettings({proxy, local_relay: $('local-relay').checked});
      say('设置已保存。');
    });
  });
  function selectView(name) {
    view = name;
    renderLimit = 100;
    if ($('nav-tasks').classList.contains('current') || $('nav-attention').classList.contains('current')) {
      selectNavigation(name === 'attention' ? 'attention' : 'tasks');
    }
    ['groups', 'batches', 'attention'].forEach(other => {
      $('view-' + other).classList.toggle('active', other === name);
      $('view-' + other).setAttribute('aria-pressed', String(other === name));
    });
    $('task-search').placeholder = name === 'attention' ? '搜索文件或目录…' : name === 'batches' ? '搜索批次或目录…' : '搜索合集或目录…';
    renderTasks(true);
  }
  ['groups', 'batches', 'attention'].forEach(name => $('view-' + name).addEventListener('click', () => selectView(name)));
  $('load-more').addEventListener('click', () => {
    renderLimit += 100;
    renderTasks(true);
  });
  $('task-search').addEventListener('input', () => {
    search = $('task-search').value.trim().toLocaleLowerCase();
    renderLimit = 100;
    renderTasks(true);
  });
  function selectNavigation(name) {
    ['overview', 'tasks', 'attention', 'settings'].forEach(other => {
      $('nav-' + other).classList.toggle('current', other === name);
      if (other === name) $('nav-' + other).setAttribute('aria-current', 'page');
      else $('nav-' + other).removeAttribute('aria-current');
    });
  }
  $('nav-overview').addEventListener('click', () => selectNavigation('overview'));
  ['tasks', 'attention'].forEach(name => $('nav-' + name).addEventListener('click', () => {
    selectNavigation(name);
    search = '';
    $('task-search').value = '';
    selectView(name === 'attention' ? 'attention' : 'groups');
    $('tasks').scrollIntoView({block: 'start'});
  }));
  function openSettings() {
    $('settings').open = true;
    selectNavigation('settings');
    $('settings').scrollIntoView({block: 'start'});
  }
  $('nav-settings').addEventListener('click', openSettings);
  $('mobile-settings').addEventListener('click', openSettings);
  const guide = $('guide-dialog');
  $('nav-guide').addEventListener('click', () => guide.showModal());
  $('mobile-guide').addEventListener('click', () => guide.showModal());
  guide.addEventListener('click', event => { if (event.target === guide) {
    const box = guide.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) guide.close();
  } });
  function offline(error) {
    if (!pageActive || quitting) return;
    connectionError = error.message;
    $('service-dot').className = 'status-dot offline';
    $('service-label').textContent = '已断开';
    say(error.message, true);
  }
  selectView('groups');
  inputHint();
  async function refresh() {
    clearTimeout(refreshTimer);
    if (!pageActive || quitting || refreshing) return;
    refreshing = true;
    try {
      if (!windowOpened) {
        await api('/api/window', {action: 'open'});
        windowOpened = true;
      }
      if (!pageActive || quitting) return;
      if (document.hidden) await api('/api/window', {action: 'ping'});
      else await poll();
    } catch (error) { offline(error); }
    finally {
      refreshing = false;
      if (pageActive && !quitting) refreshTimer = setTimeout(refresh, document.hidden ? 20000 : 2000);
    }
  }
  function openWindow() {
    if (quitting) return;
    pageActive = true;
    windowOpened = false;
    refresh();
  }
  document.addEventListener('visibilitychange', () => {
    if (pageActive) refresh();
  });
  window.addEventListener('pagehide', () => {
    if (!pageActive || quitting) return;
    pageActive = false;
    windowOpened = false;
    clearTimeout(refreshTimer);
    fetch('/api/window', {method: 'POST', credentials: 'omit', keepalive: true,
      headers: {'Content-Type': 'application/json', 'X-App-Token': token, 'X-Window-Id': windowId},
      body: JSON.stringify({action: 'close'})}).catch(() => {});
  });
  window.addEventListener('pageshow', event => { if (event.persisted) openWindow(); });
  $('exit-app').addEventListener('click', () => action($('exit-app'), async () => {
    await api('/api/stop', {});
    quitting = true;
    pageActive = false;
    clearTimeout(refreshTimer);
    $('service-label').textContent = '退出中';
    $('service-dot').className = 'status-dot';
    document.querySelectorAll('button, input, textarea').forEach(element => { element.disabled = true; });
    say('程序正在退出，可以关闭页面。');
  }));
  openWindow();
})();
