# -*- coding: utf-8 -*-
"""
API Test Generator
Captures live network traffic from a Playwright-driven browser, lets you pick
which API calls matter, detects value-chaining between them, and asks an LLM
to generate a requests + pytest test suite for the chain.

Run:  python server.py
Open: http://localhost:5002

Derived from testgenforWeb_v2's network-capture + API-chain-generation
pipeline, trimmed down to an API-only tool (no DOM capture, no UI script
generation, no codebase analyzer).
"""

import os
import re
import ast
import sys
import json
import time
import html
import queue
import shlex
import shutil
import tempfile
import threading
import subprocess
import uuid
import base64
import urllib.request
import urllib.error
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit

from api_chain_analyzer import detect_api_chains
from ui_test_generator import generate_ui_test, slugify, replay_step, resolve_locator, describe_failure


def _app_dir():
    """Where this app's writable/user data lives - next to the actual
    executable when frozen (so generated_scripts/, sessions/, and the
    editable prompt_config.json land somewhere permanent instead of a
    throwaway extraction temp dir), or this script's own folder when run
    from source. Same split desktop_agent.py uses for mobile_agent."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundled_dir():
    """Where PyInstaller extracts this build's bundled READ-ONLY data
    files (the `datas` entries in the .spec) at runtime - falls back to
    this script's own folder for a normal `python server.py` run, where
    the source tree already doubles as "the bundle"."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


BASE_DIR            = _app_dir()
os.chdir(BASE_DIR)
BUNDLED_DIR         = _bundled_dir()

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    # A frozen build extracts to a FRESH temp dir every launch (sys._MEIPASS),
    # so Playwright's own driver - bundled here via its official PyInstaller
    # hook (playwright/_impl/__pyinstaller) - can't find a browser relative
    # to itself the way a normal `pip install` + `playwright install` would;
    # it ends up looking inside that ephemeral temp dir instead. Point it at
    # the same real, persistent cache a normal `playwright install chromium`
    # already wrote to (confirmed live: the frozen build failed with
    # "Executable doesn't exist at .../_MEIxxxxx/playwright/driver/..." until
    # this was added) - the browser itself still has to be installed once per
    # machine, same one-time prerequisite as Appium/Xcode for mobile_agent,
    # this just points the frozen app at where that install actually lands.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.expanduser("~"),
        "Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright",
    )
OUTPUT_DIR          = os.path.join(BASE_DIR, "generated_scripts")
SESSIONS_DIR        = os.path.join(BASE_DIR, "sessions")
PROMPT_CONFIG_FILE  = os.path.join(BASE_DIR, "prompt_config.json")

# First run of a frozen build: seed an editable prompt_config.json next to
# the executable from the bundled template, so the UI's "Framework config"
# editor opens with real defaults instead of an empty file. A dev run from
# source never hits this (PROMPT_CONFIG_FILE already exists in the repo).
if not os.path.exists(PROMPT_CONFIG_FILE):
    _bundled_default_config = os.path.join(BUNDLED_DIR, "prompt_config.json")
    if os.path.exists(_bundled_default_config):
        shutil.copyfile(_bundled_default_config, PROMPT_CONFIG_FILE)
OLLAMA_URL          = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = "qwen3:8b"
OLLAMA_TIMEOUT      = int(os.environ.get("OLLAMA_TIMEOUT", "600"))  # local CPU inference on a
                                                                     # full framework-context prompt
                                                                     # can take several minutes
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TARGET_URL  = os.environ.get("TARGET_URL", "https://example.com")
SERVER_PORT         = int(os.environ.get("PORT", 5002))
# The one external pytest project the Script Runner (below) knows about -
# an already-real, Allure-instrumented API test suite, unrelated to this
# tool's own network-capture/generation pipeline. Its own venv is found
# the same way Suite Runner's _venv_python() already does.
TESTLOKE_PROJECT_ROOT = os.environ.get("TESTLOKE_PROJECT_ROOT", "")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")


# ── Config ────────────────────────────────────────────────────────────────────

def load_prompt_config():
    if os.path.exists(PROMPT_CONFIG_FILE):
        with open(PROMPT_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_api_framework_context():
    cfg = load_prompt_config()
    api_cfg = cfg.get("api_testing", {})
    if not api_cfg:
        return "Python + requests framework. Use requests.Session(), assert on real response data."

    imports    = "\n".join(api_cfg.get("imports", []))
    client_arc = "\n".join(f"  - {v}" for v in api_cfg.get("client_architecture", {}).values())
    chaining   = "\n".join(f"  - {v}" for v in api_cfg.get("chaining_rules", {}).values())

    parts = [
        "FRAMEWORK: " + api_cfg.get("framework", {}).get("library", "requests"),
        "STANDARD IMPORTS (always use exactly these):\n" + imports,
        "AUTH HEADER CONVENTION:\n  " + api_cfg.get("auth_convention", ""),
        "SESSION PATTERN:\n  " + api_cfg.get("session_pattern", ""),
        "API CLIENT ARCHITECTURE (follow all strictly — never put HTTP code in the test class):\n" + client_arc,
        "CHAINING RULES (follow all strictly):\n" + chaining,
        "FILE OUTPUT:\n  " + api_cfg.get("file_output", ""),
        "TEST TEMPLATE (structure to follow):\n" + api_cfg.get("test_template", ""),
        "CONFIG MANAGER (use these methods only, never invent others):\n" + "\n".join(
            f"  {m}" for m in cfg.get("config_manager", {}).get("real_methods", [])
        ),
    ]
    return "\n\n".join(parts)


def build_standalone_framework_context():
    """Same idea as build_api_framework_context() above, but for the plain
    single-file `requests` script mode (no pytest, no page objects) — see
    the "standalone_script_testing" block in prompt_config.json."""
    cfg = load_prompt_config()
    sa_cfg = cfg.get("standalone_script_testing", {})
    if not sa_cfg:
        return ("Plain Python script using only `requests` + `os` + stdlib — no pytest, no project "
                "imports. Log in first, then call each captured endpoint in order, printing "
                "'[PASS]'/'[FAIL]' lines for each check and exiting 1 on any failure.")

    imports = "\n".join(sa_cfg.get("imports", []))
    parts = [
        "FRAMEWORK: " + sa_cfg.get("framework", {}).get("library", "requests")
        + " (" + sa_cfg.get("framework", {}).get("test_runner", "no test runner") + ")",
        "STANDARD IMPORTS (always use exactly these):\n" + imports,
        "NOTE:\n  " + sa_cfg.get("note", ""),
        "BASE URL:\n  " + sa_cfg.get("base_url", ""),
        "AUTH CONVENTION:\n  " + sa_cfg.get("auth_convention", ""),
        "CREDENTIALS:\n  " + sa_cfg.get("env_credentials", ""),
        "STEP STRUCTURE:\n  " + sa_cfg.get("step_structure", ""),
        "GOAL COVERAGE:\n  " + sa_cfg.get("goal_coverage", ""),
        "REPORT OUTPUT FORMAT (follow exactly, it is machine-parsed):\n  " + sa_cfg.get("report_convention", ""),
        "RULES:\n  " + sa_cfg.get("no_invented_endpoints", ""),
        "FILE OUTPUT:\n  " + sa_cfg.get("file_output", ""),
    ]
    return "\n\n".join(parts)


def load_js(filename):
    path = os.path.join(BUNDLED_DIR, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


# ── Network helpers ───────────────────────────────────────────────────────────

def is_api_url(url):
    """Check if URL is likely an API call (not a static asset)."""
    if not url:
        return False
    url_lower = url.lower()
    skip_patterns = ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
                      '.woff', '.woff2', '.ttf', '.eot', 'fonts.googleapis', 'cdn.']
    if any(p in url_lower for p in skip_patterns):
        return False
    api_patterns = ['/api/', '/graphql', '/v1/', '/v2/', '/v3/', '/rest/', '.json',
                     '/auth/', '/login', '/logout', '/users', '/data', '/query',
                     '/mutation', '/search', '/gql', '/rpc', 'amazonaws.com',
                     'firebase', 'supabase', 'hasura']
    return any(p in url_lower for p in api_patterns)


# ── Explore (AI-driven browsing) ──────────────────────────────────────────────
# Annotates every visible interactive element on the page with a numeric
# data-tg-ref attribute and returns a compact description of each, so the
# explore loop can hand the LLM a text list ("[12] <button> 'Log in'") instead
# of a screenshot - far cheaper/faster per step, and directly selectable
# afterwards via `[data-tg-ref="12"]`. Re-run on every step since the DOM
# changes between actions; refs are only meant to live for one step.
EXPLORE_SNAPSHOT_JS = r"""
() => {
  const SELECTOR = 'a[href], button, input, select, textarea, [role="button"], [role="link"], ' +
    '[role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="switch"], ' +
    '[contenteditable="true"], [onclick]';

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const txt = labelledby.split(/\s+/).map(id => (document.getElementById(id)?.innerText || '')).join(' ').trim();
      if (txt) return txt;
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    const parentLabel = el.closest('label');
    if (parentLabel && parentLabel.innerText.trim()) return parentLabel.innerText.trim();
    if (el.placeholder) return el.placeholder.trim();
    if (el.getAttribute('title')) return el.getAttribute('title').trim();
    if (el.getAttribute('alt')) return el.getAttribute('alt').trim();
    if ('value' in el && (el.type === 'submit' || el.type === 'button') && el.value) return el.value.trim();
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    return text.slice(0, 80);
  }

  const nodes = Array.from(document.querySelectorAll(SELECTOR));
  const elements = [];
  let ref = 0;
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    ref += 1;
    el.setAttribute('data-tg-ref', String(ref));
    const tag = el.tagName.toLowerCase();
    const item = { ref, tag, type: el.getAttribute('type') || null, role: el.getAttribute('role') || null,
                   name: accessibleName(el), disabled: !!el.disabled };
    if (tag === 'input' || tag === 'textarea') {
      item.value = (el.value || '').slice(0, 200);
      if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    }
    if (tag === 'select') {
      const opt = el.options[el.selectedIndex];
      item.value = opt ? opt.text : '';
      item.options = Array.from(el.options).slice(0, 20).map(o => o.text);
    }
    if (tag === 'a') item.href = (el.getAttribute('href') || '').slice(0, 200);
    elements.push(item);
    if (elements.length >= 80) break;
  }
  return { url: location.href, title: document.title, elements };
}
"""


# ── Playwright worker (multi-portal — network capture only) ──────────────────
# "Portal" = an independently addressable browser window (id "A", "B", ...), so
# you can capture APIs across two logged-in sessions at once — e.g. Portal A
# creating a resource that Portal B (a different user/role) then fetches.

pw_queue    = queue.Queue()
pw_pending  = {}  # request id -> the queue.Queue a given pw_call() is waiting on
pw_pending_lock = threading.Lock()
session   = {"connected": False}
pw = {"driver": None, "pages": {}, "contexts": {}, "browsers": {}}


def _put_result(cmd, result):
    """Routes a worker action's result back to the specific pw_call() that
    queued it, keyed by the per-call request id - a shared single result
    queue would let two concurrent callers (e.g. the explore loop polling
    the network log every step while the UI's own periodic refresh does
    the same) steal each other's results."""
    req_id = cmd.get("_req_id")
    with pw_pending_lock:
        q = pw_pending.get(req_id)
    if q is not None:
        q.put(result)


# ── UI recorder state ─────────────────────────────────────────────────────────
# Steps arrive from ui_recorder.js (injected into every page) through a
# Playwright binding while a recording is active. One recording at a time,
# bound to one portal; the UI edits/reads this list over Socket.IO.

ui_rec = {"recording": False, "portal": "A", "portals": ["A"], "steps": [], "next_id": 1, "toolbar": True, "anchor": False}
ui_pending = []  # (portal, step) actions the in-page picker wants Playwright to really perform (hover)
ui_rec_lock = threading.Lock()


# Saved recordings live as one JSON file each (plus a rolling autosave) so work
# survives a restart/crash. They hold real recorded values - including any
# password-like input, which the generated test keeps out of its source - so
# the files are owner-only and the folder is gitignored.
UI_RECORDINGS_DIR = os.environ.get("UI_RECORDINGS_DIR") or os.path.join(BASE_DIR, "ui_recordings")
UI_AUTOSAVE_FILE = os.path.join(UI_RECORDINGS_DIR, ".autosave.json")
ui_rec["loaded_name"] = None   # name of the saved recording being edited, if any


def _write_private_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)   # never leaves a half-written file behind


def _ui_autosave():
    try:
        with ui_rec_lock:
            payload = {"steps": list(ui_rec["steps"]), "loaded_name": ui_rec.get("loaded_name")}
        _write_private_json(UI_AUTOSAVE_FILE, payload)
    except Exception as e:
        print(f"UI autosave failed: {e}")


