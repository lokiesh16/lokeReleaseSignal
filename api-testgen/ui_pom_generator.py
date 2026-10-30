"""Page-object flavour of the generated UI test: two files instead of one.

  <slug>_page.py        page classes holding the LOCATORS, one class per screen (URL path),
                        each locator a named property:  login.sign_in_button
  test_<slug>.py        the pytest TEST CLASS (and fixtures) that uses them:
                        login.sign_in_button.click()

Same recording, same behaviour as the single-file output - only the structure differs, so a UI
change means editing one selector in the page file instead of hunting through test steps.
Deterministic: the same recording always produces the same names.
"""
import builtins
import keyword
import re
from datetime import datetime

from ui_test_generator import (
    CAPTURE_HELPER_SRC, _ROLES, _SCROLL_JS, _locator, _py_label, _render_step_page, _s, slugify, split_cases,
    uses_capture, with_capture_support, uses_datetime, DATETIME_HELPER_SRC, CHARTPOINT_HELPER_SRC, uses_script, SCRIPT_HELPER_SRC,
)

_ROLE_SUFFIX = {"button": "button", "link": "link", "textbox": "input", "searchbox": "search_input",
                "checkbox": "checkbox", "radio": "radio", "combobox": "dropdown", "heading": "heading",
                "tab": "tab", "menuitem": "menu_item", "option": "option", "switch": "switch",
                "slider": "slider", "dialog": "dialog", "img": "image"}
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{12,}|[0-9a-f]{8}-[0-9a-f-]{27})$", re.I)


def _snake(text, limit=40):
    t = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_").lower()
    return t[:limit].strip("_")


def _ident(name, fallback="element"):
    name = name or fallback
    if name[0].isdigit():
        name = "e_" + name
    if keyword.iskeyword(name) or name in ("self", "page"):
        name += "_"
    if hasattr(builtins, name):      # `list`, `id`, `input`... would shadow a builtin inside the class
        name += "_element"
    return name


def _camel(text):
    return "".join(w.capitalize() for w in re.findall(r"[A-Za-z0-9]+", text or ""))


def page_class_for(url):
    """URL -> page class name. Dynamic segments (ids) are dropped, so /participants/6a7af… and
    /participants/9bc… are the same page: ParticipantsPage."""
    m = re.match(r"^[a-z][a-z0-9+.-]*://[^/]+(/[^?#]*)?", url or "", re.I)
    path = (m.group(1) if m else "") or ""
    segs = [re.sub(r"\.[A-Za-z0-9]{2,5}$", "", x) for x in path.split("/") if x and not _ID_SEGMENT.match(x)]   # /a.html -> a
    segs = [x for x in segs if x]
    name = _camel("_".join(segs[-3:])) if segs else "Home"
    if not name or name[0].isdigit():
        name = "Page" + name
    return name + "Page"


def page_path_for(url):
    """The URL path with dynamic ids shown as <id>, e.g. /participants/<id>."""
    m = re.match(r"^[a-z][a-z0-9+.-]*://[^/]+(/[^?#]*)?", url or "", re.I)
    path = (m.group(1) if m else "") or "/"
    return "/".join("<id>" if _ID_SEGMENT.match(x) else x for x in path.split("/")) or "/"


def locator_name(loc):
    """A readable attribute name for a locator, e.g. get_by_role("button", name="Sign in") -> sign_in_button."""
    if loc.get("anchor"):
        return _snake(loc["anchor"]) + "_field"
    kind, py = loc.get("kind"), loc.get("py") or ""
    if kind == "pw":
        m = re.search(r'get_by_role\("(\w+)"', py)
        role = m.group(1) if m else None
        words = [_snake(re.sub(r"[\^$]", "", x), 28) for x in re.findall(r'"((?:[^"\\]|\\.)*)"', py) if x not in _ROLES]
        words = [w for w in words if w][:2]
        if role:
            suffix = _ROLE_SUFFIX.get(role, role)
        elif "get_by_text" in py:
            suffix = "text"
        elif "get_by_label" in py or "get_by_placeholder" in py:
            suffix = "field"
        elif "get_by_test_id" in py:
            suffix = ""
        else:
            suffix = "element"
        parts = words + ([suffix] if suffix and (not words or words[-1] != suffix) else [])
        return "_".join(parts) or "element"
    if loc.get("kind") == "chartpoint":
        return "chart_point"
    if loc.get("kind") == "pattern":
        return "time_label" if re.search(r"\d.*:|:.*\d", loc.get("value") or "") else "date_label"
    val = loc.get("value") or ""
    lab = re.search(r"normalize-space\(\.\)=['\"]([^'\"]+)['\"]", val)
    if lab:
        return _snake(lab.group(1)) + "_field"
    tid = re.search(r"data-(?:testid|test-id|test|qa|cy)=[\"']([^\"']+)", val)
    if tid:
        return _snake(tid.group(1))
    idm = re.match(r"^#([\w-]+)$", val)
    if idm:
        return _snake(idm.group(1))
    words = _snake(re.sub(r"[#.\[\]\"'=>:()]", " ", val.replace("xpath=", "")), 30)
    return (words + "_element") if words else "element"


