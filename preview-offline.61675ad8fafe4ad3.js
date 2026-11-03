(() => {
  const label = document.getElementById('preview-offline-state');
  let ready = false;
  const render = () => {
    if (!label) return;
    label.textContent = ready
      ? navigator.onLine ? 'Доступ без интернета готов' : 'Без сети · правки на устройстве, отправятся после подключения'
      : 'Подготавливаю доступ без интернета…';
  };
  window.addEventListener('online', render);
  window.addEventListener('offline', render);
  render();
  if (!('serviceWorker' in navigator)) {
    if (label) label.textContent = 'Этот браузер не поддерживает открытие сайта без интернета';
    return;
  }
  navigator.serviceWorker.register('./offline-sw.js', {scope: './', updateViaCache: 'none'})
    .catch(async error => {
      // An installed shell still works when the update check has no network.
      const existing = await navigator.serviceWorker.getRegistration('./');
      if (existing?.active && existing.scope === new URL('./', location.href).href) return existing;
      throw error;
    })
    .then(() => navigator.serviceWorker.ready)
    .then(() => { ready = true; render(); })
    .catch(() => { if (label) label.textContent = 'Офлайн-копия пока не готова. Откройте сайт с интернетом ещё раз.'; });
})();