def _ui_restore_autosave():
    try:
        with open(UI_AUTOSAVE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        steps = data.get("steps") or []
        if steps:
            ui_rec["steps"] = steps
            ui_rec["next_id"] = max((x.get("id", 0) for x in steps), default=0) + 1
            ui_rec["loaded_name"] = data.get("loaded_name")
            print(f"Restored {len(steps)} recorded UI step(s) from autosave.")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Could not restore UI autosave: {e}")


def _ui_emit_steps():
    with app.app_context():
        socketio.emit("ui_steps", {"steps": list(ui_rec["steps"]), "recording": ui_rec["recording"],
                                   "loaded_name": ui_rec.get("loaded_name")})
    _ui_autosave()


_ui_restore_autosave()


def _ui_add_step(step, portal):
    """Appends one recorded step (or merges it into the previous one) and pushes the list to the UI."""
    with ui_rec_lock:
        if not ui_rec["recording"] or portal not in ui_rec["portals"]:
            return
        step["portal"] = portal   # which window this happened in (replay/codegen use it)
        steps = ui_rec["steps"]
        last = steps[-1] if steps else None
        same_target = last and last.get("locator") == step.get("locator") and last.get("portal", "A") == portal
        if last and step.get("type") == "clear" and last.get("type") == "clear" and same_target:
            return
        # Retyping the same field replaces the earlier fill instead of stacking a second one.
        if last and step.get("type") == "fill" and last.get("type") == "fill" and same_target:
            last["value"], last["secret"] = step.get("value"), step.get("secret")
        else:
            step["id"] = ui_rec["next_id"]
            ui_rec["next_id"] += 1
            steps.append(step)
    _ui_emit_steps()


def _ui_binding(source, step):
    """Called from the page. '__state' lets a freshly loaded document ask whether
    a recording is live (so the toolbar survives navigations); anything else is a step."""
    portal = getattr(source["page"], "_portal_id", "A")
    if step.get("type") == "__state":
        return {"rec": ui_rec["recording"] and portal in ui_rec["portals"], "toolbar": ui_rec["toolbar"],
                "anchor": ui_rec.get("anchor", False),
                "label": f"Portal {portal}" if len(ui_rec["portals"]) > 1 else ""}
    if step.get("type") == "__anchor":   # the toolbar's ⚓ toggle; remembered across page loads
        ui_rec["anchor"] = bool(step.get("value"))
        return None
    perform = step.pop("perform", False)
    _ui_add_step(step, portal)
    if perform and ui_rec["recording"] and portal in ui_rec["portals"]:
        ui_pending.append((portal, step))


def _run_ui_pending():
    """Performs queued picker actions. Runs from the worker's idle loop, not from
    inside the binding callback, so Playwright calls never nest in an event handler."""
    while ui_pending:
        portal, step = ui_pending.pop(0)
        page = pw["pages"].get(portal)
        try:
            if page is not None and step.get("type") == "hover":
                resolve_locator(page, step["locator"]).hover(timeout=5000)
            elif page is not None and step.get("type") == "clear":
                resolve_locator(page, step["locator"]).clear(timeout=5000)
        except Exception as e:
            with app.app_context():
                socketio.emit("error", {"msg": f"Could not {step.get('type')}: " + str(e).strip().splitlines()[0][:200]})


def _set_page_recording(portal, on, toolbar):
    page = pw["pages"].get(portal)
    if page is not None:
        label = f"Portal {portal}" if len(ui_rec["portals"]) > 1 else ""
        try:
            page.evaluate("([on, tb, an, lb]) => window.__tgSetRec && window.__tgSetRec(on, tb, an, lb)",
                          [on, toolbar, ui_rec.get("anchor", False), label])
        except Exception:
            pass


def playwright_worker():

    def get_page_for(cmd):
        return pw["pages"].get(cmd.get("portal", "A"))

    # Registered once and reused for every portal's page — state is read/written
    # through the page object the event belongs to, never a closed-over local,
    # since more than one page can be alive at once.
    def on_request(req):
        try:
            page = req.frame.page
            log = getattr(page, "_network_log", None)
            if log is None:
                return
            entry = {
                "url": req.url,
                "method": req.method,
                "requestHeaders": dict(req.headers) if req.headers else {},
                "requestBody": req.post_data[:2000] if req.post_data else None,
                "ts": int(datetime.now().timestamp() * 1000),
                "timestamp": datetime.now().isoformat(),
                "resourceType": req.resource_type,
                "isApi": is_api_url(req.url),
                "status": None,
                "responseHeaders": {},
                "responseBody": None,
                "duration": None,
                "portal": getattr(page, "_portal_id", "A"),
            }
            log.append(entry)
            if len(log) > 200:
                log.pop(0)
        except Exception as e:
            print(f"Request capture error: {e}")

    def on_response(resp):
        try:
            page = resp.frame.page
            log = getattr(page, "_network_log", None)
            if log is None:
                return
            for entry in reversed(log):
                if entry.get("url") == resp.url and entry.get("status") is None:
                    entry["status"] = resp.status
                    entry["statusText"] = resp.status_text
                    entry["responseHeaders"] = dict(resp.headers) if resp.headers else {}
                    entry["duration"] = int(datetime.now().timestamp() * 1000) - entry["ts"]
                    try:
                        body = resp.text()
                        entry["responseBody"] = body[:2000] if body else None
                    except Exception:
                        entry["responseBody"] = "[unable to read]"
                    break
        except Exception as e:
            print(f"Response capture error: {e}")

    def on_console(msg):
        try:
            if msg.type != "error":
                return
            page = msg.page
            log = getattr(page, "_console_log", None)
            if log is None:
                return
            log.append(msg.text[:500])
            del log[:-50]
        except Exception as e:
            print(f"Console capture error: {e}")

    def on_pageerror(page, exc):
        log = getattr(page, "_console_log", None)
        if log is None:
            return
        log.append(f"Uncaught exception: {exc}"[:500])
        del log[:-50]

    while True:
        try:
            cmd = pw_queue.get_nowait()
        except queue.Empty:
            # Playwright's sync API only delivers events (network, recorder
            # bindings) while it is inside a call, so a worker parked on a
            # blocking queue.get() would never see a user's clicks. Spend
            # idle time inside a short wait on any open page instead.
            _run_ui_pending()
            page = next(iter(pw["pages"].values()), None)
            try:
                if page is not None:
                    page.wait_for_timeout(100)
                else:
                    time.sleep(0.1)
            except Exception:
                time.sleep(0.1)
            continue
        action = cmd.get("action")

        if action == "launch":
            portal = cmd.get("portal", "A")
            try:
                network_js = load_js("network_inject.js")
                if pw["driver"] is None:
                    # PWDEBUG=console makes the driver expose Playwright's own selector
                    # generator to pages (window.playwright), which ui_recorder.js uses.
                    # Set only while the driver process spawns, so test subprocesses
                    # started later don't inherit it.
                    os.environ["PWDEBUG"] = "console"
                    try:
                        pw["driver"] = sync_playwright().start()
                    finally:
                        os.environ.pop("PWDEBUG", None)
                browser = pw["driver"].chromium.launch(
                    headless=False,
                    args=["--disable-dev-shm-usage"]  # macOS/Linux stability
                )
                context = browser.new_context(bypass_csp=True)
                if network_js:
                    context.add_init_script(network_js)
                ui_js = load_js("ui_recorder.js")
                if ui_js:
                    context.expose_binding("__tgRecordStep", _ui_binding)
                    context.add_init_script(ui_js)
                page = context.new_page()
                page._portal_id   = portal
                page._network_log = []
                page._console_log = []
                page.on("request", on_request)
                page.on("response", on_response)
                page.on("console", on_console)
                page.on("pageerror", lambda exc: on_pageerror(page, exc))
                # Playwright auto-dismisses native dialogs (confirm/alert/prompt) unless
                # a handler is registered, which silently breaks "confirm() then delete"
                # flows. Auto-accept so those confirmations proceed like a real click on OK.
                page.on("dialog", lambda dialog: dialog.accept())
                page.goto(cmd["url"], wait_until="domcontentloaded", timeout=30000)
                time.sleep(0.3)  # let the window render fully before returning

                # Replace any previous session on this portal slot
                old_browser = pw["browsers"].get(portal)
                pw["pages"][portal]    = page
                pw["contexts"][portal] = context
                pw["browsers"][portal] = browser
                if old_browser is not None and old_browser is not browser:
                    try:
                        old_browser.close()
                    except Exception:
                        pass

                print(f"[Playwright] Browser launched [{portal}]: {page.title()} at {page.url}")
                _put_result(cmd, {"ok": True, "title": page.title(), "url": page.url, "portal": portal})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e), "portal": portal})

        elif action == "navigate":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                page.goto(cmd["url"], wait_until="domcontentloaded", timeout=30000)
                _ui_add_step({"type": "goto", "value": page.url, "url": page.url}, cmd.get("portal", "A"))
                _put_result(cmd, {"ok": True, "url": page.url, "title": page.title()})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "ui_record_start":
            wanted = cmd.get("portals") or [cmd.get("portal", "A")]
            portals = [p for p in wanted if pw["pages"].get(p) is not None]
            if not portals:
                _put_result(cmd, {"ok": False, "error": "No browser open" if not pw["pages"] else
                                  f"Portal {', '.join(wanted)} isn't open - launch it first"}); continue
            keep = bool(cmd.get("keep_steps"))   # append to the existing steps instead of starting over
            with ui_rec_lock:
                ui_rec.update(recording=True, portals=portals, portal=portals[0], toolbar=cmd.get("toolbar", True))
                if not keep:
                    ui_rec.update(steps=[], next_id=1, loaded_name=None)
            for p in portals:
                if not keep:
                    pg = pw["pages"][p]
                    _ui_add_step({"type": "goto", "value": pg.url, "url": pg.url}, p)
                _set_page_recording(p, True, ui_rec["toolbar"])
            _put_result(cmd, {"ok": True, "url": pw["pages"][portals[0]].url, "portals": portals})

        elif action == "ui_record_stop":
            time.sleep(0.4)  # let in-flight events from the page land first
            page = next(iter(pw["pages"].values()), None)
            if page is not None:
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            with ui_rec_lock:
                ui_rec["recording"] = False
                recorded = list(ui_rec["portals"])
            for p in recorded:
                _set_page_recording(p, False, False)
            _put_result(cmd, {"ok": True})

        elif action == "ui_locator_check":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                loc = resolve_locator(page, cmd["locator"]) if cmd.get("locator") else page.locator(cmd.get("selector", ""))
                n = loc.count()
                if n:
                    loc.first.scroll_into_view_if_needed(timeout=2000)
                    loc.first.evaluate("e => { const o = e.style.outline; e.style.outline = '3px solid #f59e0b'; "
                                       "setTimeout(() => { e.style.outline = o; }, 2500); }")
                _put_result(cmd, {"ok": True, "count": n})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e).strip().splitlines()[0][:200]})

        elif action == "ui_replay":
            # One fresh context (clean cookies/storage = a separate login) per portal the steps
            # use, in the same browser as that portal. No recorder binding in these contexts,
            # so the replay can't feed back into the steps.
            if not pw["browsers"]:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            from playwright.sync_api import expect as pw_expect
            steps, slow = cmd.get("steps", []), int(cmd.get("slow_mo") or 0)
            timeout_ms = max(1000, int(float(cmd.get("timeout_s") or 15) * 1000))
            pw_expect.set_options(timeout=timeout_ms)
            failed_at = None
            ctxs, rpages = {}, {}

            def replay_page(portal):
                if portal not in rpages:
                    br = pw["browsers"].get(portal) or pw["browsers"].get("A") or next(iter(pw["browsers"].values()))
                    ctxs[portal] = br.new_context()
                    pg = ctxs[portal].new_page()
                    pg.set_default_timeout(timeout_ms)
                    pg.set_default_navigation_timeout(max(timeout_ms, 30000))
                    pg.on("dialog", lambda d: d.accept())
                    rpages[portal] = pg
                return rpages[portal]

            try:
                for pos, s in enumerate(steps):
                    rpage = replay_page(s.get("portal", "A"))
                    try:
                        if cmd.get("highlight_last") and pos == len(steps) - 1 and s.get("locator"):
                            # "Run to here": show which element this step's locator found, then act on it.
                            try:
                                tgt = resolve_locator(rpage, s["locator"]).first
                                tgt.scroll_into_view_if_needed(timeout=3000)
                                tgt.evaluate("e => { e.style.outline = '3px solid #f59e0b'; }")
                                rpage.wait_for_timeout(1200)
                            except Exception:
                                pass
                        replay_step(rpage, s, pw_expect)
                        res = {"id": s["id"], "ok": True}
                    except Exception as e:
                        failed_at = s["id"]
                        res = {"id": s["id"], "ok": False, "error": describe_failure(rpage, s, e)}
                        try:
                            res["screenshot"] = base64.b64encode(rpage.screenshot(type="jpeg", quality=50)).decode("ascii")
                        except Exception:
                            pass
                    with app.app_context():
                        socketio.emit("ui_replay_step", res)
                    if failed_at is not None:
                        break
                    if slow:
                        rpage.wait_for_timeout(slow)
                if failed_at is None and rpages:
                    next(iter(rpages.values())).wait_for_timeout(1500 if cmd.get("highlight_last") else 800)   # let the user see the end state
            finally:
                for c in ctxs.values():
                    try:
                        c.close()
                    except Exception:
                        pass
            _put_result(cmd, {"ok": True, "failed_at": failed_at})

        elif action == "dom_snapshot":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                data = page.evaluate(EXPLORE_SNAPSHOT_JS)
                console_errors = list(getattr(page, "_console_log", []))[-10:]
                _put_result(cmd, {"ok": True, "url": data.get("url"), "title": data.get("title"),
                                   "elements": data.get("elements", []), "console_errors": console_errors})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "click_ref":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                loc = page.locator(f'[data-tg-ref="{cmd.get("ref")}"]').first
                loc.scroll_into_view_if_needed(timeout=5000)
                loc.click(timeout=5000)
                _put_result(cmd, {"ok": True})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "type_ref":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                loc = page.locator(f'[data-tg-ref="{cmd.get("ref")}"]').first
                loc.scroll_into_view_if_needed(timeout=5000)
                loc.fill(cmd.get("text", ""), timeout=5000)
                if ui_rec["recording"]:
                    # fill() fires `input` but never blurs, so the recorder would only
                    # see this value on the next click; commit it the way a blur would.
                    loc.dispatch_event("change")
                if cmd.get("submit_after_type"):
                    loc.press("Enter", timeout=5000)
                _put_result(cmd, {"ok": True})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "press_key":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                page.keyboard.press(cmd.get("key", "Enter"))
                _put_result(cmd, {"ok": True})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "screenshot":
            page = get_page_for(cmd)
            if page is None:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                img = page.screenshot(type="jpeg", quality=60)
                _put_result(cmd, {"ok": True, "b64": base64.b64encode(img).decode("ascii")})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "get_network_log":
            if not pw["pages"]:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                merged = []
                for portal_id, page in pw["pages"].items():
                    net = list(getattr(page, "_network_log", []))
                    # Merge in ONLY beacon calls from the injected JS log. Fetch/XHR are
                    # already fully captured above via Playwright's native request/response
                    # events (headers, body, status, duration) — re-adding them from the JS
                    # side would duplicate every call, since the two capture paths timestamp
                    # independently (Python's clock vs the page's) and can never dedupe on
                    # (url, ts). Beacons are the one case native capture can genuinely miss
                    # (fire-and-forget, no matching response event).
                    try:
                        js_network = page.evaluate("() => window.__testgenNetworkLog || []")
                        for n in js_network:
                            if n.get("type") == "beacon":
                                n.setdefault("portal", portal_id)
                                net.append(n)
                    except Exception:
                        pass
                    merged.extend(net)
                if cmd.get("api_only", False):
                    merged = [n for n in merged if n.get("isApi", False)]
                # Sort by timestamp so the portals' calls interleave chronologically
                merged = sorted(merged, key=lambda x: x.get("ts", 0))
                _put_result(cmd, {"ok": True, "network": merged})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "clear_network_log":
            if not pw["pages"]:
                _put_result(cmd, {"ok": False, "error": "No browser open"}); continue
            try:
                for page in pw["pages"].values():
                    try:
                        page.evaluate("() => { window.__testgenNetworkLog = []; }")
                    except Exception:
                        pass
                    if hasattr(page, "_network_log"):
                        page._network_log.clear()
                _put_result(cmd, {"ok": True})
            except Exception as e:
                _put_result(cmd, {"ok": False, "error": str(e)})

        elif action == "stop":
            # No portal given -> stop every open portal session
            portal = cmd.get("portal")
            targets = [portal] if portal else list(pw["browsers"].keys())
            for pid in targets:
                try:
                    b = pw["browsers"].get(pid)
                    if b: b.close()
                except Exception:
                    pass
                pw["pages"].pop(pid, None)
                pw["contexts"].pop(pid, None)
                pw["browsers"].pop(pid, None)
            _put_result(cmd, {"ok": True})

        elif action == "quit":
            try:
                for b in pw["browsers"].values():
                    try:
                        b.close()
                    except Exception:
                        pass
                if pw["driver"]:
                    pw["driver"].stop()
            except Exception:
                pass
            break


