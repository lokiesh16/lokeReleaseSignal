"""Turns recorded UI steps (see ui_recorder.js) into a self-contained
pytest + Playwright test file. Deterministic - no LLM involved - so the same
recording always produces the same script and nothing is guessed.

Returns (code, secrets): `secrets` maps env-var name -> recorded value for
every password-like field, which is never written into the script itself.
"""
import json
import re
from datetime import datetime


def _s(value):
    """Python string literal (JSON escaping is valid Python for these)."""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


_SCROLL_JS = {"top": "window.scrollTo(0, 0)",
              "bottom": "window.scrollTo(0, document.documentElement.scrollHeight)"}


def assert_js_source(expr, expected_text):
    """JavaScript for a `wait_for_function` that passes once `expr` evaluates to the expected value.
    `expected_text` is JSON (a plain string if it isn't valid JSON). Object keys are compared order-
    independently, and it keeps re-checking until the step timeout - so data that loads a moment late
    doesn't fail the test. Used by both the generated test and the live replay, so they agree."""
    try:
        expected = json.loads(expected_text)
    except (TypeError, ValueError):
        expected = "" if expected_text is None else str(expected_text)
    return ("() => { const n = v => Array.isArray(v) ? v.map(n) : (v && typeof v === 'object') "
            "? Object.keys(v).sort().reduce((o, k) => (o[k] = n(v[k]), o), {}) : v; "
            "try { return JSON.stringify(n((" + str(expr) + "))) === JSON.stringify(n(" + json.dumps(expected) + ")); } "
            "catch (e) { return false; } }")


# Reads one value off the page for a "Capture" step: the element's text, an input's value, an attribute,
# or its tooltip (hover, then the visible role=tooltip text, else its title/aria-label). Whitespace is
# collapsed so line breaks and padding never cause a false mismatch. The generated test embeds this exact
# source, and the live replay execs it, so the two always read values the same way.
CAPTURE_HELPER_SRC = '''def capture_value(locator, source="text", attr=None):
    """Reads a value off the page: element text, input value, an attribute, or its tooltip."""
    if source == "value":
        v = locator.input_value()
    elif source == "attr":
        v = locator.get_attribute(attr or "title")
    elif source == "tooltip":
        locator.hover()
        tip = locator.page.locator('[role="tooltip"]').first
        try:
            tip.wait_for(state="visible", timeout=2000)
            v = tip.inner_text()
        except Exception:
            v = locator.get_attribute("title") or locator.get_attribute("aria-label")
    else:
        v = locator.inner_text()
    return " ".join((v or "").split())'''
exec(CAPTURE_HELPER_SRC)   # defines capture_value() for the live replay

COMPARE_OPS = {"equals": "==", "not_equals": "!=", "contains": "in", "not_contains": "not in"}


def compare_values(a, op, b):
    """`a` vs `b` for a Compare step. `contains` means: `a` contains `b`."""
    if op == "not_equals":
        return a != b
    if op == "contains":
        return b in a
    if op == "not_contains":
        return b not in a
    return a == b


def uses_capture(steps):
    return any(s.get("type") in ("capture", "compare") for s in steps)


def with_capture_support(funcs):
    """Gives each test function that captures/compares its own `captured = {}` dict."""
    out = []
    for fn, cname, lines, needs_b in funcs:
        if any(ln.startswith(("captured[", "assert compare_")) for ln in lines):
            lines = ["captured = {}"] + lines
        out.append((fn, cname, lines, needs_b))
    return out


def _position(step):
    pos = step.get("position") or {}
    return f'position={{"x": {int(pos.get("x", 0))}, "y": {int(pos.get("y", 0))}}}' if pos else ""


