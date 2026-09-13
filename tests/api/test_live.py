"""Live forecasting tests use injected observations, never local credentials."""
import copy
import http.client
import json
from datetime import datetime, timezone
import threading
import unittest

from apps.api.configuration import Configuration
from apps.api.connections import ConnectionService, APIError
from apps.api.live import LiveService
from packages.aeon_engine import simulate_day

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc).timestamp()
HORIZON = {'start': '2026-09-14T00:00:00Z', 'end': '2026-09-15T00:00:00Z'}


def event(identifier, start, end):
    return {'id': identifier, 'title': 'Observation ' + identifier,
            'planned_start': start, 'planned_end': end, 'classification': 'fixed', 'private': True,
            'overrun_minutes': {'min': 0, 'mode': 0, 'max': 0},
            'source': {'provider': 'google_calendar', 'reference': 'cal:' + identifier,
                       'synthetic': False, 'assumption': 'Uncalibrated zero prior.'},
            'etag': 'INTERNAL_ETAG', 'organizer_self': True, 'calendar_id': 'primary'}


EVENTS = [event('b', '2026-09-14T12:00:00+02:00', '2026-09-14T13:00:00+02:00'),
          event('a', '2026-09-14T09:00:00Z', '2026-09-14T09:30:00Z')]


class OAuth:
    def __init__(self, *args, **kwargs): pass
    def access_token(self, session): return 'offline-token'
    def status(self, session): return {'connected': True, 'scopes': [], 'expires_at': NOW + 3600}
    def forget(self, session): pass
    def begin(self, session): return {'authorization_url': 'https://accounts.google.com/?state=fixture'}


class Calendar:
    events = EVENTS
    incomplete = False
    fail = False
    def __init__(self, token): pass
    def list_events(self, *args, **kwargs):
        if self.fail: raise RuntimeError('PRIVATE_ERROR')
        return {'events': copy.deepcopy(self.events), 'excluded_events': [1] if self.incomplete else [],
                'deleted_event_ids': [], 'next_page_token': None}


class Routes:
    calls = []
    fail = False
    started = None
    release = None
    def __init__(self, key): self.key = key
    def compute_route(self, origin, destination, departure_time):
        self.calls.append((origin, destination, departure_time))
        if self.started:
            self.started.set()
            assert self.release.wait(3)
        if self.fail: raise RuntimeError('PRIVATE_KEY_ADDRESS_ERROR')
        return {'duration_seconds': 1200, 'static_duration_seconds': 1000, 'distance_meters': 1000,
                'observed_at': '2026-09-13T00:00:00Z',
                'source': {'provider': 'google_routes', 'reference': 'route:fixture',
                           'synthetic': False, 'assumption': 'Point-in-time observation.'}}


