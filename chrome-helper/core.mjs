// No network or Chrome APIs here. A persisted journal makes every mutation reviewable.
export const APP_ORIGIN = 'https://lego-nova.vercel.app';
export const LEGO_ORIGIN = 'https://www.lego.com';
export const MAX_BATCH = 400;
export function allowedSender(sender) {
  try { return sender.frameId === 0 && new URL(sender.url).origin === APP_ORIGIN; }
  catch { return false; }
}
export function validateIntent(intent, now = Date.now() / 1000) {
  if (!intent || !/^[a-f0-9-]{32,36}$/.test(intent.id) || typeof intent.token !== 'string' || intent.token.length < 32 ||
      !Number.isFinite(intent.expires_at) || intent.expires_at <= now || intent.expires_at > now + 660 ||
      !Array.isArray(intent.items) || !intent.items.length || intent.items.length > 10000) throw Error('Invalid or expired cart request');
  const seen = new Set();
  for (const item of intent.items) {
    if (!/^\d+$/.test(item.elementId) || !Number.isSafeInteger(item.quantity) || item.quantity < 1 || item.quantity > 100000 ||
        !Number.isSafeInteger(item.unitPrice) || item.unitPrice < 0 || seen.has(item.elementId)) throw Error('Invalid reviewed item');
    seen.add(item.elementId);
  }
}
const key = (channel, sku) => `${channel}:${sku}`;
export function quantities(carts) {
  const output = {};
  if (!Array.isArray(carts)) throw Error('LEGO returned an unfamiliar cart');
  for (const cart of carts) {
    if (!['pab', 'bap'].includes(cart.type) || !Array.isArray(cart.brickLineItems)) throw Error('LEGO cart format changed');
    for (const line of cart.brickLineItems) {
      if (!/^\d+$/.test(line.sku) || !Number.isSafeInteger(line.quantity) || line.quantity < 0) throw Error('Invalid LEGO cart quantity');
      const k = key(cart.type, line.sku);
      if (Object.hasOwn(output, k)) throw Error('Duplicate LEGO cart lines need review');
      output[k] = line.quantity;
    }
  }
  return output;
}
function preserve(before, after) {
  for (const [id, quantity] of Object.entries(before)) if ((after[id] || 0) < quantity) throw Error('The existing cart changed. Review it before resuming.');
}
function reconcile(journal, current, batch) {
  preserve(journal.baseline, current);
  for (const [k, count] of Object.entries(current)) {
    const item = batch.items.find(i => key(batch.channel, i.elementId) === k);
    const base = journal.baseline[k] || 0;
    if (count > base + (item?.quantity || 0)) throw Error('The LEGO cart changed outside this request. Review it.');
  }
}
function makeBatches(items, baseline) {
  const batches = [];
  for (const channel of ['pab','bap']) {
    const selected=items.filter(i=>i.channel===channel);
    if (!selected.length) continue;
    const existing=new Set(Object.keys(baseline).filter(k=>k.startsWith(channel+':')));
    if (existing.size>200) throw Error('The existing LEGO parts cart exceeds its known capacity.');
    let current=[]; let occupied=new Set(existing);
    for (const item of [...selected.filter(i=>existing.has(key(channel,i.elementId))),...selected.filter(i=>!existing.has(key(channel,i.elementId)))]) {
      if (!occupied.has(key(channel,item.elementId)) && occupied.size===200) {
        if (!current.length) throw Error('The existing LEGO parts cart is full. Review it before adding a new list.');
        batches.push({channel,items:current,state:'adding'});current=[];occupied=new Set();
      }
      occupied.add(key(channel,item.elementId));current.push(item);
    }
    if (current.length)batches.push({channel,items:current,state:'adding'});
  }
  return batches;
}
function preserveMain(before, after) {
  for (const line of before.lineItems) {
    const found = after.lineItems.find(item => item.id === line.id);
    if (!found || found.quantity < line.quantity || found.productVariant.sku !== line.productVariant.sku)
      throw Error('An existing LEGO bag item changed. Review the bag.');
  }
}
export function resultFor(journal, state, message) {
  return { state, message, items: journal.items.map(item => ({ elementId: item.elementId, requested: item.quantity,
    added: Math.min(item.quantity, journal.added?.[item.elementId] || 0),
    in_bag: journal.confirmed?.[item.elementId] || 0,
    missing: Math.max(0, item.quantity - (journal.added?.[item.elementId] || 0)) })) };
}
export async function fillCart(intent, adapter, storage) {
  validateIntent(intent);
  let journal = await storage.get(intent.id);
  if (journal?.state === 'complete') return journal.result;
  const elements = await adapter.catalogue(intent.items.map(item => item.elementId));
  for (const item of intent.items) {
    const element = elements.find(e => e.id === item.elementId);
    if (!element || element.availability !== 'AVAILABLE' || !['pab', 'bap'].includes(element.deliveryChannel) ||
        element.price?.currencyCode !== 'USD' || element.price.centAmount !== item.unitPrice)
      throw Error('A reviewed price or availability changed. Refresh the model quote.');
  }
  if (!journal) {
    const [carts, main] = await Promise.all([adapter.readCarts(), adapter.readMain()]);
    const baseline = quantities(carts);
    const items = intent.items.map(item => ({ ...item, channel: elements.find(e => e.id === item.elementId).deliveryChannel }));
    for (const item of items) {
      const element = elements.find(e => e.id === item.elementId);
      if ((baseline[key(item.channel, item.elementId)] || 0) + item.quantity > (element.maxOrderQuantity ?? 999))
        throw Error(`Element ${item.elementId} exceeds its quantity limit with the existing cart.`);
    }
    journal = { version:2, id: intent.id, items, baseline, mainBaseline: main, state: 'adding', added: {}, confirmed:{}, batches:makeBatches(items,baseline), cursor:0 };
    await storage.set(intent.id, journal);
  }
  if (journal.version!==2)throw Error('This request uses an older helper. Review its LEGO bag before continuing.');
  try {
    for (;journal.cursor<journal.batches.length;) {
      const batch=journal.batches[journal.cursor],channel=batch.channel;
      if (batch.state==='sent') throw Error('A bag transfer response was lost. Review the LEGO bag before creating another request.');
      let carts=await adapter.readCarts(), current=quantities(carts);
      reconcile(journal,current,batch);
      const remaining=[];
      for (const item of batch.items) {
        const base=journal.baseline[key(channel,item.elementId)]||0;
        const count=current[key(channel,item.elementId)]||0;
        if (count<base || count>base+item.quantity)throw Error('The LEGO cart changed outside this request. Review it.');
        journal.added[item.elementId]=count-base;
        if (count<base+item.quantity)remaining.push({sku:item.elementId,quantity:base+item.quantity-count});
      }
      await storage.set(intent.id,journal);
      for (let offset=0;offset<remaining.length;offset+=MAX_BATCH) {
        journal.pending={channel,items:remaining.slice(offset,offset+MAX_BATCH)};await storage.set(intent.id,journal);
        await adapter.add(channel,journal.pending.items);
        carts=await adapter.readCarts();current=quantities(carts);reconcile(journal,current,batch);
        for (const item of batch.items) journal.added[item.elementId]=Math.max(0,(current[key(channel,item.elementId)]||0)-(journal.baseline[key(channel,item.elementId)]||0));
        journal.pending=null;await storage.set(intent.id,journal);
      }
      if (batch.items.some(item=>journal.added[item.elementId]!==item.quantity)) {
        journal.state='partial';journal.result=resultFor(journal,'partial','LEGO rejected some quantities. Review the parts list.');
        await storage.set(intent.id,journal);return journal.result;
      }
      const cart=carts.find(c=>c.type===channel);
      if (!cart?.id || !cart.brickLineItems.length)throw Error('The LEGO parts cart is not ready for transfer.');
      const before=await adapter.readMain();preserveMain(journal.mainBaseline,before);
      batch.state='sent';batch.cartId=cart.id;await storage.set(intent.id,journal);
      const response=await adapter.transfer(cart);
      const after=await adapter.readMain();preserveMain(before,after);
      if (!response?.lineItems?.length)throw Error('LEGO did not return a bag transfer. Review the bag.');
      for (const line of response.lineItems) if (!after.lineItems.some(item=>item.id===line.id && item.quantity===line.quantity))
        throw Error('The LEGO bag did not confirm its returned contents. Review the bag.');
      // A transfer receipt alone cannot prove the pieces reached the main bag.
      // Require an exact element-level delta, including pre-existing draft pieces.
      const countInBag=(bag,sku)=>bag.lineItems.filter(line=>line.productVariant?.sku===sku)
        .reduce((sum,line)=>sum+line.quantity,0);
      for (const line of cart.brickLineItems) {
        if (countInBag(after,line.sku)-countInBag(before,line.sku)!==line.quantity)
          throw Error('LEGO did not expose the exact transferred quantities in the bag. Review it before continuing.');
      }
      const moved=await adapter.readCarts();
      if (moved.some(c=>c.type===channel && c.brickLineItems.length))throw Error('The transferred parts cart did not clear. Review the LEGO bag.');
      batch.state='confirmed';journal.mainBaseline=after;
      for (const item of batch.items)journal.confirmed[item.elementId]=item.quantity;
      for (const k of Object.keys(journal.baseline))if(k.startsWith(channel+':'))delete journal.baseline[k];
      journal.cursor++;await storage.set(intent.id,journal);
    }
    journal.state='complete';journal.result=resultFor(journal,'complete','The reviewed pieces are in your LEGO bag. Checkout remains on LEGO.');
    await storage.set(intent.id,journal);return journal.result;
  } catch (error) {
    journal.state='partial';journal.result=resultFor(journal,'partial',error.message);
    await storage.set(intent.id,journal);throw error;
  }
}
