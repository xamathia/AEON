"""Offline tests for the Google token endpoint trust boundary."""

import importlib
import io
import traceback
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler


ENDPOINT = 'https://oauth2.googleapis.com/token'
SECRET = 'fixture-secret-never-report'


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status
        self.read_limits = []

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class TokenTransportTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('packages.aeon_oauth.transport')
        self.requests = []
        self.openers = []
        self.response = Response(b'{"access_token":"fixture-token","expires_in":3600}')

        def open_response(opener, request, **kwargs):
            self.openers.append(opener)
            self.requests.append((request, kwargs))
            return self.response

        opener_patch = patch(
            'urllib.request.OpenerDirector.open', autospec=True,
            side_effect=open_response)
        self.open = opener_patch.start()
        self.addCleanup(opener_patch.stop)

    def assert_error(self, code, action):
        try:
            action()
        except self.module.OAuthError as error:
            self.assertEqual(error.code, code)
            rendered = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            self.assertNotIn(SECRET, str(error))
            self.assertNotIn(SECRET, repr(error))
            self.assertNotIn(SECRET, rendered)
        else:
            self.fail('Expected a sanitized OAuthError.')

    def test_posts_credentials_only_in_urlencoded_body_to_fixed_https_endpoint(self):
        transport = self.module.TokenTransport()
        result = transport.post_form({
            'grant_type': 'authorization_code',
            'client_id': 'fixture-client',
            'client_secret': SECRET,
            'code': 'fixture &code=+/é',
            'code_verifier': 'fixture-verifier',
        })
        self.assertEqual(result, {'access_token': 'fixture-token', 'expires_in': 3600})
        self.assertEqual(len(self.requests), 1)
        request, options = self.requests[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(urlsplit(request.full_url).query, '')
        self.assertEqual(request.get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(parse_qs(request.data.decode('ascii')), {
            'grant_type': ['authorization_code'],
            'client_id': ['fixture-client'],
            'client_secret': [SECRET],
            'code': ['fixture &code=+/é'],
            'code_verifier': ['fixture-verifier'],
        })
        self.assertNotIn(SECRET, repr(request.header_items()))
        self.assertEqual(options, {'timeout': 10})
        self.assertTrue(self.response.closed)
        self.assertNotIn(SECRET, repr(transport))

    def test_passes_a_custom_bounded_timeout(self):
        self.module.TokenTransport(timeout=0.25).post_form({'code': 'fixture-code'})
        self.assertEqual(self.requests[0][1], {'timeout': 0.25})

    def test_rejects_invalid_timeout_before_any_request(self):
        for value in (0, -1, 30.01, float('inf'), float('-inf'), float('nan'), True, None, SECRET):
            with self.subTest(type=type(value).__name__):
                self.assert_error('invalid_configuration', lambda: self.module.TokenTransport(timeout=value))
        self.assertEqual(self.requests, [])

    def test_does_not_install_environment_proxy_handlers(self):
        with patch('urllib.request.getproxies', return_value={'https': 'http://proxy.invalid:8080'}):
            self.module.TokenTransport().post_form({'code': 'fixture-code'})
        self.assertFalse(any(isinstance(handler, ProxyHandler)
                             for handler in self.openers[0].handlers))

    def test_redirects_never_send_a_second_request(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                seen = []
                response = Response(b'', status=status)
                headers = Message()
                headers['Location'] = 'https://untrusted.invalid/collect'

                def redirect(opener, request, **kwargs):
                    seen.append(request.full_url)
                    if len(seen) > 1:
                        raise URLError(SECRET)
                    return opener.error('http', request, response, status, 'redirect', headers)

                self.open.side_effect = redirect
                self.assert_error('transport_error', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
                self.assertEqual(seen, [ENDPOINT])
                self.assertTrue(response.closed)

    def test_accepts_the_response_size_limit_and_bounds_the_read(self):
        self.response = Response(b'{"value":"' + b'x' * 65524 + b'"}')
        result = self.module.TokenTransport().post_form({'code': 'fixture-code'})
        self.assertEqual(len(result['value']), 65524)
        self.assertEqual(self.response.read_limits, [65537])

    def test_rejects_an_oversize_success_response(self):
        self.response = Response(b'{"value":"' + b'x' * 65525 + b'"}')
        self.assert_error('invalid_response', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
        self.assertEqual(self.response.read_limits, [65537])
        self.assertTrue(self.response.closed)

    def test_rejects_nonobject_malformed_and_nonstandard_json(self):
        invalid = (
            b'[]', b'null', b'true', b'123', b'"string"', b'', b'{broken',
            b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}',
            b'{"value":1e999}', b'{"value":-1e999}',
            b'{"value":1,"value":2}', b'{"nested":{"value":1,"value":2}}',
            b'{"value":"\xff"}', b'\xef\xbb\xbf{}',
        )
        for index, body in enumerate(invalid):
            with self.subTest(case=index):
                self.response = Response(body)
                self.assert_error('invalid_response', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
                self.assertTrue(self.response.closed)

    def test_returns_valid_json_objects_for_caller_schema_validation(self):
        self.response = Response(b'{"extra":[true,null,{"value":1.25}],"label":"caf\xc3\xa9"}')
        result = self.module.TokenTransport().post_form({'code': 'fixture-code'})
        self.assertEqual(result, {'extra': [True, None, {'value': 1.25}], 'label': 'café'})

    def test_maps_invalid_grant_without_exposing_error_description(self):
        body = Response(('{"error":"invalid_grant","error_description":"' + SECRET + '"}').encode())
        self.open.side_effect = HTTPError(ENDPOINT, 400, SECRET, {}, body)
        self.assert_error('invalid_grant', lambda: self.module.TokenTransport().post_form({'refresh_token': SECRET}))
        self.assertEqual(body.read_limits, [65537])
        self.assertTrue(body.closed)

    def test_rejects_other_http_errors_and_untrusted_error_bodies(self):
        bodies = (
            b'{"error":"invalid_client"}', b'{"error":123}', b'[]',
            b'{broken', b'{"error":"invalid_grant","error":"invalid_client"}',
            b'{"error":"invalid_grant","value":NaN}',
            b'{"error":"invalid_grant","detail":"\xff"}',
            b'{"error":"invalid_grant","padding":"' + b'x' * 65536 + b'"}',
        )
        for index, raw in enumerate(bodies):
            with self.subTest(case=index):
                body = Response(raw)
                self.open.side_effect = HTTPError(ENDPOINT, 400, SECRET, {}, body)
                self.assert_error('token_rejected', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
                self.assertEqual(body.read_limits, [65537])
                self.assertTrue(body.closed)

    def test_sanitizes_network_and_response_read_failures(self):
        for failure in (URLError(SECRET), TimeoutError(SECRET), OSError(SECRET), RuntimeError(SECRET)):
            with self.subTest(kind=type(failure).__name__):
                self.open.side_effect = failure
                self.assert_error('transport_error', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
        self.open.side_effect = None
        self.open.return_value = self.response
        with patch.object(self.response, 'read', side_effect=OSError(SECRET)):
            self.assert_error('transport_error', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
        self.assertTrue(self.response.closed)

    def test_sanitizes_failure_reading_an_http_error_body(self):
        body = Response(b'')
        self.open.side_effect = HTTPError(ENDPOINT, 400, SECRET, {}, body)
        with patch.object(body, 'read', side_effect=OSError(SECRET)):
            self.assert_error('token_rejected', lambda: self.module.TokenTransport().post_form({'code': SECRET}))
        self.assertTrue(body.closed)

    def test_sanitizes_failure_closing_an_http_error_body(self):
        body = Response(b'{"error":"invalid_client"}')
        self.open.side_effect = HTTPError(ENDPOINT, 400, SECRET, {}, body)
        with patch.object(body, 'close', side_effect=OSError(SECRET)):
            self.assert_error('token_rejected', lambda: self.module.TokenTransport().post_form({'code': SECRET}))

    def test_does_not_accept_unexpected_response_status(self):
        for status, code in ((302, 'transport_error'), (400, 'token_rejected'), (500, 'token_rejected')):
            with self.subTest(status=status):
                self.response = Response(b'{"access_token":"fixture-token"}', status=status)
                self.assert_error(code, lambda: self.module.TokenTransport().post_form({'code': SECRET}))

    def test_rejects_nonstring_form_fields_without_network_or_value_conversion(self):
        class UnsafeValue:
            def __str__(self):
                raise AssertionError(SECRET)

        for fields in (None, SECRET, {'code': UnsafeValue()}, {1: SECRET}, {'code': b'bytes'}):
            with self.subTest(kind=type(fields).__name__):
                self.assert_error('invalid_configuration', lambda: self.module.TokenTransport().post_form(fields))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
