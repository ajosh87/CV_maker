/* The two halves: the page on the left; on the right, what helps with it (a preview, the posting, the live
   application, a status board) and, below that, the nerdbar when it's open. Which parts show is set by the page and
   by the `nb-open` class (put on <html> before the first paint); this only handles the dividers. Both can be dragged
   or moved with the arrow keys, double-click resets, and the sizes are remembered on this computer. On narrow
   screens the halves stack instead. */
(function () {
  var ws = document.getElementById('workspace');
  if (!ws) return;
  var root = document.documentElement, side = document.getElementById('pane-side');
  var gv = document.getElementById('gutter-v'), gh = document.getElementById('gutter-h');

  function store(key, value) { try { localStorage.setItem('layout.' + key, value); } catch (e) { /* private window */ } }
  function forget(key) { try { localStorage.removeItem('layout.' + key); } catch (e) { /* private window */ } }

  function clampSide(px) {
    var r = ws.getBoundingClientRect();
    return Math.round(Math.min(Math.max(px, 300), r.width - 340)) + 'px';
  }
  function clampNb(px) {
    var r = side.getBoundingClientRect();
    return Math.round(Math.min(Math.max(px, 110), r.height - 140)) + 'px';
  }
  function settle() { window.dispatchEvent(new Event('resize')); }

  function resizable(gutter, compute, current, set, key, cursor) {
    gutter.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      gutter.setPointerCapture(e.pointerId);
      document.body.classList.add('resizing', cursor);
      var move = function (ev) { set(compute(ev)); };
      var up = function () {
        gutter.removeEventListener('pointermove', move);
        gutter.removeEventListener('pointerup', up);
        gutter.removeEventListener('pointercancel', up);
        document.body.classList.remove('resizing', cursor);
        store(key, root.style.getPropertyValue(key === 'side' ? '--side' : '--nb'));
        settle();
      };
      gutter.addEventListener('pointermove', move);
      gutter.addEventListener('pointerup', up);
      gutter.addEventListener('pointercancel', up);
    });
    gutter.addEventListener('keydown', function (e) {
      var step = e.shiftKey ? 80 : 24;
      var delta = {ArrowLeft: step, ArrowRight: -step, ArrowUp: step, ArrowDown: -step}[e.key];
      if (delta === undefined) return;
      e.preventDefault();
      set(current() + delta);
      store(key, root.style.getPropertyValue(key === 'side' ? '--side' : '--nb'));
      settle();
    });
    gutter.addEventListener('dblclick', function () {
      root.style.removeProperty(key === 'side' ? '--side' : '--nb');
      forget(key);
      settle();
    });
  }

  resizable(gv,
    function (ev) { return ws.getBoundingClientRect().right - ev.clientX; },
    function () { return side.getBoundingClientRect().width; },
    function (px) { root.style.setProperty('--side', clampSide(px)); },
    'side', 'resizing-x');
  resizable(gh,
    function (ev) { return side.getBoundingClientRect().bottom - ev.clientY; },
    function () { return document.getElementById('nb-panel').getBoundingClientRect().height; },
    function (px) { root.style.setProperty('--nb', clampNb(px)); },
    'nerdbar', 'resizing-y');

  // Only the halves scroll on wide screens. Should anything ever stretch the window (and a link or a focused field
  // scroll it), put it back, so the header can't end up out of reach.
  var wide = window.matchMedia('(min-width: 1000px)');
  window.addEventListener('scroll', function () {
    if (wide.matches && (window.scrollY || window.scrollX)) window.scrollTo(0, 0);
  }, {passive: true});

  window.CVLayout = {refresh: settle, hasDock: function () { return ws.dataset.dock === 'yes'; }};
})();
