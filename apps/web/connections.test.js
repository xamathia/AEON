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

test('opening connections creates a session and loads status without a private read', async () => {
  const calls = [];
  const ui = app(async (path, options) => { calls.push({ path, options }); return response(path === '/api/session' ? { csrf_token: 'test-csrf', canonical_origin: 'http://127.0.0.1:8787', expires_in: 3600 } : connections()); });
  await ui.run('initConnections()');
  assert.deepEqual(calls.map((call) => call.path), ['/api/session', '/api/connections', '/api/actions']);
  assert.equal(ui.run('liveState.csrfToken'), 'test-csrf');
});

test('explicit writes send the session cookie and CSRF header without a token in the URL or body', async () => {
  let captured;
  const ui = app(async (path, options) => { captured = { path, options }; return response({ forgotten: true, provider: 'gmail' }); });
  ui.run("liveState.csrfToken = 'test-csrf'");
  await ui.run("liveApi('/api/oauth/gmail/forget', {})");
  assert.equal(captured.path, '/api/oauth/gmail/forget');
  assert.equal(captured.options.credentials, 'same-origin');
  assert.equal(captured.options.mode, 'same-origin');
  assert.equal(captured.options.headers['Content-Type'], 'application/json');
  assert.equal(captured.options.headers['X-Aeon-CSRF'], 'test-csrf');
  assert.equal(captured.options.body, '{}');
});

test('Calendar converts the selected Paris wall time to an explicit instant and rejects a missing DST hour', () => {
  const ui = app(async () => response({}));
  assert.equal(ui.run("calendarInstant('2026-09-14T09:30', 'Europe/Paris')"), '2026-09-14T07:30:00.000Z');
  assert.throws(() => ui.run("calendarInstant('2026-03-29T02:30', 'Europe/Paris')"), /does not exist/);
});

test('read timestamps from the API are Unix seconds and display their 2026 date', () => {
  const ui = app(async () => response({}));
  const timestamp = Date.parse('2026-09-14T08:00:00Z') / 1000;
  const rendered = ui.run(`readDate(${timestamp}, 'Europe/Paris')`);
  assert.match(rendered, /14 Sept 2026/);
  assert.match(rendered, /10:00/);
});

test('Calendar rejects reversed periods, unknown timezones, and horizons beyond seven days', () => {
  const ui = app(async () => response({}));
  ui.elements.get('calendar-start').value = '2026-09-14T09:00';
  ui.elements.get('calendar-end').value = '2026-09-21T09:01';
  ui.elements.get('calendar-timezone').value = 'Europe/Paris';
  assert.throws(() => ui.run('calendarRequest()'), /168 hours/);
  ui.elements.get('calendar-end').value = '2026-09-14T08:59';
  assert.throws(() => ui.run('calendarRequest()'), /168 hours/);
  ui.elements.get('calendar-end').value = '2026-09-21T09:00';
  assert.equal(ui.run('calendarRequest().time_max'), '2026-09-21T07:00:00.000Z');
  ui.elements.get('calendar-timezone').value = 'not-a-zone';
  assert.throws(() => ui.run('calendarRequest()'), /time zone is unknown/);
});

test('session expiration blocks further POSTs until an explicit session retry', async () => {
  const calls = [];
  const ui = app(async (path) => { calls.push(path); return response({ error: { code: 'session_expired', message: 'Session expired.' } }, 401); });
  ui.run("liveState.csrfToken = 'test-csrf'");
  await assert.rejects(ui.run("liveApi('/api/gmail/read', {query: 'test'})"), /Session expired/);
  assert.equal(ui.run('liveState.csrfToken'), null);
  assert.equal(ui.run('liveState.sessionError'), true);
  await assert.rejects(ui.run("liveApi('/api/gmail/read', {query: 'test'})"), /Restore/);
  assert.deepEqual(calls, ['/api/gmail/read']);
});