_pw_thread = threading.Thread(target=playwright_worker, daemon=True)
_pw_thread.start()


def pw_call(cmd, timeout=45):
    req_id = str(uuid.uuid4())
    result_q = queue.Queue(maxsize=1)
    with pw_pending_lock:
        pw_pending[req_id] = result_q
    pw_queue.put({**cmd, "_req_id": req_id})
    try:
        return result_q.get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "error": "Playwright command timed out"}
    finally:
        with pw_pending_lock:
            pw_pending.pop(req_id, None)


# ── Socket events ─────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("status", {"connected": session["connected"], "playwright": PLAYWRIGHT_AVAILABLE})


@socketio.on("start_browser")
def start_browser(data):
    """Launch browser (for the given portal, default 'A') in a completely isolated thread."""
    if not PLAYWRIGHT_AVAILABLE:
        emit("error", {"msg": "Playwright not installed"})
        return

    url    = data.get("url", DEFAULT_TARGET_URL)
    portal = data.get("portal", "A")
    label  = f" (Portal {portal})" if portal != "A" else ""
    emit("log", {"msg": f"Launching browser{label} → {url}"})

    def launch_isolated():
        time.sleep(0.2)
        res = pw_call({"action": "launch", "url": url, "portal": portal})
        time.sleep(0.1)
        if res["ok"]:
            session["connected"] = True
            with app.app_context():
                socketio.emit("log", {"msg": f"Browser{label} open — " + res.get("title", "") +
                                              ". Interact with the page to generate traffic."})
                socketio.emit("browser_started", {"url": url, "title": res.get("title", ""), "portal": portal})
        else:
            with app.app_context():
                socketio.emit("error", {"msg": f"Launch failed{label}: " + res.get("error", "")})

    threading.Thread(target=launch_isolated, daemon=True).start()


@socketio.on("navigate")
def navigate(data):
    url    = data.get("url", "")
    portal = data.get("portal", "A")
    res = pw_call({"action": "navigate", "url": url, "portal": portal})
    if res["ok"]:
        emit("log", {"msg": "-> " + res.get("url", "")})
        emit("navigated", {"url": res.get("url"), "title": res.get("title"), "portal": portal})
    else:
        emit("error", {"msg": "Navigate failed: " + res.get("error", "")})


@socketio.on("stop_browser")
def stop_browser():
    """No portal given -> closes every open portal window."""
    pw_call({"action": "stop"})
    session["connected"] = False
    emit("browser_stopped", {})
    emit("log", {"msg": "Browser(s) closed."})


@socketio.on("get_network_log")
def get_network_log(data):
    api_only = data.get("api_only", False) if data else False
    res = pw_call({"action": "get_network_log", "api_only": api_only})
    if not res["ok"]:
        emit("error", {"msg": res.get("error")}); return
    emit("network_log", {"network": res.get("network", [])})


@socketio.on("clear_network_log")
def clear_network_log():
    res = pw_call({"action": "clear_network_log"})
    if res["ok"]:
        emit("log", {"msg": "Network log cleared"})
        emit("network_log", {"network": []})
    else:
        emit("error", {"msg": res.get("error")})


# ── UI recorder (record by hand -> generate a Playwright test -> run it) ──────
# The user drives the already-open browser; ui_recorder.js reports each
# click/fill/select/... and the steps accumulate in ui_rec (see above). Generate
# turns them into a pytest+Playwright file under generated_scripts/ui_tests/,
# Run executes that file in its own subprocess/browser, streaming the output.

UI_TESTS_DIR = os.path.join(OUTPUT_DIR, "ui_tests")
ui_runs = {}  # run_id -> subprocess.Popen


@socketio.on("ui_record_start")
def ui_record_start(data):
    data = data or {}
    portals = data.get("portals") or [data.get("portal", "A")]
    keep = bool(data.get("keep_steps"))
    res = pw_call({"action": "ui_record_start", "portals": portals, "keep_steps": keep})
    if not res["ok"]:
        emit("error", {"msg": "Record failed: " + res.get("error", "")}); return
    names = " + ".join(f"Portal {p}" for p in res.get("portals", portals))
    emit("log", {"msg": f"Recording UI actions on {names} - use the app normally"
                        + (" (new steps are added at the end)." if keep else ".")})
    _ui_emit_steps()


@socketio.on("ui_record_stop")
def ui_record_stop():
    pw_call({"action": "ui_record_stop"}, timeout=15)
    with ui_rec_lock:
        ui_rec["recording"] = False
    _ui_emit_steps()


@socketio.on("ui_get_steps")
def ui_get_steps():
    _ui_emit_steps()


@socketio.on("ui_step_delete")
def ui_step_delete(data):
    sid = (data or {}).get("id")
    with ui_rec_lock:
        ui_rec["steps"] = [s for s in ui_rec["steps"] if s.get("id") != sid]
    _ui_emit_steps()


@socketio.on("ui_step_update")
def ui_step_update(data):
    """Edit a step's text value (fill text, expected text, option label, key)."""
    sid, value = (data or {}).get("id"), (data or {}).get("value")
    with ui_rec_lock:
        for s in ui_rec["steps"]:
            if s.get("id") == sid and "value" in s:
                s["value"] = value
    _ui_emit_steps()


@socketio.on("ui_step_insert")
def ui_step_insert(data):
    """Inserts a step after `after_id` (None = at the very start). Used for hand-added
    waits and for splitting the list into test cases ('case' markers)."""
    data = data or {}
    after_id, step = data.get("after_id"), dict(data.get("step") or {})
    if step.get("type") not in ("wait", "case"):
        emit("error", {"msg": "Unsupported step type"}); return
    with ui_rec_lock:
        steps = ui_rec["steps"]
        idx = 0 if after_id is None else next((i + 1 for i, s in enumerate(steps) if s.get("id") == after_id), len(steps))

        def new_step(st):
            st["id"] = ui_rec["next_id"]
            ui_rec["next_id"] += 1
            return st
        if step["type"] == "case":
            n_cases = sum(1 for s in steps if s.get("type") == "case")
            # The first split also names the leading case, so every step belongs to a named case.
            if n_cases == 0 and not (steps and steps[0].get("type") == "case"):
                steps.insert(0, new_step({"type": "case", "value": "Test case 1"}))
                idx += 1
                n_cases = 1
            step["value"] = (step.get("value") or "").strip() or f"Test case {n_cases + 1}"
        else:
            step["value"] = str(step.get("value") or 1000)
        steps.insert(idx, new_step(step))
    _ui_emit_steps()


@socketio.on("ui_step_set_locator")
def ui_step_set_locator(data):
    """Manual override for a step whose generated locator is wrong/unstable: any Playwright
    selector (css, xpath=, text=, role=, data-testid=, chained with >>)."""
    sid, sel = (data or {}).get("id"), ((data or {}).get("selector") or "").strip()
    if not sel:
        emit("error", {"msg": "Enter a selector"}); return
    with ui_rec_lock:
        for st in ui_rec["steps"]:
            if st.get("id") == sid and "locator" in st:
                st["locator"] = {"kind": "css", "value": sel}
    _ui_emit_steps()


@socketio.on("ui_step_use_alt")
def ui_step_use_alt(data):
    """Swaps in one of the alternative locators recorded with the step (e.g. the label-anchored,
    value-independent one); the replaced locator becomes an alternative in turn."""
    sid, idx = (data or {}).get("id"), (data or {}).get("index")
    with ui_rec_lock:
        for st in ui_rec["steps"]:
            if st.get("id") == sid and st.get("locator"):
                alts = list((st["locator"].get("alts") or []))
                if not isinstance(idx, int) or not 0 <= idx < len(alts):
                    break
                chosen = dict(alts[idx])
                old = {k: v for k, v in st["locator"].items() if k != "alts"}
                chosen["alts"] = [a for i, a in enumerate(alts) if i != idx] + [old]
                st["locator"] = chosen
                break
    _ui_emit_steps()


@socketio.on("ui_locator_check")
def ui_locator_check(data):
    """Counts (and briefly outlines) what a selector matches in the live recording page."""
    portal = (data or {}).get("portal") or ui_rec["portal"]
    res = pw_call({"action": "ui_locator_check", "portal": portal, "selector": (data or {}).get("selector", ""),
                   "locator": (data or {}).get("locator")}, timeout=15)
    emit("ui_locator_checked", {"id": (data or {}).get("id"), "ok": res.get("ok", False),
                                "count": res.get("count"), "error": res.get("error")})


@socketio.on("ui_steps_clear")
def ui_steps_clear():
    with ui_rec_lock:
        ui_rec["steps"] = []
        ui_rec["loaded_name"] = None
    _ui_emit_steps()


@socketio.on("ui_replay")
def ui_replay(data):
    """Replays the recorded steps in a fresh browser window, reporting each step's result live."""
    data = data or {}
    portal = data.get("portal") or ui_rec["portal"]
    with ui_rec_lock:
        if ui_rec["recording"]:
            emit("error", {"msg": "Stop recording before replaying"}); return
        steps = [dict(s) for s in ui_rec["steps"]]
    upto = data.get("upto_id")
    if upto is not None:   # "Run to here": everything from the start through that step
        idx = next((i for i, s in enumerate(steps) if s.get("id") == upto), None)
        if idx is None:
            emit("error", {"msg": "Step not found"}); return
        steps = steps[:idx + 1]
    if not steps:
        emit("error", {"msg": "Nothing recorded yet"}); return
    emit("ui_replay_started", {})

    def run():
        res = pw_call({"action": "ui_replay", "portal": portal, "steps": steps, "slow_mo": data.get("slow_mo") or 0,
                       "timeout_s": data.get("timeout_s") or 15,
                       "highlight_last": upto is not None}, timeout=600)
        with app.app_context():
            socketio.emit("ui_replay_done", {"ok": res.get("ok", False), "failed_at": res.get("failed_at"),
                                             "error": res.get("error")})

    threading.Thread(target=run, daemon=True).start()


def _rec_file(name):
    slug = slugify(name)
    path = os.path.realpath(os.path.join(UI_RECORDINGS_DIR, slug + ".json"))
    if os.path.dirname(path) != os.path.realpath(UI_RECORDINGS_DIR):
        return None, slug
    return path, slug


