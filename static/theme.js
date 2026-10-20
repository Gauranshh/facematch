/* Applies the saved theme before first paint (no flash). Shared by the app and the public share page. */
(function () {
  var ACC = ['cyanotype', 'fern', 'plum', 'marigold', 'rose', 'graphite'];
  var mq = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
  function load() { try { return JSON.parse(localStorage.getItem('fm_theme')) || {}; } catch (e) { return {}; } }
  function apply(t) {
    t = t || load(); var mode = t.mode || 'auto', acc = ACC.indexOf(t.acc) > -1 ? t.acc : 'cyanotype';
    var dark = mode === 'dark' || (mode === 'auto' && mq && mq.matches);
    var r = document.documentElement; r.dataset.mode = dark ? 'dark' : 'light'; r.dataset.acc = acc;
    var m = document.querySelector('meta[name=theme-color]'); if (m) m.content = dark ? '#0a1620' : '#0b2238';
    return { mode: mode, acc: acc };
  }
  function set(t) { try { localStorage.setItem('fm_theme', JSON.stringify(t)); } catch (e) { } return apply(t); }
  if (mq && mq.addEventListener) mq.addEventListener('change', function () { apply(); });
  window.FMTheme = { ACC: ACC, apply: apply, set: set, get: function () { var t = load(); return { mode: t.mode || 'auto', acc: ACC.indexOf(t.acc) > -1 ? t.acc : 'cyanotype' }; } };
  apply();
})();
