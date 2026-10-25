// Run against `npm run dev -- --port 5175`. All APIs are local fixtures.
// PLAYWRIGHT_MODULE and PLAYWRIGHT_EXECUTABLE may select an existing installation.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { budgetChoice } from '../src/budget.ts';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE || undefined });
const base = process.env.BUDGET_TEST_URL || 'http://127.0.0.1:5175';
const output = process.env.BUDGET_SCREENSHOTS;
if (output) await mkdir(output, { recursive: true });
const errors = [];
const model = { id:'test',model_name:'Test model',connection_status:'connected',auth_mode:'api_key',
  capabilities:{tools:true,vision:true},resolved_capabilities:{tools:true,vision:true},litellm_params:{model:'test'},
  profile:{model:'test',name:'Test model',efforts:['medium','high'],default_effort:'medium',context_window:100000,context_budgets:[100000]} };

async function fixture(options = {}) {
  const context = await browser.newContext(options);
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  const chats = new Map(), sends = [], patches = [], models = [], prices = {latest:null};
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname, method = req.method();
    const body = method === 'POST' || method === 'PATCH' ? req.postDataJSON() : null;
    let data;
    if (path === '/api/llm-models') data = {models:[model],default_id:'test'};
    else if (path === '/api/models') data = {models,pending:0};
    else if (path === '/api/quotes/latest' || path === '/api/quotes') data=prices.latest;
    else if (path === '/api/chats' && method === 'GET') data = {chats:[...chats.values()].map(d=>d.chat)};
    else if (path === '/api/chats' && method === 'POST') {
      const chat={id:`fixture-${chats.size+1}`,title:'Budget test',llm_model_id:'test',created_at:1,updated_at:1,running:false,budget:body.budget};
      chats.set(chat.id,{chat,messages:[],models:{}});data=chat;
    } else {
      const [,id, action] = path.match(/^\/api\/chats\/([^/]+)(?:\/(.*))?$/) || [];
      const detail = chats.get(id);
      if (detail && action === 'stream') return route.fulfill({contentType:'text/event-stream',body:'event: snapshot\ndata: {"running":false}\n\n'});
      if (detail && !action && method === 'GET') data=detail;
      else if (detail && !action && method === 'PATCH') {patches.push(body);Object.assign(detail.chat,body);data=detail.chat;}
      else if (detail && action === 'messages' && method === 'POST') {
        sends.push(body);detail.chat.options=body.options;detail.chat.budget=body.budget;
        detail.messages.push({id:sends.length,role:'user',content:body.text,created_at:1});data={ok:true};
      } else {errors.push(`Unexpected API: ${method} ${path}`);return route.fulfill({status:404,body:'Unexpected test request'});}
    }
    return route.fulfill({json:data});
  });
  await page.goto(`${base}/new`);
  await page.getByRole('button',{name:'Set budget',exact:true}).waitFor();
  await page.getByRole('combobox',{name:'Effort',exact:true}).waitFor();
  return {page,context,chats,sends,patches,models,prices};
}

