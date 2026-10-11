// Tells the app whether a pull-down at this touch should refresh the page.
//
// The app can only see the WebView's own scroll position. Bambuddy (and
// MakerWorld) scroll inside panels and dialogs, and the G-code preview is a
// canvas you drag to rotate, so "the page is at the top" is not enough: only
// refresh when nothing under the finger is scrolled down and it isn't a canvas
// or a control such as a slider.
(function () {
  if (window.__handyRefreshGuard) return;
  window.__handyRefreshGuard = true;

  function canRefresh(target) {
    if ((window.scrollY || document.documentElement.scrollTop) > 0) return false;
    for (var el = target; el && el.nodeType === 1; el = el.parentElement) {
      // Canvases (the G-code preview) and form controls (its layer
      // sliders) take drags of their own.
      if (/^(CANVAS|INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.scrollTop > 0) return false;
      if (el.getAttribute('role') === 'slider') return false;
    }
    return true;
  }

  document.addEventListener(
    'touchstart',
    function (e) {
      window.HandyRefresh.report(e.touches.length === 1 && canRefresh(e.target));
    },
    { capture: true, passive: true }
  );
  window.HandyRefresh.ready();
})();
