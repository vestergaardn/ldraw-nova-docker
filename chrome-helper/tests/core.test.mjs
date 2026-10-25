import test from 'node:test';
import assert from 'node:assert/strict';
import { allowedSender, fillCart, validateIntent } from '../core.mjs';
const makeIntent = () => ({id:'1234567890abcdef1234567890abcdef',token:'x'.repeat(40),expires_at:Date.now()/1000+600,
 items:[{elementId:'300121',quantity:3,unitPrice:21},{elementId:'300321',quantity:2,unitPrice:13}]});
function fixture() {
 const stored = new Map();
 const storage = {async get(id){return structuredClone(stored.get(id));},async set(id,j){stored.set(id,structuredClone(j));}};
 const carts=[{id:'draft-a',type:'pab',brickLineItems:[{sku:'300121',quantity:2},{sku:'999999',quantity:4}]},
   {id:'draft-b',type:'bap',brickLineItems:[]}];
 const main={id:'main',lineItems:[{id:'existing-set',quantity:1,productVariant:{sku:'set-123'}}]};
 let adds=0,transfers=0,loseAdd=false,loseTransfer=false,reject=false,external=false;
 const adapter={
   async catalogue(){return [{id:'300121',availability:'AVAILABLE',deliveryChannel:'pab',price:{currencyCode:'USD',centAmount:21},maxOrderQuantity:999},
     {id:'300321',availability:'AVAILABLE',deliveryChannel:'bap',price:{currencyCode:'USD',centAmount:13},maxOrderQuantity:999}];},
   async readCarts(){return structuredClone(carts);},async readMain(){return structuredClone(main);},
   async add(channel,items){adds++;const cart=carts.find(c=>c.type===channel);for(const item of items){let line=cart.brickLineItems.find(l=>l.sku===item.sku);if(!line){line={sku:item.sku,quantity:0};cart.brickLineItems.push(line);}line.quantity+=reject?Math.max(0,item.quantity-1):item.quantity;}
     if(external)cart.brickLineItems.push({sku:'12345',quantity:1});
     if(loseAdd){loseAdd=false;throw Error('lost add response');}},
   async transfer(cart){transfers++;main.lineItems.push({id:'bag-'+cart.id,quantity:1,productVariant:{sku:'parts-'+cart.type}});
     carts.find(c=>c.id===cart.id).brickLineItems=[];if(loseTransfer){loseTransfer=false;throw Error('lost transfer response');}return structuredClone(main);},
 };
 return {adapter,storage,carts,main,get adds(){return adds;},get transfers(){return transfers;},loseAdd(){loseAdd=true;},loseTransfer(){loseTransfer=true;},reject(){reject=true;},external(){external=true;}};
}
test('only the private top-level app can start a request',()=>{
 assert(allowedSender({frameId:0,url:'https://lego-nova.vercel.app/models'}));
 for(const url of ['https://evil.example','https://lego-nova.vercel.app.evil.example','http://lego-nova.vercel.app'])assert(!allowedSender({frameId:0,url}));
 assert(!allowedSender({frameId:1,url:'https://lego-nova.vercel.app'}));
 assert.throws(()=>validateIntent({...makeIntent(),expires_at:0}));
});
test('preserves existing pieces and sets, supports both channels, repeated clicks never add twice',async()=>{
 const f=fixture(),i=makeIntent();const r=await fillCart(i,f.adapter,f.storage);
 assert.equal(r.state,'complete');assert.deepEqual(r.items.map(x=>x.added),[3,2]);assert.equal(f.main.lineItems[0].quantity,1);
 assert.equal(f.adds,2);assert.equal(f.transfers,2);
 assert.deepEqual(await fillCart(i,f.adapter,f.storage),r);assert.equal(f.adds,2);
});
test('lost add response reconciles before resuming and does not duplicate',async()=>{
 const f=fixture(),i=makeIntent();f.loseAdd();await assert.rejects(fillCart(i,f.adapter,f.storage),/lost add/);
 assert.equal(f.carts[0].brickLineItems[0].quantity,5);
 const r=await fillCart(i,f.adapter,f.storage);assert.equal(r.state,'complete');assert.equal(f.adds,2);
});
test('lost bag transfer response never transfers twice',async()=>{
 const f=fixture(),i=makeIntent();f.loseTransfer();await assert.rejects(fillCart(i,f.adapter,f.storage),/lost transfer/);
 await assert.rejects(fillCart(i,f.adapter,f.storage),/transfer response was lost/);assert.equal(f.transfers,1);
});
test('partial quantities remain reported and do not transfer an incomplete list',async()=>{
 const f=fixture();f.reject();const r=await fillCart(makeIntent(),f.adapter,f.storage);
 assert.equal(r.state,'partial');assert.equal(r.items[0].missing,1);assert.equal(f.transfers,0);
});
test('unexpected external cart additions stop before bag transfer',async()=>{
 const f=fixture();f.external();await assert.rejects(fillCart(makeIntent(),f.adapter,f.storage),/outside this request/);assert.equal(f.transfers,0);
});
test('stock and price changes stop before any mutation',async()=>{
 for(const changes of [{availability:'OUT_OF_STOCK'},{price:{currencyCode:'USD',centAmount:22}},{maxOrderQuantity:4}]){
  const f=fixture();const get=f.adapter.catalogue;f.adapter.catalogue=async()=>{const es=await get();Object.assign(es[0],changes);return es;};
  await assert.rejects(fillCart(makeIntent(),f.adapter,f.storage));assert.equal(f.adds,0);
 }
});
test('more than 400 distinct pieces use bounded batches and preserve previous groups',async()=>{
 const f=fixture(),intent=makeIntent();intent.items=Array.from({length:450},(_,i)=>({elementId:String(700000+i),quantity:1,unitPrice:10}));
 f.adapter.catalogue=async()=>intent.items.map(i=>({id:i.elementId,availability:'AVAILABLE',deliveryChannel:'pab',price:{currencyCode:'USD',centAmount:10},maxOrderQuantity:999}));
 const calls=[];const add=f.adapter.add;f.adapter.add=async(channel,items)=>{assert(items.length<=200);calls.push(items.length);return add(channel,items);};
 f.adapter.transfer=async cart=>{f.main.lineItems.push({id:'group-'+f.main.lineItems.length,quantity:1,productVariant:{sku:'parts'}});f.carts.find(c=>c.id===cart.id).brickLineItems=[];return structuredClone(f.main);};
 const result=await fillCart(intent,f.adapter,f.storage);
 assert.equal(result.state,'complete');assert.deepEqual(calls,[198,200,52]);
 assert.equal(result.items.reduce((n,i)=>n+i.in_bag,0),450);assert.equal(f.main.lineItems[0].id,'existing-set');
 await fillCart(intent,f.adapter,f.storage);assert.equal(calls.length,3);
});
test('foreign intent IDs, duplicate elements and noninteger quantities are rejected',()=>{
 const i=makeIntent();
 for (const change of [{id:'../../bad'},{items:[i.items[0],i.items[0]]},{items:[{...i.items[0],quantity:1.5}]}])assert.throws(()=>validateIntent({...i,...change}));
});
