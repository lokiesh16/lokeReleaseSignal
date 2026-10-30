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

# "Assert date/time" steps: read the element's text, find the date and/or time in it, and check it against the clock
# of the machine running the test. Modes: time_now (just the time of day), datetime_now (date AND time),
# date_today (just the date). Displayed times have no seconds, so both clocks are cut to the minute and
# `tol` minutes of slack is allowed either way. The generated test embeds this exact source, and the live replay execs it.
DATETIME_MODES = {
    "time_now": "time is now (just the time)",
    "datetime_now": "date and time are now",
    "date_today": "date is today (just the date)",
}
DATETIME_HELPER_SRC = '''def assert_datetime(locator, mode="datetime_now", tol=2):
    """Checks a date/time shown on the page against the current time. Returns what it read, for the report."""
    import re
    from datetime import datetime, timedelta
    raw = locator.evaluate("e => /^(INPUT|TEXTAREA)$/.test(e.tagName) ? e.value : e.innerText")
    text = " ".join((raw or "").split())
    now = datetime.now().replace(second=0, microsecond=0)
    mons = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}

    def read_time():
        m = re.search(r"\\b(\\d{1,2}):(\\d{2})(?::\\d{2})?\\s*([AaPp])\\.?[Mm]\\.?", text)
        if m:
            return int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0), int(m.group(2))
        m = re.search(r"\\b([01]?\\d|2[0-3]):([0-5]\\d)(?::[0-5]\\d)?\\b", text)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def read_date():
        low = text.lower()
        if re.search(r"\\btoday\\b", low):
            return now.date()
        if re.search(r"\\byesterday\\b", low):
            return (now - timedelta(days=1)).date()
        try:
            m = re.search(r"\\b(\\d{4})-(\\d{2})-(\\d{2})\\b", text)
            if m:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            m = re.search(r"\\b(\\d{1,2})/(\\d{1,2})/(\\d{4})\\b", text)
            if m:
                return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
            m = re.search(r"\\b([A-Za-z]{3,9})\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+(\\d{4})\\b", text)
            if m and m.group(1)[:3].lower() in mons:
                return datetime(int(m.group(3)), mons[m.group(1)[:3].lower()], int(m.group(2))).date()
            m = re.search(r"\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+([A-Za-z]{3,9})\\.?,?\\s+(\\d{4})\\b", text)
            if m and m.group(2)[:3].lower() in mons:
                return datetime(int(m.group(3)), mons[m.group(2)[:3].lower()], int(m.group(1))).date()
        except ValueError:
            return None
        return None

    if mode == "date_today":
        d = read_date()
        assert d is not None, f"no date found in {text!r}"
        assert d == now.date(), f"date {d} is not today ({now.date()}); read {text!r}"
        return f"read {text!r}: date is today"
    t = read_time()
    assert t is not None, f"no time found in {text!r}"
    if mode == "time_now":
        shown = now.replace(hour=t[0], minute=t[1])
        diff = min(abs((shown + timedelta(days=k) - now).total_seconds()) for k in (-1, 0, 1)) / 60
    else:
        d = read_date()
        assert d is not None, f"no date found in {text!r}"
        diff = abs((datetime(d.year, d.month, d.day, t[0], t[1]) - now).total_seconds()) / 60
    assert diff <= tol, f"{text!r} is {diff:.0f} min from now ({now:%Y-%m-%d %H:%M}); allowed {tol} min"
    return f"read {text!r}: {diff:.0f} min from now (allowed {tol})"'''
exec(DATETIME_HELPER_SRC)   # defines assert_datetime() for the live replay