@app.route("/api/ui-recordings", methods=["GET"])
def list_ui_recordings():
    out = []
    try:
        names = [n for n in os.listdir(UI_RECORDINGS_DIR) if n.endswith(".json") and not n.startswith(".")]
    except FileNotFoundError:
        names = []
    for fn in names:
        try:
            with open(os.path.join(UI_RECORDINGS_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            steps = d.get("steps") or []
            out.append({"file": fn[:-5], "name": d.get("name") or fn[:-5], "saved_at": d.get("saved_at"),
                        "steps": sum(1 for x in steps if x.get("type") != "case"),
                        "cases": sum(1 for x in steps if x.get("type") == "case")})
        except Exception:
            continue   # skip unreadable files rather than break the list
    out.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
    return jsonify({"recordings": out, "loaded_name": ui_rec.get("loaded_name")})


@app.route("/api/ui-recordings", methods=["POST"])
def save_ui_recording():
    """Saves the editor's current steps under `name` (overwrites a recording of the same name)."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Give the recording a name"}), 400
    path, slug = _rec_file(name)
    if not path:
        return jsonify({"error": "Invalid name"}), 400
    with ui_rec_lock:
        if ui_rec["recording"]:
            return jsonify({"error": "Stop recording before saving"}), 409
        steps = [dict(x) for x in ui_rec["steps"]]
        if not steps:
            return jsonify({"error": "Nothing recorded yet"}), 400
        ui_rec["loaded_name"] = name
    _write_private_json(path, {"name": name, "saved_at": datetime.now().isoformat(timespec="seconds"),
                               "settings": body.get("settings") or {}, "steps": steps})
    _ui_emit_steps()
    return jsonify({"ok": True, "name": name, "file": slug, "steps": len(steps)})


@app.route("/api/ui-recordings/<slug>", methods=["GET"])
def load_ui_recording(slug):
    """Loads a saved recording into the editor (replacing what's there) so it can be edited."""
    path, _ = _rec_file(slug)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "Recording not found"}), 404
    with ui_rec_lock:
        if ui_rec["recording"]:
            return jsonify({"error": "Stop recording before loading another recording"}), 409
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Could not read the file: {e}"}), 500
    steps = d.get("steps") or []
    with ui_rec_lock:
        ui_rec["steps"] = steps
        ui_rec["next_id"] = max((x.get("id", 0) for x in steps), default=0) + 1
        ui_rec["loaded_name"] = d.get("name") or slug
    _ui_emit_steps()
    return jsonify({"ok": True, "name": ui_rec["loaded_name"], "settings": d.get("settings") or {}, "steps": steps})


@app.route("/api/ui-recordings/<slug>", methods=["DELETE"])
def delete_ui_recording(slug):
    path, _ = _rec_file(slug)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "Recording not found"}), 404
    try:
        with open(path, encoding="utf-8") as f:
            name = json.load(f).get("name")
    except Exception:
        name = None
    os.remove(path)
    with ui_rec_lock:
        if ui_rec.get("loaded_name") and ui_rec["loaded_name"] in (name, slug):
            ui_rec["loaded_name"] = None   # the steps stay in the editor, just no longer tied to a saved file
    _ui_emit_steps()
    return jsonify({"ok": True})


@socketio.on("ui_generate")
def ui_generate(data):
    name = ((data or {}).get("name") or "").strip()
    if not name:
        emit("error", {"msg": "Give the scenario a name first"}); return
    with ui_rec_lock:
        steps = [dict(s) for s in ui_rec["steps"]]
    if not steps:
        emit("error", {"msg": "Nothing recorded yet"}); return

    code, secrets = generate_ui_test(name, steps, timeout_s=(data or {}).get("timeout_s") or 15,
                                     shared_session=(data or {}).get("shared_session", True))
    slug = slugify(name)
    os.makedirs(UI_TESTS_DIR, exist_ok=True)
    rel = f"ui_tests/test_{slug}.py"
    with open(os.path.join(OUTPUT_DIR, rel), "w", encoding="utf-8") as f:
        f.write(code)
    env_path = os.path.join(UI_TESTS_DIR, f".{slug}.env")
    if secrets:
        # Recorded password-like values live here (owner-only), not in the script.
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in secrets.items()))
    elif os.path.exists(env_path):
        os.remove(env_path)
    emit("ui_generated", {"file": rel, "code": code, "secrets": list(secrets)})


def _ui_python():
    # A frozen build's own executable is not a Python interpreter, so use the
    # project venv (or system python3); from source, the running interpreter.
    return _venv_python(BASE_DIR) if getattr(sys, "frozen", False) else sys.executable


@socketio.on("ui_run")
def ui_run(data):
    data = data or {}
    rel = (data.get("file") or "").strip()
    full = os.path.realpath(os.path.join(OUTPUT_DIR, rel))
    if not full.startswith(os.path.realpath(UI_TESTS_DIR) + os.sep) or not os.path.isfile(full):
        emit("ui_run_error", {"msg": f"Test file not found: {rel} - generate it first"}); return

    run_id = f"ui-run-{int(time.time())}"
    env = dict(os.environ)
    env["UI_HEADED"] = "0" if data.get("headless") else "1"
    env["UI_SLOWMO"] = str(int(data.get("slow_mo") or 0))
    cmd = [_ui_python(), "-m", "pytest", full, "-v", "-s", "--tb=short", "--color=no", "-p", "no:cacheprovider"]

    def stream():
        with app.app_context():
            socketio.emit("ui_run_log", {"line": f"$ pytest {rel}\n\n"})
        try:
            proc = subprocess.Popen(cmd, cwd=UI_TESTS_DIR, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            ui_runs[run_id] = proc
            for line in proc.stdout:
                with app.app_context():
                    socketio.emit("ui_run_log", {"line": line})
            proc.wait()
            with app.app_context():
                socketio.emit("ui_run_done", {"returncode": proc.returncode})
        except FileNotFoundError as e:
            with app.app_context():
                socketio.emit("ui_run_error", {"msg": f"Could not start '{cmd[0]}': {e}"})
        finally:
            ui_runs.pop(run_id, None)

    threading.Thread(target=stream, daemon=True).start()
    emit("ui_run_started", {"run_id": run_id})


@socketio.on("ui_run_stop")
def ui_run_stop(data):
    proc = ui_runs.get((data or {}).get("run_id"))
    if proc:
        proc.terminate()


# ── Explore runner (autonomous goal-driven UI testing) ────────────────────────
# Drives the already-open Playwright page itself, one discrete action at a
# time, picked by the LLM from a text snapshot of the page's interactive
# elements (see EXPLORE_SNAPSHOT_JS above) - no screenshots/vision, so each
# step is one small, fast, cheap text completion. Deliberately UI-only: it
# judges success/failure from the DOM and console errors, like a human
# tester watching the screen, not from network traffic - it doesn't touch
# page._network_log and never feeds the API chain/generation pipeline.

explore_runs = {}    # run_id -> {"stop": bool}, checked once per loop iteration
explore_results = {}  # run_id -> finished run's data (incl. per-step screenshots), until /api/explore/publish consumes it


_SECRET_IN_TEXT_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|token|api[_-]?key|otp|cvv)\b(\s*[=:]\s*)(\S+)")


def _redact_secrets(text):
    """Masks `password=...`-style values in free text that gets echoed into case names,
    the on-screen summary and published reports. (The model still receives the real goal -
    it has to type the password.)"""
    return _SECRET_IN_TEXT_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text or "")


def _usage_of(result):
    """(input_tokens, output_tokens) from a call_llm result - Anthropic's `usage` block or
    the equivalent call_ollama builds. Cache reads/writes count as input."""
    u = (result or {}).get("usage") if isinstance(result, dict) else None
    if not u:
        return 0, 0
    inp = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    return inp, (u.get("output_tokens") or 0)


def _count_tokens(state, emit_log, result, label=""):
    """Adds one model call's usage to the run's running totals and logs it."""
    i, o = _usage_of(result)
    t = state.setdefault("tokens", {"in": 0, "out": 0, "calls": 0})
    t["in"] += i
    t["out"] += o
    t["calls"] += 1
    if i or o:
        emit_log(f"{label}  🔢 {i:,} in / {o:,} out  (run total {t['in'] + t['out']:,})", "info")


def _fmt_tokens(t):
    return f"{t['in']:,} in / {t['out']:,} out = {t['in'] + t['out']:,} tokens over {t['calls']} model call(s)"


def _format_explore_prompt(goal, step, max_steps, history, url, title, elements, console_errors):
    hist_lines = [f"  Step {h['step']}: {h['desc']} → {h['result']}" for h in history[-8:]]
    history_block = "\n".join(hist_lines) if hist_lines else "  (none yet — this is the first step)"

    el_lines = []
    for e in elements[:60]:
        bits = [f"[{e['ref']}] <{e['tag']}"]
        if e.get("type"):
            bits.append(f" type={e['type']}")
        bits.append(">")
        extra = []
        if e.get("value"):
            extra.append(f"value='{e['value']}'")
        if e.get("checked") is not None:
            extra.append(f"checked={e['checked']}")
        if e.get("disabled"):
            extra.append("disabled")
        if e.get("href"):
            extra.append(f"href={e['href']}")
        line = "".join(bits) + f" \"{e.get('name') or ''}\"" + (f" ({', '.join(extra)})" if extra else "")
        el_lines.append("  " + line)
    elements_block = "\n".join(el_lines) if el_lines else "  (no interactive elements found)"

    console_block = "\n".join(f"  {c}" for c in console_errors[-5:]) if console_errors else "  (none)"

    return f"""You are an autonomous web app QA tester. You control a real Chromium browser one action at a time.

GOAL: {goal}

Step {step + 1} of at most {max_steps}.

ACTION HISTORY:
{history_block}

CURRENT PAGE:
URL: {url}
TITLE: {title}

INTERACTIVE ELEMENTS (refer to one by its [ref] number only — this list is regenerated every step, numbers may change):
{elements_block}

CONSOLE ERRORS SO FAR:
{console_block}

Respond with ONLY a single JSON object, no markdown fences, no text outside it:
{{
  "thought": "short reasoning",
  "action": "click" | "type" | "press" | "navigate" | "wait" | "done" | "blocked",
  "ref": element ref number (required for click/type),
  "text": "text to type (required for type)",
  "submit_after_type": true or false (optional, presses Enter after typing),
  "key": "key name (required for press, e.g. Enter, Escape, Tab)",
  "url": "absolute url (required for navigate)",
  "wait_ms": milliseconds, max 3000 (required for wait),
  "finding": "required for done/blocked — one sentence: what you confirmed, or exactly what broke/blocked you"
}}

Rules:
- Exactly one action per response.
- Only use [ref] numbers that appear in the list above — never invent one.
- Use "done" as soon as the goal is clearly satisfied.
- Use "blocked" if a real problem stops you (error message, broken button, unexpected response, missing
  element) — put the concrete problem in "finding", this becomes bug-report feedback.
- Don't wander beyond what the goal requires.
- Behave like a careful human tester following one instruction: do what the goal says and nothing more. Do
  NOT try extra scenarios (wrong passwords, empty fields, other users...) unless the goal asks for them.
- If your last action succeeded but the page hasn't visibly changed yet, the app may still be loading: use
  "wait" once before repeating the same action. Never click the same button twice in a row hoping it works.
"""


def _parse_explore_action(text):
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if m:
        cleaned = m.group(0)
    return json.loads(cleaned)


def _take_screenshot(portal):
    shot = pw_call({"action": "screenshot", "portal": portal}, timeout=10)
    return shot.get("b64") if shot.get("ok") else None


def _format_case_plan_prompt(goal, max_cases):
    return f"""You are an expert QA engineer planning a browser-based UI test session.

CONTEXT / GOAL (one line, from the user): {goal}

Break this into a focused set of up to {max_cases} concrete, independently checkable UI test cases that give
good breadth of coverage for what's described - happy path plus the edge cases, validation/negative cases, and
boundary conditions a careful tester would check around it. Each case will be handed one at a time to a browser
automation agent that can click/type/navigate/wait, continuing from wherever the browser is left after the
previous case (so if an earlier case already logged in, a later case should assume that session is still active
instead of logging in again, unless the case is specifically about testing login itself).

Respond with ONLY a JSON array, no markdown fences, no text outside it. Each element:
{{
  "name": "short case title, e.g. 'Logs water intake successfully'",
  "category": "Happy path" | "Edge case" | "Negative path" | "Boundary",
  "objective": "one sentence starting with 'Verify that...'",
  "instruction": "precise, self-contained instructions for the automation agent to execute this one case,
                  including concrete test data to use (exact values, not placeholders)"
}}

Rules:
- Order cases so earlier ones establish state later ones can rely on (e.g. log in first).
- Keep each case achievable in a small number of UI actions.
- Use the exact credentials/values given in the goal above where relevant instead of inventing new ones.
- Return at least 1 and at most {max_cases} cases.
"""


def _parse_case_plan(text, fallback_goal):
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', (text or "").strip(), flags=re.MULTILINE).strip()
    m = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if m:
        cleaned = m.group(0)
    try:
        cases = json.loads(cleaned)
        if isinstance(cases, list) and cases:
            return [
                {
                    "name": str(c.get("name") or f"Case {i + 1}")[:120],
                    "category": str(c.get("category") or "Happy path"),
                    "objective": str(c.get("objective") or fallback_goal),
                    "instruction": str(c.get("instruction") or fallback_goal),
                }
                for i, c in enumerate(cases)
            ]
    except Exception:
        pass
    return [{"name": fallback_goal[:120], "category": "Happy path", "objective": fallback_goal,
             "instruction": fallback_goal}]


