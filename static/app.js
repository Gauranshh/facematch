'use strict';
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => Number(n || 0).toLocaleString();
const ago = t => { if (!t) return 'never'; const s = Date.now() / 1000 - t; if (s < 90) return 'just now'; if (s < 5400) return Math.round(s / 60) + ' min ago'; if (s < 129600) return Math.round(s / 3600) + ' h ago'; return Math.round(s / 86400) + ' days ago'; };
const dur = s => { if (s == null) return '…'; if (s < 90) return s + ' s'; if (s < 5400) return Math.round(s / 60) + ' min'; return (s / 3600).toFixed(1) + ' h'; };
const dstr = t => t ? new Date(t * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '';
const dtstr = t => t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'never';
const bytes = n => { if (!n) return '0 B'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024))); return (n / 1024 ** i).toFixed(i ? 1 : 0) + ' ' + u[i]; };
const toast = (m, ms = 2800) => { const t = $('#toast'); t.textContent = m; t.classList.remove('hid'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hid'), ms); };

let ME = null, ST = null, pollTimer = null;
const api = async (url, opt = {}) => {
  if (opt.json !== undefined) { opt.method = opt.method || 'POST'; opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(opt.json); }
  const r = await fetch(url, opt); let d = {}; try { d = await r.json(); } catch (e) { }
  if (r.status === 401 && ME && !url.startsWith('/api/auth/')) { ME = null; showAuth('login', 'Your session ended. Please sign in again.'); throw new Error('Signed out'); }
  if (!r.ok) throw new Error(d.error || ('Error ' + r.status));
  return d;
};
const isAdmin = () => ME && ME.role === 'admin';

// ======================================================================== auth screens
let authMode = 'login', inviteTok = null, authInfo = {};
function showAuth(mode, msg) {
  authMode = mode; clearTimeout(pollTimer); $('#app').classList.add('hid'); $('#auth').classList.remove('hid');
  const setup = mode === 'setup', inv = mode === 'invite';
  $('#aTitle').textContent = setup ? 'Create the admin account' : inv ? 'Set your password' : 'Sign in';
  $('#aSub').textContent = setup ? 'Welcome! This first account manages the library and invites everyone else.' : inv ? `Welcome${authInfo.display ? ', ' + authInfo.display : ''}. Choose a password to finish setting up “${authInfo.username || ''}”.` : 'Welcome back.';
  $('#aUserW').classList.toggle('hid', inv); $('#aUser').required = !inv; $('#aPass2').required = !login(); $('#aDispW').classList.toggle('hid', !setup);
  $('#aPass2W').classList.toggle('hid', login()); $('#aCodeW').classList.toggle('hid', !(setup && authInfo.code));
  $('#aPass').autocomplete = login() ? 'current-password' : 'new-password';
  $('#aGo').textContent = setup ? 'Create account' : inv ? 'Save password and continue' : 'Sign in';
  $('#aErr').textContent = msg || ''; $('#aPass').value = ''; $('#aPass2').value = '';
  setTimeout(() => (inv ? $('#aPass') : $('#aUser')).focus(), 50);
}
const login = () => authMode === 'login';
$('#authForm').onsubmit = async e => {
  e.preventDefault(); const err = $('#aErr'); err.textContent = ''; const pw = $('#aPass').value;
  if (!login() && pw.length < 8) return void (err.textContent = 'Use at least 8 characters for the password.');
  if (!login() && pw !== $('#aPass2').value) return void (err.textContent = 'The two passwords don’t match.');
  $('#aGo').disabled = true;
  try {
    if (authMode === 'login') await api('/api/auth/login', { json: { username: $('#aUser').value, password: pw } });
    else if (authMode === 'setup') await api('/api/auth/setup', { json: { username: $('#aUser').value, display: $('#aDisp').value, password: pw, code: $('#aCode').value } });
    else await api('/api/auth/invite/' + inviteTok, { json: { password: pw } });
    if (inviteTok) { inviteTok = null; history.replaceState(null, '', '#find'); }
    await boot();
  } catch (x) { err.textContent = x.message; } finally { $('#aGo').disabled = false; }
};

async function boot() {
  const h = location.hash;
  if (h.startsWith('#invite/')) {
    inviteTok = h.slice(8);
    try { authInfo = await api('/api/auth/invite/' + inviteTok); showAuth('invite'); } catch (e) { inviteTok = null; history.replaceState(null, '', '#'); showAuth('login', 'That invite link has expired. Ask your admin for a new one.'); }
    return;
  }
  const s = await api('/api/auth/state');
  if (!s.user) { authInfo = { code: s.setup_code_required }; return showAuth(s.setup_needed ? 'setup' : 'login'); }
  ME = s.user; startApp();
}

// ======================================================================== shell: nav, theme, account menu
function startApp() {
  $('#auth').classList.add('hid'); $('#app').classList.remove('hid');
  if (ME.theme) FMTheme.set(ME.theme);
  $('#avBtn').textContent = (ME.display || ME.username || '?').trim().slice(0, 1).toUpperCase();
  $('#mWho').textContent = ME.display || ME.username; $('#mRole').textContent = (isAdmin() ? 'Administrator' : 'Member') + ' · ' + ME.username;
  $$('.adm').forEach(e => e.classList.toggle('hid', !isAdmin()));
  $$('.xp').forEach(e => { if (!ME.can_export) e.classList.add('hid'); });
  if (!ME.can_export) $('#savePerson').classList.remove('hid');
  renderThemeMenu(); settingsInit = false; thrInit = null; ['#yFrom'].forEach(s => delete $(s).dataset.k);
  const h = location.hash.slice(1); tab(['find', 'people', 'library', 'admin'].includes(h) && (isAdmin() || (h !== 'library' && h !== 'admin')) ? h : 'find');
  poll(); renderChips();
}
function tab(n) {
  $$('nav.main button').forEach(b => b.classList.toggle('on', b.dataset.tab === n));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === n));
  if (n === 'people') loadPeople(true); if (n === 'library') renderLibrary(true); if (n === 'admin') adminTab(curAdmin);
  history.replaceState(null, '', '#' + n); scrollTo(0, 0);
}
$$('nav.main button').forEach(b => b.onclick = () => tab(b.dataset.tab));
$('#logoLink').onclick = e => { e.preventDefault(); tab('find'); };
$('#toLib').onclick = () => tab('library'); $('#toPeople').onclick = () => tab('people');

