"""Small, injectable HTTPS transport used by the Google connectors."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import socket
from typing import Dict, Mapping, Optional
import urllib.error
import urllib.parse
import urllib.request


_CREDENTIAL_HEADERS = frozenset({"authorization", "x-goog-api-key"})
_CREDENTIAL_QUERY_NAMES = frozenset({"access_token", "api_key", "key", "oauth_token"})


class ConnectorError(Exception):
    """A stable, deliberately redacted connector failure."""

    def __init__(
        self,
        provider: str,
        code: str,
        retryable: bool,
        message: str,
    ) -> None:
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.provider}:{self.code}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"ConnectorError(provider={self.provider!r}, code={self.code!r}, "
            f"retryable={self.retryable!r}, message={self.message!r})"
        )


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Dict[str, str]
    body: dict


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Forbid credential-bearing redirects to a different authority."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        has_credentials = any(
            name.lower() in _CREDENTIAL_HEADERS
            for name in dict(req.header_items())
        )
        if has_credentials and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise ConnectorError(
                "http",
                "unsafe_redirect",
                False,
                "credential-bearing redirect changed HTTPS host",
            )
        if new.scheme.lower() != "https":
            raise ConnectorError(
                "http",
                "unsafe_redirect",
                False,
                "redirect target must use HTTPS",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpTransport:
    """Perform one JSON request through urllib with conservative boundaries."""

    def __init__(self, *, timeout: float = 10.0, max_response_bytes: int = 1_000_000):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be finite and between 0 and 60 seconds")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
            or max_response_bytes > 10_000_000
        ):
            raise ValueError("max_response_bytes must be between 1 and 10000000")
        self._timeout = float(timeout)
        self._max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Optional[dict] = None,
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ConnectorError("http", "invalid_url", False, "URL must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ConnectorError(
                "http", "invalid_url", False, "URL must not contain credentials"
            )
        query_names = {name.lower() for name, _ in urllib.parse.parse_qsl(parsed.query)}
        if parsed.fragment or query_names & _CREDENTIAL_QUERY_NAMES:
            raise ConnectorError(
                "http", "invalid_url", False, "URL must not contain credentials"
            )

        data = None
        request_headers = dict(headers)
        if json_body is not None:
            try:
                data = json.dumps(
                    json_body,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise ConnectorError(
                    "http", "invalid_request", False, "request body is not valid JSON"
                ) from None
            request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("Accept", "application/json")
        request = urllib.request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            response = self._opener.open(request, timeout=self._timeout)
        except ConnectorError:
            raise
        except urllib.error.HTTPError as exc:
            response = exc
        except (TimeoutError, socket.timeout):
            raise ConnectorError(
                "http", "timeout", True, "HTTPS request timed out"
            ) from None
        except urllib.error.URLError as exc:
            retryable = isinstance(exc.reason, (TimeoutError, socket.timeout))
            code = "timeout" if retryable else "network_error"
            message = "HTTPS request timed out" if retryable else "HTTPS request failed"
            raise ConnectorError("http", code, retryable, message) from None
        except OSError:
            raise ConnectorError(
                "http", "network_error", True, "HTTPS request failed"
            ) from None

        try:
            return self._decode_response(response)
        except ConnectorError:
            raise
        except (TimeoutError, socket.timeout):
            raise ConnectorError(
                "http", "timeout", True, "HTTPS response timed out"
            ) from None
        except OSError:
            raise ConnectorError(
                "http", "network_error", True, "HTTPS response could not be read"
            ) from None

    def _decode_response(self, response) -> HttpResponse:
        try:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > self._max_response_bytes:
                        raise ConnectorError(
                            "http", "response_too_large", False, "JSON response is too large"
                        )
                except ValueError:
                    pass

            raw = response.read(self._max_response_bytes + 1)
            if len(raw) > self._max_response_bytes:
                raise ConnectorError(
                    "http", "response_too_large", False, "JSON response is too large"
                )
            try:
                body = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
            except (UnicodeDecodeError, ValueError):
                raise ConnectorError(
                    "http", "invalid_json", False, "response is not valid UTF-8 JSON"
                ) from None
            if not isinstance(body, dict):
                raise ConnectorError(
                    "http", "invalid_json", False, "JSON response must be an object"
                )
            return HttpResponse(
                status=int(response.status),
                headers={str(key): str(value) for key, value in response.headers.items()},
                body=body,
            )
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")