# "Assert table" steps check a table by what it SAYS, never by position: how many rows, which column headers, single cells
# (the row is found by a key cell, the column by its header text) and numbers elsewhere on the page that must equal the row
# count (e.g. a card saying "1 In Review"). It re-reads the table for `timeout` seconds so a table that is still loading is
# not judged half-filled. The generated test embeds this exact source, and the live replay execs it.
TABLE_CELL_OPS = {"equals": "equals", "contains": "contains", "pattern": "matches pattern", "exists": "exists"}
TABLE_HELPER_SRC = r'''def assert_table(table, rows=None, cols=None, headers=(), ordered=False, cells=(), refs=(), timeout=10):
    """Checks a table / grid by its content. rows=("exact"|"min"|"max", n); cols=n header cells; headers=[...] must be present
    (in that order when ordered); cells=[(key_col, key, col, op, value)]: the row whose `key_col` cell contains `key`, then its
    `col` cell must equal / contain / match / (just exist). refs=[(locator, "first"|"last", "=="|">="|"<=")]: the number in that
    element's text is compared with the number of rows. Returns a one-line summary of what it saw."""
    import re, time
    read = """
    (root) => {
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const KIND = 'table,[role="table"],[role="grid"],[role="treegrid"]';
      let t = root;
      if (!t.matches(KIND)) { const inner = t.querySelector(KIND); if (inner) t = inner; }
      const shown = e => e.getClientRects().length > 0;
      const cellsOf = r => Array.from(r.children).filter(c => /^(TD|TH)$/.test(c.tagName) || /^(cell|gridcell|rowheader|columnheader)$/.test(c.getAttribute('role') || ''));
      const isHead = c => c.getAttribute('role') === 'columnheader' || (c.tagName === 'TH' && c.getAttribute('scope') !== 'row' && c.getAttribute('role') !== 'rowheader');
      let headers = [], rows = [], body = false, empty = '';
      Array.from(t.querySelectorAll('tr,[role="row"]')).filter(r => r.closest(KIND) === t && shown(r)).forEach(r => {
        const cs = cellsOf(r);
        if (!cs.length) return;
        if (!body && cs.every(isHead)) { headers = cs.map(c => norm(c.innerText)); return; }
        body = true;
        if (cs.length === 1 && cs[0].colSpan > 1) { empty = norm(cs[0].innerText); return; }
        rows.push(cs.map(c => norm(c.innerText)));
      });
      return { headers: headers, rows: rows, empty: empty };
    }"""

    def norm(x):
        return " ".join((x or "").split())

    def column(hs, name):
        want = norm(name).lower()
        for i, h in enumerate(hs):
            if h.lower() == want:
                return i
        raise AssertionError(f"no column headed {name!r} (columns: {[h or '(blank)' for h in hs]})")

    def check():
        d = table.evaluate(read)
        hs, rs = d["headers"], d["rows"]
        n = len(rs)
        saw = [f"{n} row(s)", f"{len(hs)} column(s)"]
        if rows:
            mode, want = rows[0], int(rows[1])
            ok = {"exact": n == want, "min": n >= want, "max": n <= want}[mode]
            assert ok, f"table has {n} row(s), expected {'exactly' if mode == 'exact' else 'at ' + mode + 'imum'} {want}" + (f" (it says {d['empty']!r})" if d["empty"] and not n else "")
        if cols is not None:
            assert len(hs) == int(cols), f"table has {len(hs)} column(s), expected {cols}: {[h or '(blank)' for h in hs]}"
        if headers:
            low = [h.lower() for h in hs]
            missing = [h for h in headers if norm(h).lower() not in low]
            assert not missing, f"missing column(s) {missing}; the table has {[h or '(blank)' for h in hs]}"
            if ordered:
                at = [low.index(norm(h).lower()) for h in headers]
                assert at == sorted(at), f"columns are in a different order: {[h or '(blank)' for h in hs]}"
        for key_col, key, col, op, val in cells:
            ki, ci = column(hs, key_col), column(hs, col)
            hit = [r for r in rs if ki < len(r) and norm(key) in r[ki]]
            assert hit, f"no row has {key!r} in its {key_col!r} cell ({n} row(s) read)"
            assert len(hit) == 1, f"{len(hit)} rows have {key!r} in their {key_col!r} cell - the key must identify one row"
            got = hit[0][ci] if ci < len(hit[0]) else ""
            if op == "equals":
                assert got == norm(val), f"{col!r} of the row {key!r} is {got!r}, expected {val!r}"
            elif op == "contains":
                assert norm(val) in got, f"{col!r} of the row {key!r} is {got!r}, expected it to contain {val!r}"
            elif op == "pattern":
                assert re.search(val, got), f"{col!r} of the row {key!r} is {got!r}, expected it to match {val}"
            saw.append(f"{col} of {key!r} = {got[:40]!r}")
        for ref, pick, op in refs:
            text = norm(ref.inner_text())
            nums = re.findall(r"\d[\d,]*", text)
            assert nums, f"the reference element has no number in it: {text!r}"
            num = int(nums[-1 if pick == "last" else 0].replace(",", ""))
            ok = {"==": n == num, ">=": n >= num, "<=": n <= num}.get(op, n == num)
            assert ok, f"table has {n} row(s) but the reference says {num} ({text[:60]!r}); expected rows {op} {num}"
            saw.append(f"reference {num} {op} rows")
        return "; ".join(saw), (n, tuple(hs))

    deadline = time.time() + timeout
    while True:
        try:
            summary, shape = check()
            time.sleep(0.3)                                   # passed: make sure it was not a half-loaded moment
            if check()[1] == shape:
                return summary
        except AssertionError:
            if time.time() >= deadline:
                raise
        time.sleep(0.4)'''