class LiveTests(unittest.TestCase):
    def setUp(self):
        Calendar.events, Calendar.incomplete, Calendar.fail = EVENTS, False, False
        Routes.calls, Routes.fail, Routes.started, Routes.release = [], False, None, None
        self.connections = ConnectionService(8787, configuration=Configuration({
            'AEON_GOOGLE_CLIENT_ID': 'fixture', 'AEON_GOOGLE_REDIRECT_URI': 'http://127.0.0.1:8787/',
            'AEON_GOOGLE_ROUTES_API_KEY': 'fixture-key'}), oauth_factory=OAuth, calendar_factory=Calendar,
            clock=lambda: NOW)
        self.session, _ = self.connections.session(create=True)
        self.assemblies, self.simulations = [], []
        self.live = LiveService(self.connections, assembler_loader=lambda: self.assemble,
                                engine_loader=lambda: self.engine, routes_factory=Routes, clock=lambda: NOW)

    def assemble(self, **kwargs):
        self.assemblies.append(copy.deepcopy(kwargs))
        issues = []
        if not kwargs['calendar']['complete']: issues.append({'code': 'CALENDAR_INCOMPLETE', 'event_ids': []})
        events = sorted(kwargs['calendar']['events'], key=lambda e: datetime.fromisoformat(e['planned_start'].replace('Z', '+00:00')))
        if not events: issues.append({'code': 'NO_EVENTS', 'event_ids': []})
        for left, right in zip(events, events[1:]):
            if not any(edge['from_event_id'] == left['id'] and edge['to_event_id'] == right['id'] for edge in kwargs['travel_edges']):
                issues.append({'code': 'MISSING_TRAVEL_EDGE', 'event_ids': [left['id'], right['id']]})
        payload = {'schema_version': '1.0', 'scenario_id': kwargs['scenario_id'], 'mode': 'live',
                   'timezone': kwargs['timezone'], 'horizon': kwargs['horizon'], 'seed': kwargs['seed'],
                   'samples': kwargs['samples'], 'events': events,
                   'travel_edges': kwargs['travel_edges'], 'constraints': kwargs['constraints']}
        return {'status': 'incomplete' if issues else 'ready', 'scenario': None if issues else payload,
                'issues': issues, 'provenance': {}, 'assumptions': []}

    def engine(self, payload):
        self.simulations.append(copy.deepcopy(payload))
        return simulate_day(payload)

    def read(self):
        return self.connections.read(self.session, 'google_calendar',
                                     {'time_min': HORIZON['start'], 'time_max': HORIZON['end'], 'timezone': 'Europe/Paris'})

    def travel_body(self, kind='same_location'):
        state = self.live.describe(self.session)
        body = {'revision': state['revision'], 'from_event_id': 'a', 'to_event_id': 'b', 'kind': kind}
        if kind == 'google_routes': body.update(origin_address='Origin fixture', destination_address='Destination fixture')
        return body

    def assert_code(self, code, action):
        with self.assertRaises(APIError) as found: action()
        self.assertEqual(found.exception.code, code)
        self.assertNotIn('PRIVATE', str(found.exception))

    def test_no_read_never_invents_calendar(self):
        state = self.live.describe(self.session)
        self.assertIsNone(state['revision'])
        self.assertEqual(state['issues'][0]['code'], 'CALENDAR_NOT_READ')
        self.assertEqual(self.assemblies, [])

    def test_preserves_canonical_data_and_sorts_utc_without_exposing_metadata(self):
        self.read()
        original = copy.deepcopy(EVENTS)
        state = self.live.describe(self.session)
        self.assertEqual([e['id'] for e in state['calendar']['events']], ['a', 'b'])
        self.assertEqual(state['travel_pairs'][0]['departure_time'], EVENTS[1]['planned_end'])
        self.assertEqual(self.assemblies[-1]['calendar']['events'], original)
        self.assertNotIn('INTERNAL_ETAG', str(state))
        self.assertEqual(EVENTS, original)

    def test_missing_edge_prevents_engine(self):
        self.read()
        state = self.live.describe(self.session)
        result = self.live.simulate(self.session, {'revision': state['revision']})
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['result'])
        self.assertEqual(self.simulations, [])

    def test_simultaneous_starts_use_assembler_identifier_tie_break(self):
        Calendar.events = [dict(EVENTS[0], planned_start=EVENTS[1]['planned_start']), EVENTS[1]]
        self.read()
        state = self.live.describe(self.session)
        self.assertEqual([event['id'] for event in state['calendar']['events']], ['a', 'b'])
        self.assertEqual(state['travel_pairs'][0]['from_event_id'], 'a')

    def test_missing_assembler_is_explicit_without_engine(self):
        self.read()
        def missing(): raise ImportError()
        self.live._assembler_loader = missing
        state = self.live.describe(self.session)
        self.assertEqual(state['issues'][0]['code'], 'ASSEMBLER_UNAVAILABLE')
        self.assertIsNone(self.live.simulate(self.session, {'revision': state['revision']})['result'])
        self.assertEqual(self.simulations, [])

    def test_forget_during_simulation_discards_result(self):
        self.read()
        ready = self.live.travel(self.session, self.travel_body())
        started, release = threading.Event(), threading.Event()
        def blocked(payload):
            started.set()
            self.assertTrue(release.wait(3))
            return simulate_day(payload)
        self.live._engine_loader = lambda: blocked
        errors = []
        def run():
            try: self.live.simulate(self.session, {'revision': ready['revision']})
            except APIError as error: errors.append(error.code)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(2))
        self.connections.forget(self.session, 'google_calendar')
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['revision_conflict'])
        self.assertIsNone(self.live.describe(self.session)['result'])

    def test_same_location_is_explicit_and_matches_real_engine(self):
        self.read()
        ready = self.live.travel(self.session, self.travel_body())
        result = self.live.simulate(self.session, {'revision': ready['revision']})
        self.assertEqual(result['status'], 'simulated')
        self.assertEqual(result['result'], simulate_day(self.simulations[-1]))
        self.assertEqual(self.simulations[-1]['constraints'], [])
        self.assertEqual(self.simulations[-1]['travel_edges'][0]['source']['provider'], 'user')
        self.assertEqual(Routes.calls, [])

    def test_installed_assembler_and_engine_integrate_without_fixture_ids(self):
        from packages.aeon_scenario import assemble_scenario
        self.live._assembler_loader = lambda: assemble_scenario
        self.read()
        state = self.live.travel(self.session, self.travel_body('google_routes'))
        self.assertEqual(state['status'], 'ready')
        result = self.live.simulate(self.session, {'revision': state['revision']})
        self.assertEqual(result['status'], 'simulated')
        self.assertEqual([item['event_id'] for item in result['result']['events']], ['a', 'b'])
        self.assertEqual(result['result'], simulate_day(self.simulations[-1]))
        self.assertEqual(result['result']['mode'], 'live')
        self.assertEqual(len(Routes.calls), 1)

    def test_routes_uses_explicit_addresses_departure_and_prior(self):
        self.read()
        state = self.live.travel(self.session, self.travel_body('google_routes'))
        self.assertEqual(Routes.calls, [({'address': 'Origin fixture'}, {'address': 'Destination fixture'}, EVENTS[1]['planned_end'])])
        self.assertEqual(state['routes']['attempts'], 1)
        self.assertEqual(state['travel_pairs'][0]['travel']['duration_minutes'], {'min': 16, 'mode': 20, 'max': 28})
        self.assertIn('lower=0.8', state['travel_pairs'][0]['travel']['source']['assumption'])
        self.assertNotIn('fixture-key', str(state))

    def test_missing_key_has_no_request_or_budget_charge(self):
        self.read()
        self.connections._routes_key = None
        self.assert_code('routes_unavailable', lambda: self.live.travel(self.session, self.travel_body('google_routes')))
        self.assertEqual(Routes.calls, [])
        self.assertEqual(self.live.describe(self.session)['routes']['attempts'], 0)

    def test_open_routing_needs_no_google_key_and_caches_only_in_session(self):
        self.read()
        self.connections._routes_key = None
        clients, calls = [], []
        class OpenRoutes:
            def __init__(self): clients.append(self)
            def compute_route(self, origin, destination):
                calls.append((origin, destination))
                return {'duration_minutes': {'min': 16, 'mode': 20, 'max': 28},
                        'origin_label': origin, 'destination_label': destination,
                        'observed_at': '2026-09-13T00:00:00Z',
                        'source': {'provider': 'osrm', 'reference': 'osrm:fixture', 'synthetic': False,
                                   'assumption': 'Estimate without live traffic.'}}
        self.live._open_routes_factory = OpenRoutes
        body = dict(self.travel_body(), kind='osrm', origin_address=' Origin ', destination_address='Destination')
        state = self.live.travel(self.session, body)
        self.live.travel(self.session, body)
        self.assertEqual(calls, [('Origin', 'Destination')])
        self.assertEqual(len(clients), 1)
        self.assertTrue(state['routes']['open_routing'])
        self.assertFalse(state['routes']['configured'])
        self.assertEqual(state['travel_pairs'][0]['travel']['source']['provider'], 'osrm')
        self.assertEqual(Routes.calls, [])
        self.connections.forget(self.session, 'google_calendar')
        self.read()
        body['revision'] = self.live.describe(self.session)['revision']
        state = self.live.travel(self.session, body)
        self.assertEqual(len(clients), 2)
        self.assertEqual(state['routes']['attempts'], 2)

    def test_repeated_route_reuses_observation_until_expiry_without_spending_budget(self):
        self.read()
        body = self.travel_body('google_routes')
        first = self.live.travel(self.session, body)
        self.session.route_attempts = 20
        reused = self.live.travel(self.session, body)
        self.assertEqual(len(Routes.calls), 1)
        self.assertTrue(reused['travel_pairs'][0]['travel']['cached'])
        self.assertEqual(reused['travel_pairs'][0]['travel']['observed_at'], first['travel_pairs'][0]['travel']['observed_at'])
        self.live._clock = lambda: NOW + 301
        self.assert_code('routes_budget_exhausted', lambda: self.live.travel(self.session, body))
        self.session.route_attempts = 1
        refreshed = self.live.travel(self.session, body)
        self.assertEqual(len(Routes.calls), 2)
        self.assertFalse(refreshed['travel_pairs'][0]['travel']['cached'])

    def test_open_routing_failures_share_budget_and_hide_external_errors(self):
        self.read()
        calls = []
        class OpenRoutes:
            def compute_route(self, *args):
                calls.append(args)
                raise RuntimeError('PRIVATE_ADDRESS_ERROR')
        self.live._open_routes_factory = OpenRoutes
        body = dict(self.travel_body(), kind='osrm', origin_address='Origin', destination_address='Destination')
        self.session.route_attempts = 19
        self.assert_code('routes_failed', lambda: self.live.travel(self.session, body))
        self.assert_code('routes_budget_exhausted', lambda: self.live.travel(self.session, body))
        self.assert_code('routes_budget_exhausted', lambda: self.live.travel(self.session, self.travel_body('google_routes')))
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.live.describe(self.session)['travel_pairs'][0]['travel'])

    def test_unknown_pair_extra_fields_and_bad_address_fail_before_network(self):
        self.read()
        for change in ({'to_event_id': 'unknown'}, {'extra': True}, {'origin_address': ''}, {'kind': []}):
            body = self.travel_body('google_routes')
            body.update(change)
            self.assert_code('invalid_request', lambda: self.live.travel(self.session, body))
        self.assertEqual(Routes.calls, [])

    def test_budget_counts_failures_and_survives_reset(self):
        self.read()
        Routes.fail = True
        for _ in range(20):
            self.assert_code('routes_failed', lambda: self.live.travel(self.session, self.travel_body('google_routes')))
        self.assertEqual(len(Routes.calls), 20)
        state = self.live.describe(self.session)
        self.live.reset(self.session, {'revision': state['revision']})
        self.assert_code('routes_budget_exhausted', lambda: self.live.travel(self.session, self.travel_body('google_routes')))
        self.assertEqual(len(Routes.calls), 20)

    def test_read_success_changes_revision_and_failure_keeps_it_with_warning(self):
        self.read()
        state = self.live.travel(self.session, self.travel_body())
        Calendar.fail = True
        self.assert_code('read_failed', self.read)
        stale = self.live.describe(self.session)
        self.assertEqual(stale['revision'], state['revision'])
        self.assertTrue(stale['calendar']['last_read_failed'])
        Calendar.fail = False
        self.read()
        fresh = self.live.describe(self.session)
        self.assertNotEqual(fresh['revision'], state['revision'])
        self.assertIsNone(fresh['travel_pairs'][0]['travel'])
        self.assert_code('revision_conflict', lambda: self.live.simulate(self.session, {'revision': state['revision']}))

    def test_forget_clears_canonical_and_forecast(self):
        self.read()
        self.live.travel(self.session, self.travel_body())
        self.connections.forget(self.session, 'google_calendar')
        state = self.live.describe(self.session)
        self.assertIsNone(state['calendar'])
        self.assertEqual(state['travel_pairs'], [])

    def test_partial_calendar_and_limit_never_simulate(self):
        Calendar.incomplete = True
        self.read()
        ready = self.live.travel(self.session, self.travel_body())
        state = self.live.simulate(self.session, {'revision': ready['revision']})
        self.assertEqual(state['status'], 'incomplete')
        self.assertEqual(self.simulations, [])
        Calendar.incomplete = False
        Calendar.events = [dict(EVENTS[0], id=str(i)) for i in range(101)]
        self.read()
        state = self.live.describe(self.session)
        self.assertEqual(state['issues'][0]['code'], 'CALENDAR_TOO_LARGE')
        self.assertEqual(len(state['calendar']['events']), 101)

    def test_inflight_route_after_new_read_cannot_repopulate(self):
        self.read()
        body = self.travel_body('google_routes')
        Routes.started, Routes.release = threading.Event(), threading.Event()
        errors = []
        def perform():
            try: self.live.travel(self.session, body)
            except APIError as error: errors.append(error.code)
        worker = threading.Thread(target=perform)
        worker.start()
        self.assertTrue(Routes.started.wait(2))
        self.assert_code('live_busy', lambda: self.live.travel(self.session, body))
        self.read()
        Routes.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['revision_conflict'])
        self.assertIsNone(self.live.describe(self.session)['travel_pairs'][0]['travel'])
        self.assertEqual(len(Routes.calls), 1)

    def test_past_departure_has_no_request(self):
        self.read()
        self.live._clock = lambda: NOW + 10 * 86400
        self.assert_code('departure_in_past', lambda: self.live.travel(self.session, self.travel_body('google_routes')))
        self.assertEqual(Routes.calls, [])

    def test_real_plan_receives_only_explicit_private_selection_and_keeps_internal_scenario(self):
        self.read()
        state = self.live.travel(self.session, self.travel_body())
        calls = []
        def planner(payload, selection):
            calls.append((copy.deepcopy(payload), copy.deepcopy(selection)))
            return {'status': 'plans_found', 'baseline': simulate_day(payload),
                    'plans': [{'id': 'plan', 'scenario': payload}], 'search': {}, 'llm_calls': 0}
        self.live._planner_loader = lambda: planner
        body = {'revision': state['revision'], 'target_event_id': 'b', 'movable_event_ids': ['a'],
                'window': {'start': '2026-09-14T07:00:00Z', 'end': '2026-09-14T14:00:00Z'}}
        result = self.live.plans(self.session, body)
        self.assertEqual(calls[0][0]['events'][0]['classification'], 'flexible')
        self.assertEqual(calls[0][0]['events'][1]['classification'], 'fixed')
        self.assertEqual(calls[0][1]['max_candidates'], 60)
        self.assertNotIn('scenario', result['planning']['plans'][0])
        self.assertIn('scenario', self.session.live_state['planning']['plans'][0])
        self.assertTrue(all(event['classification'] == 'fixed' for event in self.session.sources['google_calendar'].canonical_data['events']))
        self.live.travel(self.session, self.travel_body())
        self.assertIsNone(self.live.describe(self.session)['planning'])

    def test_real_planning_refuses_nonprivate_or_third_party_events(self):
        for change in ({'private': False}, {'organizer_self': False}, {'attendees_count': 1}, {'attendees_omitted': True}):
            Calendar.events = [dict(event, **change) if event['id'] == 'a' else event for event in EVENTS]
            self.read()
            state = self.live.travel(self.session, self.travel_body())
            with self.assertRaises(APIError):
                self.live.plans(self.session, {'revision': state['revision'], 'target_event_id': 'b',
                    'movable_event_ids': ['a'], 'window': HORIZON})

    def test_gmail_change_during_planner_invalidates_its_result(self):
        self.read()
        state = self.live.travel(self.session, self.travel_body())
        def planner(payload, selection):
            self.connections.forget(self.session, 'gmail')
            return {'baseline': simulate_day(payload), 'plans': []}
        self.live._planner_loader = lambda: planner
        with self.assertRaises(APIError) as caught:
            self.live.plans(self.session, {'revision': state['revision'], 'target_event_id': 'b',
                'movable_event_ids': ['a'], 'window': HORIZON})
        self.assertEqual(caught.exception.code, 'revision_conflict')
        self.assertIsNone(self.live.describe(self.session)['planning'])