def _run_case(emit_log, state, instruction, portal, provider, api_key, model, ollama_url, max_steps, label, screenshots=False):
    """Runs one planned test case's own click/type/.../done-or-blocked loop, continuing
    on whatever page state the previous case left the browser in (no reset in between —
    that's what lets a later case assume an earlier case's login/setup already happened)."""
    # Screenshots only feed the optional HTML report (the model reads the page as text), and
    # capturing one makes a visible browser flicker, so they're opt-in.
    history = []
    outcome, finding = "step_limit", ""

    for step in range(max_steps):
        if state.get("stop"):
            outcome = "stopped_by_user"
            emit_log(f"{label}Stopped by user.", "warn")
            break

        snap = pw_call({"action": "dom_snapshot", "portal": portal}, timeout=15)
        if not snap.get("ok"):
            emit_log(f"{label}Could not read page: {snap.get('error')}", "err")
            outcome, finding = "error", snap.get("error", "")
            break

        prompt = _format_explore_prompt(
            instruction, step, max_steps, history, snap.get("url"), snap.get("title"),
            snap.get("elements", []), snap.get("console_errors", []),
        )
        result, _ = call_llm(provider, prompt, max_tokens=500, api_key=api_key, model=model, ollama_url=ollama_url)
        _count_tokens(state, emit_log, result, label)
        text = "".join(b.get("text", "") for b in result.get("content", [])) if isinstance(result, dict) else ""
        if not text:
            emit_log(f"{label}Model call failed: {(result or {}).get('error', 'no response')}", "err")
            outcome, finding = "error", (result or {}).get("error", "no response")
            break

        try:
            action = _parse_explore_action(text)
        except Exception:
            emit_log(f"{label}Could not parse model response as JSON: {text[:200]}", "err")
            history.append({"step": step + 1, "desc": "(unparseable model response)", "result": "error"})
            continue

        kind = action.get("action")
        if action.get("thought"):
            emit_log(f"{label}Step {step + 1}: {action['thought']}", "thought")

        if kind == "done":
            outcome, finding = "done", action.get("finding", "Goal appears satisfied.")
            emit_log(f"{label}✅ Done: {finding}", "ok")
            history.append({"step": step + 1, "desc": f"confirm: {finding}", "result": "ok",
                             "screenshot": (_take_screenshot(portal) if screenshots else None)})
            break
        if kind == "blocked":
            outcome, finding = "blocked", action.get("finding", "Blocked — could not proceed.")
            emit_log(f"{label}🚫 Blocked: {finding}", "err")
            history.append({"step": step + 1, "desc": f"blocked: {finding}", "result": "error: blocked",
                             "screenshot": (_take_screenshot(portal) if screenshots else None)})
            break

        if kind == "click":
            desc = f"click [{action.get('ref')}]"
            exec_res = pw_call({"action": "click_ref", "portal": portal, "ref": action.get("ref")}, timeout=10)
        elif kind == "type":
            # Never echo what was typed into a password field into the log / published report.
            target = next((e for e in snap.get("elements", []) if e.get("ref") == action.get("ref")), {})
            shown = "••••••" if (target.get("type") or "").lower() == "password" else action.get("text", "")
            desc = f"type into [{action.get('ref')}]: '{shown}'"
            exec_res = pw_call({"action": "type_ref", "portal": portal, "ref": action.get("ref"),
                                 "text": action.get("text", ""),
                                 "submit_after_type": bool(action.get("submit_after_type"))}, timeout=10)
        elif kind == "press":
            key = action.get("key", "Enter")
            desc = f"press '{key}'"
            exec_res = pw_call({"action": "press_key", "portal": portal, "key": key}, timeout=10)
        elif kind == "navigate":
            desc = f"navigate to {action.get('url', '')}"
            exec_res = pw_call({"action": "navigate", "portal": portal, "url": action.get("url", "")}, timeout=30)
        elif kind == "wait":
            ms = min(int(action.get("wait_ms") or 1000), 3000)
            desc = f"wait {ms}ms"
            time.sleep(ms / 1000)
            exec_res = {"ok": True}
        else:
            desc, exec_res = f"unknown action '{kind}'", {"ok": False, "error": "unrecognized action"}

        ok = bool(exec_res.get("ok"))
        emit_log(f"{label}  → {desc} ({'ok' if ok else 'error: ' + str(exec_res.get('error'))})", "ok" if ok else "err")
        history.append({"step": step + 1, "desc": desc, "result": "ok" if ok else f"error: {exec_res.get('error')}",
                         "screenshot": (_take_screenshot(portal) if screenshots else None)})
        time.sleep(0.6)
    else:
        finding = finding or "Reached the step limit before the case was confirmed done."
        emit_log(f"{label}⏸ Reached step limit ({max_steps}).", "warn")

    return outcome, finding, history


def run_explore_loop(run_id, goal, portal, provider, api_key, model, ollama_url, max_steps, max_cases, record_steps=False, screenshots=False, broaden=False):
    state = explore_runs[run_id]

    def emit_log(msg, kind="info"):
        with app.app_context():
            socketio.emit("explore_log", {"run_id": run_id, "msg": msg, "kind": kind})

    if broaden:
        emit_log(f"Planning test cases for: {_redact_secrets(goal)}")
        plan_prompt = _format_case_plan_prompt(goal, max_cases)
        plan_result, _ = call_llm(provider, plan_prompt, max_tokens=2000, api_key=api_key, model=model, ollama_url=ollama_url)
        _count_tokens(state, emit_log, plan_result, "[plan] ")
        plan_text = "".join(b.get("text", "") for b in plan_result.get("content", [])) if isinstance(plan_result, dict) else ""
        cases_plan = _parse_case_plan(plan_text, goal)[:max_cases]
        emit_log(f"📋 Planned {len(cases_plan)} case(s): " + "; ".join(_redact_secrets(c["name"]) for c in cases_plan))
    else:
        # Act like a human tester given one instruction: do what it says, nothing more. No
        # planning call, so no invented edge/negative cases (and one fewer model call).
        first_line = _redact_secrets(goal.strip().splitlines()[0] if goal.strip() else "Goal")
        cases_plan = [{"name": first_line[:120], "category": "Happy path", "objective": _redact_secrets(goal), "instruction": goal}]
        emit_log("Following your goal as written - one case, no extra edge or negative cases.")
    state.setdefault("tokens", {"in": 0, "out": 0, "calls": 0})
    plan_tokens = dict(state["tokens"])

    snap0 = pw_call({"action": "dom_snapshot", "portal": portal}, timeout=15)
    start_url = snap0.get("url") if snap0.get("ok") else None

    if record_steps:
        # Replaces any steps already in the recorder; the AI's actions are captured
        # by the same in-page recorder a human uses, so only actions that really
        # happened are kept (a failed click/fill produces no event).
        rec = pw_call({"action": "ui_record_start", "portal": portal, "toolbar": False}, timeout=15)
        if rec.get("ok"):
            emit_log("⏺ Recording the AI's actions as UI test steps.")
        else:
            record_steps = False
            emit_log(f"Could not start step recording: {rec.get('error')}", "warn")

    cases_results = []
    for i, case in enumerate(cases_plan):
        if state.get("stop"):
            emit_log("Stopped by user before remaining cases ran.", "warn")
            break
        label = f"[{i + 1}/{len(cases_plan)}] "
        emit_log(f"{label}▶ {case['name']} — {case['objective']}")
        if record_steps:
            _ui_add_step({"type": "case", "value": case["name"]}, portal)
        before = dict(state.get("tokens") or {"in": 0, "out": 0, "calls": 0})
        outcome, finding, history = _run_case(
            emit_log, state, case["instruction"], portal, provider, api_key, model, ollama_url, max_steps, label,
            screenshots,
        )
        now = state.get("tokens") or {"in": 0, "out": 0, "calls": 0}
        case_tokens = {k: now[k] - before[k] for k in ("in", "out", "calls")}
        cases_results.append({**case, "outcome": outcome, "finding": finding, "history": history, "tokens": case_tokens})

    if record_steps:
        pw_call({"action": "ui_record_stop"}, timeout=15)
        _ui_emit_steps()
        emit_log(f"⏹ Recorded {len(ui_rec['steps'])} step(s) - review them in 'Record UI test'.", "ok")

    passed = sum(1 for c in cases_results if c["outcome"] == "done")
    if not cases_results:
        overall_outcome = "error"
    elif all(c["outcome"] == "done" for c in cases_results):
        overall_outcome = "done"
    elif any(c["outcome"] == "blocked" for c in cases_results):
        overall_outcome = "blocked"
    else:
        overall_outcome = "partial"
    summary = f"{passed}/{len(cases_results)} passed" if cases_results else "no cases ran"

    feedback_lines = [f"Goal: {_redact_secrets(goal)}", f"Cases: {summary}"]
    for c in cases_results:
        tk = c.get("tokens") or {"in": 0, "out": 0}
        feedback_lines.append(f"  [{c['category']}] {c['name']} — {c['outcome']}"
                               + (f": {c['finding']}" if c['finding'] else "")
                               + (f"  ({tk['in'] + tk['out']:,} tokens)" if tk["in"] + tk["out"] else ""))
    run_tokens = state.get("tokens") or {"in": 0, "out": 0, "calls": 0}
    if run_tokens["calls"]:
        feedback_lines.append(f"Tokens: {_fmt_tokens(run_tokens)} (planning: {plan_tokens['in'] + plan_tokens['out']:,})")
    feedback_text = "\n".join(feedback_lines)

    explore_results[run_id] = {"goal": _redact_secrets(goal), "cases": cases_results, "start_url": start_url, "portal": portal,
                               "tokens": run_tokens}

    with app.app_context():
        socketio.emit("explore_done", {
            "run_id": run_id, "outcome": overall_outcome, "summary": summary, "feedback": feedback_text,
            "recorded_steps": len(ui_rec["steps"]) if record_steps else None,
            "tokens": run_tokens,
            "cases": [{"name": c["name"], "category": c["category"], "outcome": c["outcome"], "finding": c["finding"]}
                      for c in cases_results],
        })
    explore_runs.pop(run_id, None)


@socketio.on("explore_start")
def explore_start(data):
    if not PLAYWRIGHT_AVAILABLE:
        emit("error", {"msg": "Playwright not installed"}); return
    data   = data or {}
    goal   = (data.get("goal") or "").strip()
    portal = data.get("portal", "A")
    if not goal:
        emit("error", {"msg": "Enter a goal to explore"}); return
    if portal not in pw["pages"]:
        emit("error", {"msg": "Launch the browser first"}); return
    provider   = data.get("provider", "anthropic")
    api_key    = (data.get("api_key") or "").strip()
    if provider == "anthropic" and not api_key:
        emit("error", {"msg": "API key required"}); return
    max_steps = max(1, min(int(data.get("max_steps") or 10), 40))
    max_cases = max(1, min(int(data.get("max_cases") or 6), 12))

    run_id = f"explore-{int(time.time())}"
    explore_runs[run_id] = {"stop": False}
    threading.Thread(
        target=run_explore_loop,
        args=(run_id, goal, portal, provider, api_key, data.get("model", ""), data.get("ollama_url", ""),
              max_steps, max_cases, bool(data.get("record_steps")), bool(data.get("screenshots")), bool(data.get("broaden"))),
        daemon=True,
    ).start()
    emit("explore_started", {"run_id": run_id})


@socketio.on("explore_stop")
def explore_stop(data):
    run_id = (data or {}).get("run_id")
    if run_id in explore_runs:
        explore_runs[run_id]["stop"] = True
        emit("log", {"msg": f"Stop requested for exploration {run_id}"})


_CASE_CATEGORY_CLASS = {
    "Happy path": "cat-happy", "Edge case": "cat-edge",
    "Negative path": "cat-negative", "Boundary": "cat-boundary",
}


