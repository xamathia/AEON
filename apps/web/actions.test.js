'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.dataset = {}; this.style = {}; this.value = ''; this.hidden = false; this.disabled = false; this.classList = { toggle() {} }; this.listeners = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map((item) => item.textContent).join(' '); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.text = ''; this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  querySelectorAll() { return []; }
  reportValidity() { return true; }
}
function app(fetch) {
  const elements = new Map();
  const navigations = [];
  for (const match of fs.readFileSync(`${__dirname}/index.html`, 'utf8').matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element());
  const document = { body: new Element(), createElement: () => new Element(), createTextNode: (text) => { const el = new Element(); el.textContent = text; return el; }, querySelectorAll: () => [], getElementById: (id) => elements.get(id) || null };
  const context = vm.createContext({ document, fetch, Intl, Date, URL, Error, setTimeout, clearTimeout, window: { location: { origin: 'http://127.0.0.1:8787', assign: (url) => navigations.push(url) }, addEventListener() {} } });
  const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8').replace(/^init\(\);$/m, '').replace(/^initConnections\(\);$/m, '');
  vm.runInContext(source, context);
  return { run: (code) => vm.runInContext(code, context), elements, navigations };
}
function response(data, status = 200) { return { ok: status < 400, status, json: async () => data }; }
function provider(id, extra = {}) { return { id, name: id, configuration: 'ready', authorized: false, scopes: [], expires_at: null, read: { status: 'never', last_read_at: null, count: 0, complete: null, error: null }, data: null, ...extra }; }
function connections(providers = [provider('google_calendar'), provider('gmail')]) { return { canonical_origin: 'http://127.0.0.1:8787', providers, routes: { configured: false, read_status: 'not_requested' }, oauth_result: null }; }


function fixture(extra = {}) {
  return {revision:'revision-a',status:'ready',calendar:{read_at:1790000000,complete:true,last_read_failed:false,horizon:{start:'2026-09-14T00:00:00Z',end:'2026-09-15T00:00:00Z'},timezone:'Europe/Paris',events:[{id:'a',title:'<script>private</script>',planned_start:'2026-09-14T08:00:00Z',planned_end:'2026-09-14T09:00:00Z'},{id:'b',title:'Second',planned_start:'2026-09-14T10:00:00Z',planned_end:'2026-09-14T11:00:00Z'}]},travel_pairs:[{from_event_id:'a',to_event_id:'b',from_title:'<script>private</script>',to_title:'Second',departure_time:'2026-09-14T09:00:00Z',travel:null}],routes:{configured:true,attempts:0,limit:20,remaining:20},issues:[],result:null,...extra};
}
function session(ui) { ui.run("liveState.csrfToken='csrf'; liveState.canonicalOrigin='http://127.0.0.1:8787'; syncForecastConnection()"); }


function planning() {
  const simulation = (risk) => ({samples:1000,llm_calls:0,assumptions:['Reused traffic, uncalibrated.'],events:[{event_id:'b',arrival:{p10:'2026-09-14T09:45:00Z',p50:'2026-09-14T09:50:00Z',p90:'2026-09-14T10:15:00Z'},late_arrival_probability:risk}]});
  return {status:'plans_found',baseline:simulation(.71),plans:[{id:'p-a',operations:[{event_id:'a',before:{planned_start:'2026-09-14T08:00:00Z',planned_end:'2026-09-14T09:00:00Z'},after:{planned_start:'2026-09-14T07:30:00Z',planned_end:'2026-09-14T08:30:00Z'}}],simulation:simulation(.24),target_risk_before:.71,target_risk_after:.24,shift_minutes:-30,requires_approval:true}],search:{evaluated_candidates:9,truncated:false,rejected_reasons:[]},llm_calls:0};
}


function actions(extra = {}) {
 return {provider:'google_calendar_write',configuration:'ready',connected:true,pending:false,busy:false,receipts:[],budget:{executions:0,limit:10,remaining:10},audit:'memory_session_only',...extra};
}
function receipt(status = 'applied', id = 'receipt-a') {
 return {id,status,reason_codes:[],actions:planning().plans[0].operations};
}
async function readyActions(ui) {
 session(ui); ui.run(`acceptForecast(${JSON.stringify(fixture({planning:planning()}))}); renderForecast()`);
 await ui.run('refreshCalendarActions()');
}
function allButtons(element) { return element.children.flatMap(child => [...(child.type === 'button' ? [child] : []), ...allButtons(child)]); }

test('actions metadata never writes or expands Calendar read access',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(actions());});await readyActions(ui);
 assert.deepEqual(calls.map(call=>call.path),['/api/actions']);assert.equal(calls[0].options.method,undefined);
 assert.equal(ui.run('liveState.providers.google_calendar'),undefined);
 assert.match(ui.elements.get('actions-notice').textContent,/write access/i);
 assert.match(ui.elements.get('actions-budget').textContent,/0.*10/);
});

