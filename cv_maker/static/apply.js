/* The live application view: watch the assistant, take over, answer its questions, approve the submit.
   Everything on this page that comes from a job site is inserted as text, never as HTML. */
(function () {
  var root = document.getElementById('ap');
  if (!root) return;
  var id = root.dataset.id, W = +root.dataset.width, H = +root.dataset.height;
  var api = '/api/applications/' + id;
  var $ = function (sel) { return document.getElementById(sel); };
  var live = $('ap-live'), img = $('ap-frame'), target = $('ap-target'), badge = $('ap-badge'), needs = $('ap-needs');
  var state = {}, frameSeq = -2, needsKey = '', filledCount = -1, timer = null, hoverBox = null;


  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { if (k === 'class') node.className = attrs[k]; else node.setAttribute(k, attrs[k]); });
    if (text != null) node.textContent = text;
    return node;
  }
  function send(body) {
    return fetch(api + '/control', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.message) flash(d.message); poll(); return d; })
      .catch(function () { flash('The app did not answer. Is it still running?'); });
  }
  function flash(message) {
    var f = el('div', {class: 'flash error', role: 'alert'}, message);
    root.parentNode.insertBefore(f, root);
    setTimeout(function () { f.remove(); }, 6000);
  }

  // ---- the live view ----

  function refreshFrame(seq) {
    if (seq === frameSeq) return;
    frameSeq = seq;
    var next = new Image();
    next.onload = function () { img.src = next.src; };
    next.src = api + '/frame?seq=' + seq + '&t=' + Date.now();
  }
  function drawTarget(box) {
    if (!box || !img.clientWidth) { target.hidden = true; return; }
    var sx = img.clientWidth / W, sy = img.clientHeight / H;
    target.hidden = false;
    target.style.left = (box[0] * sx - 3) + 'px'; target.style.top = (box[1] * sy - 3) + 'px';
    target.style.width = (box[2] * sx + 6) + 'px'; target.style.height = (box[3] * sy + 6) + 'px';
  }
  function inControl() { return state.live && state.mode === 'paused'; }
  function point(e) {
    var r = img.getBoundingClientRect();
    return {x: Math.round((e.clientX - r.left) * W / r.width), y: Math.round((e.clientY - r.top) * H / r.height)};
  }
  live.addEventListener('click', function (e) {
    if (!inControl()) return;
    live.focus();
    var p = point(e);
    send({action: 'input', type: 'click', x: p.x, y: p.y});
  });
  var wheelDy = 0, wheelTimer = null;
  live.addEventListener('wheel', function (e) {
    if (!inControl()) return;
    e.preventDefault();
    wheelDy += e.deltaY;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(function () { send({action: 'input', type: 'scroll', dy: Math.round(wheelDy)}); wheelDy = 0; }, 120);
  }, {passive: false});
  var typed = '', typeTimer = null;
  var KEYS = {Enter: 'Enter', Tab: 'Tab', Backspace: 'Backspace', Delete: 'Delete', Escape: 'Escape', ArrowUp: 'ArrowUp',
              ArrowDown: 'ArrowDown', ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight', Home: 'Home', End: 'End',
              PageUp: 'PageUp', PageDown: 'PageDown'};
  function flushTyped() { if (typed) { send({action: 'input', type: 'type', text: typed}); typed = ''; } }
  live.addEventListener('keydown', function (e) {
    if (!inControl() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key.length === 1) {
      e.preventDefault();
      typed += e.key;
      clearTimeout(typeTimer);
      typeTimer = setTimeout(flushTyped, 150);
    } else if (KEYS[e.key]) {
      e.preventDefault();
      flushTyped();
      send({action: 'input', type: 'key', key: KEYS[e.key]});
    }
  });
  live.addEventListener('paste', function (e) {
    if (!inControl()) return;
    e.preventDefault();
    send({action: 'input', type: 'type', text: (e.clipboardData || window.clipboardData).getData('text')});
  });
  $('ap-send').addEventListener('click', function () {
    var box = $('ap-text');
    if (box.value) { send({action: 'input', type: 'type', text: box.value}); box.value = ''; }
  });
  $('ap-typebar').addEventListener('click', function (e) {
    var key = e.target.dataset && e.target.dataset.key;
    if (key) send({action: 'input', type: 'key', key: key});
  });
  // The browser bar works whoever is driving: using it means you take over.
  document.querySelectorAll('#ap-browser [data-nav]').forEach(function (b) {
    b.addEventListener('click', function () { send({action: 'nav', type: b.dataset.nav}); });
  });
  $('ap-go').addEventListener('submit', function (e) {
    e.preventDefault();
    var url = $('ap-url').value.trim();
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
    send({action: 'nav', type: 'goto', url: url});
    $('ap-url').blur();
  });

  // ---- controls ----

  $('ap-pause').addEventListener('click', function () { send({action: 'pause'}); });
  $('ap-resume').addEventListener('click', function () { send({action: 'resume'}); });
  $('ap-start').addEventListener('click', function () { send({action: 'start'}); });
  $('ap-stop').addEventListener('click', function () { send({action: 'stop'}); });

  // ---- what the assistant needs from you ----

  function button(text, cls, body) {
    var b = el('button', {type: 'button', class: cls || ''}, text);
    b.addEventListener('click', function () { b.disabled = true; send(body); });
    return b;
  }
  function renderNeeds(w) {
    var key = JSON.stringify([state.status, state.live, w, state.new_password]);
    if (key === needsKey) return;
    needsKey = key;
    needs.textContent = '';
    if (!w || !w.kind) {
      if (state.status === 'submitted') {
        needs.appendChild(el('p', {class: 'ok-text'}, '✓ Application submitted' + (state.submitted_by === 'you' ? ' (marked by you).' : '.')));
        var back = el('a', {href: root.dataset.job, class: 'btn secondary mt-md'}, 'Back to the job');
        needs.appendChild(back);
      } else if (!state.live && (state.status === 'stopped' || state.status === 'failed')) {
        if (state.error) needs.appendChild(el('p', {class: 'error-text', role: 'alert'}, state.error));
        needs.appendChild(el('p', {class: 'small muted mt-xs'}, 'Resume to pick up from the last page. If you already finished it yourself, mark it as submitted.'));
        var row = el('div', {class: 'row mt-md'});
        row.appendChild(button('Mark as submitted', 'secondary', {action: 'mark_submitted'}));
        needs.appendChild(row);
      }
      needs.hidden = !needs.childNodes.length;
      return;
    }
    needs.hidden = false;
    needs.appendChild(el('p', {}, w.text));
    var actions = el('div', {class: 'row mt-md'});
    if (w.kind === 'question') {
      needs.appendChild(questionForm(w.questions || []));
      return;
    }
    if (w.kind === 'documents') {
      needs.appendChild(el('p', {class: 'small muted mt-xs'}, 'It carries on by itself once the CV is ready. Open the nerdbar to watch the writing.'));
      return;
    }
    if (w.kind === 'letter') {
      actions.appendChild(button('Write one now', '', {action: 'write_letter'}));
      actions.appendChild(button('Resume', 'secondary', {action: 'resume'}));
      actions.appendChild(el('span', {class: 'small muted'}, 'Resume after attaching your own in the view.'));
      needs.appendChild(actions);
      return;
    }
    if (w.kind === 'account') {
      if (w.creating) actions.appendChild(button('Fill in a new account for me', '', {action: 'account_fill'}));
      if (w.saved) actions.appendChild(button('Sign me in with my saved password', w.creating ? 'secondary' : '', {action: 'account_fill', saved: true}));
      actions.appendChild(el('span', {class: 'small muted'}, 'or do it yourself in the view, then press Resume.'));
      needs.appendChild(actions);
      needs.appendChild(el('p', {class: 'small faint mt-sm'}, w.keychain ? 'A new password is generated and saved to ' + w.keychain + ', never to the app or the LLM.'
        : 'No system password store was found, so a new password will be shown to you once to save yourself.'));
      return;
    }
    if (w.kind === 'account_check' && state.new_password) {
      var pw = el('div', {class: 'row mt-sm'});
      var code = el('code', {class: 'break'}, state.new_password);
      var copy = el('button', {type: 'button', class: 'secondary sm'}, 'Copy');
      copy.addEventListener('click', function () { navigator.clipboard.writeText(state.new_password); copy.textContent = 'Copied'; });
      pw.appendChild(el('span', {class: 'small warn-text'}, 'Save this password now; the app doesn\'t keep it:'));
      pw.appendChild(code); pw.appendChild(copy);
      needs.appendChild(pw);
    }
    if (w.kind === 'site') {
      if (w.allow) actions.appendChild(button('Allow ' + w.site, '', {action: 'allow_site'}));
      actions.appendChild(button('Stop here', 'ghost', {action: 'stop'}));
    } else if (w.kind === 'submit') {
      needs.appendChild(summary());
      actions.appendChild(button(w.button || 'Submit application', '', {action: 'approve_submit'}));
      actions.appendChild(el('span', {class: 'small muted'}, 'Not ready? You\'re in control: check or change anything in the view first.'));
    } else {
      actions.appendChild(button('Resume', '', {action: 'resume'}));
      if (w.kind === 'check') actions.appendChild(button('It went through: mark as submitted', 'secondary', {action: 'mark_submitted'}));
    }
    needs.appendChild(actions);
  }
  // Each question is headed by the form's own words for the field; the field is outlined in the view while you're on it.
  function questionForm(questions) {
    var form = el('form', {class: 'mt-md'});
    questions.forEach(function (q, i) {
      var box = el('fieldset', {class: 'question panel quiet'});
      var head = el('legend', {class: 'q-label'}, q.label || 'Question');
      if (q.required) head.appendChild(el('span', {class: 'tag must'}, 'required'));
      box.appendChild(head);
      box.appendChild(el('p', {class: 'small muted'}, q.question));
      if (q.hint) box.appendChild(el('p', {class: 'small faint'}, 'The form says: ' + q.hint));
      var show = function () { hoverBox = q.box || null; drawTarget(hoverBox); };
      box.addEventListener('mouseenter', show);
      box.addEventListener('focusin', show);
      box.addEventListener('mouseleave', function () { hoverBox = null; drawTarget(null); });
      var input;
      if (q.kind === 'secret') {
        box.classList.add('secret');
        form.appendChild(box);
        return;  // typed by you in the view: there is nothing to answer here
      }
      if (q.multiple && q.options && q.options.length) {
        input = el('div', {class: 'choice-list', role: 'group'});
        input.setAttribute('aria-label', q.label || q.question);
        q.options.forEach(function (o) {
          var row = el('label', {class: 'check small'});
          var tick = el('input', {type: 'checkbox', name: 'q' + i, value: o});
          row.appendChild(tick); row.appendChild(document.createTextNode(' ' + o));
          input.appendChild(row);
        });
        box.appendChild(input);
      } else if (q.kind === 'consent' || q.kind === 'checkbox' || q.kind === 'upload') {
        input = el('select', {name: 'q' + i});
        (q.kind === 'upload' ? [['', 'Choose…'], ['yes', 'Yes, upload my tailored CV'], ['no', 'No, leave it empty']]
          : [['', 'Choose…'], ['yes', 'Yes, tick it'], ['no', 'No, leave it unticked']])
          .forEach(function (o) { input.appendChild(el('option', {value: o[0]}, o[1])); });
      } else if (q.options && q.options.length) {
        input = el('select', {name: 'q' + i});
        input.appendChild(el('option', {value: ''}, 'Choose…'));
        q.options.forEach(function (o) { input.appendChild(el('option', {value: o}, o)); });
      } else if (q.kind === 'date' || q.kind === 'month') {
        input = el('input', {type: q.kind, name: 'q' + i});
      } else if (q.kind === 'number') {
        input = el('input', {type: 'number', name: 'q' + i, step: 'any'});
      } else if (q.kind === 'textarea') {
        input = el('textarea', {name: 'q' + i, rows: '3'});
      } else {
        input = el('input', {type: 'text', name: 'q' + i});
      }
      if (!input.parentNode) {
        input.classList.add('mt-sm');
        input.setAttribute('aria-label', q.label || q.question);
        box.appendChild(input);
      }
      if (q.kind !== 'consent' && q.kind !== 'upload') {
        var save = el('label', {class: 'check small mt-sm'});
        var tick = el('input', {type: 'checkbox', name: 's' + i}); tick.checked = true;
        save.appendChild(tick); save.appendChild(document.createTextNode(' Save this answer for future applications'));
        box.appendChild(save);
      }
      form.appendChild(box);
    });
    var go = el('button', {type: 'submit', class: 'mt-md'}, 'Answer and continue');
    form.appendChild(go);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var answers = [];
      questions.forEach(function (q, i) {
        if (q.kind === 'secret') return;
        var save = form.elements['s' + i], value;
        if (q.multiple && q.options && q.options.length) {
          value = Array.prototype.filter.call(form.querySelectorAll('input[name="q' + i + '"]'), function (t) { return t.checked; })
            .map(function (t) { return t.value; }).join('; ');
        } else {
          value = form.elements['q' + i].value;
        }
        answers.push({id: q.id, value: value, save: save ? save.checked : false, required: !!q.required});
      });
      if (answers.some(function (a) { return a.required && !a.value; })) { flash('Answer the required questions first, or take over in the view.'); return; }
      go.disabled = true;
      send({action: 'answer', answers: answers});
    });
    return form;
  }
  function summary() {
    var wrap = el('div', {class: 'table-wrap mt-sm'});
    var table = el('table', {class: 'table small'});
    (state.filled || []).forEach(function (f) {
      var tr = el('tr');
      tr.appendChild(el('td', {class: 'muted'}, f.label));
      tr.appendChild(el('td', {class: 'break'}, f.value));
      table.appendChild(tr);
    });
    wrap.appendChild(table);
    return wrap;
  }
  function renderFilled(filled) {
    if (filled.length === filledCount) return;
    filledCount = filled.length;
    $('ap-filled-count').textContent = filled.length || '';
    var body = $('ap-filled');
    body.textContent = '';
    filled.forEach(function (f) {
      var tr = el('tr');
      tr.appendChild(el('td', {class: 'faint'}, f.page));
      tr.appendChild(el('td', {}, f.label));
      tr.appendChild(el('td', {class: 'break'}, f.value));
      tr.appendChild(el('td', {class: 'faint nowrap'}, {rule: 'saved details', llm: 'LLM', you: 'you'}[f.source] || f.source));
      body.appendChild(tr);
    });
  }

  // ---- progress: opening → filling in → your review → submitted ----

  var STEPS = ['Opening the application', 'Filling in', 'Your review', 'Submitted'];
  var STATE_TEXT = {done: 'done', working: 'in progress', you: 'waiting for you', failed: 'failed', todo: 'not yet'};
  function renderSteps(s) {
    var list = $('ap-steps'), detail = $('ap-step-detail');
    if (!list) return;
    var waiting = (s.waiting || {}).kind || '', filled = s.filled || [];
    var at = s.status === 'submitted' ? STEPS.length : waiting === 'submit' ? 2 : (s.status === 'starting' && !filled.length) ? 0 : 1;
    var here = s.status === 'failed' ? 'failed' : (s.status === 'needs_you' || s.status === 'stopped') ? 'you' : s.live ? 'working' : 'todo';
    var key = at + here;
    if (list.dataset.key !== key) {
      list.dataset.key = key;
      list.textContent = '';
      STEPS.forEach(function (label, i) {
        var state = i < at ? 'done' : i === at ? here : 'todo';
        var li = el('li', {class: 'st-' + state});
        if (i === at) li.setAttribute('aria-current', 'step');
        li.appendChild(el('span', {class: 'dot', 'aria-hidden': 'true'}));
        var text = el('span', {class: 'st-label'}, label);
        text.appendChild(el('span', {class: 'sr-only'}, ': ' + STATE_TEXT[state]));
        li.appendChild(text);
        list.appendChild(li);
      });
    }
    var pages = {};
    filled.forEach(function (f) { pages[f.page || '?'] = 1; });
    var n = Object.keys(pages).length;
    var counts = filled.length + ' field' + (filled.length === 1 ? '' : 's') + ' filled' + (n ? ' on ' + n + ' page' + (n === 1 ? '' : 's') : '');
    detail.textContent = at >= STEPS.length ? 'Submitted · ' + counts
      : 'Step ' + (at + 1) + ' of ' + STEPS.length + ' · ' + counts;
  }

  // ---- state ----

  function render() {
    var s = state, status = s.status;
    if (document.activeElement !== $('ap-url') && s.url) $('ap-url').value = s.url;  // until it moves, where it starts
    var mode = !s.live ? (status === 'submitted' ? 'Submitted' : 'Not running') :
      s.mode === 'paused' ? 'You\'re driving' : s.mode === 'documents' ? 'Waiting for your CV' : 'The assistant is driving';
    $('ap-mode').textContent = mode;
    $('ap-mode').className = 'drive-mode ' + (!s.live ? 'off' : s.mode === 'paused' ? 'you' : 'agent');
    document.querySelectorAll('#ap-browser [data-nav]').forEach(function (b) { b.disabled = !s.live; });
    $('ap-go').querySelector('button').disabled = !s.live;
    $('ap-pause').hidden = !(s.live && (s.mode === 'agent' || s.mode === 'documents'));
    $('ap-resume').hidden = !(s.live && s.mode === 'paused');
    $('ap-stop').hidden = !s.live;
    $('ap-start').hidden = s.live || !(status === 'stopped' || status === 'failed');
    $('ap-typebar').hidden = !inControl();
    live.classList.toggle('interactive', inControl());
    badge.textContent = s.live && s.mode === 'paused' ? 'Click and type straight into the page' : '';  // who's driving: the bar above
    badge.hidden = !badge.textContent;
    if (s.frame >= 0 || s.status !== 'starting') refreshFrame(s.frame);
    drawTarget(s.mode === 'agent' ? s.target : hoverBox);
    renderNeeds(s.waiting);
    renderFilled(s.filled || []);
    renderSteps(s);
  }
  function poll() {
    clearTimeout(timer);
    fetch(api, {headers: {'X-Requested-With': 'XMLHttpRequest'}})
      .then(function (r) { return r.json(); })
      .then(function (data) { state = data; render(); })
      .catch(function () { badge.textContent = 'Reconnecting…'; badge.hidden = false; })
      .finally(function () { timer = setTimeout(poll, state.live ? 600 : 3000); });
  }
  window.addEventListener('resize', function () { drawTarget(state.mode === 'agent' ? state.target : hoverBox); });
  poll();
})();
