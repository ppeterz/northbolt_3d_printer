'use strict';
/* NORTHBOLT touch UI. Plain JS, no build step, no network dependencies. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

let apiKey = null;
try { apiKey = localStorage.getItem('northbolt-apikey'); } catch (e) { /* storage blocked */ }

// The Pi's own screen loads the UI from localhost and has no system keyboard. Add ?osk=1 to force it elsewhere.
const KIOSK = ['localhost', '127.0.0.1', '::1', '[::1]'].includes(location.hostname) || /[?&]osk=1/.test(location.search);

let S = null;            // latest state from the server
let files = [];
let settings = null;     // full settings (Settings page)
let system = null;
let page = 'home';
const ui = {
  jogStep: 10, extrudeAmt: 10, zTool: 0, material: 0, tab: 'general', cal: 'level', lvStep: 5, source: 'printer', usbPath: '', knownDrives: null,
  shownRecovery: null, powerModal: null, zErrorSeen: null, presetsSig: '', materialSig: '',
};

// ------------------------------------------------------------------ helpers
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtT = v => (v === null || v === undefined) ? '–' : Math.round(v);
function fmtDur(s) {
  if (s === null || s === undefined) return '–';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s % 60).padStart(2, '0')}s`;
}
function fmtSize(b) {
  if (!b && b !== 0) return '';
  const u = ['B', 'KB', 'MB', 'GB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i ? 1 : 0)} ${u[i]}`;
}
const fmtDate = t => new Date(t * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ' ' +
  new Date(t * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
const fmtZ = z => (z === null || z === undefined) ? '–' : `${Number(z).toFixed(2)} mm`;
const toolName = t => (t === 1 ? 'Right' : 'Left');
const keyQuery = () => (apiKey ? `?apikey=${encodeURIComponent(apiKey)}` : '');
function el(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }
function getPath(obj, path) { return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj); }
function nest(path, value) { return path.split('.').reverse().reduce((acc, k) => ({ [k]: acc }), value); }

async function api(method, path, body) {
  const headers = {};
  if (apiKey) headers['X-Api-Key'] = apiKey;
  const opts = { method, headers };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (r.status === 403) { askKey(); throw new Error('This device needs the API key'); }
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}
const post = (p, b = {}) => api('POST', p, b);

async function act(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, 'error'); }
  finally { if (btn) btn.disabled = false; }
}

function toast(text, level = '') {
  const t = el(`<div class="toast ${level}">${esc(text)}</div>`);
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), level === 'error' ? 6000 : 3000);
}

function modal({ title, body, actions = [], onClose, sticky = false, wide = false }) {
  const back = el('<div class="backdrop"><div class="modal"><h2></h2><div class="body"></div><div class="actions"></div></div></div>');
  const box = $('.modal', back);
  if (wide) box.classList.add('wide');
  if (!actions.length) $('.actions', box).remove();
  $('h2', box).textContent = title;
  const b = $('.body', box);
  if (typeof body === 'string') b.innerHTML = body; else if (body) b.appendChild(body);
  let closed = false;
  const close = () => { if (closed) return; closed = true; back.remove(); if (onClose) onClose(); };
  for (const a of actions) {
    const btn = el(`<button class="btn ${a.cls || ''}"></button>`);
    btn.textContent = a.label;
    btn.onclick = async () => {
      if (a.onClick) {
        btn.disabled = true;
        let r;
        try { r = await a.onClick(btn, close); } catch (e) { toast(e.message, 'error'); r = false; }
        btn.disabled = false;
        if (r === false) return;
      }
      close();
    };
    $('.actions', box).appendChild(btn);
  }
  if (!sticky) back.addEventListener('click', e => { if (e.target === back) close(); });
  $('#modal-root').appendChild(back);
  return { close, box };
}

function confirmBox(title, html, okLabel = 'OK', cls = 'primary') {
  return new Promise(resolve => {
    modal({
      title, body: `<p>${html}</p>`,
      actions: [{ label: 'Back', onClick: () => resolve(false) }, { label: okLabel, cls, onClick: () => resolve(true) }],
      onClose: () => resolve(false),
    });
  });
}

function askKey() {
  if ($('#key-modal')) return;
  const body = el(`<div id="key-modal"><p class="muted">Open <b>Settings → System</b> on the printer's own screen to see the key.</p>
    <input type="password" id="key-input" placeholder="API key" style="width:100%"></div>`);
  modal({
    title: 'API key needed', body, sticky: true,
    actions: [{
      label: 'Save', cls: 'primary', onClick: () => {
        apiKey = $('#key-input').value.trim();
        try { localStorage.setItem('northbolt-apikey', apiKey); } catch (e) { /* ignore */ }
        if (ws) ws.close();
        refreshAll();
      },
    }],
  });
}

// ------------------------------------------------------------------ on-screen keyboard
const OSK_LAYOUTS = {
  abc: [
    ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
    ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
    ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
    ['{shift}', 'z', 'x', 'c', 'v', 'b', 'n', 'm', '{bksp}'],
    ['{sym}', '-', '{space}', '.', '{done}'],
  ],
  sym: [
    ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
    ['!', '@', '#', '$', '%', '^', '&', '*', '(', ')'],
    ['-', '_', '=', '+', '[', ']', '{', '}', ';', ':'],
    ["'", '"', ',', '/', '?', '\\', '|', '~', '{bksp}'],
    ['{abc}', '`', '<', '>', '{space}', '{done}'],
  ],
};
const OSK_LABELS = { '{shift}': '⇧', '{bksp}': '⌫', '{sym}': '?123', '{abc}': 'ABC', '{space}': 'space' };

/** Keyboard for the Pi's touch screen. Resolves to the typed text, or null if cancelled. */
function osk({ title = 'Type', value = '', password = false, placeholder = '', minLength = 0, maxLength = 200,
               okLabel = 'Done', hint = '', check = null }) {
  return new Promise(resolve => {
    let text = String(value || '');
    let layout = 'abc';
    let shift = false;
    let reveal = !password;
    let finished = false;
    const body = el(`<div class="osk">
      <div class="osk-field">
        <div class="osk-value"><span class="txt"></span><span class="caret"></span></div>
        ${password ? '<button class="btn small" type="button" data-osk-eye>Show</button>' : ''}
        <button class="btn small" type="button" data-osk-cancel>Cancel</button>
      </div>
      <div class="osk-rows"></div>
      <div class="osk-hint"></div>
    </div>`);
    const txt = $('.txt', body);
    const hintEl = $('.osk-hint', body);
    const setHint = (msg, bad = false) => { hintEl.textContent = msg; hintEl.classList.toggle('bad', bad); };

    const paint = () => {
      if (!text) txt.innerHTML = `<span class="ph">${esc(placeholder)}</span>`;
      else txt.textContent = reveal ? text : '•'.repeat(text.length);
      txt.scrollLeft = txt.scrollWidth;   // keep the end of long text in view
      const eye = $('[data-osk-eye]', body);
      if (eye) eye.textContent = reveal ? 'Hide' : 'Show';
    };
    const drawKeys = () => {
      const rows = $('.osk-rows', body);
      rows.innerHTML = '';
      for (const row of OSK_LAYOUTS[layout]) {
        const r = el('<div class="osk-row"></div>');
        for (const k of row) {
          let cls = '';
          if (k === '{space}') cls = 'space';
          else if (k === '{done}') cls = 'done';
          else if (k.startsWith('{') && k.length > 1) cls = 'fn' + (k === '{shift}' && shift ? ' on' : '');
          const b = el(`<button class="${cls}" type="button"></button>`);
          b.textContent = k === '{done}' ? okLabel : (OSK_LABELS[k] || (shift && layout === 'abc' ? k.toUpperCase() : k));
          b.dataset.k = k;
          r.appendChild(b);
        }
        rows.appendChild(r);
      }
    };
    const type = ch => {
      if (text.length >= maxLength) { setHint(`At most ${maxLength} characters`, true); return; }
      text += ch;
      setHint(hint);
      paint();
    };
    const backspace = () => { text = text.slice(0, -1); paint(); };
    const finish = result => {
      if (finished) return;
      finished = true;
      document.removeEventListener('keydown', onKey, true);
      m.close();
      resolve(result);
    };
    const submit = () => {
      if (text.length < minLength) { setHint(`At least ${minLength} characters`, true); return; }
      const problem = check ? check(text) : null;
      if (problem) { setHint(problem, true); return; }
      finish(text);
    };
    const press = k => {
      if (k === '{shift}') { shift = !shift; drawKeys(); }
      else if (k === '{bksp}') backspace();
      else if (k === '{sym}') { layout = 'sym'; drawKeys(); }
      else if (k === '{abc}') { layout = 'abc'; drawKeys(); }
      else if (k === '{space}') type(' ');
      else if (k === '{done}') submit();
      else type(shift && layout === 'abc' ? k.toUpperCase() : k);
    };

    // Hold backspace to keep deleting.
    let repeat = null;
    const stopRepeat = () => clearTimeout(repeat);
    body.addEventListener('pointerdown', e => {
      if (!e.target.closest('[data-k="{bksp}"]')) return;
      stopRepeat();
      repeat = setTimeout(function again() { backspace(); repeat = setTimeout(again, 70); }, 450);
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev => body.addEventListener(ev, stopRepeat));
    body.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.k) press(b.dataset.k);
      else if (b.hasAttribute('data-osk-eye')) { reveal = !reveal; paint(); }
      else if (b.hasAttribute('data-osk-cancel')) finish(null);
    });
    // A physical keyboard works as well.
    const onKey = e => {
      if (e.key === 'Enter') submit();
      else if (e.key === 'Escape') finish(null);
      else if (e.key === 'Backspace') backspace();
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) type(e.key);
      else return;
      e.preventDefault();
      e.stopPropagation();
    };
    document.addEventListener('keydown', onKey, true);

    const m = modal({
      title, body, sticky: true, wide: true,
      onClose: () => { if (!finished) { finished = true; document.removeEventListener('keydown', onKey, true); resolve(null); } },
    });
    setHint(hint);
    drawKeys();
    paint();
  });
}

/** Ask for text: the on-screen keyboard on the Pi's screen, a normal input on phones and computers. */
function askText(opts) {
  if (KIOSK) return osk(opts);
  return new Promise(resolve => {
    const body = el(`<div class="stack">
      ${opts.hint ? `<div class="muted">${esc(opts.hint)}</div>` : ''}
      <div class="row"><input id="ask-input" style="flex:1" autocomplete="off" autocapitalize="off" spellcheck="false"
        type="${opts.password ? 'password' : 'text'}" maxlength="${opts.maxLength || 200}" placeholder="${esc(opts.placeholder || '')}">
      ${opts.password ? '<button class="btn small" type="button" id="ask-eye">Show</button>' : ''}</div>
      <div class="osk-hint bad" id="ask-err"></div></div>`);
    const input = $('#ask-input', body);
    input.value = opts.value || '';
    const eye = $('#ask-eye', body);
    if (eye) {
      eye.onclick = () => {
        input.type = input.type === 'password' ? 'text' : 'password';
        eye.textContent = input.type === 'password' ? 'Show' : 'Hide';
      };
    }
    let settled = false;
    const done = v => { if (!settled) { settled = true; resolve(v); } };
    const validate = () => {
      const v = input.value;
      if (v.length < (opts.minLength || 0)) { $('#ask-err', body).textContent = `At least ${opts.minLength} characters`; return null; }
      const problem = opts.check ? opts.check(v) : null;
      if (problem) { $('#ask-err', body).textContent = problem; return null; }
      return v;
    };
    const m = modal({
      title: opts.title || 'Type', body,
      actions: [
        { label: 'Cancel', onClick: () => done(null) },
        { label: opts.okLabel || 'OK', cls: 'primary', onClick: () => { const v = validate(); if (v === null) return false; done(v); return undefined; } },
      ],
      onClose: () => done(null),
    });
    input.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      const v = validate();
      if (v !== null) { done(v); m.close(); }
    });
    setTimeout(() => input.focus(), 50);
  });
}

