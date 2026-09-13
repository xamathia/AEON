"""Session-scoped Gmail interpretation, with fake model and real numeric engine."""
import copy
import threading
import unittest
import test_live

from apps.api.configuration import Configuration
from apps.api.connections import APIError, ConnectionService
from apps.api.intelligence import IntelligenceService
from apps.api.live import LiveService
from test_live import Calendar, EVENTS, HORIZON, NOW, OAuth


class Gmail:
    def __init__(self, token): pass
    def list_messages(self, *args, **kwargs): return {'messages': [{'id': 'mail'}]}
    def get_message(self, identifier):
        return {'id': identifier, 'text': 'Appointment on 14 September 2026: arrive before 10:45 Paris time.',
                'subject': 'AEON demo', 'from': 'not-sent-to-model', 'date': '', 'truncated': False}


class Model:
    def __init__(self): self.requests, self.fail, self.hook = [], False, None
    def generate(self, request, *, max_output_tokens):
        self.requests.append(copy.deepcopy(request))
        if self.hook: self.hook()
        if self.fail: raise RuntimeError('PRIVATE_API_KEY')
        return {'output': {'schema_version': '1.0', 'status': 'proposed', 'event_id': 'b',
                'deadline': '2026-09-14T10:45:00+02:00', 'evidence_quote': 'arrive before 10:45 Paris time'},
                'usage': {'input_tokens': 100, 'output_tokens': 40}}


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        Calendar.events, Calendar.incomplete, Calendar.fail = copy.deepcopy(EVENTS), False, False
        self.connections = ConnectionService(8787, configuration=Configuration({
            'AEON_GOOGLE_CLIENT_ID': 'fixture', 'AEON_GOOGLE_REDIRECT_URI': 'http://127.0.0.1:8787/'}),
            oauth_factory=OAuth, calendar_factory=Calendar, gmail_factory=Gmail, clock=lambda: NOW)
        self.session, _ = self.connections.session(create=True)
        self.live = LiveService(self.connections)
        self.model = Model()
        self.service = IntelligenceService(self.connections, self.live, model=self.model)
        self.read_calendar()
        self.connections.read(self.session, 'gmail', {'query': 'subject:AEON'})

    def read_calendar(self):
        self.connections.read(self.session, 'google_calendar',
            {'time_min': HORIZON['start'], 'time_max': HORIZON['end'], 'timezone': 'Europe/Paris'})

    def body(self):
        state = self.service.describe(self.session)
        return {'revision': state['revision'], 'gmail_revision': state['gmail_revision'],
                'message_id': 'mail', 'event_ids': ['a', 'b'], 'consent': True}

    def confirm(self, result):
        return self.service.confirm(self.session, {'revision': result['revision'],
            'gmail_revision': result['gmail_revision'], 'proposal_id': result['result']['proposal']['proposal_id']})

    def test_proposal_is_not_constraint_until_confirm_and_changes_real_risk(self):
        body = self.body()
        self.live.travel(self.session, {'revision': body['revision'], 'from_event_id': 'a', 'to_event_id': 'b', 'kind': 'same_location'})
        before = self.live.simulate(self.session, {'revision': body['revision']})
        result = self.service.extract(self.session, body)
        self.assertEqual(result['constraints'], [])
        self.assertTrue(result['result']['proposal']['requires_confirmation'])
        self.confirm(result)
        after = self.live.simulate(self.session, {'revision': body['revision']})
        self.assertEqual(before['result']['events'][1]['late_arrival_probability'], 0)
        self.assertEqual(after['result']['events'][1]['late_arrival_probability'], 1)
        self.assertEqual(after['constraints'][0]['source']['provider'], 'gmail')

    def test_only_selected_data_reaches_model_and_cache_retains_budget(self):
        body = self.body()
        first = self.service.extract(self.session, body)
        second = self.service.extract(self.session, body)
        self.assertEqual(len(self.model.requests), 1)
        self.assertTrue(second['result']['cached'])
        self.assertEqual(second['metrics']['calls'], 1)
        self.assertEqual(second['metrics']['input_tokens'], 100)
        self.assertNotIn('not-sent-to-model', str(self.model.requests))

    def test_consent_unknown_message_or_event_fails_before_model(self):
        for changes in ({'consent': False}, {'message_id': 'unknown'}, {'event_ids': ['unknown']}, {'event_ids': []}, {'event_ids': ['a', 'a']}):
            with self.assertRaises(APIError): self.service.extract(self.session, {**self.body(), **changes})
        self.assertEqual(len(self.model.requests), 0)

    def test_forged_constraint_rejected(self):
        result = self.service.extract(self.session, self.body())
        with self.assertRaises(APIError):
            self.service.confirm(self.session, {'revision': result['revision'], 'gmail_revision': result['gmail_revision'],
                'proposal_id': result['result']['proposal']['proposal_id'], 'deadline': 'forged'})
        self.assertEqual(self.live.describe(self.session)['constraints'], [])

    def test_model_unavailable_is_explicit(self):
        service = IntelligenceService(self.connections, self.live)
        self.assertFalse(service.describe(self.session)['configured'])
        with self.assertRaises(APIError): service.extract(self.session, self.body())

    def test_failures_consume_budget_and_are_redacted(self):
        self.model.fail = True
        for _ in range(10): result = self.service.extract(self.session, self.body())
        self.assertEqual(result['metrics']['calls'], 10)
        self.assertIsNone(result['metrics']['input_tokens'])
        with self.assertRaises(APIError) as caught: self.service.extract(self.session, self.body())
        self.assertEqual(caught.exception.code, 'intelligence_budget_exhausted')
        self.assertNotIn('PRIVATE_API_KEY', str(result))

    def test_forget_gmail_removes_constraint_cache_and_keeps_budget(self):
        self.confirm(self.service.extract(self.session, self.body()))
        self.connections.forget(self.session, 'gmail')
        state = self.service.describe(self.session)
        self.assertEqual(state['constraints'], [])
        self.assertEqual(state['messages'], [])
        self.assertIsNone(state['result'])
        self.assertEqual(state['metrics']['calls'], 1)

    def test_read_gmail_or_calendar_invalidates_pending_proposal(self):
        for source in ('gmail', 'google_calendar'):
            result = self.service.extract(self.session, self.body())
            if source == 'gmail': self.connections.read(self.session, source, {'query': 'subject:AEON'})
            else: self.read_calendar()
            with self.assertRaises(APIError): self.confirm(result)

    def test_forget_during_model_call_cannot_repopulate_state(self):
        self.model.hook = lambda: self.connections.forget(self.session, 'gmail')
        with self.assertRaises(APIError): self.service.extract(self.session, self.body())
        self.assertIsNone(self.service.describe(self.session)['result'])

    def test_forget_before_model_dispatch_prevents_transmission(self):
        from packages.aeon_intelligence import DeadlineExtractor
        connections, session = self.connections, self.session
        class DelayedExtractor:
            def __init__(self, *args, **kwargs): self.inner = DeadlineExtractor(*args, **kwargs)
            def extract(self, **kwargs):
                connections.forget(session, 'gmail')
                return self.inner.extract(**kwargs)
        self.service.extractor_loader = lambda: DelayedExtractor
        with self.assertRaises(APIError): self.service.extract(self.session, self.body())
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.session.intelligence_meter['calls'], 0)

    def test_reset_travel_preserves_confirmed_gmail_constraints(self):
        result = self.service.extract(self.session, self.body())
        self.confirm(result)
        state = self.live.reset(self.session, {'revision': result['revision']})
        self.assertEqual(len(state['constraints']), 1)
        self.assertIsNone(state['travel_pairs'][0]['travel'])

    def test_reset_keeps_budget_but_erases_constraints_and_result(self):
        result = self.service.extract(self.session, self.body())
        self.confirm(result)
        state = self.service.reset(self.session, {'revision': result['revision']})
        self.assertEqual(state['constraints'], [])
        self.assertEqual(state['metrics']['calls'], 1)
        self.assertIsNone(state['result'])

    def test_stale_or_concurrent_operations_do_not_call_model(self):
        body = self.body()
        self.session.live_busy = True
        with self.assertRaises(APIError): self.service.extract(self.session, body)
        self.session.live_busy = False
        self.read_calendar()
        with self.assertRaises(APIError): self.service.extract(self.session, body)
        self.assertEqual(self.model.requests, [])