exec(TABLE_HELPER_SRC)   # defines assert_table() for the live replay


# Chart data points (SVG circles) are found by their place ON the chart - column = rank of the x position among the chart's
# points, row = rank of the y position within that column - never by their place in the DOM. Charts re-render when hovered and
# put the hovered point in a different spot of the DOM, so a DOM position changes from one hover to the next.
CHARTPOINT_HELPER_SRC = '''def chart_point(page, chart, tag="circle", col=0, row=0):
    """A data point of a chart, found by where it is drawn (column / row) rather than by its position in the page's HTML."""
    scope = page.locator(chart)
    scope.locator(tag).first.wait_for(state="attached")
    pts = scope.locator(tag).evaluate_all(
        "els => els.filter(e => e.getAttribute('fill') !== 'none' && e.getAttribute('fill-opacity') !== '0')"
        ".map(e => [e.getAttribute('cx'), e.getAttribute('cy')])")
    xs = sorted({x for x, y in pts if x is not None}, key=float)
    assert xs, f"no data points found in the chart {chart!r}"
    x = xs[min(col, len(xs) - 1)]
    ys = sorted({y for px, y in pts if px == x}, key=float)
    y = ys[min(row, len(ys) - 1)]
    return scope.locator(f'{tag}[cx="{x}"][cy="{y}"]:not([fill="none"]):not([fill-opacity="0"])').last'''
exec(CHARTPOINT_HELPER_SRC)   # defines chart_point() for the live replay