// On the Pi's screen, tapping any text box opens the on-screen keyboard.
document.addEventListener('focusin', e => {
  if (!KIOSK) return;
  const t = e.target;
  if (!t.matches || !t.matches('input[type=text], input[type=password]') || t.closest('.osk')) return;
  t.blur();
  const isConsole = t.id === 'console-input';
  osk({ title: t.dataset.label || t.placeholder || 'Type', value: t.value, okLabel: isConsole ? 'Send' : 'Done',
        password: t.type === 'password' }).then(v => {
    if (v === null) return;
    t.value = v;
    t.dispatchEvent(new Event('input', { bubbles: true }));
    if (isConsole) ACTIONS['console-send']();
  });
});

// ------------------------------------------------------------------ Wi-Fi
let wifiTimer = null;
let wifiNets = [];
let wifiStatus = null;

function signalBars(sig) {
  const n = sig > 75 ? 4 : sig > 50 ? 3 : sig > 25 ? 2 : 1;
  return `<svg class="bars" viewBox="0 0 20 16" width="24" height="20" aria-label="Signal ${sig}%">${[0, 1, 2, 3].map(i =>
    `<rect x="${i * 5}" y="${12 - i * 4}" width="3.6" height="${4 + i * 4}" rx="1" fill="${i < n ? 'currentColor' : 'var(--panel-3)'}"/>`).join('')}</svg>`;
}

function buildWifiTab(box) {
  wifiNets = [];
  box.innerHTML = `<div class="card stack" id="wifi-status"><div class="row"><div class="spinner"></div>Checking Wi-Fi…</div></div>
    <div class="row wrap" style="margin:10px 0" id="wifi-actions">
      <button class="btn small" data-wifi="scan">Scan again</button>
      <button class="btn small" data-wifi="other">Hidden network…</button>
      <button class="btn small" data-wifi="country">Wi-Fi country</button>
    </div>
    <div class="stack" id="wifi-list" style="gap:6px"></div>`;
  refreshWifi(true);
}

async function refreshWifi(scan) {
  clearTimeout(wifiTimer);
  if (page !== 'settings' || ui.tab !== 'wifi') return;
  try {
    wifiStatus = await api('GET', '/api/wifi');
    renderWifiStatus();
    if (wifiStatus.available && wifiStatus.radio && scan) {
      const list = $('#wifi-list');
      if (list && !wifiNets.length) list.innerHTML = '<div class="card row"><div class="spinner"></div>Looking for networks…</div>';
      wifiNets = (await api('GET', '/api/wifi/networks?rescan=1')).networks;
    }
    renderWifiList();
  } catch (e) { toast(e.message, 'error'); }
  wifiTimer = setTimeout(() => refreshWifi(false), 8000);
}

function renderWifiStatus() {
  const box = $('#wifi-status');
  if (!box) return;
  const st = wifiStatus;
  $('#wifi-actions').classList.toggle('hidden', !st.available || !st.radio);
  if (!st.available) {
    box.innerHTML = `<div><b>Wi-Fi setup isn't available here.</b></div>
      <div class="muted">It needs NetworkManager, which current Raspberry Pi OS uses.</div>`;
    return;
  }
  let main;
  if (!st.radio) main = '<b>Wi-Fi is off</b>';
  else if (st.ssid) main = `Connected to <b>${esc(st.ssid)}</b>`;
  else main = '<b>Not connected</b>';
  const port = location.port || '80';
  const ipLine = st.ip
    ? `<div class="muted num">Address ${esc(st.ip)} · on a phone open <b>http://${esc(st.ip)}:${esc(port)}</b></div>` : '';
  const countryWarn = st.radio && !st.country
    ? `<div class="banner warning" style="margin:4px 0 0"><div class="text">The Wi-Fi country isn't set, so the Pi may find no networks. Press “Wi-Fi country”.</div></div>` : '';
  box.innerHTML = `<div class="row between"><div class="row" style="gap:10px">
      <svg style="width:26px;height:26px;color:var(--accent)"><use href="#i-wifi"/></svg><div>${main}</div></div>
      <label class="switch" title="Wi-Fi on or off"><input type="checkbox" id="wifi-radio"${st.radio ? ' checked' : ''}><span></span></label></div>
    ${ipLine}${countryWarn}`;
  $('#wifi-radio', box).addEventListener('change', e => act(null, async () => {
    const on = e.target.checked;
    if (!on && !(await confirmBox('Turn Wi-Fi off?',
      KIOSK ? 'Phones and slicers on Wi-Fi will lose the printer until you turn it back on here.'
        : 'This device will lose its connection to the printer.', 'Turn off', 'danger'))) {
      e.target.checked = true;
      return;
    }
    wifiStatus = await post('/api/wifi/radio', { on });
    wifiNets = [];
    renderWifiStatus();
    refreshWifi(true);
  }));
}

function renderWifiList() {
  const list = $('#wifi-list');
  if (!list) return;
  if (!wifiStatus || !wifiStatus.available || !wifiStatus.radio) { list.innerHTML = ''; return; }
  if (!wifiNets.length) { list.innerHTML = '<div class="card muted">No networks found. Press “Scan again”.</div>'; return; }
  list.innerHTML = '';
  wifiNets.forEach((n, i) => {
    const current = wifiStatus.ssid === n.ssid;
    const state = current ? '<span class="tag ok">Connected</span>' : (n.saved ? '<span class="tag">Saved</span>' : '');
    const lock = n.secure ? '<svg><use href="#i-lock"/></svg>' : '<span class="tag">Open</span>';
    list.appendChild(el(`<div class="net${current ? ' current' : ''}" data-net="${i}">${signalBars(n.signal)}
      <div style="min-width:0"><div class="ssid">${esc(n.ssid)}</div>
      <div class="sub">${n.secure ? esc(n.security) : 'No password'} · ${n.signal}%</div></div>
      <div class="tags">${state}${lock}</div></div>`));
  });
}

async function wifiPick(n) {
  if (!n) return;
  const current = wifiStatus && wifiStatus.ssid === n.ssid;
  if (current) {
    modal({
      title: n.ssid,
      body: `<p>The printer is connected to this network${wifiStatus.ip ? ` at <b class="num">${esc(wifiStatus.ip)}</b>` : ''}.</p>`,
      actions: [{ label: 'Forget', cls: 'ghost', onClick: () => wifiForget(n) }, { label: 'Close', cls: 'primary' }],
    });
    return;
  }
  if (n.saved) {
    modal({
      title: n.ssid, body: '<p>This network is saved on the printer.</p>',
      actions: [
        { label: 'Forget', cls: 'ghost', onClick: () => wifiForget(n) },
        { label: 'New password', onClick: () => { setTimeout(() => wifiAskPassword(n), 0); } },
        { label: 'Connect', cls: 'primary', onClick: () => { setTimeout(() => wifiConnect(n.ssid, null), 0); } },
      ],
    });
    return;
  }
  if (!n.secure) {
    if (await confirmBox(`Join ${esc(n.ssid)}?`, 'This network has no password, so anyone nearby can see its traffic.', 'Join', 'primary')) {
      wifiConnect(n.ssid, null);
    }
    return;
  }
  wifiAskPassword(n);
}

async function wifiAskPassword(n, hidden = false) {
  const pw = await askText({
    title: `Password for ${n.ssid}`, password: true, minLength: 8, maxLength: 63, okLabel: 'Connect',
    placeholder: 'Wi-Fi password', hint: 'Capital letters matter. Tap Show to check what you typed.',
  });
  if (pw === null) return;
  wifiConnect(n.ssid, pw, hidden);
}

async function wifiConnect(ssid, password, hidden = false) {
  if (!KIOSK && !(await confirmBox(`Switch the printer to ${esc(ssid)}?`,
    'If this phone or computer is on a different network, it will lose its connection to the printer.', 'Switch', 'primary'))) return;
  const m = modal({
    title: `Connecting to ${ssid}`, sticky: true,
    body: '<div class="row" style="gap:12px"><div class="spinner"></div><div>This can take up to 40 seconds.</div></div>',
  });
  try {
    wifiStatus = await post('/api/wifi/connect', { ssid, password, hidden });
    m.close();
    toast(`Connected to ${wifiStatus.ssid || ssid}`);
  } catch (e) {
    m.close();
    const n = wifiNets.find(x => x.ssid === ssid) || { ssid, secure: true };
    const retry = n.secure
      ? [{ label: 'Try again', cls: 'primary', onClick: () => { setTimeout(() => wifiAskPassword(n, hidden), 0); } }] : [];
    modal({ title: "Couldn't connect", body: `<p>${esc(e.message)}</p>`, actions: [{ label: 'Close' }, ...retry] });
  }
  wifiNets = [];
  refreshWifi(true);
}

async function wifiForget(n) {
  if (!(await confirmBox(`Forget ${esc(n.ssid)}?`, "The printer won't join it again until you type the password.", 'Forget', 'danger'))) return;
  await act(null, async () => {
    await post('/api/wifi/forget', { ssid: n.ssid });
    toast(`Forgot ${n.ssid}`);
  });
  wifiNets = [];
  refreshWifi(true);
}

async function wifiOther() {
  const ssid = await askText({
    title: 'Hidden network name', maxLength: 32, minLength: 1, placeholder: 'Network name (SSID)', okLabel: 'Next',
    hint: 'Type it exactly. Capital letters matter.',
  });
  if (ssid === null) return;
  modal({
    title: ssid, body: '<p>Does this network have a password?</p>',
    actions: [
      { label: 'Cancel' },
      { label: 'No password', onClick: () => { setTimeout(() => wifiConnect(ssid, null, true), 0); } },
      { label: 'Has a password', cls: 'primary', onClick: () => { setTimeout(() => wifiAskPassword({ ssid, secure: true }, true), 0); } },
    ],
  });
}

async function wifiCountry() {
  const code = await askText({
    title: 'Wi-Fi country', value: (wifiStatus && wifiStatus.country) || '', minLength: 2, maxLength: 2,
    placeholder: 'GB', okLabel: 'Set', hint: 'Two letters, for example GB, US, DE, FR or NG. It decides which Wi-Fi channels are allowed.',
    check: v => (/^[a-z]{2}$/i.test(v) ? null : 'Two letters only'),
  });
  if (code === null) return;
  await act(null, async () => {
    wifiStatus = await post('/api/wifi/country', { code: code.toUpperCase() });
    toast(`Wi-Fi country set to ${code.toUpperCase()}`);
  });
  wifiNets = [];
  refreshWifi(true);
}

// ------------------------------------------------------------------ USB drives (Files page)
let usbListing = null;
let usbCopyModal = null;

function usbDrives() { return (S && S.usb && S.usb.drives) || []; }

function renderUsbSources() {
  const drives = usbDrives();
  // Tell the user when a drive appears, and jump to it if they're looking at Files.
  const known = ui.knownDrives || new Set(drives.map(d => d.id));
  for (const d of drives) {
    if (!known.has(d.id)) {
      toast(`USB drive “${d.label}” connected`);
      if (page === 'files') { ui.source = d.id; ui.usbPath = ''; loadFiles(); }
    }
  }
  ui.knownDrives = new Set(drives.map(d => d.id));
  if (ui.source !== 'printer' && !drives.some(d => d.id === ui.source)) {
    toast('The USB drive was removed');
    ui.source = 'printer';
    if (page === 'files') loadFiles();
  }
  $('#rail [data-page="files"]').classList.toggle('usb', drives.some(d => d.ready));
  const sig = JSON.stringify(drives.map(d => [d.id, d.label])) + ui.source;
  const seg = $('#file-source');
  if (seg.dataset.sig === sig) return;
  seg.dataset.sig = sig;
  seg.innerHTML = '';
  seg.appendChild(el(`<button data-source="printer" class="${ui.source === 'printer' ? 'on' : ''}">Printer</button>`));
  for (const d of drives) {
    const b = el(`<button data-source="${esc(d.id)}" class="${ui.source === d.id ? 'on' : ''}"></button>`);
    b.textContent = `USB · ${d.label.length > 14 ? d.label.slice(0, 13) + '…' : d.label}`;
    seg.appendChild(b);
  }
  seg.classList.toggle('hidden', !drives.length);
  const onUsb = ui.source !== 'printer';
  $('#upload-btn').classList.toggle('hidden', onUsb);
  $('#usb-eject').classList.toggle('hidden', !onUsb);
  $('#files-free').classList.toggle('hidden', onUsb);
}

