// In-page UI action recorder. Injected into every page by the Playwright
// worker (context.add_init_script) and reports each user action through the
// exposed binding window.__tgRecordStep(step). The Python side drops steps
// unless a recording is active, so this script is always inert-by-default.
//
// Each step is {type, locator, ...}. A locator is described, not rendered:
//   {kind: 'testid'|'role'|'label'|'placeholder'|'text'|'css', value, role?, name?, nth?}
// and server.py turns it into the matching Playwright call. Strategy order
// mirrors Playwright's own codegen: test id > role+name > label >
// placeholder > text > stable id/name > css path.
//
// While recording, a floating toolbar (shadow DOM, so it can't clash with the
// app's CSS) offers a Pick mode: hovering highlights, a click only SELECTS the
// element (the page never sees it), and a menu lets the user choose the
// operation - click, hover, wait, scroll, assert text/value/href...
(function () {
  if (window.top !== window) return;               // top frame only (iframes not recorded yet)
  if (window.__tgUiRecorderInjected) return;
  window.__tgUiRecorderInjected = true;

  var committed = new WeakMap();                   // element -> last value we reported
  var host = null, root = null, ui = {};           // toolbar (created lazily)
  var recOn = false, pickOn = false, selEl = null, anchorOn = false;   // anchorOn: prefer label-anchored locators

  function isOurs(e) {
    return !!(host && e.composedPath && e.composedPath().indexOf(host) >= 0);
  }

  function send(step) {
    step.url = location.href;
    step.ts = Date.now();
    try {
      if (window.__tgRecordStep) window.__tgRecordStep(step);
    } catch (e) {}
  }

  function norm(s) { return (s || '').replace(/\s+/g, ' ').trim(); }

  // ── accessible name / role (close enough to what Playwright computes) ──────
  function accessibleName(el) {
    var aria = el.getAttribute('aria-label');
    if (aria) return norm(aria);
    var lb = el.getAttribute('aria-labelledby');
    if (lb) {
      var t = norm(lb.split(/\s+/).map(function (id) {
        var n = document.getElementById(id); return n ? n.textContent : '';
      }).join(' '));
      if (t) return t;
    }
    var tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      var lab = labelText(el);
      if (lab) return lab;
      var ty = (el.getAttribute('type') || '').toLowerCase();
      if ((ty === 'submit' || ty === 'button' || ty === 'reset') && el.value) return norm(el.value);
      return norm(el.getAttribute('placeholder') || el.getAttribute('title'));
    }
    if (tag === 'img') return norm(el.getAttribute('alt'));
    return norm(el.textContent) || norm(el.getAttribute('title'));
  }

  function labelText(el) {
    if (el.labels && el.labels.length) return norm(el.labels[0].textContent);
    var p = el.closest && el.closest('label');
    return p ? norm(p.textContent) : '';
  }

  var IMPLICIT = { button: 'button', a: 'link', select: 'combobox', textarea: 'textbox',
                   h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading',
                   summary: 'button', option: 'option' };
  function roleOf(el) {
    var r = el.getAttribute('role');
    if (r) return r.split(/\s+/)[0];
    var tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'input') {
      var ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'submit' || ty === 'button' || ty === 'reset' || ty === 'image') return 'button';
      if (ty === 'range') return 'slider';
      if (ty === 'hidden') return null;
      return ty === 'search' ? 'searchbox' : 'textbox';
    }
    return IMPLICIT[tag] || null;
  }

  // ── locator building ──────────────────────────────────────────────────────
  function cssEscape(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1'); }
  function countCss(sel) { try { return document.querySelectorAll(sel).length; } catch (e) { return 0; } }

  // Ids that look generated (react-aria, :r1:, long hex/number runs) make
  // brittle selectors, so they're skipped in favour of the next strategy.
  function stableId(id) {
    return id && !/^\d|:|\d{4,}|[0-9a-f]{8,}/i.test(id);
  }

  function withIndex(loc, list, el) {
    var i = list.indexOf(el);
    if (list.length > 1 && i >= 0) loc.nth = i;
    return loc;
  }

  function cssPath(el) {
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      var tag = el.tagName.toLowerCase();
      if (stableId(el.id) && countCss('#' + cssEscape(el.id)) === 1) {
        parts.unshift('#' + cssEscape(el.id)); break;
      }
      var parent = el.parentElement, seg = tag;
      if (parent) {
        var same = Array.prototype.filter.call(parent.children, function (c) { return c.tagName === el.tagName; });
        if (same.length > 1) seg += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      }
      parts.unshift(seg);
      el = parent;
    }
    return parts.join(' > ');
  }

  // Preferred: Playwright's own selector generator (the one behind `codegen`),
  // exposed to the page as window.playwright when the driver runs with
  // PWDEBUG=console. It validates uniqueness against the real engine, so it
  // copes with nested/duplicate elements far better than the heuristics below.
  function playwrightLocator(el) {
    try {
      var pw = window.playwright;
      if (pw && pw.selector && pw.generateLocator) {
        var sel = pw.selector(el), py = pw.generateLocator(el, 'python');
        if (sel && py) return { kind: 'pw', selector: sel, py: py };
      }
    } catch (e) {}
    return null;
  }

  // ── value-independent locators ────────────────────────────────────────────
  // A control whose visible text is the data itself (a date picker button showing
  // "6 October 2000", a dropdown showing the chosen option) can't be found by that
  // text - it changes. Anchor on the stable label next to it instead:
  //   //label[normalize-space(.)='Date of Birth']/following-sibling::div[1]//button
  // Verified with the browser's own XPath engine, which Playwright's `xpath=` uses too.
  function xq(t) {                                       // XPath string literal
    if (t.indexOf("'") < 0) return "'" + t + "'";
    if (t.indexOf('"') < 0) return '"' + t + '"';
    return "concat('" + t.split("'").join("',\"'\",'") + "')";
  }
  function xpathAll(x) {
    var r = document.evaluate(x, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null), a = [];
    for (var i = 0; i < r.snapshotLength; i++) a.push(r.snapshotItem(i));
    return a;
  }
  function anchorText(n, ownText) {                      // a sibling that reads like a field label
    if (n.tagName === 'LABEL') return norm(n.textContent);
    if (n.children.length > 1) return '';
    var t = norm(n.textContent);
    return (t && t.length <= 40 && t !== ownText && !/^[\d\s.,:/-]+$/.test(t)) ? t : '';
  }
  var INTERACTIVE = 'button,a[href],input,select,textarea,summary,[role=button],[role=link],[role=combobox],[role=checkbox],' +
                    '[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=textbox]';
  function labelAnchoredLocator(el) {
    // Land on the control itself (the button), not the span/icon inside it that the pointer happened to hit.
    var hit = el.closest(INTERACTIVE);
    if (hit && hit !== el) el = hit;
    var own = norm(el.textContent), node = el;
    for (var depth = 0; depth < 6 && node && node !== document.body && node !== document.documentElement; depth++, node = node.parentElement) {
      for (var sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) {
        var label = anchorText(sib, own);
        if (!label) continue;
        var follow = [], k = 0, n = sib.nextElementSibling;
        while (n) { if (n.tagName === node.tagName) { k++; if (n === node) break; } n = n.nextElementSibling; }
        if (!n) break;
        var x = '//' + sib.tagName.toLowerCase() + '[normalize-space(.)=' + xq(label) + ']' +
                '/following-sibling::' + node.tagName.toLowerCase() + '[' + k + ']' +
                (node === el ? '' : '//' + el.tagName.toLowerCase());
        var hits = xpathAll(x), idx = hits.indexOf(el);   // `el` is the control here, not the raw pick
        if (idx < 0) break;                              // this label doesn't lead to el; try a higher level
        if (hits.length > 1) x = '(' + x + ')[' + (idx + 1) + ']';
        return { kind: 'css', value: 'xpath=' + x, anchor: label };
      }
    }
    return null;
  }

  function buildLocator(el) {
    var primary = playwrightLocator(el) || buildLocatorFallback(el), anchored = null;
    try { anchored = labelAnchoredLocator(el); } catch (e) {}
    if (anchored && anchorOn) { anchored.alts = [primary]; return anchored; }   // ⚓ mode: value-independent first
    if (anchored) primary.alts = [anchored];                                    // otherwise keep it as an alternative
    return primary;
  }

  function buildLocatorFallback(el) {
    var attr, v, list;
    var testAttrs = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy'];
    for (var i = 0; i < testAttrs.length; i++) {
      v = el.getAttribute(testAttrs[i]);
      if (v) {
        var sel = '[' + testAttrs[i] + '=' + JSON.stringify(v) + ']';
        if (countCss(sel) === 1) {
          return testAttrs[i] === 'data-testid' ? { kind: 'testid', value: v } : { kind: 'css', value: sel };
        }
      }
    }

    var tag = el.tagName.toLowerCase();
    var role = roleOf(el), name = accessibleName(el);

    if (role && name && name.length <= 80) {
      list = Array.prototype.filter.call(document.querySelectorAll('*'), function (n) {
        return roleOf(n) === role && accessibleName(n) === name;
      });
      return withIndex({ kind: 'role', role: role, name: name }, list, el);
    }

    if ((tag === 'input' || tag === 'textarea' || tag === 'select') && labelText(el)) {
      var lt = labelText(el);
      list = Array.prototype.filter.call(document.querySelectorAll('input,textarea,select'), function (n) {
        return labelText(n) === lt;
      });
      return withIndex({ kind: 'label', value: lt }, list, el);
    }

    v = el.getAttribute('placeholder');
    if (v) {
      list = Array.prototype.slice.call(document.querySelectorAll('[placeholder]')).filter(function (n) {
        return n.getAttribute('placeholder') === v;
      });
      return withIndex({ kind: 'placeholder', value: v }, list, el);
    }

    var own = norm(el.textContent);
    if (own && own.length <= 60) {
      // Playwright's text locator matches only the innermost element holding the
      // text, never its wrappers - so index among those, or nth() will overshoot.
      var leaf = el, kid;
      while ((kid = Array.prototype.find.call(leaf.children, function (c) { return norm(c.textContent) === own; }))) leaf = kid;
      list = Array.prototype.filter.call(document.querySelectorAll('body *'), function (n) {
        return norm(n.textContent) === own &&
               !Array.prototype.some.call(n.children, function (c) { return norm(c.textContent) === own; });
      });
      return withIndex({ kind: 'text', value: own }, list, leaf);
    }

    if (stableId(el.id) && countCss('#' + cssEscape(el.id)) === 1) return { kind: 'css', value: '#' + cssEscape(el.id) };
    attr = el.getAttribute('name');
    if (attr) {
      var nsel = tag + '[name=' + JSON.stringify(attr) + ']';
      if (countCss(nsel) === 1) return { kind: 'css', value: nsel };
    }
    return { kind: 'css', value: cssPath(el) };
  }

  // ── event handling ────────────────────────────────────────────────────────
  var CLICKABLE = 'a[href],button,summary,label,[role=button],[role=link],[role=tab],[role=menuitem],' +
                  '[role=option],[role=checkbox],[role=radio],[role=switch],[onclick],' +
                  'input[type=button],input[type=submit],input[type=reset],input[type=image]';

  function isTextEntry(el) {
    var tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return true;
    if (tag !== 'input') return el.isContentEditable === true;
    var ty = (el.getAttribute('type') || 'text').toLowerCase();
    return ['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'file', 'range', 'color', 'hidden'].indexOf(ty) < 0;
  }

  function commitFill(el) {
    if (!isTextEntry(el) || el.isContentEditable) return;
    var val = el.value;
    if (committed.get(el) === val) return;
    committed.set(el, val);
    if (val === '') { send({ type: 'clear', locator: buildLocator(el) }); return; }
    var isSecret = (el.getAttribute('type') || '').toLowerCase() === 'password' ||
                   /pass(word)?|secret|token|otp|cvv|cvc/i.test((el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('autocomplete') || ''));
    send({ type: 'fill', locator: buildLocator(el), value: val, secret: !!isSecret });
  }

  // Clicking away or pressing a key can happen before the browser fires
  // `change`, so flush whatever the focused field holds before any later action.
  function flushActive() {
    var a = document.activeElement;
    if (a && a !== document.body) commitFill(a);
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof Element) || !e.isTrusted || isOurs(e)) return;

    flushActive();
    var target = t.closest(CLICKABLE) || t;
    var tg = target.tagName.toLowerCase();
    // Fields are covered by their own fill/check/select steps - a click on
    // them is just focus, and Playwright's fill() focuses on its own.
    if (isTextEntry(target) || tg === 'select' || tg === 'option') return;
    // A label click just forwards to its control, which reports its own change/check step.
    if (tg === 'label' && target.control) return;
    if (tg === 'input' && (target.type === 'checkbox' || target.type === 'radio')) return;
    if (!e.isTrusted) return;                      // script-fired clicks aren't user actions
    send({ type: 'click', locator: buildLocator(target) });
  }, true);

  document.addEventListener('focusin', function (e) {
    var el = e.target;
    if (el instanceof Element && !isOurs(e) && isTextEntry(el) && !el.isContentEditable && !committed.has(el)) committed.set(el, el.value);
  }, true);

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!(el instanceof Element) || isOurs(e)) return;
    var tag = el.tagName.toLowerCase();
    if (tag === 'select') {
      var opt = el.options[el.selectedIndex];
      send({ type: 'select', locator: buildLocator(el), value: opt ? norm(opt.text) : el.value });
    } else if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) {
      send({ type: el.checked ? 'check' : 'uncheck', locator: buildLocator(el) });
    } else if (tag === 'input' && el.type === 'file') {
      send({ type: 'note', value: 'File upload on "' + (accessibleName(el) || 'file input') + '" is not recorded - add set_input_files() by hand.' });
    } else {
      commitFill(el);
    }
  }, true);

  document.addEventListener('keydown', function (e) {
    if (pickOn && e.key === 'Escape') { exitPick(); return; }   // before the isOurs guard: focus is usually on the toolbar
    var el = e.target;
    if (!(el instanceof Element) || e.isComposing || isOurs(e)) return;
    var tag = el.tagName.toLowerCase();
    if (e.key === 'Enter' && tag === 'input' && isTextEntry(el)) {
      commitFill(el);                              // value first, then the Enter that submits it
      send({ type: 'press', locator: buildLocator(el), value: 'Enter' });
    } else if (e.key === 'Escape') {
      flushActive();
      send({ type: 'press_page', value: 'Escape' });
    }
  }, true);

  // ── toolbar + pick mode ───────────────────────────────────────────────────
  var CSS_TEXT =
    ':host{all:initial}*{box-sizing:border-box;font:12px/1.3 -apple-system,Segoe UI,sans-serif}' +
    '#bar{position:fixed;top:10px;right:10px;display:flex;align-items:center;gap:6px;background:#111827;color:#f9fafb;' +
    'padding:6px 8px;border-radius:9px;box-shadow:0 4px 18px rgba(0,0,0,.35);pointer-events:auto}' +
    '.rec{color:#f87171;font-weight:600}#msg{color:#86efac;max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    'button{background:#374151;color:#f9fafb;border:0;border-radius:6px;padding:5px 9px;cursor:pointer}' +
    'button:hover{background:#4b5563}button.on{background:#7c6aff}' +
    'input{width:62px;background:#1f2937;color:#f9fafb;border:1px solid #4b5563;border-radius:6px;padding:4px 6px}' +
    '#box{position:fixed;display:none;pointer-events:none;border:2px solid #7c6aff;background:rgba(124,106,255,.14);border-radius:3px}' +
    '#menu{position:fixed;display:none;flex-direction:column;gap:3px;background:#111827;padding:6px;border-radius:9px;' +
    'box-shadow:0 6px 22px rgba(0,0,0,.4);max-width:330px;pointer-events:auto}' +
    '#menu .hd{color:#9ca3af;font-size:11px;padding:2px 4px 5px;word-break:break-all}' +
    '#menu button{text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    '#menu .row{display:flex;gap:4px}#menu .row button{flex:1;text-align:center}';

  function ensureUi() {
    if (host || !document.documentElement) return;
    host = document.createElement('div');
    host.style.cssText = 'all:initial;position:fixed;top:0;left:0;width:0;height:0;overflow:visible;pointer-events:none;z-index:2147483647';
    root = host.attachShadow({ mode: 'open' });
    root.innerHTML = '<style>' + CSS_TEXT + '</style><div id="box"></div><div id="menu"></div>' +
      '<div id="bar"><span class="rec">&#9679; REC</span><button id="pick" title="Select an element without clicking it (Esc to leave)">&#127919; Pick</button>' +
      '<button id="anchor" title="Prefer locators anchored to the nearest label (ignores a field\'s current value, e.g. a date or selected option)">&#9875; Anchor</button>' +
      '<input id="ms" type="number" value="1000" min="0" step="250" title="milliseconds"><button id="wait">&#9201; Wait</button>' +
      '<button id="top" title="Scroll to top">&#10514;</button><button id="bot" title="Scroll to bottom">&#10515;</button><span id="msg"></span></div>';
    ['box', 'menu', 'pick', 'anchor', 'ms', 'wait', 'top', 'bot', 'msg'].forEach(function (id) { ui[id] = root.getElementById(id); });
    ui.pick.onclick = function () { pickOn ? exitPick() : enterPick(); };
    ui.anchor.onclick = function () {
      anchorOn = !anchorOn; ui.anchor.classList.toggle('on', anchorOn);
      try { window.__tgRecordStep({ type: '__anchor', value: anchorOn }); } catch (e) {}
      say(anchorOn ? 'Anchor on: locators use the nearest label' : 'Anchor off');
    };
    ui.wait.onclick = function () { send({ type: 'wait', value: String(parseInt(ui.ms.value, 10) || 0) }); say('Added: wait ' + ui.ms.value + 'ms'); };
    ui.top.onclick = function () { window.scrollTo(0, 0); send({ type: 'scroll_page', value: 'top' }); say('Added: scroll to top'); };
    ui.bot.onclick = function () { window.scrollTo(0, document.documentElement.scrollHeight); send({ type: 'scroll_page', value: 'bottom' }); say('Added: scroll to bottom'); };
    document.documentElement.appendChild(host);
    host.style.display = 'none';
  }

  function say(m) {
    if (!ui.msg) return;
    ui.msg.textContent = m;
    clearTimeout(say.t);
    say.t = setTimeout(function () { ui.msg.textContent = ''; }, 2500);
  }

  function enterPick() { pickOn = true; ui.pick.classList.add('on'); document.documentElement.style.cursor = 'crosshair'; }
  function exitPick() {
    pickOn = false; clearSel();
    if (ui.pick) ui.pick.classList.remove('on');
    document.documentElement.style.cursor = '';
  }
  function clearSel() {
    selEl = null;
    if (ui.box) ui.box.style.display = 'none';
    if (ui.menu) ui.menu.style.display = 'none';
  }

  function drawBox(el, color) {
    var r = el.getBoundingClientRect();
    ui.box.style.cssText = 'display:block;left:' + r.left + 'px;top:' + r.top + 'px;width:' + r.width + 'px;height:' + r.height + 'px;' +
      (color ? 'border-color:' + color + ';background:rgba(34,197,94,.18)' : '');
  }

  function trunc(t, n) { return t.length > n ? t.slice(0, n - 1) + '…' : t; }

  function describe(loc) {
    if (loc.anchor) return '\u2693 label "' + loc.anchor + '"';
    if (loc.kind === 'pw') return loc.py;
    var d = loc.kind === 'role' ? loc.role + ' "' + loc.name + '"' : loc.kind + ' ' + JSON.stringify(loc.value);
    return loc.nth != null ? d + ' #' + (loc.nth + 1) : d;
  }

  function showMenu(el) {
    selEl = el; drawBox(el);
    var tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    var isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    var isToggle = tag === 'input' && (type === 'checkbox' || type === 'radio');
    var text = norm(el.textContent), href = tag === 'a' ? el.getAttribute('href') : null;
    var ops = [['Click', 'click'], ['Hover', 'hover'], ['Scroll into view', 'scroll_into_view'],
               ['Wait for element', 'wait_for'], ['Assert visible', 'assert_visible']];
    if (!isField && text && text.length <= 200) ops.push(['Assert text: "' + trunc(text, 40) + '"', 'assert_text']);
    if (isField && !isToggle && tag !== 'select') ops.splice(1, 0, ['Clear field', 'clear']);
    if (isField && !isToggle && type !== 'password') ops.push(['Assert value: "' + trunc(String(el.value || ''), 40) + '"', 'assert_value']);
    if (href) ops.push(['Assert href: ' + trunc(href, 40), 'assert_attr']);

    var menu = ui.menu;
    menu.innerHTML = '';
    var hd = document.createElement('div'); hd.className = 'hd';
    hd.textContent = '<' + tag + '> ' + describe(buildLocator(el));
    menu.appendChild(hd);
    ops.forEach(function (o) {
      var b = document.createElement('button'); b.textContent = o[0];
      b.onclick = function () { applyOp(o[1], el); };
      menu.appendChild(b);
    });
    var row = document.createElement('div'); row.className = 'row';
    var up = document.createElement('button'); up.textContent = '▲ Parent';
    up.title = 'Select the enclosing element instead';
    up.onclick = function () { if (el.parentElement && el.parentElement !== document.documentElement) showMenu(el.parentElement); };
    var x = document.createElement('button'); x.textContent = '✕ Cancel'; x.onclick = clearSel;
    row.appendChild(up); row.appendChild(x); menu.appendChild(row);

    menu.style.display = 'flex';
    var r = el.getBoundingClientRect(), mh = menu.offsetHeight, mw = menu.offsetWidth;
    var top = r.bottom + 8 + mh > window.innerHeight ? Math.max(8, r.top - mh - 8) : r.bottom + 8;
    menu.style.top = top + 'px';
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - mw - 8)) + 'px';
  }

  function applyOp(op, el) {
    var loc = buildLocator(el), tag = el.tagName.toLowerCase();
    var label = { click: 'click', clear: 'clear', hover: 'hover', scroll_into_view: 'scroll', wait_for: 'wait for element',
                  assert_visible: 'assert visible', assert_text: 'assert text', assert_value: 'assert value', assert_attr: 'assert href' }[op];
    var isToggle = tag === 'input' && /^(checkbox|radio)$/i.test(el.getAttribute('type') || '');
    drawBox(el, '#22c55e');
    if (op === 'click') {
      exitPick();                                   // the page is about to change - back to normal use
      var target = el.closest(CLICKABLE) || el;
      // Checkboxes/radios report their own check/uncheck step; anything else is recorded here.
      if (!isToggle) send({ type: 'click', locator: loc });
      try { target.click(); } catch (e) {}
    } else if (op === 'clear') {
      send({ type: 'clear', locator: loc, perform: true });  // done by Playwright so frameworks see real input events
    } else if (op === 'hover') {
      send({ type: 'hover', locator: loc, perform: true });   // real mouse move, done by Playwright
    } else if (op === 'scroll_into_view') {
      el.scrollIntoView({ block: 'center' });
      send({ type: 'scroll_into_view', locator: loc });
    } else if (op === 'wait_for') {
      send({ type: 'wait_for', locator: loc, value: 'visible' });
    } else if (op === 'assert_visible') {
      send({ type: 'assert_visible', locator: loc });
    } else if (op === 'assert_text') {
      send({ type: 'assert_text', locator: loc, value: norm(el.textContent) });
    } else if (op === 'assert_value') {
      send({ type: 'assert_value', locator: loc, value: String(el.value || '') });
    } else if (op === 'assert_attr') {
      send({ type: 'assert_attr', locator: loc, attr: 'href', value: el.getAttribute('href') });
    }
    say('Added: ' + label);
    var keep = pickOn;
    setTimeout(function () { if (pickOn) clearSel(); }, 450);
    if (!keep) clearSel();
  }

  // In pick mode the page must never see the pointer events, or selecting a
  // link would follow it. Mouse *moves* are left alone so hover styles work.
  ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'dblclick', 'contextmenu', 'auxclick', 'touchstart', 'touchend']
    .forEach(function (t) {
      window.addEventListener(t, function (e) {
        if (!pickOn || isOurs(e)) return;
        e.preventDefault(); e.stopImmediatePropagation();
      }, true);
    });
  window.addEventListener('click', function (e) {
    if (!pickOn || isOurs(e)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.target instanceof Element) showMenu(e.target);
  }, true);
  window.addEventListener('mousemove', function (e) {
    if (!pickOn || selEl || isOurs(e) || !(e.target instanceof Element)) return;
    drawBox(e.target);
  }, true);
  window.addEventListener('scroll', function () { if (selEl) drawBox(selEl); }, true);

  window.__tgSetRec = function (on, showToolbar, anchor) {
    recOn = !!on;
    anchorOn = !!anchor;
    if (on && showToolbar) ensureUi();
    if (host) host.style.display = (on && showToolbar) ? '' : 'none';
    if (ui.anchor) ui.anchor.classList.toggle('on', anchorOn);
    if (!on) exitPick();
  };

  // A fresh document (navigation/reload) asks Python whether a recording is live.
  function syncState() {
    try {
      var p = window.__tgRecordStep && window.__tgRecordStep({ type: '__state' });
      if (p && p.then) p.then(function (st) { if (st) window.__tgSetRec(st.rec, st.toolbar, st.anchor); });
    } catch (e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', syncState);
  else syncState();
})();
