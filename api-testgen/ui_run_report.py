"""Self-contained HTML report for a replay of recorded UI steps.

Layout follows the mobile-agent run reports: Goal, Expected result, Verdict,
Issues found, numbered Steps (what it did, what was seen, a screenshot) and a
Final screen. One HTML file with the screenshots embedded, so it can be opened
from disk or published as a single object.

Secret values (password-like fills) are masked everywhere, including inside
free text such as an assertion that happened to contain one.
"""
import html
import re
from datetime import datetime

MASK = "••••••"
_VERBS = {
    "goto": "go to", "back": "go back", "click": "click", "fill": "fill", "clear": "clear", "select": "select",
    "check": "check", "uncheck": "uncheck", "press": "press", "press_page": "press",
    "hover": "hover", "scroll_into_view": "scroll to", "wait_for": "wait for", "wait": "wait",
    "scroll_page": "scroll page", "assert_text": "assert text", "assert_value": "assert value",
    "assert_visible": "assert visible", "assert_attr": "assert attribute", "assert_js": "assert script", "assert_url": "assert URL", "capture": "capture", "compare": "compare", "assert_datetime": "assert date/time",
}
_ASSERTS = ("assert_datetime", "assert_text", "assert_value", "assert_visible", "assert_attr", "assert_js", "assert_url", "compare")
_SECRET_IN_TEXT = re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|token|api[_-]?key|otp|cvv)\b(\s*[=:]\s*)(\S+)")


_DT_TEXT = {"time_now": "time is now", "datetime_now": "date and time are now", "date_today": "date is today"}


def _target(s):
    loc = s.get("locator") or {}
    if loc.get("anchor"):
        return f'label "{loc["anchor"]}" → the control next to it'
    if loc.get("kind") == "pattern":
        where = " in " + loc["scope"] if loc.get("scope") else ""
        which = "last" if loc.get("nth") == -1 else "first" if loc.get("nth") == 0 else "any"
        return f'text like {loc.get("value")}{where} ({which} match)'
    return loc.get("py") or loc.get("value") or loc.get("name") or ""


def step_label(s):
    t = s.get("type")
    verb = _VERBS.get(t, t or "step")
    if t == "goto":
        return f'{verb} {s.get("value") or s.get("url") or ""}'
    if t == "wait":
        return f'{verb} {s.get("value")} ms'
    if t == "scroll_page":
        return f'{verb} to the {s.get("value")}'
    if t == "press_page":
        return f'{verb} {s.get("value")}'
    if t == "assert_js":
        return f'{verb} {s.get("expr") or ""}'.strip()
    target = _target(s)
    pos = s.get("position")
    at = f" at ({pos['x']}, {pos['y']})" if pos else ""
    return f"{verb} {target}{at}".strip()


def step_detail(s):
    """The value/expectation a step carries, with secrets masked."""
    t = s.get("type")
    if t == "fill" and s.get("secret"):
        return f'value: {MASK}'
    if t in ("fill", "select", "press"):
        return f'value: "{s.get("value", "")}"'
    if t == "assert_text":
        return f'expected text to contain: "{s.get("value", "")}"'
    if t == "assert_value":
        return f'expected value: "{s.get("value", "")}"'
    if t == "assert_attr":
        return f'expected {s.get("attr") or "attribute"} = "{s.get("value", "")}"'
    if t == "capture":
        return f'{s.get("source") or "text"} → {s.get("var", "")}'
    if t == "compare":
        return f'expected: {s.get("left", "")} {(s.get("op") or "equals").replace("_", " ")} {s.get("right", "")}'
    if t == "assert_datetime":
        return f'expected: {_DT_TEXT.get(s.get("mode"), _DT_TEXT["datetime_now"])} (±{s.get("tol", 2)} min)'
    if t == "assert_url":
        return f'expected URL to contain: "{s.get("value", "")}"'
    if t == "assert_visible":
        return "expected: visible"
    if t == "assert_js":
        return f'expected result: {s.get("value", "")}'
    if t == "wait_for":
        return f'until: {s.get("value") or "visible"}'
    return ""


def _fmt_ms(ms):
    return f"{ms / 1000:.1f}s" if ms >= 100 else f"{ms}ms"


