/* Shared page behaviour for CV Tailor. */
(function () {
  // Ask before destructive actions: put data-confirm="…" on a <form> or on a submit <button>.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) e.preventDefault();
  }, true);
  document.addEventListener('click', function (e) {
    var button = e.target.closest && e.target.closest('button[data-confirm]');
    if (button && !confirm(button.dataset.confirm)) e.preventDefault();
  }, true);

  // A CV preview's "show my original wording" switch: the preview right after it.
  document.addEventListener('change', function (e) {
    if (!e.target.classList.contains('orig-toggle')) return;
    var paper = e.target.closest('label').nextElementSibling;
    if (paper && paper.classList.contains('paper')) paper.classList.toggle('show-orig', e.target.checked);
  });

  // ---- parts of a page that update in place ----
  // Elements with an id and data-part are the parts of a page. CV.swap(doc) replaces each part that differs in `doc`
  // (a fresh copy of the same page), keeping what you've typed but not sent, where your cursor is, and which sections
  // you've opened, so a result arriving while you work never wipes or moves anything.
  function fieldKey(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    if (!el.name) return '';
    var key = el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
    return el.type === 'radio' || el.type === 'checkbox' ? key + '[value="' + CSS.escape(el.value) + '"]' : key;
  }
  function changedByYou(el) {
    if (el.type === 'checkbox' || el.type === 'radio') return el.checked !== el.defaultChecked;
    if (el.tagName === 'SELECT') return Array.prototype.some.call(el.options, function (o) { return o.selected !== o.defaultSelected; });
    return el.value !== el.defaultValue;
  }
  function carryOver(now, next) {
    now.querySelectorAll('input, textarea, select').forEach(function (el) {
      if (el.type === 'hidden' || el.type === 'file' || !changedByYou(el)) return;
      var key = fieldKey(el), twin = key && next.querySelector(key);
      if (!twin) return;
      if (el.type === 'checkbox' || el.type === 'radio') twin.checked = el.checked;
      else twin.value = el.value;
    });
    var open = {};
    now.querySelectorAll('details[data-key]').forEach(function (d) { open[d.dataset.key] = d.open; });
    next.querySelectorAll('details[data-key]').forEach(function (d) { if (d.dataset.key in open) d.open = open[d.dataset.key]; });
  }
  function plain(el) {
    return el.outerHTML.replace(/\sjust-updated/g, '')
      .replace(/\s(open|hidden|disabled|aria-selected="[^"]*"|class="[^"]*\bselected\b[^"]*")/g, '');
  }

  function swap(doc) {
    var replaced = [];
    document.querySelectorAll('[data-part][id]').forEach(function (now) {
      var next = doc.getElementById(now.id);
      if (!next || plain(now) === plain(next)) return;
      if (now.dataset.part === 'flash' && !next.children.length) return;  // messages stay until there are new ones
      var active = document.activeElement, focusKey = '', start = null, end = null;
      if (active && now.contains(active) && active !== now) {
        focusKey = fieldKey(active);
        try { start = active.selectionStart; end = active.selectionEnd; } catch (err) { /* not a text field */ }
      }
      carryOver(now, next);
      next = document.importNode(next, true);
      now.replaceWith(next);
      if (next.dataset.part !== 'quiet') {
        next.classList.add('just-updated');
        next.addEventListener('animationend', function () { next.classList.remove('just-updated'); }, {once: true});
      }
      if (focusKey) {
        var twin = next.querySelector(focusKey);
        if (twin) {
          twin.focus({preventScroll: true});
          try { if (start !== null) twin.setSelectionRange(start, end); } catch (err) { /* not a text field */ }
        }
      }
      replaced.push(next.id);
    });
    document.dispatchEvent(new CustomEvent('cv:swapped', {detail: {doc: doc, replaced: replaced}}));
    return replaced;
  }

  function refresh() {
    return fetch(location.pathname + location.search, {headers: {'X-Requested-With': 'refresh'}})
      .then(function (r) { return r.text(); })
      .then(function (html) { return swap(new DOMParser().parseFromString(html, 'text/html')); });
  }

  // Forms with data-inline are sent without leaving the page: the page the server answers with replaces the parts that
  // changed, so its message appears in the part where you pressed the button and nothing reloads or jumps.
  function busyButton(button) {
    if (!button) return function () {};
    var label = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + (button.dataset.busyText || 'Working…');
    return function () { if (document.contains(button)) { button.disabled = false; button.innerHTML = label; } };
  }
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.matches('form[data-inline]') || form.dataset.inline === 'off' || !window.fetch) return;
    var submitter = e.submitter && e.submitter.form === form ? e.submitter : form.querySelector('[type=submit]');
    if (submitter && submitter.dataset.inline === 'off') return;  // this button leaves the page
    e.preventDefault();
    var body = new FormData(form);
    if (submitter && submitter.name) body.append(submitter.name, submitter.value);
    var action = (submitter && submitter.getAttribute('formaction')) || form.action;
    var done = busyButton(submitter);
    fetch(action, {method: 'POST', body: body, headers: {'X-Requested-With': 'inline'}})
      .then(function (r) {
        if (!r.ok && r.status !== 400) throw new Error('HTTP ' + r.status);
        return r.text().then(function (html) { return {html: html, url: r.url}; });
      })
      .then(function (answer) {
        if (answer.url && new URL(answer.url).pathname !== location.pathname) { location.href = answer.url; return; }
        var replaced = swap(new DOMParser().parseFromString(answer.html, 'text/html'));
        done();
        // the answer to what you pressed: a message in that part of the page (or at the top), brought into view
        var said = null;
        replaced.some(function (id) { said = document.getElementById(id).querySelector('.sec-note, .flash'); return said; });
        if (said) said.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      })
      .catch(function () {  // the page can't be updated in place: send it the ordinary way
        done();
        form.dataset.inline = 'off';
        if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
      });
  });

  // Poll a JSON endpoint while background work runs. `url` may be a function (re-evaluated each tick);
  // `step(data)` returns true to keep polling.
  window.CV = {
    poll: function (url, step) {
      var tick = function () {
        fetch(typeof url === 'function' ? url() : url)
          .then(function (r) { return r.json(); })
          .then(function (data) { if (step(data)) setTimeout(tick, 1500); })
          .catch(function () { setTimeout(tick, 4000); });
      };
      setTimeout(tick, 1500);
    },
    swap: swap,
    refresh: refresh
  };
})();