function renderUsbCopy() {
  const c = S && S.usb && S.usb.copy;
  if (!usbCopyModal) return;
  const txt = $('.copy-text', usbCopyModal.box);
  if (!c || !txt) return;
  const pct = c.total ? Math.floor(c.done / c.total * 100) : 0;
  txt.textContent = `${c.name}: ${pct}% (${fmtSize(c.done)} of ${fmtSize(c.total)})`;
  $('.bar > div', usbCopyModal.box).style.width = `${pct}%`;
}

async function loadUsb() {
  const list = $('#file-list');
  const drive = usbDrives().find(d => d.id === ui.source);
  $('#usb-bar').classList.remove('hidden');
  if (!drive) return;
  if (!drive.ready) {
    list.innerHTML = drive.error
      ? `<div class="banner error"><div class="text">${esc(drive.error)}</div></div>`
      : '<div class="card row"><div class="spinner"></div>Opening the drive…</div>';
    if (!drive.error) setTimeout(() => { if (page === 'files' && ui.source === drive.id) loadUsb(); }, 1500);
    return;
  }
  try {
    usbListing = await api('GET', `/api/usb/${encodeURIComponent(drive.id)}/list?path=${encodeURIComponent(ui.usbPath || '')}`);
  } catch (e) {
    list.innerHTML = `<div class="banner error"><div class="text">${esc(e.message)}</div></div>`;
    return;
  }
  ui.usbPath = usbListing.path;
  $('#usb-path').textContent = [usbListing.label, ...(usbListing.path ? usbListing.path.split('/') : [])].join(' › ');
  $('[data-act="usb-up"]').disabled = !usbListing.path;
  list.innerHTML = '';
  if (!usbListing.entries.length) {
    list.innerHTML = '<div class="card muted">No G-code files or folders here.</div>';
    return;
  }
  usbListing.entries.forEach((e, i) => {
    if (e.type === 'dir') {
      list.appendChild(el(`<div class="file" data-usb="${i}"><div class="ph folder"><svg><use href="#i-files"/></svg></div>
        <div style="min-width:0"><div class="name">${esc(e.name)}</div><div class="meta">Folder</div></div><span class="muted">›</span></div>`));
    } else {
      const rel = usbListing.path ? `${usbListing.path}/${e.name}` : e.name;
      const src = `/api/usb/${encodeURIComponent(drive.id)}/thumb?path=${encodeURIComponent(rel)}${apiKey ? '&apikey=' + encodeURIComponent(apiKey) : ''}`;
      list.appendChild(el(`<div class="file" data-usb="${i}"><img loading="lazy" src="${src}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph'}))">
        <div style="min-width:0"><div class="name">${esc(e.name)}</div><div class="meta">${fmtSize(e.size)} · ${fmtDate(e.date)}</div></div><span class="muted">›</span></div>`));
    }
  });
}

function usbOpen(i) {
  const e = usbListing && usbListing.entries[i];
  if (!e) return;
  const rel = usbListing.path ? `${usbListing.path}/${e.name}` : e.name;
  if (e.type === 'dir') { ui.usbPath = rel; $('#content').scrollTop = 0; loadUsb(); return; }
  usbFileDialog(usbListing.drive, rel, e);
}

async function usbFileDialog(drive, rel, entry) {
  let meta = {};
  try { meta = await api('GET', `/api/usb/${encodeURIComponent(drive)}/meta?path=${encodeURIComponent(rel)}`); }
  catch (err) { toast(err.message, 'error'); return; }
  const rows = [
    ['Print time', meta.est_time_s ? fmtDur(meta.est_time_s) : '–'],
    ['Filament', meta.filament_g ? `${Math.round(meta.filament_g)} g` : (meta.filament_mm ? `${(meta.filament_mm / 1000).toFixed(1)} m` : '–')],
    ['Size', fmtSize(entry.size)], ['Saved', fmtDate(entry.date)],
    ['Folder', rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : 'Top of the drive'],
  ];
  const src = `/api/usb/${encodeURIComponent(drive)}/thumb?path=${encodeURIComponent(rel)}${apiKey ? '&apikey=' + encodeURIComponent(apiKey) : ''}`;
  const img = meta.has_thumbnail ? `<img class="thumb" style="width:150px;height:150px" src="${src}">` : '';
  const busy = S && S.job && !['idle', 'finished', 'cancelled', 'error'].includes(S.job.state);
  const body = `<div class="row" style="align-items:flex-start;gap:14px">${img}<div style="flex:1">${rows.map(r =>
    `<div class="row between" style="padding:3px 0"><span class="muted">${r[0]}</span><span class="num">${esc(r[1])}</span></div>`).join('')}
    <p class="muted" style="font-size:13px;margin:8px 0 0">The file is copied to the printer first, so you can take the drive out once printing starts.</p></div></div>`;
  modal({
    title: entry.name, body,
    actions: [
      { label: 'Close' },
      { label: 'Copy to printer', onClick: () => { setTimeout(() => usbTransfer(drive, rel, false), 0); } },
      { label: busy ? 'Printer busy' : 'Print', cls: 'primary', onClick: () => { if (busy) return false; setTimeout(() => usbTransfer(drive, rel, true), 0); return undefined; } },
    ],
  });
}

async function usbTransfer(drive, rel, andPrint) {
  usbCopyModal = modal({
    title: andPrint ? 'Copying, then printing' : 'Copying to the printer', sticky: true,
    body: '<div class="row" style="gap:12px"><div class="spinner"></div><div class="copy-text" style="flex:1">Starting…</div></div><div class="bar"><div></div></div>',
  });
  try {
    const r = await post(`/api/usb/${encodeURIComponent(drive)}/${andPrint ? 'print' : 'copy'}`, { path: rel });
    usbCopyModal.close();
    usbCopyModal = null;
    if (andPrint) { toast(`Printing ${r.name}. You can remove the drive.`); showPage('home'); }
    else toast(`Copied to the printer as ${r.name}`);
  } catch (e) {
    if (usbCopyModal) usbCopyModal.close();
    usbCopyModal = null;
    modal({ title: andPrint ? "Couldn't start the print" : "Couldn't copy the file", body: `<p>${esc(e.message)}</p>`, actions: [{ label: 'Close' }] });
  }
}

async function usbEject() {
  const drive = usbDrives().find(d => d.id === ui.source);
  if (!drive) return;
  ui.source = 'printer';   // switch first so the drive vanishing isn't reported as "removed"
  try {
    await post(`/api/usb/${encodeURIComponent(drive.id)}/eject`);
    toast(`You can remove “${drive.label}” now`);
  } catch (e) {
    ui.source = drive.id;
    toast(e.message, 'error');
  }
  loadFiles();
}

// ------------------------------------------------------------------ bed leveling (Calibrate page)
const SCREW_SHORT = n => n.split(/\s+/).map(w => w[0].toUpperCase()).join('');

function turnsText(t) {
  if (t < 0.06) return 'a tiny touch';
  const eighths = Math.max(1, Math.round(t * 8));
  const whole = Math.floor(eighths / 8);
  const rest = eighths % 8;
  const frac = { 1: '1/8', 2: '1/4', 3: '3/8', 4: '1/2', 5: '5/8', 6: '3/4', 7: '7/8' }[rest] || '';
  const about = whole ? `${whole}${frac ? ' ' + frac : ''}` : frac;
  return `about ${about} turn${t > 1.06 ? 's' : ''}`;
}

/** A clock-face dial showing how far to turn, with an arrow in the turning direction. */
function dialSvg(s, size = 54) {
  const r = 20;
  if (s.action === 'ok' || s.action === 'reference') {
    const mark = s.action === 'ok'
      ? '<path d="M15 27l8 8 14-16" fill="none" stroke="var(--ok)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
      : '<circle cx="26" cy="26" r="5" fill="var(--muted)"/>';
    return `<svg width="${size}" height="${size}" viewBox="0 0 52 52"><circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--border)" stroke-width="4"/>${mark}</svg>`;
  }
  const deg = Math.min(s.degrees, 345);
  const cw = s.direction === 'cw';
  const a = (deg * Math.PI) / 180;
  const ex = 26 + r * Math.sin(cw ? a : -a);
  const ey = 26 - r * Math.cos(a);
  const large = deg > 180 ? 1 : 0;
  const sweep = cw ? 1 : 0;
  // arrowhead tangent to the circle at the end of the arc
  const tx = cw ? Math.cos(a) : -Math.cos(a);
  const ty = Math.sin(a);
  const head = `M${ex + 7 * tx} ${ey + 7 * ty} L${ex - 5 * ty} ${ey + 5 * tx} L${ex + 5 * ty} ${ey - 5 * tx} Z`;
  return `<svg width="${size}" height="${size}" viewBox="0 0 52 52">
    <circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--border)" stroke-width="4"/>
    <path d="M26 ${26 - r} A${r} ${r} 0 ${large} ${sweep} ${ex} ${ey}" fill="none" stroke="var(--warn)" stroke-width="5" stroke-linecap="round"/>
    <path d="${head}" fill="var(--warn)"/>
    <line x1="26" y1="2" x2="26" y2="10" stroke="var(--text)" stroke-width="2"/>
    ${s.turns > 1 ? `<text x="26" y="31" text-anchor="middle" font-size="13" font-weight="700" fill="var(--text)">${Math.floor(s.turns)}+</text>` : ''}
  </svg>`;
}

/** Top view of the bed (front at the bottom) with the mesh and the three screws. */
function bedSvg(res, bedX, bedY) {
  const flip = y => bedY - y;
  const all = res.mesh.flat().filter(v => v !== null);
  const mean = all.reduce((a, b) => a + b, 0) / all.length;
  const span = Math.max(0.05, ...all.map(v => Math.abs(v - mean)));
  let dots = '';
  res.mesh.forEach((row, j) => row.forEach((v, i) => {
    if (v === null) return;
    const t = (v - mean) / span;
    const col = t > 0 ? `rgba(255,85,82,${(0.25 + 0.75 * t).toFixed(2)})` : `rgba(59,158,255,${(0.25 - 0.75 * t).toFixed(2)})`;
    dots += `<circle cx="${res.xs[i]}" cy="${flip(res.ys[j])}" r="9" fill="${col}"/>`;
  }));
  let screws = '';
  for (const s of res.screws) {
    const fix = s.action === 'raise' || s.action === 'lower';
    const fill = fix ? 'var(--warn)' : (s.action === 'ok' ? 'var(--ok)' : 'var(--panel-3)');
    const ink = fix || s.action === 'ok' ? '#111' : 'var(--text)';
    const cx = Math.min(bedX - 4, Math.max(4, s.x));
    const cy = flip(Math.min(bedY - 4, Math.max(4, s.y)));
    screws += `<circle cx="${cx}" cy="${cy}" r="17" fill="${fill}" stroke="var(--bg)" stroke-width="4"/>
      <text x="${cx}" y="${cy + 6}" text-anchor="middle" font-size="16" font-weight="800" fill="${ink}">${esc(SCREW_SHORT(s.name))}</text>`;
  }
  return `<svg viewBox="-24 -14 ${bedX + 48} ${bedY + 40}" class="bed-svg" aria-label="Bed seen from above, front at the bottom">
    <rect x="0" y="0" width="${bedX}" height="${bedY}" rx="8" fill="var(--panel-2)" stroke="var(--border)" stroke-width="2"/>
    ${dots}${screws}
    <text x="${bedX / 2}" y="${bedY + 22}" text-anchor="middle" font-size="14" fill="var(--muted)" letter-spacing="3">FRONT</text>
  </svg>`;
}