class IntelligenceHTTPTests(unittest.TestCase):
    request = test_live.LiveHTTPTests.request
    stop = test_live.LiveHTTPTests.stop
    read = test_live.LiveHTTPTests.read

    def setUp(self):
        test_live.LiveHTTPTests.setUp(self)
        self.server.connections._gmail_factory = Gmail
        self.server.intelligence = IntelligenceService(self.server.connections, self.server.live, model=Model())

    def test_headers_and_all_new_routes_require_session_origin_csrf(self):
        self.assertEqual(self.request('GET', '/api/intelligence', cookie=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/intelligence', overrides={'Origin': 'https://outside.invalid'})[0], 403)
        for path in ('/api/intelligence/extract', '/api/intelligence/confirm', '/api/intelligence/reset', '/api/live/plans'):
            self.assertEqual(self.request('POST', path, {}, {'X-Aeon-CSRF': 'wrong'})[0], 403)
        status, headers, state = self.request('GET', '/api/intelligence')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(state['messages'], [])

    def test_http_extract_confirm_uses_server_owned_message(self):
        self.read()
        self.assertEqual(self.request('POST', '/api/gmail/read', {'query': 'subject:AEON'})[0], 200)
        state = self.request('GET', '/api/intelligence')[2]
        body = {'revision': state['revision'], 'gmail_revision': state['gmail_revision'],
                'message_id': 'mail', 'event_ids': ['a', 'b'], 'consent': True}
        self.assertEqual(self.request('POST', '/api/intelligence/extract', {**body, 'excerpt': 'forged'})[0], 400)
        status, _, result = self.request('POST', '/api/intelligence/extract', body)
        self.assertEqual(status, 200)
        self.assertEqual(result['metrics']['calls'], 1)
        status, _, result = self.request('POST', '/api/intelligence/confirm', {
            'revision': result['revision'], 'gmail_revision': result['gmail_revision'],
            'proposal_id': result['result']['proposal']['proposal_id']})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.request('GET', '/api/live/scenario')[2]['constraints']), 1)