# "Run script" steps: a Python file (cleanup, test-data setup, a MongoDB query...) that runs at that point of the flow. It is run as its own
# process, so it can use any package; exit code 0 = fine, anything else fails the step with the script's output. The generated test
# finds the file in scripts/ next to itself and a .env above it (MONGO_URI and the like), so secrets never live in the script.
SCRIPT_HELPER_SRC = '''def run_script(name, args="", timeout=120, check=True):
    """Runs scripts/<name>.py and returns what it printed. Fails the test if it exits non-zero (unless check=False)."""
    import os
    import shlex
    import subprocess
    import sys
    from pathlib import Path
    folder = Path(globals().get("SCRIPTS_DIR") or (Path(__file__).resolve().parent / "scripts"))
    path = folder / f"{name}.py"
    assert path.is_file(), f"script {name!r} not found at {path}"
    env = dict(os.environ)
    for d in [path.resolve().parent, *path.resolve().parents]:     # a .env found above the scripts supplies MONGO_URI etc.
        envfile = d / ".env"
        if envfile.is_file():
            for line in envfile.read_text().splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip() and not k.lstrip().startswith("#"):
                    env.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("\\"'"))
            break
    py = globals().get("SCRIPT_PYTHON") or env.get("SCRIPTS_PYTHON") or sys.executable     # SCRIPTS_PYTHON (in .env): e.g. the test project\'s venv
    runner = Path(globals().get("SCRIPT_RUNNER") or (folder / "_runner.py"))      # maps arguments to a function's parameters and connects `db`; plain scripts run as they are
    builtin = globals().get("SCRIPT_COMMAND")        # the packaged app runs scripts with its own built-in Python
    cmd = [*builtin, str(path)] if builtin else ([py, str(runner), str(path)] if runner.is_file() else [py, str(path)])
    r = subprocess.run([*cmd, *shlex.split(args or "")], capture_output=True, text=True, timeout=int(timeout), env=env, cwd=str(path.parent))
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print(f"[script {name}] exit {r.returncode}" + (f"\\n{out}" if out else ""))
    if check:
        assert r.returncode == 0, f"script {name!r} failed (exit {r.returncode}):\\n{out[-2000:]}"
    return f"exit {r.returncode}" + (f": {out[-600:]}" if out else "")'''