function renderLevel() {
  const lv = S.leveling;
  if (!lv) return;
  $$('#cal-tabs button').forEach(b => b.classList.toggle('sel', b.dataset.cal === ui.cal));
  $('#cal-level').classList.toggle('hidden', ui.cal !== 'level');
  $('#cal-z').classList.toggle('hidden', ui.cal !== 'z');
  const show = id => ['lv-idle', 'lv-busy', 'lv-results', 'lv-teach'].forEach(x => $(`#${x}`).classList.toggle('hidden', x !== id));
  $('#lv-error').classList.toggle('hidden', lv.state !== 'error');
  if (lv.state === 'error') $('#lv-error-text').textContent = lv.error || 'Bed leveling stopped';
  const busyPrinter = !S.connection.ready || !['idle', 'finished', 'cancelled', 'error'].includes(S.job.state);
  if (lv.state === 'idle' || lv.state === 'error') {
    show(lv.state === 'idle' ? 'lv-idle' : null);
    $$('#lv-idle [data-lv="start"], #lv-idle [data-lv="saved"], #lv-idle [data-lv="teach"]').forEach(b => { b.disabled = busyPrinter; });
  } else if (lv.state === 'running') {
    show('lv-busy');
    $('#lv-busy-text').textContent = lv.message;
    $('#lv-busy-time').textContent = lv.elapsed ? `${fmtDur(lv.elapsed)} so far` : '';
  } else if (lv.state === 'teach') {
    show('lv-teach');
    const pick = $('#lv-teach-pick');
    const sig = JSON.stringify(lv.screws.map(s => s.name)) + lv.teach_index;
    if (pick.dataset.sig !== sig) {
      pick.dataset.sig = sig;
      pick.innerHTML = lv.screws.map((s, i) => `<button data-lvpick="${i}" class="${i === lv.teach_index ? 'on' : ''}">${esc(s.name)}</button>`).join('');
    }
    $('#lv-teach-msg').textContent = lv.message;
    $('#lv-teach-pos').textContent = lv.teach_pos ? `X ${lv.teach_pos.x}  Y ${lv.teach_pos.y}` : '–';
  } else if (lv.state === 'results' && lv.result) {
    show('lv-results');
    const res = lv.result;
    const key = JSON.stringify([res.time, res.screws, res.reference]);
    if ($('#lv-results').dataset.key !== key) {
      $('#lv-results').dataset.key = key;
      $('#lv-map').innerHTML = bedSvg(res, S.config.bed_x, S.config.bed_y);
      const list = $('#lv-list');
      list.innerHTML = '';
      for (const s of res.screws) {
        let title;
        let sub;
        if (s.action === 'reference') { title = 'Leave this one'; sub = 'Reference screw. The others are matched to it.'; }
        else if (s.action === 'ok') { title = 'Level'; sub = `Within ${res.tolerance} mm of the reference`; }
        else {
          title = `Turn ${s.direction === 'cw' ? 'clockwise ↻' : 'anticlockwise ↺'} ${s.turns.toFixed(2)} turn`;
          sub = `${turnsText(s.turns)} (${s.degrees}°). This corner is ${Math.abs(s.delta).toFixed(2)} mm too ${s.action === 'lower' ? 'high' : 'low'}.`;
        }
        list.appendChild(el(`<div class="card lv-screw ${s.action}">${dialSvg(s)}
          <div style="min-width:0"><div class="muted" style="font-size:13px">${esc(s.name)}</div>
          <div class="lv-title">${esc(title)}</div><div class="muted" style="font-size:13px">${esc(sub)}</div></div></div>`));
      }
      const warpNote = res.warp > 0.1
        ? `The bed itself is bent by ${res.warp.toFixed(2)} mm. Screws can't fix that; the mesh makes up for it while printing.`
        : `Bed shape: within ${Math.max(res.warp, 0.01).toFixed(2)} mm once tilt is removed.`;
      list.appendChild(el(`<div class="muted" style="font-size:13px;padding:0 4px">${res.level ? '<b style="color:var(--ok)">The bed is level.</b> Save the mesh so prints use it. ' : 'Seen from above. '}${esc(warpNote)}</div>`));
    }
    $('#lv-again').classList.toggle('hidden', res.level);
    $('#lv-save').classList.toggle('hidden', !res.level);
    $('#lv-again-left').classList.toggle('hidden', !res.level);
  }
}

function levelOptions() {
  const lv = S.leveling;
  const pitches = [[0.5, 'M3'], [0.7, 'M4'], [0.8, 'M5']];
  let pitch = lv.pitch;
  let cwLowers = lv.clockwise_lowers;
  let reference = lv.reference;
  const body = el(`<div class="stack">
    <div class="field"><div class="lab">Screw thread<small>Bigger threads move further per turn</small></div>
      <div class="seg" id="lo-pitch">${pitches.map(([v, n]) => `<button data-v="${v}">${n}</button>`).join('')}</div></div>
    <div class="field"><div class="lab">Turning clockwise (seen from above)<small>Check once: turn a screw a little and watch that corner</small></div>
      <div class="seg" id="lo-dir"><button data-v="1">lowers the bed</button><button data-v="0">raises the bed</button></div></div>
    <div class="field"><div class="lab">Reference screw<small>Left alone; the others are matched to it</small></div>
      <div class="seg" id="lo-ref">${lv.screws.map((s, i) => `<button data-v="${i}">${esc(s.name)}</button>`).join('')}</div></div>
  </div>`);
  const paint = () => {
    $$('#lo-pitch button', body).forEach(b => b.classList.toggle('on', Number(b.dataset.v) === pitch));
    $$('#lo-dir button', body).forEach(b => b.classList.toggle('on', (b.dataset.v === '1') === cwLowers));
    $$('#lo-ref button', body).forEach(b => b.classList.toggle('on', Number(b.dataset.v) === reference));
  };
  body.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    const grp = b.parentElement.id;
    if (grp === 'lo-pitch') pitch = Number(b.dataset.v);
    if (grp === 'lo-dir') cwLowers = b.dataset.v === '1';
    if (grp === 'lo-ref') reference = Number(b.dataset.v);
    paint();
  });
  paint();
  modal({
    title: 'Screw options', body,
    actions: [{ label: 'Cancel' }, {
      label: 'Save', cls: 'primary',
      onClick: () => post('/api/settings', { leveling: { pitch, clockwise_lowers: cwLowers, reference } }),
    }],
  });
}

const LEVEL_ACTIONS = {
  start: b => act(b, () => post('/api/leveling/start', { probe: true, heat: $('#lv-heat').checked })),
  saved: b => act(b, () => post('/api/leveling/start', { probe: false, heat: false })),
  cancel: b => act(b, () => post('/api/leveling/cancel')),
  finish: b => act(b, () => post('/api/leveling/finish')),
  save: b => act(b, async () => { await post('/api/leveling/save'); toast('Bed mesh saved to the printer'); }),
  options: () => levelOptions(),
  teach: b => act(b, () => post('/api/leveling/teach_start', { index: 0 })),
  teach_save: b => act(b, async () => { await post('/api/leveling/teach_save'); toast('Screw position saved'); }),
  teach_done: b => act(b, () => post('/api/leveling/teach_done')),
};

// ------------------------------------------------------------------ firmware updates (Settings -> Firmware)
let fwModal = null;
const FW_PHASES = {
  download: 'Downloading', prepare: 'Preparing', backup: 'Saving a backup of the current firmware',
  write: 'Writing the new firmware', verify: 'Checking what was written', reconnect: 'Reconnecting',
};

function firmwareCard() {
  const fw = settings.firmware || {};
  const card = el(`<div class="stack">
    <div class="card stack">
      <h3>Printer board firmware</h3>
      <div class="row between"><span class="muted">Running now</span><b class="num" id="fw-now" style="text-align:right">–</b></div>
      <div class="field"><div class="lab">Your GitHub repository<small>Releases there with a printer .hex file can be installed from here</small></div></div>
      <div class="row wrap">
        <input type="text" id="fw-repo" data-label="GitHub repository link" placeholder="https://github.com/you/SkriwareFirmware" style="flex:1" value="${esc(fw.github_repo || '')}">
        <button class="btn small" id="fw-save">Save</button>
      </div>
      <div class="row wrap">
        <input type="password" id="fw-token" data-label="GitHub token (private repositories only)" placeholder="Token, only for a private repository" style="flex:1" value="${esc(fw.token || '')}">
      </div>
    </div>
    <div class="split-edges">
      <div class="row wrap"><button class="btn hidden" id="fw-restore">Restore backup…</button></div>
      <div class="row wrap"><button class="btn primary" id="fw-check">Check for update</button></div>
    </div>
    <div id="fw-release"></div>
    <div id="fw-status"></div>
  </div>`);
  $('#fw-save', card).onclick = e => act(e.currentTarget, async () => {
    const changes = { github_repo: $('#fw-repo', card).value.trim() };
    const token = $('#fw-token', card).value;
    if (token !== (settings.firmware.token || '')) changes.token = token;
    settings = await post('/api/settings', { firmware: changes });
    toast('Saved');
  });
  $('#fw-check', card).onclick = e => act(e.currentTarget, async () => {
    if ($('#fw-repo', card).value.trim() !== (settings.firmware.github_repo || '')) $('#fw-save', card).click();
    await new Promise(r => setTimeout(r, 300));
    await post('/api/firmware/check');
  });
  $('#fw-restore', card).onclick = async () => {
    const b = S.firmware_update && S.firmware_update.backup;
    if (!b) return;
    if (await confirmBox('Restore the previous firmware?',
      `This puts back the firmware saved on ${esc(fmtDate(b.time))}, before the last update. It takes about a minute. Don't turn the printer off.`,
      'Restore', 'warn')) {
      act(null, () => post('/api/firmware/restore'));
    }
  };
  return card;
}

function renderFirmware() {
  const fu = S.firmware_update;
  if (!fu) return;
  // progress dialog while installing
  if (fu.state === 'working') {
    if (!fwModal) {
      fwModal = modal({ title: 'Updating the printer firmware', sticky: true,
        body: '<div class="row" style="gap:12px"><div class="spinner"></div><div class="fw-text" style="flex:1"></div></div><div class="bar"><div></div></div><p class="muted" style="font-size:13px">Don\'t turn the printer off. This takes about 2 minutes.</p>' });
    }
    $('.fw-text', fwModal.box).textContent = `${FW_PHASES[fu.phase] || fu.message}${fu.progress !== null && fu.progress !== undefined ? ` ${fu.progress}%` : ''}`;
    $('.bar > div', fwModal.box).style.width = `${fu.progress || 0}%`;
  } else if (fwModal) {
    fwModal.close();
    fwModal = null;
    if (fu.state === 'done') {
      modal({ title: 'Firmware updated', body: `<p>${esc(fu.message)}</p><p class="muted num">${esc((fu.result && fu.result.reported) || '')}</p>`, actions: [{ label: 'OK', cls: 'primary' }] });
    } else if (fu.state === 'error') {
      modal({ title: "The update didn't finish", body: `<p>${esc(fu.error)}</p>`, actions: [{ label: 'Close' }] });
    }
  }
  if (ui.tab !== 'firmware' || !$('#fw-release')) return;
  const fwNow = (S.connection.firmware || '').replace('FIRMWARE_NAME:', '').split(' SOURCE_CODE_URL')[0];
  $('#fw-now').textContent = fwNow || 'not connected';
  $('#fw-restore').classList.toggle('hidden', !fu.backup);
  $('#fw-check').disabled = fu.state === 'checking' || fu.state === 'working';
  const rel = fu.release;
  const relBox = $('#fw-release');
  const sig = JSON.stringify([rel, fu.state]);
  if (relBox.dataset.sig !== sig) {
    relBox.dataset.sig = sig;
    if (rel && (fu.state === 'ready' || fu.state === 'done' || fu.state === 'error')) {
      const when = rel.published ? new Date(rel.published).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
      relBox.innerHTML = `<div class="card stack">
        <div class="row between"><h3 style="margin:0">Latest release</h3><span class="muted">${esc(rel.repo)}</span></div>
        <div class="row between"><b>${esc(rel.name)}</b><span class="muted num">${esc(when)}</span></div>
        <div class="muted num" style="font-size:13px">${esc(rel.asset)}${rel.size ? ` · ${fmtSize(rel.size)}` : ''}${rel.digest ? ' · checksum from GitHub' : ''}</div>
        ${rel.notes ? `<div class="muted" style="font-size:13px;white-space:pre-wrap;max-height:110px;overflow:auto">${esc(rel.notes)}</div>` : ''}
        <div class="split-edges"><span class="muted" style="font-size:13px">A backup of the current firmware is saved first.</span>
          <div class="row"><button class="btn warn" id="fw-install">Install on the printer</button></div></div>
      </div>`;
      $('#fw-install', relBox).onclick = async e => {
        const printing = S.job && !['idle', 'finished', 'cancelled', 'error'].includes(S.job.state);
        if (printing) { toast('Not while printing', 'error'); return; }
        if (await confirmBox(`Install ${esc(rel.name)}?`,
          "The printer board is reprogrammed over its USB cable. The heaters turn off, the current firmware is saved as a backup first, and the whole thing takes about 2 minutes. <b>Don't turn the printer off while it runs.</b> Saved settings (Z offsets, bed mesh) are kept unless the release says otherwise.",
          'Install', 'warn')) {
          act(e.currentTarget, () => post('/api/firmware/install'));
        }
      };
    } else relBox.innerHTML = '';
  }
  const st = $('#fw-status');
  st.innerHTML = fu.state === 'checking' ? `<div class="banner info"><div class="spinner"></div><div class="text">${esc(fu.message)}</div></div>`
    : (fu.state === 'error' && fu.error ? `<div class="banner error"><div class="text">${esc(fu.error)}</div></div>` : '');
}