const ACC_NAMES = { cyanotype: 'Cyanotype', fern: 'Fern', plum: 'Plum', marigold: 'Marigold', rose: 'Rose', graphite: 'Graphite' };
const ACC_SW = { cyanotype: '#1b5fa6', fern: '#25724f', plum: '#7d3a93', marigold: '#c58a10', rose: '#b02a52', graphite: '#38424d' };
function renderThemeMenu() {
  const t = FMTheme.get();
  $$('#modeSeg button').forEach(b => b.classList.toggle('on', b.dataset.m === t.mode));
  $('#accSw').innerHTML = FMTheme.ACC.map(a => `<button data-a="${a}" class="${a === t.acc ? 'on' : ''}" style="background:${ACC_SW[a]}" title="${ACC_NAMES[a]}" aria-label="${ACC_NAMES[a]}"></button>`).join('');
  $$('#accSw button').forEach(b => b.onclick = () => saveTheme({ acc: b.dataset.a }));
}
$$('#modeSeg button').forEach(b => b.onclick = () => saveTheme({ mode: b.dataset.m }));
async function saveTheme(p) { const t = FMTheme.set({ ...FMTheme.get(), ...p }); renderThemeMenu(); try { await api('/api/me', { method: 'PATCH', json: { theme: t } }); } catch (e) { } }
$('#avBtn').onclick = e => { e.stopPropagation(); $('#menu').classList.toggle('hid'); };
$('#menu').onclick = e => e.stopPropagation();
document.addEventListener('click', e => { if (!e.target.closest('#menu')) $('#menu').classList.add('hid'); });
$('#mOut').onclick = async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { } ME = null; location.hash = ''; location.reload(); };
$('#mPass').onclick = () => { $('#menu').classList.add('hid'); modal(`<h2>Change password</h2>
  <label class="f"><span>Current password</span><input type="password" id="pw0" autocomplete="current-password"></label>
  <label class="f"><span>New password</span><input type="password" id="pw1" autocomplete="new-password"></label>
  <div class="small mut">At least 8 characters. Other devices will be signed out.</div><div class="err small" id="pwE"></div>
  <div class="row end"><button class="btn" id="c">Cancel</button><button class="btn p" id="ok">Change password</button></div>`, m => {
  m.querySelector('#c').onclick = closeModal;
  m.querySelector('#ok').onclick = async () => { try { await api('/api/me/password', { json: { old: m.querySelector('#pw0').value, new: m.querySelector('#pw1').value } }); closeModal(); toast('Password changed'); } catch (e) { m.querySelector('#pwE').textContent = e.message; } };
}); };
$('#mShares').onclick = () => { $('#menu').classList.add('hid'); sharesModal(); };

// ======================================================================== status polling
let settingsInit = false, thrInit = null;
async function poll() {
  clearTimeout(pollTimer); if (!ME) return;
  try {
    ST = await api('/api/status'); renderPill();
    if ($('#library').classList.contains('on')) renderLibrary();
    if ($('#people').classList.contains('on')) renderDisc();
    const L = ST.library;
    $('#toLib').classList.toggle('hid', !(isAdmin() && !L.sources.length));
    $('#emptyHint').textContent = !L.sources.length ? (isAdmin() ? 'First add the folders, albums or drives that hold your photos. They are analysed once, in the background.' : 'No photo folders are shared with your account yet — ask your admin.') : 'Your photos are analysed once in the background. After that, every search takes a fraction of a second.';
    const mdl = ST.settings.model; if (thrInit !== mdl) { thrInit = mdl; $('#thr').value = ST.default_threshold; $('#thrv').textContent = (+$('#thr').value).toFixed(2); }
    const sel = $('#srcSel'), want = L.sources.map(s => s.id + ':' + (s.path || s.label)).join('|');
    if (sel.dataset.k !== want) { sel.dataset.k = want; const cur = sel.value; sel.innerHTML = '<option value="">All folders</option>' + L.sources.map(s => `<option value="${s.id}">${esc(s.label || s.path)}</option>`).join(''); sel.value = cur; $('#srcWrap').classList.toggle('hid', L.sources.length < 2); }
    $('#vidWrap').classList.toggle('hid', !L.videos);
    fillYears(L);
    if (!settingsInit && isAdmin()) { settingsInit = true; initSettings(); }
  } catch (e) { if (ME) $('#pill').textContent = 'Server not reachable'; }
  const busy = ST && (ST.index.running || ST.discover.state === 'running' || ['downloading', 'switching', 'starting'].includes(ST.model_job.state));
  pollTimer = setTimeout(poll, busy ? 1500 : 6000);
}
function fillYears(L) {
  if (!L.year_min) return; const key = L.year_min + '-' + L.year_max; if ($('#yFrom').dataset.k === key) return; $('#yFrom').dataset.k = key;
  const ys = []; for (let y = L.year_max; y >= L.year_min; y--) ys.push(y); const o = a => `<option value="">${a}</option>` + ys.map(y => `<option>${y}</option>`).join('');
  const f = $('#yFrom').value, t = $('#yTo').value; $('#yFrom').innerHTML = o('Any year'); $('#yTo').innerHTML = o('Any year'); $('#yFrom').value = f; $('#yTo').value = t;
}
function renderPill() {
  const i = ST.index, p = $('#pill'), d = ST.discover; p.classList.toggle('run', i.running && i.state !== 'paused');
  if (i.running) p.textContent = i.state === 'paused' ? 'Indexing paused' : i.state === 'scanning' ? 'Scanning folders…' : i.live ? `Adding ${fmt(i.total)} new photo${i.total == 1 ? '' : 's'}…` : `Indexing ${fmt(i.done)} / ${fmt(i.total)}` + (i.rate ? ` · ${i.rate}/s` : '');
  else if (d.state === 'running') { p.textContent = 'Finding people…'; p.classList.add('run'); }
  else p.textContent = `${fmt(ST.library.photos)} photos · ${fmt(ST.library.faces)} faces`;
}

