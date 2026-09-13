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
  const document = { body: new Element(), createElement: (tag) => { const el = new Element(); el.tagName = tag; return el; }, createTextNode: (text) => { const el = new Element(); el.textContent = text; return el; }, querySelectorAll: () => [], getElementById: (id) => elements.get(id) || null };
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

test('forecast refresh only reads server state and keeps demo separate', async () => {
  const calls=[]; const ui=app(async(path,options)=>{calls.push({path,options});return response(fixture());});session(ui);
  await ui.run('refreshForecast()');
  assert.deepEqual(calls.map(x=>x.path),['/api/live/scenario']);assert.equal(calls[0].options.method,undefined);
  assert.equal(ui.run('state.reference'),null);assert.equal(ui.run('forecastState.snapshot.revision'),'revision-a');
  assert.match(ui.elements.get('forecast-pairs').textContent,/<script>private<\/script>/);
});

test('explicit same-location sends only revision pair and declaration, never calendar data', async()=>{
  const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(fixture());});session(ui);
  await ui.run('refreshForecast()');await ui.run("forecastTravel(0,'same_location')");
  assert.deepEqual(JSON.parse(calls[1].options.body),{revision:'revision-a',from_event_id:'a',to_event_id:'b',kind:'same_location'});
  assert.equal(calls[1].options.headers['X-Aeon-CSRF'],'csrf');assert.equal(calls.length,2);
});

test('Routes requires explicit addresses and available budget before sending',async()=>{
  const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(fixture());});session(ui);await ui.run('refreshForecast()');
  await ui.run("forecastTravel(0,'google_routes','','')");assert.equal(calls.length,1);
  await ui.run("forecastTravel(0,'google_routes','10 rue A','20 rue B')");
  const sent=JSON.parse(calls[1].options.body);assert.equal(sent.origin_address,'10 rue A');assert.equal(sent.destination_address,'20 rue B');assert.equal(sent.departure_time,undefined);
  ui.run('forecastState.snapshot.routes.remaining=0');await ui.run("forecastTravel(0,'google_routes','A','B')");assert.equal(calls.length,2);
});

test('reset preserves server budget and simulation renders returned probabilities as text',async()=>{
 const calculated=fixture({status:'simulated',result:{events:[{event_id:'b',arrival:{p10:'2026-09-14T09:50:00Z',p50:'2026-09-14T09:55:00Z',p90:'2026-09-14T10:15:00Z'},late_arrival_probability:0.237}],samples:1000,llm_calls:0,assumptions:['<b>non calibrated</b>']}});
 const calls=[];const ui=app(async(path,options)=>{calls.push({path,options});return response(path.endsWith('/reset')?fixture({revision:'revision-b',routes:{configured:true,attempts:4,remaining:16,limit:20}}):calculated);});session(ui);await ui.run('refreshForecast()');
 assert.match(ui.elements.get('forecast-result').textContent,/23\.7/);assert.match(ui.elements.get('forecast-assumptions').textContent,/<b>non calibrated<\/b>/);
 await ui.run("forecastAction('/api/live/reset')");assert.deepEqual(JSON.parse(calls[1].options.body),{revision:'revision-a'});assert.equal(ui.run('forecastState.snapshot.routes.remaining'),16);assert.equal(ui.run('forecastState.snapshot.result'),null);
});

test('failed action refreshes status only, preserves drafts and never retries the POST',async()=>{
 const calls=[]; const ui=app(async(path,options)=>{calls.push(path);return path==='/api/live/travel'?response({error:{code:'routes_failed',message:'Route unavailable.'}},502):response(fixture());});session(ui);await ui.run('refreshForecast()');
 await ui.run("forecastTravel(0,'google_routes','A','B')");assert.equal(calls.filter(x=>x==='/api/live/travel').length,1);assert.match(ui.elements.get('forecast-notice').textContent,/Route unavailable/);
});

