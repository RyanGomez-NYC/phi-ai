/* Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE). */
/*
 * The one script this interface serves. It is self-hosted and named in the
 * Content-Security-Policy (script-src 'self'): no inline script, no eval, no
 * third-party origin. Everything here is an ENHANCEMENT of a form that
 * already works without it - the server-side contract is the whole
 * mechanism, and this file only makes it feel immediate:
 *
 *   1. Busy buttons (data-busy): the pressed button fills solid, its label
 *      becomes the verb in progress (data-busy-label), a spinner sits beside
 *      it and a bar sweeps along the top of the page until the response
 *      lands. The back/forward cache puts it all back to rest.
 *   2. Lanes apply themselves: a tick on the wiring form posts lane_apply=1,
 *      which the server answers by landing on #wired without opening the
 *      next step (the reader is still wiring).
 *   3. Scope selectors apply themselves: a discrete control (select, radio,
 *      checkbox) submits the scope form; free text waits for Confirm.
 */
(function () {
  'use strict';

  /* 1. Busy buttons. The button is disabled AFTER the event-loop turn, or the
     browser drops its name/value from the submission and silently changes
     what the form does. ev.submitter is the element that actually submitted,
     which need not be INSIDE the form (form="..."). */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || form.tagName !== 'FORM') return;
    var btn = (ev.submitter && ev.submitter.matches && ev.submitter.matches('[data-busy]')) ? ev.submitter
            : form.querySelector('button[data-busy], input[type=submit][data-busy]');
    if (!btn || btn.dataset.busyOn === '1') return;
    btn.dataset.busyOn = '1';
    if (btn.tagName === 'BUTTON') {
      btn.dataset.busyText = btn.textContent;
      var label = btn.getAttribute('data-busy-label') || btn.textContent.trim();
      btn.textContent = label.replace(/[.\u2026\s]+$/, '') + '\u2026';
      if (!btn.querySelector('.btn-spin')) {
        var sp = document.createElement('span');
        sp.className = 'btn-spin';
        sp.setAttribute('aria-hidden', 'true');
        btn.appendChild(sp);
      }
    }
    btn.setAttribute('aria-busy', 'true');
    form.setAttribute('aria-busy', 'true');
    document.documentElement.classList.add('is-busy');
    if (!document.querySelector('.busy-bar')) {
      var bar = document.createElement('div');
      bar.className = 'busy-bar';
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-label', 'Working');
      document.body.appendChild(bar);
    }
    setTimeout(function () { btn.disabled = true; }, 0);
  }, true);
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) return;
    document.documentElement.classList.remove('is-busy');
    var bar = document.querySelector('.busy-bar');
    if (bar) bar.parentNode.removeChild(bar);
    Array.prototype.forEach.call(document.querySelectorAll('[data-busy-on="1"]'), function (b) {
      b.dataset.busyOn = ''; b.disabled = false; b.removeAttribute('aria-busy');
      if (b.dataset.busyText) b.textContent = b.dataset.busyText;
    });
    Array.prototype.forEach.call(document.querySelectorAll('form[aria-busy]'), function (f) {
      f.removeAttribute('aria-busy');
    });
  });

  /* 2. Lanes apply themselves. */
  var wiring = document.getElementById('orc-select-form');
  if (wiring) {
    wiring.addEventListener('change', function (ev) {
      var el = ev.target;
      if (!el || el.type !== 'checkbox') return;
      if (!wiring.querySelector('input[name="lane_apply"]')) {
        var h = document.createElement('input');
        h.type = 'hidden'; h.name = 'lane_apply'; h.value = '1';
        wiring.appendChild(h);
      }
      if (typeof wiring.requestSubmit === 'function') wiring.requestSubmit();
      else wiring.submit();
    });
  }

  /* 3. Scope selectors apply themselves. Text inputs fire change on blur,
     which would submit while the reader is still tabbing through the
     segment; only the discrete controls apply themselves. */
  var scope = document.querySelector('form[data-oc-autoscope]');
  if (scope) {
    var t = null;
    scope.addEventListener('change', function (ev) {
      var el = ev.target;
      if (!el || !el.name) return;
      if (el.tagName !== 'SELECT' && el.type !== 'radio' && el.type !== 'checkbox') return;
      clearTimeout(t);
      t = setTimeout(function () {
        if (typeof scope.requestSubmit === 'function') scope.requestSubmit();
        else scope.submit();
      }, 120);
    });
  }
})();