// ======================================================================== what we search for
const drop = $('#drop'); let refId = null, picks = {}, selPeople = new Map(), pos = new Set(), neg = new Set();
['dragover', 'dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.toggle('on', ev === 'dragover'); if (ev === 'drop' && e.dataTransfer.files.length) upload(e.dataTransfer.files); }));
$('#file').onchange = e => { if (e.target.files.length) upload(e.target.files); e.target.value = ''; };
async function upload(files) {
  $('#findMsg').textContent = ''; const fd = new FormData(); [...files].forEach(f => fd.append('face', f));
  try {
    const d = await api('/api/reference', { method: 'POST', body: fd });
    refId = d.ref; picks = {}; clearMarks(); renderRef(d); updQuery();
    if (d.notes.length) toast(d.notes.join(' · '), 5000); run();
  } catch (e) { $('#findMsg').textContent = e.message; }
}
function renderRef(d) {
  const box = $('#refBox'); box.innerHTML = '<div class="ref"></div>'; const w = box.firstChild;
  d.photos.forEach((p, pi) => {
    const g = document.createElement('div'); g.className = 'rp'; g.title = p.crops.length > 1 ? 'Several faces found — click the right person' : p.name;
    p.crops.forEach((c, ci) => { const im = new Image(); im.alt = ''; im.src = 'data:image/jpeg;base64,' + c; if (ci === 0) im.className = 'on'; im.onclick = () => { if (p.crops.length < 2) return; picks[pi] = ci; [...g.children].forEach((x, k) => x.classList.toggle('on', k === ci)); clearMarks(); run(); }; g.appendChild(im); });
    w.appendChild(g);
  });
  const x = document.createElement('button'); x.className = 'btn'; x.textContent = '✕'; x.title = 'Remove uploaded photo'; x.setAttribute('aria-label', 'Remove uploaded photo'); x.onclick = () => { refId = null; box.innerHTML = ''; updQuery(); if (tok) run(); }; w.appendChild(x);
}
async function renderChips() {
  const d = await api('/api/people?show=named&limit=14'), c = $('#peopleChips'); c.innerHTML = '';
  d.people.forEach(p => { const b = document.createElement('button'); b.type = 'button'; b.className = 'chip' + (selPeople.has(p.id) ? ' on' : ''); b.innerHTML = `<img src="/api/people/${p.id}/crop" alt=""><span>${esc(p.name)}</span>`;
    b.onclick = () => { selPeople.has(p.id) ? selPeople.delete(p.id) : (selPeople.size < 3 && selPeople.set(p.id, p.name)); clearMarks(); renderChips(); updQuery(); run(); }; c.appendChild(b); });
  if (d.total > 14) { const m = document.createElement('button'); m.className = 'chip'; m.textContent = `+${d.total - 14} more`; m.onclick = () => tab('people'); c.appendChild(m); }
  const un = d.counts.discovered; $('#toPeople').classList.toggle('hid', !(un > 0 && !d.people.length));
  if (!d.people.length && un > 0) $('#emptyHint').textContent = `${fmt(un)} people were found in your library. Name them in the People tab, or upload a photo here.`;
}
function updQuery() {
  const n = selPeople.size + (refId ? 1 : 0); $('#go').disabled = n === 0;
  const t = $('#together'); t.classList.toggle('hid', n < 2); if (n > 1) t.textContent = `Showing photos where all ${n} of them appear together.`;
}

// ======================================================================== search + results
let tok = null, total = 0, items = [], loading = false, selMode = 'none', selSet = new Set(), lbIdx = -1;
const thr = $('#thr'); thr.oninput = () => $('#thrv').textContent = (+thr.value).toFixed(2); thr.onchange = () => { if (tok) run(); };
['#sort', '#srcSel', '#yFrom', '#yTo', '#grp', '#hq', '#vid'].forEach(s => $(s).onchange = () => { if (tok) run(); });
$('#go').onclick = () => run();
$('#asideT').onclick = () => { const a = $('aside.panel'); a.classList.toggle('compact'); $('#asideT').textContent = a.classList.contains('compact') ? 'Change search' : 'Hide options'; };
function buildBody() {
  const q = [...selPeople.keys()].map(id => ({ person: id })); if (refId) q.push({ ref: refId, picks });
  const b = { queries: q, threshold: +thr.value, sort: $('#sort').value, sources: $('#srcSel').value ? [+$('#srcSel').value] : null, hide_low_quality: $('#hq').checked, videos: $('#vidWrap').classList.contains('hid') ? true : $('#vid').checked, pos: [...pos], neg: [...neg] };
  if ($('#yFrom').value) b.date_from = new Date(+$('#yFrom').value, 0, 1).getTime() / 1000;
  if ($('#yTo').value) b.date_to = new Date(+$('#yTo').value + 1, 0, 1).getTime() / 1000;
  const g = $('#grp').value; if (g === 'solo') b.max_faces = 1; if (g === 'small') { b.min_faces = 2; b.max_faces = 5; } if (g === 'big') b.min_faces = 6;
  return b;
}
async function run() {
  if (!selPeople.size && !refId) return; $('#findMsg').textContent = '';
  try {
    const d = await api('/api/search', { json: buildBody() });
    tok = d.token; total = d.count; items = []; selMode = 'none'; selSet = new Set(); $('#grid').innerHTML = '';
    $('#empty').classList.add('hid'); $('#rwrap').classList.remove('hid'); $('#savePerson').classList.toggle('hid', !refId);
    $('#rcount').textContent = total ? `${fmt(total)} photo${total == 1 ? '' : 's'} found` : 'No matching photos'; $('#rms').textContent = `· searched ${fmt(d.photos_indexed)} photos in ${d.ms} ms`;
    const n = $('#notice'); n.classList.add('hid');
    if (d.indexing) { n.classList.remove('hid'); n.textContent = 'Your library is still being indexed, so this list may be incomplete. Press “Find photos” again later to refresh.'; }
    else if (!d.photos_indexed) { n.classList.remove('hid'); n.textContent = isAdmin() ? 'No photos indexed yet — add a folder in the Library tab.' : 'No photos are available to your account yet.'; }
    else if (!total) { n.classList.remove('hid'); n.textContent = 'Nothing matched. Move “Match strictness” toward “More matches”, relax the filters, or use a clearer front-facing photo.'; }
    updSel(); updFb(); more();
    if (matchMedia('(max-width:900px)').matches) { $('aside.panel').classList.add('compact'); scrollTo(0, 0); }
  } catch (e) { $('#findMsg').textContent = e.message; }
}
async function more() {
  if (loading || !tok || items.length >= total) return; loading = true; const t = tok;
  try { const d = await api(`/api/search/${t}/page?offset=${items.length}&limit=60`); if (t !== tok) return; if (!d.items.length) { total = items.length; return; } d.items.forEach(add); }
  catch (e) { if (/expired/.test(e.message)) { toast('Results expired — searching again'); run(); } } finally { loading = false; }
  if (items.length < total && $('#sentinel').getBoundingClientRect().top < innerHeight + 600) more();
}
new IntersectionObserver(es => es[0].isIntersecting && more(), { rootMargin: '800px' }).observe($('#sentinel'));
function add(it) {
  const i = items.push(it) - 1, d = document.createElement('div'); d.className = 'tile vf'; d.dataset.i = i;
  let pp = '50% 50%'; if (it.box && it.w && it.h) pp = `${Math.min(100, Math.max(0, (it.box[0] + it.box[2] / 2) / it.w * 100))}% ${Math.min(100, Math.max(0, (it.box[1] + it.box[3] / 2) / it.h * 100))}%`;
  d.innerHTML = `<span class="vf-b"></span><img loading="lazy" src="/thumb/${it.id}" style="object-position:${pp}" alt=""><span class="ck" role="checkbox" aria-label="Select">✓</span>${it.vid ? '<span class="vb">▶</span>' : ''}<span class="fb"><button data-k="y" title="This is them" aria-label="This is them">✓</button><button data-k="n" title="Not them" aria-label="Not them">✗</button></span><span class="sc">${Math.round(it.score * 100)}%</span>`;
  d.title = `${it.name}\n${dstr(it.taken)}${it.dir ? '\n' + it.dir : ''}`;
  d.onclick = e => { const k = e.target.dataset && e.target.dataset.k; if (e.target.classList.contains('ck')) return toggle(it.id, d); if (k) return mark(it, k, d); openLb(i); };
  $('#grid').appendChild(d); mclass(it, d); if (isSel(it.id)) d.classList.add('sel');
}

// "this is them / not them" feedback
function mclass(it, d) { d.classList.toggle('okd', pos.has(it.f)); d.classList.toggle('rej', neg.has(it.f)); }
function mark(it, k, d) {
  if (k === 'y') { neg.delete(it.f); pos.has(it.f) ? pos.delete(it.f) : pos.add(it.f); } else { pos.delete(it.f); neg.has(it.f) ? neg.delete(it.f) : neg.add(it.f); }
  const el = d || $(`.tile[data-i="${items.indexOf(it)}"]`); el && mclass(it, el); updFb();
}
function updFb() { const n = pos.size + neg.size; $('#fbBar').classList.toggle('hid', !n); $('#fbText').textContent = `${pos.size} marked “this is them” · ${neg.size} marked “not them”`; }
function clearMarks() { pos.clear(); neg.clear(); $$('.tile').forEach(t => t.classList.remove('okd', 'rej')); updFb(); }
$('#fbClear').onclick = clearMarks;
$('#fbGo').onclick = async () => {
  try {
    if (selPeople.size === 1 && !refId) { await api(`/api/people/${[...selPeople.keys()][0]}/feedback`, { json: { pos: [...pos], neg: [...neg] } }); pos.clear(); neg.clear(); toast('Learned — this person will be found more accurately from now on'); }
    run();
  } catch (e) { toast(e.message); }
};

// selection / export
const isSel = id => selMode === 'all' ? !selSet.has(id) : selSet.has(id);
const nSel = () => selMode === 'all' ? total - selSet.size : selSet.size;
function toggle(id, el) { selSet.has(id) ? selSet.delete(id) : selSet.add(id); el.classList.toggle('sel', isSel(id)); updSel(); }
function updSel() {
  const n = nSel(); $('#saveBtn').disabled = $('#zipBtn').disabled = $('#shareBtn').disabled = !n;
  $('#selAll').textContent = selMode === 'all' && !selSet.size ? 'Select none' : `Select all ${fmt(total)}`; $('#grid').classList.toggle('selecting', n > 0);
  $('#saveBtn').textContent = n ? `Save ${fmt(n)} to folder…` : 'Save to folder…'; $('#zipBtn').textContent = n ? `Download ${fmt(n)} (.zip)` : 'Download .zip';
  $('#shareBtn').classList.toggle('hid', !ME.can_export || (ST && ST.sharing === false));
}
$('#selAll').onclick = () => { selMode = (selMode === 'all' && !selSet.size) ? 'none' : 'all'; selSet = new Set(); $$('.tile').forEach(t => t.classList.toggle('sel', isSel(items[t.dataset.i].id))); updSel(); };
const selBody = () => selMode === 'all' ? { all: true, exclude: [...selSet] } : { ids: [...selSet] };
$('#zipBtn').onclick = () => { const f = document.createElement('form'); f.method = 'POST'; f.action = `/api/search/${tok}/zip`; const b = selBody(); [['all', b.all ? 1 : 0], ['exclude', (b.exclude || []).join(',')], ['ids', (b.ids || []).join(',')], ['layout', $('#layout').value]].forEach(([k, v]) => { const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i); }); document.body.appendChild(f); f.submit(); f.remove(); toast('Preparing download…'); };
$('#saveBtn').onclick = () => modal(`<h2>Save ${fmt(nSel())} photos to a folder</h2><div class="row"><input type="text" id="dst" placeholder="Destination folder path"><button class="btn" id="br">Browse…</button></div><div class="small mut" style="margin-top:8px">Copies are made; your originals are never moved or changed.</div><div id="ex" class="small"></div><div class="row end"><button class="btn" id="c">Close</button><button class="btn p" id="ok">Copy photos</button></div>`, m => {
  m.querySelector('#c').onclick = closeModal; m.querySelector('#br').onclick = () => browse(p => setTimeout(() => $('#dst').value = p, 0), true);
  m.querySelector('#ok').onclick = async () => { const ex = m.querySelector('#ex'); try { const d = await api(`/api/search/${tok}/export`, { json: { ...selBody(), out: m.querySelector('#dst').value, layout: $('#layout').value } }); const t = setInterval(async () => { const j = await api('/api/jobs/' + d.job); ex.className = 'small'; ex.textContent = `${fmt(j.done)} / ${fmt(j.total)} copied` + (j.missing ? ` (${j.missing} unavailable)` : ''); if (j.state === 'done') { clearInterval(t); ex.textContent += ' — done ✓'; } }, 600); } catch (e) { ex.textContent = e.message; ex.className = 'small err'; } };
});
$('#savePerson').onclick = () => modal(`<h2>Remember this person</h2><input type="text" id="pn" placeholder="Name, e.g. Mom"><div class="row end"><button class="btn" id="c">Cancel</button><button class="btn p" id="ok">Save</button></div>`, m => {
  m.querySelector('#c').onclick = closeModal; m.querySelector('#ok').onclick = async () => { try { await api('/api/people', { json: { ref: refId, picks, name: m.querySelector('#pn').value, pos: [...pos], neg: [...neg] } }); closeModal(); toast('Saved to People'); renderChips(); } catch (e) { toast(e.message); } };
});