test('connection invalidation rejects late forecast responses and erases results',async()=>{
 let release;const ui=app(()=>new Promise(resolve=>{release=resolve;}));session(ui);const pending=ui.run('refreshForecast()');
 ui.run("liveState.csrfToken=null; syncForecastConnection()");release(response(fixture({status:'simulated'})));await pending;
 assert.equal(ui.run('forecastState.snapshot'),null);assert.equal(ui.elements.get('forecast-simulate').disabled,true);
});

test('server revision conflicts require refresh and cannot replay stale mutation',async()=>{
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(path==='/api/live/scenario'?fixture():{error:{code:'revision_conflict',message:'Calendar changed.'}},path==='/api/live/scenario'?200:409);});session(ui);await ui.run('refreshForecast()');await ui.run("forecastAction('/api/live/simulate')");
 assert.equal(calls.filter(x=>x==='/api/live/simulate').length,1);assert.match(ui.elements.get('forecast-notice').textContent,/Calendar changed/);
});

test('incomplete state explains missing travel and never invokes simulation',async()=>{
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(fixture({status:'incomplete',issues:[{code:'MISSING_TRAVEL_EDGE',event_ids:['a','b']}]}));});session(ui);
 await ui.run('refreshForecast()');await ui.run("forecastAction('/api/live/simulate')");
 assert.deepEqual(calls,['/api/live/scenario']);assert.equal(ui.elements.get('forecast-simulate').disabled,true);assert.match(ui.elements.get('forecast-issues').textContent,/missing travel/);
});

test('an unavailable key blocks Routes without blocking explicit same-location declarations',async()=>{
 const calls=[];const ui=app(async(path)=>{calls.push(path);return response(fixture({routes:{configured:false,attempts:0,remaining:20,limit:20}}));});session(ui);await ui.run('refreshForecast()');
 await ui.run("forecastTravel(0,'google_routes','A','B')");assert.equal(calls.length,1);
 await ui.run("forecastTravel(0,'same_location')");assert.deepEqual(calls,['/api/live/scenario','/api/live/travel']);
});

test('a failed refresh marks old results stale and blocks mutations until refresh succeeds',async()=>{
 let fail=false;const calls=[];const ui=app(async(path)=>{calls.push(path);if(fail)throw new Error('offline');return response(fixture());});session(ui);await ui.run('refreshForecast()');fail=true;await ui.run('refreshForecast()');
 await ui.run("forecastAction('/api/live/simulate')");assert.deepEqual(calls,['/api/live/scenario','/api/live/scenario']);assert.equal(ui.run('forecastState.stale'),true);assert.equal(ui.elements.get('forecast-simulate').disabled,true);
 fail=false;await ui.run('refreshForecast()');assert.equal(ui.run('forecastState.stale'),false);
});

test('a Calendar reconnect invalidates a pending mutation response without replay',async()=>{
 let release;const calls=[];const ui=app(async(path)=>{calls.push(path);if(path==='/api/live/travel')return await new Promise(resolve=>{release=resolve;});return response(fixture());});session(ui);await ui.run('refreshForecast()');
 const pending=ui.run("forecastTravel(0,'same_location')");ui.run("liveState.providers.google_calendar={authorized:false}; syncForecastConnection()");
 release(response(fixture({status:'simulated',result:{events:[],samples:1000,llm_calls:0,assumptions:[]}})));await pending;
 assert.equal(ui.run('forecastState.snapshot'),null);assert.equal(ui.elements.get('forecast-result').textContent,'');assert.equal(calls.filter(x=>x==='/api/live/travel').length,1);
});


test('Routes evidence counts only current Google observations and clears after reset', async () => {
  let current = fixture();
  const ui = app(async () => response(current)); session(ui);
  await ui.run('refreshForecast()');
  assert.match(ui.elements.get('forecast-budget').textContent, /no Google route observed/);
  const pair = current.travel_pairs[0];
  const travel = {kind: 'google_routes', duration_minutes: {min: 16, mode: 20, max: 28}, source: {assumption: 'Point-in-time observation.'}, observed_at: '2026-09-14T08:00:00Z'};
  current = fixture({travel_pairs: [{...pair, travel}, {...pair, travel: {...travel, kind: 'same_location'}}]});
  await ui.run('refreshForecast()');
  assert.match(ui.elements.get('forecast-budget').textContent, /1 Google route\(s\) observed/);
  assert.doesNotMatch(ui.elements.get('forecast-budget').textContent, /without evidence of a read/);
  current = fixture({revision: 'revision-b', routes: {configured: true, attempts: 1, limit: 20, remaining: 19}});
  await ui.run("forecastAction('/api/live/reset')");
  assert.match(ui.elements.get('forecast-budget').textContent, /no Google route observed/);
  assert.match(ui.elements.get('forecast-budget').textContent, /1 attempt/);
});