class LiveHTTPTests(unittest.TestCase):
    def setUp(self):
        from apps.api.server import create_server
        self.fixture = LiveTests()
        self.fixture.setUp()
        self.server = create_server(port=0)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        service = ConnectionService(self.server.server_port, configuration=Configuration({
            'AEON_GOOGLE_CLIENT_ID': 'fixture', 'AEON_GOOGLE_REDIRECT_URI': self.origin + '/',
            'AEON_GOOGLE_ROUTES_API_KEY': 'fixture-key'}), oauth_factory=OAuth, calendar_factory=Calendar,
            clock=lambda: NOW)
        self.server.connections = service
        self.server.live = LiveService(service, assembler_loader=lambda: self.fixture.assemble,
                                       routes_factory=Routes, clock=lambda: NOW)
        self.worker = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)
        self.cookie = self.csrf = None
        status, headers, data = self.request('GET', '/api/session')
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.csrf = data['csrf_token']

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, method, path, body=None, overrides=None, cookie=True):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Origin': self.origin, 'Content-Type': 'application/json'}
        if self.cookie and cookie: headers['Cookie'] = self.cookie
        if self.csrf: headers['X-Aeon-CSRF'] = self.csrf
        headers.update(overrides or {})
        try:
            client.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = client.getresponse()
            return response.status, response.headers, json.loads(response.read())
        finally:
            client.close()

    def read(self):
        status, _, _ = self.request('POST', '/api/calendar/read', {
            'time_min': HORIZON['start'], 'time_max': HORIZON['end'], 'timezone': 'Europe/Paris'})
        self.assertEqual(status, 200)
        return self.request('GET', '/api/live/scenario')[2]

    def test_private_get_requires_session_and_same_origin(self):
        self.assertEqual(self.request('GET', '/api/live/scenario', cookie=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/live/scenario', overrides={'Origin': 'https://outside.invalid'})[0], 403)
        status, headers, data = self.request('GET', '/api/live/scenario')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(data['issues'][0]['code'], 'CALENDAR_NOT_READ')

    def test_all_actions_require_csrf_and_origin(self):
        state = self.read()
        for path in ('travel', 'simulate', 'reset'):
            body = {'revision': state['revision']}
            self.assertEqual(self.request('POST', '/api/live/' + path, body, {'X-Aeon-CSRF': 'wrong'})[0], 403)
            self.assertEqual(self.request('POST', '/api/live/' + path, body, {'Origin': 'https://outside.invalid'})[0], 403)
        self.assertEqual(Routes.calls, [])

    def test_end_to_end_http_matches_engine_and_reset_preserves_calendar(self):
        state = self.read()
        body = {'revision': state['revision'], 'from_event_id': 'a', 'to_event_id': 'b', 'kind': 'same_location'}
        status, _, ready = self.request('POST', '/api/live/travel', body)
        self.assertEqual(status, 200)
        self.assertEqual(ready['status'], 'ready')
        status, _, result = self.request('POST', '/api/live/simulate', {'revision': state['revision']})
        self.assertEqual(status, 200)
        self.assertEqual(result['result'], simulate_day(self.fixture.assemblies[-1]['calendar'] | {
            'schema_version': '1.0', 'scenario_id': 'live:' + state['revision'], 'mode': 'live',
            'timezone': 'Europe/Paris', 'horizon': HORIZON, 'seed': 42, 'samples': 1000,
            'events': sorted(EVENTS, key=lambda e: e['id']),
            'travel_edges': self.fixture.assemblies[-1]['travel_edges'], 'constraints': []}))
        status, _, reset = self.request('POST', '/api/live/reset', {'revision': state['revision']})
        self.assertEqual(status, 200)
        self.assertNotEqual(reset['revision'], state['revision'])
        self.assertIsNone(reset['result'])
        self.assertEqual(len(reset['calendar']['events']), 2)

    def test_client_cannot_submit_scenario_or_provider_sources(self):
        state = self.read()
        for key in ('scenario', 'constraints', 'calendar', 'source', 'seed'):
            self.assertEqual(self.request('POST', '/api/live/simulate', {'revision': state['revision'], key: {}})[0], 400)
        self.assertNotIn('INTERNAL_ETAG', json.dumps(state))
        self.assertNotIn('fixture-key', json.dumps(state))


if __name__ == '__main__': unittest.main()
