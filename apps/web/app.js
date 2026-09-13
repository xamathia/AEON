'use strict';
const $ = (id) => document.getElementById(id);
// A preview never replaces the last successful reference or its traffic setting.
const state = { demo: null, reference: null, normalBaseline: null, traffic: 'normal', planning: null, preview: null, plannerAvailable: null, planningError: null, busy: false, pending: null };
const names = { google_calendar: 'Google Calendar', gmail: 'Gmail', google_routes: 'Google Routes', user: 'User preference' };
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function clock(value) {
  return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: scenario().timezone }).format(new Date(value));
}
function percent(value) { return new Intl.NumberFormat('en-GB', { style: 'percent', maximumFractionDigits: 1 }).format(value); }
function number(value) { return new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 }).format(value); }
function scenario() { return state.preview?.scenario || state.reference?.scenario || state.demo; }
function simulation() { return state.preview?.simulation || state.reference?.simulation; }
function notice(message, type = '') { $('notice-text').textContent = message; $('notice').className = `notice ${type}`; }
async function api(path, body) {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Aeon-Request': 'demo-v1' }, body: JSON.stringify(body) };
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('The local server cannot be reached. Check that ÆON is running.'); }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('The server returned an unreadable response. Run the calculation again.'); }
  if (!response.ok) {
    const error = new Error(data.error?.message || 'The calculation is unavailable. Try again.');
    error.code = data.error?.code;
    throw error;
  }
  return data;
}
function target() { return scenario().events.find((event) => event.id === 'dinner'); }
function targetProjection() { return simulation()?.events.find((event) => event.event_id === target().id); }
function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle('busy', busy);
  $('analyze').disabled = busy || !scenario();
  $('incident').disabled = busy || !state.reference || state.traffic === 'incident';
  $('reset').disabled = busy || !state.reference;
  $('find-plans').disabled = busy || !state.reference;
  $('close-preview').disabled = busy;
  document.querySelectorAll('.preview-plan, .timeline-event, #event-details button').forEach((button) => { button.disabled = busy; });
  $('timeline').setAttribute('aria-busy', String(busy));
  $('plans-panel').setAttribute('aria-busy', String(state.pending === 'plans'));
}
function renderSignals() {
  const list = $('signal-list');
  list.replaceChildren();
  const currentScenario = scenario();
  const event = target();
  const deadline = currentScenario.constraints.find((c) => c.event_id === event.id);
  const route = currentScenario.travel_edges.find((edge) => edge.to_event_id === event.id);
  const meeting = currentScenario.events.find((e) => e.overrun_minutes.max > 0);
  const signals = [];
  if (deadline) signals.push(['✉', 'An earlier arrival', `The reservation asks for arrival at ${clock(deadline.deadline)}.`, 'Synthetic Gmail']);
  if (meeting) signals.push(['◷', 'A meeting that could run over', `The model assumes an overrun of ${meeting.overrun_minutes.min} to ${meeting.overrun_minutes.max} minutes.`, 'ÆON assumption']);
  if (route) signals.push(['↗', state.traffic === 'incident' ? 'A journey that takes longer' : 'Time between appointments', `The model explores journeys lasting ${route.duration_minutes.min} to ${route.duration_minutes.max} minutes.`, state.traffic === 'incident' ? 'Injected synthetic incident' : 'Synthetic Routes, ÆON distribution']);
  for (const [icon, title, text, source] of signals) {
    const row = node('div', 'signal');
    const glyph = node('span', 'signal-icon', icon); glyph.setAttribute('aria-hidden', 'true');
    const copy = node('div', 'signal-text');
    copy.append(node('strong', '', title), node('p', '', text), node('small', '', source));
    row.append(glyph, copy); list.append(row);
  }
}
function showEvent(event, projected) {
  const panel = $('event-details');
  panel.replaceChildren(); panel.hidden = false;
  const copy = node('div');
  copy.append(node('h2', '', event.title));
  const info = projected
    ? `Median arrival ${clock(projected.arrival.p50)}. Start ${clock(projected.start.p50)}, end ${clock(projected.end.p50)}. Risk of arriving after ${clock(projected.deadline)}: ${percent(projected.late_arrival_probability)}.`
    : `Planned from ${clock(event.planned_start)} to ${clock(event.planned_end)}. ${event.classification === 'flexible' ? 'Flexible block.' : 'Fixed commitment.'} Synthetic data.`;
  copy.append(node('p', '', info));
  const close = node('button', 'text-button', 'Close details');
  close.addEventListener('click', () => { panel.hidden = true; });
  panel.append(copy, close);
}
function renderTimeline() {
  const container = $('timeline'); container.replaceChildren();
  const events = scenario().events;
  const hour = 3600000;
  const start = Math.floor(new Date(events[0].planned_start).getTime() / hour) * hour - hour;
  const ends = events.map((e) => new Date(e.planned_end).getTime());
  if (simulation()) ends.push(...simulation().events.map((e) => new Date(e.end.p90).getTime()));
  const end = Math.ceil(Math.max(...ends) / hour) * hour + hour;
  const position = (date) => (new Date(date).getTime() - start) / (end - start) * 100;
  for (let time = start; time < end; time += hour / 2) {
    const top = `${position(time)}%`;
    const line = node('div', time % hour === 0 ? 'gridline' : 'gridline halfline'); line.style.top = top; container.append(line);
    if (time % hour === 0) { const label = node('span', 'time-label', clock(time)); label.style.top = top; container.append(label); }
  }
  const planned = node('div', 'timeline-lane'); const shadow = node('div', 'timeline-lane shadow');
  container.append(planned, shadow);
  function block(event, projected) {
    const beginning = projected ? projected.start.p50 : event.planned_start;
    const ending = projected ? projected.end.p50 : event.planned_end;
    const duration = position(ending) - position(beginning);
    const kind = event.classification === 'flexible' ? 'focus-event' : event.id === target().id ? 'dinner-event' : '';
    const button = node('button', `timeline-event ${kind} ${projected ? 'projected' : ''} ${duration < 9 ? 'compact' : ''}`);
    button.style.top = `${position(beginning)}%`; button.style.height = `${duration}%`;
    button.append(node('span', 'event-time', `${clock(beginning)} – ${clock(ending)}`), node('span', 'event-title', event.title));
    const tag = projected ? 'Median model times' : event.classification === 'flexible' ? 'Personal · flexible' : 'Fixed commitment';
    button.append(node('span', 'event-tag', tag));
    button.setAttribute('aria-label', `${event.title}, ${projected ? 'projection' : 'planned'}, ${clock(beginning)} to ${clock(ending)}. View details.`);
    button.addEventListener('click', () => showEvent(event, projected));
    return button;
  }
  for (const event of events) {
    planned.append(block(event, null));
    const projected = simulation()?.events.find((e) => e.event_id === event.id);
    if (projected) {
      const band = node('div', 'uncertainty-band'); band.style.top = `${position(projected.end.p10)}%`;
      band.style.height = `${position(projected.end.p90) - position(projected.end.p10)}%`;
      band.title = `End p10–p90: ${clock(projected.end.p10)} – ${clock(projected.end.p90)}`;
      shadow.append(band, block(event, projected));
    }
  }
  if (!simulation()) {
    const empty = node('div', 'shadow-empty'); empty.append(node('span', '', '◌'), document.createTextNode('Run the analysis to reveal your probable day.')); shadow.append(empty);
  }
  const deadline = scenario().constraints.find((c) => c.event_id === target().id);
  if (deadline) { const marker = node('div', 'deadline-marker'); marker.style.top = `${position(deadline.deadline)}%`; marker.append(node('span', '', `Expected arrival ${clock(deadline.deadline)}`)); shadow.append(marker); }
}
function renderMetrics() {
  const projected = targetProjection();
  $('target-name').textContent = target().title;
  const deadline = scenario().constraints.find((c) => c.event_id === target().id)?.deadline || target().planned_start;
  $('target-time').textContent = `Expected arrival before ${clock(deadline)}`;
  $('projection-state').textContent = state.preview ? 'Alternative' : projected ? 'Calculated' : 'Pending';
  $('risk-number').textContent = projected ? percent(projected.late_arrival_probability) : '—';
  $('arrival-median').textContent = projected ? clock(projected.arrival.p50) : '—';
  $('arrival-range').textContent = projected ? `${clock(projected.arrival.p10)} – ${clock(projected.arrival.p90)}` : '—';
  const probability = projected?.late_arrival_probability || 0;
  $('ring-value').style.strokeDashoffset = String(477.522 * (1 - probability));
  $('ring-value').style.stroke = probability > .5 ? '#b78336' : '#235df3';
  $('risk-description').textContent = projected
    ? `${Math.round(probability * simulation().samples)} of ${simulation().samples} futures exceed the expected arrival time under these assumptions.`
    : 'Run the analysis to measure how vulnerable this sequence is to delays.';
  $('sample-count').textContent = simulation() ? number(simulation().samples) : '—';
  $('elapsed-label').textContent = state.preview ? 'Measured full search time' : 'Measured compute time';
  const metrics = state.preview ? state.planning.metrics : state.reference?.metrics;
  $('elapsed').textContent = metrics ? `${number(metrics.elapsed_ms)} ms` : '—';
  $('llm-count').textContent = simulation() ? simulation().llm_calls : '—';
  const baseline = state.normalBaseline?.simulation.events.find((e) => e.event_id === target().id);
  $('comparison').hidden = !state.preview && (!baseline || state.traffic !== 'incident');
  if (state.preview) $('comparison').textContent = `Same traffic, different arrangement: ${percent(state.preview.target_risk_before)} in the reference, ${percent(state.preview.target_risk_after)} with this proposal. No calendar changes.`;
  else if (baseline && projected && state.traffic === 'incident') $('comparison').textContent = `Before the incident: ${percent(baseline.late_arrival_probability)}. After: ${percent(probability)}. The same random samples are used for comparison.`;
}
function renderAssumptions() {
  const currentScenario = scenario();
  const assumptions = simulation()?.assumptions || [...new Set([...currentScenario.events, ...currentScenario.travel_edges, ...currentScenario.constraints].map((e) => e.source.assumption).filter(Boolean))];
  $('assumptions').replaceChildren(...assumptions.map((text) => node('li', '', text)));
}
function timeRange(times) { return `${clock(times.planned_start)} – ${clock(times.planned_end)}`; }
function shiftLabel(minutes) {
  const signed = new Intl.NumberFormat('en-GB', { signDisplay: 'always', maximumFractionDigits: 1 }).format(minutes);
  return `${signed} min${minutes < 0 ? ' · earlier' : minutes > 0 ? ' · later' : ''}`;
}
function planDescription(plan) {
  return plan.operations.map((operation) => {
    const event = plan.scenario.events.find((item) => item.id === operation.event_id);
    return `${event.title} : ${timeRange(operation.before)} → ${timeRange(operation.after)}`;
  }).join('. ');
}
function renderPlans() {
  const response = state.planning;
  const planning = response?.planning;
  const status = $('plans-status');
  status.className = `plans-status${state.planningError ? ' plans-error' : ''}`;
  if (state.pending === 'plans') status.textContent = 'ÆON is exploring possible moves with the reference traffic. The latest results stay visible during the calculation…';
  else if (state.planningError) status.textContent = `${state.planningError} Try again with “Find alternatives”.${planning ? ' The latest proposals remain visible.' : ''}`;
  else if (!state.reference) status.textContent = 'Analyse your day first to compare up to three proposals.';
  else if (planning?.status === 'no_better_plan') {
    const baseline = planning.baseline.events.find((event) => event.event_id === 'dinner');
    status.textContent = `No improvement found in this limited search. The calculated risk for dinner remains ${percent(baseline.late_arrival_probability)}. You can change the traffic and run the analysis again.`;
  } else if (planning) status.textContent = `${planning.plans.length} ${planning.plans.length === 1 ? 'calculated proposal' : 'calculated proposals'} with traffic set to ${state.traffic === 'incident' ? 'the synthetic incident' : 'normal'}. Compare the times and remaining risk before previewing.`;
  else if (state.plannerAvailable === false) status.textContent = 'The planner is currently unavailable. Your analysis remains available. Use the button to retry once the planner is installed.';
  else status.textContent = `Your analysis is ready, with traffic set to ${state.traffic === 'incident' ? 'the synthetic incident' : 'normal'}. Explore up to three moves of the flexible personal block.`;
  const list = $('plan-list'); list.replaceChildren();
  for (const plan of planning?.plans || []) {
    const selected = state.preview?.id === plan.id;
    const card = node('article', `plan-card${selected ? ' selected' : ''}`);
    const firstMove = plan.operations[0];
    const movedEvent = plan.scenario.events.find((event) => event.id === firstMove.event_id);
    const heading = node('div', 'plan-card-heading');
    heading.append(node('h3', '', `${movedEvent.title} at ${clock(firstMove.after.planned_start)}`), node('span', 'plan-shift', shiftLabel(plan.shift_minutes)));
    const risks = node('div', 'plan-risks');
    const beforeRisk = node('div'); beforeRisk.append(node('span', '', 'Current risk'), node('strong', 'risk-before', percent(plan.target_risk_before)));
    const afterRisk = node('div'); afterRisk.append(node('span', '', 'With this proposal'), node('strong', 'risk-after', percent(plan.target_risk_after)));
    const arrow = node('span', 'risk-arrow', '→'); arrow.setAttribute('aria-hidden', 'true');
    risks.append(beforeRisk, arrow, afterRisk);
    const schedule = node('div', 'plan-schedule');
    for (const operation of plan.operations) {
      if (plan.operations.length > 1) schedule.append(node('strong', 'moved-event-name', plan.scenario.events.find((event) => event.id === operation.event_id).title));
      for (const [label, times] of [['Current', operation.before], ['Proposal', operation.after]]) {
        const row = node('div', 'plan-time-row'); row.append(node('span', '', label));
        const range = node('span', 'plan-time-range');
        const start = node('time', '', clock(times.planned_start)); start.dateTime = times.planned_start;
        const end = node('time', '', clock(times.planned_end)); end.dateTime = times.planned_end;
        range.append(start, document.createTextNode(' – '), end); row.append(range); schedule.append(row);
      }
    }
    const button = node('button', 'secondary preview-plan', selected ? 'Preview active' : 'Preview');
    button.dataset.planId = plan.id;
    button.setAttribute('aria-pressed', String(selected));
    button.setAttribute('aria-label', `${selected ? 'Preview active' : 'Preview'} : ${planDescription(plan)}`);
    button.addEventListener('click', () => previewPlan(plan));
    card.append(heading, risks, node('p', 'plan-risk-caption', 'Risk of arriving late for dinner'), schedule, button);
    list.append(card);
  }
  $('search-summary').hidden = !planning;
  if (planning) {
    const search = planning.search;
    $('search-summary').textContent = `${number(search.evaluated_candidates)} candidates evaluated · ${number(response.metrics.elapsed_ms)} ms measured search time · ${planning.llm_calls} AI calls. ${search.truncated ? 'Budget reached: search stopped; other options may exist.' : 'Search completed within the defined scope.'}`;
    $('search-summary').classList.toggle('search-truncated', search.truncated);
  }
}
function renderPreview() {
  const preview = state.preview;
  $('preview-banner').hidden = !preview;
  $('planned-label').textContent = preview ? 'Proposed calendar' : 'Your calendar';
  $('calendar-title').textContent = preview ? 'Your day, with this alternative.' : 'One day. Two perspectives.';
  $('calendar-description').textContent = preview ? 'Proposed times and calculated forecast, with the same traffic.' : 'Planned and probable, on the same scale.';
  if (preview) $('preview-description').textContent = `${planDescription(preview)}. Traffic kept ${state.traffic === 'incident' ? 'with the synthetic incident' : 'normal'}. No calendar changes.`;
}
function previewPlan(plan) {
  if (state.busy) return;
  state.preview = plan; $('event-details').hidden = true;
  render();
  notice('Preview active. The calendar and risk show this proposal; you can return to the reference without a new calculation.');
  $('preview-banner').focus({ preventScroll: true });
  $('preview-banner').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
}
function closePreview() {
  if (state.busy || !state.preview) return;
  const id = state.preview.id;
  state.preview = null; $('event-details').hidden = true;
  render();
  notice(`Reference day restored, with traffic set to ${state.traffic === 'incident' ? 'the synthetic incident' : 'normal'}. No new calculation or calendar changes.`);
  [...document.querySelectorAll('.preview-plan')].find((button) => button.dataset.planId === id)?.focus({ preventScroll: true });
}
async function findPlans() {
  if (state.busy || !state.reference) return;
  state.pending = 'plans'; state.planningError = null;
  setBusy(true); renderPlans(); setBusy(true);
  try {
    const response = await api('/api/plans', { traffic: state.traffic });
    state.planning = response; state.preview = null; state.plannerAvailable = true;
    $('event-details').hidden = true;
    notice(response.planning.status === 'plans_found' ? 'Alternatives calculated. The calendar shows your reference until you preview a proposal.' : 'Search complete: no improvement found within this demo’s scope.');
  } catch (error) {
    state.planningError = error.message;
    if (error.code === 'planner_unavailable') state.plannerAvailable = false;
  } finally { state.pending = null; setBusy(false); render(); }
}
function render() { renderTimeline(); renderMetrics(); renderSignals(); renderAssumptions(); renderPlans(); renderPreview(); setBusy(state.busy); }
async function analyze(traffic = 'normal') {
  if (state.busy || !scenario()) return;
  state.pending = 'analysis'; setBusy(true); $('event-details').hidden = true;
  notice(traffic === 'incident' ? 'Traffic has changed. ÆON is recalculating the same 1,000 futures…' : 'ÆON is exploring 1,000 futures from your reference day…');
  try {
    const result = await api('/api/simulate', { traffic });
    state.reference = result; state.traffic = traffic;
    state.planning = null; state.preview = null; state.planningError = null;
    if (traffic === 'normal') state.normalBaseline = result;
    render(); $('reset').hidden = traffic !== 'incident';
    notice(traffic === 'incident' ? 'Synthetic incident: +12 minutes on journeys. The displayed probabilities come from the new calculation.' : 'Analysis complete. Explore events or simulate a traffic change. No calendar has been changed.', traffic === 'incident' ? 'warning' : '');
  } catch (error) {
    notice(error.message + (state.reference ? ' The latest successful results remain visible.' : ''), 'error');
  } finally { state.pending = null; setBusy(false); }
}
$('analyze').addEventListener('click', () => analyze('normal'));
$('incident').addEventListener('click', () => analyze('incident'));
$('reset').addEventListener('click', () => analyze('normal'));
$('find-plans').addEventListener('click', findPlans);
$('close-preview').addEventListener('click', closePreview);
$('show-sources').addEventListener('click', () => { const panel = $('sources-panel'); panel.open = true; panel.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' }); panel.querySelector('summary').focus({ preventScroll: true }); });
async function init() {
  setBusy(true);
  try {
    const [status, demo] = await Promise.all([api('/api/status'), api('/api/demo')]);
    state.demo = demo.scenario; state.plannerAvailable = status.planner_available;
    $('day-label').textContent = new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: scenario().timezone }).format(new Date(scenario().events[0].planned_start));
    const list = $('connection-list'); list.replaceChildren();
    for (const connection of status.integrations) { const item = node('div', 'connection'); item.append(node('strong', '', names[connection.id] || connection.name), node('span', '', 'Synthetic demo input')); list.append(item); }
    render();
    notice(status.engine_available ? 'Your reference day is ready. Run the analysis to look ahead.' : 'Your demo calendar is ready. The simulation engine is being integrated; no predictions are displayed yet.', status.engine_available ? '' : 'warning');
  } catch (error) { notice(`Could not load the day. ${error.message}`, 'error'); }
  finally { setBusy(false); }
}
init();

