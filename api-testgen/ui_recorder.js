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
  var recOn = false, pickOn = false, holdOn = false, selEl = null, anchorOn = false;   // anchorOn: prefer label-anchored locators

  function isOurs(e) {
    return !!(host && e.composedPath && e.composedPath().indexOf(host) >= 0);
  }

  // Hold: lets you inspect hover/blur-driven UI (dropdown lists, tooltips, popovers) that normally vanishes the
  // moment the pointer leaves it or focus moves. While Hold or Pick is on, the page no longer hears
  // mouse-leave / focus-loss events, so those widgets stay open while you reach for the toolbar and pick.
  // (Widgets driven purely by CSS :hover can't be held this way.) Registered first, so it runs before the page's own handlers.
  ['mouseout', 'mouseleave', 'pointerout', 'pointerleave', 'blur', 'focusout'].forEach(function (t) {
    window.addEventListener(t, function (e) {
      if (!recOn || !(holdOn || pickOn) || !e.isTrusted || isOurs(e)) return;
      e.stopImmediatePropagation();
    }, true);
  });
  // An explicit Hold also freezes the page's pointer tracking (mousemove / enter / over). Chart and map tooltips
  // follow mousemove and hide as soon as the pointer is anywhere that isn't a data point - including on the way to
  // the toolbar - so without this they vanish before you can pick them. Pick on its own leaves moves alone
  // (hover styles keep working), but Pick's own highlight still tracks the pointer.
  ['mousemove', 'pointermove', 'mouseover', 'pointerover', 'mouseenter', 'pointerenter'].forEach(function (t) {
    window.addEventListener(t, function (e) {
      if (!recOn || !e.isTrusted || isOurs(e)) return;
      if (t === 'mousemove' && pickOn && !selEl && e.target instanceof Element) drawBox(e.target);
      if (holdOn) e.stopImmediatePropagation();
    }, true);
  });
  // While our prompt is open, keep the page from hearing focus/keyboard traffic aimed at it. Modal dialogs
  // (focus traps) otherwise yank focus back out of the input, so typing and clearing silently do nothing.
  ['focusin', 'focusout', 'blur', 'keydown', 'keypress', 'keyup', 'beforeinput', 'input'].forEach(function (t) {
    window.addEventListener(t, function (e) {
      if (asking && isOurs(e)) e.stopImmediatePropagation();
    }, true);
  });
  // Keyboard shortcuts work while the pointer is parked on the widget you want to keep open:
  // Alt+H = hold, Alt+P = pick, Esc leaves pick.
  window.addEventListener('keydown', function (e) {
    if (!recOn || !e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.code === 'KeyH') { e.preventDefault(); e.stopImmediatePropagation(); setHold(!holdOn); }
    else if (e.code === 'KeyP') { e.preventDefault(); e.stopImmediatePropagation(); ensureUi(); pickOn ? exitPick() : enterPick(); }
  }, true);
  function setHold(on) {
    holdOn = !!on;
    if (ui.hold) ui.hold.classList.toggle('on', holdOn);
    if (ui.rec) ui.rec.classList.toggle('held', holdOn);
    say(holdOn ? 'Hold on: dropdowns/tooltips stay open (Alt+H to release)' : 'Hold off');
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

  // ── locator hardening ─────────────────────────────────────────────────────
  // A generated locator is FRAGILE when it leans on text that changes: a row's accessible name is all of its cells'
  // text (including what you typed or picked), a date, a number, a joined "A   B   C" string. Such a locator works
  // at record time and finds nothing on the next run. Detect that, and for controls inside table rows rebuild the
  // locator from a stable anchor: "the row whose name cell says X, then the control by its own name/label/column".
  var CONTAINER_ROLE = /^(row|cell|gridcell|rowheader|columnheader|table|grid|group|list|listitem|region|article|form|toolbar|navigation|main|dialog|tablist|menu|tree|treeitem)$/;
  function fragileText(s, el) {
    if (/\s{2,}/.test(s)) return 'joins the text of several cells';
    if (s.length > 50) return 'is a long text';
    if (/\b\d{1,2}[\/.:-]\d{1,2}(?:[\/.:-]\d{2,4})?\b/.test(s)) return 'contains a date or time';
    if (/\b\d{3,}\b/.test(s)) return 'contains a number';
    if (el && el.value && String(el.value).length >= 2 && s.indexOf(String(el.value)) >= 0) return 'contains the field\'s current value';
    return '';
  }
  function locatorIssues(loc, el) {
    var issues = [], why;
    function add(m) { if (issues.indexOf(m) < 0) issues.push(m); }
    if (loc.kind === 'pw') {
      var re = /get_by_role\("([\w-]+)"(?:,\s*name=("(?:[^"\\]|\\.)*"|re\.compile\([^)]*\)))?/g, m;
      while ((m = re.exec(loc.py || '')) !== null) {
        if (m[2] && CONTAINER_ROLE.test(m[1])) add('a ' + m[1] + '\'s name changes with its content');
      }
      var lre = /"((?:[^"\\]|\\.)*)"/g, lm;
      while ((lm = lre.exec(loc.py || '')) !== null) { why = fragileText(lm[1].replace(/\\"/g, '"'), el); if (why) add('text ' + why); }
      if (/\.nth\(/.test(loc.py || '') && el.closest('tr,[role="row"]')) add('depends on row position');
    } else if (loc.kind === 'role') {
      if (loc.name && CONTAINER_ROLE.test(loc.role)) add('a ' + loc.role + '\'s name changes with its content');
      why = loc.name && fragileText(loc.name, el); if (why) add('text ' + why);
      if (loc.nth != null && el.closest('tr,[role="row"]')) add('depends on row position');
    } else if (loc.kind === 'text') {
      why = fragileText(loc.value || '', el); if (why) add('text ' + why);
    } else if (loc.kind === 'css' && /:nth-of-type|nth=/.test(loc.value || '') && el.closest('tr,[role="row"]')) {
      add('depends on row position');
    }
    return issues;
  }

  var ROW_SEL = 'tr,[role="row"]', CELL_SEL = 'th,td,[role="cell"],[role="gridcell"],[role="rowheader"]';
  function cellKey(c) {
    var f = c.querySelector('a,strong,b,h1,h2,h3,h4,h5,h6,[class*="name" i]');
    var t = f ? norm(f.textContent) : '';
    if (!t) { for (var n = c.firstChild; n; n = n.nextSibling) { if (n.nodeType === 3 && norm(n.textContent)) { t = norm(n.textContent); break; } } }
    if (!t) t = norm(c.textContent);
    if (t.length < 2 || t.length > 40) return '';
    if (/^[\d\s.,:\/$%+-]+$/.test(t) || /\d{1,2}[\/.:-]\d{1,2}/.test(t)) return '';
    if (/^(select|save|edit|delete|remove|view|actions?|yes|no|submit|cancel)$/i.test(t)) return '';
    return t;
  }
  function stableAttr(v) { return v && v.length <= 60 && !/\d{4,}|^[:_]|^[0-9a-f]{8,}$/i.test(v); }
  // The control's own selector WITHIN its row: attribute-based for fields, role+name for buttons/links.
  function innerFor(ctl, row) {
    var tag = ctl.tagName.toLowerCase(), cands = [], v;
    function q(a, val) { return '[' + a + '=' + JSON.stringify(val) + ']'; }
    var testAttrs = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy'];
    for (var i = 0; i < testAttrs.length; i++) { v = ctl.getAttribute(testAttrs[i]); if (v) cands.push({ css: q(testAttrs[i], v), desc: q(testAttrs[i], v) }); }
    var isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    var role = roleOf(ctl), nm = accessibleName(ctl);
    if (!isField && role && nm && nm.length <= 30 && !fragileText(nm, null)) {
      cands.push({ role: role, name: nm, desc: role + ' "' + nm + '"' });
    }
    if ((v = ctl.getAttribute('name')) && stableAttr(v)) cands.push({ css: tag + q('name', v), desc: tag + q('name', v) });
    if ((v = ctl.getAttribute('aria-label')) && stableAttr(v)) cands.push({ css: tag + q('aria-label', v), desc: tag + q('aria-label', v) });
    if ((v = ctl.getAttribute('placeholder')) && stableAttr(v)) cands.push({ css: tag + q('placeholder', v), desc: tag + q('placeholder', v) });
    if ((v = ctl.id) && stableId(v) && !/^react-select-\d|^(rc|headlessui|radix)[-_:]/i.test(v)) cands.push({ css: tag + q('id', v), desc: tag + q('id', v) });
    // no steady attribute on the control itself: use the column it sits in (a table's columns rarely move)
    var cell = ctl.closest(CELL_SEL), colNo = 0, colName = '';
    if (cell && cell.parentElement === row) {
      colNo = Array.prototype.indexOf.call(row.children, cell) + 1;
      var table = row.closest('table'), hr = table && table.querySelector('thead tr'), hc = hr && hr.children[colNo - 1];
      colName = hc ? norm(hc.textContent) : '';
      if (colName.length > 30) colName = '';
    }
    var ty = (ctl.getAttribute('type') || '').toLowerCase();
    if (colNo && tag === 'input') cands.push({ css: ':scope > :nth-child(' + colNo + ') ' + tag, desc: tag + ' in the ' + (colName ? '"' + colName + '" ' : '#' + colNo + ' ') + 'column' });
    if (tag === 'input' && ty) cands.push({ css: 'input' + q('type', ty), desc: 'input' + q('type', ty) });
    cands.push({ css: tag, desc: tag });
    for (var k = 0; k < cands.length; k++) {
      var c = cands[k], hits;
      if (c.role) hits = Array.prototype.filter.call(row.querySelectorAll('*'), function (n) { return roleOf(n) === c.role && accessibleName(n) === c.name; });
      else { try { hits = Array.prototype.slice.call(row.querySelectorAll(c.css)); } catch (e) { hits = []; } }
      var at = hits.indexOf(ctl);
      if (at < 0) continue;
      c.nth = hits.length > 1 ? at : null;
      return c;
    }
    return null;
  }
  // A row's own identity beats its text: data-id="6a5d…" on the <tr>, or a hidden input / link carrying the record id.
  // Returns a CSS selector for the row that matches exactly this row in the page, or null.
  function rowIdentity(row) {
    var cands = [], v, i;
    function q(a, val) { return '[' + a + '=' + JSON.stringify(val) + ']'; }
    var attrs = ['data-id', 'data-row-id', 'data-rowid', 'data-row-key', 'data-key', 'data-testid', 'data-test-id', 'data-qa', 'data-cy', 'data-record-id', 'data-user-id'];
    for (i = 0; i < attrs.length; i++) {
      v = row.getAttribute(attrs[i]);
      if (v && v.length <= 80) cands.push({ css: q(attrs[i], v), desc: attrs[i] + ' ' + (v.length > 14 ? v.slice(0, 8) + '…' : v) });
    }
    var hid = row.querySelectorAll('input[type="hidden"][value]');
    for (i = 0; i < hid.length; i++) {
      var nm = hid[i].getAttribute('name'), hv = hid[i].getAttribute('value');
      if (nm && hv && hv.length >= 4 && hv.length <= 80 && /id|key|uuid/i.test(nm)) cands.push({ css: ':has(input[type="hidden"]' + q('name', nm) + q('value', hv) + ')', desc: nm + ' ' + (hv.length > 14 ? hv.slice(0, 8) + '…' : hv) });
    }
    for (i = 0; i < cands.length; i++) {
      var css = ':is(tr, [role="row"])' + cands[i].css, hits;
      try { hits = document.querySelectorAll(css); } catch (e) { continue; }
      if (hits.length === 1 && hits[0] === row) return { css: css, desc: cands[i].desc };
    }
    return null;
  }
  // Picking a row itself (or one of its cells) rather than a control inside it.
  function rowSelfLocator(el) {
    var isRow = el.matches && el.matches(ROW_SEL), cell = !isRow && el.closest(CELL_SEL);
    var row = isRow ? el : (cell && cell.parentElement && cell.parentElement.matches(ROW_SEL) ? cell.parentElement : null);
    if (!row) return null;
    var ident = rowIdentity(row);
    if (!ident) return null;
    var sel = ident.css, py = 'locator(' + JSON.stringify(ident.css) + ')', desc = 'the row with ' + ident.desc;
    if (cell) {
      var no = Array.prototype.indexOf.call(row.children, cell) + 1;
      sel += ' >> :scope > :nth-child(' + no + ')'; py += '.locator(":scope > :nth-child(' + no + ')")';
      desc = 'cell #' + no + ' of ' + desc;
    }
    return { kind: 'pw', selector: sel, py: py, desc: desc, stable: true, matches: 1 };
  }
  function rowScopedLocator(el) {
    var ctl = el.closest(INTERACTIVE) || el;
    var tag = ctl.tagName.toLowerCase();
    if (!(ctl.matches && ctl.matches(INTERACTIVE))) return rowSelfLocator(el);    // text/containers keep their normal locator unless the row has an id
    var row = ctl.closest(ROW_SEL);
    if (!row) return null;
    var ident = rowIdentity(row);
    if (ident) {
      var innerI = innerFor(ctl, row);
      if (innerI) {
        var selI = ident.css + ' >> ', pyI = 'locator(' + JSON.stringify(ident.css) + ')';
        if (innerI.role) {
          selI += 'role=' + innerI.role + '[name=' + JSON.stringify(innerI.name) + 's]';
          pyI += '.get_by_role(' + JSON.stringify(innerI.role) + ', name=' + JSON.stringify(innerI.name) + ', exact=True)';
        } else { selI += innerI.css; pyI += '.locator(' + JSON.stringify(innerI.css) + ')'; }
        if (innerI.nth != null) { selI += ' >> nth=' + innerI.nth; pyI += '.nth(' + innerI.nth + ')'; }
        return { kind: 'pw', selector: selI, py: pyI, desc: innerI.desc + ' in the row with ' + ident.desc, stable: true, matches: 1 };
      }
    }
    var allRows = Array.prototype.slice.call(document.querySelectorAll(ROW_SEL));
    var cells = Array.prototype.slice.call(row.querySelectorAll(CELL_SEL)).filter(function (c) { return !c.contains(ctl); });
    var key = '', matching = null;
    for (var i = 0; i < cells.length; i++) {
      var kk = cellKey(cells[i]);
      if (!kk) continue;
      var m = allRows.filter(function (r) { return norm(r.textContent).toLowerCase().indexOf(kk.toLowerCase()) >= 0; });
      if (m.indexOf(row) < 0) continue;
      if (!key || m.length < matching.length) { key = kk; matching = m; }
      if (m.length === 1) break;
    }
    if (!key) return null;
    var inner = innerFor(ctl, row);
    if (!inner) return null;
    var rowIdx = matching.length > 1 ? matching.indexOf(row) : null;
    // PW engine selector (used by replay) and the matching Python expression (used by generated tests)
    var sel = ':is(tr, [role="row"]):has-text(' + JSON.stringify(key) + ')' + (rowIdx != null ? ' >> nth=' + rowIdx : '') + ' >> ';
    var py = 'locator("tr, [role=\\"row\\"]").filter(has_text=' + JSON.stringify(key) + ')' + (rowIdx != null ? '.nth(' + rowIdx + ')' : '');
    if (inner.role) {
      sel += 'role=' + inner.role + '[name=' + JSON.stringify(inner.name) + 's]';
      py += '.get_by_role(' + JSON.stringify(inner.role) + ', name=' + JSON.stringify(inner.name) + ', exact=True)';
    } else {
      sel += inner.css;
      py += '.locator(' + JSON.stringify(inner.css) + ')';
    }
    if (inner.nth != null) { sel += ' >> nth=' + inner.nth; py += '.nth(' + inner.nth + ')'; }
    return { kind: 'pw', selector: sel, py: py, desc: inner.desc + ' in the row "' + key + '"' + (rowIdx != null ? ' (#' + (rowIdx + 1) + ' of ' + matching.length + ')' : ''), stable: true, matches: 1 };
  }

  function buildLocator(el) {
    var primary = playwrightLocator(el) || buildLocatorFallback(el), anchored = null;
    var issues = [];
    try { issues = locatorIssues(primary, el); } catch (e) {}
    if (issues.length) {
      var robust = null;
      try { robust = rowScopedLocator(el); } catch (e) {}
      if (robust) { robust.alts = [primary]; robust.hardened = issues; primary = robust; }   // the fragile one stays as an alternative
      else primary.warn = issues.join('; ');
    }
    try { anchored = labelAnchoredLocator(el); } catch (e) {}
    if (anchored) {                                       // grade it too: steady unless the label itself is changing text or it needs a position
      var aw = fragileText(anchored.anchor, null);
      if (aw) anchored.warn = 'the label "' + trunc(anchored.anchor, 30) + '" ' + aw;
      else if (/\)\[\d+\]$/.test(anchored.value || '')) anchored.warn = 'several controls share the label "' + trunc(anchored.anchor, 30) + '", so it relies on their order';
      else anchored.stable = true;
    }
    if (anchored && anchorOn) { anchored.alts = [primary]; return anchored; }   // ⚓ mode: value-independent first
    if (anchored) primary.alts = (primary.alts || []).concat([anchored]);       // otherwise keep it as an alternative
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
    if (asking) return;                                           // the name prompt owns the keyboard
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
    '#ask{position:fixed;display:none;top:70px;left:50%;transform:translateX(-50%);flex-direction:column;gap:6px;background:#111827;color:#f9fafb;padding:10px 12px;' +
    'border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.5);width:min(420px,90vw);pointer-events:auto}' +
    '#askq{white-space:pre-line;color:#d1d5db;font-size:12px}#askin{width:100%;font-size:13px;padding:6px 8px}' +
    '#ask .row{display:flex;gap:6px;justify-content:flex-end}#askok{background:#7c6aff}' +
    '#grip{cursor:grab;color:#9ca3af;padding:0 2px;letter-spacing:-2px;user-select:none;touch-action:none}#grip:active{cursor:grabbing}' +
    '#bar.mini>*:not(#grip):not(#rec):not(#hold):not(#pick):not(#min){display:none}' +
    '.rec.held:after{content:" \\2744";color:#7dd3fc}' +
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
    root.innerHTML = '<style>' + CSS_TEXT + '</style><div id="box"></div><div id="menu"></div><div id="ask"><label id="askq"></label><input id="askin" type="text" autocomplete="off" spellcheck="false"><div class="row"><button id="askok">OK</button><button id="askno">Cancel</button></div></div>' +
      '<div id="bar"><span id="grip" title="Drag to move the toolbar (double-click to reset)">&#8942;&#8942;</span><span class="rec" id="rec">&#9679; REC</span>' +
      '<button id="hold" title="Hold: keep dropdowns, tooltips and popovers open even when the pointer leaves them (shortcut Alt+H - use it while hovering the widget)">&#10052; Hold</button>' +
      '<button id="pick" title="Select an element without clicking it (Alt+P, Esc to leave)">&#127919; Pick</button>' +
      '<button id="anchor" title="Prefer locators anchored to the nearest label (ignores a field\'s current value, e.g. a date or selected option)">&#9875; Anchor</button>' +
      '<input id="ms" type="number" value="1000" min="0" step="250" title="milliseconds"><button id="wait">&#9201; Wait</button>' +
      '<button id="cmp" title="Compare two captured values, e.g.  price_list = price_cart   (also: !=, contains, !contains)">&#9878; Compare</button>' +
      '<button id="url" title="Assert the current page URL (path without query; edit the step to loosen or tighten it)">&#128279; URL</button>' +
      '<button id="back" title="Go back to the previous page (recorded as a step)">&#9664; Back</button>' +
      '<button id="reload" title="Reload the page (recorded as a step)">&#8635; Reload</button>' +
      '<button id="top" title="Scroll to top">&#10514;</button><button id="bot" title="Scroll to bottom">&#10515;</button><span id="msg"></span>' +
      '<button id="min" title="Shrink / expand the toolbar">&#8211;</button></div>';
    ['box', 'menu', 'ask', 'askq', 'askin', 'askok', 'askno', 'bar', 'grip', 'min', 'hold', 'rec', 'pick', 'anchor', 'ms', 'wait', 'cmp', 'url', 'back', 'reload', 'top', 'bot', 'msg'].forEach(function (id) { ui[id] = root.getElementById(id); });
    ui.hold.onclick = function () { setHold(!holdOn); };
    ui.min.onclick = function () { ui.bar.classList.toggle('mini'); savePos(); };
    // Keep focus in the page when a toolbar button is pressed, so a blur-closed widget isn't dismissed by the click itself.
    ui.bar.addEventListener('mousedown', function (e) { if (e.target.tagName !== 'INPUT') e.preventDefault(); });
    // Drag the toolbar anywhere (it can cover what you're trying to pick); the spot is remembered across pages.
    var drag = null;
    ui.grip.addEventListener('pointerdown', function (e) {
      var r = ui.bar.getBoundingClientRect();
      drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
      try { ui.grip.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    ui.grip.addEventListener('pointermove', function (e) { if (drag) placeBar(e.clientX - drag.dx, e.clientY - drag.dy); });
    ui.grip.addEventListener('pointerup', function () { if (drag) { drag = null; savePos(); } });
    ui.grip.addEventListener('dblclick', function () { barPos = null; ui.bar.classList.remove('mini'); ui.bar.style.left = ui.bar.style.top = ''; ui.bar.style.right = '10px'; savePos(); });
    window.addEventListener('resize', function () { if (barPos) placeBar(barPos.x, barPos.y); });
    ui.pick.onclick = function () { pickOn ? exitPick() : enterPick(); };
    ui.anchor.onclick = function () {
      anchorOn = !anchorOn; ui.anchor.classList.toggle('on', anchorOn);
      try { window.__tgRecordStep({ type: '__anchor', value: anchorOn }); } catch (e) {}
      say(anchorOn ? 'Anchor on: locators use the nearest label' : 'Anchor off');
    };
    ui.wait.onclick = function () { send({ type: 'wait', value: String(parseInt(ui.ms.value, 10) || 0) }); say('Added: wait ' + ui.ms.value + 'ms'); };
    ui.cmp.onclick = function () {
      ask('Compare two captured variables.\nExamples:  price_list = price_cart   |   title != old_title   |   banner contains name', '', function (t) {
        t = (t || '').trim();
        if (!t) return;
        var m = t.match(/^(\w+)\s*(!=|=|!contains|contains)\s*(\w+)$/);
        if (!m) { say('Use:  first = second'); return; }
        send({ type: 'compare', left: m[1], right: m[3], op: { '=': 'equals', '!=': 'not_equals', contains: 'contains', '!contains': 'not_contains' }[m[2]], value: '' });
        say('Added: compare ' + m[1] + ' ' + m[2] + ' ' + m[3]);
      });
    };
    ui.url.onclick = function () {
      var u = location.origin + location.pathname;
      send({ type: 'assert_url', value: u }); say('Added: assert URL contains ' + u);
    };
    ui.back.onclick = function () { send({ type: 'back' }); say('Added: go back'); history.back(); };
    ui.reload.onclick = function () { send({ type: 'reload' }); say('Added: reload'); location.reload(); };
    ui.top.onclick = function () { window.scrollTo(0, 0); send({ type: 'scroll_page', value: 'top' }); say('Added: scroll to top'); };
    ui.bot.onclick = function () { window.scrollTo(0, document.documentElement.scrollHeight); send({ type: 'scroll_page', value: 'bottom' }); say('Added: scroll to bottom'); };
    document.documentElement.appendChild(host);
    host.style.display = 'none';
  }

  // In-page replacement for window.prompt(): the automation browser auto-accepts native dialogs (so the
  // old prompt flashed and closed with its default answer). Calls done(text) on OK/Enter, done(null) on Cancel/Esc.
  var asking = false;
  function ask(question, dflt, done) {
    ensureUi();
    asking = true;
    ui.askq.textContent = question;
    ui.askin.value = dflt || '';
    ui.ask.style.display = 'flex';
    function close(val) {
      asking = false;
      ui.ask.style.display = 'none';
      ui.askok.onclick = ui.askno.onclick = ui.askin.onkeydown = null;
      done(val);
    }
    ui.askok.onclick = function () { close(ui.askin.value); };
    ui.askno.onclick = function () { close(null); };
    ui.askin.onkeydown = function (e) {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); close(ui.askin.value); }
      else if (e.key === 'Escape') { e.preventDefault(); close(null); }
    };
    ['keyup', 'keypress'].forEach(function (t) { ui.askin['on' + t] = function (e) { e.stopPropagation(); }; });
    setTimeout(function () { ui.askin.focus(); ui.askin.select(); }, 0);
  }

  var barPos = null;   // {x, y} of the toolbar's top-left when the user has moved it
  function placeBar(x, y) {
    if (!ui.bar) return;
    var w = ui.bar.offsetWidth, h = ui.bar.offsetHeight;
    x = Math.max(0, Math.min(x, window.innerWidth - Math.min(w, window.innerWidth)));
    y = Math.max(0, Math.min(y, window.innerHeight - h));
    barPos = { x: x, y: y };
    ui.bar.style.right = 'auto'; ui.bar.style.left = x + 'px'; ui.bar.style.top = y + 'px';
  }
  function savePos() {   // remembered by the Python side, so it follows the toolbar across pages and origins
    try { window.__tgRecordStep({ type: '__toolpos', pos: barPos, mini: !!(ui.bar && ui.bar.classList.contains('mini')) }); } catch (e) {}
  }
  function applyPos(st) {
    if (!ui.bar || !st) return;
    if (st.pos) placeBar(st.pos.x, st.pos.y);
    ui.bar.classList.toggle('mini', !!st.mini);
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
    if (loc.desc) return loc.desc;
    if (loc.kind === 'pw') return loc.py;
    var d = loc.kind === 'role' ? loc.role + ' "' + loc.name + '"' : loc.kind + ' ' + JSON.stringify(loc.value);
    if (loc.kind === 'pattern') return 'text like /' + loc.value + '/' + (loc.scope ? ' in ' + loc.scope : '') + (loc.nth === -1 ? ' (last)' : loc.nth === 0 ? ' (first)' : '');
    return loc.nth != null ? d + ' #' + (loc.nth + 1) : d;
  }

  // Tooltips, hover cards and similar overlays ignore the mouse (pointer-events:none) and vanish or change
  // if the pointer moves onto them - so they can't be picked by clicking them, and a plain pick lands on
  // whatever is underneath. Find the ones visible on screen right now: those under the pointer first, then
  // the nearest. Only the outermost element of each overlay is returned.
  function findGhosts(x, y, under) {
    var all = document.body ? document.body.getElementsByTagName('*') : [], cand = [], vw = window.innerWidth, vh = window.innerHeight;
    for (var i = 0; i < all.length; i++) {
      var n = all[i];
      if (host && (n === host || host.contains(n))) continue;
      var r = n.getBoundingClientRect();
      if (r.width < 40 || r.height < 18 || r.width * r.height > vw * vh * 0.5) continue;
      if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
      var cs = getComputedStyle(n);
      if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.05) continue;
      if (cs.pointerEvents !== 'none' && n.getAttribute('role') !== 'tooltip') continue;
      if (!norm(n.textContent) || (under && n.contains(under))) continue;
      cand.push({ el: n, r: r });
    }
    cand = cand.filter(function (c) { return !cand.some(function (o) { return o.el !== c.el && o.el.contains(c.el); }); });
    cand.forEach(function (c) {
      var dx = Math.max(c.r.left - x, 0, x - c.r.right), dy = Math.max(c.r.top - y, 0, y - c.r.bottom);
      c.dist = Math.sqrt(dx * dx + dy * dy); c.inside = c.dist === 0;
    });
    cand.sort(function (a, b) { return (b.inside - a.inside) || (a.dist - b.dist); });
    return cand.slice(0, 4);
  }
  var pickCtx = null;          // where the pick happened: {x, y, under, ghost}

  function chartIndex(el) { return Array.prototype.indexOf.call(document.querySelectorAll('canvas'), el); }
  function chartData(el) {     // Chart.js instance data, when the page exposes the Chart global
    try {
      var c = window.Chart && window.Chart.getChart && window.Chart.getChart(el);
      return c ? { labels: c.data.labels || [], datasets: c.data.datasets.map(function (d) { return d.data; }) } : null;
    } catch (e) { return null; }
  }

  function showMenu(el, ctx) {
    if (ctx) pickCtx = ctx;
    selEl = el; drawBox(el);
    var tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    var isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    var isToggle = tag === 'input' && (type === 'checkbox' || type === 'radio');
    var text = norm(el.textContent), href = tag === 'a' ? el.getAttribute('href') : null;
    var ops = [['Click', 'click'], ['Hover', 'hover'], ['Scroll into view', 'scroll_into_view'],
               ['Wait for element', 'wait_for'], ['Assert visible', 'assert_visible']];
    if (!isField && text && text.length <= 200) ops.push(['Assert text: "' + trunc(text, 40) + '"', 'assert_text']);
    if (isField && !isToggle && tag !== 'select') { ops.splice(1, 0, ['Fill value…', 'fill']); ops.splice(2, 0, ['Clear field', 'clear']); }
    if (isField && !isToggle && type !== 'password') ops.push(['Assert value: "' + trunc(String(el.value || ''), 40) + '"', 'assert_value']);
    var canText = !isField && text && text.length <= 200;
    var dtText = isField ? String(el.value || '') : text;
    if (/\b\d{1,2}:\d{2}\b|\b(today|yesterday)\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\/\d{1,2}\/\d{4}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}/i.test(dtText) && dtText.length <= 200 && type !== 'password') {
      ops.push(['🕒 Assert time is now (just the time, ±2 min)', 'dt_time_now']);
      ops.push(['🕒 Assert date & time are now (±2 min)', 'dt_datetime_now']);
      ops.push(['📅 Assert date is today (just the date)', 'dt_date_today']);
    }
    if (canText) ops.push(['Capture text as variable…', 'capture_text']);
    if (isField && !isToggle && type !== 'password') ops.push(['Capture value as variable…', 'capture_value']);
    ops.push(['Capture tooltip (hover) as variable…', 'capture_tooltip']);
    if (href) ops.push(['Assert href: ' + trunc(href, 40), 'assert_attr']);
    var isCanvas = tag === 'canvas', cd = isCanvas ? chartData(el) : null;
    if (isCanvas && pickCtx) ops.splice(2, 0, ['Hover at this point (opens a tooltip)', 'hover_at']);
    if (cd) ops.push(['Assert chart data (Chart.js)', 'assert_chart']);

    var menu = ui.menu;
    menu.innerHTML = '';
    var hd = document.createElement('div'); hd.className = 'hd';
    var hl = buildLocator(el);
    hd.textContent = '<' + tag + '> ' + describe(hl);
    menu.appendChild(hd);
    var qual = document.createElement('div'); qual.className = 'hd';
    if (hl.hardened) { qual.textContent = '✓ Steady locator (the generated one depended on changing text: ' + hl.hardened[0] + ')'; qual.style.color = '#6fd8b4'; }
    else if (hl.warn) { qual.textContent = '⚠ May break next run: ' + hl.warn + '. ' + (anchorOn ? 'Try ▲ Parent to move to a steadier element, or ask developers to add a data-testid (or data-id on the row).' : 'Try ▲ Parent or ⚓ Anchor for a steadier one.'); qual.style.color = '#ffc766'; }
    else if (hl.stable) { qual.textContent = hl.anchor ? '✓ Steady locator (⚓ anchored on the label "' + trunc(hl.anchor, 30) + '")' : '✓ Steady locator'; qual.style.color = '#6fd8b4'; }
    if (hl.anchor && hl.warn) { qual.textContent = '⚠ ⚓ Anchor may break next run: ' + hl.warn + '.'; qual.style.color = '#ffc766'; }
    if (qual.textContent) menu.appendChild(qual);
    var anAlt = !hl.anchor && (hl.alts || []).filter(function (a) { return a.anchor; })[0];   // the ⚓ option, when it is not the one in use
    if (anAlt && (hl.warn || hl.hardened)) {
      var aq = document.createElement('div'); aq.className = 'hd';
      aq.textContent = (anAlt.stable ? '⚓ Also possible: anchor on the label "' : '⚓ Anchor on the label "') + trunc(anAlt.anchor, 30) + '"' + (anAlt.stable ? ' (steady) - turn on ⚓ Anchor in the toolbar' : ' (' + (anAlt.warn || 'less certain') + ')');
      aq.style.color = anAlt.stable ? '#6fd8b4' : '#ffc766';
      menu.appendChild(aq);
    }
    if (pickCtx && pickCtx.overlays && pickCtx.overlays.length) {
      var lab = document.createElement('div'); lab.className = 'hd'; lab.textContent = 'Overlays on screen right now (e.g. tooltips):';
      menu.appendChild(lab);
      pickCtx.overlays.forEach(function (ov) {
        var ob = document.createElement('button');
        ob.textContent = '🗨 ' + trunc(norm(ov.textContent), 52);
        ob.title = 'Select this overlay. The hover that makes it appear is recorded automatically.';
        ob.onclick = function () { showMenu(ov, { x: pickCtx.x, y: pickCtx.y, under: pickCtx.under, ghost: true, overlays: [] }); };
        menu.appendChild(ob);
      });
    }
    if (pickCtx && pickCtx.ghost && pickCtx.under && pickCtx.under !== el) {
      var below = document.createElement('button');
      below.textContent = '⇩ Pick what is underneath instead (<' + pickCtx.under.tagName.toLowerCase() + '>)';
      below.title = 'The selected element ignores the mouse (e.g. a tooltip). Pick the thing under it.';
      below.onclick = function () { showMenu(pickCtx.under, { x: pickCtx.x, y: pickCtx.y, under: pickCtx.under, ghost: false }); };
      menu.appendChild(below);
    }
    ops.forEach(function (o) {
      var b = document.createElement('button'); b.textContent = o[0];
      b.onclick = function () { applyOp(o[1], el); };
      menu.appendChild(b);
    });
    var row = document.createElement('div'); row.className = 'row';
    var up = document.createElement('button'); up.textContent = '▲ Parent';
    up.title = 'Select the enclosing element instead';
    up.onclick = function () { if (el.parentElement && el.parentElement !== document.documentElement) showMenu(el.parentElement, pickCtx); };
    var x = document.createElement('button'); x.textContent = '✕ Cancel'; x.onclick = clearSel;
    row.appendChild(up); row.appendChild(x); menu.appendChild(row);

    menu.style.display = 'flex';
    var r = el.getBoundingClientRect(), mh = menu.offsetHeight, mw = menu.offsetWidth;
    var top = r.bottom + 8 + mh > window.innerHeight ? Math.max(8, r.top - mh - 8) : r.bottom + 8;
    menu.style.top = top + 'px';
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - mw - 8)) + 'px';
  }

  // Text-shape locator for a date/time element: digits become \\d+, month names and AM/PM become classes.
  var DT_FIND = /(?:(?:[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-z]{3,9}\.?,? \d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\/\d{1,2}\/\d{4}|[Tt]oday|[Yy]esterday)[ ,]*(?:at )?)?(?:\d{1,2}:\d{2}(?::\d{2})? ?(?:[AaPp]\.?[Mm]\.?)?)?/g;
  function shapeOf(t) {
    return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\d+/g, '\\d+')
            .replace(/\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b/g, '[A-Za-z]+')
            .replace(/\b[AaPp]\\?\.?[Mm]\\?\.?\b/g, '[AaPp][Mm]').replace(/ +/g, '\\s+');
  }
  function stableScope(el) {
    for (var n = el.parentElement; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      var id = n.getAttribute('id'), tid = n.getAttribute('data-testid') || n.getAttribute('data-test') || n.getAttribute('data-qa');
      var lab = n.getAttribute('aria-label'), role = n.getAttribute('role');
      var sel = tid ? '[data-testid=' + JSON.stringify(tid) + ']' : null;
      if (!sel && id && !/\d{3,}|^:|^react|^radix|^mui|^headlessui/i.test(id)) sel = '#' + (window.CSS && CSS.escape ? CSS.escape(id) : id);
      if (!sel && lab && (role || /dialog|chat|panel|modal/i.test(lab))) sel = '[aria-label=' + JSON.stringify(lab) + ']';
      if (!sel && (role === 'dialog' || n.getAttribute('aria-modal') === 'true' || n.tagName === 'DIALOG')) sel = role === 'dialog' ? '[role="dialog"]' : n.tagName === 'DIALOG' ? 'dialog' : '[aria-modal="true"]';
      if (sel && countCss(sel) === 1) return sel;
    }
    return null;
  }
  function shapeLocator(el) {
    var t = norm(el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? el.value : el.textContent);
    if (!t) return null;
    var anchored = t.length <= 40;
    if (!anchored) {
      var best = '', m; DT_FIND.lastIndex = 0;
      while ((m = DT_FIND.exec(t)) !== null) { if (m[0].length > best.length) best = m[0]; if (m.index === DT_FIND.lastIndex) DT_FIND.lastIndex++; }
      if (best.length < 4) return null;
      t = best.trim();
    }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return null;      // inputs have no text node to match: keep the normal locator
    return { kind: 'pattern', value: (anchored ? '^' : '') + shapeOf(t) + (anchored ? '$' : ''), scope: stableScope(el), nth: -1 };
  }

  function applyOp(op, el) {
    var loc = buildLocator(el), tag = el.tagName.toLowerCase();
    var label = { hover_at: 'hover', assert_chart: 'assert chart data', click: 'click', clear: 'clear', fill: 'fill', hover: 'hover', scroll_into_view: 'scroll', wait_for: 'wait for element',
                  assert_visible: 'assert visible', assert_text: 'assert text', assert_value: 'assert value', assert_attr: 'assert href',
                  capture_text: 'capture text', capture_value: 'capture value', capture_tooltip: 'capture tooltip' }[op];
    var isToggle = tag === 'input' && /^(checkbox|radio)$/i.test(el.getAttribute('type') || '');
    drawBox(el, '#22c55e');
    if (pickCtx && pickCtx.ghost && pickCtx.under && op !== 'hover') {
      // The picked element only exists while the pointer is over something else (a tooltip over a chart):
      // record that hover first so replay and the generated test make it appear before checking it.
      var ur = pickCtx.under.getBoundingClientRect();
      send({ type: 'hover', locator: buildLocator(pickCtx.under), position: { x: Math.round(pickCtx.x - ur.left), y: Math.round(pickCtx.y - ur.top) } });
    }
    if (op === 'hover_at') {
      var rr = el.getBoundingClientRect();
      send({ type: 'hover', locator: loc, position: { x: Math.round(pickCtx.x - rr.left), y: Math.round(pickCtx.y - rr.top) } });
      label = 'hover at point';
    } else if (op === 'assert_chart') {
      var data = chartData(el);
      send({ type: 'assert_js', expr: 'Chart.getChart(document.querySelectorAll("canvas")[' + chartIndex(el) + ']).data.datasets.map(d => d.data)',
             value: JSON.stringify(data.datasets), url: location.href });
      label = 'assert chart data';
    } else if (op === 'click') {
      exitPick();                                   // the page is about to change - back to normal use
      var target = el.closest(CLICKABLE) || el;
      // Checkboxes/radios report their own check/uncheck step; anything else is recorded here.
      if (!isToggle) send({ type: 'click', locator: loc });
      try { target.click(); } catch (e) {}
    } else if (op === 'fill') {
      // Pick mode swallows clicks and keystrokes, so typing into a field isn't possible here: ask for the text instead.
      ui.menu.style.display = 'none';
      var isSecretField = (el.getAttribute('type') || '').toLowerCase() === 'password' ||
                          /pass(word)?|secret|token|otp|cvv|cvc/i.test((el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('autocomplete') || ''));
      ask('Text to fill into this field:', isSecretField ? '' : String(el.value || ''), function (txt) {
        if (txt === null) { say('Fill cancelled'); clearSel(); return; }
        if (txt === '') { send({ type: 'clear', locator: loc, perform: true }); say('Added: clear'); }
        else { send({ type: 'fill', locator: loc, value: txt, secret: !!isSecretField, perform: true }); say('Added: fill'); }
        if (!pickOn) clearSel(); else setTimeout(function () { if (pickOn) clearSel(); }, 450);
      });
      return;
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
    } else if (/^dt_/.test(op)) {
      // A time like "2:08 AM" changes on every run, so locate it by its SHAPE (digits:digits AM) and take the newest
      // one, not by its current text or a position-based xpath. Scoped to the nearest recognisable container.
      send({ type: 'assert_datetime', locator: shapeLocator(el) || loc, mode: op.slice(3), tol: 2 });
      label = { dt_time_now: 'assert time is now', dt_datetime_now: 'assert date & time are now', dt_date_today: 'assert date is today' }[op];
    } else if (/^capture_/.test(op)) {
      var source = op.slice(8);
      ui.menu.style.display = 'none';
      var dflt = (describe(loc).replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase().slice(0, 24)) || 'value';
      ask('Name this captured ' + source + ' (use it later in a Compare):', dflt, function (name) {
        var vname = (name || '').trim().replace(/\W+/g, '_').replace(/^_+|_+$/g, '');
        if (!vname) { say('Capture cancelled'); clearSel(); return; }
        send({ type: 'capture', locator: loc, source: source, 'var': vname });
        say('Added: capture ' + source + ' as ' + vname);
        if (!pickOn) clearSel(); else setTimeout(function () { if (pickOn) clearSel(); }, 450);
      });
      return;
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
    if (!(e.target instanceof Element)) return;
    var ghosts = findGhosts(e.clientX, e.clientY, e.target), inside = ghosts.length && ghosts[0].inside ? ghosts[0].el : null;
    showMenu(inside || e.target, { x: e.clientX, y: e.clientY, under: e.target, ghost: !!inside,
                                   overlays: ghosts.filter(function (g) { return g.el !== inside; }).map(function (g) { return g.el; }) });
  }, true);
  window.addEventListener('scroll', function () { if (selEl) drawBox(selEl); }, true);

  window.__tgSetRec = function (on, showToolbar, anchor, label) {
    recOn = !!on;
    anchorOn = !!anchor;
    if (on && showToolbar) ensureUi();
    if (host) host.style.display = (on && showToolbar) ? '' : 'none';
    if (ui.anchor) ui.anchor.classList.toggle('on', anchorOn);
    if (ui.rec) ui.rec.innerHTML = '&#9679; REC' + (label ? ' &middot; ' + label : '');   // which portal, when recording several
    if (!on) { exitPick(); setHold(false); }
  };

  // A fresh document (navigation/reload) asks Python whether a recording is live.
  function syncState() {
    try {
      var p = window.__tgRecordStep && window.__tgRecordStep({ type: '__state' });
      if (p && p.then) p.then(function (st) { if (st) { window.__tgSetRec(st.rec, st.toolbar, st.anchor, st.label); applyPos(st); } });
    } catch (e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', syncState);
  else syncState();
})();