test('failed Calendar reads retain coherent cached data and clearly identify the failed attempt', async () => {
  const cached = { synthetic: false, read_at: '2026-09-14T08:00:00Z', complete: true, timezone: 'Europe/Paris', horizon: { start: '2026-09-14T00:00:00Z', end: '2026-09-15T00:00:00Z' }, events: [{ title: '<img src=x onerror=alert(1)>', planned_start: '2026-09-14T10:00:00Z', planned_end: '2026-09-14T11:00:00Z', private: true }], excluded_count: 0, deleted_count: 0 };
  let failed = false;
  const ui = app(async (path) => {
    if (path === '/api/calendar/read') { failed = true; return response({ error: { code: 'read_failed', message: 'Read unavailable.' } }, 502); }
    const calendar = provider('google_calendar', { authorized: true, data: cached, read: { status: failed ? 'failed' : 'succeeded', error: failed ? { message: 'Read unavailable.' } : null } });
    return response(connections([calendar, provider('gmail')]));
  });
  ui.run("liveState.csrfToken = 'test-csrf'");
  await ui.run('loadConnections()');
  await ui.run("connectionAction('google_calendar', 'read', {time_min: '2026-09-14T00:00:00Z', time_max: '2026-09-15T00:00:00Z'})");
  assert.equal(ui.run('liveState.providers.google_calendar.data.events[0].title'), cached.events[0].title);
  assert.match(ui.elements.get('google_calendar-data').textContent, /previous read/);
  assert.match(ui.elements.get('google_calendar-data').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(ui.elements.get('google_calendar-feedback').textContent, /Read unavailable/);
});

test('incomplete Gmail results show the query, safe excerpts, and no inferred constraint', async () => {
  const data = { synthetic: false, read_at: '2026-09-14T08:00:00Z', complete: false, query: 'subject:reservation', messages: [{ subject: '<script>unsafe()</script>', from: 'sender@example.test', date: 'Mon, 14 Sep 2026', excerpt: 'Arrival at 19:00 <b>received text</b>', truncated: true }] };
  const ui = app(async () => response(connections([provider('google_calendar'), provider('gmail', { authorized: true, data, read: { status: 'incomplete', error: null } })])));
  await ui.run('loadConnections()');
  const visible = ui.elements.get('gmail-data').textContent;
  assert.match(visible, /Incomplete read/);
  assert.match(visible, /subject:reservation/);
  assert.match(visible, /<script>unsafe\(\)<\/script>/);
  assert.match(visible, /Truncated excerpt/);
  assert.match(visible, /No constraints inferred/);
  assert.equal(ui.run('state.demo'), null);
});

test('forgetting Gmail reloads state and leaves Calendar authorization and cache intact', async () => {
  const calls = [];
  const calendar = provider('google_calendar', { authorized: true, data: { complete: true, events: [], horizon: {} }, read: { status: 'succeeded', error: null } });
  let forgotten = false;
  const ui = app(async (path) => {
    calls.push(path);
    if (path.endsWith('/forget')) { forgotten = true; return response({ forgotten: true, provider: 'gmail' }); }
    return response(connections([calendar, provider('gmail', { authorized: !forgotten })]));
  });
  ui.run("liveState.csrfToken = 'test-csrf'");
  await ui.run('loadConnections()');
  await ui.run("connectionAction('gmail', 'forget')");
  assert.deepEqual(calls, ['/api/connections', '/api/actions', '/api/oauth/gmail/forget', '/api/connections', '/api/actions']);
  assert.equal(ui.run('liveState.providers.gmail.authorized'), false);
  assert.equal(ui.run('liveState.providers.google_calendar.authorized'), true);
  assert.equal(ui.run('liveState.providers.google_calendar.data.complete'), true);
});

test('empty and oversized Gmail searches never request a private read', () => {
  const calls = [];
  const ui = app(async (path) => { calls.push(path); return response({}); });
  ui.run("liveState.csrfToken = 'test-csrf'");
  for (const value of ['   ', 'a'.repeat(501)]) {
    ui.elements.get('gmail-query').value = value;
    ui.elements.get('gmail-read-form').listeners.submit({ preventDefault() {} });
  }
  assert.deepEqual(calls, []);
  assert.match(ui.elements.get('gmail-feedback').textContent, /1 to 500 characters/);
});

test('Connect opens genuine Google consent only after the explicit action', async () => {
  const calls = [];
  const authorizationUrl = 'https://accounts.google.com/o/oauth2/v2/auth?state=offline-test';
  const ui = app(async (path) => {
    calls.push(path);
    if (path === '/api/session') return response({ csrf_token: 'test-csrf', canonical_origin: 'http://127.0.0.1:8787' });
    if (path.endsWith('/begin')) return response({ authorization_url: authorizationUrl, expires_in: 300 });
    return response(connections());
  });
  await ui.run('initConnections()');
  assert.deepEqual(ui.navigations, []);
  await ui.run("connectionAction('gmail', 'connect')");
  assert.deepEqual(ui.navigations, [authorizationUrl]);
  assert.deepEqual(calls, ['/api/session', '/api/connections', '/api/actions', '/api/oauth/gmail/begin', '/api/connections', '/api/actions']);
});

test('Connect rejects an unexpected consent destination without navigating', async () => {
  const ui = app(async (path) => response(path.endsWith('/begin') ? { authorization_url: 'https://accounts.google.com.evil.test/consent' } : connections()));
  ui.run("liveState.csrfToken = 'test-csrf'");
  await ui.run('loadConnections()');
  await ui.run("connectionAction('gmail', 'connect')");
  assert.deepEqual(ui.navigations, []);
  assert.match(ui.elements.get('gmail-feedback').textContent, /Google consent address is invalid/);
});

test('a pending Calendar read leaves old data visible and Gmail independently usable', async () => {
  let finishRead;
  let completed = false;
  const oldData = { synthetic: false, complete: true, events: [{ title: 'Previous data' }], horizon: {} };
  const newData = { synthetic: false, complete: false, events: [{ title: 'New read' }], horizon: {} };
  const ui = app(async (path) => {
    if (path === '/api/calendar/read') return new Promise((resolve) => { finishRead = () => { completed = true; resolve(response({ provider: 'google_calendar', data: newData })); }; });
    return response(connections([provider('google_calendar', { authorized: true, data: completed ? newData : oldData, read: { status: completed ? 'incomplete' : 'succeeded', error: null } }), provider('gmail', { authorized: true })]));
  });
  ui.run("liveState.csrfToken = 'test-csrf'");
  await ui.run('loadConnections()');
  const request = ui.run("connectionAction('google_calendar', 'read', {})");
  assert.match(ui.elements.get('google_calendar-data').textContent, /Previous data/);
  assert.equal(ui.elements.get('calendar-read-fields').disabled, true);
  assert.equal(ui.elements.get('gmail-read-fields').disabled, false);
  finishRead();
  await request;
  assert.match(ui.elements.get('google_calendar-data').textContent, /New read/);
  assert.match(ui.elements.get('google_calendar-data').textContent, /Incomplete read/);
});

test('refreshing another source preserves the DOM and scroll target of unchanged Calendar results', async () => {
  const ui = app(async () => response(connections([provider('google_calendar', { authorized: true, data: { synthetic: false, complete: true, events: [{ title: 'Retained calendar' }], horizon: {} }, read: { status: 'succeeded', error: null } }), provider('gmail')])));
  await ui.run('loadConnections()');
  const previousNodes = ui.elements.get('google_calendar-data').children;
  await ui.run('loadConnections()');
  assert.equal(ui.elements.get('google_calendar-data').children, previousNodes);
});