// Private reads never enter the demo, its planner, or browser storage.
const providerIds = ['google_calendar', 'gmail'];
const liveState = { csrfToken: null, canonicalOrigin: null, providers: {}, pending: {}, feedback: {}, loading: false, sessionError: false, syncVersion: 0, lastOauthResult: null };
const actionsState = { snapshot: null, loading: false, pending: null, version: 0, stale: true, error: '', sessionToken: null, calendarNeedsRead: false };
const renderedReads = new Map();
function connectionNotice(message, error = false) {
  $('connections-notice').textContent = message;
  $('connections-notice').classList.toggle('connection-error', error);
}
function sessionFailure(error) {
  if (['session_required', 'session_expired', 'session_invalid', 'csrf_invalid'].includes(error.code)) {
    liveState.csrfToken = null;
    liveState.sessionError = true;
    syncForecastConnection();
    connectionNotice('Your local session expired or was rejected. Click “Restore session”, then reconnect sources if needed. No read will be retried automatically.', true);
  }
}
async function liveApi(path, body) {
  const options = { credentials: 'same-origin', mode: 'same-origin', cache: 'no-store' };
  if (body !== undefined) {
    if (!liveState.csrfToken) throw Object.assign(new Error('Restore your local session before trying again.'), { code: 'session_required' });
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json', 'X-Aeon-CSRF': liveState.csrfToken };
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('The local server cannot be reached. Check that ÆON is running, then try again.'); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error('The server response is unreadable. Refresh connections before trying again.'); }
  if (!response.ok) {
    const error = Object.assign(new Error(result.error?.message || 'This operation failed. Try again.'), { code: result.error?.code });
    sessionFailure(error);
    throw error;
  }
  return result;
}
function setCanonicalOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || url.origin !== value) throw new Error('The returned local address is invalid. Reopen ÆON at its 127.0.0.1 address.');
  liveState.canonicalOrigin = url.origin;
  $('canonical-address').href = `${url.origin}/`;
  $('canonical-address').textContent = `${url.origin}/`;
}
function readDate(value, timezone = 'Europe/Paris') {
  if (value === null || value === undefined || value === '') return 'unknown date';
  try { return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(typeof value === 'number' ? value * 1000 : value)); }
  catch { return 'date unavailable'; }
}
function calendarInstant(value, timezone) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new Error('Enter a date and time for both ends of the period.');
  const [, year, month, day, hour, minute] = match.map(Number);
  const wallTime = Date.UTC(year, month - 1, day, hour, minute);
  let formatter;
  try { formatter = new Intl.DateTimeFormat('en-GB', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }); }
  catch { throw new Error('This time zone is unknown. Try Europe/Paris, for example.'); }
  const localTime = (instant) => {
    const parts = Object.fromEntries(formatter.formatToParts(new Date(instant)).map((part) => [part.type, part.value]));
    return Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  };
  let instant = wallTime;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const difference = wallTime - localTime(instant);
    if (!difference) return new Date(instant).toISOString();
    instant += difference;
  }
  throw new Error('This time does not exist in this time zone during the daylight saving change. Choose another time.');
}
function calendarRequest() {
  const timezone = $('calendar-timezone').value.trim();
  if (!timezone) throw new Error('Enter the time zone for this period.');
  const time_min = calendarInstant($('calendar-start').value, timezone);
  const time_max = calendarInstant($('calendar-end').value, timezone);
  const duration = new Date(time_max).getTime() - new Date(time_min).getTime();
  if (duration <= 0 || duration > 7 * 24 * 3600000) throw new Error('The end must follow the start, with a maximum duration of 7 days (168 hours).');
  return { time_min, time_max, timezone };
}
function renderReadData(id, provider) {
  // A change to Gmail must not remove Calendar's focused or scrolled list.
  const content = JSON.stringify([provider?.data, provider?.authorized, provider?.read?.status === 'failed']);
  if (renderedReads.get(id) === content) return;
  renderedReads.set(id, content);
  const container = $(`${id}-data`);
  container.replaceChildren();
  const data = provider?.data;
  if (!data) {
    container.append(node('p', 'read-empty', provider?.authorized ? 'Source authorised. No data read: choose a period or search, then start reading.' : 'No data read. Connect this source to request a read.'));
    return;
  }
  const incomplete = data.complete !== true;
  const summary = node('div', `read-summary${incomplete ? ' read-incomplete' : ''}`);
  summary.append(node('strong', '', incomplete ? 'Incomplete read' : 'Read succeeded within the requested scope'));
  summary.append(node('p', '', `Data last read on ${readDate(data.read_at, data.timezone)}.`));
  if (incomplete) summary.append(node('p', '', id === 'gmail' ? 'Other messages may exist or some excerpts may be truncated. These results do not cover the full search.' : 'Some events or details are not shown in full because of read limits, excluded items or truncated titles.'));
  if (provider.read?.status === 'failed') summary.append(node('p', 'read-stale', 'The latest attempt failed. The data below comes from the previous read.'));
  container.append(summary);
  if (id === 'google_calendar') {
    container.append(node('p', 'read-scope', `Primary calendar · ${readDate(data.horizon?.start, data.timezone)} to ${readDate(data.horizon?.end, data.timezone)} (${data.timezone || 'Europe/Paris'}).`));
    const events = data.events || [];
    container.append(node('p', 'read-count', `${events.length} event${events.length === 1 ? '' : 's'} shown · ${data.excluded_count || 0} excluded · ${data.deleted_count || 0} deleted. All displayed events remain fixed.`));
    if (!events.length) container.append(node('p', 'read-empty', 'No displayable events in this read.'));
    const list = node('ul', 'import-list');
    list.tabIndex = 0; list.setAttribute('aria-label', 'Events received from Google Calendar');
    for (const event of events) {
      const item = node('li', 'import-event');
      item.append(node('h4', '', event.title || 'Untitled event'), node('p', '', `${readDate(event.planned_start, data.timezone)} – ${readDate(event.planned_end, data.timezone)}`), node('small', '', `Google Calendar · Fixed commitment${event.private ? ' · Personal, no other attendees' : ''}`));
      list.append(item);
    }
    container.append(list);
  } else {
    container.append(node('p', 'read-scope', `Search: ${data.query}`));
    const messages = data.messages || [];
    container.append(node('p', 'read-count', `${messages.length} excerpt${messages.length === 1 ? '' : 's'} received (10 maximum). No constraints inferred.`));
    if (!messages.length) container.append(node('p', 'read-empty', 'No messages found in this read.'));
    const list = node('ul', 'import-list');
    list.tabIndex = 0; list.setAttribute('aria-label', 'Excerpts received from Gmail');
    for (const message of messages) {
      const item = node('li', 'import-message');
      item.append(node('h4', '', message.subject || 'No subject'), node('p', 'message-meta', `${message.from || 'Sender not provided'} · ${message.date || 'Date not provided'}`), node('p', 'message-excerpt', message.excerpt || 'No excerpt available.'));
      item.append(node('small', message.truncated ? 'excerpt-truncated' : '', message.truncated ? 'Gmail · Truncated excerpt' : 'Gmail · Received excerpt'));
      list.append(item);
    }
    container.append(list);
  }
}
function renderConnections() {
  const sameOrigin = liveState.canonicalOrigin === window.location.origin;
  const active = Boolean(liveState.csrfToken) && sameOrigin && !liveState.loading && !actionsState.pending;
  $('refresh-connections').disabled = liveState.loading || Boolean(actionsState.pending) || providerIds.some((id) => liveState.pending[id]);
  $('refresh-connections').textContent = liveState.sessionError || !liveState.csrfToken ? 'Restore session' : 'Refresh connections';
  for (const id of providerIds) {
    const provider = liveState.providers[id];
    const pending = liveState.pending[id];
    const ready = provider?.configuration === 'ready';
    const authorized = provider?.authorized === true;
    $(`${id}-panel`).setAttribute('aria-busy', String(Boolean(pending) || liveState.loading));
    const badge = $(`${id}-badge`);
    badge.textContent = provider ? authorized ? 'Authorised' : ready ? 'Not connected' : 'Needs configuration' : 'Pending';
    badge.className = `provider-badge${authorized ? ' provider-authorized' : ''}`;
    let status = 'Checking configuration…';
    if (provider) {
      if (provider.configuration === 'missing') status = 'Google configuration is missing on the local server. Add this source’s configuration, then restart ÆON.';
      else if (provider.configuration === 'invalid') status = 'Google configuration is invalid on the local server. Correct it, then restart ÆON.';
      else if (!authorized) status = 'Connect this source to authorise reading. No data is read while connecting.';
      else if (provider.read.status === 'never') status = 'Authorisation received. No data has been read yet.';
      else if (provider.read.status === 'failed') status = 'The latest read failed. You can retry it explicitly.';
      else if (provider.read.status === 'incomplete') status = 'A read succeeded with incomplete results.';
      else status = 'A read succeeded. See its period or search below.';
    }
    $(`${id}-status`).textContent = status;
    $(`${id}-connect`).disabled = !active || !ready || Boolean(pending);
    $(`${id}-connect`).textContent = pending === 'connect' ? 'Opening Google…' : `${authorized ? 'Reconnect' : 'Connect'} ${id === 'gmail' ? 'Gmail' : 'Calendar'}`;
    $(`${id}-forget`).disabled = !active || !provider || Boolean(pending);
    $(`${id}-forget`).textContent = pending === 'forget' ? 'Forgetting…' : `Forget ${id === 'gmail' ? 'Gmail' : 'Calendar'}`;
    $(`${id === 'gmail' ? 'gmail' : 'calendar'}-read-fields`).disabled = !active || !authorized || Boolean(pending);
    $(`${id === 'gmail' ? 'gmail' : 'calendar'}-read`).textContent = pending === 'read' ? 'Reading…' : id === 'gmail' ? 'Read up to 10 excerpts' : 'Read this period';
    const feedback = liveState.feedback[id];
    $(`${id}-feedback`).textContent = pending === 'read' ? 'Read requested. The latest data stays visible during the request…' : feedback?.message || provider?.read.error?.message || '';
    $(`${id}-feedback`).classList.toggle('connection-error', Boolean(feedback?.error || provider?.read.error));
    renderReadData(id, provider);
  }
  syncForecastConnection();
}
async function loadConnections() {
  const version = ++liveState.syncVersion;
  const result = await liveApi('/api/connections');
  if (version !== liveState.syncVersion) return;
  setCanonicalOrigin(result.canonical_origin);
  if (!Array.isArray(result.providers) || providerIds.some((id) => !result.providers.find((provider) => provider.id === id))) throw new Error('Source status is incomplete. Refresh connections.');
  liveState.providers = Object.fromEntries(result.providers.filter((provider) => providerIds.includes(provider.id)).map((provider) => [provider.id, provider]));
  if (result.oauth_result && JSON.stringify(result.oauth_result) !== liveState.lastOauthResult) {
    const outcome = result.oauth_result;
    liveState.lastOauthResult = JSON.stringify(outcome);
    connectionNotice(outcome.provider === 'google_calendar_write' ? outcome.message || 'Calendar write consent updated. Check write access below.' : outcome.status === 'connected' ? `${names[outcome.provider] || 'Google source'}: authorisation received. Choose the data to read and start reading.` : outcome.message || 'Google connection failed. Try again with “Connect”.', outcome.status !== 'connected');
  }
  if (liveState.canonicalOrigin !== window.location.origin) connectionNotice('Open the 127.0.0.1 address shown above in your regular browser to connect your sources.', true);
  renderConnections();
  await refreshCalendarActions();
}
async function initConnections() {
  if (liveState.loading) return;
  liveState.loading = true;
  renderConnections();
  try {
    const session = await liveApi('/api/session');
    if (typeof session.csrf_token !== 'string' || !session.csrf_token) throw new Error('The local session is unavailable. Try again.');
    setCanonicalOrigin(session.canonical_origin);
    liveState.csrfToken = session.csrf_token;
    liveState.sessionError = false;
    connectionNotice('Connections and reads are independent. Authorising Google does not start a read.');
    await loadConnections();
  } catch (error) {
    sessionFailure(error);
    if (!liveState.sessionError) connectionNotice(error.message, true);
  } finally { liveState.loading = false; renderConnections(); }
}
async function synchronizeConnections() {
  try { await loadConnections(); }
  catch (error) {
    sessionFailure(error);
    if (!liveState.sessionError) connectionNotice(`Could not refresh connection status. ${error.message} The latest received data remains visible.`, true);
  }
}
async function connectionAction(id, action, body = {}) {
  if (!providerIds.includes(id) || liveState.pending[id] || liveState.loading || !liveState.csrfToken || actionsState.pending) return;
  suspendAgentWork();
  liveState.pending[id] = action;
  liveState.feedback[id] = null;
  renderConnections();
  let authorizationUrl;
  try {
    const path = action === 'read' ? `/api/${id === 'google_calendar' ? 'calendar' : 'gmail'}/read` : `/api/oauth/${id}/${action === 'connect' ? 'begin' : 'forget'}`;
    const result = await liveApi(path, body);
    if (action === 'connect') {
      const url = new URL(result.authorization_url);
      if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com' || url.username || url.password || url.port) throw new Error('The Google consent address is invalid. Refresh connections before trying again.');
      authorizationUrl = url.href;
      liveState.providers[id] = { ...liveState.providers[id], authorized: false, data: null, read: { status: 'never', error: null } };
      liveState.feedback[id] = { message: 'Opening Google consent in this browser…', error: false };
    } else if (action === 'forget') {
      liveState.providers[id] = { ...liveState.providers[id], authorized: false, data: null, read: { status: 'never', error: null } };
      liveState.feedback[id] = { message: 'Connection and local data forgotten for this source.', error: false };
    } else {
      invalidateForecast();
      invalidateIntelligence();
      if (result.provider !== id || result.data?.synthetic !== false) throw new Error('The received read is invalid. Refresh connections.');
      if (id === 'google_calendar') actionsState.calendarNeedsRead = false;
      liveState.providers[id] = { ...liveState.providers[id], data: result.data, read: { status: result.data.complete === true ? 'succeeded' : 'incomplete', error: null } };
      liveState.feedback[id] = { message: result.data.complete === true ? 'Read complete. The received data is shown below.' : 'Read complete with incomplete results. See the limits below.', error: false };
    }
  } catch (error) {
    liveState.feedback[id] = { message: error.message, error: true };
    if (action === 'read' && liveState.providers[id]) liveState.providers[id].read = { ...liveState.providers[id].read, status: 'failed', error: { message: error.message } };
    sessionFailure(error);
  } finally {
    await synchronizeConnections();
    liveState.pending[id] = null;
    renderConnections();
  }
  if (authorizationUrl && !liveState.sessionError) window.location.assign(authorizationUrl);
}
for (const id of providerIds) {
  $(`${id}-connect`).addEventListener('click', () => connectionAction(id, 'connect'));
  $(`${id}-forget`).addEventListener('click', () => connectionAction(id, 'forget'));
}
$('calendar-read-form').addEventListener('submit', (event) => {
  event.preventDefault();
  try { const body = calendarRequest(); connectionAction('google_calendar', 'read', body); }
  catch (error) { liveState.feedback.google_calendar = { message: error.message, error: true }; renderConnections(); }
});
$('gmail-read-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const query = $('gmail-query').value.trim();
  if (!query || query.length > 500) { liveState.feedback.gmail = { message: 'Enter a search of 1 to 500 characters.', error: true }; renderConnections(); return; }
  connectionAction('gmail', 'read', { query });
});
$('refresh-connections').addEventListener('click', async () => {
  if (!liveState.csrfToken) { await initConnections(); return; }
  liveState.loading = true; renderConnections();
  connectionNotice('Refreshing connection status. No new Google read…');
  try { await loadConnections(); if (!liveState.sessionError) connectionNotice('Connection status refreshed. No new Google read performed.'); }
  catch (error) { sessionFailure(error); if (!liveState.sessionError) connectionNotice(error.message, true); }
  finally { liveState.loading = false; renderConnections(); }
});

