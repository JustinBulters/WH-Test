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

  function poll() {
    if (busy || document.hidden || !navigator.onLine) return;
    busy = true;
    fetch(url + '?na=' + last, {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
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
  setInterval(poll, 15000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