// ======================================================================== share links
$('#shareBtn').onclick = () => modal(`<h2>Share ${fmt(nSel())} photos</h2>
  <p class="mut small">Anyone with the link can view these photos — no account needed. The link stops working when it expires or when you revoke it.</p>
  <label class="f"><span>Title</span><input type="text" id="shT" value="Photos for you"></label>
  <div class="row"><label class="f grow"><span>Expires after</span><select id="shD"><option value="1">1 day</option><option value="7" selected>7 days</option><option value="30">30 days</option><option value="90">90 days</option></select></label></div>
  <label class="chk"><input type="checkbox" id="shDl" checked> Let people download them (.zip)</label>
  <div id="shOut"></div><div class="err small" id="shE"></div>
  <div class="row end"><button class="btn" id="c">Close</button><button class="btn p" id="ok">Create link</button></div>`, m => {
  m.querySelector('#c').onclick = closeModal;
  m.querySelector('#ok').onclick = async () => {
    try {
      const d = await api(`/api/search/${tok}/share`, { json: { ...selBody(), title: m.querySelector('#shT').value, days: +m.querySelector('#shD').value, allow_download: m.querySelector('#shDl').checked } });
      const url = location.origin + d.url; m.querySelector('#shOut').innerHTML = `<div class="linkbox" style="margin-top:12px"><input type="text" id="shU" readonly value="${esc(url)}"><button class="btn" id="cp">Copy</button></div><div class="small mut">${fmt(d.count)} photos in this link.</div>`;
      m.querySelector('#cp').onclick = () => { m.querySelector('#shU').select(); try { navigator.clipboard.writeText(url); } catch (e) { document.execCommand('copy'); } toast('Link copied'); }; m.querySelector('#ok').classList.add('hid');
    } catch (e) { m.querySelector('#shE').textContent = e.message; }
  };
});
async function sharesModal() {
  const d = await api('/api/shares');
  modal(`<h2>${isAdmin() ? 'All share links' : 'My share links'}</h2>` + (d.shares.length ? `<table class="t"><thead><tr><th>Link</th><th>Views</th><th>Expires</th><th></th></tr></thead><tbody>${d.shares.map(s => {
    const live = !s.revoked && s.expires > Date.now() / 1000;
    return `<tr><td><b>${esc(s.title)}</b><div class="small mut">${fmt(s.count)} photos${isAdmin() ? ' · ' + esc(s.by) : ''}</div></td><td>${fmt(s.views)}</td><td class="small">${s.revoked ? 'Revoked' : live ? dstr(s.expires) : 'Expired'}</td><td class="row">${live ? `<button class="btn" data-cp="${esc(s.token)}">Copy</button><button class="btn danger" data-rv="${esc(s.token)}">Revoke</button>` : ''}</td></tr>`;
  }).join('')}</tbody></table>` : '<p class="mut">You haven’t shared anything yet. Select photos in your results and press “Share link…”.</p>') + '<div class="row end"><button class="btn" id="c">Close</button></div>', m => {
    m.querySelector('#c').onclick = closeModal;
    m.querySelectorAll('[data-cp]').forEach(b => b.onclick = () => { try { navigator.clipboard.writeText(location.origin + '/s/' + b.dataset.cp); toast('Link copied'); } catch (e) { toast('Copy failed'); } });
    m.querySelectorAll('[data-rv]').forEach(b => b.onclick = async () => { await api('/api/shares/' + b.dataset.rv, { method: 'DELETE' }); toast('Link revoked'); sharesModal(); });
  });
}