_CSS = """
:root{--bg:#e9ecf0;--card:#fff;--ink:#25122f;--mute:#6a5f78;--line:#d5d9e0;--ok:#0f8a65;--okbg:#dff5ec;--bad:#c42d42;--badbg:#fbe4e7;--warn:#a86a00;--warnbg:#fdf1d8;--accent:#e8920a;--accent-ink:#8a5000;--head:#1d0f2c;--head-ink:#ece8f1;--soft:rgba(37,18,47,.045)}
@media (prefers-color-scheme:dark){:root{--bg:#12081c;--card:#1c0f2b;--ink:#ebe8f0;--mute:#a396b5;--line:#35224a;--ok:#6fd8b4;--okbg:#12302a;--bad:#ff7a8a;--badbg:#341522;--warn:#ffd27a;--warnbg:#33260f;--accent:#f5a524;--accent-ink:#ffc766;--head:#0d0614;--head-ink:#ece8f1;--soft:rgba(255,255,255,.05)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.55 Inter,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.top{background:var(--head);color:var(--head-ink);border-bottom:3px solid var(--accent)}
.top .in{max-width:1000px;margin:0 auto;padding:22px 20px 18px}
h1{font-size:22px;margin:0 0 8px;letter-spacing:-.01em;font-weight:650}
.status{display:inline-block;font-weight:700;padding:3px 13px;border-radius:99px;font-size:12.5px;letter-spacing:.04em;margin-right:10px}
.status.pass{background:var(--okbg);color:var(--ok)}.status.fail{background:var(--badbg);color:var(--bad)}
.meta{display:inline;color:#b6a9c6;font-size:13px}
.wrap{max-width:1000px;margin:0 auto;padding:6px 20px 60px}
h2{font-size:11.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--mute);margin:26px 0 8px;font-weight:650}
h2.row{display:flex;align-items:center;gap:10px}
.btns{margin-left:auto;display:flex;gap:6px}.btns button{font:600 11.5px Inter,sans-serif;text-transform:none;letter-spacing:0;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:7px;padding:4px 10px;cursor:pointer}
.btns button:hover{border-color:var(--accent)}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px}
.box ul{margin:0;padding-left:20px}.box li{margin:3px 0}
details.tc{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--ok);border-radius:12px;margin:10px 0;overflow:hidden}
details.tc.bad{border-left-color:var(--bad)}details.tc.skip{border-left-color:var(--line)}
details.tc>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;padding:13px 16px;user-select:none}
details.tc>summary::-webkit-details-marker{display:none}
details.tc>summary:hover{background:var(--soft)}
details.tc>summary .car{color:var(--accent-ink);transition:transform .15s;display:inline-block;width:14px}
details.tc[open]>summary .car{transform:rotate(90deg)}
details.tc>summary .nm{font-weight:650;flex:1;min-width:0;word-break:break-word}
.pill{font-size:11.5px;font-weight:700;border-radius:99px;padding:2px 10px}
.pill.pass{background:var(--okbg);color:var(--ok)}.pill.fail{background:var(--badbg);color:var(--bad)}.pill.skip{background:var(--soft);color:var(--mute)}
.cm{color:var(--mute);font-size:12.5px;white-space:nowrap}
.tcb{padding:2px 14px 12px;border-top:1px solid var(--line);background:var(--soft)}
.case{margin:20px 0 8px;font-weight:700;color:var(--accent-ink)}
.step{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--ok);border-radius:10px;padding:12px 16px;margin:8px 0;display:flex;gap:14px;justify-content:space-between}
.step.bad{border-left-color:var(--bad)}.step.skip{border-left-color:var(--line);opacity:.6}
.step .t{font-weight:600;word-break:break-word}.step .n{color:var(--mute);margin-right:6px}
.chip{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--mute);margin-left:6px;font-weight:500}
.sub{color:var(--mute);font-size:13px;margin-top:2px;word-break:break-word}
.saw b,.err b{font-weight:700}.err{color:var(--bad);margin-top:6px;white-space:pre-wrap;word-break:break-word;font-size:13px}
.shot{flex:0 0 auto}.shot img{width:190px;max-height:130px;object-fit:cover;object-position:top;border:1px solid var(--line);border-radius:8px;cursor:zoom-in;display:block}
.final{display:flex;gap:16px;flex-wrap:wrap}.final figure{margin:0}.final img{max-width:460px;width:100%;border:1px solid var(--line);border-radius:10px;cursor:zoom-in}
.final figcaption{color:var(--mute);font-size:12px;margin-top:4px}
#lb{position:fixed;inset:0;background:rgba(15,6,24,.88);display:none;align-items:center;justify-content:center;z-index:9;cursor:zoom-out}#lb img{max-width:96vw;max-height:94vh;border-radius:6px}
@media (max-width:640px){.step{flex-direction:column}.shot img{width:100%;max-height:none}details.tc>summary{flex-wrap:wrap}}
@media print{details.tc{break-inside:avoid}}
"""
_JS = ("document.addEventListener('click',function(e){var i=e.target;var lb=document.getElementById('lb');"
       "if(i.tagName==='IMG'&&i.dataset.zoom){lb.firstChild.src=i.src;lb.style.display='flex'}else if(lb.contains(i)||i===lb){lb.style.display='none'}"
       "var b=i.closest&&i.closest('[data-all]');if(b){var o=b.dataset.all==='open';document.querySelectorAll('details.tc').forEach(function(d){d.open=o})}});")


