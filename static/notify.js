/* Shows new notifications as a pop-up without reloading the page.
 * Asks the server when a page opens and every 15 seconds while the page is visible (a few hundred bytes each time).
 */
(function () {
  var box = document.getElementById('toasts');
  if (!box || !window.fetch) return;
  // Remember (per account, on this device) the newest notification that already popped up,
  // so unread ones also pop up after a page load, but each one only once.
  var key = 'wh-toast-last-' + box.getAttribute('data-user'), url = box.getAttribute('data-poll');
  var last = 0;
  try { last = parseInt(localStorage.getItem(key), 10) || 0; } catch (e) {}
  function remember(n) { last = Math.max(last, n); try { localStorage.setItem(key, String(last)); } catch (e) {} }
  var bell = document.querySelector('.bell'), busy = false;

  function setBadge(n) {
    if (!bell) return;
    var b = bell.querySelector('.badge');
    if (!n) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement('span'); b.className = 'badge'; bell.appendChild(b); }
    b.textContent = n;
  }

  function toast(item) {
    var t = document.createElement('div');
    t.className = 'toast';
    var a = document.createElement('a');
    a.href = item.url; a.textContent = item.text;
    var x = document.createElement('button');
    x.type = 'button'; x.className = 'ghost'; x.setAttribute('aria-label', 'Sluiten'); x.textContent = '×';
    x.addEventListener('click', function () { t.remove(); });
    t.appendChild(a); t.appendChild(x);
    box.appendChild(t);
    setTimeout(function () { t.classList.add('fade'); setTimeout(function () { t.remove(); }, 400); }, 15000);
  }

  var timer = null;
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  // fetch with a time limit, so a stuck poll does not freeze all later polls.
  function fetchT(u, opts) {
    var ctl = window.AbortController ? new AbortController() : null, t;
    if (ctl) { opts.signal = ctl.signal; t = setTimeout(function () { ctl.abort(); }, 12000); }
    return fetch(u, opts).then(function (r) { clearTimeout(t); return r; },
                              function (e) { clearTimeout(t); throw e; });
  }

  function poll() {
    if (busy || document.hidden || !navigator.onLine) return;
    busy = true;
    fetchT(url + '?na=' + last + '&ajax=1', {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) {
        // Session ended: the server asks us to log in again. Stop polling instead of looping.
        if (r.status === 401) { stop(); return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (!d) return;
        d.items.forEach(toast);
        remember(d.last);
        setBadge(d.unread);
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }

  poll();
  timer = setInterval(poll, 15000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
