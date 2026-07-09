/*!
 * Chart.js dynamic bootstrap
 * Loads Chart.js from CDN if not already available.
 * Attachs to window.chartReady (Promise) so downstream code can await it.
 */
(function() {
  if (window.Chart) {
    window.chartReady = Promise.resolve();
    return;
  }
  window.chartReady = new Promise(function(resolve) {
    var s = document.createElement('script');
    s.src = '/static/js/chart.min.js';
    s.onload = resolve;
    s.onerror = resolve;
    document.head.appendChild(s);
  });
})();
