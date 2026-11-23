/* Offline check-in for checkpoint staff.
 *
 * Every Inchecken/Uitchecken tap is sent in the background with the moment it was
 * tapped. Without signal the tap is kept on this phone and sent as soon as there
 * is a connection again; the server then stores the original tap time.
 *
 * A tap only leaves the queue when the server really handled it. Server errors,
 * a restart during an update, an expired login or a stuck connection keep it waiting.
 */
(function () {
  var KEY = 'wh-offline-queue', FAILED = 'wh-offline-failed';
  var STAMP = /\/checkpoint\/\d+\/groep\/\d+\/(in|uit)$/;
  var TIMEOUT = 15000;  // ms; on a weak signal a request can hang without ever failing
  var sending = false, needLogin = false;

  function load(k) { try { return JSON.parse(localStorage.getItem(k || KEY)) || []; } catch (e) { return []; } }
  function store(q, k) { try { localStorage.setItem(k || KEY, JSON.stringify(q)); } catch (e) {} }
  function hhmm(iso) { var d = new Date(iso); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); }

  function show(id, cls, text) {
    var el = document.getElementById(id);
    if (!text) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement('div');
      el.id = id; el.className = 'flash ' + cls; el.setAttribute('role', 'status');
      var main = document.querySelector('main'); main.insertBefore(el, main.firstChild);
    }
    el.textContent = text;
  }

  function banner() {
    var q = load(), failed = load(FAILED);
    show('offline-banner', 'offline', !q.length ? '' :
      (q.length === 1 ? '1 in/uitcheck wacht' : q.length + ' in/uitchecks wachten') +
      (needLogin ? '. Log opnieuw in, dan worden ze verstuurd met de tijd van het tikken.'
                 : ' op verbinding. Ze worden vanzelf verstuurd met de tijd van het tikken. Sluit de site niet af.'));
    show('offline-failed', 'error', !failed.length ? '' :
      'Niet opgeslagen: ' + failed.map(function (f) { return f.text; }).join('; ') +
      '. Doe deze opnieuw of geef ze door aan de beheerder.');
    var el = document.getElementById('offline-failed');
    if (el && !el.querySelector('button')) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'ghost'; b.textContent = 'Gezien';
      b.addEventListener('click', function () { store([], FAILED); banner(); });
      el.appendChild(document.createTextNode(' ')); el.appendChild(b);
    }
  }

  // fetch with a time limit, so a stuck request counts as "no connection".
  function fetchT(url, opts) {
    var ctl = window.AbortController ? new AbortController() : null, timer;
    if (ctl) { opts.signal = ctl.signal; timer = setTimeout(function () { ctl.abort(); }, TIMEOUT); }
    return fetch(url, opts).then(function (r) { clearTimeout(timer); return r; },
                                 function (e) { clearTimeout(timer); throw e; });
  }

  // The form token of the current login. A queued tap may come from an earlier login,
  // so its own token can be out of date.
  function token() {
    return fetchT('/csrf?ajax=1', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (r.status === 401) { needLogin = true; return null; }
      if (!r.ok) throw new Error('csrf ' + r.status);
      needLogin = false;
      return r.json().then(function (d) { return d.csrf; });
    });
  }

  function post(item, csrf) {
    var f = new FormData();
    f.append('csrf', csrf); f.append('at', item.at); f.append('ajax', '1');
    return fetchT(item.url, {method: 'POST', body: f, credentials: 'same-origin', cache: 'no-store'});
  }

  function describe(item) {
    var m = item.url.match(/\/checkpoint\/(\d+)\/groep\/(\d+)\//);
    return (item.action === 'in' ? 'incheck' : 'uitcheck') + ' ' + hhmm(item.at) +
      (item.group ? ' groep ' + item.group : (m ? ' (groep-id ' + m[2] + ')' : ''));
  }

  // What to do with a tap after the server answered.
  //   'done'  : stored (or it was already stored)
  //   'retry' : try again later (server error, restart during an update, not logged in)
  //   'fail'  : the server will not accept it (e.g. group dropped out, no access): tell the user
  function outcome(r) {
    if (r.status === 401) { needLogin = true; return 'retry'; }
    if (r.ok) return 'done';
    if (r.status === 400 || r.status === 403 || r.status === 404 || r.status === 409) return 'fail';
    return 'retry';
  }

  // The server's reason ("Groep 101 is uitgevallen."), or the error code.
  function reason(r) {
    return r.json().then(function (d) { return d.message || ('fout ' + r.status); },
                         function () { return 'fout ' + r.status; });
  }

  // Send waiting taps in order. Stops at the first problem and retries later.
  function flush() {
    if (sending || !load().length) return Promise.resolve();
    sending = true;
    var sent = 0;
    function next(csrf) {
      var q = load();
      if (!q.length) return Promise.resolve();
      return post(q[0], csrf).then(function (r) {
        var what = outcome(r);
        if (what === 'retry') return;
        var saved = what === 'done' ? Promise.resolve() : reason(r).then(function (why) {
          store(load(FAILED).concat([{text: describe(q[0]) + ' (' + why + ')'}]), FAILED);
        });
        return saved.then(function () {
          store(load().slice(1)); sent++;
          return next(csrf);
        });
      });
    }
    return token().then(function (csrf) { if (csrf) return next(csrf); })
      .catch(function () {})
      .then(function () {
        sending = false; banner();
        if (sent && !load().length && !load(FAILED).length) location.reload();
      });
  }

  function markTapped(form, item) {
    var btn = form.querySelector('button');
    var tag = document.createElement('span');
    tag.className = 'tag in';
    tag.textContent = (item.action === 'in' ? 'in ' : 'uit ') + hhmm(item.at) + ' · wacht';
    form.parentNode.insertBefore(tag, form);
    if (item.action === 'in' && btn) {
      // The group can be checked out later, also without signal.
      form.action = form.action.replace(/\/in$/, '/uit');
      btn.textContent = 'Uitchecken'; btn.classList.add('alt'); btn.disabled = false;
    } else if (btn) {
      btn.remove();
    }
  }

  function queue(form, item) { store(load().concat([item])); markTapped(form, item); banner(); }

  document.addEventListener('submit', function (e) {
    var form = e.target, m = form.action && form.action.match(STAMP);
    if (!m) return;
    e.preventDefault();
    var csrf = form.querySelector('[name=csrf]');
    var item = {url: form.action, action: m[1], at: new Date().toISOString(),
                group: form.getAttribute('data-group') || ''};
    var btn = form.querySelector('button');
    if (btn) btn.disabled = true;
    // Earlier taps go first, so an "uit" never overtakes its "in".
    if (load().length) { queue(form, item); flush(); return; }
    post(item, csrf ? csrf.value : '').then(function (r) {
      var what = outcome(r);
      if (what === 'retry') { queue(form, item); return; }
      if (what === 'fail') {
        // Usually an out-of-date page (for example after logging in again): queue it so it
        // is sent with a fresh token, instead of losing the tap.
        if (r.status === 400) { queue(form, item); flush(); return; }
        return reason(r).then(function (why) { alert('Niet opgeslagen: ' + why); location.reload(); });
      }
      location.reload();
    }).catch(function () { queue(form, item); });
  }, true);

  window.addEventListener('online', flush);
  setInterval(flush, 20000);
  banner();
  flush();

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(function () {});
  }
})();
