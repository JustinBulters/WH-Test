/* Offline check-in for checkpoint staff.
 *
 * Every Inchecken/Uitchecken tap is sent in the background with the moment it was
 * tapped. Without signal the tap is kept on this phone and sent as soon as there
 * is a connection again; the server then stores the original tap time.
 */
(function () {
  var KEY = 'wh-offline-queue';
  var STAMP = /\/checkpoint\/\d+\/groep\/\d+\/(in|uit)$/;
  var sending = false;

  function load() { try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch (e) { return []; } }
  function store(q) { try { localStorage.setItem(KEY, JSON.stringify(q)); } catch (e) {} }
  function hhmm(iso) { var d = new Date(iso); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); }

  function banner() {
    var q = load(), el = document.getElementById('offline-banner');
    if (!q.length) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement('div');
      el.id = 'offline-banner'; el.className = 'flash offline'; el.setAttribute('role', 'status');
      var main = document.querySelector('main'); main.insertBefore(el, main.firstChild);
    }
    el.textContent = (q.length === 1 ? '1 in/uitcheck wacht' : q.length + ' in/uitchecks wachten') +
      ' op verbinding. Ze worden vanzelf verstuurd met de tijd van het tikken. Sluit de site niet af.';
  }

  function post(item) {
    var f = new FormData();
    f.append('csrf', item.csrf); f.append('at', item.at); f.append('ajax', '1');
    return fetch(item.url, {method: 'POST', body: f, credentials: 'same-origin', cache: 'no-store'});
  }

  // Send waiting taps in order. Stops at the first network error and retries later.
  function flush() {
    if (sending) return Promise.resolve();
    var q = load();
    if (!q.length) return Promise.resolve();
    sending = true;
    var sent = 0;
    function next() {
      var q = load();
      if (!q.length) return Promise.resolve();
      return post(q[0]).then(function (r) {
        // Sent: also when the server refused it (e.g. already checked in), so it never blocks the queue.
        if (r.status === 400 || r.status === 403) console.warn('Tap refused', r.status, q[0]);
        store(load().slice(1)); sent++;
        return next();
      });
    }
    return next().catch(function () {}).then(function () {
      sending = false; banner();
      if (sent && !load().length) location.reload();
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

  document.addEventListener('submit', function (e) {
    var form = e.target, m = form.action && form.action.match(STAMP);
    if (!m) return;
    e.preventDefault();
    var csrf = form.querySelector('[name=csrf]');
    var item = {url: form.action, action: m[1], at: new Date().toISOString(), csrf: csrf ? csrf.value : ''};
    var btn = form.querySelector('button');
    if (btn) btn.disabled = true;
    // Earlier taps go first, so an "uit" never overtakes its "in".
    if (load().length) { store(load().concat([item])); markTapped(form, item); banner(); flush(); return; }
    post(item).then(function (r) {
      if (r.ok || r.status === 409) { location.reload(); return; }
      location.reload();
    }).catch(function () {
      store(load().concat([item])); markTapped(form, item); banner();
    });
  }, true);

  window.addEventListener('online', flush);
  setInterval(flush, 20000);
  banner();
  flush();

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(function () {});
  }
})();
