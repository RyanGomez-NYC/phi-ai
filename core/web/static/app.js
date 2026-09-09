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
 *   4. Builders apply themselves (data-autoapply): the cohort builder and
 *      the report's selector bar submit as their controls change, and the
 *      search box once typing pauses.
 *   5. The condition box is an autocomplete (input[data-suggest]): a
 *      listbox of the corpus's own condition names opens under it as the
 *      reader types, and picking one counts that condition exactly. Both
 *      screens carry the SAME box (templates/_condition_box.html), so this
 *      one handler is the behaviour of both. 4's submit-after-typing
 *      stands down from the moment a lookup GOES OUT - not from the moment
 *      the list opens: an answer slower than that 500 ms timer would
 *      otherwise reload the page mid-word, out from under the reader.
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

  /* 4. Builders apply themselves. One handler for every form[data-autoapply]:
     a discrete control (select, radio, checkbox) submits 120 ms after it
     changes; the search box submits 500 ms after the last keystroke once it
     holds two characters or has been emptied, and first un-picks an exact
     choice (a hidden input marked data-autoapply-reset), because a new term
     is a new search; Enter submits at once. Any other text input never
     submits itself - it fires change on blur, mid-form. */
  Array.prototype.forEach.call(document.querySelectorAll('form[data-autoapply]'), function (form) {
    var timer = null;
    function submit() {
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.submit();
    }
    function later(delay) {
      clearTimeout(timer);
      timer = setTimeout(submit, delay);
    }
    form.addEventListener('change', function (ev) {
      var el = ev.target;
      if (!el || !el.name) return;
      if (el.tagName !== 'SELECT' && el.type !== 'radio' && el.type !== 'checkbox') return;
      later(120);
    });
    form.addEventListener('input', function (ev) {
      var el = ev.target;
      if (!el || el.type !== 'search') return;
      Array.prototype.forEach.call(form.querySelectorAll('input[type=hidden][data-autoapply-reset]'), function (h) {
        h.value = '';
      });
      // While the suggestion panel (5) is open the reader is choosing from
      // a list; reloading the page under it would take the list away.
      if (form.getAttribute('data-suggest-open') === '1') { clearTimeout(timer); return; }
      var value = el.value.trim();
      if (value.length >= 2 || value.length === 0) later(500);
      else clearTimeout(timer);
    });
    form.addEventListener('keydown', function (ev) {
      var el = ev.target;
      if (!el || el.type !== 'search' || ev.key !== 'Enter') return;
      ev.preventDefault();
      clearTimeout(timer);
      submit();
    });
    // 5 tells 4 when it goes looking for a list and when that list closes.
    // The first cancels a submit already scheduled - it fires when the
    // lookup goes out, not when the panel opens, so a slow answer cannot
    // be overtaken by the reload. Closing because there was nothing to
    // suggest hands the typing back (the server then states what the term
    // resolved to); Escape and a pick leave the page as it is.
    form.addEventListener('phi-suggest-open', function () { clearTimeout(timer); });
    form.addEventListener('phi-suggest-close', function (ev) {
      if (ev && ev.detail && ev.detail.resume) later(500);
      else clearTimeout(timer);
    });
    form.addEventListener('submit', function () { clearTimeout(timer); });
  });

  /* 5. The condition box is an AUTOCOMPLETE. One handler for every
     input[data-suggest]: 150 ms after typing pauses, and from the second
     character, it asks the endpoint named by the attribute for the KNOWN
     condition names that match (the previous request is aborted), and
     fills the listbox named by aria-controls. The rows are clones of the
     panel's own [data-suggest-row] prototype, so the markup lives in the
     template. ArrowDown/ArrowUp move, Enter picks the highlighted row (and
     submits the typed text when none is), Escape closes, a click picks,
     blur closes after the click has landed. Picking sets the condition
     exactly - the hidden [data-suggest-exact] input - and counts at once;
     typing again un-picks it. */
  Array.prototype.forEach.call(document.querySelectorAll('input[data-suggest]'), function (input) {
    var panel = document.getElementById(input.getAttribute('aria-controls') || '');
    var proto = panel && panel.querySelector('[data-suggest-row]');
    if (!panel || !proto) return;
    var endpoint = input.getAttribute('data-suggest');
    var form = input.form;
    var timer = null, blurTimer = null, watchdog = null, controller = null;
    var items = [], active = -1;

    function tell(name, resume) {
      if (!form) return;
      var ev;
      try {
        ev = new CustomEvent(name, { detail: { resume: !!resume } });
      } catch (e) {                                   /* older engines */
        ev = document.createEvent('CustomEvent');
        ev.initCustomEvent(name, false, false, { resume: !!resume });
      }
      form.dispatchEvent(ev);
    }
    function options() {
      return panel.querySelectorAll('[role=option]:not([data-suggest-row])');
    }
    function unpick() {
      if (!form) return;
      Array.prototype.forEach.call(form.querySelectorAll('input[type=hidden][data-suggest-exact]'), function (h) {
        h.value = '';
      });
    }
    function close(resume) {
      panel.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.setAttribute('aria-activedescendant', '');
      active = -1;
      if (form) form.setAttribute('data-suggest-open', '0');
      tell('phi-suggest-close', resume);
    }
    /* 4 stands down the moment the lookup GOES OUT, not when the panel
       finally opens: on a slow answer its 500 ms submit would otherwise
       land first and take the list away as it arrives. */
    function pending() {
      if (form) form.setAttribute('data-suggest-open', '1');
      tell('phi-suggest-open', false);
    }
    function open() {
      panel.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      pending();
    }
    function highlight(index) {
      var rows = options();
      active = index;
      Array.prototype.forEach.call(rows, function (row, i) {
        var on = i === index;
        row.setAttribute('aria-selected', on ? 'true' : 'false');
        if (on) {
          row.className = 'cr-suggest-row on';
          input.setAttribute('aria-activedescendant', row.id);
          if (row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
        } else {
          row.className = 'cr-suggest-row';
        }
      });
      if (index < 0) input.setAttribute('aria-activedescendant', '');
    }
    function pick(index) {
      var item = items[index];
      if (!item) return;
      input.value = item.name;
      if (form) {
        Array.prototype.forEach.call(form.querySelectorAll('input[type=hidden][data-suggest-exact]'), function (h) {
          h.value = '1';
        });
      }
      close(false);
      if (!form) return;
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.submit();
    }
    function render(data) {
      var list = (data && data.conditions) || [];
      var note = (data && (data.withheld || data.note)) || '';
      items = list;
      Array.prototype.forEach.call(panel.querySelectorAll('li:not([data-suggest-row])'), function (li) {
        li.parentNode.removeChild(li);
      });
      if (!list.length && !note) { close(true); return; }
      list.forEach(function (item, i) {
        var row = proto.cloneNode(true);
        row.removeAttribute('data-suggest-row');
        row.hidden = false;
        row.id = panel.id + '-option-' + i;
        row.querySelector('.nm').textContent = item.name;
        row.querySelector('.n').textContent = item.label || '';
        row.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
        row.addEventListener('click', function () { pick(i); });
        panel.appendChild(row);
      });
      if (note) {
        var line = document.createElement('li');
        line.className = 'cr-suggest-note';
        line.setAttribute('role', 'presentation');
        line.textContent = note;
        panel.appendChild(line);
      }
      if (!list.length) {
        // One line and nothing to choose from - a deployment that withholds
        // its names, or a layer that could not answer. It is shown, but the
        // reader is not held: 4 goes back to applying the typed text, which
        // is what the page did before there was a panel at all.
        panel.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        if (form) form.setAttribute('data-suggest-open', '0');
        tell('phi-suggest-close', true);
        return;
      }
      highlight(-1);
      open();
    }
    function ask(term) {
      if (controller) controller.abort();
      controller = (typeof AbortController === 'function') ? new AbortController() : null;
      var init = { credentials: 'same-origin', headers: { 'Accept': 'application/json' } };
      if (controller) init.signal = controller.signal;
      pending();
      // A lookup that never answers must not leave the typing suspended:
      // it is given four seconds, then the form goes back to applying
      // itself and the server answers the typed text instead.
      clearTimeout(watchdog);
      watchdog = setTimeout(function () {
        if (controller) controller.abort();
        close(true);
      }, 4000);
      fetch(endpoint + '?q=' + encodeURIComponent(term), init).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (data) {
        clearTimeout(watchdog);
        if (data && data.q !== undefined && data.q !== input.value.trim()) return;  // a stale answer
        if (data) render(data); else close(true);
      }).catch(function (err) {
        if (err && err.name === 'AbortError') return;                 // a newer keystroke won
        clearTimeout(watchdog);
        close(true);
      });
    }

    input.addEventListener('input', function () {
      unpick();
      clearTimeout(timer);
      var value = input.value.trim();
      if (value.length < 2) {
        clearTimeout(watchdog);
        if (controller) controller.abort();
        close(false);
        return;
      }
      timer = setTimeout(function () { ask(value); }, 150);
    });
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') {
        clearTimeout(timer);
        clearTimeout(watchdog);
        if (controller) controller.abort();
        if (!panel.hidden) { ev.preventDefault(); ev.stopPropagation(); }
        close(false);
        return;
      }
      if (panel.hidden || !items.length) return;      // 4 keeps Enter when nothing is open
      if (ev.key === 'ArrowDown') {
        ev.preventDefault(); ev.stopPropagation();
        highlight(active + 1 >= items.length ? 0 : active + 1);
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault(); ev.stopPropagation();
        highlight(active <= 0 ? items.length - 1 : active - 1);
      } else if (ev.key === 'Enter') {
        if (active < 0) { close(false); return; }     // 4 submits the typed text
        ev.preventDefault(); ev.stopPropagation();
        pick(active);
      }
    });
    input.addEventListener('blur', function () {
      blurTimer = setTimeout(function () { close(false); }, 150);
    });
    input.addEventListener('focus', function () { clearTimeout(blurTimer); });
  });
})();