// ======================================================================== lightbox
function openLb(i) {
  lbIdx = i; const it = items[i], m = $('#lbMedia'); $('#lb').classList.remove('hid'); m.innerHTML = '';
  if (it.vid) { const v = document.createElement('video'); v.controls = true; v.autoplay = true; v.src = '/photo/' + it.id; v.onloadedmetadata = () => { try { v.currentTime = Math.max(0, (it.t || 0) - 1); } catch (e) { } }; m.appendChild(v); }
  else { const im = new Image(); im.alt = ''; im.src = '/photo/' + it.id; m.appendChild(im); }
  $('#lbName').textContent = `${it.name} · ${dstr(it.taken)} · ${Math.round(it.score * 100)}% match`; $('#lbOpen').href = '/photo/' + it.id;
  $('#lbYes').style.outline = pos.has(it.f) ? '2px solid #51cf66' : ''; $('#lbNo').style.outline = neg.has(it.f) ? '2px solid #ff6b6b' : '';
  if (i > items.length - 5) more();
}
const lbGo = d => { const n = lbIdx + d; if (n >= 0 && n < items.length) openLb(n); };
const lbClose = () => { $('#lb').classList.add('hid'); $('#lbMedia').innerHTML = ''; };
$('#lbX').onclick = lbClose; $('#lbL').onclick = () => lbGo(-1); $('#lbR').onclick = () => lbGo(1);
$('#lbYes').onclick = () => { mark(items[lbIdx], 'y'); openLb(lbIdx); }; $('#lbNo').onclick = () => { mark(items[lbIdx], 'n'); openLb(lbIdx); };
$('#lb').onclick = e => { if (e.target.id === 'lb') lbClose(); };
let tx = null; $('#lb').addEventListener('touchstart', e => { tx = e.touches[0].clientX; }, { passive: true });
$('#lb').addEventListener('touchend', e => { if (tx == null) return; const d = e.changedTouches[0].clientX - tx; tx = null; if (Math.abs(d) > 60) lbGo(d < 0 ? 1 : -1); }, { passive: true });
addEventListener('keydown', e => { if ($('#lb').classList.contains('hid')) { if (e.key === 'Escape') closeModal(); return; } if (e.key === 'Escape') lbClose(); if (e.key === 'ArrowLeft') lbGo(-1); if (e.key === 'ArrowRight') lbGo(1); });

// ======================================================================== modal + folder browser
function modal(html, init) { const m = $('#mbox'); m.innerHTML = html; $('#modal').classList.remove('hid'); init && init(m); const i = m.querySelector('input[type=text],input[type=password]'); i && !i.readOnly && i.focus(); }
function closeModal() { $('#modal').classList.add('hid'); }
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
function browse(cb, reopenSave) {
  async function load(p) {
    let d; try { d = await api('/api/fs?path=' + encodeURIComponent(p)); } catch (e) { toast(e.message); return; }
    modal(`<h2>Choose a folder</h2><input type="text" id="cur" value="${esc(d.path)}" placeholder="Type or paste a path (e.g. E:\\Photos or /media/usb)"><div class="fl" id="fl"></div><div class="row end"><button class="btn" id="c">Cancel</button><button class="btn p" id="use">Use this folder</button></div>`, m => {
      const fl = m.querySelector('#fl'); if (d.parent !== null) { const u = document.createElement('div'); u.textContent = '⬆ ..'; u.onclick = () => load(d.parent); fl.appendChild(u); }
      d.dirs.forEach(x => { const r = document.createElement('div'); r.textContent = '📁 ' + (d.path ? x.name : x.path); r.onclick = () => load(x.path); fl.appendChild(r); });
      m.querySelector('#cur').onkeydown = e => { if (e.key === 'Enter') load(e.target.value); };
      m.querySelector('#c').onclick = () => reopenSave ? (closeModal(), $('#saveBtn').click()) : closeModal();
      m.querySelector('#use').onclick = () => { const v = m.querySelector('#cur').value; if (!v) return toast('Open a folder first'); if (reopenSave) $('#saveBtn').click(); else closeModal(); cb(v); };
    });
  }
  load('');
}
$('#addSrc').onclick = () => browse(async p => { try { await api('/api/sources', { json: { path: p } }); toast('Folder added — indexing started'); poll(); } catch (e) { toast(e.message, 4000); } });

// ======================================================================== people
let pShow = 'named', pOff = 0, mergeSel = new Set();
$$('.tabs button[data-ps]').forEach(b => b.onclick = () => { pShow = b.dataset.ps; $$('.tabs button[data-ps]').forEach(x => x.classList.toggle('on', x === b)); loadPeople(true); });
async function loadPeople(reset) {
  if (reset) { pOff = 0; mergeSel.clear(); $('#peopleGrid').innerHTML = ''; updMerge(); }
  const d = await api(`/api/people?show=${pShow}&offset=${pOff}&limit=60`), g = $('#peopleGrid');
  $('#cNamed').textContent = d.counts.named || ''; $('#cDisc').textContent = d.counts.discovered || ''; $('#cHidden').textContent = d.counts.hidden || '';
  if (reset && !d.people.length) g.innerHTML = `<p class="mut">${pShow === 'named' ? 'Nobody named yet. Open “Discovered” and name someone, or upload a photo on the Find tab and press “Remember this person”.' : pShow === 'discovered' ? (isAdmin() ? 'No new people found yet. Press “Scan for people” once your photos are indexed.' : 'No new people to name right now.') : 'Nothing hidden.'}</p>`;
  d.people.forEach(p => g.appendChild(card(p))); pOff += d.people.length; $('#pMore').classList.toggle('hid', pOff >= d.total);
  renderDisc();
}
$('#pMore').onclick = () => loadPeople(false);
function card(p) {
  const c = document.createElement('div'); c.className = 'pc';
  c.innerHTML = `${p.pool ? '' : '<input type="checkbox" class="mg" title="Select two people to merge" aria-label="Select to merge">'}<img loading="lazy" src="/api/people/${p.id}/crop" alt="" data-hideerr="1"><span class="name ${p.name ? '' : 'un'}" role="button" tabindex="0">${p.name ? esc(p.name) : 'Add a name'}</span><div class="meta">${fmt(p.nphotos)} photo${p.nphotos == 1 ? '' : 's'}</div><div class="row"><button class="btn p" data-a="find">Find photos</button><button class="btn" data-a="hide">${p.hidden ? 'Unhide' : 'Hide'}</button>${p.pool ? '' : '<button class="btn danger" data-a="del" title="Delete" aria-label="Delete">✕</button>'}</div>`;
  c.querySelector('img').onerror = e => e.target.style.visibility = 'hidden';
  const mg = c.querySelector('.mg'); if (mg) mg.onchange = e => { e.target.checked ? mergeSel.add(p.id) : mergeSel.delete(p.id); updMerge(); };
  const nm = c.querySelector('.name'), editName = () => modal(`<h2>${p.name ? 'Rename' : 'Name this person'}</h2><input type="text" id="pn" value="${esc(p.name)}" placeholder="e.g. Mom">${p.fb ? `<div class="small mut" style="margin-top:8px">Learned from ${p.fb} corrections you made. <a href="#" id="rst">Forget them</a></div>` : ''}<div class="row end"><button class="btn" id="c">Cancel</button><button class="btn p" id="ok">Save</button></div>`, m => {
    m.querySelector('#c').onclick = closeModal; const rs = m.querySelector('#rst'); if (rs) rs.onclick = async e => { e.preventDefault(); await api(`/api/people/${p.id}/feedback`, { method: 'DELETE' }); toast('Corrections forgotten'); closeModal(); loadPeople(true); };
    const go = async () => { try { const nmv = m.querySelector('#pn').value; if (p.pool) await api('/api/people/claim', { json: { pool_id: p.id, name: nmv } }); else await api('/api/people/' + p.id, { method: 'PATCH', json: { name: nmv } }); closeModal(); loadPeople(true); renderChips(); } catch (e) { toast(e.message); } };
    m.querySelector('#ok').onclick = go; m.querySelector('#pn').onkeydown = e => e.key === 'Enter' && go();
  });
  nm.onclick = editName; nm.onkeydown = e => e.key === 'Enter' && editName();
  c.querySelector('[data-a=find]').onclick = () => { selPeople = new Map([[p.id, p.name || 'Person']]); refId = null; $('#refBox').innerHTML = ''; clearMarks(); tab('find'); renderChips(); updQuery(); run(); };
  c.querySelector('[data-a=hide]').onclick = async () => { try { if (p.pool) await api('/api/people/claim', { json: { pool_id: p.id, hidden: true } }); else await api('/api/people/' + p.id, { method: 'PATCH', json: { hidden: !p.hidden } }); loadPeople(true); renderChips(); } catch (e) { toast(e.message); } };
  const del = c.querySelector('[data-a=del]'); if (del) del.onclick = async () => { if (p.name && !confirm(`Remove ${p.name} from People?`)) return; await api('/api/people/' + p.id, { method: 'DELETE' }); loadPeople(true); renderChips(); };
  return c;
}
function updMerge() { $('#mergeBtn').classList.toggle('hid', mergeSel.size !== 2); }
$('#mergeBtn').onclick = async () => { if (!confirm('Merge these two into one person? (Use this when the same person was split into two groups.)')) return; try { await api('/api/people/merge', { json: { ids: [...mergeSel] } }); toast('Merged'); loadPeople(true); renderChips(); } catch (e) { toast(e.message); } };
$('#discBtn').onclick = async () => { try { await api('/api/people/discover', { json: {} }); toast('Scanning for people…'); poll(); } catch (e) { toast(e.message, 4000); } };
function renderDisc() {
  if (!ST) return; const d = ST.discover, run = d.state === 'running'; $('#discBtn').disabled = run || ST.index.running;
  $('#discMsg').textContent = run ? d.message : ST.index.running ? 'Indexing is still running — people are grouped automatically when it finishes.' : (d.finished ? `Last scan ${ago(d.finished)}: ${d.message}` : d.message || '');
  if (d.state === 'idle' && d.finished && renderDisc.last !== d.finished) { renderDisc.last = d.finished; if ($('#people').classList.contains('on')) loadPeople(true); renderChips(); }
}