test('write connect explicitly requests its own consent with CSRF and a fixed Google destination',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(path.endsWith('/connect')?{authorization_url:'https://accounts.google.com/o/oauth2/v2/auth?state=write-test'}:actions({connected:false}));});await readyActions(ui);
 assert.equal(ui.navigations.length,0);await ui.run("calendarAction('connect')");
 const sent=calls.find(call=>call.path.endsWith('/connect'));
 assert.deepEqual(JSON.parse(sent.options.body),{});assert.equal(sent.options.headers['X-Aeon-CSRF'],'csrf');assert.equal(sent.options.credentials,'same-origin');
 assert.deepEqual(ui.navigations,['https://accounts.google.com/o/oauth2/v2/auth?state=write-test']);
});

test('write connect rejects an unexpected consent destination',async()=>{
 const ui=app(async(path)=>response(path.endsWith('/connect')?{authorization_url:'https://other.example/consent'}:actions({connected:false})));await readyActions(ui);
 await ui.run("calendarAction('connect')");assert.deepEqual(ui.navigations,[]);assert.match(ui.elements.get('actions-notice').textContent,/invalid/i);
});

test('only the explicit apply button sends the selected server plan and exact approval body',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(actions(path.endsWith('/execute')?{receipts:[receipt()],budget:{executions:1,limit:10,remaining:9}}:{}));});await readyActions(ui);
 const list=ui.elements.get('live-plan-list');assert.match(list.textContent,/Before.*10:00/);assert.match(list.textContent,/After.*09:30/);
 assert.equal(calls.length,1);ui.run("previewLivePlan('p-a'); closeLivePreview()");assert.equal(calls.length,1);
 const button=allButtons(ui.elements.get('live-plan-list')).find(item=>item.textContent==='Apply this plan to Calendar');assert.ok(button);assert.equal(button.disabled,false);
 await button.listeners.click();
 const sent=calls.find(call=>call.path.endsWith('/execute'));assert.deepEqual(JSON.parse(sent.options.body),{revision:'revision-a',plan_id:'p-a',approved:true});
 assert.equal(sent.options.headers['X-Aeon-CSRF'],'csrf');assert.equal(sent.options.credentials,'same-origin');
 assert.equal(ui.run('forecastState.snapshot'),null);assert.equal(ui.run('forecastState.preview'),null);assert.equal(ui.run('state.reference'),null);
 assert.equal(ui.run('actionsState.calendarNeedsRead'),true);assert.match(ui.elements.get('actions-receipts').textContent,/Applied/);
 await ui.run('refreshForecast()');assert.equal(calls.length,2);
});

test('missing write consent, stale plans and exhausted execution budget never apply',async()=>{
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(actions({connected:false}));});await readyActions(ui);
 await ui.run("applyLivePlan('p-a')");ui.run('actionsState.snapshot.connected=true; actionsState.snapshot.budget.remaining=0');await ui.run("applyLivePlan('p-a')");
 ui.run('actionsState.snapshot.budget.remaining=10; forecastState.stale=true');await ui.run("applyLivePlan('p-a')");
 ui.run('forecastState.stale=false');await ui.run("applyLivePlan('forged')");assert.deepEqual(calls,['/api/actions']);
});

test('undo uses only an applied receipt and does not depend on the execution budget',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(actions({receipts:[receipt(path.endsWith('/undo')?'undone':'applied')],budget:{executions:10,limit:10,remaining:0}}));});await readyActions(ui);
 await ui.run("undoCalendarReceipt('receipt-a')");const sent=calls.find(call=>call.path.endsWith('/undo'));
 assert.deepEqual(JSON.parse(sent.options.body),{receipt_id:'receipt-a',approved:true});assert.equal(sent.options.headers['X-Aeon-CSRF'],'csrf');
 assert.match(ui.elements.get('actions-receipts').textContent,/Undone/);await ui.run("undoCalendarReceipt('receipt-a')");assert.equal(calls.length,2);
});

test('unknown and conflict receipts remain honest text and never offer undo',async()=>{
 const unsafe=receipt('unknown','<img src=x onerror=alert(1)>');unsafe.actions[0].event_id='<script>private</script>';
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(actions({receipts:[unsafe,receipt('conflict','conflict-a')]}));});await readyActions(ui);
 const container=ui.elements.get('actions-receipts');assert.match(container.textContent,/Outcome unknown/);assert.match(container.textContent,/Check Google Calendar/);assert.match(container.textContent,/<script>private<\/script>/);
 assert.equal(allButtons(container).filter(button=>!button.disabled).length,0);
 await ui.run("undoCalendarReceipt('conflict-a')");assert.deepEqual(calls,['/api/actions']);
});

test('a failed apply reads receipts only and never replays a write',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});if(path.endsWith('/execute'))throw new Error('offline');return response(actions({receipts:calls.length>1?[receipt('unknown')]:[]}));});await readyActions(ui);
 await ui.run("applyLivePlan('p-a')");assert.deepEqual(calls.map(call=>call.path),['/api/actions','/api/actions/execute','/api/actions']);
 assert.match(ui.elements.get('actions-notice').textContent,/cannot be reached/);assert.match(ui.elements.get('actions-receipts').textContent,/Outcome unknown/);
 await ui.run("applyLivePlan('p-a')");assert.equal(calls.length,3);
});

