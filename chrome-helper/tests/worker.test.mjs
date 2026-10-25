import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import {allowedSender, fillCart, validateIntent} from '../core.mjs';

const valid = () => ({id:crypto.randomUUID(),token:'x'.repeat(40),expires_at:Date.now()/1000+300,review_hash:'review',items:[{elementId:'300121',quantity:2,unitPrice:21}]});
const sender={frameId:0,url:'https://lego-nova.vercel.app/models',tab:{id:10}};
async function harness() {
  let listener, release;
  const values={}, firstRead=new Promise(resolve=>{release=resolve;});
  let reads=0;
  const chrome={storage:{local:{
    async get(key){reads++;if(reads===1)await firstRead;return {[key]:values[key]};},
    async set(data){Object.assign(values,data);},async remove(key){delete values[key];}
  }},runtime:{id:'test',getURL:p=>'chrome-extension://test/'+p,onMessage:{addListener:fn=>{listener=fn;}}},
  tabs:{async sendMessage(){throw Error('Simulated unavailable app tab');}},action:{async setBadgeText(){}}};
  const source=(await fs.readFile(new URL('../worker.js',import.meta.url),'utf8')).replace(/^import .*?;\n/,'');
  vm.runInNewContext(source,{chrome,crypto,allowedSender,fillCart,validateIntent,URL});
  return {release,listener,reads:()=>reads,request:message=>new Promise(resolve=>listener(message,sender,resolve))};
}

test('simultaneous clicks cannot acquire two requests across a storage await',async()=>{
  const h=await harness();const first=h.request({type:'cart-intent',intent:valid()});
  const second=await h.request({type:'cart-intent',intent:valid()});
  assert.match(second.message,/Another cart request/);assert.equal(h.reads(),1);
  h.release();assert.equal((await first).state,'failed');
});

test('malformed intent returns a failure instead of leaving the UI waiting',async()=>{
  const h=await harness();const result=await h.request({type:'cart-intent',intent:{}});
  assert.equal(result.state,'failed');assert.match(result.message,/Invalid/);assert.equal(h.reads(),0);
});

test('an unauthorized website cannot start the worker',async()=>{
  const h=await harness();let replied=false;
  assert.equal(h.listener({type:'cart-intent',intent:valid()},{...sender,url:'https://unapproved.example/'},()=>{replied=true;}),undefined);
  assert.equal(replied,false);assert.equal(h.reads(),0);
});

test('malformed existing LEGO group state stops mutations before fetch',async()=>{
  let listener, fetched=false, response;
  const window={};window.top=window;
  const chrome={runtime:{id:'test',onMessage:{addListener:fn=>{listener=fn;}}}};
  vm.runInNewContext(await fs.readFile(new URL('../lego.js',import.meta.url),'utf8'),{
    location:{origin:'https://www.lego.com',pathname:'/en-us/pick-and-build/pick-a-brick'},window,chrome,
    localStorage:{getItem:()=>'{broken'},fetch:()=>{fetched=true;}
  });
  listener({type:'lego-operation',operation:'add',variables:{}},{id:'test'},r=>{response=r;});
  assert.equal(response.ok,false);assert.equal(fetched,false);
});
