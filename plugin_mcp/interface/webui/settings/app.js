/* plugin-mcp settings iframe (008.5/phase12) — vanilla port of the old
   SettingsTab.tsx. Lists servers, add/edit/test/enable/disable/remove, tool
   detail. Auth via the ?token= query param the host appends to iframe_src. */

const BASE = '/api/p/plugin-mcp';
const token = new URLSearchParams(location.search).get('token');

function authHeaders(extra) {
  const h = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
  if (token) h['Authorization'] = 'Bearer ' + token;
  return h;
}
async function fetchJSON(url, init) {
  const resp = await fetch(url, Object.assign({}, init, { headers: authHeaders(init && init.headers) }));
  if (!resp.ok) throw new Error((await resp.text()) || resp.statusText);
  const txt = await resp.text();
  return txt ? JSON.parse(txt) : null;
}
const el = (id) => document.getElementById(id);
function showError(m) { const e = el('error'); if (m) { e.textContent = m; e.style.display = 'block'; } else { e.style.display = 'none'; } }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

let servers = [];
let selected = null;

function dotClass(s) {
  if (s.enabled && s.connected) return 'green';
  if (s.last_error) return 'red';
  if (s.enabled) return 'yellow';
  return 'off';
}

async function refresh() {
  try {
    servers = await fetchJSON(BASE + '/servers');
    showError(null);
    renderList();
    renderDetail();
  } catch (e) { showError(e.message); }
}

function renderList() {
  const box = el('list');
  if (!servers.length) {
    box.innerHTML = '<div class="empty">No MCP servers yet. Click <em>Add server</em>.</div>';
    return;
  }
  box.innerHTML = '';
  servers.forEach((s) => {
    const row = document.createElement('div');
    row.className = 'srow' + (selected === s.name ? ' sel' : '');
    row.setAttribute('data-testid', 'mcp-row-' + s.name);
    const cmd = esc((s.config.command || '') + ' ' + ((s.config.args || []).join(' ')));
    row.innerHTML =
      '<span class="dot ' + dotClass(s) + '"></span>' +
      '<div class="sname"><div class="nm">' + esc(s.name) + '</div><code class="cmd">' + cmd + '</code></div>' +
      '<span class="tcount">' + s.tool_count + ' ' + (s.tool_count === 1 ? 'tool' : 'tools') + '</span>' +
      '<button class="toggle ' + (s.enabled ? 'on' : '') + '" data-testid="mcp-toggle-' + s.name + '" title="' + (s.enabled ? 'Enabled' : 'Disabled') + '"><span class="knob"></span></button>' +
      '<button class="iconbtn act-refresh" title="Refresh tools"' + (s.enabled ? '' : ' disabled') + '>&#x21bb;</button>' +
      '<button class="iconbtn danger act-remove" data-testid="mcp-delete-' + s.name + '" title="Remove">&#x1f5d1;</button>';
    row.addEventListener('click', () => { selected = selected === s.name ? null : s.name; renderList(); renderDetail(); });
    row.querySelector('.toggle').addEventListener('click', (e) => { e.stopPropagation(); toggle(s); });
    row.querySelector('.act-refresh').addEventListener('click', (e) => { e.stopPropagation(); if (s.enabled) doRefresh(s.name); });
    row.querySelector('.act-remove').addEventListener('click', (e) => { e.stopPropagation(); remove(s.name); });
    box.appendChild(row);
  });
}

async function toggle(s) {
  try { await fetchJSON(BASE + '/servers/' + encodeURIComponent(s.name), { method: 'PUT', body: JSON.stringify({ enabled: !s.enabled }) }); await refresh(); }
  catch (e) { showError(e.message); }
}
async function doRefresh(name) {
  try { await fetchJSON(BASE + '/servers/' + encodeURIComponent(name) + '/refresh', { method: 'POST' }); await refresh(); }
  catch (e) { showError(e.message); }
}
async function remove(name) {
  if (!confirm('Remove MCP server "' + name + '"?')) return;
  try { await fetchJSON(BASE + '/servers/' + encodeURIComponent(name), { method: 'DELETE' }); if (selected === name) selected = null; await refresh(); }
  catch (e) { showError(e.message); }
}

function parseConfig(command, argsText, envText) {
  const args = argsText.split('\n').map((x) => x.trim()).filter(Boolean);
  const env = {};
  envText.split('\n').map((x) => x.trim()).filter(Boolean).forEach((line) => {
    const i = line.indexOf('='); if (i > 0) env[line.slice(0, i).trim()] = line.slice(i + 1);
  });
  const cfg = { command: command.trim(), args };
  if (Object.keys(env).length) cfg.env = env;
  return cfg;
}