# The runner that sits next to the scripts (as _runner.py) and runs them: connects `db`, maps arguments to a function's parameters,
# prints what it returns. Stdlib only, so it works with whichever Python SCRIPTS_PYTHON names. Also imported here for describe().
SCRIPT_RUNNER_SRC = r'''"""Runs a script file for a "Run script" step:   python _runner.py <script.py> [arguments...]

A script is either plain Python (top-level code runs as written) or "function style": only imports, constants and defs.
Function style: the last def (or the one named run) is called, its parameters are filled from the arguments
(--partner_id 6a5d...  /  partner_id=6a5d...  /  by position), and what it returns is printed.
Ready to use without importing: db, client, env, ObjectId, os, json, re, datetime, timedelta, DRY_RUN.
The database is the one named by MONGO_URI (or MONGODB_URI) and DB_NAME in the .env file (nothing else is read).
"""
import ast
import datetime
import inspect
import json
import os
import re
import runpy
import sys
import traceback
import warnings

warnings.filterwarnings("ignore", message=r".*urllib3 v2 only supports OpenSSL.*")   # noise from Pythons built with LibreSSL


def _fail_short(error, path):
    """End the step with just the script's own lines and the error, not the runner's or a library's call stack."""
    mine = os.path.basename(path)
    for fr in [f for f in traceback.extract_tb(error.__traceback__) if os.path.basename(f.filename) == mine][-3:]:
        print(f"  in {mine} line {fr.lineno}: {fr.line}", file=sys.stderr)
    sys.exit(f"{type(error).__name__}: {error}")


class Lazy:
    """Stands in for something that is only created when a script first touches it (so scripts that never use the database never connect)."""

    def __init__(self, make, what):
        self.__dict__.update(_make=make, _what=what, _obj=None)

    def _real(self):
        if self.__dict__["_obj"] is None:
            self.__dict__["_obj"] = self.__dict__["_make"]()
        return self.__dict__["_obj"]

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._real(), name)

    def __getitem__(self, key):
        return self._real()[key]

    def __repr__(self):
        return f"<{self.__dict__['_what']}: connects when first used>"


def env(name, default=None):
    """A value from the .env file (or the environment). Stops with a clear message when it is missing and has no default."""
    value = os.getenv(name)
    if value:
        return value
    if default is not None:
        return default
    raise RuntimeError(f"{name} is not set - add it to the .env file")


def _make_client():
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")          # both spellings are in common use
    if not uri:
        raise RuntimeError("MONGO_URI (or MONGODB_URI) is not set - add it to the .env file")
    if "prod" in uri.lower():
        raise RuntimeError("Refusing to connect: the MongoDB URI looks like production")
    try:
        from pymongo import MongoClient
    except ImportError:
        raise RuntimeError("pymongo is not available in this Python (the packaged app includes it; from source: pip install pymongo)")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)


def _make_db():
    name = env("DB_NAME")
    if "prod" in name.lower():
        raise RuntimeError("Refusing to use a database that looks like production")
    return client.get_database(name)


try:
    from bson import ObjectId
except ImportError:
    def ObjectId(*_a, **_k):
        raise RuntimeError("pymongo is not available in this Python (the packaged app includes it; from source: pip install pymongo)")

client = Lazy(_make_client, "MongoDB client")
db = Lazy(_make_db, "MongoDB database")


def _is_declaration(node):
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
        return True
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)   # a docstring


def describe(src):
    """What the console shows for a script: its style, the function a step will call and that function's parameters."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"style": "error", "entry": None, "params": [], "doc": "", "error": f"line {e.lineno}: {e.msg}"}
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    doc = (ast.get_docstring(tree) or "").strip()
    has_main_guard = any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name)
                         and n.test.left.id == "__name__" for n in tree.body)
    if not has_main_guard and (any(f.name.startswith("test") for f in funcs)
                               or any(isinstance(n, ast.ClassDef) and n.name.startswith("Test") for n in tree.body)):
        return {"style": "pytest", "entry": None, "params": [], "doc": doc}      # a generated pytest file: run with pytest
    if not funcs or not all(_is_declaration(n) for n in tree.body):
        return {"style": "plain", "entry": None, "params": [], "doc": doc}
    named = [f for f in funcs if f.name == "run"]
    entry = named[0] if named else ([f for f in funcs if not f.name.startswith("_")] or funcs)[-1]
    a = entry.args
    positional = a.posonlyargs + a.args
    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    rows = [(p, d, d is not None) for p, d in zip(positional, defaults)] + [(p, d, d is not None) for p, d in zip(a.kwonlyargs, a.kw_defaults)]

    def text(node):
        try:
            return ast.unparse(node)
        except Exception:
            return ""
    params = [{"name": p.arg, "required": not has, "default": text(d) if has else None, "type": text(p.annotation) if p.annotation else ""}
              for p, d, has in rows]
    return {"style": "function", "entry": entry.name, "params": params,
            "doc": doc or (ast.get_docstring(entry) or "").strip()}


def _parse_args(tokens):
    named, positional, i = {}, [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--") and len(t) > 2:
            key = t[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                named[k.replace("-", "_")] = v
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                named[key.replace("-", "_")] = tokens[i + 1]
                i += 1
            else:
                named[key.replace("-", "_")] = True
        elif re.match(r"^[A-Za-z_]\w*=", t):
            k, v = t.split("=", 1)
            named[k] = v
        else:
            positional.append(t)
        i += 1
    return named, positional


def _convert(value, annotation, name):
    if value is True or annotation in (inspect.Parameter.empty, None, "", "str", str):
        return value
    kind = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
    try:
        if kind == "ObjectId":
            return ObjectId(value)
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "bool":
            return str(value).lower() in ("1", "true", "yes", "y", "on")
        if kind in ("list", "dict"):
            return json.loads(value)
    except Exception:
        sys.exit(f"Argument {name}: cannot read {value!r} as {kind}")
    return value


def main(argv):
    if len(argv) < 2:
        sys.exit("usage: _runner.py <script.py> [arguments...]")
    path, tokens = argv[1], argv[2:]
    sys.argv = [path] + tokens                       # plain scripts read sys.argv as usual
    dry = "--dry-run" in tokens
    tokens = [t for t in tokens if t != "--dry-run"]
    with open(path, encoding="utf-8") as f:
        info = describe(f.read())
    if info["style"] == "error":
        sys.exit(f"Syntax error in the script, {info['error']}")
    injected = {"db": db, "client": client, "env": env, "ObjectId": ObjectId, "os": os, "json": json, "re": re,
                "datetime": datetime.datetime, "timedelta": datetime.timedelta, "DRY_RUN": dry}
    if info["style"] == "pytest":
        try:
            import pytest
        except ImportError:
            sys.exit("This is a pytest file and pytest is not available here - set SCRIPTS_PYTHON in .env to a Python that has pytest")
        sys.exit(int(pytest.main([path, "-q", "-s", "--tb=short", "-p", "no:cacheprovider", *tokens])))
    if info["style"] == "plain":
        try:
            runpy.run_path(path, init_globals=injected, run_name="__main__")
        except SystemExit:
            raise
        except Exception as e:
            _fail_short(e, path)
        return
    fn = runpy.run_path(path, init_globals=injected, run_name="script")[info["entry"]]
    named, positional = _parse_args(tokens)
    kwargs, missing = {}, []
    for p in inspect.signature(fn).parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.name in named:
            value = named.pop(p.name)
        elif positional:
            value = positional.pop(0)
        elif p.default is not p.empty:
            continue
        else:
            missing.append(p.name)
            continue
        kwargs[p.name] = _convert(value, p.annotation, p.name)
    if missing:
        sys.exit(f"{info['entry']}() needs: " + ", ".join(f"--{m} VALUE" for m in missing) + "   (type them in the step's Arguments box)")
    print(f"run {info['entry']}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})" + ("   [dry run]" if dry else ""))
    try:
        result = fn(**kwargs)
        if inspect.iscoroutine(result):
            import asyncio
            result = asyncio.run(result)
    except SystemExit:
        raise
    except Exception as e:
        _fail_short(e, path)
    if result is False:
        sys.exit(f"{info['entry']}() returned False")
    if isinstance(result, (dict, list, tuple)):
        print("result:", json.dumps(result, default=str, indent=2))
    elif result is not None:
        print("result:", result)


if __name__ == "__main__":
    main(sys.argv)
'''
_runner_ns = {"__name__": "_script_runner"}
exec(compile(SCRIPT_RUNNER_SRC, "_runner.py", "exec"), _runner_ns)
describe_script = _runner_ns["describe"]     # (source) -> {"style": "function"|"plain"|"error", "entry", "params", "doc"}

