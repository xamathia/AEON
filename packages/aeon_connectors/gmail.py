"""Read-only Gmail message adapter."""

from __future__ import annotations

import base64
import binascii
from typing import Any, Dict, Iterator, Optional
import urllib.parse

from ._google import perform_request, require_secret
from .transport import ConnectorError, HttpTransport


_PROVIDER = "gmail"
_TEXT_LIMIT = 10_000


class GmailClient:
    def __init__(self, access_token: str, transport: Any = None) -> None:
        self._access_token = require_secret(access_token, "access_token")
        self._transport = transport if transport is not None else HttpTransport()

    def list_messages(
        self,
        query: str,
        *,
        max_results: int = 20,
        page_token: Optional[str] = None,
    ) -> dict:
        if not isinstance(query, str):
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, "query must be a string")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 50:
            raise ConnectorError(
                _PROVIDER, "invalid_arguments", False, "max_results must be between 1 and 50"
            )
        if page_token is not None and (not isinstance(page_token, str) or not page_token):
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, "page_token is invalid")
        params = {"q": query, "maxResults": str(max_results)}
        if page_token is not None:
            params["pageToken"] = page_token
        response = self._request(
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages?"
            + urllib.parse.urlencode(params),
        )
        raw_messages = response.body.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ConnectorError(_PROVIDER, "invalid_response", False, "Gmail messages are invalid")
        messages = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                raise ConnectorError(_PROVIDER, "invalid_response", False, "Gmail message is invalid")
            message_id = raw.get("id")
            thread_id = raw.get("threadId")
            if not isinstance(message_id, str) or not message_id or not isinstance(thread_id, str) or not thread_id:
                raise ConnectorError(_PROVIDER, "invalid_response", False, "Gmail message is invalid")
            messages.append({"id": message_id, "thread_id": thread_id})
        token = response.body.get("nextPageToken")
        return {
            "messages": messages,
            "next_page_token": token if isinstance(token, str) and token else None,
        }

    def get_message(self, message_id: str) -> dict:
        if not isinstance(message_id, str) or not message_id:
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, "message_id is required")
        encoded_id = urllib.parse.quote(message_id, safe="")
        response = self._request(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{encoded_id}?format=full",
        )
        body = response.body
        returned_id = body.get("id")
        thread_id = body.get("threadId")
        payload = body.get("payload")
        if (
            not isinstance(returned_id, str)
            or not returned_id
            or not isinstance(thread_id, str)
            or not thread_id
            or not isinstance(payload, dict)
        ):
            raise ConnectorError(_PROVIDER, "invalid_response", False, "Gmail message is invalid")
        headers = _headers(payload.get("headers", []))
        plain_text = "\n".join(part for part in _plain_text_parts(payload) if part)
        truncated = len(plain_text) > _TEXT_LIMIT
        return {
            "id": returned_id,
            "thread_id": thread_id,
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "date": headers.get("date", ""),
            "snippet": body.get("snippet", "") if isinstance(body.get("snippet", ""), str) else "",
            "text": plain_text[:_TEXT_LIMIT],
            "truncated": truncated,
            "source": {
                "provider": _PROVIDER,
                "reference": f"gmail:{returned_id}",
                "synthetic": False,
                "assumption": "untrusted body and headers; no automatic interpretation",
            },
        }

    def _request(self, method: str, url: str):
        return perform_request(
            _PROVIDER,
            self._transport,
            method,
            url,
            {"Authorization": f"Bearer {self._access_token}"},
        )


def _headers(value: Any) -> Dict[str, str]:
    if not isinstance(value, list):
        return {}
    result = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        header_value = item.get("value")
        if isinstance(name, str) and isinstance(header_value, str):
            result.setdefault(name.lower(), header_value)
    return result


def _plain_text_parts(part: Dict[str, Any]) -> Iterator[str]:
    body = part.get("body")
    if part.get("filename") or (
        isinstance(body, dict) and body.get("attachmentId")
    ):
        return
    parts = part.get("parts", [])
    if isinstance(parts, list):
        for child in parts:
            if isinstance(child, dict):
                yield from _plain_text_parts(child)
    if part.get("mimeType") != "text/plain":
        return
    if not isinstance(body, dict):
        return
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return
    try:
        padded = data + "=" * (-len(data) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return
    yield raw.decode("utf-8", errors="replace")