def _build_explore_report_html(goal, cases, start_url, portal, tokens=None):
    rows = []
    cases = [{**c, "name": _redact_secrets(c.get("name", "")), "objective": _redact_secrets(c.get("objective", "")),
              "finding": _redact_secrets(c.get("finding", ""))} for c in cases]
    for case_idx, case in enumerate(cases):
        status_cls, status_label = {
            "done": ("pass", "PASS"), "blocked": ("fail", "FAIL"),
        }.get(case.get("outcome"), ("warn", "INCOMPLETE"))

        rows_data = case.get("history") or [{"desc": "(no steps recorded)", "result": "—", "screenshot": None}]
        n = len(rows_data)

        for i, h in enumerate(rows_data):
            is_fail = str(h.get("result", "")).startswith("error")
            icon, icon_cls = ("✗", "fail") if is_fail else ("✓", "pass")
            shot = h.get("screenshot")
            shot_cell = (
                f'<a href="data:image/jpeg;base64,{shot}" target="_blank">'
                f'<img src="data:image/jpeg;base64,{shot}" class="step-shot"></a>'
            ) if shot else "—"

            cells = []
            if i == 0:
                cells.append(f'<td rowspan="{n}">{html.escape(case.get("name", ""))}</td>')
                cat = case.get("category", "Happy path")
                cat_cls = _CASE_CATEGORY_CLASS.get(cat, "cat-happy")
                cells.append(f'<td rowspan="{n}"><span class="cat-badge {cat_cls}">{html.escape(cat)}</span></td>')
                cells.append(f'<td rowspan="{n}">{html.escape(case.get("objective", ""))}</td>')
                precondition = (f"App open at {start_url}" if case_idx == 0 and start_url
                                else "Continuing from the previous case's state")
                cells.append(f'<td rowspan="{n}">{html.escape(precondition)}</td>')
            cells.append(f'<td><span class="step-icon {icon_cls}">{icon}</span>{html.escape(str(h.get("desc", "")))}</td>')
            cells.append('<td>—</td>')
            cells.append(f'<td>{html.escape(str(h.get("result", "")))}</td>')
            cells.append(f'<td>{shot_cell}</td>')
            if i == 0:
                cells.append(f'<td rowspan="{n}"><span class="status {status_cls}">{status_label}</span></td>')
            rows.append('<tr>' + ''.join(cells) + '</tr>')

    table = (
        '<table><thead><tr>'
        '<th>Name</th><th>Category</th><th>Objective</th><th>Precondition</th>'
        '<th>Test Script (Step-by-Step) — Step</th>'
        '<th>Test Script (Step-by-Step) — Test Data</th>'
        '<th>Test Script (Step-by-Step) — Expected Result</th>'
        '<th>Screenshot</th><th>Test Execution</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )

    passed = sum(1 for c in cases if c.get("outcome") == "done")

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>AI Explore report</title>
<style>
body {{ background:#0b0b12; color:#e6e6f0; font-family:ui-monospace,monospace; padding:24px; max-width:1300px; margin:0 auto; }}
h1 {{ font-size:18px; }}
.path {{ color:#8a8aa0; font-size:12px; }}
.status {{ display:inline-block; padding:2px 10px; border-radius:6px; font-size:12px; margin-left:10px; }}
.pass {{ background:#134e2e; color:#4ade80; }}
.fail {{ background:#4c1d24; color:#f87171; }}
.warn {{ background:#4a3a12; color:#facc15; }}
.cat-badge {{ display:inline-block; padding:2px 8px; border-radius:5px; font-size:10.5px; }}
.cat-happy {{ background:#1d3a2e; color:#4ade80; }}
.cat-edge {{ background:#1e2c4a; color:#7dd3fc; }}
.cat-negative {{ background:#4c1d24; color:#f87171; }}
.cat-boundary {{ background:#4a3a12; color:#facc15; }}
.goal-label {{ font-size:11px; letter-spacing:1px; color:#8a8aa0; margin:16px 0 4px; }}
.goal-text {{ background:#14141f; border:1px solid #2a2a3a; border-radius:8px; padding:12px 16px; white-space:pre-wrap; font-size:12.5px; margin:0; }}
table {{ width:100%; border-collapse:collapse; margin:16px 0 20px; font-size:12px; table-layout:fixed; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #24243a; vertical-align:top; word-wrap:break-word; }}
th {{ color:#8a8aa0; font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:0.5px; }}
tr:hover td {{ background:#14141f; }}
.step-row td {{ background:#1b1b2b; color:#c4b5fd; font-weight:600; }}
.objective {{ color:#8a8aa0; font-weight:400; }}
td.data {{ font-size:11.5px; color:#b8b8cc; }}
td.num {{ color:#8a8aa0; }}
.step-icon {{ display:inline-block; width:14px; text-align:center; font-weight:700; margin-right:6px; background:none; padding:0; }}
.step-icon.pass {{ color:#4ade80; }}
.step-icon.fail {{ color:#f87171; }}
.step-shot {{ max-width:110px; max-height:160px; border-radius:4px; border:1px solid #2a2a3a; display:block; }}
</style></head><body>
<h1>AI Explore report <span class="status {'pass' if passed == len(cases) else 'warn'}">{passed}/{len(cases)} PASSED</span></h1>
<p class="path">Generated UI test cases · {len(cases)} case(s) · Portal {html.escape(portal)}</p>
<div class="goal-label">CONTEXT</div><pre class="goal-text">{html.escape(_redact_secrets(goal))}</pre>
{('<div class="goal-label">AI TOKENS USED</div><pre class="goal-text">' + html.escape(_fmt_tokens(tokens)) + '</pre>') if tokens and tokens.get("calls") else ''}
{table}
</body></html>"""


@app.route("/api/explore/publish/<run_id>", methods=["POST"])
def publish_explore_report(run_id):
    data = explore_results.get(run_id)
    if not data:
        return jsonify({"error": f"No exploration result found for '{run_id}' — run an exploration first"}), 404

    import cf_r2
    if not cf_r2.is_configured():
        return jsonify({"error": "Cloudflare R2 is not configured — see CLOUDFLARE_R2_* in .env"}), 400

    report_html = _build_explore_report_html(data["goal"], data["cases"], data["start_url"], data["portal"], data.get("tokens"))
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "_", data["goal"][:40]) or "explore"
    filename = f"{slug}_{timestamp}.html"
    try:
        url = cf_r2.publish_html_report("api-testgen-explore-reports", filename, report_html)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"publish failed: {e}"}), 500

    explore_results.pop(run_id, None)
    return jsonify({"url": url})


# ── Suite Runner (runs an external Playwright/pytest project, auto-captures) ──
# Lets an already-instrumented external project (see tests/testgen_capture.py in
# that project) be driven straight from this UI instead of a separate terminal.
# Runs with THAT project's own interpreter/venv, since it has its own deps
# (pytest-playwright, page objects, etc.) this server's venv knows nothing about.
# No sandboxing beyond path/arg validation: this is the same trust model as
# /api/scripts/run — a localhost, single-user dev tool that already executes
# LLM-generated code, so a second arbitrary-pytest-invocation surface doesn't
# change the threat model.

suite_runs = {}  # run_id -> subprocess.Popen, so a run can be stopped


def _venv_python(project_root):
    for candidate in ("venv/bin/python", ".venv/bin/python", "venv/bin/python3", ".venv/bin/python3"):
        p = os.path.join(project_root, candidate)
        if os.path.isfile(p):
            return p
    return "python3"


@socketio.on("run_playwright_suite")
def run_playwright_suite(data):
    data         = data or {}
    project_root = os.path.realpath((data.get("project_root") or "").strip())
    test_args    = (data.get("test_path") or "").strip()
    session_name = (data.get("session_name") or "").strip()

    if not project_root or not os.path.isdir(project_root):
        emit("playwright_run_error", {"msg": f"Project path not found: {project_root}"})
        return
    if not test_args:
        emit("playwright_run_error", {"msg": "Enter a test path or pytest args, e.g. tests/PlayWrightTests/... or -m some_marker"})
        return
    try:
        arg_list = shlex.split(test_args)
    except ValueError as e:
        emit("playwright_run_error", {"msg": f"Could not parse test args: {e}"})
        return

    run_id = session_name or f"suite-run-{int(time.time())}"
    python_exe = _venv_python(project_root)
    cmd = [python_exe, "-m", "pytest", *arg_list, "-v"]

    def stream():
        env = dict(os.environ)
        env["CAPTURE_TO_TESTGEN"]   = "1"
        env["TESTGEN_URL"]          = f"http://localhost:{SERVER_PORT}"
        env["TESTGEN_SESSION_NAME"] = run_id

        with app.app_context():
            socketio.emit("playwright_run_log", {"line": f"$ {' '.join(cmd)}\n(cwd={project_root})\n\n"})
        try:
            proc = subprocess.Popen(
                cmd, cwd=project_root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            suite_runs[run_id] = proc
            for line in proc.stdout:
                with app.app_context():
                    socketio.emit("playwright_run_log", {"line": line})
            proc.wait()
            with app.app_context():
                socketio.emit("playwright_run_done", {"returncode": proc.returncode, "session_name": run_id})
        except FileNotFoundError as e:
            with app.app_context():
                socketio.emit("playwright_run_error", {"msg": f"Could not start '{python_exe}': {e}"})
        finally:
            suite_runs.pop(run_id, None)

    threading.Thread(target=stream, daemon=True).start()
    emit("playwright_run_started", {"run_id": run_id, "cmd": " ".join(cmd)})


@socketio.on("stop_playwright_suite")
def stop_playwright_suite(data):
    run_id = ((data or {}).get("run_id") or "").strip()
    proc = suite_runs.get(run_id)
    if not proc:
        emit("error", {"msg": "No matching run to stop"})
        return
    proc.terminate()
    emit("log", {"msg": f"Stop signal sent to '{run_id}'"})


# ── Script Runner (run one existing test file from TESTLOKE_PROJECT_ROOT, ──
# ── publish its Allure report to Cloudflare R2) ──────────────────────────────
# Distinct from Suite Runner above (which drives a whole capture-instrumented
# suite): this runs ONE already-written pytest file as-is with --alluredir,
# then uploads the generated report - same two-step "run it, then publish
# the report" shape as mobile_agent's own Test Scripts tab + cf_r2.py.

testloke_runs = {}  # run_id -> {"mode": "pytest"|"script", "script", "returncode", "alluredir"?, "log"?}

_PYTEST_CONTENT_RE = re.compile(r'^\s*(async\s+)?def\s+test_\w*\s*\(|^\s*class\s+Test\w*', re.MULTILINE)


def _testloke_scripts():
    """Every test_*.py/*_test.py file under TESTLOKE_PROJECT_ROOT (pytest's
    own discovery convention), as paths relative to it - skips venv/.git/
    cache dirs so this doesn't waste time walking dependency trees. Not
    every file matching this NAME is actually pytest-structured (see
    _is_pytest_file) - this is just what populates the picker."""
    root = TESTLOKE_PROJECT_ROOT
    if not root or not os.path.isdir(root):
        return []
    skip_dirs = {"venv", ".venv", ".git", "__pycache__", ".pytest_cache", "allure-results", "allure-report"}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            if name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py"):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _is_pytest_file(script_path: str) -> bool:
    """Content-based, not just filename-based - confirmed live that
    testLoke has a file (tests/dump/test_foodTarget.py) that matches the
    test_*.py NAMING convention but has no actual `def test_*`/`class
    Test*` inside it; it's a standalone script meant to be run directly
    with `python file.py`. A name-only check would send that through
    pytest and get "collected 0 items" instead of actually running it."""
    try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return False
    return bool(_PYTEST_CONTENT_RE.search(content))


@app.route("/api/testloke/scripts")
def list_testloke_scripts():
    if not TESTLOKE_PROJECT_ROOT:
        return jsonify({"error": "TESTLOKE_PROJECT_ROOT is not set - add it to .env", "scripts": []}), 400
    if not os.path.isdir(TESTLOKE_PROJECT_ROOT):
        return jsonify({"error": f"TESTLOKE_PROJECT_ROOT not found: {TESTLOKE_PROJECT_ROOT}", "scripts": []}), 400
    return jsonify({"project_root": TESTLOKE_PROJECT_ROOT, "scripts": _testloke_scripts()})


@socketio.on("run_testloke_script")
def run_testloke_script(data):
    data = data or {}
    script = (data.get("script") or "").strip()
    # "auto" (default) detects from file content; "pytest"/"script" force
    # a specific mode regardless of what auto-detection would pick - the
    # explicit escape hatch for whichever edge case content-sniffing gets
    # wrong, same reasoning as any heuristic in this codebase.
    force_mode = data.get("mode") or "auto"
    if not TESTLOKE_PROJECT_ROOT or not os.path.isdir(TESTLOKE_PROJECT_ROOT):
        emit("testloke_run_error", {"msg": f"TESTLOKE_PROJECT_ROOT not found: {TESTLOKE_PROJECT_ROOT}"})
        return
    script_path = os.path.realpath(os.path.join(TESTLOKE_PROJECT_ROOT, script))
    if not script or os.path.commonpath([script_path, os.path.realpath(TESTLOKE_PROJECT_ROOT)]) != os.path.realpath(TESTLOKE_PROJECT_ROOT) or not os.path.isfile(script_path):
        emit("testloke_run_error", {"msg": f"Script not found under TESTLOKE_PROJECT_ROOT: {script}"})
        return

    if force_mode in ("pytest", "script"):
        mode = force_mode
    else:
        mode = "pytest" if _is_pytest_file(script_path) else "script"

    run_id = f"testloke-run-{int(time.time())}"
    python_exe = _venv_python(TESTLOKE_PROJECT_ROOT)
    alluredir = None
    if mode == "pytest":
        alluredir = tempfile.mkdtemp(prefix="api_testgen_testloke_alluredir_")
        cmd = [python_exe, "-m", "pytest", script, "-v", f"--alluredir={alluredir}"]
    else:
        cmd = [python_exe, script]

    def stream():
        with app.app_context():
            socketio.emit("testloke_run_log", {"run_id": run_id, "line": f"$ {' '.join(cmd)}\n(cwd={TESTLOKE_PROJECT_ROOT}, mode={mode})\n\n"})
        log_lines = []
        try:
            proc = subprocess.Popen(
                cmd, cwd=TESTLOKE_PROJECT_ROOT, env=dict(os.environ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            for line in proc.stdout:
                log_lines.append(line)
                with app.app_context():
                    socketio.emit("testloke_run_log", {"run_id": run_id, "line": line})
            proc.wait()
            testloke_runs[run_id] = {
                "mode": mode, "script": script, "returncode": proc.returncode,
                "alluredir": alluredir, "log": "".join(log_lines),
            }
            with app.app_context():
                socketio.emit("testloke_run_done", {"run_id": run_id, "returncode": proc.returncode, "mode": mode})
        except FileNotFoundError as e:
            if alluredir:
                shutil.rmtree(alluredir, ignore_errors=True)
            with app.app_context():
                socketio.emit("testloke_run_error", {"msg": f"Could not start '{python_exe}': {e}"})

    threading.Thread(target=stream, daemon=True).start()
    emit("testloke_run_started", {"run_id": run_id, "script": script, "mode": mode, "cmd": " ".join(cmd)})


def _script_docstring(script_path: str) -> str:
    """The script's own module docstring, if it has one - test_foodTarget.py
    (and presumably other hand-written check scripts in the same style)
    already states its purpose there ("What it checks: ..."), which is a
    genuine, generalizable "Test Goal" statement rather than anything this
    server would have to invent."""
    try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
            tree = ast.parse(f.read())
        doc = ast.get_docstring(tree)
        return doc.strip() if doc else ""
    except Exception:
        return ""


_SECTION_RE = re.compile(r'^\s*(?:▶\s*|Step\s+[\d.]+:\s*)(.+?)\s*$')
_CHECK_RE = re.compile(r'^\s*\[(PASS|FAIL)\]\s+(.+?)\s*$')
_BEFORE_AFTER_RE = re.compile(r'^(?:before|expected)=(.*?)\s+(?:after|actual)=(.*)$')
_INPUT_RE = re.compile(r'^\s*input:\s*(.*)$', re.IGNORECASE)
_OUTPUT_RE = re.compile(r'^\s*output:\s*(.*)$', re.IGNORECASE)
_OBJECTIVE_RE = re.compile(r'^\s*objective:\s*(.*)$', re.IGNORECASE)
_PRECONDITION_RE = re.compile(r'^\s*precondition:\s*(.*)$', re.IGNORECASE)
_ARROW_ONLY_RE = re.compile(r'^\s*->\s*(.+)$')
_SUMMARY_START_RE = re.compile(r'^\s*SCENARIO SUMMARY:?\s*$', re.IGNORECASE)
_VALIDATION_END_RE = re.compile(r'^\s*={3,}\s*VALIDATION', re.IGNORECASE)


def _parse_standalone_log(log_text: str):
    """Best-effort structure extraction from a standalone script's stdout.
    Recognizes two section-header conventions - the original "▶ <section>"
    style (test_foodTarget.py and other hand-written check scripts already
    use this) and this tool's own "Step N: <description>" style (see
    report_convention in prompt_config.json's standalone_script_testing
    block) - then "   [PASS]/[FAIL] <description>" per check, with
    "  -> before=X after=Y" (or a plain "  -> <actual>") on failures only,
    an optional "input:"/"output:" line per section (before its first
    check) showing what was actually sent/received, and a trailing
    "SCENARIO SUMMARY:" paragraph running until a "=== VALIDATION ... ==="
    line. Returns None if the log doesn't contain a single recognizable
    check line, so the caller falls back to a plain raw-log report instead
    of rendering a table that's actually empty."""
    context_lines = []
    sections = []
    current = None
    any_check = False
    in_summary = False
    summary_lines = []

    for raw_line in log_text.splitlines():
        line = raw_line.rstrip("\n")

        if in_summary:
            if _VALIDATION_END_RE.match(line):
                in_summary = False
                continue
            summary_lines.append(line)
            continue
        if _SUMMARY_START_RE.match(line):
            in_summary = True
            continue

        m_section = _SECTION_RE.match(line)
        if m_section:
            current = {"name": m_section.group(1), "objective": "", "precondition": "", "input": "", "output": "", "checks": []}
            sections.append(current)
            continue
        m_check = _CHECK_RE.match(line)
        if m_check:
            any_check = True
            status, rest = m_check.group(1), m_check.group(2)
            description, expected, actual = rest, "—", "—"
            if "  -> " in rest:
                description, detail = rest.split("  -> ", 1)
                m_ba = _BEFORE_AFTER_RE.match(detail)
                if m_ba:
                    expected, actual = m_ba.group(1), m_ba.group(2)
                else:
                    actual = detail
            if current is None:
                current = {"name": "", "objective": "", "precondition": "", "input": "", "output": "", "checks": []}
                sections.append(current)
            current["checks"].append({
                "description": description.strip(), "expected": expected, "actual": actual, "status": status,
            })
            continue
        m_arrow = _ARROW_ONLY_RE.match(line)
        if m_arrow and current is not None and current["checks"]:
            detail = m_arrow.group(1)
            m_ba = _BEFORE_AFTER_RE.match(detail)
            last = current["checks"][-1]
            if m_ba:
                last["expected"], last["actual"] = m_ba.group(1), m_ba.group(2)
            else:
                last["actual"] = detail
            continue
        m_objective = _OBJECTIVE_RE.match(line)
        if m_objective and current is not None and not current["checks"]:
            current["objective"] = m_objective.group(1).strip()
            continue
        m_precondition = _PRECONDITION_RE.match(line)
        if m_precondition and current is not None and not current["checks"]:
            current["precondition"] = m_precondition.group(1).strip()
            continue
        m_input = _INPUT_RE.match(line)
        if m_input and current is not None and not current["checks"]:
            current["input"] = m_input.group(1).strip()
            continue
        m_output = _OUTPUT_RE.match(line)
        if m_output and current is not None and not current["checks"]:
            current["output"] = m_output.group(1).strip()
            continue
        if current is None and line.strip():
            context_lines.append(line)

    if not any_check:
        return None
    return {"context": context_lines, "sections": sections, "summary": "\n".join(summary_lines).strip()}


def _build_standalone_report_html(title, ok, returncode, script_rel, project_root, log_text, script_path):
    goal = _script_docstring(script_path)
    parsed = _parse_standalone_log(log_text)

    body = [
        f'<h1>{html.escape(title)} <span class="status {"pass" if ok else "fail"}">'
        f'{"PASSED" if ok else f"EXIT {returncode}"}</span></h1>',
        f'<p class="path">{html.escape(project_root)}/{html.escape(script_rel)} '
        f'— run as a standalone script (not pytest)</p>',
    ]
    if goal:
        body.append(f'<div class="goal-label">TEST GOAL</div><pre class="goal-text">{html.escape(goal)}</pre>')

    if parsed:
        if parsed["context"]:
            body.append(
                '<div class="goal-label">TEST DATA / CONTEXT</div><pre class="goal-text">'
                + html.escape("\n".join(parsed["context"])) + '</pre>'
            )
        rows = []
        n_pass = n_fail = 0
        for section in parsed["sections"]:
            checks = section["checks"]
            if not checks:
                continue
            name      = section["name"] or "—"
            objective = section.get("objective") or ""
            test_data = section.get("input") or "—"
            heading = html.escape(name) + (f' <span class="objective">— {html.escape(objective)}</span>' if objective else "")
            rows.append(f'<tr class="step-row"><td colspan="6">{heading}</td></tr>')
            for c in checks:
                ok_c = c["status"] == "PASS"
                n_pass += ok_c
                n_fail += (not ok_c)
                expected = c["expected"] if c["expected"] not in ("", "—") else "—"
                actual = c["actual"] if c["actual"] not in ("", "—") else (section.get("output") or "—")
                cls = "pass" if ok_c else "fail"
                rows.append(
                    f'<tr><td class="num">{n_pass + n_fail}</td>'
                    f'<td>{html.escape(c["description"])}</td>'
                    f'<td class="data">{html.escape(test_data)}</td>'
                    f'<td class="data">{html.escape(expected)}</td>'
                    f'<td class="data">{html.escape(actual)}</td>'
                    f'<td><span class="status {cls}">{"PASS" if ok_c else "FAIL"}</span></td></tr>'
                )

        if rows:
            body.append(f'<p><span class="status pass">{n_pass} passed</span><span class="status fail">{n_fail} failed</span></p>')
            body.append(
                '<table><thead><tr><th style="width:4%">#</th><th style="width:26%">Test description</th>'
                '<th style="width:22%">Test data</th><th style="width:20%">Expected result</th>'
                '<th style="width:20%">Actual result</th><th style="width:8%">Pass / Fail</th>'
                f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            )
        if parsed.get("summary"):
            body.append(
                '<div class="summary-box"><div class="summary-label">Scenario summary</div>'
                f'<p>{html.escape(parsed["summary"])}</p></div>'
            )
        body.append(f'<details><summary>Raw output</summary><pre>{html.escape(log_text)}</pre></details>')
    else:
        body.append(f'<pre>{html.escape(log_text)}</pre>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ background:#0b0b12; color:#e6e6f0; font-family:ui-monospace,monospace; padding:24px; max-width:1100px; margin:0 auto; }}
h1 {{ font-size:18px; }}
h2 {{ font-size:14px; margin-top:28px; color:#c4b5fd; border-bottom:1px solid #2a2a3a; padding-bottom:6px; }}
.path {{ color:#8a8aa0; font-size:12px; }}
.status {{ display:inline-block; padding:2px 10px; border-radius:6px; font-size:12px; margin-left:10px; }}
.pass {{ background:#134e2e; color:#4ade80; }}
.fail {{ background:#4c1d24; color:#f87171; }}
.goal-label {{ font-size:11px; letter-spacing:1px; color:#8a8aa0; margin:16px 0 4px; }}
.goal-text {{ background:#14141f; border:1px solid #2a2a3a; border-radius:8px; padding:12px 16px; white-space:pre-wrap; font-size:12.5px; margin:0; }}
.io-box {{ background:#14141f; border:1px solid #2a2a3a; border-radius:8px; padding:8px 12px; margin:6px 0 10px; font-size:11.5px; color:#8a8aa0; }}
.io-box b {{ color:#c4b5fd; }}
.summary-box {{ background:rgba(124,106,255,0.08); border:1px solid #4c3f8a; border-radius:8px; padding:14px 16px; margin-top:20px; }}
.summary-label {{ font-size:10.5px; letter-spacing:1px; text-transform:uppercase; color:#c4b5fd; margin-bottom:6px; }}
.summary-box p {{ margin:0; white-space:pre-wrap; font-size:12.5px; line-height:1.6; }}
table {{ width:100%; border-collapse:collapse; margin:10px 0 20px; font-size:12.5px; table-layout:fixed; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #24243a; vertical-align:top; word-wrap:break-word; }}
th {{ color:#8a8aa0; font-weight:600; text-transform:uppercase; font-size:10.5px; letter-spacing:0.5px; }}
tr:hover td {{ background:#14141f; }}
.step-row td {{ background:#1b1b2b; color:#c4b5fd; font-weight:600; }}
.objective {{ color:#8a8aa0; font-weight:400; }}
td.data {{ font-size:11.5px; color:#b8b8cc; }}
td.num {{ color:#8a8aa0; }}
.step-icon {{ display:inline-block; width:14px; text-align:center; font-weight:700; margin-right:6px; background:none; padding:0; }}
.step-icon.pass {{ color:#4ade80; }}
.step-icon.fail {{ color:#f87171; }}
details {{ margin-top:20px; }}
summary {{ cursor:pointer; color:#8a8aa0; font-size:12px; }}
pre {{ background:#14141f; border:1px solid #2a2a3a; border-radius:8px; padding:16px; white-space:pre-wrap; font-size:12px; overflow-x:auto; }}
</style></head><body>
{''.join(body)}
</body></html>"""


@app.route("/api/testloke/publish/<run_id>", methods=["POST"])
def publish_testloke_report(run_id):
    info = testloke_runs.get(run_id)
    if not info:
        return jsonify({"error": f"No run found for '{run_id}' - run a script first"}), 404

    import cf_r2
    lines = []
    try:
        if info["mode"] == "pytest":
            url = cf_r2.publish_allure_report(info["alluredir"], TESTLOKE_PROJECT_ROOT, lines.append)
        else:
            ok = info["returncode"] == 0
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
            title = os.path.basename(info["script"])
            script_path = os.path.join(TESTLOKE_PROJECT_ROOT, info["script"])
            report_html = _build_standalone_report_html(
                title, ok, info["returncode"], info["script"], TESTLOKE_PROJECT_ROOT, info["log"], script_path,
            )
            filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", title) + f"_{timestamp}.html"
            url = cf_r2.publish_html_report("testloke-reports", filename, report_html)
            lines.append(f"Published standalone-script report to Cloudflare R2: {url}")
    except RuntimeError as e:
        return jsonify({"error": str(e), "log": "\n".join(lines)}), 400
    except Exception as e:
        return jsonify({"error": f"publish failed: {e}", "log": "\n".join(lines)}), 500
    finally:
        if info.get("alluredir"):
            shutil.rmtree(info["alluredir"], ignore_errors=True)
        testloke_runs.pop(run_id, None)

    return jsonify({"url": url, "log": "\n".join(lines)})


# ── Config endpoints ──────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_prompt_config())


@app.route("/api/config", methods=["POST"])
def save_config_endpoint():
    with open(PROMPT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(request.json, f, indent=2)
    return jsonify({"saved": True})


# ── Sessions (persist a capture so it survives a restart) ─────────────────────

SESSION_NAME_RE = re.compile(r'^[\w \-]{1,80}$')


def _session_path(name):
    """Raises ValueError for anything that isn't a plain, non-traversing name."""
    if not name or not SESSION_NAME_RE.match(name):
        raise ValueError("Session name must be 1-80 chars: letters, numbers, spaces, _ or -")
    return os.path.join(SESSIONS_DIR, name.strip() + ".json")


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    out = []
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        out.append({
            "name":        fname[:-len(".json")],
            "saved_at":    data.get("saved_at"),
            "entry_count": len(data.get("network", [])),
        })
    out.sort(key=lambda s: s.get("saved_at") or "", reverse=True)
    return jsonify({"sessions": out})


@app.route("/api/sessions", methods=["POST"])
def save_session():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    try:
        path = _session_path(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    payload = {
        "saved_at": datetime.now().isoformat(),
        "network":  data.get("network", []),
        "selected": data.get("selected", []),
        "scenario": data.get("scenario", ""),
        "links":    data.get("links", []),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return jsonify({"saved": True, "name": name})


@app.route("/api/sessions/<name>", methods=["GET"])
def load_session(name):
    try:
        path = _session_path(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": f"No session named '{name}'"}), 404
    with open(path, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/sessions/<name>", methods=["DELETE"])
def delete_session(name):
    try:
        path = _session_path(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": f"No session named '{name}'"}), 404
    os.remove(path)
    return jsonify({"deleted": True})


# ── REST ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/status")
def status():
    return jsonify({
        "playwright_available": PLAYWRIGHT_AVAILABLE,
        "connected":            session["connected"],
        "target_url":           DEFAULT_TARGET_URL,
    })


@app.route("/api/scripts/save", methods=["POST"])
def save_scripts():
    scripts = request.json.get("scripts", [])
    saved   = []
    for s in scripts:
        path = os.path.join(OUTPUT_DIR, s["filename"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(s["content"])
        saved.append(path)
    return jsonify({"saved": saved})


@app.route("/api/scripts/run", methods=["POST"])
def run_script():
    """Runs a saved file (must already exist under OUTPUT_DIR — call
    /api/scripts/save first) and returns its output. These are real
    integration tests that hit the live target API — this genuinely
    executes them, it doesn't just lint or import-check.

    Auto-detects which of the two generation modes produced the file, the
    same way _is_pytest_file already does for the Script Runner below: a
    pytest-mode file (has def test_*/class Test*) runs with `python -m
    pytest`; a standalone-mode file (plain script, no pytest content) runs
    directly with `python <file>` instead — running it under pytest would
    just collect 0 items."""
    test_file = (request.json or {}).get("test_file", "")
    full_path = os.path.realpath(os.path.join(OUTPUT_DIR, test_file))
    if not full_path.startswith(os.path.realpath(OUTPUT_DIR) + os.sep):
        return jsonify({"error": "Invalid test file path"}), 400
    if not os.path.isfile(full_path):
        return jsonify({"error": f"'{test_file}' not found — save the generated files first"}), 404

    is_pytest = _is_pytest_file(full_path)
    cmd = [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short", "--color=no"] if is_pytest \
        else [sys.executable, test_file]

    try:
        proc = subprocess.run(cmd, cwd=OUTPUT_DIR, capture_output=True, text=True, timeout=120)
        return jsonify({
            "passed":     proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout":     proc.stdout,
            "stderr":     proc.stderr,
            "mode":       "pytest" if is_pytest else "standalone",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Test run timed out after 120s"}), 504
    except FileNotFoundError:
        return jsonify({"error": "pytest not found in this environment — pip install pytest"}), 500


@app.route("/api/scripts/publish-report", methods=["POST"])
def publish_script_report():
    """Wraps a /api/scripts/run result (must be re-sent here, this endpoint
    keeps no run state) into the same styled HTML report Script Runner's
    testloke publish flow produces, and uploads it to Cloudflare R2 for a
    public URL — so a run here can be linked or pasted straight into a
    Jira ticket instead of only living in this UI's run-results panel."""
    import cf_r2
    if not cf_r2.is_configured():
        return jsonify({"error": "Cloudflare R2 is not configured — see CLOUDFLARE_R2_* in .env"}), 400

    data = request.json or {}
    test_file = (data.get("test_file") or "").strip()
    log_text  = (data.get("log") or "")
    passed    = bool(data.get("passed"))
    returncode = data.get("returncode", 0 if passed else 1)

    full_path = os.path.realpath(os.path.join(OUTPUT_DIR, test_file))
    if not test_file or not full_path.startswith(os.path.realpath(OUTPUT_DIR) + os.sep):
        return jsonify({"error": "Invalid test file path"}), 400
    if not os.path.isfile(full_path):
        return jsonify({"error": f"'{test_file}' not found — save/run the generated files first"}), 404

    title = os.path.basename(test_file)
    report_html = _build_standalone_report_html(title, passed, returncode, test_file, OUTPUT_DIR, log_text, full_path)

    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", title) + f"_{timestamp}.html"
    try:
        url = cf_r2.publish_html_report("api-testgen-reports", filename, report_html)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"publish failed: {e}"}), 500

    return jsonify({"url": url})


def call_claude(api_key, prompt, max_tokens=16000, model=""):
    """Calls the Anthropic Messages API. Returns (response_json, status_code)."""
    payload = json.dumps({
        "model":      model or CLAUDE_DEFAULT_MODEL,
        "max_tokens": max_tokens,
        # One-shot code generation from a heavily-prescriptive prompt, not exploratory
        # tool-use reasoning — adaptive thinking just eats into max_tokens with no benefit,
        # and can truncate the generated file before it's complete.
        "thinking":   {"type": "disabled"},
        "messages":   [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data    = payload,
        headers = {
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method = "POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8")), 200
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode("utf-8")}, e.code
    except Exception as e:
        return {"error": str(e)}, 500


def call_ollama(prompt, model=None, max_tokens=16000, ollama_url=None):
    """Calls a local Ollama server's chat API. Returns (response_json, status_code)
    normalized to the same {"content": [{"type": "text", "text": ...}]} shape
    call_claude returns, so callers don't need to branch on provider."""
    base = (ollama_url or OLLAMA_URL).rstrip("/")

    # Ollama defaults to a small runtime context window unless num_ctx is set explicitly,
    # silently truncating the FRONT of the prompt once exceeded — which for us means the
    # captured API sequence (placed first) can vanish while the model still sees the later
    # framework rules. Size num_ctx off the actual prompt + expected output instead of
    # trusting Ollama's default. chars//2 (not //4): code-heavy text (quotes, dots, parens)
    # tokenizes less efficiently than prose.
    estimated_prompt_tokens = len(prompt) // 2
    num_ctx = max(8192, min(32768, estimated_prompt_tokens + max_tokens + 1024))

    payload = json.dumps({
        "model":    model or OLLAMA_DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream":   False,
        "think":    False,  # hybrid-reasoning models (e.g. qwen3) otherwise burn the whole
                             # num_predict budget on <think> tokens and return empty content
        "options":  {"num_predict": max_tokens, "num_ctx": num_ctx}
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/api/chat",
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST"
    )
    timeout_msg = (
        f"Ollama didn't respond within {OLLAMA_TIMEOUT}s. Large prompts + local CPU inference "
        "can take several minutes — try a smaller/faster model, a shorter scenario (fewer selected "
        "API calls), or raise the OLLAMA_TIMEOUT env var."
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data.get("message", {}).get("content", "")
            # Normalize to Anthropic's stop_reason vocabulary so callers can check
            # response["stop_reason"] == "max_tokens" regardless of provider.
            stop_reason = "max_tokens" if data.get("done_reason") == "length" else data.get("done_reason")
            return {"content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
                    "usage": {"input_tokens": data.get("prompt_eval_count") or 0,
                              "output_tokens": data.get("eval_count") or 0}}, 200
    except TimeoutError:
        return {"error": timeout_msg}, 504
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return {"error": timeout_msg}, 504
        return {"error": f"Could not reach Ollama at {base} — is it running? ({e.reason if hasattr(e, 'reason') else e})"}, 502
    except Exception as e:
        return {"error": str(e)}, 500


def call_llm(provider, prompt, max_tokens=16000, api_key="", model="", ollama_url=""):
    """Dispatches to the configured AI provider. Returns (response_json, status_code)."""
    if provider == "ollama":
        return call_ollama(prompt, model, max_tokens, ollama_url)
    else:  # default to anthropic
        return call_claude(api_key, prompt, max_tokens, model=model)


@app.route("/api/ollama/models", methods=["GET"])
def list_ollama_models():
    base = request.args.get("url", OLLAMA_URL).rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name") for m in data.get("models", []) if m.get("name")]
            return jsonify({"models": models})
    except Exception as e:
        return jsonify({"error": f"Could not reach Ollama at {base} — is it running? ({e})"}), 502


# ── API Chaining ──────────────────────────────────────────────────────────────

def _format_headers_for_prompt(headers):
    """Keep only headers likely to carry chained values; drop cookie/UA/sec-fetch-* noise."""
    if not headers:
        return "{}"
    keep = {
        k: v for k, v in headers.items()
        if (k or "").lower() in ("content-type", "authorization") or (k or "").lower().startswith("x-")
    }
    return json.dumps(keep) if keep else "{}"


def _format_api_chain_prompt(scenario, entries, links, framework_context):
    ordered = sorted(entries, key=lambda e: e.get("ts") or 0)

    steps_text = []
    for i, e in enumerate(ordered):
        steps_text.append(
            f"Step {i}: {e.get('method')} {e.get('url')}\n"
            f"  Request headers: {_format_headers_for_prompt(e.get('requestHeaders'))}\n"
            f"  Request body: {(e.get('requestBody') or '')[:1000]}\n"
            f"  Response status: {e.get('status')}\n"
            f"  Response body: {(e.get('responseBody') or '')[:1000]}"
        )
    steps_block = "\n\n".join(steps_text)

    links_lines = [
        f"  - Step {l.get('from_step')}'s response field '{l.get('from_path')}' "
        f"(value like '{l.get('value_preview')}') flows into Step {l.get('to_step')}'s "
        f"{l.get('location')} ({l.get('location_detail')})"
        for l in links
    ]
    links_block = "\n".join(links_lines) if links_lines else \
        "  (none confirmed — infer reasonable chaining from the captured calls above)"

    return f"""You are an expert API test automation engineer using Python + requests.

TEST SCENARIO: {scenario or 'Validate this API call sequence end-to-end'}

CAPTURED API CALL SEQUENCE (chronological):
{steps_block}

DETECTED VALUE CHAINING (confirmed by the user — use these exactly):
{links_block}

FRAMEWORK RULES — follow these exactly, no deviations:

{framework_context}

Generate this chain of calls against the real API, extracting values from each response and feeding them
into the next request precisely as described above, then assert the end state proves the functionality
actually works — not just status codes, but that the returned data reflects the expected mutation/behavior.

Output EXACTLY two files, in this exact order, using the FILE OUTPUT and API CLIENT ARCHITECTURE rules above:
1. # FILE: api_clients/<domain>_client.py — must fully define every client class (e.g. AuthClient, OrdersClient)
   that the test file below imports. Do not import a client class without defining it in this same file.
2. # FILE: tests/test_<scenario>.py — imports the classes from file 1 and contains ONLY the test class:
   no requests/session/header/URL code, just client instantiation, method calls, and data assertions.

In file 2, run the ENTIRE chain exactly once inside @classmethod def setup_class(cls), storing every
client and response value later tests need as class attributes. Then write MULTIPLE small test methods —
one per aspect of the outcome — that only read those class attributes and assert on them. Do not write a
single test method that both performs the chain and asserts everything.

Before writing file 2, re-check that every class/method it references was actually defined in file 1.
"""


def _format_standalone_script_prompt(scenario, entries, links, framework_context):
    ordered = sorted(entries, key=lambda e: e.get("ts") or 0)

    steps_text = []
    for i, e in enumerate(ordered):
        steps_text.append(
            f"Step {i}: {e.get('method')} {e.get('url')}\n"
            f"  Request headers: {_format_headers_for_prompt(e.get('requestHeaders'))}\n"
            f"  Request body: {(e.get('requestBody') or '')[:1000]}\n"
            f"  Response status: {e.get('status')}\n"
            f"  Response body: {(e.get('responseBody') or '')[:1000]}"
        )
    steps_block = "\n\n".join(steps_text)

    links_lines = [
        f"  - Step {l.get('from_step')}'s response field '{l.get('from_path')}' "
        f"(value like '{l.get('value_preview')}') flows into Step {l.get('to_step')}'s "
        f"{l.get('location')} ({l.get('location_detail')})"
        for l in links
    ]
    links_block = "\n".join(links_lines) if links_lines else \
        "  (none confirmed — infer reasonable chaining from the captured calls above)"

    return f"""You are an expert API test automation engineer using Python + requests.

TEST SCENARIO: {scenario or 'Validate this API call sequence end-to-end'}

CAPTURED API CALL SEQUENCE (chronological):
{steps_block}

DETECTED VALUE CHAINING (confirmed by the user — use these exactly):
{links_block}

FRAMEWORK RULES — follow these exactly, no deviations:

{framework_context}

Generate this chain of calls against the real API, extracting values from each response and feeding them
into the next request precisely as described above, then validate the end state proves the functionality
actually works — not just status codes, but that the returned data reflects the expected mutation/behavior.

Output EXACTLY one file, using the FILE OUTPUT, STEP STRUCTURE, and REPORT OUTPUT FORMAT rules above:
# FILE: tests/APITests/<Portal>/test_<scenario>.py — a single, self-contained plain script.
No pytest, no classes, no project imports — only requests/os/stdlib. It must be directly runnable with
`python tests/APITests/<Portal>/test_<scenario>.py` and produce a non-zero exit code if any check fails.
"""


@app.route("/api/api-chain/detect", methods=["POST"])
def api_chain_detect():
    data    = request.json or {}
    entries = data.get("entries", [])
    try:
        return jsonify(detect_api_chains(entries))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/api-chain/generate", methods=["POST"])
def api_chain_generate():
    data       = request.json or {}
    provider   = data.get("provider", "anthropic")
    api_key    = data.get("api_key", "").strip()
    model      = data.get("model", "")
    ollama_url = data.get("ollama_url", "")
    scenario   = data.get("scenario", "")
    entries    = data.get("entries", [])
    links      = data.get("links", [])
    mode       = data.get("mode", "pytest")  # "pytest" (client + test class) or "standalone" (one plain script)

    if provider == "anthropic" and not api_key:
        return jsonify({"error": "API key required"}), 400
    if not entries:
        return jsonify({"error": "No API calls selected"}), 400

    if mode == "standalone":
        framework_context = build_standalone_framework_context()
        prompt = _format_standalone_script_prompt(scenario, entries, links, framework_context)
    else:
        framework_context = build_api_framework_context()
        prompt = _format_api_chain_prompt(scenario, entries, links, framework_context)

    result, status = call_llm(provider, prompt, data.get("max_tokens", 16000), api_key, model, ollama_url)
    return jsonify(result), status


@app.route("/api/testloke/save-script", methods=["POST"])
def save_testloke_script():
    """Saves generated file(s) straight into TESTLOKE_PROJECT_ROOT (instead of
    this tool's own generated_scripts/ - see /api/scripts/save) so a generated
    standalone script lands exactly where the Script Runner picker above
    already looks (tests/APITests/<Portal>/test_*.py), with no manual copy
    step in between."""
    if not TESTLOKE_PROJECT_ROOT or not os.path.isdir(TESTLOKE_PROJECT_ROOT):
        return jsonify({"error": f"TESTLOKE_PROJECT_ROOT not found: {TESTLOKE_PROJECT_ROOT}"}), 400

    scripts = (request.json or {}).get("scripts", [])
    if not scripts:
        return jsonify({"error": "No scripts to save"}), 400

    root = os.path.realpath(TESTLOKE_PROJECT_ROOT)
    saved = []
    for s in scripts:
        filename = (s.get("filename") or "").strip()
        full_path = os.path.realpath(os.path.join(root, filename))
        if not filename or os.path.commonpath([full_path, root]) != root:
            return jsonify({"error": f"Invalid file path: {filename}"}), 400
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(s.get("content", ""))
        saved.append(os.path.relpath(full_path, root).replace(os.sep, "/"))

    return jsonify({"saved": saved, "project_root": TESTLOKE_PROJECT_ROOT})


if __name__ == "__main__":
    print("=" * 60)
    print("  API Test Generator")
    print("=" * 60)
    print(f"  Playwright  : {PLAYWRIGHT_AVAILABLE}")
    print(f"  Target URL  : {DEFAULT_TARGET_URL}")
    print("=" * 60)
    print("\n  1. Launch browser, browse the target app to generate traffic")
    print("  2. Check the boxes on the API calls that make up your scenario")
    print("  3. Analyze Chain to detect value-passing between calls")
    print("  4. Generate API Chain Test\n")
    print("  Open http://localhost:5002 in your browser\n")

    socketio.run(app, host="0.0.0.0", port=SERVER_PORT, debug=False, allow_unsafe_werkzeug=True)