SCRIPTS_DIR = None       # the bridge points these at its own folder / interpreter / runner for the live replay
SCRIPT_PYTHON = None
SCRIPT_RUNNER = None
SCRIPT_COMMAND = None
exec(SCRIPT_HELPER_SRC)  # defines run_script() for the live replay


def uses_script(steps):
    return any(s.get("type") == "script" for s in steps)


def script_names(steps):
    return sorted({s.get("script") for s in steps if s.get("type") == "script" and s.get("script")})


def uses_chartpoint(steps):
    return any((s.get("locator") or {}).get("kind") == "chartpoint" for s in steps)


def _tol(step):
    try:
        return max(0, min(1440, int(float(step.get("tol", 2)))))
    except (TypeError, ValueError):
        return 2


def uses_datetime(steps):
    return any(s.get("type") == "assert_datetime" for s in steps)


def uses_table(steps):
    return any(s.get("type") == "assert_table" for s in steps)


def table_spec(s):
    """An assert_table step's settings, cleaned up: the keyword arguments for assert_table() minus the locators."""
    mode = s.get("rows_mode") if s.get("rows_mode") in ("exact", "min", "max") else None
    try:
        n = max(0, int(float(s.get("rows_n"))))
    except (TypeError, ValueError):
        n, mode = 0, None
    try:
        cols = max(0, int(float(s.get("cols")))) if s.get("cols") not in (None, "") else None
    except (TypeError, ValueError):
        cols = None
    cells = [(str(c.get("key_col") or ""), str(c.get("key") or ""), str(c.get("col") or ""),
              c.get("op") if c.get("op") in TABLE_CELL_OPS else "equals", str(c.get("value") or ""))
             for c in (s.get("cells") or []) if c.get("key_col") and c.get("col")]
    refs = [(r.get("locator") or {}, "last" if r.get("pick") == "last" else "first", r.get("op") if r.get("op") in ("==", ">=", "<=") else "==")
            for r in (s.get("refs") or []) if r.get("locator")]
    return {"rows": (mode, n) if mode else None, "cols": cols, "headers": [str(h) for h in (s.get("headers") or []) if str(h).strip()],
            "ordered": bool(s.get("ordered")), "cells": cells, "refs": refs}


