"""Memory-only, provider-specific Google OAuth for the local desktop application."""
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from .errors import OAuthError
from .transport import TokenTransport

_SCOPES = {
    'google_calendar_write': 'https://www.googleapis.com/auth/calendar.events',
    'google_calendar': 'https://www.googleapis.com/auth/calendar.events.readonly',
    'gmail': 'https://www.googleapis.com/auth/gmail.readonly',
}
_AUTHORIZATION_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
_MAX_SESSIONS = 32
_ATTEMPT_TTL = 300
_REFRESH_MARGIN = 30
_MAX_TOKEN_LIFETIME = 86400
_MAX_TOKEN_LENGTH = 16384
_MAX_RESPONSE_BYTES = 65536


@dataclass(repr=False)
class _Attempt:
    state: str
    verifier: str
    deadline: float


@dataclass(repr=False)
class _Tokens:
    access: str
    refresh: object
    scopes: tuple
    expires_at: float
    deadline: float


@dataclass(repr=False)
class _Session:
    lock: object = field(default_factory=threading.RLock)
    pending: object = None
    tokens: object = None
    generation: int = 0
    refresh_error: object = None


def _text(value, maximum):
    return (type(value) is str and 0 < len(value) <= maximum
            and all(33 <= ord(char) <= 126 for char in value))


def _validate_json(value, depth=0):
    """Apply the JSON boundary to injected transports as well as real HTTP."""
    if depth > 32:
        raise OAuthError('invalid_response')
    kind = type(value)
    if kind is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise OAuthError('invalid_response')
            _validate_json(child, depth + 1)
    elif kind is list:
        for child in value:
            _validate_json(child, depth + 1)
    elif kind is float:
        if not math.isfinite(value):
            raise OAuthError('invalid_response')
    elif kind not in (str, int, bool, type(None)):
        raise OAuthError('invalid_response')


