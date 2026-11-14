/* 在 head 中同步应用服务端保存的主题，避免切换时闪烁。 */
(function () {
  var root = document.documentElement;
  var saved = root.getAttribute('data-theme-source') === 'saved' ? root.getAttribute('data-theme') : null;
  if (!saved) { try { saved = localStorage.getItem('theme'); } catch (e) {} }
  var theme = saved === 'dark' || saved === 'light'
    ? saved
    : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  root.setAttribute('data-theme', theme);

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    function persist(value) {
      return fetch('/api/settings/theme', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({theme: value})}).then(function (response) {
          if (!response.ok) throw new Error('主题保存失败');
        });
    }
    if (saved && root.getAttribute('data-theme-source') !== 'saved') persist(theme).catch(function () {});
    btn.addEventListener('click', async function () {
      var previous = root.getAttribute('data-theme');
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try {
        await persist(next);
        try { localStorage.setItem('theme', next); } catch (e) {}
      } catch (e) {
        root.setAttribute('data-theme', previous);
        alert('主题没有保存成功，请重试。');
      }
    });
  });
})();
