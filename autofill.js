(async function(){
  var P = __PAYLOAD__, d = P.data, rules = P.rules, lists = P.listRules;
  var filled = 0, skipped = 0, combos = 0, added = 0, unmatched = [];
  var SKIP = new RegExp(P.skip, 'i');
  var SECT = {};
  for (var s in P.sections) SECT[s] = new RegExp(P.sections[s], 'i');
  var sleep = function(ms){ return new Promise(function(r){ setTimeout(r, ms); }); };
  /* Workday fetches prompt options from the server, so these waits are
     generous — too short and the menu is still empty when it is read. */
  var PACE = {menu: 1300, pick: 900, settle: 250};

  function norm(s){
    return String(s)
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
      .replace(/[_\-.\[\]()/]+/g, ' ');
  }
  function labelText(el){
    var bits = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                el.getAttribute('autocomplete'), el.getAttribute('data-automation-id')];
    if (el.id) {
      var l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) bits.push(l.innerText);
    }
    var p = el.closest('label');
    if (p) bits.push(p.innerText);
    var fid = el.getAttribute('aria-labelledby');
    if (fid){
      var lb = document.getElementById(fid.split(' ')[0]);
      if (lb) bits.push(lb.innerText);
    }
    if (!p && !fid) {
      var lbls = document.querySelectorAll('label, legend');
      var nearest = null;
      for (var i = 0; i < lbls.length; i++){
        if (lbls[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) nearest = lbls[i];
        else break;
      }
      if (nearest) bits.push(nearest.innerText);
    }
    return bits.filter(Boolean).map(norm).join(' ').toLowerCase();
  }
  function visibleLabel(el){
    var t = '';
    if (el.id){
      var l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) t = l.innerText;
    }
    if (!t){ var p = el.closest('label'); if (p) t = p.innerText; }
    if (!t){
      var lb = el.getAttribute('aria-labelledby');
      if (lb){
        var ref = document.getElementById(lb.split(' ')[0]);
        if (ref) t = ref.innerText;
      }
    }
    if (!t) {
      var lbls = document.querySelectorAll('label, legend');
      var nearest = null;
      for (var i = 0; i < lbls.length; i++){
        if (lbls[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) nearest = lbls[i];
        else break;
      }
      if (nearest) t = nearest.innerText;
    }
    if (!t) t = el.getAttribute('aria-label') || el.placeholder || '';
    /* Unlabelled field: its own automation-id names it just as precisely, and
       without this the anchored rules fall through to the profile — which put
       the candidate's home city into every job's Location box. */
    if (!t) t = el.getAttribute('data-automation-id') || '';
    return norm(t).toLowerCase().replace(/[*✱]/g, '').trim();
  }
  function ancestorText(el){
    var bits = [], n = el.parentElement;
    for (var i = 0; i < 5 && n; i++, n = n.parentElement){
      if (!n.getAttribute) continue;
      var a = n.getAttribute('data-automation-id'), al = n.getAttribute('aria-label');
      if (a) bits.push(a);
      if (al) bits.push(al);
    }
    return bits.map(norm).join(' ').toLowerCase();
  }
  function datePart(el){
    var own = ((el.getAttribute('data-automation-id') || '') + ' ' +
               (el.getAttribute('aria-label') || '') + ' ' +
               (el.placeholder || '')).toLowerCase();
    /* A single "MM/YYYY" box is a whole date, not a month. Reading it as a
       month typed "03" into a field expecting "03/2025", which Workday then
       discarded — leaving From and To empty. */
    var hasMonth = /\bmonth\b|\bmm\b/.test(own), hasYear = /\byear\b|\byyyy\b/.test(own);
    if (hasMonth && hasYear) return null;
    if (hasMonth) return 'month';
    if (hasYear) return 'year';
    return null;
  }
  function datePiece(value, part, el){
    var m = String(value).match(/(\d{1,2})\s*[\/\-.]\s*(\d{4})/);
    var month = m ? ('0' + m[1]).slice(-2) : '';
    var year = m ? m[2] : (String(value).match(/\b(\d{4})\b/) || [])[1] || '';
    if (part === 'month') return month;
    if (part === 'year') return year;
    var ph = (el.placeholder || '').toLowerCase();
    if (/yyyy/.test(ph) && !/mm/.test(ph)) return year;
    if (/mm/.test(ph) && /yyyy/.test(ph)) return month && year ? month + '/' + year : year;
    return String(value);
  }
  function sectionOf(el){
    var n = el;
    for (var depth = 0; depth < 10 && n; depth++, n = n.parentElement){
      if (!n.getAttribute) continue;
      var own = norm((n.getAttribute('data-automation-id') || '') + ' ' + (n.id || ''));
      for (var s in SECT) if (SECT[s].test(own)) return s;
      var sib = n.previousElementSibling;
      for (var k = 0; k < 4 && sib; k++, sib = sib.previousElementSibling){
        if (!/^H[1-6]$|^LEGEND$/.test(sib.tagName)) continue;
        for (var s2 in SECT) if (SECT[s2].test(sib.innerText || '')) return s2;
        break;
      }
    }
    var heads = document.querySelectorAll('h1,h2,h3,h4,h5,legend'), nearest = null;
    for (var i = 0; i < heads.length; i++){
      if (!(heads[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) break;
      nearest = heads[i];
    }
    if (nearest){
      for (var s3 in SECT) if (SECT[s3].test(nearest.innerText || '')) return s3;
    }
    return '';
  }

  /* Identify which block a field belongs to, rather than what number it is.
     Workday ids read "workExperience-251--jobTitle", where 251 is an arbitrary
     widget id — useless as an entry number, but a perfectly good block label.
     Blocks are then numbered by the order their labels first appear. */
  var BLOCK_KEY = new RegExp(
    '(work\\s*experience|employment|education|school|websites?)\\s*(\\d{1,5})', 'i');
  function blockKey(el){
    var n = el;
    for (var depth = 0; depth < 10 && n; depth++, n = n.parentElement){
      if (!n.getAttribute) continue;
      var raw = (n.id || '') + ' ' + (n.getAttribute('name') || '') + ' ' +
                (n.getAttribute('data-automation-id') || '');
      var m = norm(raw).match(BLOCK_KEY);
      if (m) return (m[1] + '-' + m[2]).toLowerCase().replace(/\s+/g, '');
    }
    return null;
  }

  function isCombo(el){
    if (el.getAttribute('role') === 'combobox') return true;
    if ((el.className || '').toString().indexOf('select__input') >= 0) return true;
    if (el.hasAttribute('aria-haspopup')) return true;
    var p = el.parentElement;
    for (var i = 0; i < 5 && p; i++, p = p.parentElement) {
      if (p.getAttribute('role') === 'combobox') return true;
    }
    return false;
  }
  function setText(el, v){
    var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                          : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.style.outline = '2px solid #4c8dff';
    return true;
  }
  /* Date boxes are keyboard-driven spinbuttons: assigning .value looks right
     until they revalidate on blur and throw it away. Typing through
     execCommand produces the same events a person would, and the result is
     checked rather than assumed. */
  function typeText(el, v){
    try {
      el.focus();
      if (el.setSelectionRange) el.setSelectionRange(0, (el.value || '').length);
      el.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'a'}));
      var ok = false;
      try { ok = document.execCommand('insertText', false, v); } catch (e) {}
      if (!ok || (el.value || '').indexOf(v.slice(0, 2)) < 0) setText(el, v);
      el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, key:v.slice(-1)}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
      el.style.outline = '2px solid #4c8dff';
      return true;
    } catch (e) {
      return setText(el, v);
    }
  }
  function setSelect(el, v){
    var want = String(v).toLowerCase().trim(), best = -1;
    for (var i = 0; i < el.options.length; i++){
      var t = (el.options[i].text || '').toLowerCase().trim();
      var val = (el.options[i].value || '').toLowerCase().trim();
      if (!t && !val) continue;
      if (t === want || val === want) { best = i; break; }
      if (best < 0 && t.length > 2 && (t.indexOf(want) >= 0 || want.indexOf(t) >= 0)) best = i;
    }
    if (best < 0) return false;
    el.selectedIndex = best;
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.style.outline = '2px solid #4c8dff';
    return true;
  }
  async function setCombo(el, v){
    var control = el.closest('[role="combobox"]') ||
                  el.closest('[class*="select__control"]') ||
                  el.closest('[class*="select"]') || el.parentElement;
    control.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
    control.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, button:0}));
    if (control.click) control.click();
    try { el.focus(); } catch (e) {}
    el.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'ArrowDown', keyCode:40}));
    
    var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    if (setter) {
      try {
        if (!document.execCommand('insertText', false, v)) setter.call(el, v);
      } catch(e) { try { setter.call(el, v); } catch(e2){} }
      el.dispatchEvent(new Event('input', {bubbles:true}));
    }
    
    await sleep(350);
    var opts = document.querySelectorAll(
      '[class*="select__option"],[role=option],[data-automation-id="promptOption"]'
    );
    var want = String(v).toLowerCase().trim(), hit = null;
    for (var i = 0; i < opts.length; i++){
      var t = (opts[i].innerText || '').trim().toLowerCase();
      if (!t) continue;
      if (t === want) { hit = opts[i]; break; }
      if (!hit && t.indexOf(want) >= 0) hit = opts[i];
    }
    if (hit){
      ['mousedown','mouseup','click'].forEach(function(ev){
        hit.dispatchEvent(new MouseEvent(ev, {bubbles:true, button:0}));
      });
      await sleep(180);
      control.style.outline = '2px solid #4c8dff';
      el.style.outline = '2px solid #4c8dff';
      return true;
    }
    try {
      if (setter) setter.call(el, '');
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'Escape'}));
    } catch (e) {}
    return false;
  }
  /* Workday's Skills / Field of Study are multiselects: a search box plus a
     picker. Typing "Java, Springboot, NodeJS" into it matches nothing and
     submits nothing, so each value is searched and picked one at a time, and
     the box is emptied afterwards rather than left holding dead text. */
  function isMultiSelect(el){
    return /multi\s*select/.test(ancestorText(el));
  }
  /* Count only the chosen pills. Counting every <li> also counted the open
     menu's own rows, so the before/after check meant nothing. */
  function countChips(container){
    return container.querySelectorAll(
      '[data-automation-id*="selectedItem"],[data-automation-id="pill"],' +
      '[class*="multi-value"],[data-automation-id="selectedItemList"] > *').length;
  }
  function squash(s){ return String(s).toLowerCase().replace(/[^a-z0-9]/g, ''); }
  /* Is this value already a pill? Pill text carries the remove button's "×" or
     "Remove", so those are stripped and the rest compared whole — substring
     matching would read the "Ruby on Rails" pill as "Ruby" being done. */
  function chipsHave(container, v){
    var want = squash(v);
    if (!want) return false;
    var pills = container.querySelectorAll(
      '[data-automation-id*="selectedItem"],[data-automation-id="pill"],' +
      '[class*="multi-value"],[data-automation-id="selectedItemList"] > *');
    for (var i = 0; i < pills.length; i++){
      var t = squash(pills[i].innerText || '')
                .replace(/^(x|remove|delete)/, '')
                .replace(/(x|remove|delete)$/, '');
      if (t === want) return true;
    }
    return false;
  }
  /* Already ticked — clicking it again would remove it. */
  function alreadyChosen(opt){
    if (opt.getAttribute('aria-selected') === 'true') return true;
    if (opt.getAttribute('aria-checked') === 'true') return true;
    var box = opt.querySelector('input[type=checkbox]');
    return !!(box && box.checked);
  }
  async function clearBox(el){
    try {
      el.focus();
      if (el.setSelectionRange) el.setSelectionRange(0, (el.value || '').length);
      if (!document.execCommand('insertText', false, '')) setText(el, '');
    } catch (e) { setText(el, ''); }
    if (el.value) setText(el, '');
    el.dispatchEvent(new Event('input', {bubbles:true}));
    await sleep(PACE.settle);
  }
  /* Type `query`, wait for the menu, return the best option element.
     `strict` accepts only a punctuation-insensitive exact match; `target` is
     the value being matched when it differs from what was typed. */
  async function search(box, query, strict, target){
    box.focus();
    if (box.setSelectionRange) box.setSelectionRange(0, (box.value || '').length);
    try { document.execCommand('insertText', false, query); }
    catch (e) { setText(box, query); }
    box.dispatchEvent(new Event('input', {bubbles:true}));
    await sleep(PACE.menu);
    var opts = document.querySelectorAll(
      '[data-automation-id="promptOption"],[data-automation-id="promptLeafNode"],' +
      '[data-automation-id="multiSelectOption"],[role=option],[role=checkbox],' +
      '[class*="select__option"]');
    var want = String(target || query).toLowerCase(), flat = squash(want);
    var exact = null, same = null, loose = null;
    for (var j = 0; j < opts.length; j++){
      var t = (opts[j].innerText || '').trim().toLowerCase();
      if (!t || alreadyChosen(opts[j])) continue;
      if (t === want) { exact = opts[j]; break; }
      if (!same && squash(t) === flat) same = opts[j];
      if (!loose && !strict && t.indexOf(want) >= 0) loose = opts[j];
    }
    return exact || same || (strict ? null : loose);
  }
  /* One shot. Token inputs that let you type your own value (react-select
     creatable, Greenhouse, most plain tag boxes) turn a pasted comma list into
     every pill at once, and otherwise accept type+Enter without any server
     round trip. Either way all skills land in one pass instead of ~40 searches
     against the portal's own catalogue. Returns how many pills appeared, 0 if
     the widget is a server-backed picker — the caller then does it the slow way.
     ponytail: no per-widget detection, just try and count the pills. */
  async function oneShot(box, container, values){
    var before = countChips(container);
    box.focus();
    try {
      var dt = new DataTransfer();
      dt.setData('text/plain', values.join(', '));
      box.dispatchEvent(new ClipboardEvent('paste',
        {bubbles:true, cancelable:true, clipboardData:dt}));
      await sleep(PACE.settle);
    } catch (e) {}
    var gained = countChips(container) - before;
    if (gained >= 2){ await clearBox(box); return gained; }

    var total = 0;
    for (var i = 0; i < values.length; i++){
      var live = box.isConnected ? box : (box.id ? document.getElementById(box.id) : null);
      if (!live) break;
      live.focus();
      try { document.execCommand('insertText', false, values[i]); }
      catch (e) { setText(live, values[i]); }
      live.dispatchEvent(new Event('input', {bubbles:true}));
      await sleep(PACE.settle);
      ['keydown','keypress','keyup'].forEach(function(ev){
        live.dispatchEvent(new KeyboardEvent(ev,
          {bubbles:true, cancelable:true, key:'Enter', code:'Enter', keyCode:13, which:13}));
      });
      await sleep(PACE.settle);
      var now = countChips(container);
      /* Enter did nothing — server-backed picker. Undo the dead text and bail
         so the one-at-a-time path can run cleanly. */
      if (now === before){
        await clearBox(live);
        if (!total) return 0;
        continue;
      }
      total += now - before;
      before = now;
      box = live;
    }
    return total;
  }
  async function setMultiSelect(el, v){
    /* Workday rebuilds this widget after every pick, so the input node from
       the first round is detached by the second — typing into it does nothing
       and only the first skill ever lands. Everything is re-found each pass. */
    var boxId = el.id || '';
    var find = function(){
      var live = boxId ? document.getElementById(boxId) : null;
      return (live && live.isConnected) ? live : (el.isConnected ? el : null);
    };
    var holder = function(node){
      return node.closest('[data-automation-id="multiSelectContainer"]') ||
             node.closest('[class*="multiSelect"]') ||
             node.parentElement.parentElement || node.parentElement;
    };
    var wanted = String(v).split(',').map(function(x){ return x.trim(); })
                          .filter(Boolean);
    var picked = 0, container = holder(el);

    var fast = await oneShot(el, container, wanted);
    if (fast >= wanted.length){
      container.style.outline = '2px solid #4c8dff';
      return true;
    }
    /* Partial one-shot: keep what landed, let the slow path chase the rest.
       ponytail: no diffing which ones landed — alreadyChosen() skips dupes. */
    picked = fast;

    for (var i = 0; i < wanted.length; i++){
      /* Landed in the one shot already — searching for it again finds only a
         ticked option, which would be reported as "not offered". */
      if (fast && chipsHave(container, wanted[i])) continue;
      var box = find();
      if (!box) break;
      container = holder(box);
      var before = countChips(container);

      /* Two passes. The portal's own search is literal, so "Springboot" finds
         nothing when its catalogue says "Spring Boot". The retry searches a
         short prefix and then only accepts a match that is identical once
         punctuation and spaces are removed — close enough to be the same
         skill, strict enough not to invent one. */
      var hit = await search(box, wanted[i], false);
      if (!hit && squash(wanted[i]).length > 3){
        box = find() || box;
        hit = await search(box, wanted[i].slice(0, 4), true, wanted[i]);
      }
      var got = false;
      if (hit){
        var target = hit.querySelector('input[type=checkbox]') || hit;
        ['mousedown','mouseup','click'].forEach(function(ev){
          target.dispatchEvent(new MouseEvent(ev, {bubbles:true, button:0}));
        });
        await sleep(PACE.pick);
        if (countChips(holder(find() || box)) > before){ picked++; got = true; }
      }
      /* The portal has no such option — say so rather than dropping it
         silently, since the box ends up looking correctly filled. */
      if (!got) unmatched.push(wanted[i]);
      var after = find();
      if (after) await clearBox(after);
    }
    if (picked){ container.style.outline = '2px solid #4c8dff'; return true; }
    var last = find();
    if (last) last.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'Escape'}));
    return false;
  }
  async function setVal(el, v){
    if (el.tagName === 'SELECT') return setSelect(el, v);
    if (isMultiSelect(el)) return await setMultiSelect(el, v);
    if (isCombo(el)) return await setCombo(el, v);
    return setText(el, v);
  }

  function fillable(el){
    if (el.type === 'hidden' || el.type === 'file' || el.type === 'password') return false;
    if (el.type === 'radio') return false;   /* handled separately, by group */
    if (el.disabled || el.readOnly) return false;
    return true;
  }

  /* Workday shows one empty block and expects "Add Another" for the rest, so
     without this only the first job is ever filled. */
  var SIGNATURE = {experience: /\bcompany\b|\bemployer\b/i,
                   education: /\bschool\b|\buniversity\b|\bcollege\b/i,
                   websites: /^url\b|^website\b|^link\b/i};
  function countBlocks(listName){
    var seen = 0;
    var els = document.querySelectorAll('input, textarea, select');
    for (var i = 0; i < els.length; i++){
      if (!fillable(els[i])) continue;
      if (sectionOf(els[i]) !== listName) continue;
      if (SIGNATURE[listName].test(visibleLabel(els[i]) || labelText(els[i]))) seen++;
    }
    return seen;
  }
  function findAddButton(listName){
    var btns = document.querySelectorAll('button,[role=button],a');
    var best = null;
    for (var i = 0; i < btns.length; i++){
      var t = (btns[i].innerText || btns[i].getAttribute('aria-label') || '').trim();
      if (!t || t.length > 30) continue;
      if (!/^\s*(add another|add|\+\s*add)\b/i.test(t)) continue;
      if (btns[i].disabled) continue;
      if (sectionOf(btns[i]) !== listName) continue;
      best = btns[i];   /* last one in the section is the section's own button */
    }
    return best;
  }
  async function ensureBlocks(listName){
    var wanted = (d[listName] || []).length;
    if (!wanted) return;
    for (var guard = 0; guard < wanted + 3; guard++){
      var have = countBlocks(listName);
      if (have >= wanted) return;
      var btn = findAddButton(listName);
      if (!btn) return;
      /* Workday renders Websites with no blocks at all — just "Add". Bailing on
         have === 0 meant the section was skipped entirely, so nothing was
         filled. The Add button is section-scoped, so clicking it here can only
         open a block in this section. */
      btn.click();
      added++;
      await sleep(420);
      if (countBlocks(listName) <= have) return;   /* click did nothing */
    }
  }

  if (!P.inspect){
    await ensureBlocks('experience');
    await ensureBlocks('education');
    await ensureBlocks('websites');
  }
  var report = [];

  /* Yes/No asked as radios rather than a dropdown (Lever, Greenhouse).
     The question lives above the buttons, so each group is matched on its
     surrounding text and then the button whose own label is exactly the
     answer is clicked — never a partial match. */
  function planRadios(){
    var groups = {}, radios = document.querySelectorAll('input[type=radio]');
    for (var i = 0; i < radios.length; i++){
      var r = radios[i];
      if (r.disabled) continue;
      var name = r.name || ('anon' + i);
      (groups[name] = groups[name] || []).push(r);
    }
    for (var g in groups){
      var set = groups[g];
      if (set.some(function(x){ return x.checked; })) continue;
      var fs = set[0].closest('fieldset,[role=radiogroup],[role=group]') ||
               set[0].parentElement.parentElement;
      var qtext = norm((fs && fs.innerText) || '').toLowerCase();
      if (!qtext || SKIP.test(qtext)) continue;
      for (var r2 = 0; r2 < rules.length; r2++){
        var key = rules[r2][0], want = String(d[key] || '').toLowerCase();
        if (!want || key.indexOf('q_') !== 0) continue;
        if (!new RegExp(rules[r2][1], 'i').test(qtext)) continue;
        for (var k = 0; k < set.length; k++){
          if (visibleLabel(set[k]).trim() === want){
            plan.push({el: set[k], tick: true});
            break;
          }
        }
        break;
      }
    }
  }

  /* ---- plan ---------------------------------------------------------- */
  var plan = [], bases = {}, counters = {}, explicit = {};
  var dupCheck = {}, unreliable = {}, mapping = {}, blockOrder = {};
  var els = document.querySelectorAll('input, textarea, select');
  for (var i = 0; i < els.length; i++){
    var el = els[i];
    if (!fillable(el)) continue;

    /* A field that already has a value is not skipped outright: it still has
       to be counted, or the block ordering shifts. Clicking the bookmarklet
       twice used to make block 2 "the first empty Company" and refill it with
       job 1. It is planned as usual and simply not written. */
    var prefilled = false;
    if (el.tagName === 'SELECT') prefilled = el.selectedIndex > 0;
    else if (isCombo(el)){
      var ctrl = el.closest('[class*="select__control"]');
      prefilled = !!(ctrl && ctrl.querySelector('[class*="single-value"]'));
    } else if (el.type !== 'checkbox'){
      prefilled = !!(el.value && el.value.trim());
    }

    var part = datePart(el);
    var hay = labelText(el);
    var lab = visibleLabel(el);
    if (part){
      /* A Month box is labelled only "Month"; which date it belongs to is in
         its own id (workExperience-260--startDate-dateSectionMonth-input) and
         sometimes in an ancestor. Both go into the probe, or the anchored
         start/end rules never see "start date" and From/To stay empty. */
      var ctx = ' ' + norm((el.id || '') + ' ' + (el.name || '')).toLowerCase()
              + ' ' + ancestorText(el);
      hay = hay + ctx;
      lab = lab + ctx;
    }
    /* Some fields carry no id, name or label of their own — Workday's Degree
       box sits bare inside formField-degree. Its wrapper names it. */
    if (!hay.trim()) hay = ancestorText(el);
    if (!lab.trim()) lab = ancestorText(el);

    /* Diagnostic mode: describe what the page looks like and what this script
       decided, with no values, so it can be shared without leaking details. */
    if (P.inspect){
      var chain = [], nn = el.parentElement;
      for (var c = 0; c < 6 && nn; c++, nn = nn.parentElement){
        if (!nn.getAttribute) continue;
        var aid = nn.getAttribute('data-automation-id');
        if (aid) chain.push(aid);
      }
      report.push({
        tag: el.tagName, type: el.type || '',
        id: el.id || '', name: el.name || '',
        auto: el.getAttribute('data-automation-id') || '',
        aria: el.getAttribute('aria-label') || '',
        ph: el.placeholder || '',
        label: visibleLabel(el).slice(0, 60),
        ancestors: chain,
        section: sectionOf(el),
        blockKey: blockKey(el),
        datePart: part,
        hasValue: prefilled
      });
      continue;
    }
    if (!hay.trim()) continue;

    if (el.type === 'checkbox'){
      if (/currently[\s_-]*work|current[\s_-]*(role|position)/.test(hay)
          && sectionOf(el) === 'experience' && (d.experience || []).length){
        pushList(el, 'experience', 'current', null, true, false);
        continue;
      }
      /* A lone consent tickbox: only ever ticked when you have set that
         answer to Yes yourself. Agreeing to terms is your decision. */
      if (!SKIP.test(hay)){
        for (var cr = 0; cr < rules.length; cr++){
          if (!new RegExp(rules[cr][1], 'i').test(hay)) continue;
          if (String(d[rules[cr][0]] || '').toLowerCase() === 'yes' && !prefilled){
            plan.push({el: el, tick: true});
          }
          break;
        }
      }
      continue;
    }
    if (SKIP.test(hay)) { if (!prefilled) skipped++; continue; }

    var sect = sectionOf(el);
    var matched = false;

    if (sect){
      var claimed = false;
      for (var L = 0; L < lists.length && !matched; L++){
        var listName = lists[L][0], field = lists[L][1];
        var pattern = lists[L][2], anchored = lists[L][3];
        if (listName !== sect) continue;
        if (!new RegExp(pattern, 'i').test(anchored ? lab : hay)) continue;
        claimed = true;
        if (!(d[listName] || []).length) break;
        pushList(el, listName, field, part, false, prefilled);
        matched = true;
      }
      if (matched || claimed) continue;
    }

    for (var r = 0; r < rules.length && !matched; r++){
      var key = rules[r][0], val = d[key];
      if (!val) continue;
      if (new RegExp(rules[r][1], 'i').test(hay)){
        if (!prefilled) plan.push({el: el, value: val});
        matched = true;
      }
    }
    if (matched) continue;

    for (var L2 = 0; L2 < lists.length; L2++){
      var ln = lists[L2][0], fl = lists[L2][1];
      var pat = lists[L2][2], needs = lists[L2][3];
      if (!(d[ln] || []).length || needs) continue;
      if (!new RegExp(pat, 'i').test(hay)) continue;
      pushList(el, ln, fl, part, false, prefilled);
      break;
    }
  }

  function pushList(el, listName, field, part, isCheck, skipFill){
    /* Two independent readings of "which entry is this?":
         key     the block this field sits in, numbered by first appearance
         ordIdx  document order of this particular field
       The key is preferred because a block may be missing a field — the first
       job here has no End date, which shifted every later To by one when
       counting fields alone. The key is discarded if the same field turns up
       twice under it, which means it labels the whole section, not a block. */
    var ckey = listName + '.' + field + (part ? '.' + part : '');
    var ordIdx = (counters[ckey] = (counters[ckey] === undefined ? 0 : counters[ckey] + 1));

    var key = blockKey(el), keyIdx = null;
    if (key !== null){
      var order = blockOrder[listName] || (blockOrder[listName] = []);
      keyIdx = order.indexOf(key);
      if (keyIdx < 0){ order.push(key); keyIdx = order.length - 1; }
      var seenKey = key + '|' + field + (part ? '.' + part : '');
      if (dupCheck[seenKey]) unreliable[listName] = true;
      dupCheck[seenKey] = true;
    } else {
      unreliable[listName] = true;     /* no block label -> fall back to order */
    }
    plan.push({el: el, list: listName, field: field, ordIdx: ordIdx, keyIdx: keyIdx,
               part: part, check: isCheck, skip: skipFill});
  }

  if (P.inspect){
    var addBtns = [];
    var allBtns = document.querySelectorAll('button,[role=button],a');
    for (var b = 0; b < allBtns.length; b++){
      var bt = (allBtns[b].innerText || allBtns[b].getAttribute('aria-label') || '').trim();
      if (bt && bt.length < 30 && /^\s*(add another|add|\+\s*add)\b/i.test(bt)){
        addBtns.push({text: bt, section: sectionOf(allBtns[b])});
      }
    }
    var out = JSON.stringify({build: P.build, url: location.hostname,
      counts: {experience: countBlocks('experience'), education: countBlocks('education')},
      addButtons: addBtns, fields: report}, null, 1);
    try { await navigator.clipboard.writeText(out); }
    catch (e) { console.log(out); }
    alert('Form report copied to the clipboard (' + report.length + ' fields, no values).'
        + '\nBlocks seen — experience: ' + countBlocks('experience')
        + ', education: ' + countBlocks('education')
        + '\n\nPaste it into the chat. Nothing on this page was changed.');
    return;
  }

  planRadios();

  /* ---- fill ---------------------------------------------------------- */
  for (var pi = 0; pi < plan.length; pi++){
    var item = plan[pi];
    var entries = item.list ? (d[item.list] || []) : null;
    var entry = null, which = -1;
    if (entries){
      var useKey = item.keyIdx !== null && !unreliable[item.list];
      which = useKey ? item.keyIdx : item.ordIdx;
      entry = entries[which];
      if (entry && !item.skip){
        var mk = item.list;
        (mapping[mk] = mapping[mk] || {})[which + 1] =
          entry.company || entry.school || entry.url || ('entry ' + (which + 1));
      }
    }

    if (item.skip) continue;
    if (item.tick){
      if (!item.el.checked){
        item.el.click();
        item.el.style.outline = '2px solid #4c8dff';
        filled++;
      }
      continue;
    }
    if (item.check){
      var isCurrent = entry && (entry.current === 'yes' ||
                        /^(present|current|now)$/i.test((entry.end || '').trim()));
      if (isCurrent && !item.el.checked){
        item.el.click();
        item.el.style.outline = '2px solid #4c8dff';
        filled++;
      }
      continue;
    }

    var value = item.value;
    if (value === undefined) value = entry ? entry[item.field] : '';
    if (!value) continue;
    var isDate = item.field === 'start' || item.field === 'end';
    if (isDate){
      value = datePiece(value, item.part, item.el);
      if (!value) continue;
    }
    var ok = isDate && item.el.tagName === 'INPUT' && !isCombo(item.el)
             && !isMultiSelect(item.el)
      ? typeText(item.el, value)
      : await setVal(item.el, value);
    if (ok) filled++;
    else if (isCombo(item.el) || isMultiSelect(item.el)) combos++;
  }

  /* Show which entry went into which block — if this ever reads
     "1 Guidewire, 2 Guidewire" the mismatch is obvious immediately. */
  var mapLines = [];
  for (var ml in mapping){
    var parts = [];
    for (var slot in mapping[ml]) parts.push(slot + ') ' + mapping[ml][slot]);
    if (parts.length) mapLines.push(ml + ': ' + parts.join(', '));
  }

  var fileInputs = document.querySelectorAll('input[type=file]').length;

  /* Skills is the field that most often loses a race with the portal's own
     re-render, so leave one CTA sitting in the top layer that refills the whole
     list in one shot. A link, not a button: forms swallow stray button clicks. */
  function findSkillsBox(){
    var els = document.querySelectorAll('input, textarea');
    for (var i = 0; i < els.length; i++){
      if (!fillable(els[i])) continue;
      if (!/\bskills?\b/.test(visibleLabel(els[i]) + ' ' + labelText(els[i]))) continue;
      if (els[i].offsetParent === null) continue;
      return els[i];
    }
    return null;
  }
  var skillsBox = findSkillsBox(), skillsVal = d && (d.skills || (d.profile || {}).skills);
  if (skillsBox && skillsVal){
    var cta = document.createElement('a');
    cta.href = 'javascript:void 0';
    cta.textContent = '＋ Add all skills (one shot)';
    cta.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483647;' +
      'background:#4c8dff;color:#fff;font:600 13px/1 system-ui,sans-serif;' +
      'padding:10px 14px;border-radius:8px;text-decoration:none;' +
      'box-shadow:0 2px 10px rgba(0,0,0,.25);cursor:pointer';
    cta.addEventListener('click', async function(e){
      e.preventDefault();
      if (cta.dataset.busy) return;
      cta.dataset.busy = '1';
      cta.textContent = 'Adding…';
      var live = findSkillsBox() || skillsBox;
      var ok = false;
      try { ok = await setMultiSelect(live, skillsVal); } catch (err) {}
      cta.textContent = ok ? '✓ Skills added — click to redo' : '⚠ Nothing added — retry';
      delete cta.dataset.busy;
    });
    document.body.appendChild(cta);
  }

  alert('Apply kit ' + (P.build || '?') + ' — filled ' + filled + ' field(s).' +
        (mapLines.length ? '\n' + mapLines.join('\n') : '') +
        (added ? '\nAdded ' + added + ' extra block(s) for your other entries.' : '') +
        (skipped ? '\nSkipped ' + skipped + ' demographic/EEO field(s) on purpose.' : '') +
        (combos ? '\n' + combos + ' dropdown(s) had no matching option — pick those yourself.' : '') +
        (unmatched.length ? '\nNot offered by this form: ' + unmatched.join(', ') : '') +
        (fileInputs ? '\n' + fileInputs + ' file upload(s) still need you — ' +
                      'browsers do not let scripts attach your resume.' : '') +
        '\n\nCheck every value, then submit it yourself.');
})();
