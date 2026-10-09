"""Reading a form and acting on it in the assistant's browser (Playwright, sync API, one thread).

The page is read as text: each field's question, its kind, its options, buttons, headings. Element ids (data-cvt)
are re-assigned on every read, so an action always refers to the page as it was just seen; when a site re-renders
its form (most single-page application systems do), the session finds the field again by its question.

Every fill is read back: a value the site didn't keep (a typeahead that wanted a pick from its list, a custom
dropdown that ignored the click) raises NotKept, so you're asked instead of the assistant moving on with an empty
field. Fields for passwords, codes and ID numbers are marked `secret`: never filled from an answer, never sent to
the LLM, never logged.
"""
import re
from datetime import datetime

# Runs inside the page. Returns a plain description of what can be filled and clicked.
OBSERVE_JS = r"""
(prefix) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const box = el => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; };
  const byId = id => id ? document.getElementById(id) : null;
  const textOf = ids => (ids || '').split(/\s+/).map(i => norm(byId(i) && byId(i).innerText)).filter(Boolean).join(' ');
  const FIELDS = 'input:not([type=hidden]), select, textarea, [role=combobox], [role=listbox], [role=radio], [role=checkbox], [role=switch]';
  // Passwords, one-time codes, security answers, ID and bank numbers: yours to type, never filled from an answer.
  const SECRET = /password|passcode|passphrase|\bpin\b(?!\s*-?\s*code)|security (question|answer)|one[- ]time|\botp\b|verification code|social security|\bssn\b|national (insurance|identity|id)( number)?|aadhaa?r|\bpan( card| number)\b|passport (number|no)|\biban\b|routing number|sort code|(bank )?account number|card number|\bcvv\b|\bcvc\b/i;
  // The question a field belongs to when the site gives it no <label>: the closest text before it, inside the
  // smallest container that holds the field (a "form row"), never another field's text.
  const questionOf = el => {
    let p = el.parentElement;
    for (let hops = 0; p && p !== document.body && hops < 6; hops++, p = p.parentElement) {
      let best = '';
      for (const c of p.querySelectorAll('label, legend, h2, h3, h4, h5, p, span, div, [class*=label], [class*=question], [class*=title]')) {
        if (c.contains(el) || !(c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        if (c.querySelector(FIELDS)) continue;
        const t = norm(c.innerText);
        if (t && t.length >= 2 && t.length <= 200 && vis(c)) best = t;  // document order: the last one is the closest
      }
      if (best) return best;
      if (p.querySelectorAll(FIELDS).length > 8) break;  // that's the whole form, not this field's row
    }
    return '';
  };
  const labelOf = el => {
    let t = textOf(el.getAttribute('aria-labelledby'));
    if (!t) t = norm(el.getAttribute('aria-label'));
    if (!t && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = norm(l.innerText); }
    if (!t) { const l = el.closest('label'); if (l) t = norm(l.innerText); }
    if (!t) t = questionOf(el);
    if (!t) t = norm(el.getAttribute('placeholder') || el.getAttribute('title'));
    if (!t) t = norm(el.getAttribute('name'));
    return t.slice(0, 200);
  };
  const hintOf = el => {
    let t = textOf(el.getAttribute('aria-describedby'));
    const ph = norm(el.getAttribute('placeholder'));
    if (ph && !t) t = ph;
    return t.slice(0, 160);
  };
  // The question a group of choices answers (radios, "select all that apply" boxes).
  const groupLabel = (holder, first) => {
    if (holder) {
      const legend = holder.querySelector('legend');
      const t = (legend && norm(legend.innerText)) || textOf(holder.getAttribute('aria-labelledby')) || norm(holder.getAttribute('aria-label'));
      if (t) return t.slice(0, 200);
    }
    return questionOf(holder || first.closest('label') || first).slice(0, 200);
  };
  const isChecked = el => !!el.checked || el.getAttribute('aria-checked') === 'true';

  document.querySelectorAll('[data-cvt]').forEach(el => el.removeAttribute('data-cvt'));
  let n = 0;
  const mark = el => { const id = prefix + 'c' + (n++); el.setAttribute('data-cvt', id); return id; };
  const fields = [], groups = {}, buttons = [];
  // Site search, newsletters and job-alert sign-ups aren't part of an application: never read, never filled.
  const notApplication = new Set();
  for (const form of document.querySelectorAll('form, [role=search]')) {
    const count = form.querySelectorAll('input:not([type=hidden]), select, textarea').length;
    const about = norm(form.innerText) + ' ' + (form.getAttribute('action') || '') + ' ' + (form.getAttribute('aria-label') || '');
    if (form.getAttribute('role') === 'search' || (count <= 3 && /search|job alerts?|newsletter|subscribe|talent (community|network)|sign up for|keyword/i.test(about))) notApplication.add(form);
  }
  const outside = el => {
    const host = el.closest('form, [role=search]');
    return (host && notApplication.has(host)) || !!el.closest('nav, header, footer, [role=search], [role=navigation], [role=banner], [role=contentinfo]');
  };
  // Lists that belong to a dropdown (its popup) are read as that dropdown's options, not as fields of their own.
  const popups = new Set();
  for (const c of document.querySelectorAll('[role=combobox], input[list]')) {
    for (const attr of ['aria-controls', 'aria-owns']) (c.getAttribute(attr) || '').split(/\s+/).forEach(i => { if (byId(i)) popups.add(byId(i)); });
  }
  const optionTexts = list => [...list.querySelectorAll('[role=option]')].map(o => norm(o.innerText || o.getAttribute('aria-label'))).filter(Boolean);
  // Checkboxes that share a name, or sit together in a fieldset/group, are one "select all that apply" question.
  const boxGroup = el => {
    if (el.name && document.querySelectorAll('input[type=checkbox][name="' + CSS.escape(el.name) + '"]').length > 1) return 'n:' + el.name;
    const holder = el.closest('fieldset, [role=group]');
    return holder && holder.querySelectorAll('input[type=checkbox], [role=checkbox]').length > 1 ? holder : null;
  };
  const holderKeys = new Map();
  const secretOf = (el, label) => (el.getAttribute('type') || '').toLowerCase() === 'password' || SECRET.test(label);

  for (const el of document.querySelectorAll(FIELDS)) {
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase(), role = el.getAttribute('role') || '';
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled || el.getAttribute('aria-disabled') === 'true' || outside(el)) continue;
    if (type === 'search' && !el.closest('form')) continue;
    if (type !== 'file' && type !== 'radio' && type !== 'checkbox' && !vis(el)) continue;
    if ((type === 'radio' || type === 'checkbox') && !vis(el) && !(el.closest('label') && vis(el.closest('label')))) continue;  // styled inputs hide the box itself
    if (tag === 'input' && role === 'combobox' && el.closest('[role=combobox]') !== el) continue;
    if (role === 'listbox' && (popups.has(el) || el.closest('[role=combobox]'))) continue;
    const id = mark(el);
    const label = labelOf(el);
    // Required as the form says it: the attribute, or the usual asterisk after the label.
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label);
    if (type === 'radio' || role === 'radio') {
      const holder = el.closest('fieldset, [role=radiogroup]');
      const key = el.name || (holder && (holder.id || holder.getAttribute('aria-label') || holder.getAttribute('aria-labelledby'))) || ('g' + id);
      const g = groups[key] || (groups[key] = {id, kind: 'radio', label: groupLabel(holder, el), required: false, options: [], ids: [], filled: false, box: box(holder || el)});
      g.options.push(label); g.ids.push(id);
      g.required = g.required || required || (holder && holder.getAttribute('aria-required') === 'true');
      g.filled = g.filled || isChecked(el);
      continue;
    }
    if (type === 'checkbox' || role === 'checkbox') {
      const together = type === 'checkbox' ? boxGroup(el) : (el.closest('[role=group]') && el.closest('[role=group]').querySelectorAll('[role=checkbox]').length > 1 ? el.closest('[role=group]') : null);
      if (together) {
        let key = together;
        if (typeof together !== 'string') { if (!holderKeys.has(together)) holderKeys.set(together, 'h' + holderKeys.size); key = holderKeys.get(together); }
        const holder = typeof together === 'string' ? el.closest('fieldset, [role=group]') : together;
        const g = groups[key] || (groups[key] = {id, kind: 'multi', label: groupLabel(holder, el), required: false, options: [], ids: [], selected: [], filled: false, box: box(holder || el)});
        g.options.push(label); g.ids.push(id);
        if (isChecked(el)) { g.selected.push(label); g.filled = true; }
        g.required = g.required || required;
        continue;
      }
    }
    const f = {id, label, hint: hintOf(el), required, autocomplete: el.getAttribute('autocomplete') || '', box: box(el)};
    if (tag === 'select') {
      f.kind = el.multiple ? 'multi' : 'select';
      f.options = [...el.options].map(o => norm(o.text)).filter(t => t && !/^(select|choose|please select|--)/i.test(t)).slice(0, 200);
      if (el.multiple) { f.selected = [...el.selectedOptions].map(o => norm(o.text)); f.filled = f.selected.length > 0; f.native = true; }
      else { const chosen = el.options[el.selectedIndex]; f.filled = !!(chosen && el.value && !/^(select|choose|please|--|\s*$)/i.test(chosen.text)); }
    } else if (type === 'checkbox' || role === 'checkbox' || role === 'switch') {
      f.kind = 'checkbox';
      f.filled = isChecked(el);
    } else if (type === 'file') {
      f.kind = 'file';
      f.filled = !!(el.files && el.files.length);
      f.accept = el.getAttribute('accept') || '';
    } else if (role === 'listbox') {
      // A scrollable list you pick from (not a dropdown): each option gets its own id.
      const opts = [...el.querySelectorAll('[role=option]')].slice(0, 200);
      f.kind = 'listbox';
      f.multiple = el.getAttribute('aria-multiselectable') === 'true';
      f.options = opts.map(o => norm(o.innerText || o.getAttribute('aria-label')));
      f.ids = opts.map(o => mark(o));
      f.selected = opts.filter(o => o.getAttribute('aria-selected') === 'true').map(o => norm(o.innerText));
      f.filled = f.selected.length > 0;
    } else if (role === 'combobox' || (tag === 'input' && (el.getAttribute('list') || el.getAttribute('aria-autocomplete')))) {
      f.kind = 'combobox';
      f.typeahead = tag === 'input';
      const ctl = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\s+/).map(byId).filter(Boolean);
      const listed = el.list ? [...el.list.options].map(o => norm(o.value || o.text)) : [];
      f.options = [...ctl.flatMap(optionTexts), ...listed].filter(Boolean).slice(0, 200);
      const shown = tag === 'input' ? norm(el.value) : norm(el.innerText || el.getAttribute('aria-valuetext'));
      f.filled = !!shown && !/^(select|choose|please|--)/i.test(shown);
    } else {
      f.kind = tag === 'textarea' ? 'textarea' : (type || 'text');
      f.filled = !!norm(el.value);
      if (type === 'date') f.format = 'YYYY-MM-DD';
      if (type === 'month') f.format = 'YYYY-MM';
      if (el.getAttribute('maxlength') && +el.getAttribute('maxlength') > 0) f.maxlength = +el.getAttribute('maxlength');
    }
    f.secret = secretOf(el, label);
    fields.push(f);
    if (fields.length >= 150) break;
  }
  for (const g of Object.values(groups)) { g.required = g.required || /\*\s*$/.test(g.label); g.secret = false; fields.push(g); }
  for (const el of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], a[href]')) {
    if (!vis(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const host = el.closest('form, [role=search]');
    if (host && notApplication.has(host)) continue;
    if (el.closest('[role=listbox]')) continue;
    const text = norm(el.innerText || el.value || el.getAttribute('aria-label'));
    if (!text || text.length > 60) continue;
    if (el.tagName === 'A' && !/apply|next|continue|sign|log ?in|create|register|submit|review|back|save|start|account|autofill|manual|guest|proceed|reject|decline|necessary|accept|glassdoor|indeed|process|interview/i.test(text)) continue;
    buttons.push({id: mark(el), text, box: box(el), href: el.tagName === 'A' ? el.href : ''});
    if (buttons.length >= 40) break;
  }
  const texts = sel => [...document.querySelectorAll(sel)].filter(vis).map(e => norm(e.innerText)).filter(Boolean);
  return {
    url: location.href, title: document.title,
    headings: texts('h1, h2, h3, legend').slice(0, 8).map(t => t.slice(0, 120)),
    errors: texts('[role=alert], .error, .errors, [class*=error-message], [class*=errorMessage], [data-automation-id*=rror]').slice(0, 6).map(t => t.slice(0, 160)),
    text: norm(document.body ? document.body.innerText : '').slice(0, 2000),
    // Only a challenge you can see counts: invisible reCAPTCHA (a badge in the corner) needs nothing from you.
    captcha: [...document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], iframe[src*="arkoselabs"], .g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]')]
      .some(e => vis(e) && !/size=invisible/.test(e.getAttribute('src') || '') && e.getAttribute('data-size') !== 'invisible' && !e.closest('.grecaptcha-badge') && e.getBoundingClientRect().height > 30),
    password_fields: [...document.querySelectorAll('input[type=password]')].filter(vis).length,
    fields, buttons,
  };
}
"""