class _PageClasses:
    """Collects locators per page class and hands out unique, stable attribute names."""

    def __init__(self):
        self.classes = {}   # class -> {"attrs": {expr: name}, "names": set(), "order": [(name, expr)], "paths": set()}

    def attr(self, cls, loc, expr):
        c = self.classes.setdefault(cls, {"attrs": {}, "names": set(), "order": []})
        if expr in c["attrs"]:
            return c["attrs"][expr]
        base = _ident(locator_name(loc))
        name, n = base, 1
        while name in c["names"]:
            n += 1
            name = f"{base}_{n}"
        c["attrs"][expr] = name
        c["names"].add(name)
        c["order"].append((name, expr))
        return name


def generate_ui_pom(name, steps, timeout_s=15, shared_session=True):
    """Returns (test_code, page_code, secrets, page_module_name)."""
    slug = slugify(name)
    base = slug if slug[0].isalpha() else "t_" + slug
    page_module = f"{base}_page"
    class_name = "Test" + (_camel(slug) or "Scenario")
    if class_name[4:5].isdigit():
        class_name = "TestScenario" + class_name[4:]
    timeout_ms = max(1, int(float(timeout_s or 15) * 1000))
    secrets = {}
    reg = _PageClasses()
    start_url = next((s.get("url") for s in steps if s.get("type") == "goto"), "")
    uses_b = any(st.get("portal") == "B" for st in steps)
    cases = split_cases(name, steps)
    multi = len(cases) > 1
    used_classes = []
    funcs, used_names = [], set()
    opened = set()
    _PV = {"A": "page", "B": "page_b"}

    for n, (cname, csteps) in enumerate(cases, 1):
        lines = []
        if not shared_session:
            opened = set()
        cur = {}                 # portal -> (page class, local variable) for this test method
        last_cls = {}            # portal -> class of the previous step, for steps recorded without a url

        def locfn(loc, st):
            portal = st.get("portal", "A")
            cls = page_class_for(st.get("url")) if st.get("url") else last_cls.get(portal, "AppPage")
            last_cls[portal] = cls
            expr = _locator(loc)
            if expr.startswith("page."):
                expr = "self.page." + expr[5:]
            elif expr.startswith("chart_point(page,"):
                expr = "chart_point(self.page," + expr[len("chart_point(page,"):]
            attr = reg.attr(cls, loc, expr)
            if st.get("url"):
                reg.classes[cls].setdefault("path", page_path_for(st["url"]))
            var = _snake(cls[:-4]) + ("_b" if portal == "B" else "")
            var = _ident(var or "page_obj", "page_obj")
            if cur.get(portal) != (cls, var):
                pending.append(f"{var} = {cls}({_PV.get(portal, 'page')})")
                cur[portal] = (cls, var)
            return f"{var}.{attr}"

        for st in csteps:
            portal = st.get("portal", "A")
            if st.get("type") == "goto":
                opened.add(portal)
            elif portal not in opened and st.get("url") and st.get("type") not in ("wait", "note", "case"):
                lines.append(f'{_PV.get(portal, "page")}.goto({_s(st["url"])})')
                opened.add(portal)
            pending = []
            line = _render_step_page(st, secrets, locfn=locfn)
            if line and portal == "B" and line.startswith(("page.",)):
                line = "page_b" + line[4:]
            elif line and portal == "B" and line.startswith("captured[") and "capture_value(page." in line:
                line = line.replace("capture_value(page.", "capture_value(page_b.", 1)
            elif line and portal == "B" and line.startswith("assert_datetime(page."):
                line = "assert_datetime(page_b" + line[len("assert_datetime(page"):]
            elif line and portal == "B" and line.startswith("expect(page)"):
                line = "expect(page_b" + line[len("expect(page"):]
            lines += pending
            if line:
                lines.append(line)
        fn = f"test_{n:02d}_{slugify(cname)}" if multi else f"test_{slug}"
        while fn in used_names:
            fn += "_x"
        used_names.add(fn)
        needs_b = any(st.get("portal") == "B" for st in csteps)
        funcs.append((fn, cname, lines or ["pass"], needs_b))
    if not funcs:
        funcs.append((f"test_{slug}", name or slug, ["pass  # no steps were recorded"], False))
    funcs = with_capture_support(funcs)

    # ── page file ────────────────────────────────────────────────────────────
    pg = [
        f'"""Page objects for "{name or slug}" - the LOCATORS live here.',
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from {start_url or 'the browser'}.",
        "",
        "One class per screen (by URL path). When the app's UI changes, fix the selector here once;",
        'the tests in the matching test file only say what to do ("sign_in_button.click()").',
        '"""',
        "import re",
        "",
        "from playwright.sync_api import Page",
    ]
    if any("chart_point(" in expr for c in reg.classes.values() for _a, expr in c["order"]):
        pg += ["", ""] + CHARTPOINT_HELPER_SRC.splitlines()      # the page objects call it, so it lives in this file
    for cls in sorted(reg.classes):
        where = reg.classes[cls].get("path")
        pg += ["", "", f"class {cls}:", f'    """Locators for the screen at {where}."""' if where else '    """Locators for this screen."""', "",
               "    def __init__(self, page: Page):", "        self.page = page"]
        for attr, expr in reg.classes[cls]["order"]:
            pg += ["", "    @property", f"    def {attr}(self):", f"        return {expr}"]
    if not reg.classes:
        pg += ["", "", "# No element locators were recorded."]
    page_code = "\n".join(pg) + "\n"

    # ── test file ────────────────────────────────────────────────────────────
    imports = ", ".join(sorted(reg.classes))
    out = [
        f'"""UI test: {name or slug}',
        f"Recorded {datetime.now().strftime('%Y-%m-%d %H:%M')} from {start_url or 'the browser'}.",
        "",
        f"Locators are in {page_module}.py (same folder); this file only holds the test steps.",
        "Run:  python -m pytest <this file> -v -s",
        f"Env:  UI_TIMEOUT_MS (default {timeout_ms}) - how long each step waits for its element;",
        "      UI_HEADED=0 runs headless, UI_SLOWMO=<ms> slows each action down.",
    ]
    if uses_b:
        out.append("Two portals: `page` is Portal A and `page_b` is Portal B - separate browser contexts, so each keeps its own login.")
    if multi:
        out.append(f"{len(funcs)} test cases, run in order" +
                   (" in ONE shared browser session (each continues where the previous stopped)." if shared_session
                    else ", each in a fresh browser."))
    if secrets:
        out.append("Secrets (read from the environment, never stored in this file): " + ", ".join(secrets))
    out.append('"""')
    out.append("import os")
    out.append("import re")
    if secrets:
        out += [
            "", "",
            "def _load_local_env():",
            f'    """Pick up .{slug}.env next to this file (written by the recorder) without overriding real env vars."""',
            f'    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".{slug}.env")',
            "    if not os.path.isfile(path):",
            "        return",
            "    for line in open(path, encoding='utf-8'):",
            "        key, sep, val = line.rstrip('\\n').partition('=')",
            "        if sep and key and key not in os.environ:",
            "            os.environ[key] = val",
            "", "",
            "_load_local_env()",
        ]
    scope = "module" if (multi and shared_session) else "function"
    out += [
        "",
        "import pytest",
        "from playwright.sync_api import sync_playwright, expect",
        "",
        *([f"from {page_module} import {imports}", ""] if reg.classes else [""]),
        f'TIMEOUT_MS = int(os.environ.get("UI_TIMEOUT_MS", "{timeout_ms}"))',
        "expect.set_options(timeout=TIMEOUT_MS)",
        "",
        "",
        *((CAPTURE_HELPER_SRC.splitlines() + ["", ""]) if uses_capture(steps) else []),
        *((DATETIME_HELPER_SRC.splitlines() + ["", ""]) if uses_datetime(steps) else []),
        *((SCRIPT_HELPER_SRC.splitlines() + ["", ""]) if uses_script(steps) else []),
        '@pytest.fixture(scope="session")',
        "def browser():",
        "    with sync_playwright() as p:",
        '        b = p.chromium.launch(headless=os.environ.get("UI_HEADED", "1") == "0",',
        '                              slow_mo=int(os.environ.get("UI_SLOWMO", "0")))',
        "        yield b",
        "        b.close()",
    ]
    def fixture(fname, comment=None):
        return ["", "", f'@pytest.fixture(scope="{scope}")', f"def {fname}(browser):"] + \
               ([f"    # {comment}"] if comment else []) + [
               "    context = browser.new_context()",
               "    page = context.new_page()",
               "    page.set_default_timeout(TIMEOUT_MS)",
               "    page.set_default_navigation_timeout(max(TIMEOUT_MS, 30_000))",
               '    page.on("dialog", lambda d: d.accept())',
               "    yield page",
               "    context.close()"]
    out += fixture("page")
    if uses_b:
        out += fixture("page_b", "Portal B: its own context, so it is a separate session/login from `page` (Portal A)")
    out += ["", "", f"class {class_name}:"]
    for fn, cname, lines, needs_b in funcs:
        out += ["", f"    def {fn}(self, page, page_b):" if needs_b else f"    def {fn}(self, page):"]
        if multi:
            out.append(f"        {_s(cname)}")
        out += ["        " + ln for ln in lines]
    return "\n".join(out) + "\n", page_code, secrets, page_module