// ------------------------------------------------------------------ websocket
let ws = null;
let wsRetry = null;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws${keyQuery()}`);
  ws.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type === 'state') { S = d; render(); }
    else if (d.type === 'log') appendLog(d.entries);
  };
  ws.onclose = () => {
    if (S) S._stale = true;
    renderTop();
    clearTimeout(wsRetry);
    wsRetry = setTimeout(connectWS, 2000);
  };
}

// ------------------------------------------------------------------ navigation
function updateRail() {
  const key = page === 'settings' && ui.tab === 'wifi' ? 'wifi' : page;
  $$('#rail button').forEach(b => b.classList.toggle('active', b.dataset.page === key));
}

function showPage(name) {
  if (name === 'wifi') { ui.tab = 'wifi'; name = 'settings'; }
  else if (name === 'settings' && ui.tab === 'wifi') ui.tab = 'general';   // the Settings button opens General
  page = name;
  updateRail();
  $$('.page').forEach(p => p.classList.toggle('active', p.id === `page-${name}`));
  $('#content').scrollTop = 0;
  if (name === 'files') loadFiles();
  if (name === 'settings') loadSettings();
  render();
}

// ------------------------------------------------------------------ rendering
function statusOf() {
  if (!S || S._stale) return ['Host offline', 'bad'];
  const c = S.connection;
  if (c.halted) return ['Halted', 'bad'];
  if (!c.ready) return [c.connected ? 'Connecting' : 'Printer offline', 'bad'];
  if (c.stopped) return ['Stopped', 'bad'];
  const j = S.job;
  const map = {
    printing: [`Printing ${j.progress ?? 0}%`, 'busy'], resuming: ['Resuming', 'busy'], starting: ['Starting', 'busy'],
    pausing: ['Pausing', 'warn'], paused: ['Paused', 'warn'], cancelling: ['Cancelling', 'warn'],
  };
  if (map[j.state]) return map[j.state];
  if (S.op) return [S.op.name === 'load' ? 'Loading' : 'Unloading', 'busy'];
  if (!['idle', 'error'].includes(S.zoffset.state)) return ['Z offset', 'busy'];
  if (heating()) return ['Heating', 'warn'];
  return ['Ready', 'ok'];
}

function heating() {
  return ['t0', 't1', 'bed'].some(k => {
    const t = S.temps[k];
    return t.target > 0 && t.actual !== null && t.actual < t.target - 3;
  });
}

function render() {
  renderTop();
  if (!S) return;
  bindTemps();
  renderAlerts();
  renderRecoveryPrompt();
  renderPowerPrompt();
  if (page === 'home') renderHome();
  if (page === 'control') renderControl();
  if (page === 'filament') renderFilament();
  if (page === 'zoffset') {
    // follow whichever calibration is running
    if (!['idle', 'error'].includes(S.zoffset.state)) ui.cal = 'z';
    else if (S.leveling && S.leveling.state !== 'idle') ui.cal = 'level';
    renderLevel();
    renderZ();
  }
  renderUsbSources();
  renderUsbCopy();
  renderFirmware();
  if (page === 'settings') renderSettingsLive();
}

function wifiIcon(level, off) {
  const arcs = ['M8.5 15.5a5 5 0 0 1 7 0', 'M5 12a10 10 0 0 1 14 0', 'M2 8.5a15 15 0 0 1 20 0'];
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">${
    arcs.map((d, i) => `<path d="${d}" opacity="${i < level ? 1 : 0.25}"/>`).join('')}
    <circle cx="12" cy="19" r="1.3" fill="currentColor" stroke="none" opacity="${off ? 0.35 : 1}"/>${
    off ? '<path d="M4 3.5 20 20.5"/>' : ''}</svg>`;
}

function renderRailWifi() {
  const btn = $('#rail-wifi');
  const w = S && S.wifi;
  btn.classList.toggle('hidden', !w || !w.available);
  if (!w || !w.available) return;
  let level = 0;
  let label = 'No Wi-Fi';
  let cls = 'none';
  if (!w.radio) { label = 'Wi-Fi off'; cls = 'off'; }
  else if (w.ssid) {
    level = w.signal > 66 ? 3 : w.signal > 33 ? 2 : 1;
    label = 'Wi-Fi';
    cls = 'connected';
  }
  const key = `${cls}:${level}`;
  if (btn.dataset.key !== key) {
    btn.dataset.key = key;
    $('.wicon', btn).innerHTML = wifiIcon(level, cls === 'off');
    $('.wlabel', btn).textContent = label;
    ['none', 'off', 'connected'].forEach(c => btn.classList.toggle(c, c === cls));
  }
  btn.title = w.ssid ? `${w.ssid} · ${w.signal}%${w.ip ? ' · ' + w.ip : ''}` : label;
}

function renderTop() {
  renderRailWifi();
  const [text, cls] = statusOf();
  const pill = $('#status-pill');
  pill.textContent = text;
  pill.className = `pill ${cls}`;
  if (!S || S._stale) { $('#top-temps').innerHTML = ''; return; }
  const t = S.temps;
  const part = (label, k) => {
    const x = t[k];
    return `<span>${label} <b>${fmtT(x.actual)}°</b>${x.target ? `/${fmtT(x.target)}` : ''}</span>`;
  };
  $('#top-temps').innerHTML = part('L', 't0') + part('R', 't1') + part('Bed', 'bed');
}

function bindTemps() {
  for (const node of $$('[data-b]')) {
    const [k, field] = node.dataset.b.split('.');
    const t = S.temps[k];
    if (!t) continue;
    if (field === 'actual') node.textContent = fmtT(t.actual);
    else node.textContent = t.target ? `→ ${fmtT(t.target)}°C` : 'off';
  }
  for (const k of ['t0', 't1', 'bed']) {
    const tile = $(`#tile-${k}`);
    const t = S.temps[k];
    const bar = $('.heatbar', tile);
    const pct = t.target ? Math.min(100, Math.max(0, (t.actual || 0) / t.target * 100)) : 0;
    bar.style.width = `${pct}%`;
    bar.style.background = pct >= 97 ? 'var(--ok)' : 'var(--warn)';
    tile.classList.toggle('active-tool', k === `t${S.tool}`);
  }
}

function renderAlerts() {
  const box = $('#alerts');
  const sig = JSON.stringify(S.alerts.map(a => [a.id, a.text]));
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = '';
  const conn = S.connection;
  for (const a of S.alerts.slice(-3)) {
    const extra = a.key === 'halted' ? '<button class="btn small" data-act="reconnect">Reconnect</button>'
      : a.key === 'stopped' ? '<button class="btn small" data-act="m999">Restart</button>' : '';
    box.appendChild(el(`<div class="banner ${a.level}"><div class="text">${esc(a.text)}</div>${extra}
      <button class="x" data-dismiss="${a.id}" aria-label="Dismiss">×</button></div>`));
  }
  if (!conn.ready && conn.error && !S.alerts.length) {
    box.appendChild(el(`<div class="banner info"><div class="text">Printer not connected: ${esc(conn.error)}</div>
      <button class="btn small" data-act="connect">Connect</button></div>`));
  }
}

function renderRecoveryPrompt() {
  const r = S.recovery;
  const banner = $('#recovery-banner');
  banner.classList.toggle('hidden', !r);
  if (!r) return;
  $('#recovery-text').textContent =
    `${r.file}, layer ${r.layer ?? '?'} at ${fmtZ(r.z)}, about ${r.progress}% done, ${toolName(r.tool).toLowerCase()} nozzle.`;
  const sig = `${r.file}:${r.saved}`;
  if (ui.shownRecovery !== sig && S.connection.ready) {
    ui.shownRecovery = sig;
    if (page !== 'home') showPage('home');
    recoveryDialog();
  }
}

function recoveryDialog() {
  const r = S.recovery;
  if (!r) return;
  const toolNote = r.tool === 1
    ? '<li>The right nozzle was printing. Check the extruder lift is raised (Control page) before resuming.</li>' : '';
  modal({
    title: 'Resume the interrupted print?',
    body: `<p><b>${esc(r.file)}</b> stopped at layer ${r.layer ?? '?'} (${fmtZ(r.z)}).</p>
      <p class="muted">The printer will heat up, set Z without homing it, home X and Y, then carry on from just before where it stopped.</p>
      <ul class="checklist"><li>Check the part is still stuck firmly to the bed.</li>
      <li>Make sure nothing is under or around the nozzle.</li>${toolNote}
      <li>Expect a small seam where it resumes.</li></ul>`,
    actions: [
      { label: 'Not now' },
      { label: 'Discard', onClick: async () => { await post('/api/recovery/discard'); } },
      { label: 'Resume', cls: 'warn', onClick: async () => { await post('/api/recovery/resume'); } },
    ],
  });
}

function renderPowerPrompt() {
  const pr = S.power_request;
  if (!pr) {
    if (ui.powerModal) { ui.powerModal.close(); ui.powerModal = null; }
    return;
  }
  if (ui.powerModal || pr.shutting_down) return;
  let left = pr.printing ? null : 15;
  const body = el(`<div><p>${pr.printing ? '<b>A print is running.</b> Turning off now ruins it.' : 'The printer turns off in <b id="pw-count">15</b> seconds.'}</p></div>`);
  const m = modal({
    title: 'Power button pressed', body, sticky: true,
    actions: [
      { label: 'Keep on', onClick: async () => { await post('/api/power/cancel'); } },
      { label: pr.printing ? 'Turn off anyway' : 'Turn off now', cls: 'danger', onClick: async () => { await post('/api/power/off', { force: true }); } },
    ],
    onClose: () => { clearInterval(timer); ui.powerModal = null; },
  });
  const timer = left === null ? null : setInterval(async () => {
    left -= 1;
    const c = $('#pw-count', body);
    if (c) c.textContent = left;
    if (left <= 0) { clearInterval(timer); m.close(); post('/api/power/off', {}).catch(e => toast(e.message, 'error')); }
  }, 1000);
  ui.powerModal = m;
}

