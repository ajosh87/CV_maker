/* nerdbar: a live, terminal-style view of what the app is doing. Reads /api/events (this computer only). Closed, it's
   a pill in the corner showing what's running; open (the pill, or the ` key), it splits the right half: the page's
   viewer above, the nerdbar below (or the whole right half on a page without a viewer). */
(function () {
  var panel = document.getElementById('nb-panel'), toggle = document.getElementById('nb-toggle');
  if (!panel || !toggle) return;
  var log = panel.querySelector('.nb-log'), state = panel.querySelector('.nb-state'), tasksBox = panel.querySelector('[data-stat=tasks]');
  var followBtn = panel.querySelector('[data-act=follow]'), count = toggle.querySelector('.nb-count'), now = toggle.querySelector('.nb-now');
  var lastSeq = 0, follow = true, open = false, timer = null, serverOffset = 0, active = [], recent = [], rows = {};

  function remember(key, value) { try { localStorage.setItem('nerdbar.' + key, value); } catch (e) { /* private window */ } }
  function recall(key) { try { return localStorage.getItem('nerdbar.' + key); } catch (e) { return null; } }

  function fmt(n) { return Math.round(n).toLocaleString(); }
  function short(n) { return n >= 10000 ? (n / 1000).toFixed(1) + 'k' : fmt(n); }
  function clock(t) { var d = new Date(t * 1000); return d.toTimeString().slice(0, 8); }
  function secs(s) { return s < 60 ? s.toFixed(1) + 's' : Math.floor(s / 60) + 'm ' + String(Math.round(s % 60)).padStart(2, '0') + 's'; }

  function line(e) {
    var row = document.createElement('div');
    row.className = 'nb-line k-' + e.kind;
    var t = document.createElement('span'); t.className = 'nb-t'; t.textContent = clock(e.t) + ' ';
    var tag = document.createElement('span'); tag.className = 'nb-tag'; tag.textContent = e.tag ? '[' + e.tag + '] ' : '';
    var msg = document.createElement('span'); msg.className = 'nb-msg'; msg.textContent = e.text;
    row.appendChild(t); row.appendChild(tag); row.appendChild(msg);
    return row;
  }

  function setStat(name, text) {
    var el = panel.querySelector('[data-stat=' + name + ']');
    if (el.textContent !== text) el.textContent = text;
  }

  function render(data) {
    var stuck = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    if (data.events.length) {
      var frag = document.createDocumentFragment();
      data.events.forEach(function (e) { frag.appendChild(line(e)); lastSeq = Math.max(lastSeq, e.seq); });
      log.appendChild(frag);
      while (log.childNodes.length > 1500) log.removeChild(log.firstChild);
      if (follow && stuck) log.scrollTop = log.scrollHeight;
    }
    var s = data.stats, totals = s.session;
    serverOffset = s.now - Date.now() / 1000;
    active = s.active;
    recent = s.recent;
    var approx = totals.estimated ? '~' : '';
    setStat('running', active.length ? active.length + ' task' + (active.length > 1 ? 's' : '') : 'idle');
    setStat('calls', fmt(totals.llm_calls) + (totals.llm_calls ? ' · ' + secs(totals.llm_seconds) : ''));
    setStat('tokens', approx + short(totals.tokens_in) + ' · ' + approx + short(totals.tokens_out));
    setStat('sent', (totals.chars_sent / 1024).toFixed(1) + ' KB' + (totals.hidden ? ' · ' + fmt(totals.hidden) + ' hidden' : ''));
    toggle.classList.toggle('busy', active.length > 0);
    count.textContent = active.length > 1 ? String(active.length) : '';
    var doing = active.length ? active[0].label : '';
    if (now.textContent !== doing) { now.textContent = doing; toggle.title = doing ? doing + ' (open the nerdbar: `)' : 'nerdbar: see what the app is doing (press `)'; }
    if (state.textContent !== (active.length ? 'working' : 'idle')) state.textContent = active.length ? 'working' : 'idle';
    drawTasks();
  }

  // One line per task, always three lines tall while anything ran recently, updated in place: no jumping.
  function drawTasks() {
    var now = Date.now() / 1000 + serverOffset;
    var shown = active.concat(recent.slice(0, Math.max(0, 3 - active.length))).slice(0, 3);
    var keep = {};
    shown.forEach(function (t, i) {
      var running = !t.ended;
      var row = rows[t.id];
      if (!row) {
        row = document.createElement('div');
        row.innerHTML = '<span class="nb-task-mark"></span><span class="nb-task-label"></span><span class="nb-task-meta"></span>';
        rows[t.id] = row;
      }
      row.className = 'nb-task' + (running ? ' running' : '') + (t.status === 'failed' ? ' failed' : '');
      row.children[0].textContent = running ? '▶' : t.status === 'failed' ? '✗' : '■';
      if (row.children[1].textContent !== t.label) { row.children[1].textContent = t.label; row.title = t.label; }
      var tokens = t.tokens_in + t.tokens_out;
      var meta = secs(Math.max(0, running ? now - t.started : t.elapsed)) +
        (t.llm_calls ? ' · ' + t.llm_calls + ' call' + (t.llm_calls > 1 ? 's' : '') + ' · ' + (t.estimated ? '~' : '') + short(tokens) + ' tok' : '');
      if (row.children[2].textContent !== meta) row.children[2].textContent = meta;
      if (tasksBox.children[i] !== row) tasksBox.insertBefore(row, tasksBox.children[i] || null);
      keep[t.id] = true;
    });
    Object.keys(rows).forEach(function (id) { if (!keep[id]) { rows[id].remove(); delete rows[id]; } });
    tasksBox.classList.toggle('filled', shown.length > 0);
  }

  function poll() {
    clearTimeout(timer);
    if (document.hidden) { timer = setTimeout(poll, 4000); return; }
    fetch('/api/events?after=' + lastSeq, {headers: {'X-Requested-With': 'XMLHttpRequest'}})
      .then(function (r) { return r.json(); })
      .then(function (data) { render(data); })
      .catch(function () { /* the server may be restarting */ })
      .finally(function () { timer = setTimeout(poll, open || active.length ? 1000 : 4000); });
  }

  function setOpen(value) {
    open = value;
    document.documentElement.classList.toggle('nb-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    remember('open', open ? '1' : '0');
    if (window.CVLayout) window.CVLayout.refresh();
    if (open) { log.scrollTop = log.scrollHeight; poll(); }
    else if (document.activeElement && panel.contains(document.activeElement)) toggle.focus({preventScroll: true});
  }

  toggle.addEventListener('click', function () { setOpen(!open); });
  panel.addEventListener('click', function (e) {
    var act = e.target.closest('[data-act]');
    if (!act) return;
    if (act.dataset.act === 'close') setOpen(false);
    if (act.dataset.act === 'clear') log.textContent = '';
    if (act.dataset.act === 'follow') {
      follow = !follow;
      followBtn.setAttribute('aria-pressed', String(follow));
      remember('follow', follow ? '1' : '0');
      if (follow) log.scrollTop = log.scrollHeight;
    }
  });
  document.addEventListener('keydown', function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
    if (e.key === '`' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); setOpen(!open); }
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  setInterval(function () { if (open && active.length) drawTasks(); }, 500);  // live timers, same rows

  follow = recall('follow') !== '0';
  followBtn.setAttribute('aria-pressed', String(follow));
  open = document.documentElement.classList.contains('nb-open');  // set before the first paint (base.html)
  toggle.setAttribute('aria-expanded', String(open));
  poll();
})();
