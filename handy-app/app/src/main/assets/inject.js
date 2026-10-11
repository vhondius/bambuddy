// "Print with Bambuddy" button for MakerWorld model pages.
//
// The one place to fix when MakerWorld changes: the app injects this file on
// every page load of the MakerWorld tab, and it calls back into the app with
// BambuddyHandy.print(url). The URL includes the selected print profile
// (#profileId-…), which MakerWorld keeps in the address as you switch plates.
(function () {
  if (window.__bambuddyHandy) {
    window.__bambuddyHandy.update();
    return;
  }

  // /models/123…, optionally behind a language prefix: /en/models/123…
  var MODEL_PATH = /^\/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?\/)?models\/\d+/;

  var btn = document.createElement('button');
  btn.id = 'bambuddy-handy-print';
  btn.type = 'button';
  btn.textContent = 'Print with Bambuddy';
  btn.setAttribute(
    'style',
    [
      'position:fixed', 'right:16px', 'bottom:88px', 'z-index:2147483647',
      'padding:12px 18px', 'border:0', 'border-radius:24px',
      // MakerWorld's own primary green and typeface, so it looks native there.
      'background:var(--mui-palette-colorSystem-primary,#00B800)', 'color:#fff',
      'font-weight:600', 'font-size:15px', 'line-height:1', 'font-family:inherit',
      'box-shadow:0 4px 12px rgba(0,0,0,.35)', 'display:none',
    ].join(';')
  );
  btn.addEventListener('click', function (e) {
    e.preventDefault();
    e.stopPropagation();
    window.BambuddyHandy.print(location.href);
  });

  function update() {
    var onModel = MODEL_PATH.test(location.pathname);
    // The site re-renders its body now and then; put the button back.
    if (onModel && !btn.isConnected && document.body) document.body.appendChild(btn);
    btn.style.display = onModel ? 'block' : 'none';
  }

  // ponytail: a 1 s poll follows MakerWorld's in-page navigation and puts
  // back a button a re-render dropped (up to 1 s late); swap for a
  // MutationObserver if it ever shows up in battery use.
  setInterval(update, 1000);

  window.__bambuddyHandy = { update: update };
  update();
})();