function renderHome() {
  const j = S.job;
  const active = ['starting', 'printing', 'pausing', 'paused', 'resuming', 'cancelling'].includes(j.state);
  const ended = ['finished', 'cancelled', 'error'].includes(j.state);
  const thumb = $('#job-thumb');
  const thumbKey = j.file && j.has_thumbnail ? j.file : '';
  if (thumb.dataset.file !== thumbKey) {
    thumb.dataset.file = thumbKey;
    if (thumbKey) {
      const img = el('<img class="thumb" alt="">');
      img.src = `/api/thumb/${encodeURIComponent(thumbKey)}${keyQuery()}`;
      img.id = 'job-thumb';
      img.dataset.file = thumbKey;
      thumb.replaceWith(img);
    } else {
      const ph = el(`<div id="job-thumb" class="thumb empty" data-file="">${j.file ? 'No preview' : 'No file'}</div>`);
      thumb.replaceWith(ph);
    }
  }
  const labels = {
    printing: 'Printing', paused: 'Paused', pausing: 'Pausing…', resuming: 'Resuming…', cancelling: 'Cancelling…',
    finished: 'Finished', cancelled: 'Cancelled', error: 'Stopped', starting: 'Starting…',
  };
  $('#job-name').textContent = j.file || 'Ready to print';
  $('#job-state').textContent = labels[j.state] || '';
  const showNums = active || ended;
  $('#job-pct').textContent = showNums && j.progress !== null ? `${Math.floor(j.progress)}%` : '';
  const bar = $('#job-bar');
  bar.firstElementChild.style.width = `${showNums ? (j.progress || 0) : 0}%`;
  bar.classList.toggle('paused', j.state === 'paused');
  $('#job-layer').textContent = showNums && j.layer ? `${j.layer}${j.layers ? ' / ' + j.layers : ''}` : '–';
  $('#job-z').textContent = showNums ? fmtZ(j.z) : '–';
  $('#job-elapsed').textContent = showNums ? fmtDur(j.elapsed) : '–';
  $('#job-remaining').textContent = active ? fmtDur(j.remaining) : (j.state === 'finished' ? 'Done' : '–');
  if (active && j.remaining && j.state !== 'paused') {
    const t = new Date(Date.now() + j.remaining * 1000);
    $('#job-eta').textContent = `Done about ${t.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;
  } else $('#job-eta').textContent = '';

  const reason = $('#job-reason');
  let rtext = '';
  if (j.state === 'paused' && j.pause_reason) {
    rtext = `${j.pause_reason}.`;
    if (j.runout_side) rtext += ' Load new filament on the Filament page, then press Resume.';
    if (j.heaters_off_for_pause) rtext += ' The nozzles were switched off while waiting. Resume heats them again.';
  } else if (j.state === 'error' && j.error) rtext = j.error;
  reason.textContent = rtext;
  reason.className = `banner ${j.state === 'error' ? 'error' : 'warning'}${rtext ? '' : ' hidden'}`;
  reason.style.margin = '12px 0 0';

  const ready = S.connection.ready && !S.connection.halted;
  $('#btn-choose').classList.toggle('hidden', active);
  $('#btn-choose').disabled = !ready;
  $('#btn-pause').classList.toggle('hidden', !['printing', 'resuming'].includes(j.state));
  $('#btn-resume').classList.toggle('hidden', j.state !== 'paused');
  $('#btn-cancel').classList.toggle('hidden', !active || j.state === 'cancelling');
  $('#btn-filament').classList.toggle('hidden', j.state !== 'paused');
  $('#job-tune').classList.toggle('hidden', !active);
  $('#tune-speed').textContent = `${S.speed}%`;
  $('#tune-flow').textContent = `${S.flow}%`;

  for (const side of ['left', 'right']) {
    const s = S.scales[side];
    $(`#spool-${side}`).textContent = s && s.grams !== null && s.grams !== undefined ? `${Math.max(0, s.grams)} g` : (s && s.raw !== null && s.raw !== undefined ? 'not calibrated' : '–');
  }
  $('#spools').classList.toggle('hidden', !S.hardware.i2c);
}

function renderControl() {
  const j = S.job;
  const printing = j.state !== 'idle' && !['finished', 'cancelled', 'error', 'paused'].includes(j.state);
  $('#control-lock').classList.toggle('hidden', !printing);
  $$('#page-control [data-jog], #page-control [data-act^="home"], #page-control [data-act="motors-off"], #page-control [data-act^="lift"], #tool-select button, #page-control [data-act="extrude"], #page-control [data-act="retract"]')
    .forEach(b => { b.disabled = printing || !S.connection.ready; });
  // the tool can't be switched while paused either: the resume expects the same nozzle
  $$('#tool-select button, #page-control [data-act^="lift"], #page-control [data-act^="home"]').forEach(b => { if (j.state === 'paused') b.disabled = true; });
  $$('#tool-select button').forEach(b => b.classList.toggle('on', Number(b.dataset.tool) === S.tool));
  $$('.lift-only').forEach(x => x.classList.toggle('hidden', !S.config.lift_mechanism));
  $('#fan-value').textContent = `${Math.round(S.fan / 2.55)}%`;
  const sig = JSON.stringify(S.config.presets);
  if (ui.presetsSig !== sig) {
    ui.presetsSig = sig;
    const box = $('#preset-buttons');
    box.innerHTML = '';
    S.config.presets.forEach((p, i) => box.appendChild(el(`<button class="btn" data-preset="${i}">${esc(p.name)}<span class="muted num">${p.hotend}/${p.bed}</span></button>`)));
    box.appendChild(el('<button class="btn" data-act="cooldown">Cool down</button>'));
  }
}

function renderFilament() {
  const op = S.op;
  $('#op-banner').classList.toggle('hidden', !op);
  if (op) $('#op-text').textContent = `${toolName(op.tool)} nozzle: ${op.message}`;
  const sig = JSON.stringify(S.config.presets);
  if (ui.materialSig !== sig) {
    ui.materialSig = sig;
    const seg = $('#material-select');
    seg.innerHTML = '';
    S.config.presets.forEach((p, i) => seg.appendChild(el(`<button data-material="${i}">${esc(p.name)} ${p.hotend}°</button>`)));
  }
  $$('#material-select button').forEach(b => b.classList.toggle('on', Number(b.dataset.material) === ui.material));
  const st = S.runout.status;
  for (const side of ['left', 'right']) {
    const cb = $(`[data-runout="${side}"]`);
    if (document.activeElement !== cb) cb.checked = !!S.runout.enabled[side];
    const card = $(`[data-side="${side}"]`);
    let text = 'Press “Check sensors” to read it';
    if (st && st.off) text = 'Both sensors must be on to read them';
    else if (st && side in st) text = st[side] ? 'Filament detected' : 'No filament';
    $('.sensor-state', card).textContent = S.runout.enabled[side] ? text : 'Off: runout is not detected';
    $$('[data-fil]', card).forEach(b => { b.disabled = !!op || !S.connection.ready || (S.job.state !== 'idle' && !['paused', 'finished', 'cancelled', 'error'].includes(S.job.state)); });
  }
}

function renderZ() {
  const z = S.zoffset;
  const show = id => ['z-idle', 'z-busy', 'z-adjust', 'z-done'].forEach(x => $(`#${x}`).classList.toggle('hidden', x !== id));
  const off = S.offsets;
  $('#z-current').textContent = off
    ? `Left ${off.ho.toFixed(2)} mm` + (S.config.lift_mechanism ? ` · Right ${off.e1.toFixed(2)} mm` : '') : '–';
  $('#z-tool').classList.toggle('hidden', !S.config.lift_mechanism);
  $$('#z-tool button').forEach(b => b.classList.toggle('on', Number(b.dataset.ztool) === ui.zTool));
  const errBox = $('#z-error');
  const showErr = z.state === 'error' && z.error && ui.zErrorSeen !== z.error;
  errBox.classList.toggle('hidden', !showErr);
  if (showErr) $('#z-error-text').textContent = `The test stopped: ${z.error}`;

  if (z.state === 'idle' || z.state === 'error') {
    show('z-idle');
    $('[data-act="z-start"]').disabled = !S.connection.ready || S.job.state !== 'idle' && !['finished', 'cancelled', 'error'].includes(S.job.state);
  } else if (z.state === 'preparing' || z.state === 'saving') {
    show('z-busy');
    $('#z-busy-text').textContent = z.message;
  } else if (z.state === 'adjusting') {
    show('z-adjust');
    $('#z-tool-label').textContent = `${toolName(z.tool)} nozzle, current Z`;
    $('#z-read').textContent = z.z === null ? '–' : `${z.z.toFixed(2)} mm`;
    const preview = z.old_offset - z.z + (z.paper || 0);
    $('#z-delta').textContent = `New offset would be ${preview.toFixed(2)} mm (now ${z.old_offset.toFixed(2)})`;
    $('#z-instr').textContent = z.message;
    $$('[data-zstep]').forEach(b => {
      const d = Number(b.dataset.zstep);
      b.disabled = (d < 0 && z.z + d < z.min_z - 1e-6);
    });
  } else {
    show('z-done');
    $('#z-done-title').textContent = z.state === 'checking' ? 'Checking' : 'Saved';
    $('#z-done-text').textContent = z.state === 'checking'
      ? `${z.message} Point: ${(z.check_point || '').replace('_', ' ')}.` : z.message;
    $('#z-check').classList.toggle('hidden', z.tool !== 0);
    $$('[data-zpoint]').forEach(b => b.classList.toggle('sel', b.dataset.zpoint === z.check_point));
  }
}

// ------------------------------------------------------------------ files
let filesTimer = null;
async function loadFiles() {
  clearTimeout(filesTimer);
  if (S) renderUsbSources();
  if (ui.source !== 'printer') return loadUsb();
  $('#usb-bar').classList.add('hidden');
  try {
    const d = await api('GET', '/api/files');
    files = d.files;
    $('#files-free').textContent = d.free ? `· ${fmtSize(d.free)} free` : '';
    renderFiles();
  } catch (e) { toast(e.message, 'error'); }
  if (page === 'files') filesTimer = setTimeout(loadFiles, files.some(f => f.scanning) ? 1500 : 6000);
}

function renderFiles() {
  const list = $('#file-list');
  if (!files.length) {
    list.innerHTML = '<div class="card muted">No files yet. Upload from a phone or computer, or send from your slicer (OctoPrint upload).</div>';
    return;
  }
  list.innerHTML = '';
  for (const f of files) {
    const img = f.has_thumbnail ? `<img loading="lazy" src="/api/thumb/${encodeURIComponent(f.name)}${keyQuery()}" alt="">` : '<div class="ph"></div>';
    const meta = [f.scanning ? 'reading…' : null, f.est_time_s ? fmtDur(f.est_time_s) : null,
      f.filament_g ? `${Math.round(f.filament_g)} g` : null, fmtDate(f.date)].filter(Boolean).join(' · ');
    const row = el(`<div class="file" data-file="${esc(f.name)}">${img}<div style="min-width:0"><div class="name">${esc(f.name)}</div><div class="meta">${esc(meta)}</div></div><span class="muted">›</span></div>`);
    list.appendChild(row);
  }
}

function fileDialog(name) {
  const f = files.find(x => x.name === name);
  if (!f) return;
  const rows = [
    ['Print time', f.est_time_s ? fmtDur(f.est_time_s) : '–'],
    ['Filament', f.filament_g ? `${Math.round(f.filament_g)} g` : (f.filament_mm ? `${(f.filament_mm / 1000).toFixed(1)} m` : '–')],
    ['Layers', f.layers || '–'], ['Height', f.max_z ? `${f.max_z} mm` : '–'],
    ['Nozzles', (f.tools || [0]).map(toolName).join(' + ')], ['Size', fmtSize(f.size)], ['Added', fmtDate(f.date)],
  ];
  const img = f.has_thumbnail ? `<img class="thumb" style="width:160px;height:160px" src="/api/thumb/${encodeURIComponent(f.name)}${keyQuery()}">` : '';
  const body = `<div class="row" style="align-items:flex-start;gap:14px">${img}<div style="flex:1">${rows.map(r =>
    `<div class="row between" style="padding:3px 0"><span class="muted">${r[0]}</span><span class="num">${esc(r[1])}</span></div>`).join('')}</div></div>`;
  const busy = S && S.job && !['idle', 'finished', 'cancelled', 'error'].includes(S.job.state);
  modal({
    title: f.name, body,
    actions: [
      {
        label: 'Delete', cls: 'ghost', onClick: async () => {
          if (!(await confirmBox('Delete file?', esc(f.name), 'Delete', 'danger'))) return false;
          await api('DELETE', `/api/files/${encodeURIComponent(f.name)}`);
          loadFiles();
        },
      },
      { label: 'Close' },
      {
        label: busy ? 'Printer busy' : 'Print', cls: 'primary', onClick: async () => {
          if (busy) return false;
          await post('/api/print', { file: f.name });
          showPage('home');
        },
      },
    ],
  });
}

function upload(file) {
  const fd = new FormData();
  fd.append('file', file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/files');
  if (apiKey) xhr.setRequestHeader('X-Api-Key', apiKey);
  const box = $('#upload-progress');
  box.classList.remove('hidden');
  xhr.upload.onprogress = e => { if (e.lengthComputable) $('#upload-text').textContent = `Uploading ${file.name}: ${Math.round(e.loaded / e.total * 100)}%`; };
  xhr.onload = () => {
    box.classList.add('hidden');
    if (xhr.status >= 200 && xhr.status < 300) { toast(`Uploaded ${file.name}`); loadFiles(); }
    else { let m = 'Upload failed'; try { m = JSON.parse(xhr.responseText).error || m; } catch (e) { /* */ } toast(m, 'error'); }
  };
  xhr.onerror = () => { box.classList.add('hidden'); toast('Upload failed', 'error'); };
  xhr.send(fd);
}

// ------------------------------------------------------------------ temperature dialog
function tempDialog(heater) {
  if (!S) return;
  const isBed = heater === 'bed';
  const names = { t0: 'Left nozzle', t1: 'Right nozzle', bed: 'Bed' };
  let target = S.temps[heater].target || (isBed ? 60 : 200);
  const max = isBed ? 120 : 285;
  const body = el(`<div><div class="big-temp num"><span class="v"></span> °C</div>
    <div class="row wrap" style="justify-content:center">${[-10, -1, 1, 10].map(d => `<button class="btn" data-d="${d}">${d > 0 ? '+' : '−'}${Math.abs(d)}</button>`).join('')}</div>
    <div class="row wrap" style="justify-content:center;margin-top:10px">${S.config.presets.map(p => {
      const v = isBed ? p.bed : p.hotend; return `<button class="btn small" data-p="${v}">${esc(p.name)} ${v}°</button>`;
    }).join('')}</div></div>`);
  const upd = () => { $('.v', body).textContent = target; };
  body.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.d) target = Math.max(0, Math.min(max, target + Number(b.dataset.d)));
    if (b.dataset.p) target = Number(b.dataset.p);
    upd();
  });
  upd();
  modal({
    title: names[heater], body,
    actions: [
      { label: 'Turn off', onClick: () => post('/api/temp', { heater, target: 0 }) },
      { label: 'Cancel' },
      { label: 'Set', cls: 'primary', onClick: () => post('/api/temp', { heater, target }) },
    ],
  });
}