def build_report(name, steps, rows, finals=None, started=None, total_ms=0, goal="", expected=""):
    started = started or datetime.now()
    by_id = {r["id"]: r for r in rows}
    secrets = {str(s.get("value")) for s in steps if s.get("secret") and s.get("value")}
    multi = any(s.get("portal") == "B" for s in steps)
    real = [s for s in steps if s.get("type") not in ("case", "note")]
    asserts = [s for s in real if s.get("type") in _ASSERTS]
    failed = next((r for r in rows if not r.get("ok")), None)
    failed_step = next((s for s in steps if failed and s["id"] == failed["id"]), None)
    ran = [s for s in real if s["id"] in by_id]
    passed = failed is None and len(ran) == len(real)
    cases = sum(1 for s in steps if s.get("type") == "case")
    e = html.escape

    title = f"{(name or 'UI test').strip()} · {started.strftime('%Y-%m-%d %H:%M:%S')}"
    status = "PASS" if passed else "FAIL"
    meta = f"{len(real)} steps" + (f" · {cases} test case{'s' if cases != 1 else ''}" if cases else "") + f" · {_fmt_ms(total_ms)}" + (" · 2 portals" if multi else "")

    # Expected result: what the run actually asserts (and an honest note when it asserts nothing)
    exp = ""
    if expected:
        exp += f'<div style="white-space:pre-wrap">{e(expected)}</div>'
    if asserts:
        exp += ("<div class=\"sub\">Checked automatically:</div>" if expected else "") + \
               "<ul>" + "".join(f"<li>{e(step_label(s))} — {e(step_detail(s))}</li>" for s in asserts) + "</ul>"
    elif expected:
        exp += '<div class="sub"><i>Not checked automatically — no assertions were recorded, so compare it with the step screenshots below.</i></div>'
    else:
        exp = "<i>No expected result was entered and no assertions were recorded, so this run only shows the steps executing without error.</i>"

    # Verdict
    if passed:
        verdict = f"All {len(real)} steps ran without error" + (f", including {len(asserts)} assertion(s)." if asserts else
                                                                  (". The expected result was not verified automatically." if expected else "."))
    elif failed_step:
        n = real.index(failed_step) + 1 if failed_step in real else "?"
        skipped = len(real) - len(ran)
        verdict = (f"Stopped at step {n} ({step_label(failed_step)}): {failed.get('error') or 'failed'}. "
                   f"{skipped} later step(s) were not run." if skipped else
                   f"Failed at step {n} ({step_label(failed_step)}): {failed.get('error') or 'failed'}.")
    else:
        verdict = "The run did not finish."

    # Issues
    issues = []
    if failed and failed_step:
        issues.append(f"Step failed — {step_label(failed_step)}: {failed.get('error') or ''}")
    if not asserts:
        issues.append("No assertions recorded — add checks (Pick → Assert text/value/visible) so a pass means the app is correct, not just that the clicks worked."
                      + (" Your expected result was not verified automatically." if expected else ""))
    for r in rows:
        if r.get("ok") and r.get("ms", 0) > 10000:
            s = next((x for x in steps if x["id"] == r["id"]), {})
            issues.append(f"Slow step ({_fmt_ms(r['ms'])}): {step_label(s)}")
    issues_html = "<ul>" + "".join(f"<li>{e(i)}</li>" for i in issues) + "</ul>" if issues else "None."

    # Steps, grouped by test case: each case is a collapsible block (failed ones start open)
    n = 0

    def step_html(s, n):
        r = by_id.get(s["id"])
        cls = "skip" if r is None else ("" if r.get("ok") else "bad")
        icon = "–" if r is None else ("✓" if r.get("ok") else "✗")
        chip = f'<span class="chip">Portal {e(s.get("portal", "A"))}</span>' if multi else ""
        detail = step_detail(s)
        lines = [f'<div class="t"><span class="n">{n}</span>{icon} {e(step_label(s))}{chip}'
                 + (f' <span class="chip">{_fmt_ms(r["ms"])}</span>' if r else "") + "</div>"]
        if detail:
            lines.append(f'<div class="sub">{e(detail)}</div>')
        if r is None:
            lines.append('<div class="sub">not run</div>')
        else:
            saw = []
            if r.get("saw") is not None:
                saw.append(f'actual: "{r["saw"]}"')
            if r.get("title") or r.get("url"):
                saw.append(f'{r.get("title") or ""} — {r.get("url") or ""}'.strip(" —"))
            if saw:
                lines.append('<div class="sub saw"><b>Saw:</b> ' + e(" · ".join(saw)) + "</div>")
            if r.get("error"):
                lines.append(f'<div class="err"><b>Error:</b> {e(r["error"])}</div>')
        shot = (f'<div class="shot"><img data-zoom="1" alt="step {n}" src="data:image/jpeg;base64,{r["shot"]}"></div>'
                if r and r.get("shot") else "")
        return f'<div class="step {cls}"><div>{"".join(lines)}</div>{shot}</div>'

    groups = [{"name": None, "steps": []}]
    for s in steps:
        if s.get("type") == "case":
            groups.append({"name": s.get("value") or "Test case", "steps": []})
        elif s.get("type") != "note":
            n += 1
            groups[-1]["steps"].append((s, n))
    out = []
    for g in groups:
        if not g["steps"] and g["name"] is None:
            continue
        html_steps = "".join(step_html(s, k) for s, k in g["steps"])
        if g["name"] is None:                                   # steps before the first case header: shown plainly
            out.append(html_steps)
            continue
        rs = [by_id.get(s["id"]) for s, _ in g["steps"]]
        bad = any(r is not None and not r.get("ok") for r in rs)
        notrun = any(r is None for r in rs)
        state = "fail" if bad else ("skip" if notrun or not rs else "pass")
        label = {"fail": "FAIL", "skip": "NOT RUN" if notrun else "EMPTY", "pass": "PASS"}[state]
        ms = sum((r or {}).get("ms", 0) for r in rs)
        out.append(f'<details class="tc {"bad" if bad else ("skip" if state == "skip" else "")}"{" open" if bad else ""}>'
                   f'<summary><span class="car">▸</span><span class="nm">📋 {e(g["name"])}</span>'
                   f'<span class="cm">{len(g["steps"])} step{"s" if len(g["steps"]) != 1 else ""} · {_fmt_ms(ms)}</span>'
                   f'<span class="pill {state}">{label}</span></summary><div class="tcb">{html_steps}</div></details>')
    has_cases = any(g["name"] is not None for g in groups)
    steps_head = ('<h2 class="row">Test cases<span class="btns"><button data-all="open">Expand all</button>'
                  '<button data-all="close">Collapse all</button></span></h2>') if has_cases else "<h2>Steps</h2>"

    fin = "".join(f'<figure><img data-zoom="1" alt="final screen" src="data:image/jpeg;base64,{b}">'
                  f'<figcaption>{"Portal " + e(p) if multi else "Final screen"}</figcaption></figure>'
                  for p, b in sorted((finals or {}).items()))

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{_CSS}</style></head><body>
<div class="top"><div class="in"><h1>{e(title)}</h1><span class="status {'pass' if passed else 'fail'}">{status}</span><span class="meta">{e(meta)}</span></div></div>
<div class="wrap">
<h2>Goal</h2><div class="box" style="white-space:pre-wrap">{e(goal or (name or 'UI test').strip())}</div>
<h2>Expected result</h2><div class="box">{exp}</div>
<h2>Verdict</h2><div class="box">{e(verdict)}</div>
<h2>Issues found</h2><div class="box">{issues_html}</div>
{steps_head}{''.join(out)}
{'<h2>Final screen</h2><div class="final">' + fin + '</div>' if fin else ''}
</div><div id="lb"><img alt=""></div><script>{_JS}</script></body></html>"""

    # Belt and braces: no secret value or `password=...` text survives in the page's text. Done per
    # segment, skipping the base64 screenshots - a short secret could otherwise match inside image data.
    parts = re.split(r"(data:image/jpeg;base64,[A-Za-z0-9+/=]+)", page)
    for i in range(0, len(parts), 2):
        for sec in secrets:
            parts[i] = parts[i].replace(sec, MASK).replace(html.escape(sec), MASK)
        parts[i] = _SECRET_IN_TEXT.sub(lambda m: m.group(1) + m.group(2) + MASK, parts[i])
    return "".join(parts)
