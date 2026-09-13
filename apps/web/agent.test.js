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


function intelligence(extra = {}) {
  return { revision:'revision-a', gmail_revision:'gmail-a', provider:'Anthropic', model:'claude-test', configured:true,
    messages:[{id:'mail-a',subject:'<b>Reservation</b>',excerpt:'Please arrive at 11:50.',source:{provider:'gmail'}}],
    result:null, constraints:[], metrics:{calls:0,limit:10,remaining_calls:10,input_tokens:null,output_tokens:null}, ...extra };
}
function proposal() { return {status:'proposed',reason_code:null,cached:false,proposal:{proposal_id:'proposal-a',event_id:'b',deadline:'2026-09-14T09:50:00Z',evidence_quote:'Please arrive at 11:50.',source:{provider:'gmail'},requires_confirmation:true}}; }
function planning() {
  const simulation = (risk) => ({samples:1000,llm_calls:0,assumptions:['Reused traffic, uncalibrated.'],events:[{event_id:'b',arrival:{p10:'2026-09-14T09:45:00Z',p50:'2026-09-14T09:50:00Z',p90:'2026-09-14T10:15:00Z'},late_arrival_probability:risk}]});
  return {status:'plans_found',baseline:simulation(.71),plans:[{id:'p-a',operations:[{event_id:'a',before:{planned_start:'2026-09-14T08:00:00Z',planned_end:'2026-09-14T09:00:00Z'},after:{planned_start:'2026-09-14T07:30:00Z',planned_end:'2026-09-14T08:30:00Z'}}],simulation:simulation(.24),target_risk_before:.71,target_risk_after:.24,shift_minutes:-30,requires_approval:true}],search:{evaluated_candidates:9,truncated:false,rejected_reasons:[]},llm_calls:0};
}
async function prepared(ui) { session(ui); await ui.run('refreshForecast()'); await ui.run('refreshIntelligence()'); }

test('intelligence refresh reads cached excerpts only and leaves unknown tokens unknown', async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(path==='/api/intelligence'?intelligence():fixture());});
 await prepared(ui);
 assert.deepEqual(calls.map(x=>x.path),['/api/live/scenario','/api/intelligence']);
 assert.equal(calls[1].options.method,undefined);
 assert.match(ui.elements.get('intelligence-metrics').textContent,/unknown/i);
 assert.match(ui.elements.get('intelligence-consent').textContent,/Anthropic/);
 assert.equal(ui.elements.get('intelligence-extract').disabled,true);
});

test('only explicit extraction sends selected message identifiers and consent with CSRF',async()=>{
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(path==='/api/live/scenario'?fixture():intelligence(path.endsWith('/extract')?{result:proposal(),metrics:{calls:1,limit:10,remaining_calls:9,input_tokens:21,output_tokens:12}}:{}));});await prepared(ui);
 await ui.run('extractIntelligence()');assert.equal(calls.length,2);
 ui.run("intelligenceState.messageId='mail-a'; intelligenceState.eventIds=['b']; renderIntelligence()");
 assert.match(ui.elements.get('intelligence-excerpt').textContent,/Please arrive/);
 await ui.elements.get('intelligence-extract').listeners.click();
 assert.deepEqual(JSON.parse(calls[2].options.body),{revision:'revision-a',gmail_revision:'gmail-a',message_id:'mail-a',event_ids:['b'],consent:true});
 assert.equal(calls[2].options.headers['X-Aeon-CSRF'],'csrf');
 assert.match(ui.elements.get('intelligence-result').textContent,/Please arrive at 11:50/);
 assert.equal(ui.elements.get('intelligence-confirm').disabled,false);
});

test('confirmation sends server proposal id only and invalidates old planning',async()=>{
 const calls=[];let confirmed=false;
 const ui=app(async(path,options)=>{calls.push({path,options});if(path.endsWith('/confirm'))confirmed=true;return response(path==='/api/live/scenario'?fixture({revision:confirmed?'revision-b':'revision-a',planning:confirmed?null:planning()}):intelligence({revision:confirmed?'revision-b':'revision-a',result:confirmed?null:proposal(),constraints:confirmed?[{id:'c',event_id:'b',deadline:'2026-09-14T09:50:00Z'}]:[]}));});
 await prepared(ui);await ui.run('confirmIntelligence()');
 const request=calls.find(x=>x.path.endsWith('/confirm'));assert.deepEqual(JSON.parse(request.options.body),{revision:'revision-a',gmail_revision:'gmail-a',proposal_id:'proposal-a'});
 assert.equal(ui.run('forecastState.snapshot?.planning ?? null'),null);assert.equal(ui.run('forecastState.preview'),null);
 assert.match(ui.elements.get('intelligence-constraints').textContent,/Second/);
});