def _ms(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 1000


def slugify(name):
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_").lower()
    return slug or "ui_scenario"


def _locator(loc):
    kind = loc.get("kind")
    if kind == "pw":
        return "page." + loc["py"]          # Playwright's own generated expression (may use re.compile)
    if kind == "testid":
        expr = f"page.get_by_test_id({_s(loc['value'])})"
    elif kind == "role":
        expr = f"page.get_by_role({_s(loc['role'])}, name={_s(loc['name'])}, exact=True)"
    elif kind == "label":
        expr = f"page.get_by_label({_s(loc['value'])}, exact=True)"
    elif kind == "placeholder":
        expr = f"page.get_by_placeholder({_s(loc['value'])}, exact=True)"
    elif kind == "text":
        expr = f"page.get_by_text({_s(loc['value'])}, exact=True)"
    else:
        expr = f"page.locator({_s(loc.get('value'))})"
    if loc.get("nth") is not None:
        expr += f".nth({int(loc['nth'])})"
    return expr


def _py_label(py):
    """The human text inside a Playwright expression, e.g. get_by_role("textbox", name="Password") -> Password."""
    lits = re.findall(r'"((?:[^"\\]|\\.)*)"', py or "")
    lits = [x for x in lits if x not in _ROLES]
    return lits[0] if lits else ""


_ROLES = {"button", "link", "textbox", "checkbox", "radio", "combobox", "heading", "tab", "menuitem", "option",
          "switch", "searchbox", "slider", "listbox", "dialog", "img", "row", "cell", "listitem", "list"}


def _secret_var(loc, taken):
    base = (loc.get("name") or loc.get("anchor") or _py_label(loc.get("py")) or loc.get("value") or "SECRET")
    if base.startswith("xpath=") or base.startswith("#") or base.startswith("["):
        base = "SECRET"
    base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").upper()[:30] or "SECRET"
    var, n = f"UI_{base}", 1
    while var in taken:
        n += 1
        var = f"UI_{base}_{n}"
    return var


_PAGE_VAR = {"A": "page", "B": "page_b"}


def _for_portal(line, portal):
    """Retargets a rendered line from `page` to the portal's page fixture. Only the call
    target is rewritten (line start / inside expect(...)), never text inside string values."""
    var = _PAGE_VAR.get(portal, "page")
    if var == "page" or not line:
        return line
    if line.startswith("page."):
        return var + line[4:]
    if line.startswith("captured[") and "capture_value(page." in line:
        return line.replace("capture_value(page.", f"capture_value({var}.", 1)
    if line.startswith("expect(page)"):
        return "expect(" + var + line[len("expect(page"):]
    if line.startswith("expect(page."):
        return "expect(" + var + line[len("expect(page"):]
    return line


def _render_step(s, secrets):
    """One recorded step -> one line of test code (or None for steps that emit nothing)."""
    line = _render_step_page(s, secrets)
    return _for_portal(line, s.get("portal", "A"))


def _render_step_page(s, secrets, locfn=None):
    """`locfn(loc, step) -> expression` lets the page-object generator point a step at a named
    locator (e.g. `login.sign_in_button`) instead of the inline `page.get_by_...(...)`."""
    t, loc = s.get("type"), s.get("locator") or {}
    _locator_expr = (lambda l: locfn(l, s)) if (locfn and loc) else _locator
    if t == "goto":
        return f"page.goto({_s(s.get('value') or s.get('url'))})"
    if t == "back":
        return "page.go_back()"
    if t == "click":
        return f"{_locator_expr(loc)}.click()"
    if t == "fill":
        if s.get("secret"):
            var = _secret_var(loc, secrets)
            secrets[var] = s.get("value", "")
            return f'{_locator_expr(loc)}.fill(os.environ["{var}"])'
        return f"{_locator_expr(loc)}.fill({_s(s.get('value'))})"
    if t == "clear":
        return f"{_locator_expr(loc)}.clear()"
    if t == "select":
        return f"{_locator_expr(loc)}.select_option(label={_s(s.get('value'))})"
    if t == "check":
        return f"{_locator_expr(loc)}.check()"
    if t == "uncheck":
        return f"{_locator_expr(loc)}.uncheck()"
    if t == "press":
        return f"{_locator_expr(loc)}.press({_s(s.get('value'))})"
    if t == "press_page":
        return f"page.keyboard.press({_s(s.get('value'))})"
    if t == "assert_text":
        return f"expect({_locator_expr(loc)}).to_contain_text({_s(s.get('value'))})"
    if t == "assert_value":
        return f"expect({_locator_expr(loc)}).to_have_value({_s(s.get('value'))})"
    if t == "assert_visible":
        return f"expect({_locator_expr(loc)}).to_be_visible()"
    if t == "capture":
        return (f"captured[{_s(s.get('var'))}] = capture_value({_locator_expr(loc)}, {_s(s.get('source') or 'text')}"
                + (f", {_s(s.get('attr'))}" if s.get("source") == "attr" else "") + ")")
    if t == "compare":
        a, b = f"captured[{_s(s.get('left'))}]", f"captured[{_s(s.get('right'))}]"
        op = s.get("op") or "equals"
        expr = {"equals": f"{a} == {b}", "not_equals": f"{a} != {b}", "contains": f"{b} in {a}",
                "not_contains": f"{b} not in {a}"}.get(op, f"{a} == {b}")
        am, bm = (f"captured[{json.dumps(str(s.get(k)))[1:-1].join(chr(39) * 2)}]" for k in ("left", "right"))   # single-quoted keys: legal inside the f-string on older Pythons
        return f"assert {expr}, f\"compare {s.get('left')} {op} {s.get('right')}: {{{am}!r}} vs {{{bm}!r}}\""
    if t == "assert_url":
        return f"expect(page).to_have_url(re.compile(re.escape({_s(s.get('value'))})))"
    if t == "hover":
        return f"{_locator_expr(loc)}.hover({_position(s)})"
    if t == "assert_js":
        return f"page.wait_for_function({_s(assert_js_source(s.get('expr'), s.get('value')))})"
    if t == "scroll_into_view":
        return f"{_locator_expr(loc)}.scroll_into_view_if_needed()"
    if t == "wait_for":
        return f"{_locator_expr(loc)}.wait_for(state={_s(s.get('value') or 'visible')})"
    if t == "wait":
        return f"page.wait_for_timeout({_ms(s.get('value'))})"
    if t == "scroll_page":
        return f"page.evaluate({_s(_SCROLL_JS.get(s.get('value'), _SCROLL_JS['bottom']))})"
    if t == "assert_attr":
        return f"expect({_locator_expr(loc)}).to_have_attribute({_s(s.get('attr') or 'href')}, {_s(s.get('value'))})"
    if t == "note":
        return f"# NOTE: {(s.get('value') or '').replace(chr(10), ' ')}"
    return None


def split_cases(name, steps):
    """[(case name, [steps])] - a 'case' step starts a new test case; steps before the
    first one (if any) form a leading case named after the scenario."""
    cases, cur = [], None
    for s in steps:
        if s.get("type") == "case":
            cur = (s.get("value") or f"Case {len(cases) + 1}", [])
            cases.append(cur)
        else:
            if cur is None:
                cur = (name or "scenario", [])
                cases.append(cur)
            cur[1].append(s)
    cases = [c for c in cases if c[1]]
    # An opening page load that sits above the first split would become a test of its own
    # that only opens the page - fold it into the first real case instead.
    if len(cases) > 1 and all(st.get("type") in ("goto", "note") for st in cases[0][1]):
        cases[1] = (cases[1][0], cases[0][1] + cases[1][1])
        cases = cases[1:]
    return cases


def generate_ui_test(name, steps, timeout_s=15, shared_session=True):
    """Every action in the generated file auto-waits up to `timeout_s` (Playwright
    waits for the element to exist, be visible, stable and enabled before acting;
    assertions retry the same way), overridable per run with UI_TIMEOUT_MS."""
    slug = slugify(name)
    secrets = {}
    start_url = next((s.get("url") for s in steps if s.get("type") == "goto"), "")
    cases = split_cases(name, steps)
    multi = len(cases) > 1
    timeout_ms = max(1, int(float(timeout_s or 15) * 1000))

    uses_b = any(st.get("portal") == "B" for st in steps)
    funcs, used = [], set()
    opened = set()   # portals that have been navigated somewhere so far
    for n, (cname, csteps) in enumerate(cases, 1):
        lines = []
        if not shared_session:
            opened = set()   # fresh browser per case: every portal starts blank again
        for st in csteps:
            portal = st.get("portal", "A")
            if st.get("type") == "goto":
                opened.add(portal)
            elif portal not in opened and st.get("url") and st.get("type") not in ("wait", "note", "case"):
                # No explicit "Go to" before this step (e.g. steps appended later, or a fresh case):
                # open the portal on the page the step was recorded on, instead of a blank tab.
                lines.append(_for_portal(f"page.goto({_s(st['url'])})", portal))
                opened.add(portal)
            line = _render_step(st, secrets)
            if line:
                lines.append(line)
        fn = f"test_{slug}_{n:02d}_{slugify(cname)}" if multi else f"test_{slug}"
        while fn in used:
            fn += "_x"
        used.add(fn)
        needs_b = any(st.get("portal") == "B" for st in csteps)
        funcs.append((fn, cname, lines or ["pass"], needs_b))
    if not funcs:
        funcs.append((f"test_{slug}", name or slug, ["pass  # no steps were recorded"], False))
    funcs = with_capture_support(funcs)

    out = [
        f'"""UI test: {name or slug}',
        f"Recorded {datetime.now().strftime('%Y-%m-%d %H:%M')} from {start_url or 'the browser'}.",
        "",
        "Run:  python -m pytest <this file> -v -s",
        f"Env:  UI_TIMEOUT_MS (default {timeout_ms}) - how long each step waits for its element;",
        "      UI_HEADED=0 runs headless, UI_SLOWMO=<ms> slows each action down.",
    ]
    if uses_b:
        out.append("Two portals: `page` is Portal A and `page_b` is Portal B - separate browser contexts, so each keeps its own login.")
    if multi:
        out.append(f"{len(funcs)} test cases, run in order" +
                   (" in ONE shared browser session (each continues where the previous stopped)." if shared_session
                    else ", each in a fresh browser (later cases reopen the page they started on)."))
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
        f'TIMEOUT_MS = int(os.environ.get("UI_TIMEOUT_MS", "{timeout_ms}"))',
        "expect.set_options(timeout=TIMEOUT_MS)",
        "",
        "",
        *((CAPTURE_HELPER_SRC.splitlines() + ["", ""]) if uses_capture(steps) else []),
        '@pytest.fixture(scope="session")',
        "def browser():",
        "    with sync_playwright() as p:",
        '        b = p.chromium.launch(headless=os.environ.get("UI_HEADED", "1") == "0",',
        '                              slow_mo=int(os.environ.get("UI_SLOWMO", "0")))',
        "        yield b",
        "        b.close()",
        "",
        "",
        f'@pytest.fixture(scope="{scope}")',
        "def page(browser):",
        "    context = browser.new_context()",
        "    page = context.new_page()",
        "    page.set_default_timeout(TIMEOUT_MS)",
        "    page.set_default_navigation_timeout(max(TIMEOUT_MS, 30_000))",
        '    page.on("dialog", lambda d: d.accept())',
        "    yield page",
        "    context.close()",
    ]
    if uses_b:
        out += [
            "",
            "",
            f'@pytest.fixture(scope="{scope}")',
            "def page_b(browser):",
            "    # Portal B: its own context, so it is a separate session/login from `page` (Portal A)",
            "    context = browser.new_context()",
            "    page = context.new_page()",
            "    page.set_default_timeout(TIMEOUT_MS)",
            "    page.set_default_navigation_timeout(max(TIMEOUT_MS, 30_000))",
            '    page.on("dialog", lambda d: d.accept())',
            "    yield page",
            "    context.close()",
        ]
    for fn, cname, lines, needs_b in funcs:
        out += ["", "", f"def {fn}(page, page_b):" if needs_b else f"def {fn}(page):"]
        if multi:
            out.append(f"    {_s(cname)}")
        out += ["    " + ln for ln in lines]
    return "\n".join(out) + "\n", secrets