def table_summary(s):
    """The checks of an assert_table step in words, for reports and the step list."""
    sp, out = table_spec(s), []
    if sp["rows"]:
        out.append({"exact": "exactly", "min": "at least", "max": "at most"}[sp["rows"][0]] + f" {sp['rows'][1]} row(s)")
    if sp["cols"] is not None:
        out.append(f"{sp['cols']} column(s)")
    if sp["headers"]:
        out.append(f"{len(sp['headers'])} header(s)" + (" in order" if sp["ordered"] else ""))
    if sp["cells"]:
        out.append(f"{len(sp['cells'])} cell check(s)")
    if sp["refs"]:
        out.append(f"rows match {len(sp['refs'])} reference number(s)")
    return ", ".join(out) or "nothing selected yet"


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


def _script_timeout(step):
    try:
        return max(1, min(1800, int(float(step.get("timeout", 120)))))
    except (TypeError, ValueError):
        return 120


def _force(loc):
    """Chart shapes (data-point circles, bars) sit under an invisible overlay that catches the mouse, which makes Playwright's
    'is something covering it?' check time out. Locators flagged `force` skip that check; the event still goes to the same spot."""
    if not loc:
        return False
    if loc.get("force"):
        return True
    # an edited selector loses the flag, so recognise "<svg> >> circle" style chart-shape selectors too
    return bool(re.search(r"\bsvg\b.*>>\s*(circle|ellipse|path|rect|line|polyline|polygon)\b", loc.get("selector") or loc.get("py") or ""))