function renderDetail() {
  const box = el('detail');
  if (!selected) { box.innerHTML = ''; return; }
  const s = servers.find((x) => x.name === selected);
  if (!s) { box.innerHTML = ''; return; }

  let html = '';
  if (s.last_error) html += '<div class="panel"><h3>Last error</h3><pre style="white-space:pre-wrap;font-size:.78rem;color:#fca5a5;margin:0">' + esc(s.last_error) + '</pre></div>';

  // edit panel
  const argsText = (s.config.args || []).join('\n');
  const envText = Object.entries(s.config.env || {}).map(([k, v]) => k + '=' + v).join('\n');
  html += '<div class="panel"><h3>Edit <code>' + esc(s.name) + '</code></h3>' +
    '<label>Command</label><input id="e-cmd" value="' + esc(s.config.command || '') + '">' +
    '<label>Arguments (one per line)</label><textarea id="e-args" rows="3">' + esc(argsText) + '</textarea>' +
    '<label>Environment (KEY=VALUE, one per line — use vault:&lt;name&gt; for secrets)</label><textarea id="e-env" rows="3">' + esc(envText) + '</textarea>' +
    '<button id="e-save" class="primary">Save config</button> <span id="e-ok" class="muted"></span></div>';

  // tools
  html += '<div class="panel"><h3>Tools from <code>' + esc(s.name) + '</code></h3><div id="tools"><span class="muted">Loading…</span></div></div>';

  box.innerHTML = html;
  el('e-save').addEventListener('click', async () => {
    try {
      const cfg = parseConfig(el('e-cmd').value, el('e-args').value, el('e-env').value);
      await fetchJSON(BASE + '/servers/' + encodeURIComponent(s.name), { method: 'PUT', body: JSON.stringify({ config: cfg }) });
      el('e-ok').textContent = 'Saved'; setTimeout(() => { el('e-ok').textContent = ''; }, 1500);
      await refresh();
    } catch (e) { showError(e.message); }
  });
  loadTools(s.name);
}

async function loadTools(name) {
  try {
    const tools = await fetchJSON(BASE + '/servers/' + encodeURIComponent(name) + '/tools');
    const box = el('tools'); if (!box) return;
    if (!tools.length) { box.innerHTML = '<span class="muted">No tools cached. Enable the server to discover tools.</span>'; return; }
    const ul = document.createElement('ul'); ul.className = 'tools';
    tools.forEach((t) => {
      const li = document.createElement('li');
      li.setAttribute('data-testid', 'mcp-tool-' + name + '-' + t.name);
      li.innerHTML = (t.destructive ? '<span class="badge">destructive</span>' : '') + '<code>' + esc(t.name) + '</code>' +
        (t.description ? '<div class="tdesc">' + esc(t.description) + '</div>' : '');
      ul.appendChild(li);
    });
    box.innerHTML = ''; box.appendChild(ul);
  } catch (e) { const box = el('tools'); if (box) box.innerHTML = '<span class="muted">' + esc(e.message) + '</span>'; }
}

function openAddModal() {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML =
    '<div class="modal" data-testid="mcp-add-modal"><h3>Add MCP server</h3>' +
    '<label>Name</label><input id="a-name" placeholder="filesystem" data-testid="mcp-add-name">' +
    '<label>Command</label><input id="a-cmd" placeholder="npx" data-testid="mcp-add-command">' +
    '<label>Arguments (one per line)</label><textarea id="a-args" rows="4" placeholder="-y&#10;@modelcontextprotocol/server-filesystem&#10;/tmp" data-testid="mcp-add-args"></textarea>' +
    '<label>Environment (KEY=VALUE, one per line)</label><textarea id="a-env" rows="2"></textarea>' +
    '<label class="chk"><input type="checkbox" id="a-enable" checked> Enable immediately after adding</label>' +
    '<div id="a-msg"></div>' +
    '<div class="modal-actions"><button id="a-test" class="ghost" data-testid="mcp-add-test">Test connection</button>' +
    '<span><button id="a-cancel" class="ghost">Cancel</button> <button id="a-save" class="primary" data-testid="mcp-add-save">Save</button></span></div></div>';
  document.body.appendChild(bg);
  const close = () => document.body.removeChild(bg);
  bg.addEventListener('click', (e) => { if (e.target === bg) close(); });
  el('a-cancel').addEventListener('click', close);
  el('a-test').addEventListener('click', async () => {
    el('a-msg').innerHTML = '';
    try {
      const cfg = parseConfig(el('a-cmd').value, el('a-args').value, el('a-env').value);
      const r = await fetchJSON(BASE + '/test', { method: 'POST', body: JSON.stringify({ transport_type: 'stdio', config: cfg }) });
      el('a-msg').innerHTML = r.ok
        ? '<div class="ok">ok — ' + r.tool_count + ' tools: ' + esc((r.tools || []).join(', ')) + '</div>'
        : '<div class="err">' + esc(r.error || 'connect failed') + '</div>';
    } catch (e) { el('a-msg').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  });
  el('a-save').addEventListener('click', async () => {
    try {
      const cfg = parseConfig(el('a-cmd').value, el('a-args').value, el('a-env').value);
      await fetchJSON(BASE + '/servers', { method: 'POST', body: JSON.stringify({ name: el('a-name').value.trim(), transport_type: 'stdio', config: cfg, enable: el('a-enable').checked }) });
      close(); await refresh();
    } catch (e) { el('a-msg').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  });
}

el('add-btn').addEventListener('click', openAddModal);
refresh();