test('session changes discard late metadata and erase another session’s receipts',async()=>{
 let release;const ui=app(()=>new Promise(resolve=>{release=resolve;}));session(ui);const pending=ui.run('refreshCalendarActions()');
 ui.run('liveState.csrfToken=null; syncForecastConnection()');release(response(actions({receipts:[receipt()]})));await pending;
 assert.equal(ui.run('actionsState.snapshot'),null);assert.equal(ui.elements.get('actions-receipts').textContent,'');assert.equal(ui.elements.get('actions-connect').disabled,true);
});

test('source changes discard an in-flight apply response and do not restore plans',async()=>{
 let release;const ui=app(async(path)=>path.endsWith('/execute')?await new Promise(resolve=>{release=resolve;}):response(actions()));await readyActions(ui);
 const pending=ui.run("applyLivePlan('p-a')");ui.run(`liveState.providers.gmail=${JSON.stringify(provider('gmail'))}; syncForecastConnection()`);
 release(response(actions({receipts:[receipt()]})));await pending;
 assert.equal(ui.run('forecastState.snapshot'),null);assert.equal(ui.run('actionsState.stale'),true);assert.equal(ui.run('actionsState.pending'),null);
 assert.doesNotMatch(ui.elements.get('actions-receipts').textContent,/Applied/);
});

test('mutations disable source reads and duplicate actions until the response arrives',async()=>{
 let release;const calls=[];const ui=app(async(path)=>{calls.push(path);return path.endsWith('/execute')?await new Promise(resolve=>{release=resolve;}):response(actions());});await readyActions(ui);
 const pending=ui.run("applyLivePlan('p-a')");assert.equal(ui.elements.get('calendar-read-fields').disabled,true);assert.equal(ui.elements.get('intelligence-extract').disabled,true);assert.equal(ui.elements.get('actions-connect').disabled,true);
 await ui.run("applyLivePlan('p-a')");await ui.run("connectionAction('gmail','read',{query:'test'})");assert.equal(calls.length,2);
 release(response(actions({receipts:[receipt()]})));await pending;assert.equal(ui.run('actionsState.pending'),null);
});

test('forgetting write access uses an empty body and preserves session receipts',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(actions({connected:!path.endsWith('/forget'),receipts:[receipt()],budget:{executions:1,limit:10,remaining:9}}));});await readyActions(ui);
 await ui.run("calendarAction('forget')");assert.deepEqual(JSON.parse(calls[1].options.body),{});assert.equal(calls[1].path,'/api/actions/forget');
 assert.equal(ui.run('actionsState.snapshot.connected'),false);assert.match(ui.elements.get('actions-receipts').textContent,/Applied/);assert.equal(ui.run('forecastState.snapshot'),null);
});

test('a successful Calendar read permits forecasting again and refreshes retained receipts',async()=>{
 const data={...fixture().calendar,synthetic:false,read_at:1790000100};
 const calendar=provider('google_calendar',{authorized:true,data,read:{status:'succeeded',error:null}});
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(path==='/api/calendar/read'?{provider:'google_calendar',data}:path==='/api/connections'?connections([calendar,provider('gmail')]):path==='/api/live/scenario'?fixture():actions({receipts:[receipt()]}));});await readyActions(ui);
 ui.run('actionsState.calendarNeedsRead=true; invalidateForecast(); renderForecast()');
 await ui.run("connectionAction('google_calendar','read',{time_min:'2026-09-14T00:00:00Z',time_max:'2026-09-15T00:00:00Z',timezone:'Europe/Paris'})");
 assert.equal(ui.run('actionsState.calendarNeedsRead'),false);assert.match(ui.elements.get('actions-receipts').textContent,/Applied/);
 assert.deepEqual(calls,['/api/actions','/api/calendar/read','/api/connections','/api/actions']);
 await ui.run('refreshForecast()');assert.equal(calls.at(-1),'/api/live/scenario');
});

test('failed receipt refresh preserves stale receipts and blocks undo until metadata recovers',async()=>{
 let fail=false;const calls=[];const ui=app(async(path)=>{calls.push(path);if(fail)throw new Error('offline');return response(actions({receipts:[receipt()]}));});await readyActions(ui);
 fail=true;await ui.run('refreshCalendarActions()');assert.match(ui.elements.get('actions-receipts').textContent,/Applied/);assert.equal(ui.run('actionsState.stale'),true);
 await ui.run("undoCalendarReceipt('receipt-a')");assert.deepEqual(calls,['/api/actions','/api/actions']);
 fail=false;await ui.run('refreshCalendarActions()');assert.equal(ui.run('actionsState.stale'),false);assert.equal(allButtons(ui.elements.get('actions-receipts'))[0].disabled,false);
});