// ======================================================================== library (admin)
function initSettings() {
  const s = ST.settings; $('#wk').max = ST.cpus; $('#wk').value = s.workers; $('#wkv').textContent = s.workers; $('#detail').value = s.detail; $('#auto').value = String(s.autoscan_hours);
  $('#optVideo').checked = s.videos; $('#optDisc').checked = s.auto_discover; $('#optBackup').checked = s.backup; $('#optWatch').checked = s.watch !== false; $('#optShare').checked = s.sharing !== false; $('#excl').value = s.exclude;
}
const WATCH_TXT = { live: ['watching live', 'live'], poll: ['checking every few seconds', 'live'], off: ['not watched', 'off'] };
function renderLibrary(force) {
  if (!ST || !isAdmin()) return; const i = ST.index, run = i.running;
  $('#idxTitle').textContent = run ? (i.state === 'paused' ? 'Paused' : i.state === 'scanning' ? 'Scanning…' : i.live ? 'Adding new photos' : 'Indexing photos in the background') : (i.message || 'Idle');
  $('#idxBar').style.width = (i.total ? 100 * i.done / i.total : (run ? 3 : 0)) + '%';
  $('#idxText').textContent = run ? (i.state === 'scanning' ? i.message : `${fmt(i.done)} of ${fmt(i.total)} · ${fmt(i.faces)} faces found` + (i.rate ? ` · ${i.rate}/s · about ${dur(i.eta)} left` : '') + (i.errors ? ` · ${i.errors} unreadable` : '')) : `${fmt(ST.library.photos)} files analysed, ${fmt(ST.library.faces)} faces${ST.library.videos ? ', ' + fmt(ST.library.videos) + ' videos' : ''}. Searches are instant.`;
  $('#bPause').textContent = i.paused ? 'Resume' : 'Pause'; $('#bPause').disabled = $('#bStop').disabled = !run; $('#bScan').disabled = run;
  const l = $('#srcList'), key = JSON.stringify(ST.library.sources) + run; if (force || l.dataset.k !== key) {
    l.dataset.k = key; l.innerHTML = '';
    if (!ST.library.sources.length) l.innerHTML = '<div class="panel mut">No folders yet. Click “+ Add folder” and pick your photo folder, album or external drive.</div>';
    ST.library.sources.forEach(s => {
      const w = WATCH_TXT[s.watch] || WATCH_TXT.off, d = document.createElement('div'); d.className = 'panel src';
      d.innerHTML = `<div><div class="path">${esc(s.path)} <span class="badge ${s.online ? '' : 'off'}">${s.online ? 'connected' : 'not connected'}</span> <span class="badge ${w[1]}">${w[0]}</span></div><div class="mut small">${fmt(s.photos)} files · ${fmt(s.faces)} faces · last scan ${ago(s.last_scan)}${s.online ? '' : ' · still searchable, originals unavailable until reconnected'}</div></div><div class="row"><button class="btn" data-a="scan">Rescan</button><button class="btn danger" data-a="rm">Remove</button></div>`;
      d.querySelector('[data-a=scan]').disabled = run || !s.online; d.querySelector('[data-a=scan]').onclick = async () => { await api('/api/index/start', { json: { source_ids: [s.id] } }); poll(); };
      d.querySelector('[data-a=rm]').onclick = async () => { if (!confirm('Remove this folder from FaceMatch? Your photos are not touched.')) return; try { await api('/api/sources/' + s.id, { method: 'DELETE' }); toast('Removed'); setTimeout(poll, 800); } catch (e) { toast(e.message, 4000); } }; l.appendChild(d);
    });
  }
  const sel = $('#modelSel'), mk = ST.models.map(m => m.id + m.available).join(); if (sel.dataset.k !== mk) { sel.dataset.k = mk; sel.innerHTML = ST.models.map(m => `<option value="${m.id}">${esc(m.label)}${m.available ? '' : ' — one-time 175 MB download'}</option>`).join(''); sel.value = ST.settings.model; }
  const j = ST.model_job, busy = ['starting', 'downloading', 'switching'].includes(j.state); $('#modelGo').disabled = busy; $('#modelProg').classList.toggle('hid', j.state !== 'downloading'); $('#modelBar').style.width = (j.progress || 0) + '%';
  $('#modelMsg').textContent = busy || j.state === 'error' || j.state === 'done' ? j.message : `Current model: ${(ST.models.find(m => m.id === ST.settings.model) || {}).label || ''}`;
  $('#bkText').textContent = ST.settings.last_backup ? `Last backup ${ago(ST.settings.last_backup)}` : 'No backup yet';
}
$('#bPause').onclick = async () => { await api('/api/index/' + (ST.index.paused ? 'resume' : 'pause'), { json: {} }); poll(); };
$('#bStop').onclick = async () => { await api('/api/index/stop', { json: {} }); poll(); };
$('#bScan').onclick = async () => { await api('/api/index/start', { json: {} }); poll(); };
const setSetting = async (o, msg) => { try { await api('/api/settings', { json: o }); toast(msg || 'Saved'); } catch (e) { toast(e.message); } };
$('#wk').oninput = e => $('#wkv').textContent = e.target.value; $('#wk').onchange = e => setSetting({ workers: +e.target.value }, 'Applies from the next scan');
$('#detail').onchange = e => setSetting({ detail: e.target.value }, 'Applies to photos scanned from now on');
$('#auto').onchange = e => setSetting({ autoscan_hours: +e.target.value });
$('#optVideo').onchange = e => setSetting({ videos: e.target.checked }, e.target.checked ? 'Videos will be included on the next scan' : 'Saved');
$('#optDisc').onchange = e => setSetting({ auto_discover: e.target.checked });
$('#optBackup').onchange = e => setSetting({ backup: e.target.checked });
$('#optWatch').onchange = e => setSetting({ watch: e.target.checked }, e.target.checked ? 'New photos will be picked up instantly' : 'Folders are no longer watched');
$('#optShare').onchange = e => setSetting({ sharing: e.target.checked });
$('#exclSave').onclick = () => setSetting({ exclude: $('#excl').value }, 'Saved — press “Scan for new photos” to apply');
$('#bBackup').onclick = async () => { try { await api('/api/backup', { json: {} }); toast('Backed up'); poll(); } catch (e) { toast(e.message); } };
$('#modelGo').onclick = async () => {
  const t = $('#modelSel').value; if (t === ST.settings.model) return toast('That model is already in use');
  if (!confirm('Switching the recognition model re-analyses your whole library (this can take hours for big libraries; you can keep searching meanwhile, and saved people are carried over). Continue?')) return;
  try { await api('/api/model', { json: { model: t } }); toast('Started'); settingsInit = false; poll(); } catch (e) { toast(e.message, 4000); }
};

