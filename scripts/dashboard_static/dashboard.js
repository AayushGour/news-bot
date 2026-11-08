/* Mission control — shell behaviour. Vanilla, no dependencies. Read once by scripts/dashboard.py
   and inlined at the end of <body>. Every feature degrades: without JS the nav is a plain block,
   search is always visible and the page simply does not auto-refresh (noscript meta refresh does). */
(function () {
  'use strict';
  var d = document, root = d.documentElement, body = d.body;
  var $ = function (s, c) { return (c || d).querySelector(s); };
  var $$ = function (s, c) { return Array.prototype.slice.call((c || d).querySelectorAll(s)); };
  var narrow = function () { return window.matchMedia('(max-width:1023px)').matches; };
  var phone = function () { return window.matchMedia('(max-width:640px)').matches; };

  /* ---- drawer (< 1024px): focus trapped while open, Esc / scrim / close dismiss ---- */
  var side = $('#side'), scrim = $('#scrim'), menuBtn = $('#menu-btn');
  function focusables(el) {
    return $$('a[href],button:not([disabled]),input:not([type=hidden]),select,textarea,[tabindex="0"]', el)
      .filter(function (n) { return n.offsetParent !== null; });
  }
  /* Everything outside the drawer goes inert while it is open, so the page behind can neither be
     tabbed into nor read by assistive tech (the drawer is a modal). */
  function setBehindInert(on) {
    $$('main, header.top, .skip').forEach(function (n) { n.inert = on; });
  }
  function openDrawer() {
    if (!side) return;
    side.classList.add('open'); scrim.hidden = false; body.classList.add('overlay-open');
    menuBtn.setAttribute('aria-expanded', 'true');
    setBehindInert(true);
    /* The CSS shows the drawer with no visibility delay on open, so it can take focus now; the
       frame callback is a retry for a browser that has not applied the style yet. */
    var f = focusables(side); if (f.length) f[0].focus();
    if (window.requestAnimationFrame) window.requestAnimationFrame(function () {
      if (side.classList.contains('open') && !side.contains(d.activeElement)) {
        var g = focusables(side); if (g.length) g[0].focus();
      }
    });
  }
  function closeDrawer(restore) {
    if (!side || !side.classList.contains('open')) return;
    side.classList.remove('open'); scrim.hidden = true; body.classList.remove('overlay-open');
    menuBtn.setAttribute('aria-expanded', 'false');
    setBehindInert(false);
    if (restore) menuBtn.focus();
  }
  if (menuBtn && side) {
    menuBtn.addEventListener('click', openDrawer);
    scrim.addEventListener('click', function () { closeDrawer(true); });
    var closeBtn = $('#side-close');
    if (closeBtn) closeBtn.addEventListener('click', function () { closeDrawer(true); });
    /* Trap Tab at the document level: wrap at the ends, and pull focus back in if it ever got out. */
    d.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab' || !side.classList.contains('open') || !narrow()) return;
      var f = focusables(side); if (!f.length) return;
      var first = f[0], last = f[f.length - 1], at = d.activeElement;
      if (!side.contains(at)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
      else if (e.shiftKey && at === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && at === last) { e.preventDefault(); first.focus(); }
    });
    window.addEventListener('resize', function () { if (!narrow()) closeDrawer(false); });
  }

  /* ---- search toggle (<= 640px) ---- */
  var searchForm = $('#search'), searchBtn = $('#search-btn'), searchInput = $('#q');
  function setSearch(open) {
    if (!searchForm || !searchBtn) return;
    searchForm.classList.toggle('open', open);
    searchBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && searchInput) searchInput.focus();
  }
  if (searchBtn) searchBtn.addEventListener('click', function () {
    setSearch(!searchForm.classList.contains('open'));
  });

  /* ---- sidebar rail (>= 1024px), remembered ---- */
  var collapse = $('#collapse-btn');
  function syncRail() {
    var on = root.classList.contains('rail');
    collapse.setAttribute('aria-pressed', on ? 'true' : 'false');
    collapse.setAttribute('aria-label', on ? 'Expand sidebar' : 'Collapse sidebar');
    collapse.title = on ? 'Expand sidebar' : 'Collapse sidebar';
  }
  if (collapse) {
    syncRail();
    collapse.addEventListener('click', function () {
      root.classList.toggle('rail');
      try { localStorage.setItem('mc-rail', root.classList.contains('rail') ? '1' : '0'); } catch (e) {}
      syncRail();
    });
  }

  /* ---- keys: Esc closes overlays, "/" focuses search, Cmd/Ctrl+Enter submits a textarea ---- */
  function typing(el) {
    return !!el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || (!!el && el.isContentEditable);
  }
  d.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (side && side.classList.contains('open')) { closeDrawer(true); return; }
      if (searchForm && searchForm.classList.contains('open') && phone()) {
        setSearch(false); if (searchBtn) searchBtn.focus();
      }
    } else if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !typing(d.activeElement)
               && searchInput) {
      e.preventDefault();
      if (phone()) setSearch(true); else searchInput.focus();
    } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && d.activeElement
               && d.activeElement.tagName === 'TEXTAREA' && d.activeElement.form) {
      e.preventDefault();
      if (d.activeElement.form.requestSubmit) d.activeElement.form.requestSubmit();
    }
  });

  /* ---- forms: data-confirm prompt, then a double-submit guard with a busy label ---- */
  d.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f || f.tagName !== 'FORM' || (f.method || '').toLowerCase() !== 'post') return;
    if (f.hasAttribute('data-busy')) { e.preventDefault(); return; }
    var msg = f.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    f.setAttribute('data-busy', '');
    var b = e.submitter || $('button', f);
    if (b) { b.setAttribute('data-busy', ''); busySaved.push([b, b.innerHTML]); b.textContent = 'Working…'; }
  });
  var busySaved = [];   // [button, original innerHTML] so icons survive the label swap
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;   // back/forward cache: undo the busy state
    $$('[data-busy]').forEach(function (n) {
      n.removeAttribute('data-busy');
    });
    busySaved.forEach(function (p) { p[0].innerHTML = p[1]; });
    busySaved = [];
  });

  /* ---- toast: dismiss button; success toasts leave after 6s ---- */
  $$('.toast').forEach(function (t) {
    var x = $('button', t);
    if (x) x.addEventListener('click', function () { t.remove(); });
    if (t.classList.contains('ok')) setTimeout(function () { t.remove(); }, 6000);
  });

  /* ---- refresh policy: reload only when it cannot cost the operator anything ---- */
  var every = parseInt(body.getAttribute('data-refresh') || '0', 10);
  if (every > 0) {
    var live = $('#live'), liveText = $('#live-text'), loaded = Date.now();
    var draft = function () {      // a field the operator has typed in (or is typing in)
      if (typing(d.activeElement)) return true;
      return $$('textarea,input,select').some(function (n) {
        if (n.type === 'hidden' || n.type === 'checkbox' || n.type === 'radio') return false;
        if (n.tagName === 'SELECT') {   // selects have no defaultValue: compare the chosen option
          var was = 0, opts = $$('option', n);   // no defaultSelected option = the first one
          opts.forEach(function (o, i) { if (o.defaultSelected) was = i; });
          return n.selectedIndex >= 0 && n.selectedIndex !== was;   // no options: nothing to have changed
        }
        return n.value !== n.defaultValue;
      });
    };
    var overlay = function () {
      return !!$('dialog[open]') || body.classList.contains('overlay-open');
    };
    var paused = function () { return draft() ? 'typing' : (overlay() ? 'open' : ''); };
    var say = function () {
      if (!live) return;
      var why = paused();
      live.setAttribute('data-state', why ? 'paused' : 'live');
      liveText.textContent = why === 'typing' ? 'Paused while you type'
        : why ? 'Paused while a panel is open'
        : 'Live · updated ' + Math.round((Date.now() - loaded) / 1000) + 's ago';
    };
    say();
    setInterval(say, 1000);
    setInterval(function () { if (!d.hidden && !paused()) window.location.reload(); }, every);
  }

  /* ==== UX-2 ==== */
  /* ==== /UX-2 ==== */
  /* ==== UX-3 ==== */
  /* ==== /UX-3 ==== */
  /* ==== UX-4 ==== slide viewer: a click on a gallery tile opens the native <dialog> */
  var viewer = $('#slide-viewer');
  if (viewer && typeof viewer.showModal === 'function') {
    var tiles = $$('.gallery .slide'), at = 0, opener = null;
    var vImg = $('.viewer-img', viewer), vCount = $('[data-count]', viewer);
    var show = function (n) {
      at = (n + tiles.length) % tiles.length;
      var img = $('img', tiles[at]);
      vImg.src = tiles[at].getAttribute('href');
      vImg.alt = img ? img.alt : '';
      vCount.textContent = 'Slide ' + (at + 1) + ' of ' + tiles.length;
    };
    tiles.forEach(function (t, i) {
      t.removeAttribute('target');
      t.addEventListener('click', function (e) {
        e.preventDefault(); opener = t; show(i); viewer.showModal();
      });
    });
    $('.viewer-prev', viewer).addEventListener('click', function () { show(at - 1); });
    $('.viewer-next', viewer).addEventListener('click', function () { show(at + 1); });
    viewer.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); show(at - 1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); show(at + 1); }
    });
    viewer.addEventListener('click', function (e) { if (e.target === viewer) viewer.close(); });
    viewer.addEventListener('close', function () { if (opener) opener.focus(); });
  }
  /* decision bar: publish its real height as --decide-h (room under it, scroll-padding), and make
     "Ask for changes" land in the note box instead of only scrolling */
  var bar = $('.decide');
  if (bar) {
    var measure = function () { root.style.setProperty('--decide-h', bar.offsetHeight + 'px'); };
    measure();
    if (window.ResizeObserver) new ResizeObserver(measure).observe(bar);
    else window.addEventListener('resize', measure);
    $$('a[href="#changes"]').forEach(function (a) {
      a.addEventListener('click', function (e) {
        var box = $('#changes textarea');
        if (!box) return;
        e.preventDefault();
        box.scrollIntoView({ block: 'center' });
        box.focus({ preventScroll: true });
      });
    });
  }
  /* ==== /UX-4 ==== */
  /* ==== UX-5 ==== */
  /* ==== /UX-5 ==== */
  /* ==== UX-6 ==== once the toast is on the page, drop ?notice= so a reload or the live refresh does not repeat it */
  if (/[?&]notice=/.test(window.location.search) && window.history && history.replaceState) {
    var here = new URL(window.location.href);
    here.searchParams.delete('notice');
    history.replaceState(null, '', here.pathname + here.search + here.hash);
  }
  /* ==== /UX-6 ==== */
  /* ==== UX-7 ==== */
  /* ==== /UX-7 ==== */
})();