class GoogleOAuth:
    """One provider's independent consent, attempts and credentials.

    The local server must supply an opaque session id, never copy it from the
    OAuth callback query. No browser, callback listener, disk or Google API
    connector is opened here. Call access_token only on the server side.
    """

    def __init__(self, client_id, redirect_uri, *, provider, client_secret=None,
                 transport=None, clock=None):
        if (not _text(client_id, 4096)
                or (client_secret is not None and not _text(client_secret, 4096))
                or type(provider) is not str or provider not in _SCOPES
                or (clock is not None and not callable(clock))):
            raise OAuthError('invalid_configuration')
        try:
            if not _text(redirect_uri, 2048) or '?' in redirect_uri or '#' in redirect_uri:
                raise ValueError()
            parsed = urlsplit(redirect_uri)
            if (parsed.scheme != 'http'
                    or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', parsed.netloc)
                    or parsed.port is None or not 1 <= parsed.port <= 65535
                    or parsed.path not in ('', '/')):
                raise ValueError()
        except Exception:
            raise OAuthError('invalid_configuration') from None
        try:
            if transport is not None and not callable(getattr(transport, 'post_form', None)):
                raise ValueError()
        except Exception:
            raise OAuthError('invalid_configuration') from None
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._provider = provider
        self._scope = _SCOPES[provider]
        self._transport = transport if transport is not None else TokenTransport()
        self._clock = clock if clock is not None else time.time
        # Lifetimes follow a monotonic clock; expires_at remains a Unix timestamp.
        self._lifetime_clock = clock if clock is not None else time.monotonic
        self._sessions = {}
        self._registry_lock = threading.RLock()

    def __repr__(self):
        return '<GoogleOAuth provider={!r}>'.format(self._provider)

    @staticmethod
    def _time(clock):
        try:
            value = clock()
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError()
            return value
        except Exception:
            raise OAuthError('invalid_configuration') from None

    @staticmethod
    def _session_id(session_id):
        if not _text(session_id, 256):
            raise OAuthError('invalid_session')

    def _lookup(self, session_id, create=False):
        self._session_id(session_id)
        with self._registry_lock:
            entry = self._sessions.get(session_id)
            if entry is None and create:
                if len(self._sessions) >= _MAX_SESSIONS:
                    raise OAuthError('session_limit')
                entry = _Session()
                self._sessions[session_id] = entry
            return entry

    def _registered(self, session_id, entry):
        with self._registry_lock:
            return self._sessions.get(session_id) is entry

    def begin(self, session_id):
        """Create a five-minute, one-use attempt, replacing local prior access."""
        while True:
            entry = self._lookup(session_id, create=True)
            with entry.lock:
                # forget may have removed this entry while we waited for its lock.
                if not self._registered(session_id, entry):
                    continue
                now = self._time(self._lifetime_clock)
                verifier = secrets.token_urlsafe(64)
                state = secrets.token_urlsafe(32)
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode('ascii')).digest()
                ).rstrip(b'=').decode('ascii')
                entry.pending = _Attempt(state, verifier, now + _ATTEMPT_TTL)
                entry.tokens = None
                entry.refresh_error = None
                entry.generation += 1
                params = {'client_id': self._client_id, 'redirect_uri': self._redirect_uri,
                          'response_type': 'code', 'scope': self._scope, 'state': state,
                          'code_challenge': challenge, 'code_challenge_method': 'S256'}
                return {'authorization_url': _AUTHORIZATION_ENDPOINT + '?' + urlencode(params),
                        'expires_in': _ATTEMPT_TTL}

    def complete(self, session_id, *, state, code=None, error=None):
        """Consume a matching attempt once, and return only connection metadata."""
        entry = self._lookup(session_id)
        if entry is None:
            raise OAuthError('invalid_state')
        with entry.lock:
            attempt = entry.pending
            if (not self._registered(session_id, entry) or attempt is None
                    or not _text(state, 256)
                    or not hmac.compare_digest(attempt.state, state)):
                raise OAuthError('invalid_state')
            entry.pending = None
            if self._time(self._lifetime_clock) >= attempt.deadline:
                raise OAuthError('expired_state')
            if error is not None:
                if code is not None:
                    raise OAuthError('invalid_callback')
                raise OAuthError('authorization_denied')
            if not _text(code, 16384):
                raise OAuthError('invalid_callback')
            fields = self._credentials()
            fields.update({'code': code, 'code_verifier': attempt.verifier,
                           'redirect_uri': self._redirect_uri, 'grant_type': 'authorization_code'})
            entry.tokens = self._exchange(fields)
            entry.refresh_error = None
            entry.generation += 1
            return self._status(entry)

    def _credentials(self):
        fields = {'client_id': self._client_id}
        if self._client_secret is not None:
            fields['client_secret'] = self._client_secret
        return fields

    def _exchange(self, fields, previous_refresh=None):
        try:
            response = self._transport.post_form(fields)
        except OAuthError as error:
            raise OAuthError(error.code) from None
        except Exception:
            raise OAuthError('transport_error') from None
        try:
            if type(response) is not dict:
                raise OAuthError('invalid_response')
            _validate_json(response)
            if len(json.dumps(response, allow_nan=False, ensure_ascii=False).encode('utf-8')) > _MAX_RESPONSE_BYTES:
                raise OAuthError('invalid_response')
            if 'error' in response:
                raise OAuthError('invalid_grant' if response['error'] == 'invalid_grant' else 'token_rejected')
            access = response.get('access_token')
            kind = response.get('token_type')
            expires = response.get('expires_in')
            refresh = response.get('refresh_token', previous_refresh)
            if (not _text(access, _MAX_TOKEN_LENGTH)
                    or type(kind) is not str or kind.lower() != 'bearer'
                    or type(expires) is not int or not 1 <= expires <= _MAX_TOKEN_LIFETIME
                    or ('refresh_token' in response and not _text(refresh, _MAX_TOKEN_LENGTH))):
                raise OAuthError('invalid_response')
            scope = response.get('scope', self._scope)
            if (type(scope) is not str
                    or any(ord(char) < 32 or ord(char) > 126 for char in scope)):
                raise OAuthError('invalid_response')
            scopes = tuple(sorted(set(scope.split(' ')) - {''}))
            if self._scope not in scopes:
                raise OAuthError('insufficient_scope')
            expires_at = self._time(self._clock) + expires
            deadline = self._time(self._lifetime_clock) + expires
            if not math.isfinite(expires_at) or not math.isfinite(deadline):
                raise OAuthError('invalid_response')
            return _Tokens(access, refresh, scopes, expires_at, deadline)
        except OAuthError as error:
            raise OAuthError(error.code) from None
        except Exception:
            raise OAuthError('invalid_response') from None

    @staticmethod
    def _disconnected():
        return {'connected': False, 'scopes': [], 'expires_at': None}

    def _status(self, entry):
        tokens = entry.tokens
        if (tokens is None or (self._time(self._lifetime_clock) >= tokens.deadline
                               and tokens.refresh is None)):
            return self._disconnected()
        return {'connected': True, 'scopes': list(tokens.scopes), 'expires_at': tokens.expires_at}

    def status(self, session_id):
        """Read local metadata; no token or network call is returned or performed."""
        entry = self._lookup(session_id)
        if entry is None:
            return self._disconnected()
        with entry.lock:
            if not self._registered(session_id, entry):
                return self._disconnected()
            return self._status(entry)

    def access_token(self, session_id):
        """Internal connector access. Renew once before expiry when possible."""
        entry = self._lookup(session_id)
        if entry is None:
            raise OAuthError('not_connected')
        observed_generation = entry.generation
        with entry.lock:
            tokens = entry.tokens
            if not self._registered(session_id, entry) or tokens is None:
                raise OAuthError('not_connected')
            if entry.generation != observed_generation and entry.refresh_error is not None:
                # Existing waiters share this failed attempt; a later caller sees
                # the current generation and may retry instead of caching failure.
                raise OAuthError(entry.refresh_error) from None
            now = self._time(self._lifetime_clock)
            if now < tokens.deadline and (tokens.refresh is None
                    or now < tokens.deadline - _REFRESH_MARGIN
                    or entry.generation != observed_generation):
                return tokens.access
            if tokens.refresh is None:
                entry.tokens = None
                raise OAuthError('not_connected')
            fields = self._credentials()
            fields.update({'grant_type': 'refresh_token', 'refresh_token': tokens.refresh})
            try:
                updated = self._exchange(fields, previous_refresh=tokens.refresh)
            except OAuthError as error:
                if error.code in ('invalid_grant', 'insufficient_scope'):
                    entry.tokens = None
                    entry.pending = None
                entry.refresh_error = error.code
                entry.generation += 1
                raise OAuthError(error.code) from None
            entry.tokens = updated
            entry.refresh_error = None
            entry.generation += 1
            return updated.access

    def forget(self, session_id):
        """Erase local memory only; this does not revoke consent at Google."""
        entry = self._lookup(session_id)
        if entry is None:
            return
        with entry.lock:
            with self._registry_lock:
                if self._sessions.get(session_id) is entry:
                    entry.pending = None
                    entry.tokens = None
                    del self._sessions[session_id]