// ======================================================================== admin
let curAdmin = 'ov';
$$('.tabs button[data-at]').forEach(b => b.onclick = () => adminTab(b.dataset.at));
function adminTab(t) {
  curAdmin = t; $$('.tabs button[data-at]').forEach(b => b.classList.toggle('on', b.dataset.at === t));
  ['ov', 'users', 'log'].forEach(k => $('#at-' + k).classList.toggle('hid', k !== t));
  if (!isAdmin()) return; ({ ov: adminOverview, users: adminUsers, log: adminLog })[t]();
}
async function adminOverview() {
  const d = await api('/api/admin/overview'), box = $('#at-ov'), up = d.uptime > 86400 ? Math.floor(d.uptime / 86400) + ' d' : dur(d.uptime);
  const total = d.db_bytes + d.thumb_bytes, used = d.disk_total - d.disk_free;
  box.innerHTML = `<div class="stats">
    <div class="stat"><b>${fmt(d.photos)}</b><span>files indexed (${fmt(d.with_faces)} with faces)</span></div>
    <div class="stat"><b>${fmt(d.faces)}</b><span>faces</span></div>
    <div class="stat"><b>${fmt(d.users)}</b><span>accounts</span></div>
    <div class="stat"><b>${fmt(d.pool_people)}</b><span>people discovered</span></div>
    <div class="stat"><b>${fmt(d.shares)}</b><span>active share links</span></div>
    <div class="stat"><b>${bytes(total)}</b><span>index + thumbnails on disk</span></div></div>
  <div class="panel"><div class="row between wrap"><h2>Photos with faces, by year</h2><button class="btn" id="chT">View as table</button></div><div class="chart" id="chart"></div></div>
  <div class="panel"><h2>System</h2><div class="grid3 small">
    <div><span class="mut">Version</span><br>${esc(d.version)} · up ${up}</div>
    <div><span class="mut">Model</span><br>${esc(d.model === 'arcface' ? 'Accurate (ArcFace)' : 'Standard (SFace)')}</div>
    <div><span class="mut">Disk</span><br>${bytes(d.disk_free)} free of ${bytes(d.disk_total)} (${Math.round(100 * used / d.disk_total)}% used)</div>
    <div><span class="mut">Last backup</span><br>${d.last_backup ? ago(d.last_backup) : 'none yet'}</div></div></div>`;
  drawChart(d.by_year); $('#chT').onclick = e => { const c = $('#chart'); c.dataset.tbl = c.dataset.tbl ? '' : '1'; e.target.textContent = c.dataset.tbl ? 'View as chart' : 'View as table'; drawChart(d.by_year); };
}
function drawChart(by) {
  const c = $('#chart'), ys = Object.keys(by).sort(), vals = ys.map(y => by[y]);
  if (!ys.length) { c.innerHTML = '<p class="mut">No indexed photos with faces yet.</p>'; return; }
  if (c.dataset.tbl) { c.innerHTML = `<table class="t"><thead><tr><th>Year</th><th>Photos</th></tr></thead><tbody>${ys.map(y => `<tr><td>${y}</td><td>${fmt(by[y])}</td></tr>`).join('')}</tbody></table>`; return; }
  const W = Math.max(360, c.clientWidth || 720), H = 240, L = 46, R = 8, T = 10, B = 28, max = Math.max(...vals), step = Math.pow(10, Math.floor(Math.log10(max))), top = Math.ceil(max / step) * step, n = ys.length, bw = (W - L - R) / n, gap = Math.max(bw * .25, bw - 64);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Photos with faces per year">`;
  for (let k = 0; k <= 4; k++) { const v = top * k / 4, y = T + (H - T - B) * (1 - k / 4); s += `<line class="gl" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text x="${L - 6}" y="${y + 4}" text-anchor="end">${fmt(Math.round(v))}</text>`; }
  ys.forEach((y, i) => { const h = (H - T - B) * vals[i] / top, x = L + i * bw + gap / 2, w = bw - gap; s += `<path class="bar" data-y="${y}" data-v="${vals[i]}" d="M${x},${H - B} V${H - B - h + 4} a4,4 0 0 1 4,-4 h${Math.max(0, w - 8)} a4,4 0 0 1 4,4 V${H - B} Z"/>`; if (n <= 16 || i % Math.ceil(n / 12) === 0) s += `<text x="${x + w / 2}" y="${H - 8}" text-anchor="middle">${y}</text>`; });
  c.innerHTML = s + '</svg><div class="tip2 hid" id="tip2"></div>';
  const tip = $('#tip2'); c.querySelectorAll('.bar').forEach(b => { b.onmousemove = e => { const r = c.getBoundingClientRect(); tip.classList.remove('hid'); tip.textContent = `${b.dataset.y}: ${fmt(b.dataset.v)} photos`; tip.style.left = e.clientX - r.left + 'px'; tip.style.top = e.clientY - r.top + 'px'; }; b.onmouseleave = () => tip.classList.add('hid'); });
}