// ------------------------------------------------------------------ settings page
const num = (path, label, unit, min, max, step, hint) => ({ type: 'num', path, label, unit, min, max, step, hint });
const tog = (path, label, hint) => ({ type: 'tog', path, label, hint });
const FORMS = {
  general: [
    { title: 'Pause', fields: [num('pause.lift', 'Lift when paused', 'mm', 0, 50, 1), num('pause.retract', 'Retract when paused', 'mm', 0, 10, 0.5),
      num('pause.heater_timeout_min', 'Turn nozzles off after', 'min', 0, 120, 5, '0 means never')] },
    { title: 'Filament', fields: [num('filament.load_length', 'Load length', 'mm', 0, 900, 10), num('filament.load_speed', 'Load speed', 'mm/s', 1, 60, 1),
      num('filament.purge_length', 'Purge after loading', 'mm', 0, 100, 5), num('filament.unload_length', 'Unload length', 'mm', 0, 900, 10),
      num('filament.unload_speed', 'Unload speed', 'mm/s', 1, 80, 5), num('filament.min_temp', 'Minimum temperature', '°C', 150, 300, 5)] },
    { title: 'Z offset test', fields: [num('zoffset.paper_thickness', 'Paper thickness', 'mm', 0, 0.3, 0.01, '0 makes Z 0 the height where the paper drags'),
      num('zoffset.bed_temp', 'Bed temperature', '°C', 0, 110, 5), num('zoffset.nozzle_temp', 'Nozzle temperature', '°C', 0, 250, 5, 'Warm, but cool enough not to ooze'),
      tog('zoffset.use_mesh', 'Use the bed mesh while measuring')] },
    { title: 'Machine', fields: [tog('lift_mechanism', 'Right nozzle has a lift mechanism', 'Gives the right nozzle its own Z offset (M206 E)')] },
  ],
  recovery: [
    { title: 'Power-loss recovery', fields: [tog('recovery.enabled', 'Save progress for power-loss recovery'),
      tog('recovery.layer_sync', 'Sync at layer changes', 'Waits for each layer to finish so the checkpoint is exact'),
      tog('recovery.sync_z_moves', 'Sync before Z moves', 'Needed to know the nozzle height after a power cut. Adds a short stop at each Z-hop.'),
      num('recovery.progress_interval_s', 'Save progress every', 's', 1, 30, 1),
      num('recovery.z_lift', 'Lift before homing X/Y', 'mm', 1, 20, 1), num('recovery.prime_length', 'Prime before resuming', 'mm', 0, 30, 1),
      tog('recovery.use_mesh', 'Turn bed leveling back on after homing')] },
  ],
  leds: [
    { title: 'LED lights', fields: [tog('leds.enabled', 'Status lights'), tog('leds.progress', 'Show print progress on the side lights'),
      num('leds.brightness', 'Brightness', '', 0.1, 1, 0.1)] },
  ],
};

async function loadSettings() {
  try {
    settings = await api('GET', '/api/settings');
    system = await api('GET', '/api/system');
  } catch (e) { toast(e.message, 'error'); return; }
  buildTab(ui.tab);
}

let saveTimer = null;
const pending = {};
function saveSetting(path, value) {
  const parts = path.split('.');
  let o = settings;
  for (const p of parts.slice(0, -1)) o = o[p];
  o[parts[parts.length - 1]] = value;
  Object.assign(pending, deepMergeObj(pending, nest(path, value)));
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const changes = { ...pending };
    for (const k of Object.keys(pending)) delete pending[k];
    try { settings = await post('/api/settings', changes); toast('Saved'); } catch (e) { toast(e.message, 'error'); }
  }, 600);
}
function deepMergeObj(a, b) {
  const out = { ...a };
  for (const [k, v] of Object.entries(b)) out[k] = (v && typeof v === 'object' && !Array.isArray(v) && out[k]) ? deepMergeObj(out[k], v) : v;
  return out;
}

function fieldNode(f) {
  const value = getPath(settings, f.path);
  const hint = f.hint ? `<small>${esc(f.hint)}</small>` : '';
  if (f.type === 'tog') {
    const node = el(`<div class="field"><div class="lab">${esc(f.label)}${hint}</div><label class="switch"><input type="checkbox"${value ? ' checked' : ''}><span></span></label></div>`);
    $('input', node).addEventListener('change', e => saveSetting(f.path, e.target.checked));
    return node;
  }
  const node = el(`<div class="field"><div class="lab">${esc(f.label)}${hint}</div><div class="stepper"><button>−</button><span class="v num"></span><button>+</button></div></div>`);
  let v = Number(value);
  const dec = (String(f.step).split('.')[1] || '').length;
  const show = () => { $('.v', node).textContent = `${v.toFixed(dec)}${f.unit ? ' ' + f.unit : ''}`; };
  const [minus, plus] = $$('button', node);
  const change = d => { v = Math.min(f.max, Math.max(f.min, Number((v + d).toFixed(dec)))); show(); saveSetting(f.path, v); };
  minus.onclick = () => change(-f.step);
  plus.onclick = () => change(f.step);
  show();
  return node;
}

function buildForm(container, sections) {
  for (const sec of sections) {
    const card = el(`<div class="card" style="margin-bottom:var(--gap)"><h3>${esc(sec.title)}</h3></div>`);
    sec.fields.forEach(f => card.appendChild(fieldNode(f)));
    container.appendChild(card);
  }
}

function buildTab(tab) {
  ui.tab = tab;
  updateRail();
  $$('#settings-tabs button').forEach(b => b.classList.toggle('sel', b.dataset.tab === tab));
  $$('.tab').forEach(t => t.classList.toggle('hidden', t.id !== `tab-${tab}`));
  const box = $(`#tab-${tab}`);
  if (!settings) return;
  if (tab === 'console') { scrollLog(true); return; }
  box.innerHTML = '';
  if (FORMS[tab]) buildForm(box, FORMS[tab]);
  if (tab === 'general') box.appendChild(presetsCard());
  if (tab === 'recovery') {
    box.appendChild(el(`<div class="card muted" style="font-size:14px">After a power cut the printer is reheated, Z is set to where the nozzle stopped (it is never homed into the part), X and Y are homed, and printing continues from just before the last confirmed move. The lead screws hold Z while the power is off.</div>`));
  }
  if (tab === 'scales') box.appendChild(scalesCard());
  if (tab === 'connection') box.appendChild(connectionCard());
  if (tab === 'system') box.appendChild(systemCard());
  if (tab === 'firmware') box.appendChild(firmwareCard());
  if (tab === 'wifi') buildWifiTab(box);
  renderSettingsLive();
}

function presetsCard() {
  const card = el('<div class="card"><h3>Material presets</h3><div class="stack" id="preset-rows"></div><div class="row" style="margin-top:8px"><button class="btn small primary" id="presets-save">Save presets</button></div></div>');
  const rows = $('#preset-rows', card);
  const presets = settings.presets.map(p => ({ ...p }));
  presets.forEach((p, i) => {
    const r = el(`<div class="row wrap"><input type="text" value="${esc(p.name)}" style="width:110px">
      <span class="muted">Nozzle</span><div class="stepper"><button>−</button><span class="v num">${p.hotend}</span><button>+</button></div>
      <span class="muted">Bed</span><div class="stepper"><button>−</button><span class="v num">${p.bed}</span><button>+</button></div></div>`);
    $('input', r).addEventListener('input', e => { presets[i].name = e.target.value; });
    const [st1, st2] = $$('.stepper', r);
    const wire = (st, key, max) => {
      const [m, pl] = $$('button', st);
      m.onclick = () => { presets[i][key] = Math.max(0, presets[i][key] - 5); $('.v', st).textContent = presets[i][key]; };
      pl.onclick = () => { presets[i][key] = Math.min(max, presets[i][key] + 5); $('.v', st).textContent = presets[i][key]; };
    };
    wire(st1, 'hotend', 285); wire(st2, 'bed', 120);
    rows.appendChild(r);
  });
  $('#presets-save', card).onclick = e => act(e.currentTarget, async () => { settings = await post('/api/settings', { presets }); toast('Presets saved'); });
  return card;
}

function scalesCard() {
  const box = el('<div class="grid cols-2"></div>');
  for (const side of ['left', 'right']) {
    const card = el(`<div class="card stack" data-scale="${side}"><h3>${side === 'left' ? 'Left' : 'Right'} spool holder</h3>
      <div class="row between"><span class="muted">Filament on holder</span><b class="num scale-g">–</b></div>
      <div class="row between"><span class="muted">Raw reading</span><span class="num scale-raw muted">–</span></div>
      <div class="row wrap"><button class="btn small" data-scale-act="tare">1. Tare (empty holder)</button>
      <button class="btn small" data-scale-act="calibrate">2. Calibrate with a weight</button></div></div>`);
    card.appendChild(fieldNode(num(`scales.${side}.spool_g`, 'Empty spool weight', 'g', 0, 1500, 10, 'Subtracted from the reading')));
    box.appendChild(card);
  }
  const wrap = el('<div></div>');
  if (!S || !S.hardware.i2c) wrap.appendChild(el('<div class="banner info"><div class="text">The LED and scale board (I2C) was not found on this computer.</div></div>'));
  wrap.appendChild(box);
  return wrap;
}

function connectionCard() {
  const card = el(`<div class="stack"><div class="card stack" id="conn-status"></div>
    <div class="card stack"><h3>Serial port</h3>
      <div class="row wrap"><input type="text" id="conn-port" value="${esc(settings.serial_port)}" style="flex:1">
      <select id="conn-baud">${[115200, 250000, 57600].map(b => `<option${b === settings.baudrate ? ' selected' : ''}>${b}</option>`).join('')}</select>
      <button class="btn small" id="conn-save">Save</button></div>
      <div class="muted" style="font-size:13px">"auto" picks the first USB serial port. The Skriware firmware runs at 115200.</div>
    </div>
    <div class="row wrap"><button class="btn" data-act="connect">Connect</button><button class="btn" data-act="disconnect">Disconnect</button>
      <button class="btn" data-act="reconnect">Reset board</button><button class="btn" data-act="m999">Restart after error (M999)</button></div></div>`);
  $('#conn-save', card).onclick = e => act(e.currentTarget, async () => {
    settings = await post('/api/settings', { serial_port: $('#conn-port', card).value.trim() || 'auto', baudrate: Number($('#conn-baud', card).value) });
    toast('Saved. Press Reset board to reconnect.');
  });
  return card;
}

