"""The agent's browser tools (Claude-style), on top of Playwright MCP.

The guard (pos_worker.browser_guard) shows the agent this tool set instead of
Playwright MCP's raw one: the same shape as Claude's own browser tools
(navigate, read_page with refs, find, get_page_text, click/type by ref or
coordinates, key, scroll, tabs, screenshot with a scale, zoom, console and
network logs, a batch of steps in one call). Each tool maps onto a Playwright
MCP tool, or onto a short Playwright snippet the guard runs itself through
`browser_run_code_unsafe` (never offered to the agent: it is code execution in
the pool container).

Everything here is pure (schemas, the snippets, parsing and filtering), so the
tests exercise it without a browser.
"""

import json
import re

# ------------------------------------------------------------------ schemas

REF = {"type": "string", "description": "the element's ref from read_page / find (e.g. e12)"}
COORD = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
         "description": "[x, y] in page (CSS) pixels of the viewport; a screenshot says how its pixels map"}
ELEMENT = {"type": "string", "description": "the element in words, as a person would name it (for the audit log)"}
MODS = {"type": "string", "description": "modifier keys held, e.g. 'ctrl' or 'shift+alt'"}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS: dict[str, tuple[str, dict]] = {
    "browser_navigate": (
        "Open a URL in the current tab, or 'back' / 'forward' in its history. Returns the page's title and URL "
        "only: read it with browser_get_page_text or browser_read_page. A cookie banner is declined for you "
        "(only the necessary cookies).",
        _schema({"url": {"type": "string", "description": "the URL (https:// assumed), 'back' or 'forward'"}},
                ["url"])),
    "browser_get_page_text": (
        "The page's readable text (the main content, tables as tab-separated rows): the cheapest way to read "
        "an article, a list or a table. No refs: to act on elements use browser_find or browser_read_page.",
        _schema({"max_chars": {"type": "integer", "description": "cut after this many characters (default 20000)"},
                 "whole_page": {"type": "boolean", "description": "the whole body, not only the main content"}})),
    "browser_read_page": (
        "The page's accessibility tree with a ref for every element ([ref=e12]): what is on the page and how to "
        "act on it. filter='interactive' keeps only what you can click or type into (much shorter). ref limits "
        "it to one part of the page.",
        _schema({"filter": {"type": "string", "enum": ["all", "interactive"], "description": "default all"},
                 "ref": {"type": "string", "description": "only this element and what is inside it"},
                 "depth": {"type": "integer", "description": "at most this deep"},
                 "max_chars": {"type": "integer", "description": "cut after this many characters (default 25000)"}})),
    "browser_find": (
        "Find elements by a description in plain words ('search box', 'Log in button', 'price of the second "
        "item'): returns the best matching lines of the page's tree with their refs, to click or type into.",
        _schema({"query": {"type": "string"}, "max_results": {"type": "integer", "description": "default 10"}},
                ["query"])),
    "browser_click": (
        "Click an element by ref (from browser_find / browser_read_page), or a point by coordinate. clicks=2 "
        "double-clicks, 3 triple-clicks (selects a line).",
        _schema({"ref": REF, "coordinate": COORD, "element": ELEMENT,
                 "button": {"type": "string", "enum": ["left", "right", "middle"]},
                 "clicks": {"type": "integer", "minimum": 1, "maximum": 3}, "modifiers": MODS})),
    "browser_type": (
        "Type text into a field by ref (its content is replaced), or into the focused element without a ref. "
        "submit=true presses Enter afterwards. Never type a password or a token: use browser_login.",
        _schema({"text": {"type": "string"}, "ref": REF, "element": ELEMENT,
                 "submit": {"type": "boolean", "description": "press Enter afterwards"}}, ["text"])),
    "browser_fill_form": (
        "Fill several fields at once: [{ref, name, type: textbox|checkbox|radio|combobox|slider, value}].",
        _schema({"fields": {"type": "array", "items": {"type": "object", "properties": {
            "ref": REF, "name": {"type": "string"}, "value": {"type": "string"},
            "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox", "slider"]}},
            "required": ["ref", "name", "type", "value"]}}}, ["fields"])),
    "browser_select": (
        "Choose option(s) in a dropdown (<select>) by ref.",
        _schema({"ref": REF, "element": ELEMENT, "values": {"type": "array", "items": {"type": "string"}}},
                ["ref", "values"])),
    "browser_hover": (
        "Move the pointer over an element (ref) or a point (coordinate): menus, tooltips.",
        _schema({"ref": REF, "coordinate": COORD, "element": ELEMENT})),
    "browser_key": (
        "Press keys: one or more, space-separated ('Enter', 'Tab Tab', 'ctrl+a', 'Escape', 'PageDown').",
        _schema({"keys": {"type": "string"}, "repeat": {"type": "integer", "minimum": 1, "maximum": 50}},
                ["keys"])),
    "browser_scroll": (
        "Scroll: an element into view (ref), or the page / the part under a coordinate in a direction "
        "(amount in wheel ticks of ~100 px, default 5).",
        _schema({"ref": REF, "coordinate": COORD,
                 "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                 "amount": {"type": "integer", "minimum": 1, "maximum": 30}})),
    "browser_drag": (
        "Drag from one element / point to another.",
        _schema({"start_ref": REF, "end_ref": REF, "start_coordinate": COORD, "coordinate": COORD,
                 "element": ELEMENT})),
    "browser_wait": (
        "Wait for something: text to appear (text) or disappear (text_gone), the network to go quiet "
        "(network_idle=true: an app that loads its data after the page), or a few seconds (at most 30).",
        _schema({"text": {"type": "string"}, "text_gone": {"type": "string"},
                 "network_idle": {"type": "boolean"}, "seconds": {"type": "number", "maximum": 30}})),
    "browser_tabs": (
        "Tabs: list them, open a new one (url), select or close one (index).",
        _schema({"action": {"type": "string", "enum": ["list", "new", "select", "close"]},
                 "index": {"type": "integer"}, "url": {"type": "string"}}, ["action"])),
    "browser_screenshot": (
        "A picture of the page (JPEG). Costly (~1,500 tokens at scale 0.5) and capped per run: read text with "
        "browser_get_page_text / browser_find first; take one when the layout or an image matters.",
        _schema({"scale": {"type": "number", "minimum": 0.1, "maximum": 1,
                           "description": "size of the image, default 0.5 (half)"},
                 "full_page": {"type": "boolean"}, "ref": {"type": "string", "description": "only this element"}})),
    "browser_zoom": (
        "A region of the page [x0, y0, x1, y1] (CSS pixels) as a sharp image: small text, an icon.",
        _schema({"region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                 "scale": {"type": "number", "minimum": 0.1, "maximum": 2}}, ["region"])),
    "browser_console": (
        "The page's console messages (errors first): debugging a web app.",
        _schema({"level": {"type": "string", "enum": ["error", "warning", "info", "debug"],
                           "description": "this level and more severe (default info)"},
                 "pattern": {"type": "string", "description": "only lines matching this regular expression"},
                 "limit": {"type": "integer", "description": "the last N lines (default 50)"}})),
    "browser_network": (
        "The page's network requests (method, URL, status), or one request's details by index (headers and "
        "body; secrets masked): debugging an app that does not load.",
        _schema({"index": {"type": "integer", "description": "details of this request"},
                 "filter": {"type": "string", "description": "only URLs matching this regular expression"},
                 "static": {"type": "boolean", "description": "include images, fonts, scripts (default false)"}})),
    "browser_dialog": (
        "Answer a JavaScript dialog (alert, confirm, prompt) the page opened.",
        _schema({"accept": {"type": "boolean"}, "prompt_text": {"type": "string"}}, ["accept"])),
    "browser_upload": (
        "Upload files from your work folder: into a file input by ref, or into the file chooser a click opened.",
        _schema({"paths": {"type": "array", "items": {"type": "string"}}, "ref": REF, "element": ELEMENT},
                ["paths"])),
    "browser_evaluate": (
        "Run a JavaScript function in the page and get its result, e.g. '() => document.title' (debugging; "
        "a script that sends requests or clicks by itself asks the owner).",
        _schema({"function": {"type": "string"}, "ref": REF, "element": ELEMENT}, ["function"])),
    "browser_batch": (
        "Several browser steps in one call (saves round trips and tokens): [{tool, args}, ...], e.g. click, "
        "type, key, wait, then get_page_text. Runs in order, stops at the first error (stop_on_error, default "
        "true). Only batch steps you can predict; refs must come from a read you already did.",
        _schema({"actions": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
            "tool": {"type": "string", "description": "a browser tool, e.g. click or browser_click"},
            "args": {"type": "object"}}, "required": ["tool"]}},
            "stop_on_error": {"type": "boolean"}}, ["actions"])),
    "browser_login": (
        "Fill one of your credentials (a user name, password or token) into a field without ever seeing it. "
        "Open the login page first; take ref from browser_find / browser_read_page. Works only on the "
        "credential's allowed hosts; the value is redacted everywhere and the field is masked in screenshots. "
        "submit=true presses Enter afterwards.",
        _schema({"credential": {"type": "string", "description": "the credential's name (credentials_list)"},
                 "ref": REF, "element": ELEMENT,
                 "submit": {"type": "boolean", "description": "press Enter afterwards (default false)"}},
                ["credential", "ref"])),
}

# Older names (Playwright MCP's, the guard before 2026-09-29) still work when an agent remembers them.
ALIASES = {
    "browser_snapshot": "browser_read_page", "browser_take_screenshot": "browser_screenshot",
    "browser_press_key": "browser_key", "browser_select_option": "browser_select",
    "browser_wait_for": "browser_wait", "browser_console_messages": "browser_console",
    "browser_network_requests": "browser_network", "browser_network_request": "browser_network",
    "browser_handle_dialog": "browser_dialog", "browser_file_upload": "browser_upload",
    "browser_navigate_back": "browser_navigate", "browser_get_text": "browser_get_page_text",
}
# What the gate (pos.browser.decide) calls each tool; reads are not asked about.
GATE_NAME = {
    "browser_navigate": "browser_navigate", "browser_click": "browser_click", "browser_type": "browser_type",
    "browser_fill_form": "browser_fill_form", "browser_select": "browser_select_option",
    "browser_key": "browser_press_key", "browser_drag": "browser_drag", "browser_dialog": "browser_handle_dialog",
    "browser_upload": "browser_file_upload", "browser_evaluate": "browser_evaluate", "browser_tabs": "browser_navigate",
}
# Tools whose result is page content (untrusted, Ú2): wrapped as external data.
READS = {"browser_get_page_text", "browser_read_page", "browser_find", "browser_console", "browser_network",
         "browser_tabs", "browser_evaluate", "browser_wait", "browser_navigate"}
IMAGES = {"browser_screenshot", "browser_zoom"}
INTERACTIVE_ROLES = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
                     "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
                     "spinbutton", "treeitem", "gridcell", "columnheader"}
REF_RE = re.compile(r"^f?\d*e\d+$")


def canonical(name: str) -> str:
    """browser_click, click, browser_snapshot (an alias) -> the tool's name here."""
    n = name if name.startswith("browser_") else f"browser_{name}"
    return ALIASES.get(n, n)


def alias_args(name: str, args: dict) -> dict:
    """Arguments of an older name in this tool set's shape."""
    a = dict(args or {})
    if "target" in a and "ref" not in a:
        a["ref"] = a.pop("target")
    if name == "browser_navigate_back":
        return {"url": "back"}
    if name == "browser_press_key":
        return {"keys": a.get("key", "")}
    if name == "browser_wait_for":
        return {k2: a[k] for k, k2 in (("time", "seconds"), ("text", "text"), ("textGone", "text_gone")) if k in a}
    if name == "browser_handle_dialog":
        return {"accept": a.get("accept", True), "prompt_text": a.get("promptText")}
    return a


# ------------------------------------------------------------------ Playwright snippets (run by the guard)

def locator_js(ref: str) -> str:
    """A Playwright locator expression for a ref from a snapshot (aria-ref) or a selector."""
    sel = f"aria-ref={ref}" if REF_RE.match(ref or "") else ref
    return f"page.locator({json.dumps(sel)})"


def snippet(tag: str, body: str) -> str:
    """`async (page) => {...}` for browser_run_code_unsafe, tagged so a test can tell them apart."""
    return f"/*pos:{tag}*/ async (page) => {{ {body} }}"


PAGE_INFO = snippet("info", "return {url: page.url(), title: await page.title().catch(() => ''), "
                            "tabs: page.context().pages().length};")

TEXT_JS = r"""
return await page.evaluate(([max, whole]) => {
  const root = whole ? document.body : (document.querySelector('main, [role=main], article') || document.body);
  let t = (root && root.innerText) || '';
  if (!whole && root !== document.body && t.trim().length < 200) t = document.body.innerText || '';
  t = t.replace(/[ \t ]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  return {url: location.href, title: document.title, total: t.length, text: t.slice(0, max)};
}, [%d, %s]);"""

DESCRIBE_EL = r"""(el) => {
  const pick = el.closest('a,button,input,select,textarea,summary,label,[role=button],[role=link],[role=menuitem],[role=tab],[role=checkbox],[onclick]') || el;
  const t = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  return {tag: pick.tagName.toLowerCase(), role: pick.getAttribute('role') || '', type: pick.type || '',
          name: t(pick.getAttribute('aria-label') || pick.innerText || pick.value || pick.title || pick.alt || pick.placeholder || pick.name),
          form: !!(pick.form || pick.closest('form')), href: pick.href || ''};
}"""


def describe_js(ref: str | None = None, xy: list | None = None) -> str:
    """What is really there (the gate asks the page, not the agent's words)."""
    if xy:
        return snippet("describe", f"return await page.evaluate(([x, y]) => {{ const el = document.elementFromPoint(x, y); "
                                   f"return el ? ({DESCRIBE_EL})(el) : null; }}, {json.dumps([float(xy[0]), float(xy[1])])});")
    return snippet("describe", f"return await {locator_js(ref or '')}.evaluate({DESCRIBE_EL}, null, {{timeout: 3000}});")


def text_js(max_chars: int, whole: bool) -> str:
    return snippet("text", TEXT_JS % (int(max_chars), "true" if whole else "false"))


PW_KEYS = {"ctrl": "Control", "control": "Control", "cmd": "Meta", "meta": "Meta", "super": "Meta", "win": "Meta",
           "alt": "Alt", "option": "Alt", "shift": "Shift", "enter": "Enter", "return": "Enter", "esc": "Escape",
           "escape": "Escape", "tab": "Tab", "space": "Space", "backspace": "Backspace", "delete": "Delete",
           "del": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
           "pageup": "PageUp", "pagedown": "PageDown", "pgup": "PageUp", "pgdn": "PageDown", "home": "Home",
           "end": "End"}


def pw_key(combo: str) -> str:
    """'ctrl+a' -> 'Control+a', 'return' -> 'Enter' (Playwright's key names)."""
    parts = [p for p in re.split(r"\+", combo.strip()) if p]
    out = []
    for p in parts:
        low = p.lower()
        out.append(PW_KEYS.get(low, p if len(p) == 1 else p[:1].upper() + p[1:]))
    return "+".join(out)


def keys_js(keys: str, repeat: int) -> str:
    seq = [pw_key(k) for k in keys.split() if k.strip()]
    return snippet("key", f"for (let i = 0; i < {int(repeat)}; i++) for (const k of {json.dumps(seq)}) "
                          "await page.keyboard.press(k); return true;")


def mods(spec: str | None) -> list[str]:
    return [pw_key(m) for m in re.split(r"[+ ]", spec or "") if m.strip()]


def click_xy_js(x: float, y: float, button: str, clicks: int, modifiers: list[str]) -> str:
    return snippet("click_xy", f"for (const m of {json.dumps(modifiers)}) await page.keyboard.down(m); "
                               f"await page.mouse.click({float(x)}, {float(y)}, {{button: {json.dumps(button)}, "
                               f"clickCount: {int(clicks)}}}); "
                               f"for (const m of {json.dumps(modifiers)}) await page.keyboard.up(m); return true;")


def click_ref_js(ref: str, button: str, clicks: int, modifiers: list[str]) -> str:
    return snippet("click_ref", f"await {locator_js(ref)}.click({{button: {json.dumps(button)}, clickCount: {int(clicks)}, "
                                f"modifiers: {json.dumps(modifiers)}, timeout: 8000}}); return true;")


def hover_xy_js(x: float, y: float) -> str:
    return snippet("hover_xy", f"await page.mouse.move({float(x)}, {float(y)}); return true;")


def type_focused_js(text: str, submit: bool) -> str:
    return snippet("type_focused", f"await page.keyboard.insertText({json.dumps(text)}); "
                                   + ("await page.keyboard.press('Enter'); " if submit else "") + "return true;")


def scroll_js(ref: str | None, xy: list | None, direction: str, amount: int) -> str:
    if ref:
        return snippet("scroll", f"await {locator_js(ref)}.scrollIntoViewIfNeeded({{timeout: 5000}}); return true;")
    dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.get(direction or "down", (0, 1))
    move = f"await page.mouse.move({float(xy[0])}, {float(xy[1])}); " if xy else ""
    return snippet("scroll", f"{move}await page.mouse.wheel({dx * 100 * amount}, {dy * 100 * amount}); "
                             "await page.waitForTimeout(300); return await page.evaluate(() => [scrollX, scrollY]);")


def drag_xy_js(start: list, end: list) -> str:
    return snippet("drag_xy", f"await page.mouse.move({float(start[0])}, {float(start[1])}); await page.mouse.down(); "
                              f"await page.mouse.move({float(end[0])}, {float(end[1])}, {{steps: 12}}); "
                              "await page.mouse.up(); return true;")


def screenshot_js(full_page: bool = False, clip: list | None = None, ref: str | None = None, quality: int = 70) -> str:
    opts = f"type: 'jpeg', quality: {int(quality)}, fullPage: {'true' if full_page and not clip else 'false'}"
    if clip:
        x0, y0, x1, y1 = (float(v) for v in clip)
        opts += f", clip: {{x: {min(x0, x1)}, y: {min(y0, y1)}, width: {max(1.0, abs(x1 - x0))}, height: {max(1.0, abs(y1 - y0))}}}"
    target = f"{locator_js(ref)}" if ref else "page"
    return snippet("screenshot", f"const b = await {target}.screenshot({{{opts}, timeout: 15000}}); "
                                 "const v = page.viewportSize() || {width: 0, height: 0}; "
                                 "return {data: b.toString('base64'), vw: v.width, vh: v.height, url: page.url(), "
                                 "title: await page.title().catch(() => ''), tabs: page.context().pages().length};")


def idle_js(timeout_ms: int) -> str:
    return snippet("idle", f"try {{ await page.waitForLoadState('networkidle', {{timeout: {int(timeout_ms)}}}); "
                           "return 'idle'; } catch (e) { return 'still busy'; }")


FORWARD = snippet("forward", "await page.goForward({timeout: 30000}); return page.url();")
STORAGE_STATE = snippet("storage", "return await page.context().storageState({indexedDB: true});")


def upload_ref_js(ref: str, paths: list[str]) -> str:
    return snippet("upload", f"await {locator_js(ref)}.setInputFiles({json.dumps(paths)}, {{timeout: 8000}}); return true;")


def mask_ref_js(ref: str) -> str:
    """The field browser_login filled shows dots in every screenshot from now on."""
    return snippet("mask", f"await {locator_js(ref)}.evaluate((el) => {{ el.style.webkitTextSecurity = 'disc'; "
                           "el.style.textSecurity = 'disc'; }); return true;")


# Consent banners: the privacy-preserving choice (only the necessary cookies), as the owner wants.
COOKIE_JS = r"""
const known = ['#onetrust-reject-all-handler', '#CybotCookiebotDialogBodyButtonDecline',
  '#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll', '#didomi-notice-disagree-button',
  'button[data-cookiebanner="reject_button"]', '.cc-deny', '#cookiescript_reject', '[data-testid="uc-deny-all-button"]',
  '.fc-cta-do-not-consent', 'button[mode="secondary"][aria-label*="Reject"]', '.cmpboxbtnno', '#tarteaucitronAllDenied2',
  '.cky-btn-reject', '#wt-cli-reject-btn', '.sp_choice_type_13', 'button.reject-all', '#reject-all', '#rejectAll'];
const words = /^\s*(reject all|reject|decline all|decline|deny all|deny|refuse all|refuse|only necessary|necessary only|only essential|essential only|use necessary cookies only|continue without accepting|odmítnout vše|odmítnout|pouze nezbytné|jen nezbytné|nezbytné|nesouhlasím|pokračovat bez souhlasu|alle ablehnen|ablehnen|nur notwendige)\b/i;
for (const frame of page.frames()) {
  let hit = null;
  try {
    hit = await frame.evaluate(([known, src]) => {
      const words = new RegExp(src, 'i');
      const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      for (const sel of known) { const el = document.querySelector(sel); if (el && visible(el)) { el.click(); return sel; } }
      const cookieish = (el) => { for (let n = el, i = 0; n && i < 8; n = n.parentElement, i++) {
        if (/cookie|consent|souhlas|gdpr|privacy|soukrom|tracking/i.test((n.id || '') + ' ' + (n.className || '') + ' ' + (n.getAttribute && (n.getAttribute('aria-label') || '')))) return true; }
        return /cookie|consent|souhlas/i.test(document.title) || /cookie|souhlas/i.test((el.closest('[role=dialog],dialog,aside,section,div') || {}).innerText || ''); };
      for (const el of document.querySelectorAll('button, [role=button], a, input[type=button], input[type=submit]')) {
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        if (t && t.length < 60 && words.test(t) && visible(el) && cookieish(el)) { el.click(); return t; }
      }
      return null;
    }, [known, words.source]);
  } catch (e) { hit = null; }
  if (hit) return hit;
}
return null;"""
COOKIES = snippet("cookies", COOKIE_JS)


def parse_result(text: str):
    """The value a browser_run_code_unsafe call returned ('### Result\\n<json>\\n### ...')."""
    m = re.search(r"### Result\s*\n(.*?)(?:\n### |\Z)", text or "", re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def error_of(text: str) -> str | None:
    m = re.search(r"### Error\s*\n(.*?)(?:\n### |\Z)", text or "", re.S)
    return m.group(1).strip() if m else None


def strip_code(text: str) -> str:
    """Playwright MCP's '### Ran Playwright code' block and '### Result' header: noise for the agent (tokens)."""
    text = re.sub(r"### Ran Playwright code\s*\n```[a-z]*\n.*?```\s*\n?", "", text or "", flags=re.S)
    text = re.sub(r"^### Result\s*\n", "", text, flags=re.M)
    return text.strip()


def compact(text: str) -> str:
    """An action's result without the page header (the guard adds one 'Now: title — url' line)."""
    text = re.sub(r"### Page\s*\n(?:- [^\n]*\n?)*", "", strip_code(text))
    return text.strip()


# ------------------------------------------------------------------ the page tree: filter and find

_LINE = re.compile(r"^(?P<indent>\s*)- (?P<role>[a-zA-Z]+)(?P<rest>.*)$")


def snapshot_body(text: str) -> str:
    """The YAML tree out of a browser_snapshot result (without the ``` fence and the page header)."""
    m = re.search(r"```yaml\n(.*?)```", text or "", re.S)
    return m.group(1).rstrip() if m else (text or "")


def page_header(text: str) -> str:
    m = re.search(r"### Page\n(.*?)(?:\n### |\Z)", text or "", re.S)
    return m.group(1).strip() if m else ""


def interactive_only(tree: str) -> str:
    """Only the lines you can act on (and headings, for orientation), flattened: a fraction of the tree."""
    out = []
    for line in tree.splitlines():
        m = _LINE.match(line)
        if not m or "[ref=" not in line:
            continue
        role = m.group("role").lower()
        if role in INTERACTIVE_ROLES or role == "heading":
            depth = len(m.group("indent")) // 2
            out.append("  " * min(depth, 3) + "- " + m.group("role") + m.group("rest").split(" [cursor=")[0])
    return "\n".join(out)


STOP = {"the", "a", "an", "of", "on", "in", "to", "for", "and", "or", "with", "at", "by", "is", "it", "this",
        "that", "element", "na", "v", "ve", "do", "pro", "k", "s", "se"}
ROLE_WORDS = {"button": {"button"}, "btn": {"button"}, "tlačítko": {"button"}, "link": {"link"}, "odkaz": {"link"},
              "input": {"textbox", "searchbox", "combobox"}, "box": {"textbox", "searchbox", "combobox"},
              "field": {"textbox", "searchbox", "combobox"}, "pole": {"textbox", "searchbox"},
              "search": {"searchbox"}, "checkbox": {"checkbox"}, "dropdown": {"combobox", "listbox"},
              "select": {"combobox", "listbox"}, "menu": {"menuitem", "menu"}, "tab": {"tab"},
              "heading": {"heading"}, "title": {"heading"}, "image": {"img"}, "table": {"table"},
              "row": {"row"}, "cell": {"cell", "gridcell"}}


def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def find(tree: str, query: str, max_results: int = 10) -> list[str]:
    """The tree lines that best match a description in plain words, with their parent for context.
    A word scores when it is in the line's text; a role word ('button', 'search box') when the line
    has that role; interactive elements win a tie."""
    words = [w for w in re.findall(r"[\w@.#-]+", _norm(query)) if w not in STOP]
    if not words:
        return []
    lines = tree.splitlines()
    scored = []
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if not m or "[ref=" not in line:
            continue
        role, text = m.group("role").lower(), _norm(line)
        score = 0.0
        for w in words:
            roles = ROLE_WORDS.get(w)
            if roles and role in roles:
                score += 1.5
            elif w in text:
                score += 2.0 if re.search(rf"\b{re.escape(w)}\b", text) else 1.0
        if score <= 0:
            continue
        score += 0.5 if role in INTERACTIVE_ROLES else 0
        scored.append((score, i))
    scored.sort(key=lambda s: (-s[0], s[1]))
    out = []
    for _, i in scored[:max_results]:
        line = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        parent = next((lines[j].strip() for j in range(i - 1, -1, -1)
                       if len(lines[j]) - len(lines[j].lstrip()) < indent and "[ref=" in lines[j]), "")
        out.append(line + (f"   (in: {parent[:100]})" if parent else ""))
    return out


# ------------------------------------------------------------------ network details: secrets masked

SECRET_HEADERS = re.compile(r"^(\s*[-*]?\s*)(authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
                            r"x-auth-token|x-csrf-token|x-xsrf-token)(\s*:\s*)(.+)$", re.I | re.M)


def mask_headers(text: str) -> str:
    return SECRET_HEADERS.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<hidden>", text or "")


def clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + (f"\n… [cut at {max_chars} of {len(text)} characters: ask for a part (ref), "
                               "filter='interactive', or browser_find]")