// Real forecasts consume only server snapshots; they never replace the demo state.
const forecastState = { snapshot: null, busy: false, version: 0, connectionKey: null, error: '', stale: false, drafts: {}, preview: null, selection: {target: '', movable: [], start: '', end: ''} };
const forecastIssues = {
  CALENDAR_NOT_READ: 'Explicitly read a Calendar period in “My Google connections”.',
  CALENDAR_INCOMPLETE: 'The Calendar read is incomplete. Choose a shorter period and read it again.',
  NO_EVENTS: 'No events available in this period.',
  MISSING_TRAVEL_EDGE: 'Fill in the missing travel between the events below.',
  CALENDAR_TOO_LARGE: 'More than 100 events: shorten the read period. No events will be dropped to run a calculation.',
  ASSEMBLER_UNAVAILABLE: 'The scenario assembly module is unavailable on this server.'
};
function invalidateForecast() {
  forecastState.version += 1;
  forecastState.snapshot = null;
  forecastState.drafts = {};
  forecastState.preview = null;
  forecastState.selection = {target: '', movable: [], start: '', end: ''};
  forecastState.error = '';
  forecastState.stale = false;
}
function syncForecastConnection() {
  const calendar = liveState.providers.google_calendar;
  const gmail = liveState.providers.gmail;
  const key = JSON.stringify([liveState.csrfToken, calendar?.authorized, calendar?.data?.read_at, calendar?.data?.horizon, gmail?.authorized, gmail?.data?.read_at, gmail?.data?.query]);
  if (key !== forecastState.connectionKey) {
    invalidateForecast();
    invalidateIntelligence();
    actionsState.version += 1; actionsState.stale = true; actionsState.loading = false; actionsState.error = '';
    if (actionsState.sessionToken !== liveState.csrfToken) {
      actionsState.snapshot = null; actionsState.calendarNeedsRead = false;
      actionsState.sessionToken = liveState.csrfToken;
    }
    forecastState.connectionKey = key;
  }
  if (forecastState.snapshot?.calendar && calendar?.read?.status === 'failed') forecastState.snapshot.calendar.last_read_failed = true;
  renderForecast();
}
function forecastActive() {
  return agentActive() && !intelligenceState.busy && !actionsState.calendarNeedsRead;
}
function forecastNotice(message, error = false) {
  $('forecast-notice').textContent = message;
  $('forecast-notice').classList.toggle('connection-error', error);
}
function acceptForecast(data) {
  if (!data || !['incomplete', 'ready', 'simulated'].includes(data.status) || !Array.isArray(data.travel_pairs) || !Array.isArray(data.issues) || !data.routes || (data.revision !== null && typeof data.revision !== 'string')) throw new Error('The forecast state is invalid. Refresh the imported calendar.');
  if (forecastState.snapshot?.revision !== data.revision) {
    forecastState.drafts = {}; forecastState.preview = null;
    forecastState.selection = {target: '', movable: [], start: '', end: ''};
    if (intelligenceState.snapshot && intelligenceState.snapshot.revision !== data.revision) invalidateIntelligence();
  }
  if (forecastState.preview && !data.planning?.plans.some((plan) => JSON.stringify(plan) === JSON.stringify(forecastState.preview))) forecastState.preview = null;
  forecastState.snapshot = data;
  forecastState.stale = false;
}
async function refreshForecast() {
  if (forecastState.busy || !forecastActive()) return;
  const version = ++forecastState.version;
  forecastState.preview = null;
  forecastState.busy = true; forecastState.error = ''; renderForecast();
  try {
    const data = await liveApi('/api/live/scenario');
    if (version === forecastState.version) acceptForecast(data);
  } catch (error) {
    if (version === forecastState.version) { forecastState.error = error.message; forecastState.stale = true; }
  } finally { forecastState.busy = false; renderForecast(); }
}
async function forecastAction(path, fields = {}) {
  if (forecastState.busy || !forecastActive() || !forecastState.snapshot?.revision || forecastState.stale) return;
  if (path === '/api/live/simulate' && !['ready', 'simulated'].includes(forecastState.snapshot.status)) return;
  if (!['/api/live/simulate', '/api/live/reset', '/api/live/travel', '/api/live/plans'].includes(path)) return;
  const version = ++forecastState.version;
  const body = { ...fields, revision: forecastState.snapshot.revision };
  forecastState.preview = null;
  forecastState.busy = true; forecastState.error = ''; renderForecast();
  try {
    const data = await liveApi(path, body);
    if (version === forecastState.version) acceptForecast(data);
  } catch (error) {
    if (version === forecastState.version) {
      forecastState.error = error.message;
      forecastState.stale = true;
      // Read back the revision and consumed budget after a failed action; never retry a POST.
      if (forecastActive()) {
        try { const data = await liveApi('/api/live/scenario'); if (version === forecastState.version) acceptForecast(data); }
        catch { /* Keep the previous result explicitly stale until manual refresh. */ }
      }
    }
  } finally { forecastState.busy = false; renderForecast(); }
}
function pairKey(pair) { return JSON.stringify([pair.from_event_id, pair.to_event_id]); }
async function forecastTravel(index, kind, originAddress = '', destinationAddress = '') {
  const snapshot = forecastState.snapshot;
  const pair = snapshot?.travel_pairs[index];
  if (!pair || forecastState.busy || !forecastActive() || forecastState.stale) return;
  const fields = { from_event_id: pair.from_event_id, to_event_id: pair.to_event_id, kind };
  if (kind === 'google_routes' || kind === 'osrm') {
    const origin = originAddress.trim(); const destination = destinationAddress.trim();
    forecastState.drafts[pairKey(pair)] = { origin: originAddress, destination: destinationAddress, kind };
    const available = kind === 'osrm' ? snapshot.routes.open_routing : snapshot.routes.configured;
    if (!available || snapshot.routes.remaining <= 0) {
      forecastState.error = available ? 'The travel attempt budget is exhausted.'
        : kind === 'osrm' ? 'The OpenStreetMap / OSRM service is unavailable on this server.' : 'The Google Routes key is not configured on the server.';
      renderForecast(); return;
    }
    if (!origin || !destination || origin.length > 500 || destination.length > 500) {
      forecastState.error = 'Explicitly enter two addresses of 1 to 500 characters.'; renderForecast(); return;
    }
    fields.origin_address = origin; fields.destination_address = destination;
  } else if (kind !== 'same_location') return;
  await forecastAction('/api/live/travel', fields);
}
function renderForecast() {
  const snapshot = forecastState.snapshot;
  const disabled = !forecastActive() || forecastState.busy;
  $('live-forecast').setAttribute('aria-busy', String(forecastState.busy));
  $('forecast-refresh').disabled = disabled;
  $('forecast-reset').disabled = disabled || !snapshot?.revision || forecastState.stale;
  $('forecast-simulate').disabled = disabled || forecastState.stale || !snapshot?.revision || !['ready', 'simulated'].includes(snapshot?.status);
  const text = forecastState.busy ? 'Calculating or refreshing. No other action will start automatically.'
    : forecastState.error ? forecastState.error + (forecastState.stale ? ' The displayed data is out of date; refresh before any further action.' : '')
    : actionsState.calendarNeedsRead ? 'A Calendar write was attempted. Read Calendar again in “My Google connections” before preparing another forecast. Your receipts remain available below.'
    : !forecastActive() ? 'Open a local session and finish any Calendar read before continuing.'
    : !snapshot ? 'Refresh the imported calendar to prepare a forecast. This button does not read Google.'
    : snapshot.status === 'simulated' ? 'Forecast calculated for this Calendar read. The synthetic demo is unchanged.'
    : snapshot.status === 'ready' ? 'The data is ready. Start the calculation explicitly.' : 'Information is missing; no forecast has been invented.';
  forecastNotice(text, Boolean(forecastState.error));
  const calendar = snapshot?.calendar;
  $('forecast-calendar').textContent = calendar ? `Read on ${readDate(calendar.read_at, calendar.timezone)} · ${readDate(calendar.horizon.start, calendar.timezone)} to ${readDate(calendar.horizon.end, calendar.timezone)} · ${calendar.events.length} events.${calendar.last_read_failed ? ' The latest read attempt failed: this import comes from the previous read.' : ''}` : '';
  const issues = snapshot?.issues || [];
  $('forecast-issues').replaceChildren(...issues.map((issue) => node('li', '', forecastIssues[issue.code] || 'Some data needs checking before the calculation.')));
  const routes = snapshot?.routes;
  const observedRoutes = (snapshot?.travel_pairs || []).filter((pair) => pair.travel?.kind === 'google_routes').length;
  const openRoutes = (snapshot?.travel_pairs || []).filter((pair) => pair.travel?.kind === 'osrm').length;
  const observationLabel = observedRoutes ? `${observedRoutes} Google route(s) observed in this calendar` : 'no Google route observed in this calendar';
  $('forecast-budget').textContent = routes ? `OSRM : ${routes.open_routing ? 'available without a Google key' : 'unavailable'} · ${openRoutes} OSRM route(s) calculated in this calendar. Google Routes : ${routes.configured ? 'key configured' : 'key not configured'} · ${observationLabel}. Shared budget: ${routes.attempts} attempt(s) of ${routes.limit} · ${routes.remaining} remaining. Failures count; clearing travel does not reset this budget.` : '';
  const pairs = $('forecast-pairs'); pairs.replaceChildren();
  for (const [index, pair] of (snapshot?.travel_pairs || []).entries()) {
    const card = node('article', 'forecast-pair');
    card.append(node('h3', '', `${pair.from_title} → ${pair.to_title}`), node('p', 'read-scope', `Planned departure in the model: ${readDate(pair.departure_time, calendar?.timezone)} (planned end of the first event).`));
    if (pair.travel) {
      const duration = pair.travel.duration_minutes;
      const label = pair.travel.kind === 'same_location' ? 'same location declared' : pair.travel.kind === 'osrm' ? 'OSRM, driving, without live traffic' : 'Google Routes observation';
      card.append(node('p', 'forecast-travel-saved', `Travel entered: ${label}. Triangular model min ${number(duration.min)}, mode ${number(duration.mode)}, max ${number(duration.max)} min.`));
      if (pair.travel.cached === true) card.append(node('p', 'read-help', 'Cached route · no new request'));
      if (pair.travel.kind === 'osrm') card.append(node('p', 'forecast-map-matches', `Matches to verify: ${pair.travel.origin_label || 'origin not provided'} → ${pair.travel.destination_label || 'destination not provided'}. Correct the addresses and try again if these locations are not right.`));
      card.append(node('p', 'read-help', `${pair.travel.source.synthetic ? 'Synthetic / declared source' : 'Real source'} · ${pair.travel.source.assumption}${pair.travel.observed_at ? ' Observed on ' + readDate(pair.travel.observed_at, calendar?.timezone) + '.' : ''}`));
    }
    const samePlace = node('button', 'secondary forecast-control', 'Declare the same location — no travel');
    samePlace.type = 'button'; samePlace.disabled = disabled || forecastState.stale;
    samePlace.addEventListener('click', () => forecastTravel(index, 'same_location'));
    card.append(samePlace, node('p', 'read-help', 'Use this declaration only if both events really take place at the same location.'));
    const form = node('form', 'forecast-route-form');
    const draft = forecastState.drafts[pairKey(pair)] || {};
    const serviceLabel = node('label', '', 'Routing service');
    const service = node('select'); service.id = `forecast-service-${index}`; serviceLabel.htmlFor = service.id;
    const kind = draft.kind || (routes.open_routing ? 'osrm' : routes.configured ? 'google_routes' : 'osrm');
    for (const [value, title, available] of [['osrm', 'OpenStreetMap / OSRM — no key or bank card', routes.open_routing], ['google_routes', 'Google Routes — potentially billable option', routes.configured]]) {
      if (available || kind === value) { const option = node('option', '', title); option.value = value; option.disabled = !available; service.append(option); }
    }
    service.value = kind; service.disabled = disabled || forecastState.stale || routes.remaining <= 0 || (!routes.open_routing && !routes.configured);
    serviceLabel.append(service); form.append(serviceLabel);
    const serviceHelp = node('p', 'read-help'); serviceHelp.id = `forecast-service-help-${index}`;
    service.setAttribute('aria-describedby', serviceHelp.id);
    const inputs = [];
    for (const [name, labelText, value] of [['origin', 'Origin address', draft.origin || ''], ['destination', 'Destination address', draft.destination || '']]) {
      const label = node('label', '', labelText);
      const input = node('input'); input.type = 'text'; input.maxLength = 500; input.required = true; input.value = value; input.autocomplete = 'off';
      input.id = `forecast-${name}-${index}`; label.htmlFor = input.id;
      input.addEventListener('input', () => { forecastState.drafts[pairKey(pair)] = { ...(forecastState.drafts[pairKey(pair)] || {}), [name]: input.value }; });
      label.append(input); form.append(label); inputs.push(input);
    }
    const submit = node('button', 'secondary forecast-control'); submit.type = 'submit';
    const updateService = () => {
      const open = service.value === 'osrm';
      const available = open ? routes.open_routing : routes.configured;
      submit.textContent = open ? 'Request OSRM route (1 attempt)' : 'Request Google Routes (1 attempt)';
      submit.disabled = disabled || forecastState.stale || !available || routes.remaining <= 0;
      inputs.forEach((input) => { input.disabled = submit.disabled; });
      serviceHelp.textContent = open
        ? 'Driving, without live traffic. Clicking sends the addresses to Nominatim and the route to OSRM. Use public places, without personal or confidential data.'
        : 'Clicking sends the addresses and departure time to Google Routes. Google may charge for this request; the attempt limit is not a spending cap.';
    };
    service.addEventListener('change', () => { forecastState.drafts[pairKey(pair)] = { ...(forecastState.drafts[pairKey(pair)] || {}), kind: service.value }; updateService(); });
    updateService();
    form.append(serviceHelp, submit); form.addEventListener('submit', (event) => { event.preventDefault(); return forecastTravel(index, service.value, inputs[0].value, inputs[1].value); });
    card.append(form); pairs.append(card);
  }
  const results = $('forecast-result'); results.replaceChildren();
  const result = forecastState.preview?.simulation || snapshot?.planning?.baseline || snapshot?.result;
  if (result && calendar) {
    results.append(node('h3', '', 'Engine results — real calendar'), node('p', 'read-help', `${number(result.samples)} samples · ${result.llm_calls} AI calls for simulation. Risk measures arrival after the model’s deadline, not certainty of being late.`));
    const wrapper = node('div', 'forecast-table-wrap'); wrapper.tabIndex = 0; wrapper.setAttribute('role', 'region'); wrapper.setAttribute('aria-label', 'Event forecasts, scrollable table');
    const table = node('table', 'forecast-table'); const caption = node('caption', '', 'Times in time zone ' + calendar.timezone); table.append(caption);
    const head = node('thead'); const heading = node('tr');
    for (const label of ['Event', 'Planned start', 'Median arrival', 'Arrival p10–p90', 'Risk of arriving late']) { const cell = node('th', '', label); cell.scope = 'col'; heading.append(cell); }
    head.append(heading); table.append(head); const body = node('tbody');
    for (const event of result.events) {
      const original = calendar.events.find((item) => item.id === event.event_id); const row = node('tr');
      const label = node('th', '', original?.title || 'Event'); label.scope = 'row'; row.append(label);
      for (const value of [readDate(forecastState.preview?.operations.find((operation) => operation.event_id === event.event_id)?.after.planned_start || original?.planned_start, calendar.timezone), readDate(event.arrival.p50, calendar.timezone), `${readDate(event.arrival.p10, calendar.timezone)} – ${readDate(event.arrival.p90, calendar.timezone)}`, percent(event.late_arrival_probability)]) row.append(node('td', '', value));
      body.append(row);
    }
    table.append(body); wrapper.append(table); results.append(wrapper);
  }
  $('forecast-assumptions').replaceChildren(...(result?.assumptions || []).map((text) => node('li', '', text)));
  renderLivePlans();
  renderIntelligence();
  renderCalendarActions();
}
$('forecast-refresh').addEventListener('click', refreshForecast);
$('forecast-simulate').addEventListener('click', () => forecastAction('/api/live/simulate'));
$('forecast-reset').addEventListener('click', () => forecastAction('/api/live/reset'));