async function adminUsers() {
  const d = await api('/api/admin/users'), box = $('#at-users');
  box.innerHTML = `<div class="row between wrap" style="margin-bottom:12px"><span class="mut small">Members only see the folders you allow, and their People are private.</span><button class="btn p" id="uAdd">+ Add user</button></div>
  <div class="panel" style="padding:4px 10px"><table class="t"><thead><tr><th>User</th><th>Role</th><th>Access</th><th>Last sign-in</th><th></th></tr></thead><tbody>${d.users.map(u => `<tr>
    <td><b>${esc(u.display)}</b><div class="small mut">${esc(u.username)}${u.disabled ? ' · disabled' : ''}${u.pending_invite ? ' · invite pending' : ''}</div></td>
    <td>${u.role === 'admin' ? 'Admin' : 'Member'}</td><td class="small">${u.role === 'admin' || u.all_sources ? 'All folders' : fmt(u.sources.length) + ' folder' + (u.sources.length == 1 ? '' : 's')}${u.can_export ? '' : ' · view only'}</td>
    <td class="small">${u.last_login ? ago(u.last_login) : 'never'}</td><td class="row end"><button class="btn" data-ed="${u.id}">Edit</button></td></tr>`).join('')}</tbody></table></div>`;
  $('#uAdd').onclick = () => userForm(null);
  box.querySelectorAll('[data-ed]').forEach(b => b.onclick = () => userForm(d.users.find(u => u.id == b.dataset.ed)));
}
function linkBox(path, note) { const url = location.origin + (path.startsWith('/') ? path : '/' + path); return `<div class="notice"><div class="small">${note}</div><div class="linkbox" style="margin-top:6px"><input type="text" readonly value="${esc(url)}" id="invU"><button class="btn" id="invC">Copy</button></div></div>`; }
function userForm(u) {
  const srcs = ST ? ST.library.sources : [];
  modal(`<h2>${u ? 'Edit ' + esc(u.username) : 'Add a user'}</h2>
  ${u ? '' : '<label class="f"><span>Username</span><input type="text" id="fU" autocapitalize="none"></label>'}
  <label class="f"><span>Display name</span><input type="text" id="fD" value="${u ? esc(u.display) : ''}"></label>
  <label class="f"><span>Role</span><select id="fR"><option value="member">Member — searches the folders they’re given</option><option value="admin">Admin — manages library, users and settings</option></select></label>
  <label class="chk"><input type="checkbox" id="fX"> Can download, share and export photos</label>
  <div id="accW"><label class="chk"><input type="checkbox" id="fA"> Can see every folder</label>
    <div id="srcW" class="fl" style="padding:6px 10px">${srcs.map(s => `<label class="chk"><input type="checkbox" class="fs" value="${s.id}"> ${esc(s.path)}</label>`).join('') || '<span class="mut small">Add folders in Library first.</span>'}</div></div>
  <div id="uOut"></div><div class="err small" id="uE"></div>
  <div class="row wrap">${u ? `<button class="btn" id="uInv">New password link</button><button class="btn" id="uDis">${u.disabled ? 'Enable account' : 'Disable account'}</button><button class="btn danger" id="uDel">Delete</button>` : ''}<span class="grow"></span><button class="btn" id="c">Close</button><button class="btn p" id="ok">${u ? 'Save' : 'Create user'}</button></div>`, m => {
    const R = m.querySelector('#fR'), A = m.querySelector('#fA'), X = m.querySelector('#fX');
    R.value = u ? u.role : 'member'; X.checked = u ? u.can_export : true; A.checked = u ? u.all_sources : false;
    if (u) m.querySelectorAll('.fs').forEach(c => c.checked = u.sources.includes(+c.value));
    const sync = () => { m.querySelector('#accW').classList.toggle('hid', R.value === 'admin'); m.querySelector('#srcW').classList.toggle('hid', A.checked); }; R.onchange = A.onchange = sync; sync();
    const body = () => ({ display: m.querySelector('#fD').value, role: R.value, can_export: X.checked, all_sources: A.checked || R.value === 'admin', sources: [...m.querySelectorAll('.fs:checked')].map(c => +c.value) });
    const showLink = (inv, note) => { m.querySelector('#uOut').innerHTML = linkBox(inv, note); m.querySelector('#invC').onclick = () => { m.querySelector('#invU').select(); try { navigator.clipboard.writeText(m.querySelector('#invU').value); } catch (e) { document.execCommand('copy'); } toast('Link copied'); }; };
    m.querySelector('#c').onclick = () => { closeModal(); adminUsers(); };
    m.querySelector('#ok').onclick = async () => {
      try {
        if (u) { await api('/api/admin/users/' + u.id, { method: 'PATCH', json: body() }); toast('Saved'); closeModal(); adminUsers(); }
        else { const r = await api('/api/admin/users', { json: { username: m.querySelector('#fU').value, ...body() } }); showLink(r.invite, 'Send this one-time link to them. It lets them choose their own password (valid for 7 days).'); m.querySelector('#ok').classList.add('hid'); }
      } catch (e) { m.querySelector('#uE').textContent = e.message; }
    };
    if (u) {
      m.querySelector('#uInv').onclick = async () => { try { const r = await api(`/api/admin/users/${u.id}/invite`, { json: {} }); showLink(r.invite, 'One-time link to set a new password (all current sessions stay signed in until they use it).'); } catch (e) { m.querySelector('#uE').textContent = e.message; } };
      m.querySelector('#uDis').onclick = async () => { try { await api('/api/admin/users/' + u.id, { method: 'PATCH', json: { disabled: !u.disabled } }); toast(u.disabled ? 'Account enabled' : 'Account disabled'); closeModal(); adminUsers(); } catch (e) { m.querySelector('#uE').textContent = e.message; } };
      m.querySelector('#uDel').onclick = async () => { if (!confirm(`Delete ${u.username}? Their saved people and share links are removed. Photos are not touched.`)) return; try { await api('/api/admin/users/' + u.id, { method: 'DELETE' }); toast('Deleted'); closeModal(); adminUsers(); } catch (e) { m.querySelector('#uE').textContent = e.message; } };
    }
  });
}
const EV = { login: 'Signed in', logout: 'Signed out', setup: 'Admin created', invite_redeemed: 'Joined via invite', password_changed: 'Changed password', password_reset_link: 'Reset link created', user_created: 'User added', user_updated: 'User edited', user_deleted: 'User deleted', source_added: 'Folder added', source_removed: 'Folder removed', settings_changed: 'Settings changed', export_zip: 'Downloaded .zip', export_folder: 'Saved to folder', share_created: 'Share link created', share_revoked: 'Share link revoked', login_failed: 'Failed sign-in' };
async function adminLog() {
  const d = await api('/api/admin/audit?limit=100'), box = $('#at-log');
  box.innerHTML = '<div class="panel" style="padding:6px 14px">' + (d.events.length ? d.events.map(e => `<div class="ev"><span class="mut">${dtstr(e.ts)}</span><b>${esc(e.username || '—')}</b><span>${esc(EV[e.event] || e.event)}</span><span class="mut">${esc(e.detail || '')}${e.ip ? ' · ' + esc(e.ip) : ''}</span></div>`).join('') : '<p class="mut">Nothing yet.</p>') + '</div>';
}

// ======================================================================== start
addEventListener('hashchange', () => { if (location.hash.startsWith('#invite/')) boot(); });
boot().catch(() => showAuth('login', 'The server is not reachable.'));
try { 'serviceWorker' in navigator && navigator.serviceWorker.register('/sw.js'); } catch (e) { }
