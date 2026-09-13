"""Offline contract tests: only the injected token transport handles credentials."""
import base64
import concurrent.futures
import hashlib
import json
import threading
import traceback
import unittest
from urllib.parse import parse_qs, urlsplit

try:
    from packages.aeon_oauth import GoogleOAuth, OAuthError
except ImportError:
    GoogleOAuth = None
    OAuthError = Exception

CALENDAR = 'https://www.googleapis.com/auth/calendar.events.readonly'
GMAIL = 'https://www.googleapis.com/auth/gmail.readonly'


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post_form(self, fields):
        self.calls.append(dict(fields))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def token(**updates):
    result = {'access_token': 'offline-access-value', 'token_type': 'Bearer',
              'expires_in': 3600, 'refresh_token': 'offline-refresh-value'}
    result.update(updates)
    return result


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(GoogleOAuth, 'GoogleOAuth public API is not implemented')
        self.clock = Clock()
        self.transport = Transport(token())
        self.oauth = self.make_oauth()

    def make_oauth(self, **kwargs):
        options = {'provider': 'google_calendar', 'clock': self.clock,
                   'transport': self.transport}
        options.update(kwargs)
        return GoogleOAuth('offline-client', 'http://127.0.0.1:8787/', **options)

    def begin(self, session='browser-a', oauth=None):
        result = (oauth or self.oauth).begin(session)
        self.assertEqual(result['expires_in'], 300)
        return parse_qs(urlsplit(result['authorization_url']).query)

    def connect(self, session='browser-a', oauth=None):
        instance = oauth or self.oauth
        params = self.begin(session, instance)
        return instance.complete(session, state=params['state'][0], code='offline-code')

    def error_code(self, expected, callback):
        with self.assertRaises(OAuthError) as caught:
            callback()
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def test_pkce_is_bound_to_exchange_and_has_no_verifier_in_url(self):
        result = self.oauth.begin('browser-a')
        parsed = urlsplit(result['authorization_url'])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ('https', 'accounts.google.com', '/o/oauth2/v2/auth'))
        params = parse_qs(parsed.query)
        self.assertEqual(params['response_type'], ['code'])
        self.assertEqual(params['scope'], [CALENDAR])
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertEqual(params['redirect_uri'], ['http://127.0.0.1:8787/'])
        self.assertNotIn('code_verifier', params)
        self.assertEqual(self.transport.calls, [])
        self.oauth.complete('browser-a', state=params['state'][0], code='offline-code')
        fields = self.transport.calls[0]
        verifier = fields['code_verifier']
        self.assertRegex(verifier, r'^[A-Za-z0-9._~-]{43,128}$')
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.assertEqual(params['code_challenge'], [challenge])
        self.assertNotEqual(verifier, params['state'][0])
        self.assertEqual(fields['grant_type'], 'authorization_code')
        self.assertEqual(fields['code'], 'offline-code')
        self.assertEqual(fields['client_id'], 'offline-client')
        self.assertNotIn('client_secret', fields)

    def test_provider_has_separate_consent_and_session_memory(self):
        gmail_transport = Transport(token(scope=GMAIL, access_token='gmail-only-access'))
        gmail = self.make_oauth(provider='gmail', transport=gmail_transport)
        calendar_params = self.begin()
        gmail_params = self.begin(oauth=gmail)
        self.assertEqual(gmail_params['scope'], [GMAIL])
        self.assertNotEqual(calendar_params['state'], gmail_params['state'])
        self.error_code('invalid_state', lambda: gmail.complete('browser-a', state=calendar_params['state'][0], code='code'))
        gmail.complete('browser-a', state=gmail_params['state'][0], code='code')
        self.assertFalse(self.oauth.status('browser-a')['connected'])
        self.assertEqual(gmail.access_token('browser-a'), 'gmail-only-access')

    def test_optional_client_secret_stays_out_of_authorization_url(self):
        oauth = self.make_oauth(client_secret='offline-client-secret')
        params = self.begin(oauth=oauth)
        self.assertNotIn('client_secret', params)
        oauth.complete('browser-a', state=params['state'][0], code='code')
        self.assertEqual(self.transport.calls[0]['client_secret'], 'offline-client-secret')
        self.assertNotIn('offline-client-secret', repr(oauth))

    def test_wrong_session_or_state_does_not_consume_valid_attempt(self):
        params = self.begin()
        for session, state in [('browser-b', params['state'][0]), ('browser-a', 'wrong'), ('browser-a', 'é')]:
            self.error_code('invalid_state', lambda: self.oauth.complete(session, state=state, code='code'))
        self.assertEqual(self.transport.calls, [])
        self.oauth.complete('browser-a', state=params['state'][0], code='code')
        self.assertTrue(self.oauth.status('browser-a')['connected'])

    def test_state_replay_fails_without_second_exchange(self):
        params = self.begin()
        self.oauth.complete('browser-a', state=params['state'][0], code='code')
        self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))
        self.assertEqual(len(self.transport.calls), 1)

    def test_new_attempt_invalidates_previous_state(self):
        first = self.begin()
        second = self.begin()
        self.assertNotEqual(first['state'], second['state'])
        self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=first['state'][0], code='code'))
        self.oauth.complete('browser-a', state=second['state'][0], code='code')

    def test_expiration_boundary_rejects_and_consumes_attempt(self):
        params = self.begin()
        self.clock.now = 1300
        self.error_code('expired_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))
        self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))
        self.assertEqual(self.transport.calls, [])

    def test_user_denial_consumes_attempt_without_network(self):
        params = self.begin()
        self.error_code('authorization_denied', lambda: self.oauth.complete('browser-a', state=params['state'][0], error='access_denied-private'))
        self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))
        self.assertEqual(self.transport.calls, [])

    def test_missing_or_ambiguous_callback_consumes_attempt(self):
        for kwargs in [{}, {'code': ''}, {'code': True}, {'code': 'code', 'error': 'denied'}, {'code': 'code\nunsafe'}]:
            params = self.begin()
            self.error_code('invalid_callback', lambda: self.oauth.complete('browser-a', state=params['state'][0], **kwargs))
            self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))
        self.assertEqual(self.transport.calls, [])

    def test_complete_returns_metadata_and_access_token_is_internal_only(self):
        result = self.connect()
        self.assertEqual(result, {'connected': True, 'scopes': [CALENDAR], 'expires_at': 4600})
        result['scopes'].clear()
        self.assertEqual(self.oauth.status('browser-a')['scopes'], [CALENDAR])
        self.assertEqual(self.oauth.access_token('browser-a'), 'offline-access-value')
        self.assertNotIn('offline-access-value', repr(self.oauth))
        self.assertNotIn('offline-refresh-value', repr(self.oauth))
        self.assertNotIn('offline-access-value', json.dumps(self.oauth.status('browser-a')))
        self.assertEqual(len(self.transport.calls), 1)

    def test_partial_or_wrong_provider_scope_never_connects(self):
        for scope in ['', GMAIL, 'https://www.googleapis.com/auth/calendar.readonly', None, [CALENDAR]]:
            self.transport.responses = [token(scope=scope)]
            params = self.begin()
            with self.assertRaises(OAuthError):
                self.oauth.complete('browser-a', state=params['state'][0], code='code')
            self.assertFalse(self.oauth.status('browser-a')['connected'])

    def test_missing_scope_uses_requested_scope_and_actual_scope_is_reported(self):
        self.transport.responses = [token(scope=CALENDAR + ' openid')]
        self.assertEqual(self.connect()['scopes'], [CALENDAR, 'openid'])

    def test_malformed_token_responses_are_rejected_and_attempt_is_consumed(self):
        invalid = [None, [], {'access_token': 'access'}, token(access_token=''), token(access_token='a\nb'),
                   token(access_token=True), token(token_type='Basic'), token(expires_in=True),
                   token(expires_in=0), token(expires_in=-1), token(expires_in=86401),
                   token(expires_in=1.5), token(expires_in='3600'), token(refresh_token=''),
                   token(refresh_token=[]), token(unused=float('nan')), token(unused=float('inf')),
                   token(unused={'x': object()}), token(unused=('tuple',)), token(access_token='a' * 16385)]
        for response in invalid:
            with self.subTest(response_type=type(response).__name__):
                self.transport.responses = [response]
                params = self.begin()
                self.error_code('invalid_response', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))
                self.assertFalse(self.oauth.status('browser-a')['connected'])
                self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))

    def test_transport_failure_is_expurgated_in_formatted_traceback(self):
        secret = 'injected-sensitive-detail'
        self.transport.responses = [RuntimeError(secret)]
        params = self.begin()
        try:
            self.oauth.complete('browser-a', state=params['state'][0], code='code')
        except OAuthError as error:
            rendered = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            self.assertNotIn(secret, rendered)
            self.assertEqual(error.code, 'transport_error')
        else:
            self.fail('transport failure was accepted')
        self.error_code('invalid_state', lambda: self.oauth.complete('browser-a', state=params['state'][0], code='code'))

    def test_refresh_at_margin_uses_new_token_and_keeps_refresh_when_omitted(self):
        self.connect()
        self.clock.now = 4570
        refreshed = token(access_token='refreshed-access', expires_in=120)
        del refreshed['refresh_token']
        self.transport.responses = [refreshed, token(access_token='second-refresh')]
        self.assertEqual(self.oauth.access_token('browser-a'), 'refreshed-access')
        self.assertEqual(self.transport.calls[-1], {'grant_type': 'refresh_token', 'refresh_token': 'offline-refresh-value', 'client_id': 'offline-client'})
        self.assertEqual(self.oauth.status('browser-a')['expires_at'], 4690)
        self.clock.now = 4660
        self.assertEqual(self.oauth.access_token('browser-a'), 'second-refresh')

    def test_status_never_refreshes_and_expired_unrefreshable_token_disconnects(self):
        response = token()
        del response['refresh_token']
        self.transport.responses = [response]
        self.connect()
        self.clock.now = 4570
        self.assertEqual(self.oauth.access_token('browser-a'), 'offline-access-value')
        self.clock.now = 4600
        self.assertEqual(self.oauth.status('browser-a'), {'connected': False, 'scopes': [], 'expires_at': None})
        self.error_code('not_connected', lambda: self.oauth.access_token('browser-a'))
        self.assertEqual(len(self.transport.calls), 1)

    def test_expired_refreshable_status_is_connected_without_network(self):
        self.connect()
        self.clock.now = 9000
        self.assertTrue(self.oauth.status('browser-a')['connected'])
        self.assertEqual(len(self.transport.calls), 1)

    def test_invalid_grant_refresh_forgets_tokens_and_requires_reconnection(self):
        self.connect()
        self.clock.now = 4570
        self.transport.responses = [{'error': 'invalid_grant', 'error_description': 'private detail'}]
        self.error_code('invalid_grant', lambda: self.oauth.access_token('browser-a'))
        self.assertFalse(self.oauth.status('browser-a')['connected'])
        self.error_code('not_connected', lambda: self.oauth.access_token('browser-a'))
        self.assertEqual(len(self.transport.calls), 2)

    def test_refresh_scope_loss_disconnects_instead_of_reusing_old_authorization(self):
        self.connect()
        self.clock.now = 4570
        self.transport.responses = [token(scope=GMAIL)]
        self.error_code('insufficient_scope', lambda: self.oauth.access_token('browser-a'))
        self.assertFalse(self.oauth.status('browser-a')['connected'])
        self.error_code('not_connected', lambda: self.oauth.access_token('browser-a'))

    def test_refresh_transport_failure_preserves_retry(self):
        self.connect()
        self.clock.now = 4570
        self.transport.responses = [RuntimeError('private'), token(access_token='retried-token')]
        self.error_code('transport_error', lambda: self.oauth.access_token('browser-a'))
        self.assertTrue(self.oauth.status('browser-a')['connected'])
        self.assertEqual(self.oauth.access_token('browser-a'), 'retried-token')

    def test_new_connection_does_not_reuse_previous_accounts_refresh(self):
        self.connect()
        second_account = token(access_token='new-account-access')
        del second_account['refresh_token']
        self.transport.responses = [second_account]
        self.connect()
        self.clock.now = 4600
        self.error_code('not_connected', lambda: self.oauth.access_token('browser-a'))
        self.assertEqual(len(self.transport.calls), 2)

    def test_forget_removes_only_selected_session_including_pending_attempt(self):
        self.connect()
        other = self.begin('browser-b')
        self.oauth.forget('browser-a')
        self.assertFalse(self.oauth.status('browser-a')['connected'])
        self.transport.responses = [token(access_token='browser-b-access')]
        self.oauth.complete('browser-b', state=other['state'][0], code='code')
        self.assertEqual(self.oauth.access_token('browser-b'), 'browser-b-access')
        self.oauth.forget('browser-b')
        self.oauth.forget('absent')
        self.assertFalse(self.oauth.status('browser-b')['connected'])

    def test_32_session_limit_and_forget_reclaims_capacity(self):
        for index in range(32):
            self.begin('session-' + str(index))
        self.error_code('session_limit', lambda: self.begin('session-overflow'))
        self.begin('session-0')
        self.oauth.forget('session-0')
        self.begin('session-overflow')

    def test_invalid_redirect_provider_and_configuration_are_rejected(self):
        redirects = ['https://127.0.0.1:8787/', 'http://localhost:8787/', 'http://127.0.0.1/',
                     'http://127.0.0.1:0/', 'http://127.0.0.1:65536/', 'http://127.0.0.1:8787/callback',
                     'http://user@127.0.0.1:8787/', 'http://127.0.0.1:8787/?', 'http://127.0.0.1:8787/#',
                     'http://[::1]:8787/', 'http://127.0.0.1:8787/\n', None]
        for redirect in redirects:
            self.error_code('invalid_configuration', lambda: GoogleOAuth('client', redirect, provider='gmail', transport=self.transport))
        for kwargs in [{'provider': 'routes'}, {'provider': ''}, {'provider': None}, {'client_secret': ''}, {'clock': 5}]:
            self.error_code('invalid_configuration', lambda: self.make_oauth(**kwargs))
        with self.assertRaises(TypeError):
            GoogleOAuth('client', 'http://127.0.0.1:8787/', transport=self.transport)
        for session in ['', None, True, 's' * 257]:
            self.error_code('invalid_session', lambda: self.oauth.begin(session))

    def test_invalid_transport_configuration_does_not_expose_exception_details(self):
        secret = 'private-configuration-detail'
        class BrokenTransport:
            @property
            def post_form(inner):
                raise RuntimeError(secret)
        try:
            self.make_oauth(transport=BrokenTransport())
        except Exception as error:
            self.assertIsInstance(error, OAuthError)
            self.assertEqual(error.code, 'invalid_configuration')
            rendered = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            self.assertNotIn(secret, rendered)
        else:
            self.fail('Invalid transport configuration was accepted')

    def test_same_session_concurrent_complete_exchanges_only_once(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingTransport(Transport):
            def post_form(inner, fields):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test synchronization timed out')
                return super().post_form(fields)
        transport = BlockingTransport(token())
        oauth = self.make_oauth(transport=transport)
        params = self.begin(oauth=oauth)
        def complete():
            return oauth.complete('browser-a', state=params['state'][0], code='code')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(complete)
            self.assertTrue(entered.wait(2))
            second = executor.submit(complete)
            release.set()
            self.assertTrue(first.result(2)['connected'])
            with self.assertRaises(OAuthError):
                second.result(2)
        self.assertEqual(len(transport.calls), 1)

    def test_failed_refresh_is_shared_by_waiters_but_later_call_can_retry(self):
        self.connect()
        self.clock.now = 4570
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        original_post = self.transport.post_form
        self.transport.responses = [RuntimeError('offline transient failure'),
                                    token(access_token='successful-later-retry')]
        def blocking(fields):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test synchronization timed out')
            return original_post(fields)
        self.transport.post_form = blocking
        # Observe contention without replacing the real lock or using timing sleeps.
        entry = self.oauth._lookup('browser-a')
        underlying_lock = entry.lock
        class ObservedLock:
            acquisitions = 0
            def __enter__(inner):
                inner.acquisitions += 1
                if inner.acquisitions == 2:
                    waiting.set()
                underlying_lock.acquire()
                return inner
            def __exit__(inner, *args):
                underlying_lock.release()
        entry.lock = ObservedLock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.oauth.access_token, 'browser-a')
            self.assertTrue(entered.wait(2))
            second = executor.submit(self.oauth.access_token, 'browser-a')
            self.assertTrue(waiting.wait(2))
            release.set()
            for future in (first, second):
                with self.assertRaises(OAuthError) as caught:
                    future.result(2)
                self.assertEqual(caught.exception.code, 'transport_error')
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(self.oauth.status('browser-a')['connected'])
        self.assertEqual(self.oauth.access_token('browser-a'), 'successful-later-retry')
        self.assertEqual(len(self.transport.calls), 3)

    def test_concurrent_refresh_is_single_and_other_session_can_begin(self):
        self.connect()
        self.clock.now = 4570
        entered, release = threading.Event(), threading.Event()
        old_transport = self.transport
        old_transport.responses = [token(access_token='refreshed-shared')]
        # Swap only the injected object's method, keeping the public API under test.
        original = old_transport.post_form
        def blocking(fields):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test synchronization timed out')
            return original(fields)
        old_transport.post_form = blocking
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(self.oauth.access_token, 'browser-a')
            self.assertTrue(entered.wait(2))
            second = executor.submit(self.oauth.access_token, 'browser-a')
            independent = executor.submit(self.oauth.begin, 'browser-b')
            self.assertEqual(independent.result(1)['expires_in'], 300)
            release.set()
            self.assertEqual(first.result(2), 'refreshed-shared')
            self.assertEqual(second.result(2), 'refreshed-shared')
        self.assertEqual(len(old_transport.calls), 2)


if __name__ == '__main__':
    unittest.main()
