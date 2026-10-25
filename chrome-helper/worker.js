import { allowedSender, fillCart, validateIntent } from './core.mjs';
const running = new Set();
const storage = {
  async get(id) { return (await chrome.storage.local.get(id))[id]; },
  async set(id, value) { await chrome.storage.local.set({ [id]: value }); },
};
async function send(tabId, message) {
  const response = await chrome.tabs.sendMessage(tabId, message);
  if (!response?.ok) throw Error(response?.error || 'The helper could not reach the page. Open it and retry.');
  return response.body;
}
async function run(intent, appTabId) {
  validateIntent(intent);
  if (running.size) return { message: 'Another cart request is running. Wait for its result.' };
  // Acquire synchronously, before storage or network yields to another message.
  running.add(intent.id);
  try {
  const previous = await storage.get(intent?.id);
  if (previous?.state === 'complete') {
    const session=(await chrome.storage.local.get('request-'+intent.id))['request-'+intent.id];
    if(session)try {await send(session.appTabId,{type:'private-api',path:`/api/cart-intents/${intent.id}/result`,body:{token:session.intent.token,claim_id:session.claimId,result:previous.result}});} catch {}
    return previous.result;
  }
  const active=(await chrome.storage.local.get('activeRequest')).activeRequest;
  if(active && active!==intent.id) {
    const other=await storage.get(active);
    if(other?.state!=='complete')return {message:'Resume the previous request, or review its bag and release it in the helper.'};
  }
  await chrome.storage.local.set({activeRequest:intent.id});
    const sessionKey = 'request-' + intent.id;
    let session = (await chrome.storage.local.get(sessionKey))[sessionKey];
    if (!session) { session = { intent, appTabId, claimId: crypto.randomUUID() }; await chrome.storage.local.set({ [sessionKey]: session, lastRequest: sessionKey }); }
    const body = { token: intent.token, claim_id: session.claimId };
    const claimed = await send(appTabId, { type: 'private-api', path: `/api/cart-intents/${intent.id}/claim`, body });
    if (claimed.review_hash !== intent.review_hash) throw Error('The reviewed parts list changed.');
    intent = { ...intent, items: claimed.items };
    validateIntent(intent);
    const tabs = await chrome.tabs.query({ url: 'https://www.lego.com/en-us/*' });
    let tab = tabs.find(tab => tab.url?.includes('/pick-and-build/pick-a-brick'));
    if (!tab) {
      await chrome.tabs.create({ url: 'https://www.lego.com/en-us/pick-and-build/pick-a-brick' });
      return { message: 'The LEGO tab is open. Sign in if needed, then select Resume in the helper.' };
    }
    const operation = (operation, variables = {}) => send(tab.id, { type: 'lego-operation', operation, variables });
    const adapter = {
      async catalogue(ids) { const rows = []; for (let i = 0; i < ids.length; i += 100) {
        const r = await operation('catalogue', { input: {skus:ids.slice(i,i+100),perPage:Math.min(100,ids.length-i),fetchSiblings:false,availability:['AVAILABLE','OUT_OF_STOCK']} });
        rows.push(...r.searchElements.results);
        if(i+100<ids.length)await new Promise(resolve=>setTimeout(resolve,1100));
      } return rows; },
      async readCarts() { return (await operation('readCarts', { cartTypes: ['pab', 'bap'] })).elementCarts.carts; },
      async readMain() { const cart = (await operation('readMain')).me?.cart; if (!cart?.lineItems) throw Error('Sign in to LEGO and open your bag before resuming.'); return cart; },
      async add(channel, items) { return operation('add', { items, cartType: channel, returnCarts: ['pab', 'bap'] }); },
      async transfer(cart) { return (await operation('transfer', { elementCartId: cart.id, elementQuantity: cart.brickLineItems.reduce((sum,l)=>sum+l.quantity,0), hasHandlingFee: cart.shippingMethod ? !cart.shippingMethod.isFree : undefined })).addElementCartToMainCart; },
    };
    const result = await fillCart(intent, adapter, storage);
    await send(appTabId, { type: 'private-api', path: `/api/cart-intents/${intent.id}/result`, body: { ...body, result } });
    if(result.state==='complete')await chrome.storage.local.remove('activeRequest');
    await chrome.action.setBadgeText({text:result.state==='complete'?'✓':'!'});
    await chrome.tabs.update(tab.id, { active: true });
    return result;
  } catch (error) {
    await chrome.action.setBadgeText({text:'!'});
    const journal=await storage.get(intent.id);
    const result=journal?.result || {state:'failed',message:error.message,items:intent.items.map(i=>({elementId:i.elementId,requested:i.quantity,added:0,in_bag:0,missing:i.quantity}))};
    const session=(await chrome.storage.local.get('request-'+intent.id))['request-'+intent.id];
    if(session)try {await send(appTabId,{type:'private-api',path:`/api/cart-intents/${intent.id}/result`,body:{token:intent.token,claim_id:session.claimId,result}});} catch {}
    if(!journal)await chrome.storage.local.remove('activeRequest');
    return result;
  } finally { running.delete(intent.id); }
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.type === 'cart-intent' && allowedSender(sender)) { run(message.intent, sender.tab.id).then(respond, error=>respond({state:'failed',message:error.message})); return true; }
  if(message.type==='release-reviewed' && sender.id===chrome.runtime.id && sender.url===chrome.runtime.getURL('popup.html')) {chrome.storage.local.remove('activeRequest').then(()=>respond({message:'The previous request is released. Review a fresh list in the app.'}));return true;}
  if (message.type === 'resume' && sender.id === chrome.runtime.id && sender.url === chrome.runtime.getURL('popup.html')) {
    chrome.storage.local.get('lastRequest').then(async data => {
      const session = (await chrome.storage.local.get(data.lastRequest))[data.lastRequest];
      return session ? run(session.intent, session.appTabId) : { message: 'No reviewed cart request.' };
    }).then(respond, error=>respond({state:'failed',message:error.message})); return true;
  }
});