def _args(*parts):
    return ", ".join(p for p in parts if p)


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
    elif kind == "chartpoint":
        return f"chart_point(page, {_s(loc['scope'])}, {_s(loc.get('tag') or 'circle')}, {int(loc.get('col', 0))}, {int(loc.get('row', 0))})"
    elif kind == "pattern":   # matched by the SHAPE of the text (e.g. 12:34 PM), so a time that changes every run still resolves
        root = f"page.locator({_s(loc['scope'])})" if loc.get("scope") else "page"
        expr = f"{root}.get_by_text(re.compile({_s(loc['value'])}))"
    else:
        expr = f"page.locator({_s(loc.get('value'))})"
    if loc.get("nth") is not None:
        n = int(loc["nth"])
        expr += ".last" if n == -1 else ".first" if n == 0 else f".nth({n})"
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
    if line.startswith("chart_point(page,"):
        return "chart_point(" + var + line[len("chart_point(page"):]
    if line.startswith("assert_table(page."):
        return "assert_table(" + var + line[len("assert_table(page"):]
    if line.startswith("assert_datetime(page."):
        return "assert_datetime(" + var + line[len("assert_datetime(page"):]
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
    if t == "reload":
        return "page.reload()"
    if t == "script":
        extra = (f", args={_s(s.get('args'))}" if s.get("args") else "") + (", check=False" if s.get("continue") else "")
        return f"run_script({_s(s.get('script'))}{extra}, timeout={_script_timeout(s)})"
    if t == "click":
        return f"{_locator_expr(loc)}.click({'force=True' if _force(loc) else ''})"
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
    if t == "assert_datetime":
        mode = s.get("mode") if s.get("mode") in DATETIME_MODES else "datetime_now"
        return f"assert_datetime({_locator_expr(loc)}, {_s(mode)}, {_tol(s)})"
    if t == "assert_table":
        sp, pv = table_spec(s), _PAGE_VAR.get(s.get("portal", "A"), "page")
        on = lambda e: pv + e[4:] if e.startswith("page.") else e   # refs are inline expressions; point them at this step's window
        parts = [_locator_expr(loc)]
        if sp["rows"]:
            parts.append(f"rows=({_s(sp['rows'][0])}, {sp['rows'][1]})")
        if sp["cols"] is not None:
            parts.append(f"cols={sp['cols']}")
        if sp["headers"]:
            parts.append("headers=" + json.dumps(sp["headers"]))
            if sp["ordered"]:
                parts.append("ordered=True")
        if sp["cells"]:
            parts.append("cells=[" + ", ".join("(" + ", ".join(_s(x) for x in c) + ")" for c in sp["cells"]) + "]")
        if sp["refs"]:
            parts.append("refs=[" + ", ".join(f"({on(_locator(l))}, {_s(pk)}, {_s(op)})" for l, pk, op in sp["refs"]) + "]")
        return "assert_table(" + ", ".join(parts) + ")"
    if t == "assert_url":
        return f"expect(page).to_have_url(re.compile(re.escape({_s(s.get('value'))})))"
    if t == "hover":
        return f"{_locator_expr(loc)}.hover({_args(_position(s), 'force=True' if _force(loc) else '')})"
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
        *((DATETIME_HELPER_SRC.splitlines() + ["", ""]) if uses_datetime(steps) else []),
        *((TABLE_HELPER_SRC.splitlines() + ["", ""]) if uses_table(steps) else []),
        *((SCRIPT_HELPER_SRC.splitlines() + ["", ""]) if uses_script(steps) else []),
        *((CHARTPOINT_HELPER_SRC.splitlines() + ["", ""]) if uses_chartpoint(steps) else []),
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
    if kind == "chartpoint":
        return chart_point(page, loc["scope"], loc.get("tag") or "circle", int(loc.get("col", 0)), int(loc.get("row", 0)))
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
    elif kind == "pattern":
        target = (page.locator(loc["scope"]) if loc.get("scope") else page).get_by_text(re.compile(loc["value"]))
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
    elif t == "reload":
        page.reload(wait_until="domcontentloaded")
    elif t == "script":
        out = run_script(step.get("script") or "", step.get("args") or "", _script_timeout(step), not step.get("continue"))
        if captured is not None:
            captured["__saw__"] = out
    elif t == "click":
        resolve_locator(page, loc).click(force=_force(loc))
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
    elif t == "assert_datetime":
        mode = step.get("mode") if step.get("mode") in DATETIME_MODES else "datetime_now"
        saw = assert_datetime(resolve_locator(page, loc), mode, _tol(step))
        if captured is not None:
            captured["__saw__"] = saw
    elif t == "assert_table":
        sp = table_spec(step)
        saw = assert_table(resolve_locator(page, loc), rows=sp["rows"], cols=sp["cols"], headers=sp["headers"], ordered=sp["ordered"],
                           cells=sp["cells"], refs=[(resolve_locator(page, l), pk, op) for l, pk, op in sp["refs"]])
        if captured is not None:
            captured["__saw__"] = saw
    elif t == "hover":
        pos = step.get("position")
        resolve_locator(page, loc).hover(position={"x": pos["x"], "y": pos["y"]} if pos else None, force=_force(loc))
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
    if step.get("type") == "script":      # the script's own output is the useful part: keep it
        return " | ".join(l.strip() for l in str(exc).strip().splitlines() if l.strip())[:600]
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