function descendants(element, tag) {
  return element.children.flatMap((child) => [...(child.tagName === tag ? [child] : []), ...descendants(child, tag)]);
}
function routeForm(ui) {
  const form = descendants(ui.elements.get('forecast-pairs'), 'form')[0];
  return { form, service: descendants(form, 'select')[0], inputs: descendants(form, 'input'), submit: descendants(form, 'button')[0] };
}
function openFixture(extra = {}) {
  return fixture({ routes: { configured: false, open_routing: true, attempts: 0, limit: 20, remaining: 20 }, ...extra });
}

test('OSRM is the default without a Google key and only submitting sends explicit addresses', async () => {
  const calls = []; const ui = app(async (path, options) => { calls.push({ path, options }); return response(openFixture()); }); session(ui);
  await ui.run('refreshForecast()');
  const { form, service, inputs, submit } = routeForm(ui);
  assert.equal(service.value, 'osrm'); assert.equal(submit.disabled, false);
  assert.deepEqual(service.children.map((option) => option.value), ['osrm']);
  assert.ok(inputs.every((input) => !input.disabled && input.autocomplete === 'off'));
  inputs[0].value = ' Gare de Lyon, Paris '; inputs[1].value = ' Gare du Nord, Paris ';
  inputs.forEach((input) => input.listeners.input());
  assert.equal(calls.length, 1);
  await form.listeners.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(calls[1].options.body), { revision: 'revision-a', from_event_id: 'a', to_event_id: 'b', kind: 'osrm', origin_address: 'Gare de Lyon, Paris', destination_address: 'Gare du Nord, Paris' });
  assert.equal(calls[1].options.headers['X-Aeon-CSRF'], 'csrf');
  assert.equal(calls.length, 2);
});

test('Google remains an explicit optional choice and changing service never requests a route', async () => {
  const calls = []; const ui = app(async (path, options) => { calls.push({ path, options }); return response(openFixture({ routes: { configured: true, open_routing: true, attempts: 0, limit: 20, remaining: 20 } })); }); session(ui);
  await ui.run('refreshForecast()');
  const controls = routeForm(ui);
  assert.equal(controls.service.value, 'osrm');
  assert.deepEqual(controls.service.children.map((option) => option.value), ['osrm', 'google_routes']);
  controls.inputs[0].value = 'Place A'; controls.inputs[1].value = 'Place B';
  controls.inputs.forEach((input) => input.listeners.input());
  controls.service.value = 'google_routes'; controls.service.listeners.change();
  assert.equal(calls.length, 1);
  const updated = routeForm(ui);
  assert.match(updated.submit.textContent, /Google Routes/);
  assert.equal(updated.inputs[0].value, 'Place A');
  await updated.form.listeners.submit({ preventDefault() {} });
  assert.equal(JSON.parse(calls[1].options.body).kind, 'google_routes');
  assert.equal(calls.length, 2);
});

