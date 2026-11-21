/* Shows new notifications as a pop-up without reloading the page.
 * Asks the server every 20 seconds while the page is visible (a few hundred bytes each time).
 */
(function () {
  var box = document.getElementById('toasts');
  if (!box || !window.fetch) return;
  var last = parseInt(box.getAttribute('data-last'), 10) || 0, url = box.getAttribute('data-poll');
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
        last = Math.max(last, d.last);
        setBadge(d.unread);
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }

  setInterval(poll, 20000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