_CAPTCHA_FRAMES = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare\.com|arkoselabs|funcaptcha", re.I)


class NotKept(Exception):
    """The site didn't keep the value (it cleared it, or no option matched). `options` lists what it offers, when
    that was seen, so you can be asked to pick one."""

    def __init__(self, message: str, options: list[str] | None = None) -> None:
        super().__init__(message)
        self.options = options or []


def observe(page) -> dict:
    """Read the page and any embedded application frames (some sites put the form in an iframe)."""
    info = page.main_frame.evaluate(OBSERVE_JS, "")
    info["frames"] = {}
    for n, frame in enumerate(page.frames[1:], start=1):
        if _CAPTCHA_FRAMES.search(frame.url or ""):
            info["captcha"] = info["captcha"] or _visible_challenge(frame)
            continue
        if not (frame.url or "").startswith("http"):
            continue
        try:
            sub = frame.evaluate(OBSERVE_JS, f"f{n}-")
        except Exception:  # detached or cross-origin frame that refuses scripts
            continue
        if sub["fields"] or sub["buttons"]:
            info["fields"] += sub["fields"]
            info["buttons"] += sub["buttons"]
            info["text"] = (info["text"] + " " + sub["text"])[:3000]
            info["errors"] += sub["errors"]
            info["password_fields"] += sub["password_fields"]
            info["captcha"] = info["captcha"] or sub["captcha"]
    return info


