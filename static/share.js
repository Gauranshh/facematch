'use strict';
(function () {
  var $ = function (s) { return document.querySelector(s); }, tok = location.pathname.split('/')[2], items = [], idx = -1, canDl = false;
  var esc = function (s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  fetch('/api/public/share/' + encodeURIComponent(tok)).then(function (r) { if (!r.ok) throw 0; return r.json(); }).then(function (d) {
    document.title = d.title + ' — FaceMatch'; $('#ttl').textContent = d.title; items = d.items; canDl = d.allow_download;
    $('#info').textContent = items.length + ' photo' + (items.length === 1 ? '' : 's') + ' · link expires ' + new Date(d.expires * 1000).toLocaleDateString(undefined, { dateStyle: 'medium' });
    if (d.allow_download) { var a = $('#dl'); a.href = '/s/' + tok + '/zip'; a.classList.remove('hid'); }
    var g = $('#grid'); items.forEach(function (it, i) {
      var t = document.createElement('div'); t.className = 'tile vf'; t.tabIndex = 0;
      t.innerHTML = '<span class="vf-b"></span>' + (it.thumb ? '<img loading="lazy" alt="" src="/s/' + tok + '/thumb/' + it.id + '">' : '') + (it.vid ? '<span class="vb">▶</span>' : '');
      t.onclick = function () { open(i); }; t.onkeydown = function (e) { if (e.key === 'Enter') open(i); }; g.appendChild(t);
    });
  }).catch(function () { $('#ttl').textContent = 'This link isn’t available'; $('#msg').textContent = 'It may have expired or been turned off by whoever shared it. Ask them for a new one.'; });
  function open(i) {
    idx = i; var it = items[i], m = $('#lbMedia'); $('#lb').classList.remove('hid'); m.innerHTML = '';
    var url = '/s/' + tok + '/photo/' + it.id, el;
    if (it.vid) { el = document.createElement('video'); el.controls = true; el.autoplay = true; } else { el = new Image(); el.alt = ''; }
    el.src = url; m.appendChild(el);
    $('#lbCap').innerHTML = esc(it.name) + (canDl ? ' · <a href="' + url + '" download>download</a>' : '');
  }
  function go(d) { var n = idx + d; if (n >= 0 && n < items.length) open(n); }
  function close() { $('#lb').classList.add('hid'); $('#lbMedia').innerHTML = ''; }
  $('#lbX').onclick = close; $('#lbL').onclick = function () { go(-1); }; $('#lbR').onclick = function () { go(1); };
  $('#lb').onclick = function (e) { if (e.target.id === 'lb') close(); };
  addEventListener('keydown', function (e) { if ($('#lb').classList.contains('hid')) return; if (e.key === 'Escape') close(); if (e.key === 'ArrowLeft') go(-1); if (e.key === 'ArrowRight') go(1); });
})();
