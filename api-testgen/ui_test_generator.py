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


def _secret_var(loc, taken):
    base = loc.get("name") or loc.get("value") or "SECRET"
    base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").upper()[:30] or "SECRET"
    var, n = f"UI_{base}", 1
    while var in taken:
        n += 1
        var = f"UI_{base}_{n}"
    return var


def _render_step(s, secrets):
    """One recorded step -> one line of test code (or None for steps that emit nothing)."""
    t, loc = s.get("type"), s.get("locator") or {}
    if t == "goto":
        return f"page.goto({_s(s.get('value') or s.get('url'))})"
    if t == "click":
        return f"{_locator(loc)}.click()"
    if t == "fill":
        if s.get("secret"):
            var = _secret_var(loc, secrets)
            secrets[var] = s.get("value", "")
            return f'{_locator(loc)}.fill(os.environ["{var}"])'
        return f"{_locator(loc)}.fill({_s(s.get('value'))})"
    if t == "clear":
        return f"{_locator(loc)}.clear()"
    if t == "select":
        return f"{_locator(loc)}.select_option(label={_s(s.get('value'))})"
    if t == "check":
        return f"{_locator(loc)}.check()"
    if t == "uncheck":
        return f"{_locator(loc)}.uncheck()"
    if t == "press":
        return f"{_locator(loc)}.press({_s(s.get('value'))})"
    if t == "press_page":
        return f"page.keyboard.press({_s(s.get('value'))})"
    if t == "assert_text":
        return f"expect({_locator(loc)}).to_contain_text({_s(s.get('value'))})"
    if t == "assert_value":
        return f"expect({_locator(loc)}).to_have_value({_s(s.get('value'))})"
    if t == "assert_visible":
        return f"expect({_locator(loc)}).to_be_visible()"
    if t == "hover":
        return f"{_locator(loc)}.hover()"
    if t == "scroll_into_view":
        return f"{_locator(loc)}.scroll_into_view_if_needed()"
    if t == "wait_for":
        return f"{_locator(loc)}.wait_for(state={_s(s.get('value') or 'visible')})"
    if t == "wait":
        return f"page.wait_for_timeout({_ms(s.get('value'))})"
    if t == "scroll_page":
        return f"page.evaluate({_s(_SCROLL_JS.get(s.get('value'), _SCROLL_JS['bottom']))})"
    if t == "assert_attr":
        return f"expect({_locator(loc)}).to_have_attribute({_s(s.get('attr') or 'href')}, {_s(s.get('value'))})"
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

    funcs, used = [], set()
    for n, (cname, csteps) in enumerate(cases, 1):
        lines = []
        # Fresh-session mode: every case after the first starts from the page it began on.
        if multi and not shared_session and n > 1 and csteps[0].get("type") != "goto" and csteps[0].get("url"):
            lines.append(f"page.goto({_s(csteps[0]['url'])})")
        lines += [ln for ln in (_render_step(s, secrets) for s in csteps) if ln]
        fn = f"test_{slug}_{n:02d}_{slugify(cname)}" if multi else f"test_{slug}"
        while fn in used:
            fn += "_x"
        used.add(fn)
        funcs.append((fn, cname, lines or ["pass"]))
    if not funcs:
        funcs.append((f"test_{slug}", name or slug, ["pass  # no steps were recorded"]))

    out = [
        f'"""UI test: {name or slug}',
        f"Recorded {datetime.now().strftime('%Y-%m-%d %H:%M')} from {start_url or 'the browser'}.",
        "",
        "Run:  python -m pytest <this file> -v -s",
        f"Env:  UI_TIMEOUT_MS (default {timeout_ms}) - how long each step waits for its element;",
        "      UI_HEADED=0 runs headless, UI_SLOWMO=<ms> slows each action down.",
    ]
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
    for fn, cname, lines in funcs:
        out += ["", "", f"def {fn}(page):"]
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


def replay_step(page, step, expect):
    """Runs one recorded step. Raises on failure; `expect` is playwright's expect."""
    t, loc = step.get("type"), step.get("locator") or {}
    val = step.get("value")
    if t == "goto":
        page.goto(val or step.get("url"))
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
    elif t == "hover":
        resolve_locator(page, loc).hover()
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