def _visible_challenge(frame) -> bool:
    """A CAPTCHA frame you'd actually have to solve (not an invisible one or a corner badge)."""
    if "size=invisible" in (frame.url or ""):
        return False
    try:
        element = frame.frame_element()
        box = element.bounding_box()
        return element.is_visible() and box is not None and box["width"] > 60 and box["height"] > 60
    except Exception:
        return False


def _frame_for(page, element_id: str):
    m = re.match(r"f(\d+)-", element_id)
    if not m:
        return page.main_frame
    index = int(m.group(1))
    return page.frames[index] if index < len(page.frames) else page.main_frame


def locate(page, element_id: str):
    return _frame_for(page, element_id).locator(f'[data-cvt="{element_id}"]').first


# ---- options a dropdown shows only once it's open ----

def _option_list(page, element_id: str, loc):
    """The options of the dropdown `loc` opened: its own list when it names one, else any visible option."""
    frame = _frame_for(page, element_id)
    owned = (loc.get_attribute("aria-controls", timeout=2000) or loc.get_attribute("aria-owns", timeout=2000) or "").split()
    for list_id in owned:
        options = frame.locator(f'[id="{list_id}"] [role=option]')
        if options.count():
            return options
    return frame.locator("[role=option]:visible")


def _texts(options, limit: int = 200) -> list[str]:
    out = []
    for i in range(min(options.count(), limit)):
        try:
            text = " ".join((options.nth(i).inner_text(timeout=1500) or "").split())
        except Exception:
            continue
        if text:
            out.append(text)
    return out


