import base64
import traceback
import unittest
import urllib.parse

from packages.aeon_connectors import ConnectorError, GmailClient, HttpResponse


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response or HttpResponse(200, {}, {})
        self.error = error
        self.calls = []

    def request(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.response


def encoded(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class GmailClientTests(unittest.TestCase):
    def test_lists_one_page_with_real_parameters(self):
        transport = FakeTransport(
            HttpResponse(200, {}, {"messages": [{"id": "m/1", "threadId": "t1"}], "nextPageToken": "p2"})
        )
        result = GmailClient("token-value", transport).list_messages(
            "after:2026/01/01 subject:dinner", max_results=50, page_token="p1"
        )
        method, url, headers = transport.calls[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual("GET", method)
        self.assertEqual(["after:2026/01/01 subject:dinner"], query["q"])
        self.assertEqual(["50"], query["maxResults"])
        self.assertEqual(["p1"], query["pageToken"])
        self.assertEqual("Bearer token-value", headers["Authorization"])
        self.assertEqual({"messages": [{"id": "m/1", "thread_id": "t1"}], "next_page_token": "p2"}, result)

    def test_decodes_nested_plain_text_skips_html_and_attachments_and_truncates(self):
        long_text = "é" * 10_001
        body = {
            "id": "m/1",
            "threadId": "thread-1",
            "snippet": "untrusted snippet",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "Subject", "value": "Dinner"},
                    {"name": "From", "value": "sender@example.test"},
                    {"name": "Date", "value": "Thu, 1 Jan 2026 12:00:00 +0000"},
                ],
                "parts": [
                    {"mimeType": "text/html", "body": {"data": encoded("<script>bad()</script>")}},
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [{"mimeType": "text/plain", "body": {"data": encoded(long_text)}}],
                    },
                    {
                        "mimeType": "text/plain",
                        "filename": "private.txt",
                        "body": {"data": encoded("attachment-secret")},
                    },
                    {
                        "mimeType": "message/rfc822",
                        "filename": "attached.eml",
                        "body": {},
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": encoded("attached-message-secret")},
                            }
                        ],
                    },
                    {"mimeType": "text/plain", "body": {"attachmentId": "attachment-1"}},
                ],
            },
        }
        transport = FakeTransport(HttpResponse(200, {}, body))
        result = GmailClient("token", transport).get_message("m/1")
        self.assertEqual(10_000, len(result["text"]))
        self.assertTrue(result["truncated"])
        self.assertNotIn("script", result["text"])
        self.assertNotIn("attachment-secret", result["text"])
        self.assertNotIn("attached-message-secret", result["text"])
        self.assertEqual("Dinner", result["subject"])
        self.assertEqual("sender@example.test", result["from"])
        self.assertEqual("gmail", result["source"]["provider"])
        method, url, _ = transport.calls[0]
        self.assertEqual("GET", method)
        self.assertIn("/users/me/messages/m%2F1", url)
        self.assertEqual(["full"], urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["format"])

    def test_empty_list_is_valid_and_bounds_are_enforced(self):
        self.assertEqual(
            {"messages": [], "next_page_token": None},
            GmailClient("token", FakeTransport()).list_messages(""),
        )
        for invalid in (0, 51, True, 1.5):
            with self.subTest(value=invalid), self.assertRaises(ConnectorError) as raised:
                GmailClient("token", FakeTransport()).list_messages("query", max_results=invalid)
            self.assertEqual("invalid_arguments", raised.exception.code)

    def test_http_errors_timeout_and_invalid_json_are_structured_and_redacted(self):
        for status, code, retryable in (
            (401, "unauthorized", False),
            (403, "forbidden", False),
            (429, "rate_limited", True),
            (503, "provider_unavailable", True),
        ):
            with self.subTest(status=status):
                client = GmailClient(
                    "credential-secret",
                    FakeTransport(HttpResponse(status, {}, {"private": "mail-body"})),
                )
                with self.assertRaises(ConnectorError) as raised:
                    client.list_messages("query")
                self.assertEqual(code, raised.exception.code)
                self.assertEqual(retryable, raised.exception.retryable)
                self.assertNotIn("credential-secret", repr(raised.exception))
                self.assertNotIn("mail-body", str(raised.exception))

        for error, code in (
            (TimeoutError("credential-secret mail-body"), "timeout"),
            (ConnectorError("http", "invalid_json", False, "response is not valid JSON"), "invalid_json"),
        ):
            with self.subTest(code=code), self.assertRaises(ConnectorError) as raised:
                GmailClient("credential-secret", FakeTransport(error=error)).list_messages("query")
            self.assertEqual("gmail", raised.exception.provider)
            self.assertEqual(code, raised.exception.code)
            self.assertNotIn("credential-secret", repr(raised.exception))
            formatted = "".join(
                traceback.format_exception(
                    type(raised.exception),
                    raised.exception,
                    raised.exception.__traceback__,
                )
            )
            self.assertNotIn("credential-secret", formatted)
            self.assertNotIn("mail-body", formatted)

    def test_injected_connector_error_message_is_replaced(self):
        transport_error = ConnectorError(
            "http", "network_error", True, "credential-secret mail-body"
        )
        with self.assertRaises(ConnectorError) as raised:
            GmailClient("credential-secret", FakeTransport(error=transport_error)).list_messages(
                "query"
            )
        formatted = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertEqual("Google request failed", raised.exception.message)
        self.assertNotIn("credential-secret", formatted)
        self.assertNotIn("mail-body", formatted)


if __name__ == "__main__":
    unittest.main()
