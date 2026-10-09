"""Reading a form and acting on it in the assistant's browser (Playwright, sync API, one thread).

The page is read as text: labels, options, buttons, headings. Element ids (data-cvt) are re-assigned
on every read, so an action always refers to the page as it was just seen.
"""
import re

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
  const labelOf = el => {
    let t = '';
    const ids = el.getAttribute('aria-labelledby');
    if (ids) t = ids.split(/\s+/).map(i => norm(byId(i) && byId(i).innerText)).join(' ');
    if (!t) t = norm(el.getAttribute('aria-label'));
    if (!t && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = norm(l.innerText); }
    if (!t) { const l = el.closest('label'); if (l) t = norm(l.innerText); }
    if (!t) t = norm(el.getAttribute('placeholder') || el.getAttribute('title'));
    if (!t) { let p = el.parentElement, hops = 0; while (p && hops < 3 && !t) { const s = norm(p.innerText); if (s && s.length < 140) t = s; p = p.parentElement; hops++; } }
    if (!t) t = norm(el.getAttribute('name'));
    return t.slice(0, 160);
  };
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
  const inputs = document.querySelectorAll('input, select, textarea, [role=combobox], [role=checkbox], [role=radio], [role=switch]');
  for (const el of inputs) {
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase(), role = el.getAttribute('role') || '';
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled || outside(el)) continue;
    if (type === 'search' && !el.closest('form')) continue;
    if (type !== 'file' && !vis(el)) continue;
    if (tag === 'input' && role === 'combobox' && el.closest('[role=combobox]') !== el) continue;
    const id = mark(el);
    // Required as the form says it: the attribute, or the usual asterisk after the label.
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelOf(el));
    if (type === 'radio' || role === 'radio') {
      const holder = el.closest('fieldset, [role=radiogroup]');
      const key = el.name || (holder && (holder.id || holder.getAttribute('aria-label'))) || ('g' + id);
      const g = groups[key] || (groups[key] = {id, kind: 'radio', label: '', required: false, options: [], ids: [], filled: false, box: box(el)});
      g.options.push(labelOf(el)); g.ids.push(id);
      g.required = g.required || required;
      g.filled = g.filled || el.checked || el.getAttribute('aria-checked') === 'true';
      if (!g.label && holder) g.label = norm((holder.querySelector('legend') || {}).innerText || holder.getAttribute('aria-label') || '').slice(0, 160);
      continue;
    }
    const f = {id, label: labelOf(el), required, autocomplete: el.getAttribute('autocomplete') || '', box: box(el)};
    if (tag === 'select') {
      f.kind = 'select';
      f.options = [...el.options].map(o => norm(o.text)).filter(t => t && !/^(select|choose|please select|--)/i.test(t)).slice(0, 80);
      const chosen = el.options[el.selectedIndex];
      f.filled = !!(chosen && el.value && !/^(select|choose|please|--|\s*$)/i.test(chosen.text));
    } else if (type === 'checkbox' || role === 'checkbox' || role === 'switch') {
      f.kind = 'checkbox';
      f.filled = el.checked || el.getAttribute('aria-checked') === 'true';
    } else if (type === 'file') {
      f.kind = 'file';
      f.filled = !!(el.files && el.files.length);
      f.accept = el.getAttribute('accept') || '';
    } else if (role === 'combobox' && tag !== 'input') {
      f.kind = 'combobox';
      f.filled = !!norm(el.innerText) && !/^(select|choose|please)/i.test(norm(el.innerText));
    } else {
      f.kind = tag === 'textarea' ? 'textarea' : (role === 'combobox' ? 'combobox' : (type || 'text'));
      f.filled = !!norm(el.value);
    }
    fields.push(f);
    if (fields.length >= 150) break;
  }
  for (const g of Object.values(groups)) { g.required = g.required || /\*\s*$/.test(g.label); fields.push(g); }
  for (const el of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], a[href]')) {
    if (!vis(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const host = el.closest('form, [role=search]');
    if (host && notApplication.has(host)) continue;
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


def perform(page, action, field: dict | None = None) -> str:
    """Do one action. Returns a short description of what happened (raises on failure)."""
    loc = locate(page, action.id)
    if action.op == "fill":
        if field and field.get("kind") == "combobox":
            return _choose_custom(page, loc, action.value)
        loc.fill(action.value, timeout=8000)
        loc.dispatch_event("change")
        loc.evaluate("el => el.blur && el.blur()")
        return "filled"
    if action.op == "choose":
        kind = (field or {}).get("kind")
        if kind == "select":
            loc.select_option(label=action.value, timeout=8000)
            return "chosen"
        if kind == "radio":
            index = [o.casefold() for o in field["options"]].index(action.value.casefold())
            locate(page, field["ids"][index]).check(timeout=8000, force=True)
            return "chosen"
        return _choose_custom(page, loc, action.value)
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


def _choose_custom(page, loc, value: str) -> str:
    """Dropdowns built from buttons and lists (Workday and others): open, then pick the matching option."""
    loc.click(timeout=8000)
    page.wait_for_timeout(300)
    options = page.locator("[role=option]:visible")
    if options.count() == 0:  # a typeahead: type and let it suggest
        page.keyboard.type(value, delay=20)
        page.wait_for_timeout(600)
        options = page.locator("[role=option]:visible")
    want = value.casefold()
    for i in range(min(options.count(), 200)):
        text = " ".join((options.nth(i).inner_text(timeout=2000) or "").split()).casefold()
        if text == want or text.startswith(want) or want in text:
            options.nth(i).click(timeout=5000)
            return "chosen"
    page.keyboard.press("Enter")
    return "typed"


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