// Extraction is a separate, explicitly consented request; only confirmed server proposals become constraints.
const intelligenceState = { snapshot: null, busy: false, version: 0, stale: false, error: '', messageId: '', eventIds: [] };
function agentActive() {
  return Boolean(liveState.csrfToken) && !liveState.sessionError && liveState.canonicalOrigin === window.location.origin && !liveState.loading && !actionsState.pending && !providerIds.some((id) => liveState.pending[id]);
}
function invalidateIntelligence() {
  intelligenceState.version += 1;
  intelligenceState.snapshot = null; intelligenceState.stale = false; intelligenceState.error = '';
  intelligenceState.messageId = ''; intelligenceState.eventIds = [];
}
function suspendAgentWork() {
  forecastState.version += 1; intelligenceState.version += 1;
  forecastState.preview = null;
  if (forecastState.busy) forecastState.stale = true;
  if (intelligenceState.busy) intelligenceState.stale = true;
}
function selectOptions(select, entries, selected, placeholder) {
  const key = JSON.stringify(entries);
  if (select.dataset.options !== key) {
    select.dataset.options = key;
    const empty = node('option', '', placeholder); empty.value = '';
    select.replaceChildren(empty, ...entries.map(([value, title]) => { const option = node('option', '', title); option.value = value; return option; }));
  }
  select.value = selected;
}
function eventOptions(container, events, selected, disabled, onChange, limit) {
  container.agentOnChange = onChange;
  const key = JSON.stringify(events.map((event) => [event.id, event.title, event.planned_start]));
  if (container.dataset.options !== key) {
    container.dataset.options = key; container.replaceChildren(); container.agentInputs = [];
    for (const event of events) {
      const label = node('label', 'agent-option'); const input = node('input'); input.type = 'checkbox'; input.value = event.id;
      input.addEventListener('change', () => container.agentOnChange(event.id, input.checked));
      label.append(input, node('span', '', event.title)); container.append(label); container.agentInputs.push(input);
    }
    if (!events.length) container.append(node('p', 'read-help', 'No events available for this selection.'));
  }
  for (const input of container.agentInputs || []) { input.checked = selected.includes(input.value); input.disabled = disabled || (!input.checked && selected.length >= limit); }
}
function toggleSelection(values, id, checked) { return checked ? [...new Set([...values, id])] : values.filter((value) => value !== id); }
function calendarEvents() { return forecastState.snapshot?.calendar?.events || []; }
function intelligenceReady() {
  const snapshot = intelligenceState.snapshot;
  return agentActive() && !intelligenceState.busy && !forecastState.busy && !intelligenceState.stale && !forecastState.stale && snapshot?.revision && snapshot.revision === forecastState.snapshot?.revision;
}
function canExtractIntelligence() {
  const snapshot = intelligenceState.snapshot;
  const selected = intelligenceState.eventIds;
  return intelligenceReady() && snapshot.configured && snapshot.metrics.remaining_calls > 0 && snapshot.gmail_revision && snapshot.messages.some((message) => message.id === intelligenceState.messageId) && selected.length >= 1 && selected.length <= 20 && new Set(selected).size === selected.length && selected.every((id) => calendarEvents().some((event) => event.id === id));
}
function acceptIntelligence(data) {
  if (!data || !Array.isArray(data.messages) || !Array.isArray(data.constraints) || !data.metrics || data.provider !== 'Anthropic') throw new Error('The analysis state is incomplete. Refresh excerpts.');
  const previous = intelligenceState.snapshot;
  if (previous?.revision !== data.revision || previous?.gmail_revision !== data.gmail_revision) {
    intelligenceState.messageId = ''; intelligenceState.eventIds = [];
    if (forecastState.snapshot && (forecastState.snapshot.revision !== data.revision || (previous && previous.gmail_revision !== data.gmail_revision))) invalidateForecast();
  }
  intelligenceState.snapshot = data; intelligenceState.stale = false;
}
async function refreshIntelligence() {
  if (!agentActive() || intelligenceState.busy || forecastState.busy) return;
  if (!forecastState.snapshot) await refreshForecast();
  if (!agentActive() || intelligenceState.busy || forecastState.busy) return;
  const version = ++intelligenceState.version;
  intelligenceState.busy = true; intelligenceState.error = ''; renderForecast();
  try {
    const data = await liveApi('/api/intelligence');
    if (version === intelligenceState.version) acceptIntelligence(data);
  } catch (error) {
    if (version === intelligenceState.version) { intelligenceState.error = error.message; intelligenceState.stale = true; }
  } finally { intelligenceState.busy = false; renderForecast(); }
}
async function intelligenceAction(action, fields) {
  if (!intelligenceReady()) return;
  const snapshot = intelligenceState.snapshot;
  const version = ++intelligenceState.version;
  const body = { revision: snapshot.revision, ...fields };
  intelligenceState.busy = true; intelligenceState.error = ''; forecastState.preview = null; renderForecast();
  let updateForecast = false;
  try {
    const data = await liveApi(`/api/intelligence/${action}`, body);
    if (version === intelligenceState.version) {
      acceptIntelligence(data);
      if (action !== 'extract' || forecastState.snapshot?.revision !== data.revision) { invalidateForecast(); updateForecast = true; }
    }
  } catch (error) {
    if (version === intelligenceState.version) {
      intelligenceState.error = error.message; intelligenceState.stale = true;
      // A GET may refresh counters after a failed attempt; never replay a model request.
      if (agentActive()) {
        try { const data = await liveApi('/api/intelligence'); if (version === intelligenceState.version) acceptIntelligence(data); }
        catch { /* An explicit refresh is required when the server cannot be reached. */ }
      }
    }
  } finally { intelligenceState.busy = false; renderForecast(); }
  if (updateForecast && agentActive()) await refreshForecast();
}
async function extractIntelligence() {
  if (!canExtractIntelligence()) return;
  const snapshot = intelligenceState.snapshot;
  await intelligenceAction('extract', { gmail_revision: snapshot.gmail_revision, message_id: intelligenceState.messageId, event_ids: [...intelligenceState.eventIds], consent: true });
}
async function confirmIntelligence() {
  const snapshot = intelligenceState.snapshot; const proposal = snapshot?.result?.proposal;
  if (!intelligenceReady() || !proposal?.proposal_id || !proposal.requires_confirmation) return;
  await intelligenceAction('confirm', { gmail_revision: snapshot.gmail_revision, proposal_id: proposal.proposal_id });
}
async function resetIntelligence() { if (intelligenceReady()) await intelligenceAction('reset', {}); }
function renderIntelligence() {
  const snapshot = intelligenceState.snapshot;
  const disabled = !agentActive() || intelligenceState.busy || forecastState.busy;
  const ready = intelligenceReady();
  $('intelligence-panel').setAttribute('aria-busy', String(intelligenceState.busy));
  $('intelligence-refresh').disabled = disabled;
  $('intelligence-notice').textContent = intelligenceState.busy ? 'Analysing or refreshing… No other call will start automatically.' : intelligenceState.error ? intelligenceState.error + (intelligenceState.stale ? ' Refresh excerpts before trying again.' : '') : !snapshot ? 'Refresh the imported excerpts. Then use the imported calendar to choose relevant events.' : !snapshot.configured ? 'Claude is not configured on this server. Your Google reads and the demo remain available.' : !snapshot.messages.length ? 'No excerpts in memory. Start a Gmail read in “My connections”.' : !ready ? 'Refresh the imported calendar and excerpts before continuing.' : 'Choose an excerpt and its events. You will still need to confirm the proposed evidence.';
  $('intelligence-notice').classList.toggle('connection-error', Boolean(intelligenceState.error));
  const metrics = snapshot?.metrics;
  const tokens = (value) => value === null || value === undefined ? 'unknown' : number(value);
  $('intelligence-metrics').textContent = metrics ? `Anthropic${snapshot.model ? ' · ' + snapshot.model : ''} · ${metrics.calls} call(s) of ${metrics.limit} · ${metrics.remaining_calls} remaining. Input tokens: ${tokens(metrics.input_tokens)}; output: ${tokens(metrics.output_tokens)}. The budget persists after clearing data.` : 'Anthropic · budget of 10 calls per session. Token counts are unknown until measured.';
  selectOptions($('intelligence-message'), (snapshot?.messages || []).map((message) => [message.id, message.subject || 'Message with no subject']), intelligenceState.messageId, 'Choose a message');
  $('intelligence-message').disabled = disabled || !snapshot || intelligenceState.stale;
  const message = snapshot?.messages.find((item) => item.id === intelligenceState.messageId);
  $('intelligence-excerpt').textContent = message?.excerpt || 'The selected excerpt will appear here before anything is sent to Anthropic.';
  $('intelligence-event-fields').disabled = !ready;
  eventOptions($('intelligence-events'), calendarEvents(), intelligenceState.eventIds, !ready, (id, checked) => { intelligenceState.eventIds = toggleSelection(intelligenceState.eventIds, id, checked); renderIntelligence(); }, 20);
  $('intelligence-consent').textContent = `By clicking “Analyse with Claude”, you authorise sending the displayed excerpt and ${intelligenceState.eventIds.length} selected event(s) to Anthropic. No other message is sent. The analysis may use a billable call; it does not change your calendar.`;
  $('intelligence-extract').disabled = !canExtractIntelligence();
  $('intelligence-extract').textContent = intelligenceState.busy ? 'Analysing…' : 'Analyse with Claude';
  const result = snapshot?.result; const proposal = result?.proposal; const container = $('intelligence-result'); container.replaceChildren();
  if (proposal) {
    const event = calendarEvents().find((item) => item.id === proposal.event_id);
    container.append(node('h3', '', `Proposed arrival: ${readDate(proposal.deadline, forecastState.snapshot?.calendar?.timezone)}`), node('p', '', event?.title || 'Selected event'), node('blockquote', 'intelligence-excerpt', proposal.evidence_quote), node('p', 'read-help', 'Quote from the Gmail excerpt. Check its meaning and date before confirming this simulation constraint.'));
  } else if (result) container.append(node('p', 'read-help', 'No usable arrival time proposed in this analysis. No constraint added.'));
  if (result?.cached) container.append(node('p', 'read-help', 'Result already in memory, with no new model call.'));
  $('intelligence-confirm').hidden = !proposal;
  $('intelligence-confirm').disabled = !ready || !proposal?.requires_confirmation;
  $('intelligence-reset').disabled = !ready || (!snapshot?.constraints.length && !result);
  $('intelligence-constraints').replaceChildren(...(snapshot?.constraints || []).map((constraint) => node('li', '', `${calendarEvents().find((event) => event.id === constraint.event_id)?.title || 'Event'}: confirmed arrival before ${readDate(constraint.deadline, forecastState.snapshot?.calendar?.timezone)}.`)));
}
function planSelectionValid() {
  const selection = forecastState.selection; const events = calendarEvents();
  return selection.target && events.some((event) => event.id === selection.target) && selection.movable.length >= 1 && selection.movable.length <= 5 && new Set(selection.movable).size === selection.movable.length && selection.movable.every((id) => id !== selection.target && events.some((event) => event.id === id && event.private === true));
}
async function findLivePlans() {
  if (!forecastActive() || forecastState.busy || forecastState.stale || !['ready', 'simulated'].includes(forecastState.snapshot?.status)) return;
  if (!planSelectionValid()) { forecastState.error = 'Choose a fixed target and 1 to 5 distinct personal blocks to move.'; renderForecast(); return; }
  const selection = forecastState.selection; const calendar = forecastState.snapshot.calendar;
  try {
    const start = calendarInstant(selection.start, calendar.timezone); const end = calendarInstant(selection.end, calendar.timezone);
    const duration = Date.parse(end) - Date.parse(start);
    if (!(duration > 0 && duration <= 24 * 3600000) || Date.parse(start) < Date.parse(calendar.horizon.start) || Date.parse(end) > Date.parse(calendar.horizon.end)) throw new Error('The window must be no longer than 24 hours and stay within the imported period.');
    await forecastAction('/api/live/plans', { target_event_id: selection.target, movable_event_ids: [...selection.movable], window: {start, end} });
  } catch (error) { forecastState.error = error.message; renderForecast(); }
}
function previewLivePlan(id) {
  if (!forecastActive() || forecastState.busy || forecastState.stale) return;
  const plan = forecastState.snapshot?.planning?.plans.find((item) => item.id === id);
  if (!plan) return;
  forecastState.preview = plan; renderForecast();
  $('live-preview-banner').focus?.();
  $('live-preview-banner').scrollIntoView?.({block: 'nearest'});
}
function closeLivePreview() { forecastState.preview = null; renderForecast(); }
function renderLivePlans() {
  const snapshot = forecastState.snapshot; const selection = forecastState.selection;
  const disabled = !forecastActive() || forecastState.busy || forecastState.stale || !['ready', 'simulated'].includes(snapshot?.status);
  const events = calendarEvents(); const timezone = snapshot?.calendar?.timezone;
  selectOptions($('live-plan-target'), events.map((event) => [event.id, event.title]), selection.target, 'Choose an appointment');
  $('live-plan-target').disabled = disabled; $('live-movable-fields').disabled = disabled;
  eventOptions($('live-movable-events'), events.filter((event) => event.private === true && event.id !== selection.target), selection.movable, disabled, (id, checked) => { selection.movable = toggleSelection(selection.movable, id, checked); renderLivePlans(); }, 5);
  for (const key of ['start', 'end']) { $(`live-plan-${key}`).disabled = disabled; $(`live-plan-${key}`).value = selection[key]; }
  $('live-plan-window-help').textContent = `A window of at most 24 hours within the imported period${timezone ? ', time zone ' + timezone : ''}. Unknown locations and routes are not invented.`;
  $('live-plan-search').disabled = disabled || !planSelectionValid() || !selection.start || !selection.end;
  const planning = snapshot?.planning;
  $('live-plan-status').textContent = planning ? `${planning.status === 'no_better_plan' ? 'No improvement found in this limited search.' : `${planning.plans.length} alternative(s) calculated.`} ${planning.search.evaluated_candidates} candidate(s) evaluated.${planning.search.truncated ? ' Budget reached: search stopped.' : ''}` : 'Choose what can move and a window that works for you.';
  const list = $('live-plan-list'); list.replaceChildren();
  for (const plan of planning?.plans || []) {
    const selected = forecastState.preview?.id === plan.id;
    const card = node('article', `plan-card${selected ? ' selected' : ''}`);
    card.append(node('h3', '', `Proposal · ${shiftLabel(plan.shift_minutes)}`), node('p', 'plan-risk-caption', 'Risk for the target appointment'));
    const risks = node('div', 'plan-risks');
    for (const [label, value] of [['Reference', plan.target_risk_before], ['Proposal', plan.target_risk_after]]) { const metric = node('div'); metric.append(node('span', '', label), node('strong', '', percent(value))); risks.append(metric); }
    card.append(risks);
    for (const operation of plan.operations) {
      const title = events.find((event) => event.id === operation.event_id)?.title || 'Selected block';
      card.append(node('p', 'moved-event-name', title));
      for (const [label, times] of [['Before', operation.before], ['After', operation.after]]) card.append(node('p', 'read-help', `${label} : ${readDate(times.planned_start, timezone)} – ${readDate(times.planned_end, timezone)}`));
    }
    const button = node('button', 'secondary', selected ? 'Preview active' : 'Preview'); button.type = 'button'; button.disabled = disabled;
    button.setAttribute('aria-pressed', String(selected)); button.addEventListener('click', () => previewLivePlan(plan.id)); card.append(button);
    const apply = node('button', 'primary apply-live-plan', 'Apply this plan to Calendar'); apply.type = 'button';
    apply.disabled = disabled || !calendarWriteReady() || actionsState.snapshot.budget.remaining <= 0;
    apply.addEventListener('click', () => applyLivePlan(plan.id));
    card.append(node('p', 'read-help', 'Apply updates the times shown above in your primary Google Calendar. It requires separate write access; no attendee notifications are sent.'), apply); list.append(card);
  }
  $('live-preview-banner').hidden = !forecastState.preview;
  $('live-preview-close').disabled = forecastState.busy || intelligenceState.busy;
  $('live-preview-description').textContent = forecastState.preview ? `The times and risks below match this proposal. Target risk: ${percent(forecastState.preview.target_risk_before)} → ${percent(forecastState.preview.target_risk_after)}. No calendar changes.` : '';
}
$('intelligence-refresh').addEventListener('click', refreshIntelligence);
$('intelligence-message').addEventListener('change', () => { intelligenceState.messageId = $('intelligence-message').value; renderIntelligence(); });
$('intelligence-extract').addEventListener('click', extractIntelligence);
$('intelligence-confirm').addEventListener('click', confirmIntelligence);
$('intelligence-reset').addEventListener('click', resetIntelligence);
$('live-plan-target').addEventListener('change', () => { forecastState.selection.target = $('live-plan-target').value; forecastState.selection.movable = forecastState.selection.movable.filter((id) => id !== forecastState.selection.target); renderLivePlans(); });
for (const key of ['start', 'end']) $(`live-plan-${key}`).addEventListener('input', () => { forecastState.selection[key] = $(`live-plan-${key}`).value; renderLivePlans(); });
$('live-plans-form').addEventListener('submit', (event) => { event.preventDefault(); findLivePlans(); });
$('live-preview-close').addEventListener('click', closeLivePreview);