def probe_options(page, info: dict, limit: int = 8) -> None:
    """Open each empty dropdown whose options aren't in the page yet, read them, and close it again, so the
    options can be matched (and shown to you) instead of guessed. Typeaheads are left alone: their list depends
    on what's typed."""
    todo = [f for f in info["fields"] if f.get("kind") == "combobox" and not f.get("options") and not f.get("filled")
            and not f.get("typeahead")][:limit]
    for f in todo:
        try:
            loc = locate(page, f["id"])
            loc.click(timeout=3000)
            page.wait_for_timeout(250)
            found = _texts(_option_list(page, f["id"], loc))
            if found:
                f["options"] = found
        except Exception:
            pass  # an option list that won't open is asked about with whatever was seen
        finally:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass


# ---- doing one thing ----

_DATE_IN = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
            "%B %Y", "%b %Y", "%m/%Y", "%Y-%m")


def as_date(value: str, fmt: str) -> str:
    """'15 March 2026' -> '2026-03-15' for a date input (which only takes ISO dates); unchanged if unreadable."""
    value = " ".join((value or "").split())
    for pattern in _DATE_IN:
        try:
            when = datetime.strptime(value, pattern)
        except ValueError:
            continue
        return when.strftime("%Y-%m" if fmt == "YYYY-MM" else "%Y-%m-%d")
    return value