function systemCard() {
  const urls = (system.ips || []).map(ip => `http://${ip}:${system.port}`);
  const card = el(`<div class="stack">
    <div class="card stack"><h3>Use from a phone or computer</h3>
      ${urls.length ? urls.map(u => `<div class="num" style="font-size:18px">${esc(u)}</div>`).join('') : '<div class="muted">No network address found.</div>'}
      ${system.api_key ? `<div class="row between"><span class="muted">API key (also for slicer upload)</span><b class="num" style="user-select:text">${esc(system.api_key)}</b></div>` : ''}
    </div>
    <div class="card" id="sys-key-field"></div>
    <div class="card stack"><h3>Slicer upload</h3><div class="muted" style="font-size:14px">In PrusaSlicer or OrcaSlicer, add a physical printer of type OctoPrint, use the address above and the API key. "Upload and print" then sends straight to this printer.</div></div>
    <div class="row wrap"><button class="btn" id="sys-reload">Reload screen</button><button class="btn danger" id="sys-off">Turn printer off</button>
      <span class="muted">NORTHBOLT ${esc(system.version)}</span></div></div>`);
  $('#sys-key-field', card).appendChild(fieldNode(tog('require_key_remote', 'Require the API key on other devices', 'This screen never needs it')));
  $('#sys-reload', card).onclick = () => location.reload();
  $('#sys-off', card).onclick = async () => {
    const printing = S && !['idle', 'finished', 'cancelled', 'error'].includes(S.job.state);
    if (await confirmBox('Turn the printer off?', printing ? '<b>A print is running and will be lost.</b>' : 'The Raspberry Pi shuts down and the printer turns off.', 'Turn off', 'danger')) {
      act(null, () => post('/api/power/off', { force: printing }));
    }
  };
  return card;
}

function renderSettingsLive() {
  if (!settings) return;
  if (ui.tab === 'scales') {
    for (const side of ['left', 'right']) {
      const card = $(`[data-scale="${side}"]`);
      if (!card) continue;
      const s = S.scales[side] || {};
      $('.scale-g', card).textContent = s.grams !== null && s.grams !== undefined ? `${s.grams} g` : (s.raw !== null && s.raw !== undefined ? 'not calibrated' : '–');
      $('.scale-raw', card).textContent = s.raw ?? '–';
    }
  }
  if (ui.tab === 'connection') {
    const c = S.connection;
    const box = $('#conn-status');
    if (!box) return;
    box.innerHTML = `<h3>Status</h3>
      <div class="row between"><span class="muted">Printer</span><b>${c.ready ? 'Connected' : (c.connected ? 'Connecting' : 'Not connected')}</b></div>
      <div class="row between"><span class="muted">Port</span><span class="num">${esc(c.port || '–')}</span></div>
      ${c.error ? `<div class="row between"><span class="muted">Last error</span><span>${esc(c.error)}</span></div>` : ''}
      <div class="row between"><span class="muted">Firmware</span><span class="num" style="font-size:12px;text-align:right">${esc((c.firmware || '–').replace('FIRMWARE_NAME:', '').split(' SOURCE_CODE_URL')[0])}</span></div>
      <div class="row between"><span class="muted">Line errors fixed by resend</span><span class="num">${c.stats.resends}</span></div>`;
  }
}

// ------------------------------------------------------------------ console
function appendLog(entries) {
  const log = $('#console-log');
  const near = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  for (const e of entries) {
    const cls = e.d === '>' ? 'out' : (/^error/i.test(e.text) ? 'err' : '');
    log.appendChild(el(`<div class="${cls}">${e.d === '>' ? '&gt; ' : ''}${esc(e.text)}</div>`));
  }
  while (log.childElementCount > 400) log.firstElementChild.remove();
  if (near) scrollLog(true);
}
function scrollLog() { const log = $('#console-log'); log.scrollTop = log.scrollHeight; }
async function sendConsole(text) {
  if (!text.trim()) return;
  try { await post('/api/console', { command: text }); } catch (e) { toast(e.message, 'error'); }
}

// ------------------------------------------------------------------ actions
const ACTIONS = {
  pause: b => act(b, () => post('/api/job/pause')),
  resume: b => act(b, () => post('/api/job/resume')),
  cancel: async b => {
    if (await confirmBox('Cancel the print?', 'The print stops now, the heaters turn off and the head moves away.', 'Cancel print', 'danger')) {
      act(b, () => post('/api/job/cancel'));
    }
  },
  'recovery-resume': () => recoveryDialog(),
  'recovery-discard': async b => {
    if (await confirmBox('Discard the interrupted print?', 'You won\'t be able to resume it.', 'Discard', 'danger')) act(b, () => post('/api/recovery/discard'));
  },
  'home-all': b => act(b, () => post('/api/home', { axes: '' })),
  'home-xy': b => act(b, () => post('/api/home', { axes: 'XY' })),
  'home-z': b => act(b, () => post('/api/home', { axes: 'Z' })),
  'motors-off': b => act(b, () => post('/api/motors_off')),
  'lift-up': b => act(b, () => post('/api/lift', { direction: 'up' })),
  'lift-down': b => act(b, () => post('/api/lift', { direction: 'down' })),
  extrude: b => act(b, () => post('/api/extrude', { tool: S.tool, amount: ui.extrudeAmt, speed: 3 })),
  retract: b => act(b, () => post('/api/extrude', { tool: S.tool, amount: -ui.extrudeAmt, speed: 10 })),
  cooldown: b => act(b, () => post('/api/cooldown')),
  'check-sensors': b => act(b, () => post('/api/runout/query')),
  'z-start': b => act(b, () => post('/api/zoffset/start', { tool: ui.zTool, heat: $('#z-heat').checked })),
  'z-cancel': b => act(b, () => post('/api/zoffset/cancel')),
  'z-accept': async b => {
    const z = S.zoffset;
    const next = z.old_offset - z.z + (z.paper || 0);
    if (await confirmBox('Save this height as Z 0?', `The ${toolName(z.tool).toLowerCase()} nozzle offset changes from <b>${z.old_offset.toFixed(2)}</b> to <b>${next.toFixed(2)} mm</b> and is saved to the printer.`, 'Save', 'ok')) {
      act(b, () => post('/api/zoffset/accept'));
    }
  },
  'z-finish': b => act(b, () => post('/api/zoffset/finish')),
  'z-reset': () => { ui.zErrorSeen = S.zoffset.error; renderZ(); },
  'usb-up': () => { ui.usbPath = ui.usbPath.includes('/') ? ui.usbPath.slice(0, ui.usbPath.lastIndexOf('/')) : ''; loadUsb(); },
  'usb-eject': () => usbEject(),
  'console-send': () => { const i = $('#console-input'); sendConsole(i.value); i.value = ''; },
  connect: b => act(b, () => post('/api/connection/connect')),
  disconnect: b => act(b, () => post('/api/connection/disconnect')),
  reconnect: b => act(b, () => post('/api/connection/reconnect')),
  m999: b => act(b, () => post('/api/connection/m999')),
};

document.addEventListener('click', e => {
  if (e.target.closest('.osk')) return;   // the keyboard handles its own keys
  const t = e.target.closest('button, [data-file], [data-heater], [data-net], [data-usb]');
  if (!t) return;
  const d = t.dataset;
  if (d.page) return showPage(d.page);
  if (d.goto) return showPage(d.goto);
  if (d.act && ACTIONS[d.act]) return ACTIONS[d.act](t);
  if (d.dismiss) return act(null, () => post('/api/alerts/dismiss', { id: Number(d.dismiss) }));
  if (d.heater) return tempDialog(d.heater);
  if (d.cal) { ui.cal = d.cal; return renderLevel(); }
  if (d.lv && LEVEL_ACTIONS[d.lv]) return LEVEL_ACTIONS[d.lv](t);
  if (d.lvjog) return act(t, () => post('/api/leveling/teach_jog', { [d.lvjog === 'x' ? 'dx' : 'dy']: Number(d.dir) * ui.lvStep }));
  if (d.lvstep) { ui.lvStep = Number(d.lvstep); $$('#lv-steps button').forEach(b => b.classList.toggle('on', b === t)); return; }
  if (d.lvpick !== undefined) return act(t, () => post('/api/leveling/teach_go', { index: Number(d.lvpick) }));
  if (d.usb !== undefined) return usbOpen(Number(d.usb));
  if (d.source) { ui.source = d.source; ui.usbPath = ''; $('#file-source').dataset.sig = ''; return loadFiles(); }
  if (d.file !== undefined && t.classList.contains('file')) return fileDialog(d.file);
  if (d.step) { ui.jogStep = Number(d.step); $$('#jog-steps button').forEach(b => b.classList.toggle('on', b === t)); return; }
  if (d.jog) return act(t, () => post('/api/move', { [d.jog]: Number(d.dir) * ui.jogStep }));
  if (d.tool !== undefined && t.closest('#tool-select')) return act(t, () => post('/api/tool', { tool: Number(d.tool) }));
  if (d.amt) { ui.extrudeAmt = Number(d.amt); $$('#extrude-amount button').forEach(b => b.classList.toggle('on', b === t)); return; }
  if (d.fan !== undefined) return act(t, () => post('/api/fan', { value: Math.round(Number(d.fan) * 2.55) }));
  if (d.preset !== undefined) {
    const p = S.config.presets[Number(d.preset)];
    return act(t, async () => { await post('/api/temp', { heater: `t${S.tool}`, target: p.hotend }); await post('/api/temp', { heater: 'bed', target: p.bed }); });
  }
  if (d.material !== undefined) { ui.material = Number(d.material); return renderFilament(); }
  if (d.fil) {
    const tool = Number(t.closest('[data-tool]').dataset.tool);
    const p = S.config.presets[ui.material] || S.config.presets[0];
    return act(t, () => post('/api/filament', { action: d.fil, tool, temp: p.hotend }));
  }
  if (d.tune) {
    const cur = d.tune === 'speed' ? S.speed : S.flow;
    return act(t, () => post(`/api/${d.tune}`, { value: cur + Number(d.d) }));
  }
  if (d.zstep) return act(null, () => post('/api/zoffset/step', { delta: Number(d.zstep) }));
  if (d.zpoint) return act(t, () => post('/api/zoffset/check', { point: d.zpoint }));
  if (d.ztool !== undefined) { ui.zTool = Number(d.ztool); return renderZ(); }
  if (d.tab) return buildTab(d.tab);
  if (d.net !== undefined) return wifiPick(wifiNets[Number(d.net)]);
  if (d.wifi === 'scan') { wifiNets = []; renderWifiList(); return refreshWifi(true); }
  if (d.wifi === 'other') return wifiOther();
  if (d.wifi === 'country') return wifiCountry();
  if (d.gcode) return sendConsole(d.gcode);
  if (d.scaleAct) {
    const side = t.closest('[data-scale]').dataset.scale;
    if (d.scaleAct === 'tare') return act(t, async () => { await post('/api/scales/tare', { side }); toast('Tared'); settings = await api('GET', '/api/settings'); });
    return scaleCalibrateDialog(side);
  }
});

document.addEventListener('change', e => {
  const t = e.target;
  if (t.dataset.runout) act(null, () => post('/api/runout', { side: t.dataset.runout, enabled: t.checked }));
  if (t.id === 'upload-input' && t.files[0]) { upload(t.files[0]); t.value = ''; }
});

$('#console-input').addEventListener('keydown', e => { if (e.key === 'Enter') ACTIONS['console-send'](); });

$('#estop').addEventListener('click', () => {
  modal({
    title: 'Emergency stop',
    body: '<p>Stops everything immediately: motors, heaters, the print. The board then needs a reset (Reconnect).</p>',
    actions: [{ label: 'Back' }, { label: 'STOP NOW', cls: 'danger', onClick: () => post('/api/connection/estop') }],
  });
});

function scaleCalibrateDialog(side) {
  let grams = 1000;
  const body = el(`<div><p class="muted">Put something of known weight on the empty ${side} holder, for example a full 1 kg spool's label weight minus its empty spool, or a kitchen-scale-checked object.</p>
    <div class="row" style="justify-content:center"><div class="stepper"><button data-g="-50">−</button><span class="v num"></span><button data-g="50">+</button></div></div></div>`);
  const upd = () => { $('.v', body).textContent = `${grams} g`; };
  body.addEventListener('click', e => { const b = e.target.closest('[data-g]'); if (b) { grams = Math.max(50, grams + Number(b.dataset.g)); upd(); } });
  upd();
  modal({
    title: 'Calibrate scale', body,
    actions: [{ label: 'Cancel' }, {
      label: 'Calibrate', cls: 'primary', onClick: async () => {
        await post('/api/scales/calibrate', { side, grams });
        settings = await api('GET', '/api/settings');
        toast('Scale calibrated');
      },
    }],
  });
}

// ------------------------------------------------------------------ start
function tick() {
  $('#clock').textContent = new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

async function refreshAll() {
  try { S = await api('GET', '/api/state'); render(); } catch (e) { /* askKey shows if needed */ }
  connectWS();
}

tick();
setInterval(tick, 10000);
refreshAll();