test('missing provider, exhausted budget and more than twenty selected events never extract',async()=>{
 let current=intelligence({configured:false});const calls=[];const ui=app(async(path)=>{calls.push(path);return response(path==='/api/live/scenario'?fixture():current);});await prepared(ui);
 ui.run("intelligenceState.messageId='mail-a'; intelligenceState.eventIds=['b']");await ui.run('extractIntelligence()');
 current=intelligence({metrics:{calls:10,limit:10,remaining_calls:0,input_tokens:null,output_tokens:null}});await ui.run('refreshIntelligence()');await ui.run('extractIntelligence()');
 current=intelligence();await ui.run('refreshIntelligence()');ui.run("intelligenceState.eventIds=Array(21).fill('b')");await ui.run('extractIntelligence()');
 assert.equal(calls.some(x=>x.endsWith('/extract')),false);
});

test('Gmail invalidation discards a late extraction and clears plans and preview',async()=>{
 let release;const ui=app(async(path)=>path.endsWith('/extract')?await new Promise(resolve=>{release=resolve;}):response(path==='/api/live/scenario'?fixture({planning:planning()}):intelligence()));await prepared(ui);
 ui.run("intelligenceState.messageId='mail-a'; intelligenceState.eventIds=['b']; previewLivePlan('p-a')");const pending=ui.run('extractIntelligence()');
 ui.run("liveState.providers.gmail={authorized:false,data:null}; syncForecastConnection()");release(response(intelligence({result:proposal()})));await pending;
 assert.equal(ui.run('intelligenceState.snapshot'),null);assert.equal(ui.run('forecastState.preview'),null);assert.equal(ui.run('forecastState.snapshot'),null);
});

test('live plans require explicit private blocks and window, then preview and restore without a request',async()=>{
 const calls=[];const day=fixture();day.calendar.events[0].private=true;day.calendar.events[1].private=false;
 const ui=app(async(path,options)=>{calls.push({path,options});return response({...day,planning:path.endsWith('/plans')?planning():null});});session(ui);await ui.run('refreshForecast()');
 ui.run("forecastState.selection={target:'b',movable:['b'],start:'2026-09-14T08:00',end:'2026-09-14T18:00'}");await ui.run('findLivePlans()');assert.equal(calls.length,1);
 ui.run("forecastState.selection.movable=['a']");await ui.run('findLivePlans()');
 assert.deepEqual(JSON.parse(calls[1].options.body),{revision:'revision-a',target_event_id:'b',movable_event_ids:['a'],window:{start:'2026-09-14T06:00:00.000Z',end:'2026-09-14T16:00:00.000Z'}});
 const baseline=ui.elements.get('forecast-result').textContent;ui.run("previewLivePlan('p-a')");assert.match(ui.elements.get('forecast-result').textContent,/24/);assert.equal(ui.elements.get('live-preview-banner').hidden,false);
 ui.run('closeLivePreview()');assert.equal(ui.elements.get('forecast-result').textContent,baseline);assert.equal(calls.length,2);assert.match(ui.elements.get('live-plan-list').textContent,/-30/);
});

test('new Calendar revision erases old preview and clears old extraction',async()=>{
 const ui=app(async(path)=>response(path==='/api/live/scenario'?fixture({planning:planning()}):intelligence({result:proposal()})));await prepared(ui);ui.run("previewLivePlan('p-a')");
 ui.run(`acceptForecast(${JSON.stringify(fixture({revision:'revision-b'}))}); renderForecast()`);
 assert.equal(ui.run('forecastState.preview'),null);assert.equal(ui.run('intelligenceState.snapshot'),null);assert.equal(ui.elements.get('live-preview-banner').hidden,true);
});

test('failed extraction never replays the POST and requires refresh if even the status GET fails',async()=>{
 const calls=[];let fail=false;const ui=app(async(path)=>{calls.push(path);if(fail)throw new Error('offline');return response(path==='/api/live/scenario'?fixture():intelligence());});await prepared(ui);
 ui.run("intelligenceState.messageId='mail-a'; intelligenceState.eventIds=['b']");fail=true;await ui.run('extractIntelligence()');await ui.run('extractIntelligence()');
 assert.equal(calls.filter(x=>x.endsWith('/extract')).length,1);assert.equal(ui.elements.get('intelligence-confirm').disabled,true);assert.match(ui.elements.get('intelligence-notice').textContent,/cannot be reached/);
});