# ── Live replay (used by the recorder UI to verify steps before generating) ───
# Mirrors generate_ui_test() action for action, but executes against a real
# Playwright page instead of emitting source, so "replay passes" means the
# generated script's steps will resolve and act the same way.

def resolve_locator(page, loc):
    kind = loc.get("kind")
    if kind == "pw":
        return page.locator(loc["selector"])
    if kind == "testid":
        target = page.get_by_test_id(loc["value"])
    elif kind == "role":
        target = page.get_by_role(loc["role"], name=loc["name"], exact=True)
    elif kind == "label":
        target = page.get_by_label(loc["value"], exact=True)
    elif kind == "placeholder":
        target = page.get_by_placeholder(loc["value"], exact=True)
    elif kind == "text":
        target = page.get_by_text(loc["value"], exact=True)
    else:
        target = page.locator(loc.get("value") or "")
    if loc.get("nth") is not None:
        target = target.nth(int(loc["nth"]))
    return target


def replay_step(page, step, expect, captured=None):
    """Runs one recorded step. Raises on failure; `expect` is playwright's expect."""
    t, loc = step.get("type"), step.get("locator") or {}
    val = step.get("value")
    if t == "goto":
        page.goto(val or step.get("url"))
    elif t == "back":
        page.go_back(wait_until="domcontentloaded")
    elif t == "click":
        resolve_locator(page, loc).click()
    elif t == "fill":
        resolve_locator(page, loc).fill(val or "")
    elif t == "clear":
        resolve_locator(page, loc).clear()
    elif t == "select":
        resolve_locator(page, loc).select_option(label=val)
    elif t == "check":
        resolve_locator(page, loc).check()
    elif t == "uncheck":
        resolve_locator(page, loc).uncheck()
    elif t == "press":
        resolve_locator(page, loc).press(val)
    elif t == "press_page":
        page.keyboard.press(val)
    elif t == "assert_text":
        expect(resolve_locator(page, loc)).to_contain_text(val)
    elif t == "assert_value":
        expect(resolve_locator(page, loc)).to_have_value(val)
    elif t == "assert_visible":
        expect(resolve_locator(page, loc)).to_be_visible()
    elif t == "assert_url":
        expect(page).to_have_url(re.compile(re.escape(val or "")))
    elif t == "capture":
        got = capture_value(resolve_locator(page, loc), step.get("source") or "text", step.get("attr"))
        if captured is not None:
            captured[step.get("var")] = got
            captured["__saw__"] = got
    elif t == "compare":
        captured = captured if captured is not None else {}
        for k in (step.get("left"), step.get("right")):
            if k not in captured:
                raise AssertionError(f"Nothing was captured as '{k}' before this comparison")
        a, b, op = captured[step["left"]], captured[step["right"]], step.get("op") or "equals"
        captured["__saw__"] = f"{a!r} vs {b!r}"
        if not compare_values(a, op, b):
            raise AssertionError(f"{step['left']} {op.replace('_', ' ')} {step['right']} failed: {a!r} vs {b!r}")
    elif t == "hover":
        pos = step.get("position")
        resolve_locator(page, loc).hover(position={"x": pos["x"], "y": pos["y"]} if pos else None)
    elif t == "assert_js":
        page.wait_for_function(assert_js_source(step.get("expr"), val))
    elif t == "scroll_into_view":
        resolve_locator(page, loc).scroll_into_view_if_needed()
    elif t == "wait_for":
        resolve_locator(page, loc).wait_for(state=val or "visible")
    elif t == "wait":
        page.wait_for_timeout(_ms(val))
    elif t == "scroll_page":
        page.evaluate(_SCROLL_JS.get(val, _SCROLL_JS["bottom"]))
    elif t == "assert_attr":
        expect(resolve_locator(page, loc)).to_have_attribute(step.get("attr") or "href", val)
    # "note" and "case" steps are markers - nothing to run


def describe_failure(page, step, exc):
    """First line of the error, plus how many elements the locator matched -
    'matched 0' means a wrong locator, not a slow page."""
    msg = str(exc).strip().splitlines()[0][:300]
    loc = step.get("locator")
    if loc:
        try:
            n = resolve_locator(page, {**loc, "nth": None}).count()
            if loc.get("kind") == "pw":
                return msg + f"  [selector matched {n} element(s)]"
            msg += f"  [locator matched {n} element(s)" + (f", wanted #{loc['nth'] + 1}" if loc.get("nth") is not None else "") + "]"
        except Exception:
            pass
    return msg