def _kept(loc) -> str:
    try:
        return " ".join((loc.input_value(timeout=2000) or "").split())
    except Exception:  # not an input: a custom widget, read its text
        try:
            return " ".join((loc.inner_text(timeout=2000) or "").split())
        except Exception:
            return ""


def perform(page, action, field: dict | None = None) -> str:
    """Do one action. Returns a short description of what happened (raises on failure, NotKept when the site
    didn't keep the value)."""
    field = field or {}
    # Only your own account step (a saved password from your keychain, or a new one you asked for) types into these.
    if field.get("secret") and action.op in ("fill", "choose") and action.source != "account":
        raise NotKept("this field is for a password, code or ID number: yours to type")
    loc = locate(page, action.id)
    if action.op == "fill":
        if field.get("kind") == "combobox":
            return _choose_custom(page, action.id, loc, action.value, typeahead=field.get("typeahead", False))
        value = as_date(action.value, field["format"]) if field.get("format") else action.value
        loc.fill(value, timeout=8000)
        loc.dispatch_event("change")
        loc.evaluate("el => el.blur && el.blur()")
        if field.get("kind") in ("text", "textarea", "email", "tel", "url", "number", "date", "month", "") and not _kept(loc):
            # Some sites wipe a value set all at once: type it like a person would.
            loc.click(timeout=4000)
            loc.press_sequentially(value, delay=25, timeout=15000)
            loc.press("Tab")
            if not _kept(loc):
                raise NotKept("the site cleared what was typed")
        return "filled"
    if action.op == "choose":
        kind = field.get("kind")
        if kind == "select":
            try:
                loc.select_option(label=action.value, timeout=8000)
            except Exception as exc:
                raise NotKept(f"no option “{action.value}”", field.get("options")) from exc
            return "chosen"
        if kind == "radio":
            index = [o.casefold() for o in field["options"]].index(action.value.casefold())
            target = locate(page, field["ids"][index])
            try:
                target.check(timeout=8000, force=True)
            except Exception:
                target.click(timeout=5000, force=True)  # role=radio widgets that aren't real inputs
            return "chosen"
        if kind in ("multi", "listbox"):
            return _choose_many(page, action, field)
        return _choose_custom(page, action.id, loc, action.value)
    if action.op in ("check", "uncheck"):
        try:
            loc.check(timeout=5000) if action.op == "check" else loc.uncheck(timeout=5000)
        except Exception:
            loc.click(timeout=5000)  # role=checkbox widgets that aren't real inputs
        return "ticked" if action.op == "check" else "unticked"
    if action.op == "upload":
        loc.set_input_files(action.value, timeout=15000)
        return "attached"
    if action.op == "click":
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=8000)
        return "clicked"
    raise ValueError(f"unknown action {action.op}")