// Write consent and receipts are separate from read-only sources and local previews.
function actionsLocalSession() {
  return Boolean(liveState.csrfToken) && !liveState.sessionError && liveState.canonicalOrigin === window.location.origin;
}
function calendarWriteReady() {
  return actionsLocalSession() && !actionsState.loading && !actionsState.pending && !actionsState.stale && !liveState.loading
    && !forecastState.busy && !intelligenceState.busy && !providerIds.some((id) => liveState.pending[id])
    && actionsState.snapshot?.connected === true && !actionsState.snapshot.busy && !actionsState.snapshot.pending;
}
function acceptCalendarActions(data) {
  if (!data || data.provider !== 'google_calendar_write' || !Array.isArray(data.receipts) || !data.budget
    || typeof data.connected !== 'boolean' || !['ready', 'missing', 'invalid'].includes(data.configuration)) throw new Error('Write access status is incomplete. Refresh write access and receipts.');
  actionsState.snapshot = data; actionsState.stale = false;
}
async function refreshCalendarActions() {
  if (!actionsLocalSession() || actionsState.pending) return;
  const version = ++actionsState.version;
  actionsState.loading = true; actionsState.error = ''; renderForecast();
  try {
    const data = await liveApi('/api/actions');
    if (version === actionsState.version) acceptCalendarActions(data);
  } catch (error) {
    if (version === actionsState.version) { actionsState.error = error.message; actionsState.stale = true; }
  } finally {
    if (version === actionsState.version) actionsState.loading = false;
    renderConnections();
  }
}
async function calendarAction(action, fields = {}) {
  if (!actionsLocalSession() || actionsState.pending || actionsState.loading || liveState.loading || forecastState.busy || intelligenceState.busy || providerIds.some((id) => liveState.pending[id])) return;
  if (!['connect', 'forget', 'execute', 'undo'].includes(action)) return;
  if (action === 'connect' && (actionsState.stale || actionsState.snapshot?.configuration !== 'ready')) return;
  if (['execute', 'undo'].includes(action) && !calendarWriteReady()) return;
  const version = ++actionsState.version;
  actionsState.pending = action; actionsState.error = '';
  suspendAgentWork();
  if (['execute', 'undo', 'forget'].includes(action)) { invalidateForecast(); invalidateIntelligence(); }
  if (action === 'execute' || action === 'undo') actionsState.calendarNeedsRead = true;
  renderConnections();
  if (action === 'execute' || action === 'undo') {
    $('calendar-actions').focus?.({ preventScroll: true });
    $('calendar-actions').scrollIntoView?.({ block: 'nearest' });
  }
  let authorizationUrl;
  try {
    const data = await liveApi(`/api/actions/${action}`, fields);
    if (version !== actionsState.version) return;
    if (action === 'connect') {
      const url = new URL(data.authorization_url);
      if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com' || url.username || url.password || url.port) throw new Error('The Google consent address is invalid. Refresh write access before trying again.');
      authorizationUrl = url.href;
      actionsState.snapshot = { ...actionsState.snapshot, connected: false, pending: true };
    } else acceptCalendarActions(data);
  } catch (error) {
    if (version === actionsState.version) {
      actionsState.error = error.message; actionsState.stale = true;
      // Reading metadata can recover a receipt; a mutation is never replayed.
      if (actionsLocalSession()) {
        try { const data = await liveApi('/api/actions'); if (version === actionsState.version) acceptCalendarActions(data); }
        catch { /* Keep previous receipts stale until an explicit refresh succeeds. */ }
      }
    }
  } finally { actionsState.pending = null; renderConnections(); }
  if (authorizationUrl && version === actionsState.version && actionsLocalSession()) window.location.assign(authorizationUrl);
}
async function applyLivePlan(id) {
  const snapshot = forecastState.snapshot;
  const plan = snapshot?.planning?.plans.find((item) => item.id === id);
  if (!plan || !forecastActive() || forecastState.stale || !calendarWriteReady() || actionsState.snapshot.budget.remaining <= 0) return;
  await calendarAction('execute', { revision: snapshot.revision, plan_id: plan.id, approved: true });
}
async function undoCalendarReceipt(id) {
  const receipt = actionsState.snapshot?.receipts.find((item) => item.id === id);
  if (!calendarWriteReady() || receipt?.status !== 'applied') return;
  await calendarAction('undo', { receipt_id: receipt.id, approved: true });
}
function renderCalendarActions() {
  const snapshot = actionsState.snapshot;
  const busy = Boolean(actionsState.pending) || actionsState.loading;
  const blocked = !actionsLocalSession() || busy || liveState.loading || forecastState.busy || intelligenceState.busy || providerIds.some((id) => liveState.pending[id]);
  $('calendar-actions').setAttribute('aria-busy', String(busy || Boolean(snapshot?.busy)));
  $('actions-refresh').disabled = !actionsLocalSession() || busy;
  $('actions-connect').disabled = blocked || actionsState.stale || snapshot?.configuration !== 'ready' || Boolean(snapshot?.busy);
  $('actions-connect').textContent = snapshot?.connected ? 'Reconnect Calendar write access' : 'Connect Calendar write access';
  $('actions-forget').disabled = blocked || !snapshot || Boolean(snapshot?.busy);
  $('actions-notice').textContent = actionsState.pending ? 'Calendar action in progress. No other write will start automatically.' : actionsState.loading ? 'Refreshing write access and receipts. This refresh does not read Google data.'
    : actionsState.error ? actionsState.error + ' No write will be retried automatically. Refresh receipts to check the outcome.'
    : actionsState.stale ? 'Refresh write access and receipts before applying or undoing a plan.'
    : snapshot?.busy ? 'A Calendar action is still running on the server. Refresh receipts to check its outcome.'
    : snapshot?.configuration !== 'ready' ? 'Calendar write access is not configured on this server. Read-only connections and local previews remain available.'
    : snapshot?.connected ? 'Calendar write access is connected. Apply a real plan only after reviewing its exact time changes.'
    : snapshot?.pending ? 'Write consent is pending. Complete it in Google, then refresh write access.' : 'Connect Calendar write access separately to apply a real plan. Connecting alone does not change an event.';
  $('actions-notice').classList.toggle('connection-error', Boolean(actionsState.error));
  $('actions-budget').textContent = snapshot ? `${snapshot.budget.executions} execution attempt(s) of ${snapshot.budget.limit} · ${snapshot.budget.remaining} remaining. Undo does not use this budget. Forgetting write access does not reset it.` : '';
  const container = $('actions-receipts'); container.replaceChildren();
  const descriptions = {
    applied: ['Applied', 'The requested times were applied. Undo restores the original times only if Calendar has not changed since.'],
    undone: ['Undone', 'The original times were restored.'],
    unknown: ['Outcome unknown', 'Check Google Calendar before any further action. This uncertain receipt cannot be retried or undone here.'],
    conflict: ['Conflict or refused', 'The action could not be completed safely. Check Google Calendar and read it again before making a new plan.'],
    partial: ['Partially completed', 'Some changes may remain. Check Google Calendar; automatic retry and undo are unavailable for this receipt.'],
    compensated: ['Rolled back', 'The plan was not completed; earlier changes were restored. Check Calendar before planning again.'],
    denied: ['Not authorised', 'The plan did not meet the requirements for an authorised Calendar change.']
  };
  for (const [index, receipt] of [...(snapshot?.receipts || [])].reverse().entries()) {
    const [label, explanation] = descriptions[receipt.status] || ['Unconfirmed outcome', 'Refresh receipts and check Google Calendar. No automatic retry is available.'];
    const card = node('article', `action-receipt${['unknown', 'partial', 'conflict'].includes(receipt.status) ? ' action-receipt-alert' : ''}`);
    card.append(node('h3', '', `Change ${snapshot.receipts.length - index} · ${label}`), node('p', 'read-help', explanation));
    for (const operation of receipt.actions || []) {
      const event = liveState.providers.google_calendar?.data?.events?.find((item) => item.id === operation.event_id);
      card.append(node('strong', 'moved-event-name', event?.title || operation.event_id));
      for (const [title, times] of [['Original times', operation.before], ['Requested times', operation.after]]) {
        if (times) card.append(node('p', 'read-help', `${title}: ${readDate(times.planned_start)} – ${readDate(times.planned_end)} (Europe/Paris)`));
      }
    }
    if (receipt.status === 'applied') {
      const undo = node('button', 'secondary', 'Undo this Calendar change'); undo.type = 'button'; undo.disabled = !calendarWriteReady();
      undo.addEventListener('click', () => undoCalendarReceipt(receipt.id)); card.append(undo);
    }
    container.append(card);
  }
  $('actions-empty').hidden = Boolean(snapshot?.receipts.length);
}
$('actions-refresh').addEventListener('click', refreshCalendarActions);
$('actions-connect').addEventListener('click', () => calendarAction('connect'));
$('actions-forget').addEventListener('click', () => calendarAction('forget'));
initConnections();
