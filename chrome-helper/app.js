(() => {
  const origin = 'https://lego-nova.vercel.app';
  if (location.origin !== origin || window !== window.top) return;
  const post = payload => window.postMessage({ source: 'lego-nova-helper', ...payload }, origin);
  window.addEventListener('message', async event => {
    if (event.origin !== origin || event.source !== window || event.data?.source !== 'lego-nova-app') return;
    if (event.data.type === 'ping') return post({ type: 'ready' });
    if (event.data.type !== 'cart-intent') return;
    const intent = event.data.intent;
    try {
      const result = await chrome.runtime.sendMessage({ type: 'cart-intent', intent });
      post({ type: 'result', intentId: intent?.id, message: result.message, items: result.items, state: result.state });
    } catch (error) { post({ type: 'result', intentId: intent?.id, message: error.message }); }
  });
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || message.type !== 'private-api' ||
        !/^\/api\/cart-intents\/[a-f0-9-]{32,36}\/(claim|result)$/.test(message.path)) return;
    fetch(message.path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(message.body) })
      .then(async response => { const body = await response.json(); if (!response.ok) throw Error(body.detail || 'The app rejected this request'); return body; })
      .then(body => respond({ ok: true, body }), error => respond({ ok: false, error: error.message }));
    return true;
  });
  post({ type: 'ready' });
})();