test('OSRM respects availability, explicit addresses and the shared attempt budget', async () => {
  const calls = []; const ui = app(async (path) => { calls.push(path); return response(openFixture()); }); session(ui);
  await ui.run('refreshForecast()');
  await ui.run("forecastTravel(0, 'osrm', '', 'Place B')");
  await ui.run("forecastTravel(0, 'osrm', 'A'.repeat(501), 'Place B')");
  ui.run('forecastState.snapshot.routes.open_routing = false');
  await ui.run("forecastTravel(0, 'osrm', 'Place A', 'Place B')");
  ui.run('forecastState.snapshot.routes.open_routing = true; forecastState.snapshot.routes.remaining = 0; renderForecast()');
  assert.equal(routeForm(ui).submit.disabled, true);
  await ui.run("forecastTravel(0, 'osrm', 'Place A', 'Place B')");
  assert.deepEqual(calls, ['/api/live/scenario']);
  await ui.run("forecastTravel(0, 'same_location')");
  assert.deepEqual(calls, ['/api/live/scenario', '/api/live/travel']);
});

test('OSRM failure preserves chosen service and drafts without retry or Google fallback', async () => {
  const calls = []; const ui = app(async (path, options) => {
    calls.push({ path, options });
    return path === '/api/live/travel' ? response({ error: { code: 'routing_failed', message: 'Route unavailable.' } }, 502)
      : response(openFixture({ routes: { configured: true, open_routing: true, attempts: calls.length > 1 ? 1 : 0, limit: 20, remaining: calls.length > 1 ? 19 : 20 } }));
  }); session(ui); await ui.run('refreshForecast()');
  await ui.run("forecastTravel(0, 'osrm', 'Place A', 'Place B')");
  assert.deepEqual(calls.map((call) => call.path), ['/api/live/scenario', '/api/live/travel', '/api/live/scenario']);
  assert.equal(JSON.parse(calls[1].options.body).kind, 'osrm');
  const controls = routeForm(ui);
  assert.equal(controls.service.value, 'osrm'); assert.equal(controls.inputs[0].value, 'Place A'); assert.equal(controls.inputs[1].value, 'Place B');
  assert.equal(controls.submit.disabled, false);
  assert.match(ui.elements.get('forecast-budget').textContent, /1 attempt/);
  assert.match(ui.elements.get('forecast-notice').textContent, /Route unavailable/);
});

test('OSRM results identify map matches safely and never count as Google observations', async () => {
  const pair = fixture().travel_pairs[0];
  const travel = { kind: 'osrm', duration_minutes: { min: 8, mode: 10, max: 14 }, source: { provider: 'osrm', synthetic: false, assumption: 'Estimate without live traffic.' }, observed_at: '2026-09-14T08:00:00Z', origin_label: '<img src=x onerror=alert(1)>', destination_label: 'Public square' };
  const ui = app(async () => response(openFixture({ travel_pairs: [{ ...pair, travel }] }))); session(ui); await ui.run('refreshForecast()');
  const pairs = ui.elements.get('forecast-pairs');
  assert.match(pairs.textContent, /OSRM.*without live traffic/);
  assert.match(pairs.textContent, /Matches to verify/);
  assert.match(pairs.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(pairs.textContent, /mode 10/);
  assert.equal(descendants(pairs, 'img').length, 0);
  assert.match(ui.elements.get('forecast-budget').textContent, /1 OSRM route\(s\)/);
  assert.match(ui.elements.get('forecast-budget').textContent, /no Google route observed/);
});

test('only a confirmed route cache hit claims no new request', async () => {
  const pair = fixture().travel_pairs[0];
  const travel = { kind: 'osrm', cached: false, duration_minutes: { min: 8, mode: 10, max: 14 }, source: { provider: 'osrm', synthetic: false, assumption: 'Without live traffic.' }, observed_at: '2026-09-14T08:00:00Z', origin_label: 'Public place A', destination_label: 'Public place B' };
  let current = openFixture({ travel_pairs: [{ ...pair, travel }] });
  const ui = app(async () => response(current)); session(ui); await ui.run('refreshForecast()');
  assert.doesNotMatch(ui.elements.get('forecast-pairs').textContent, /Cached route/);
  current = openFixture({ travel_pairs: [{ ...pair, travel: { ...travel, cached: true } }] });
  await ui.run('refreshForecast()');
  assert.match(ui.elements.get('forecast-pairs').textContent, /Cached route · no new request/);
  assert.match(ui.elements.get('forecast-pairs').textContent, /Observed on 14 Sept 2026, 10:00/);
});
