"""Shared validation and error translation for Google REST clients."""

from __future__ import annotations

import socket
from typing import Any
import urllib.error

from .transport import ConnectorError, HttpResponse


_TRANSPORT_MESSAGES = {
    "invalid_json": "Google response was not valid JSON",
    "network_error": "Google request failed",
    "response_too_large": "Google response was too large",
    "timeout": "Google request timed out",
    "unsafe_redirect": "Google response redirect was rejected",
}


def require_secret(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def perform_request(provider: str, transport: Any, *args: Any, **kwargs: Any) -> HttpResponse:
    try:
        response = transport.request(*args, **kwargs)
    except ConnectorError as exc:
        message = _TRANSPORT_MESSAGES.get(exc.code, "Google transport failed")
        raise ConnectorError(provider, exc.code, exc.retryable, message) from None
    except (TimeoutError, socket.timeout):
        raise ConnectorError(provider, "timeout", True, "Google request timed out") from None
    except urllib.error.URLError as exc:
        retryable = isinstance(exc.reason, (TimeoutError, socket.timeout))
        code = "timeout" if retryable else "network_error"
        message = "Google request timed out" if retryable else "Google request failed"
        raise ConnectorError(provider, code, retryable, message) from None
    except OSError:
        raise ConnectorError(provider, "network_error", True, "Google request failed") from None

    status = getattr(response, "status", None)
    body = getattr(response, "body", None)
    headers = getattr(response, "headers", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise ConnectorError(provider, "invalid_response", False, "invalid HTTP response")
    if not isinstance(body, dict) or not isinstance(headers, dict):
        raise ConnectorError(provider, "invalid_response", False, "invalid JSON response")
    normalized = HttpResponse(status=status, headers=dict(headers), body=body)
    if 200 <= status < 300:
        return normalized
    raise_for_status(provider, status)


def raise_for_status(provider: str, status: int) -> None:
    if provider == "google_calendar" and status == 410:
        code, retryable, message = (
            "sync_expired",
            False,
            "Calendar sync token expired; a full synchronization is required",
        )
    elif status == 401:
        code, retryable, message = "unauthorized", False, "Google credentials were rejected"
    elif status == 403:
        code, retryable, message = "forbidden", False, "Google access was forbidden"
    elif status == 408:
        code, retryable, message = "timeout", True, "Google request timed out"
    elif status == 429:
        code, retryable, message = "rate_limited", True, "Google rate limit reached"
    elif 500 <= status <= 599:
        code, retryable, message = (
            "provider_unavailable",
            True,
            "Google service is temporarily unavailable",
        )
    elif status == 400:
        code, retryable, message = "bad_request", False, "Google rejected the request"
    else:
        code, retryable, message = "http_error", False, "Google request failed"
    raise ConnectorError(provider, code, retryable, message)
