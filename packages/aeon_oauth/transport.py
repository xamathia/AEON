"""Bounded, non-redirecting transport for Google's OAuth token endpoint."""

from collections.abc import Mapping
import json
import math
import urllib.error
import urllib.parse
import urllib.request

from .errors import OAuthError


_TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
_MAX_RESPONSE_BYTES = 65536


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError()
    return number


def _reject_constant(value):
    raise ValueError()


def _decode_object(raw):
    try:
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError()
        data = json.loads(
            raw.decode('utf-8'), object_pairs_hook=_unique_object,
            parse_float=_finite_float, parse_constant=_reject_constant,
        )
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        # The decoder exception can contain the response document.
        raise OAuthError('invalid_response') from None


def _rejection_code(response, status):
    if 300 <= status < 400:
        return 'transport_error'
    try:
        data = _decode_object(response.read(_MAX_RESPONSE_BYTES + 1))
        if data.get('error') == 'invalid_grant':
            return 'invalid_grant'
    except Exception:
        # Error bodies are optional and untrusted, including failures to read.
        pass
    return 'token_rejected'


class TokenTransport:
    """Exchange form fields without storing credentials on the transport."""

    def __init__(self, timeout=10):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise OAuthError('invalid_configuration') from None
        self._timeout = timeout

    def post_form(self, fields: Mapping[str, str]) -> dict:
        """POST string fields and return an object for caller schema validation."""
        try:
            if not isinstance(fields, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in fields.items()
            ):
                raise ValueError()
            body = urllib.parse.urlencode(fields).encode('ascii')
        except Exception:
            raise OAuthError('invalid_configuration') from None

        try:
            request = urllib.request.Request(
                _TOKEN_ENDPOINT, data=body, method='POST',
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            )
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirects(),
            )
            with opener.open(request, timeout=self._timeout) as response:
                status = response.getcode()
                if not 200 <= status < 300:
                    raise OAuthError(_rejection_code(response, status)) from None
                return _decode_object(response.read(_MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as error:
            code = _rejection_code(error, error.code)
            try:
                error.close()
            except Exception:
                # Cleanup failures must not expose headers, URLs, or bodies.
                pass
            raise OAuthError(code) from None
        except OAuthError:
            raise
        except Exception:
            raise OAuthError('transport_error') from None
