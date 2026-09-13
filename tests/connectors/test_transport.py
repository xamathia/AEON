import io
import socket
import traceback
import unittest
from unittest import mock
import urllib.error

from packages.aeon_connectors import ConnectorError, HttpTransport
from packages.aeon_connectors.transport import _SafeRedirectHandler


class FakeResponse:
    def __init__(self, body=b"{}", *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        pass


class HttpTransportTests(unittest.TestCase):
    def test_encodes_json_and_returns_decoded_object(self):
        transport = HttpTransport()
        transport._opener = mock.Mock()
        transport._opener.open.return_value = FakeResponse(
            b'{"answer":42}', headers={"X-Test": "yes"}
        )

        response = transport.request(
            "post",
            "https://example.test/resource",
            {"Authorization": "Bearer secret-token"},
            {"name": "ÆON"},
        )

        request = transport._opener.open.call_args.args[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual(b'{"name":"\xc3\x86ON"}', request.data)
        self.assertEqual("application/json", request.get_header("Content-type"))
        self.assertEqual({"answer": 42}, response.body)
        self.assertEqual(200, response.status)

    def test_rejects_non_https_and_url_credentials(self):
        transport = HttpTransport()
        for url in (
            "http://example.test",
            "https://user:password@example.test",
            "https://example.test?access_token=secret-token",
            "https://example.test#secret-token",
            "not-a-url",
        ):
            with self.subTest(url=url), self.assertRaises(ConnectorError) as raised:
                transport.request("GET", url, {})
            self.assertEqual("invalid_url", raised.exception.code)

    def test_rejects_invalid_or_oversized_json(self):
        for body, code in (
            (b"not-json", "invalid_json"),
            (b"[]", "invalid_json"),
            (b'{"x":NaN}', "invalid_json"),
            (b'{"long":"value"}', "response_too_large"),
        ):
            with self.subTest(code=code):
                transport = HttpTransport(max_response_bytes=10)
                transport._opener = mock.Mock()
                transport._opener.open.return_value = FakeResponse(body)
                with self.assertRaises(ConnectorError) as raised:
                    transport.request("GET", "https://example.test", {})
                self.assertEqual(code, raised.exception.code)

    def test_timeout_is_redacted_and_retryable(self):
        transport = HttpTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = socket.timeout("secret-token private-body")

        with self.assertRaises(ConnectorError) as raised:
            transport.request(
                "GET",
                "https://example.test",
                {"Authorization": "Bearer secret-token"},
            )

        error = raised.exception
        self.assertEqual("timeout", error.code)
        self.assertTrue(error.retryable)
        self.assertNotIn("secret-token", str(error))
        self.assertNotIn("private-body", repr(error))
        formatted = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertNotIn("secret-token", formatted)
        self.assertNotIn("private-body", formatted)

    def test_timeout_while_reading_http_error_is_wrapped_and_redacted(self):
        failing_body = mock.Mock()
        failing_body.read.side_effect = socket.timeout(
            "secret-token private-error-body"
        )
        http_error = urllib.error.HTTPError(
            "https://example.test",
            503,
            "Service Unavailable",
            {},
            failing_body,
        )
        transport = HttpTransport()
        transport._opener = mock.Mock()
        transport._opener.open.side_effect = http_error

        with self.assertRaises(ConnectorError) as raised:
            transport.request(
                "GET",
                "https://example.test",
                {"Authorization": "Bearer secret-token"},
            )

        error = raised.exception
        self.assertEqual("timeout", error.code)
        self.assertTrue(error.retryable)
        formatted = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertNotIn("secret-token", formatted)
        self.assertNotIn("private-error-body", formatted)

    def test_blocks_cross_host_or_non_https_redirect_with_credentials(self):
        handler = _SafeRedirectHandler()
        request = mock.Mock()
        request.full_url = "https://www.googleapis.com/resource"
        request.header_items.return_value = [("Authorization", "Bearer secret-token")]
        for target in (
            "https://attacker.example/resource",
            "http://www.googleapis.com/resource",
        ):
            with self.subTest(target=target), self.assertRaises(ConnectorError) as raised:
                handler.redirect_request(request, None, 302, "Found", {}, target)
            self.assertEqual("unsafe_redirect", raised.exception.code)
            self.assertNotIn("secret-token", repr(raised.exception))

    def test_content_length_is_checked_before_reading(self):
        transport = HttpTransport(max_response_bytes=10)
        transport._opener = mock.Mock()
        response = FakeResponse(b"{}", headers={"Content-Length": "11"})
        response.read = mock.Mock(side_effect=AssertionError("must not read"))
        transport._opener.open.return_value = response
        with self.assertRaises(ConnectorError) as raised:
            transport.request("GET", "https://example.test", {})
        self.assertEqual("response_too_large", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