test('unchanged event checkboxes bind to the current selection after a server revision changes',async()=>{
 const day=fixture();day.calendar.events[0].private=true;
 const ui=app(async()=>response(day));session(ui);await ui.run('refreshForecast()');
 const input=ui.elements.get('live-movable-events').agentInputs[0];
 ui.run(`acceptForecast(${JSON.stringify({...day,revision:'revision-b'})}); renderForecast()`);
 input.checked=true;input.listeners.change();
 assert.deepEqual(JSON.parse(ui.run('JSON.stringify(forecastState.selection.movable)')),['a']);
});

test('reset removes confirmed constraints and preserves model counters without model replay',async()=>{
 const calls=[];let reset=false;
 const ui=app(async(path,options)=>{calls.push({path,options});if(path.endsWith('/reset'))reset=true;return response(path==='/api/live/scenario'?fixture({revision:reset?'revision-b':'revision-a'}):intelligence({revision:reset?'revision-b':'revision-a',constraints:reset?[]:[{id:'c',event_id:'b',deadline:'2026-09-14T09:50:00Z'}],metrics:{calls:4,limit:10,remaining_calls:6,input_tokens:null,output_tokens:43}}));});await prepared(ui);
 await ui.run('resetIntelligence()');assert.deepEqual(JSON.parse(calls.find(x=>x.path.endsWith('/reset')).options.body),{revision:'revision-a'});
 assert.equal(ui.elements.get('intelligence-constraints').textContent,'');assert.match(ui.elements.get('intelligence-metrics').textContent,/4 call/);assert.equal(calls.some(x=>x.path.endsWith('/extract')),false);
});

test('abstention displays an honest empty result and cannot confirm a constraint',async()=>{
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(path==='/api/live/scenario'?fixture():intelligence({result:{status:'abstained',reason_code:'NO_DEADLINE',cached:false,proposal:null}}));});await prepared(ui);
 await ui.run('confirmIntelligence()');assert.equal(calls.length,2);assert.match(ui.elements.get('intelligence-result').textContent,/No usable arrival time/);assert.equal(ui.elements.get('intelligence-confirm').hidden,true);
});

test('Gmail forget started during plan search discards its late response',async()=>{
 let release;const ui=app(async(path)=>path==='/api/live/plans'?await new Promise(resolve=>{release=resolve;}):path==='/api/live/scenario'?response(fixture()):path==='/api/connections'?response(connections()):response({forgotten:true,provider:'gmail'}));session(ui);await ui.run('refreshForecast()');
 const pending=ui.run("forecastAction('/api/live/plans',{target_event_id:'b',movable_event_ids:['a'],window:{start:'2026-09-14T00:00:00Z',end:'2026-09-14T20:00:00Z'}})");
 await ui.run("connectionAction('gmail','forget')");release(response(fixture({planning:planning()})));await pending;
 assert.equal(ui.run('forecastState.snapshot'),null);assert.equal(ui.run('forecastState.preview'),null);assert.equal(ui.elements.get('live-plan-list').textContent,'');
});

test('no better live plan preserves its real baseline and offers no preview',async()=>{
 const p=planning();p.status='no_better_plan';p.plans=[];
 const ui=app(async()=>response(fixture({planning:p})));session(ui);await ui.run('refreshForecast()');
 assert.match(ui.elements.get('live-plan-status').textContent,/No improvement/);assert.equal(ui.elements.get('live-plan-list').textContent,'');assert.match(ui.elements.get('forecast-result').textContent,/71/);
});

test('refreshing excerpts can load the imported Calendar cache without a Google read',async()=>{
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(path==='/api/live/scenario'?fixture():intelligence());});session(ui);
 await ui.run('refreshIntelligence()');
 assert.equal(ui.run('calendarEvents().length'),2);assert.deepEqual(calls,['/api/live/scenario','/api/intelligence']);
});

test('a failed live refresh exits preview before marking the reference stale',async()=>{
 let fail=false;const ui=app(async()=>{if(fail)throw new Error('offline');return response(fixture({planning:planning()}));});session(ui);await ui.run('refreshForecast()');ui.run("previewLivePlan('p-a')");
 fail=true;await ui.run('refreshForecast()');assert.equal(ui.run('forecastState.preview'),null);assert.equal(ui.elements.get('live-preview-banner').hidden,true);assert.equal(ui.run('forecastState.stale'),true);
});

test('plan comparison uses the returned planning baseline instead of an older simulation',async()=>{
 const p=planning();const older={...p.baseline,events:p.baseline.events.map(event=>({...event,late_arrival_probability:.99}))};
 const ui=app(async()=>response(fixture({planning:p,result:older})));session(ui);await ui.run('refreshForecast()');
 assert.match(ui.elements.get('forecast-result').textContent,/71/);assert.doesNotMatch(ui.elements.get('forecast-result').textContent,/99/);
 ui.run("previewLivePlan('p-a'); closeLivePreview()");assert.match(ui.elements.get('forecast-result').textContent,/71/);
});
