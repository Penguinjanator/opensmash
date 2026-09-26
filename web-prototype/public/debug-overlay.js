// On-device diagnostics for layout bugs we cannot reproduce locally. Visit
// any page with ?debug=1 to get an on-screen readout (errors, viewport, roster
// layout, what sits on top of the grid) that a player can screenshot or copy.
// Loaded as a classic script ahead of the app bundle so it still reports when
// the bundle itself fails.
(function () {
  var params;
  try { params = new URLSearchParams(location.search); } catch (e) { return; }
  if (params.get('debug') !== '1') return;

  var errors = [];
  function record(kind, message) {
    errors.push(kind + ': ' + String(message).slice(0, 300));
    if (errors.length > 12) errors.shift();
  }
  window.addEventListener('error', function (event) {
    var where = event.filename ? ' @' + event.filename.split('/').pop() + ':' + event.lineno : '';
    record('error', (event.message || (event.target && event.target.src) || 'resource failed') + where);
  }, true);
  window.addEventListener('unhandledrejection', function (event) {
    var reason = event.reason;
    record('rejection', reason && (reason.stack || reason.message) || reason);
  });
  ['error', 'warn'].forEach(function (level) {
    var original = console[level];
    console[level] = function () {
      try {
        record('console.' + level, Array.prototype.map.call(arguments, function (arg) {
          return arg && arg.message ? arg.message : typeof arg === 'object' ? JSON.stringify(arg) : String(arg);
        }).join(' '));
      } catch (e) { /* never break the page for diagnostics */ }
      return original.apply(console, arguments);
    };
  });

  function rect(el) {
    if (!el) return 'missing';
    var r = el.getBoundingClientRect();
    return Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height);
  }
  function describe(el) {
    var parts = [];
    for (var i = 0; el && i < 3; i++, el = el.parentElement) {
      var name = el.tagName.toLowerCase();
      if (el.id) name += '#' + el.id;
      if (typeof el.className === 'string' && el.className) name += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
      parts.push(name);
    }
    return parts.join(' < ') || 'none';
  }

  function snapshot() {
    var lines = [];
    var grid = document.getElementById('replica-grid');
    var surface = document.querySelector('.arena-surface');
    var cells = grid ? grid.querySelectorAll('.replica-cell') : [];
    var fighter = grid && grid.querySelector('.replica-cell[data-kind="fighter"]');
    var rules = document.querySelector('.replica-rule-layer');
    var metrics = document.getElementById('replica-metrics');
    lines.push('ua: ' + navigator.userAgent);
    lines.push('viewport: ' + innerWidth + 'x' + innerHeight + ' dpr ' + devicePixelRatio +
      (window.visualViewport ? ' vv ' + Math.round(visualViewport.width) + 'x' + Math.round(visualViewport.height) + ' scale ' + visualViewport.scale.toFixed(2) : '') +
      ' scrollY ' + Math.round(scrollY));
    lines.push('html: ' + document.documentElement.className);
    lines.push('body: ' + document.body.className);
    lines.push('metrics: ' + (metrics ? metrics.textContent : 'missing'));
    lines.push('surface: ' + rect(surface) + (surface ? ' ar ' + getComputedStyle(surface).aspectRatio : ''));
    lines.push('grid: ' + rect(grid) + ' mounted ' + cells.length);
    if (fighter) {
      var style = getComputedStyle(fighter);
      lines.push('fighter0: ' + rect(fighter) + ' h ' + style.height + ' vis ' + style.visibility +
        ' op ' + style.opacity + ' disp ' + style.display + ' --ch ' + fighter.style.getPropertyValue('--cell-height'));
      var portrait = fighter.querySelector('img');
      if (portrait) lines.push('portrait0: complete ' + portrait.complete + ' ' + portrait.naturalWidth + 'x' + portrait.naturalHeight);
    } else {
      lines.push('fighter0: none mounted');
    }
    lines.push('rules: ' + (rules ? rules.tagName + ' x' + rules.children.length + ' ' + rect(rules) : 'missing'));
    if (grid) {
      var box = grid.getBoundingClientRect();
      var y = Math.min(innerHeight - 20, Math.max(box.top + 40, 20));
      lines.push('top@grid: ' + describe(document.elementFromPoint(innerWidth / 2, y)));
    }
    lines.push('errors (' + errors.length + '):');
    errors.forEach(function (line) { lines.push('  ' + line); });
    return lines.join('\n');
  }

  function mount() {
    var panel = document.createElement('div');
    panel.setAttribute('style', [
      'position:fixed', 'left:6px', 'right:6px', 'bottom:6px', 'z-index:2147483647',
      'max-height:55vh', 'overflow:auto', 'padding:8px', 'border:1px solid #f2d9a6',
      'background:rgba(0,0,0,.9)', 'color:#f2d9a6', 'font:10px/1.35 ui-monospace,Menlo,monospace',
      'white-space:pre-wrap', 'word-break:break-all', 'cursor:auto', 'user-select:text', '-webkit-user-select:text'
    ].join(';'));
    var bar = document.createElement('div');
    bar.setAttribute('style', 'display:flex;gap:8px;margin-bottom:6px');
    var body = document.createElement('div');
    function button(label, onClick) {
      var el = document.createElement('button');
      el.type = 'button';
      el.textContent = label;
      el.setAttribute('style', 'font:inherit;padding:4px 10px;background:#f2d9a6;color:#000;border:0;cursor:pointer');
      el.addEventListener('click', onClick);
      bar.appendChild(el);
    }
    button('Copy', function () {
      var text = snapshot();
      if (navigator.clipboard) navigator.clipboard.writeText(text).catch(function () {});
    });
    button('Hide', function () { panel.remove(); });
    panel.appendChild(bar);
    panel.appendChild(body);
    document.body.appendChild(panel);
    function refresh() {
      if (!panel.isConnected) return;
      try { body.textContent = snapshot(); } catch (e) { body.textContent = 'snapshot failed: ' + e; }
      setTimeout(refresh, 1000);
    }
    refresh();
  }
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
