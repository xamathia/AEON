"""Real local HTTP + real OAuth protocol, with offline token and source transports."""
import concurrent.futures
import contextlib
import copy
import http.client
import io
import json
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from apps.api import server as application
from packages.aeon_oauth import GoogleOAuth
try:
    from apps.api.connections import ConnectionService
    from apps.api.configuration import Configuration
except ImportError:
    ConnectionService = None
    Configuration = None


class Clock:
    def __init__(self):
        self.now = 1000
    def __call__(self):
        return self.now


class TokenTransport:
    def __init__(self, provider):
        self.provider = provider
        self.calls = []
    def post_form(self, fields):
        self.calls.append(dict(fields))
        return {'access_token': 'fixture-access-' + self.provider,
                'refresh_token': 'fixture-refresh-' + self.provider,
                'token_type': 'Bearer', 'expires_in': 3600}


def event(identifier='event-a', title='Rendez-vous de test'):
    return {'id': identifier, 'title': title, 'planned_start': '2026-09-14T10:00:00+02:00',
            'planned_end': '2026-09-14T11:00:00+02:00', 'classification': 'fixed', 'private': True,
            'source': {'provider': 'google_calendar', 'reference': 'calendar:' + identifier,
                       'synthetic': False, 'assumption': 'fixture'}}


class AuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ConnectionService, 'HTTP connection service is not implemented')
        self.clock = Clock()
        self.transports = {provider: TokenTransport(provider) for provider in ('google_calendar', 'gmail')}
        self.calendar_calls = []
        self.gmail_calls = []
        self.calendar_pages = [{'events': [event()], 'excluded_events': [],
                                'deleted_event_ids': [], 'next_page_token': None}]
        self.gmail_listing = {'messages': [{'id': 'mail-a', 'thread_id': 'thread-a'}], 'next_page_token': None}
        self.gmail_message = {'id': 'mail-a', 'subject': 'Message de test', 'from': 'Fixture',
                              'date': '2026-09-14', 'text': '<script>untrusted text</script>',
                              'snippet': 'extrait', 'truncated': False,
                              'source': {'provider': 'gmail', 'reference': 'gmail:mail-a',
                                         'synthetic': False, 'assumption': 'fixture'}}
        owner = self
        class Calendar:
            def __init__(inner, token):
                owner.assertEqual(token, 'fixture-access-google_calendar')
            def list_events(inner, calendar_id, **kwargs):
                owner.calendar_calls.append((calendar_id, dict(kwargs)))
                page = owner.calendar_pages.pop(0)
                if callable(page):
                    return page()
                if isinstance(page, Exception):
                    raise page
                return copy.deepcopy(page)
        class Gmail:
            def __init__(inner, token):
                owner.assertEqual(token, 'fixture-access-gmail')
            def list_messages(inner, query, **kwargs):
                owner.gmail_calls.append(('list', query, kwargs))
                result = owner.gmail_listing() if callable(owner.gmail_listing) else owner.gmail_listing
                return copy.deepcopy(result)
            def get_message(inner, identifier):
                owner.gmail_calls.append(('get', identifier))
                message = copy.deepcopy(owner.gmail_message)
                message['id'] = identifier
                return message
        self.oauth_factory = lambda *args, provider, **kwargs: GoogleOAuth(
            *args, provider=provider, transport=self.transports[provider], clock=self.clock, **kwargs)
        self.server = application.create_server(port=0)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        configuration = Configuration({'AEON_GOOGLE_CLIENT_ID': 'fixture-client',
                                      'AEON_GOOGLE_CLIENT_SECRET': 'fixture-secret',
                                      'AEON_GOOGLE_REDIRECT_URI': self.origin + '/'})
        self.service = ConnectionService(self.server.server_port, configuration=configuration,
                                         oauth_factory=self.oauth_factory, calendar_factory=Calendar,
                                         gmail_factory=Gmail, clock=self.clock)
        self.server.connections = self.service
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.cookie = None
        self.csrf = None

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None, cookies=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        merged = {'Content-Type': 'application/json', 'Origin': self.origin}
        if cookies and self.cookie:
            merged['Cookie'] = self.cookie
        if self.csrf:
            merged['X-Aeon-CSRF'] = self.csrf
        merged.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
        try:
            connection.request(method, path, body=body, headers=merged)
            response = connection.getresponse()
            raw = response.read()
            return response.status, response.headers, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw
        finally:
            connection.close()

    def session(self):
        status, headers, data = self.request('GET', '/api/session', cookies=False)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.csrf = data['csrf_token']
        return headers, data

    def begin(self, provider='google_calendar'):
        status, _, data = self.request('POST', '/api/oauth/' + provider + '/begin', {})
        self.assertEqual(status, 200)
        self.assertEqual(data['expires_in'], 300)
        return parse_qs(urlsplit(data['authorization_url']).query)['state'][0]

    def connect(self, provider='google_calendar'):
        nonce = self.begin(provider)
        status, headers, _ = self.request('GET', '/?' + urlencode({'state': nonce, 'code': 'fixture-code'}))
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/')
        return nonce

    def connections(self):
        status, _, data = self.request('GET', '/api/connections')
        self.assertEqual(status, 200)
        return data

    def provider(self, name='google_calendar'):
        return next(item for item in self.connections()['providers'] if item['id'] == name)

    def read_calendar(self, **updates):
        body = {'time_min': '2026-09-14T00:00:00+02:00', 'time_max': '2026-09-15T00:00:00+02:00', 'timezone': 'Europe/Paris'}
        body.update(updates)
        return self.request('POST', '/api/calendar/read', body)

    def test_session_cookie_is_host_only_http_only_lax_and_csrf_stable(self):
        headers, data = self.session()
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertIn('Path=/', cookie)
        self.assertIn('Max-Age=3600', cookie)
        self.assertNotIn('Domain=', cookie)
        self.assertEqual(data['canonical_origin'], self.origin)
        self.assertEqual(data['expires_in'], 3600)
        self.assertNotIn(self.cookie.split('=', 1)[1], json.dumps(data))
        status, repeated_headers, repeated = self.request('GET', '/api/session')
        self.assertEqual(status, 200)
        self.assertEqual(repeated['csrf_token'], self.csrf)
        self.assertIsNone(repeated_headers.get('Set-Cookie'))

    def test_unknown_duplicate_and_expired_cookie_are_refused_and_clearable(self):
        self.session()
        for cookie in ['aeon_session=unknown', self.cookie + '; ' + self.cookie]:
            status, headers, _ = self.request('GET', '/api/session', headers={'Cookie': cookie})
            self.assertEqual(status, 401)
            self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.clock.now += 3600
        status, headers, data = self.request('GET', '/api/session')
        self.assertEqual(status, 401)
        self.assertEqual(data['error']['code'], 'session_expired')
        self.assertIn('Max-Age=0', headers['Set-Cookie'])

    def test_session_registry_is_bounded_and_expiration_reclaims_capacity(self):
        for _ in range(32):
            self.session()
        self.assertEqual(self.request('GET', '/api/session', cookies=False)[0], 503)
        self.clock.now += 3600
        self.session()

    def test_localhost_cannot_start_auth_and_cross_origin_cannot_create_session(self):
        status, _, _ = self.request('GET', '/api/session', headers={'Host': 'localhost:' + str(self.server.server_port)})
        self.assertEqual(status, 403)
        self.assertEqual(self.request('GET', '/api/session', headers={'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.request('GET', '/api/session', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_auth_requires_cookie_exact_origin_csrf_json_and_empty_body(self):
        path = '/api/oauth/google_calendar/begin'
        self.assertEqual(self.request('POST', path, {})[0], 401)
        self.session()
        cases = [({}, {'X-Aeon-CSRF': ''}, 403), ({}, {'Origin': ''}, 403),
                 ({}, {'Origin': 'https://other.invalid'}, 403), ({}, {'Content-Type': 'text/plain'}, 415),
                 ({'provider': 'gmail'}, {}, 400), ('{"x":NaN}', {}, 400),
                 ('{"x":1,"x":2}', {}, 400), ('[]', {}, 400)]
        for body, headers, expected in cases:
            self.assertEqual(self.request('POST', path, body, headers)[0], expected)
        self.assertEqual(self.transports['google_calendar'].calls, [])

    def test_callback_connects_only_its_source_and_does_not_read_any_api(self):
        self.session()
        self.connect()
        provider = self.provider()
        self.assertTrue(provider['authorized'])
        self.assertEqual(provider['read']['status'], 'never')
        self.assertIsNone(provider['data'])
        self.assertFalse(self.provider('gmail')['authorized'])
        self.assertEqual(self.calendar_calls, [])
        self.assertEqual(self.gmail_calls, [])
        self.assertEqual(self.connections()['oauth_result']['status'], 'connected')

    def test_callback_with_no_cookie_wrong_state_or_replay_never_exchanges_twice(self):
        self.session()
        nonce = self.begin()
        path = '/?' + urlencode({'state': nonce, 'code': 'fixture-code'})
        self.assertEqual(self.request('GET', path, cookies=False)[0], 303)
        self.assertEqual(self.transports['google_calendar'].calls, [])
        self.assertEqual(self.request('GET', '/?state=wrong&code=fixture-code')[0], 303)
        self.assertEqual(self.transports['google_calendar'].calls, [])
        self.request('GET', path)
        self.request('GET', path)
        self.assertEqual(len(self.transports['google_calendar'].calls), 1)
        self.assertEqual(self.connections()['oauth_result']['code'], 'invalid_state')

    def test_callback_duplicate_parameters_are_rejected_without_sensitive_echo(self):
        self.session()
        nonce = self.begin()
        for suffix in ['&code=fixture-code-2', '&state=another', '&scope=a&scope=b']:
            path = '/?' + urlencode({'state': nonce, 'code': 'fixture-code'}) + suffix
            status, headers, body = self.request('GET', path)
            self.assertEqual(status, 303)
            self.assertEqual(headers['Location'], '/')
            self.assertNotIn(b'fixture-code', body)
        self.assertEqual(self.transports['google_calendar'].calls, [])

    def test_denial_and_expired_state_are_clean_redirects_without_network(self):
        self.session()
        nonce = self.begin()
        self.request('GET', '/?' + urlencode({'state': nonce, 'error': 'access_denied', 'error_description': 'fixture-private-error'}))
        self.assertFalse(self.provider()['authorized'])
        self.assertEqual(self.connections()['oauth_result']['code'], 'authorization_denied')
        nonce = self.begin()
        self.clock.now += 300
        self.request('GET', '/?' + urlencode({'state': nonce, 'code': 'fixture-code'}))
        self.assertEqual(self.connections()['oauth_result']['code'], 'expired_state')
        self.assertEqual(self.transports['google_calendar'].calls, [])

    def test_forget_cancels_pending_state_and_keeps_other_provider(self):
        self.session()
        self.connect('gmail')
        nonce = self.begin()
        self.assertEqual(self.request('POST', '/api/oauth/google_calendar/forget', {})[0], 200)
        self.request('GET', '/?' + urlencode({'state': nonce, 'code': 'fixture-code'}))
        self.assertFalse(self.provider()['authorized'])
        self.assertTrue(self.provider('gmail')['authorized'])
        self.assertEqual(self.transports['google_calendar'].calls, [])

    def test_missing_or_invalid_configuration_preserves_demo_and_never_exposes_values(self):
        self.session()
        for config, expected in [(Configuration({}), 'missing'),
                                 (Configuration({'AEON_GOOGLE_CLIENT_ID': 'fixture-client', 'AEON_GOOGLE_REDIRECT_URI': 'https://unsafe.invalid'}), 'invalid')]:
            self.server.connections = ConnectionService(self.server.server_port, configuration=config)
            self.session()
            provider = self.provider()
            self.assertEqual(provider['configuration'], expected)
            self.assertEqual(self.request('POST', '/api/oauth/google_calendar/begin', {})[0], 503)
            status, _, demo = self.request('GET', '/api/demo')
            self.assertEqual(status, 200)
            self.assertEqual(demo['scenario']['mode'], 'synthetic')
            self.assertNotIn('fixture-client', json.dumps(self.connections()))

    def test_calendar_read_is_explicit_primary_fixed_and_cached(self):
        self.session()
        self.assertEqual(self.read_calendar()[0], 401)
        self.connect()
        status, _, result = self.read_calendar()
        self.assertEqual(status, 200)
        self.assertEqual(result['provider'], 'google_calendar')
        self.assertFalse(result['data']['synthetic'])
        self.assertTrue(result['data']['complete'])
        self.assertEqual(result['data']['events'][0]['classification'], 'fixed')
        calendar, arguments = self.calendar_calls[0]
        self.assertEqual(calendar, 'primary')
        self.assertEqual(arguments['authorized_flexible_ids'], ())
        self.assertEqual(arguments['time_min'], '2026-09-14T00:00:00+02:00')
        self.assertEqual(self.provider()['read']['status'], 'succeeded')
        self.assertEqual(self.provider()['data'], result['data'])

    def test_calendar_horizon_validation_precedes_external_read(self):
        self.session()
        self.connect()
        for changes in [{'time_max': '2026-09-22T00:00:00+02:00'}, {'time_min': '2026-09-14'},
                        {'time_max': '2026-09-13T00:00:00+02:00'}, {'timezone': 'Unknown/Invalid'},
                        {'calendar_id': 'another-calendar'}, {'time_min': True}]:
            self.assertEqual(self.read_calendar(**changes)[0], 400)
        self.assertEqual(self.calendar_calls, [])

    def test_calendar_pagination_is_capped_at_three_pages_and_marks_incomplete(self):
        self.session()
        self.connect()
        self.calendar_pages = [dict(events=[event(str(index))], excluded_events=[], deleted_event_ids=[], next_page_token='private-page-' + str(index)) for index in range(4)]
        status, _, result = self.read_calendar()
        self.assertEqual(status, 200)
        self.assertEqual(result['data']['pages_read'], 3)
        self.assertEqual(len(result['data']['events']), 3)
        self.assertFalse(result['data']['complete'])
        self.assertEqual(len(self.calendar_calls), 3)
        self.assertNotIn('private-page-', json.dumps(result))
        self.assertEqual(self.provider()['read']['status'], 'incomplete')

    def test_read_failure_preserves_prior_cache_and_forget_erases_it(self):
        self.session()
        self.connect()
        previous = self.read_calendar()[2]['data']
        self.calendar_pages = [RuntimeError('fixture-private-read-error')]
        status, _, error = self.read_calendar()
        self.assertEqual(status, 502)
        self.assertNotIn('fixture-private-read-error', json.dumps(error))
        provider = self.provider()
        self.assertEqual(provider['data'], previous)
        self.assertEqual(provider['read']['status'], 'failed')
        self.request('POST', '/api/oauth/google_calendar/forget', {})
        self.assertIsNone(self.provider()['data'])
        self.assertFalse(self.provider()['authorized'])

    def test_gmail_requires_explicit_query_and_returns_only_ten_bounded_excerpts(self):
        self.session()
        self.connect('gmail')
        for body in [{}, {'query': ''}, {'query': ' '}, {'query': 'x' * 501}, {'query': 'a', 'message_id': 'b'}]:
            self.assertEqual(self.request('POST', '/api/gmail/read', body)[0], 400)
        self.assertEqual(self.gmail_calls, [])
        self.gmail_listing = {'messages': [{'id': str(index)} for index in range(12)], 'next_page_token': 'private-page'}
        self.gmail_message['text'] = 'x' * 1500
        status, _, result = self.request('POST', '/api/gmail/read', {'query': 'subject:fixture'})
        self.assertEqual(status, 200)
        self.assertEqual(len(result['data']['messages']), 10)
        self.assertFalse(result['data']['complete'])
        self.assertEqual(result['data']['messages'][0]['excerpt'], 'x' * 1000)
        self.assertNotIn('text', result['data']['messages'][0])
        self.assertEqual(self.gmail_calls[0], ('list', 'subject:fixture', {'max_results': 10}))
        self.assertEqual(len(self.gmail_calls), 11)
        self.assertNotIn('private-page', json.dumps(result))

    def test_live_text_is_preserved_as_data_and_never_enters_demo(self):
        self.session()
        self.connect('gmail')
        original = self.request('GET', '/api/demo')[2]
        result = self.request('POST', '/api/gmail/read', {'query': 'fixture'})[2]
        self.assertEqual(result['data']['messages'][0]['excerpt'], '<script>untrusted text</script>')
        self.assertEqual(self.request('GET', '/api/demo')[2], original)
        self.assertNotIn('fixture-access', json.dumps(self.connections()))
        self.assertNotIn('fixture-secret', json.dumps(self.connections()))

    def test_forget_during_read_prevents_cache_resurrection(self):
        self.session()
        self.connect()
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('offline wait timed out')
            return {'events': [event()], 'excluded_events': [], 'deleted_event_ids': [], 'next_page_token': None}
        self.calendar_pages = [blocked]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            reading = executor.submit(self.read_calendar)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.read_calendar()[0], 409)
            self.assertEqual(self.request('POST', '/api/oauth/google_calendar/forget', {})[0], 200)
            release.set()
            self.assertEqual(reading.result(2)[0], 409)
        self.assertIsNone(self.provider()['data'])
        self.assertFalse(self.provider()['authorized'])

    def test_forget_after_calendar_page_one_prevents_page_two_request(self):
        self.session()
        self.connect()
        entered, release = threading.Event(), threading.Event()
        def blocked_page():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('offline wait timed out')
            return {'events': [event()], 'excluded_events': [], 'deleted_event_ids': [], 'next_page_token': 'page-two'}
        self.calendar_pages = [blocked_page, {'events': [event('second')], 'excluded_events': [], 'deleted_event_ids': [], 'next_page_token': None}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            reading = executor.submit(self.read_calendar)
            self.assertTrue(entered.wait(2))
            self.request('POST', '/api/oauth/google_calendar/forget', {})
            release.set()
            self.assertEqual(reading.result(2)[0], 409)
        self.assertEqual(len(self.calendar_calls), 1)
        self.assertIsNone(self.provider()['data'])

    def test_forget_after_gmail_listing_prevents_message_requests(self):
        self.session()
        self.connect('gmail')
        entered, release = threading.Event(), threading.Event()
        def blocked_listing():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('offline wait timed out')
            return {'messages': [{'id': 'mail-a'}], 'next_page_token': None}
        self.gmail_listing = blocked_listing
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            reading = executor.submit(self.request, 'POST', '/api/gmail/read', {'query': 'fixture'})
            self.assertTrue(entered.wait(2))
            self.request('POST', '/api/oauth/gmail/forget', {})
            release.set()
            self.assertEqual(reading.result(2)[0], 409)
        self.assertEqual(len(self.gmail_calls), 1)
        self.assertIsNone(self.provider('gmail')['data'])

    def test_reconnect_during_token_acquisition_cancels_the_old_read(self):
        self.session()
        self.connect()
        entered, release = threading.Event(), threading.Event()
        oauth = self.service._oauth['google_calendar']
        original = oauth.access_token
        def blocked_token(session_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('offline wait timed out')
            return original(session_id)
        with patch.object(oauth, 'access_token', side_effect=blocked_token):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                reading = executor.submit(self.read_calendar)
                self.assertTrue(entered.wait(2))
                self.connect()
                release.set()
                self.assertEqual(reading.result(2)[0], 409)
        self.assertEqual(self.calendar_calls, [])
        self.assertIsNone(self.provider()['data'])

    def test_http_logs_do_not_contain_callback_codes_or_provider_errors(self):
        self.session()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.connect()
            self.calendar_pages = [RuntimeError('fixture-private-body')]
            self.read_calendar()
        self.assertNotIn('fixture-code', output.getvalue())
        self.assertNotIn('fixture-private-body', output.getvalue())


if __name__ == '__main__':
    unittest.main()