try {
  const {page,context,chats,sends,patches}=await fixture();
  const message=page.getByRole('textbox',{name:'Message',exact:true});
  await message.fill('Build a red car');
  await page.getByRole('combobox',{name:'Effort',exact:true}).selectOption('high');
  await page.getByLabel('Document files').setInputFiles({name:'idea.txt',mimeType:'text/plain',buffer:Buffer.from('Four wheels')});
  await page.getByRole('button',{name:'Remove idea.txt',exact:true}).waitFor();
  await page.getByLabel('Image files').setInputFiles({name:'reference.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jqz8AAAAASUVORK5CYII=','base64')});
  await page.getByRole('button',{name:'Remove reference.png',exact:true}).waitFor();
  await page.getByRole('button',{name:/^Send/}).click();
  const dialog=page.getByRole('dialog');
  await dialog.waitFor();assert.equal(chats.size,0);assert.equal(sends.length,0);
  const input=dialog.getByLabel('Parts budget (USD)',{exact:true});
  await dialog.getByRole('button',{name:'Cancel',exact:true}).focus();await page.keyboard.press('Tab');
  assert.equal(await input.evaluate(el=>el===document.activeElement),true,await page.evaluate(()=>document.activeElement?.outerHTML.slice(0,250)));
  await input.fill('1.234');await dialog.getByRole('button',{name:'Start with budget'}).click();
  await dialog.getByRole('alert').waitFor();assert.equal(sends.length,0);
  await input.fill('100');
  assert.equal(await dialog.locator('.budget-limit').textContent(),'$100.00 budget → $105.00 maximum');
  if(output)await page.screenshot({path:`${output}/budget-desktop.png`});
  // Escape cancels and restores the original draft, attachments and AI settings.
  await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
  assert.equal(await message.inputValue(),'Build a red car');assert.equal(chats.size,0);
  assert.equal(await page.getByRole('combobox',{name:'Effort',exact:true}).inputValue(),'high');
  assert.equal(await page.getByRole('button',{name:'Remove idea.txt',exact:true}).count(),1);
  assert.equal(await page.getByRole('button',{name:'Remove reference.png',exact:true}).count(),1);
  assert.equal(await message.evaluate(el=>el===document.activeElement),true);
  await message.press('Enter');await dialog.waitFor();
  await dialog.getByLabel('Parts budget (USD)',{exact:true}).fill('10.01');
  await page.keyboard.press('Enter');await page.waitForURL('**/chat/fixture-1');
  assert.equal(sends.length,1);assert.deepEqual(sends[0].budget,budgetChoice(1001));
  assert.equal(sends[0].documents[0].name,'idea.txt');assert.equal(sends[0].options.effort,'high');
  assert.equal(sends[0].images.length,1);
  await page.getByRole('button',{name:'Change budget',exact:true}).waitFor();
  await message.fill('Add a roof');await page.getByRole('button',{name:/^Send/}).click();
  await page.waitForFunction(()=>document.querySelector('textarea')?.value==='');
  assert.equal(sends.length,2);assert.deepEqual(sends[1].budget,budgetChoice(1001));assert.equal(await dialog.count(),0);
  await page.getByRole('button',{name:'Change budget',exact:true}).click();
  await dialog.getByLabel('Parts budget (USD)',{exact:true}).fill('20');
  await dialog.getByRole('button',{name:'Save budget'}).click();await dialog.waitFor({state:'hidden'});
  assert.deepEqual(patches.at(-1).budget,budgetChoice(2000));
  await page.getByRole('button',{name:'Remove budget',exact:true}).click();
  await page.getByText('No parts budget',{exact:true}).waitFor();
  assert.deepEqual(patches.at(-1).budget,budgetChoice(null));
  await context.close();

  const plan=await fixture();
  await plan.page.getByRole('combobox',{name:'Mode',exact:true}).selectOption('plan');
  await plan.page.getByRole('textbox',{name:'Message',exact:true}).fill('Plan a car');
  await plan.page.getByRole('button',{name:/^Send/}).click();await plan.page.waitForURL('**/chat/fixture-1');
  assert.equal(plan.sends.length,1);assert.equal(plan.sends[0].budget.status,'pending');
  await plan.page.getByRole('combobox',{name:'Mode',exact:true}).selectOption('agent');
  await plan.page.getByRole('textbox',{name:'Message',exact:true}).fill('Build it');
  await plan.page.getByRole('button',{name:/^Send/}).click();
  await plan.page.getByRole('dialog').getByRole('button',{name:'Skip budget'}).click();
  await plan.page.waitForFunction(()=>document.querySelector('textarea')?.value==='');
  assert.equal(plan.sends.length,2);assert.equal(plan.sends[1].budget.status,'skipped');
  await plan.context.close();

  const mobile=await fixture({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  await mobile.page.getByRole('textbox',{name:'Message',exact:true}).fill('Build a robot');
  await mobile.page.getByRole('button',{name:/^Send/}).click();
  const mobileDialog=mobile.page.getByRole('dialog');await mobileDialog.waitFor();
  const bounds=await mobileDialog.boundingBox();assert.ok(bounds.x>=0&&bounds.x+bounds.width<=390);
  for(const button of await mobileDialog.getByRole('button').all())assert.ok((await button.boundingBox()).height>=44);
  if(output)await mobile.page.screenshot({path:`${output}/budget-mobile.png`});
  await mobileDialog.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(mobile.chats.size,0);assert.equal(await mobile.page.getByRole('textbox',{name:'Message',exact:true}).inputValue(),'Build a robot');
  await mobile.context.close();

  const pricing=await fixture();
  const now=Date.now()/1000, revision='a'.repeat(64);
  const initialQuote={id:'initial',revision,currency:'USD',created_at:now,checked_at:now,subtotal:900,known_subtotal:900,
    total_quantity:10,priced_quantity:10,purchasable_quantity:10,coverage:1,unresolved_lines:0,stale:false,fees:[],fees_complete:false,review_hash:'initial',lines:[]};
  const assessment={budget:budgetChoice(1000),status:'within_budget',subtotal:900,revision,verification_quote:'initial',checked_at:now};
  pricing.models.push({file:'fixture.mpd',name:'Car',description:'Budget test car',model_url:'/files/generated/fixture.mpd',
    parts:10,gallery:false,size:100,mtime:now,status:'ready',bom_status:'ready',quote:initialQuote,budget_assessment:assessment});
  pricing.prices.latest=initialQuote;
  await pricing.page.goto(`${base}/models`);
  await pricing.page.getByText('At completion: $9.00 · Within budget',{exact:true}).waitFor();
  await pricing.page.getByRole('button',{name:'Review parts and cost'}).click();
  const review=pricing.page.getByRole('dialog');
  await review.getByText('Last checked parts cost: $9.00 · Within budget',{exact:true}).waitFor();
  pricing.prices.latest={...initialQuote,id:'later',subtotal:1200,known_subtotal:1200,review_hash:'later'};
  await review.getByRole('button',{name:'Refresh prices',exact:true}).click();
  await review.getByText('Last checked parts cost: $12.00 · $1.50 above the maximum',{exact:true}).waitFor();
  assert.equal(await review.getByText('At completion: $9.00 · Within budget',{exact:true}).count(),1);
  assert.equal(await review.getByRole('button',{name:'Add to LEGO bag',exact:true}).isDisabled(),true);
  assert.equal(assessment.subtotal,900);
  pricing.prices.latest={...initialQuote,id:'missing',subtotal:null};
  await review.getByRole('button',{name:'Refresh prices',exact:true}).click();
  await review.getByText('Latest prices are incomplete.',{exact:true}).waitFor();
  await pricing.context.close();
  assert.deepEqual(errors,[]);
  console.log('PASS: validation, cancel/focus, attachments/settings, send, edit persistence, change/remove, plan-to-build, skip, mobile, changed/missing prices, immutable original assessment, zero cart calls');
} finally {await browser.close();}