def split_choices(value: str) -> list[str]:
    """"Python; SQL" (or a list from the LLM, joined) -> ["Python", "SQL"]."""
    return [v.strip() for v in re.split(r"\s*[;\n|]\s*", value or "") if v.strip()]


def _choose_many(page, action, field: dict) -> str:
    """Tick every option asked for in a "select all that apply" group, list or multi-select."""
    wanted = split_choices(action.value)
    options = field.get("options") or []
    picked = [o for o in options if o.casefold() in {w.casefold() for w in wanted}]
    if not picked:
        raise NotKept(f"no option matching “{action.value}”", options)
    if field.get("native"):  # <select multiple>
        locate(page, action.id).select_option(label=picked, timeout=8000)
        return "chosen"
    for option in picked:
        target = locate(page, field["ids"][options.index(option)])
        if field.get("kind") == "multi":
            try:
                target.check(timeout=5000, force=True)
            except Exception:
                target.click(timeout=5000, force=True)
        else:
            target.scroll_into_view_if_needed(timeout=4000)
            target.click(timeout=5000, modifiers=["Control"] if field.get("multiple") and len(picked) > 1 else None)
    return "chosen"


def _choose_custom(page, element_id: str, loc, value: str, typeahead: bool = False) -> str:
    """Dropdowns built from buttons and lists (Workday, Oracle and others) and typeaheads: open (or type), pick the
    option that matches, and check the widget shows it. Nothing matching: NotKept, with the options seen."""
    from cv_maker.apply.planner import pick_option  # the same matching the rules use

    loc.click(timeout=8000)
    page.wait_for_timeout(300)
    options = _option_list(page, element_id, loc)
    if typeahead or options.count() == 0 or options.count() > 60:  # type to narrow a long list or start a typeahead
        try:
            loc.fill("", timeout=3000)
            loc.press_sequentially(value, delay=30, timeout=15000)
        except Exception:
            page.keyboard.type(value, delay=30)
        page.wait_for_timeout(800)
        options = _option_list(page, element_id, loc)
    seen = _texts(options)
    choice = pick_option(value, seen)
    if choice is None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        raise NotKept(f"no option matching “{value}”", seen)
    options.nth(seen.index(choice)).click(timeout=5000)
    page.wait_for_timeout(250)
    shown = _kept(loc).casefold()
    if shown and choice.casefold() not in shown and not shown.startswith(choice.casefold()[:12]) and value.casefold() not in shown:
        raise NotKept(f"the dropdown didn't take “{choice}”", seen)
    return "chosen"


def settle(page) -> None:
    """Give the page a moment to react (navigation, validation, the next step rendering)."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass  # pages that keep a connection open never go idle; that's fine
    page.wait_for_timeout(250)


def screenshot(page) -> bytes:
    return page.screenshot(type="jpeg", quality=62, timeout=8000)
