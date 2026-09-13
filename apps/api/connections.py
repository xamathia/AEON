"""Local session and read-only Google integration; no live simulation or writes."""
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import math
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from packages.aeon_connectors import GoogleCalendarClient, GmailClient
from packages.aeon_oauth import GoogleOAuth, OAuthError
from .configuration import Configuration

PROVIDERS = {'google_calendar': 'Google Calendar', 'gmail': 'Gmail'}
SESSION_TTL = 3600
MAX_SESSIONS = 32
_ERRORS = {
    'session_required': (401, 'Start a session from the AEON app.'),
    'session_invalid': (401, 'This local session is unknown. Reload your session.'),
    'session_expired': (401, 'Your local session has expired. Reconnect your sources.'),
    'session_limit': (503, 'The local session limit has been reached. Try again after a session expires.'),
    'csrf_invalid': (403, 'Restart this action from your AEON session.'),
    'oauth_unavailable': (503, 'Google sign-in is not configured correctly on this local server.'),
    'invalid_request': (400, 'The request parameters are invalid.'),
    'not_connected': (401, 'Connect this Google source before requesting data.'),
    'read_busy': (409, 'This source is already being read.'),
    'operation_cancelled': (409, 'This operation was cancelled because the connection changed.'),
    'read_failed': (502, 'The Google request failed. The last available results have been kept.'),
    'oauth_failed': (502, 'Google sign-in could not be completed. Try connecting again.'),
    'revision_conflict': (409, 'The imported calendar has changed. Refresh the live forecast.'),
    'live_busy': (409, 'An operation or source read is already in progress for this calendar.'),
    'routes_unavailable': (503, 'Google Routes is not configured on this server.'),
    'routes_budget_exhausted': (429, 'Route request budget reached. Try again later.'),
    'routes_failed': (502, 'The route request failed. No new route has been saved.'),
    'departure_in_past': (400, 'The planned departure is in the past. Choose a future period when reading Calendar.'),
    'live_unavailable': (503, 'Live computation is unavailable. No forecast has been created.'),
    'intelligence_unavailable': (503, 'Claude is not configured or available on this server.'),
    'intelligence_budget_exhausted': (429, 'This session has used all ten AI calls.'),
    'gmail_not_read': (409, 'Read and select a Gmail excerpt before running this analysis.'),
    'planning_unavailable': (503, 'Live alternative planning is unavailable.'),
}


class APIError(Exception):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'oauth_failed'
        self.status, message = _ERRORS[self.code]
        super().__init__(message)


def never_read():
    return {'status': 'never', 'last_read_at': None, 'count': 0, 'complete': None, 'error': None}


@dataclass(repr=False)
class _Source:
    operation_lock: object = field(default_factory=threading.RLock)
    generation: int = 0
    pending_state: object = None
    reading: bool = False
    read: dict = field(default_factory=never_read)
    data: object = None
    canonical_data: object = None


@dataclass(repr=False)
class _Session:
    id: str
    csrf: str
    expires_at: float
    deadline: float
    active: bool = True
    lock: object = field(default_factory=threading.RLock)
    sources: dict = field(default_factory=lambda: {key: _Source() for key in PROVIDERS})
    oauth_result: object = None
    live_state: object = None
    live_busy: bool = False
    route_attempts: int = 0
    open_routing_client: object = None
    intelligence_state: object = None
    intelligence_meter: dict = field(default_factory=lambda: {'calls': 0, 'input_tokens': 0, 'output_tokens': 0})


class ConnectionService:
    """The server supplies trusted cookies; callers cannot choose OAuth sessions."""
    def __init__(self, port, *, configuration=None, oauth_factory=GoogleOAuth,
                 calendar_factory=GoogleCalendarClient, gmail_factory=GmailClient, clock=None):
        self.origin = 'http://127.0.0.1:' + str(port)
        self._clock = clock if clock is not None else time.time
        self._lifetime = clock if clock is not None else time.monotonic
        self._lock = threading.RLock()
        self._sessions = {}
        self._oauth = {}
        self.action_discard = None
        self._configuration = {}
        self._calendar_factory = calendar_factory
        self._gmail_factory = gmail_factory
        config = configuration if configuration is not None else Configuration({})
        values = config.values
        self._routes_key = values.get('AEON_GOOGLE_ROUTES_API_KEY') if config.valid else None
        self.routes_configured = bool(config.valid and values.get('AEON_GOOGLE_ROUTES_API_KEY'))
        client_id = values.get('AEON_GOOGLE_CLIENT_ID', '')
        redirect = values.get('AEON_GOOGLE_REDIRECT_URI', '')
        secret = values.get('AEON_GOOGLE_CLIENT_SECRET') or None
        for provider in PROVIDERS:
            state = 'invalid' if not config.valid else 'missing'
            if config.valid and client_id and redirect:
                try:
                    if redirect not in (self.origin, self.origin + '/'):
                        raise ValueError()
                    self._oauth[provider] = oauth_factory(client_id, redirect, provider=provider, client_secret=secret)
                    state = 'ready'
                except Exception:
                    state = 'invalid'
            self._configuration[provider] = state

    def _now(self, lifetime=False):
        value = (self._lifetime if lifetime else self._clock)()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise APIError('oauth_failed')
        return value

    def _ensure_active(self, session):
        if not session.active or self._now(lifetime=True) >= session.deadline:
            raise APIError('session_expired')

    def _discard(self, session):
        with session.lock:
            session.active = False
            session.oauth_result = None
            session.live_state = None
            session.intelligence_state = None
            session.open_routing_client = None
            for source in session.sources.values():
                source.generation += 1
                source.pending_state = None
                source.data = None
                source.canonical_data = None
                source.read = never_read()
        if self.action_discard is not None:
            self.action_discard(session)
        for oauth in self._oauth.values():
            try:
                oauth.forget(session.id)
            except Exception:
                pass

    def session(self, identifier=None, *, create=False):
        now = self._now(lifetime=True)
        with self._lock:
            expired = [entry for entry in self._sessions.values() if entry.deadline <= now]
            expired_ids = {entry.id for entry in expired}
            for entry in expired:
                entry.active = False
                del self._sessions[entry.id]
        # No registry lock is held while OAuth may wait for its per-session I/O.
        for entry in expired:
            self._discard(entry)
        if identifier in expired_ids:
            raise APIError('session_expired')
        with self._lock:
            if identifier is not None:
                if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', identifier):
                    raise APIError('session_invalid')
                entry = self._sessions.get(identifier)
                if entry is None:
                    raise APIError('session_invalid')
                return entry, False
            if not create:
                raise APIError('session_required')
            if len(self._sessions) >= MAX_SESSIONS:
                raise APIError('session_limit')
            entry = _Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                             self._now() + SESSION_TTL, now + SESSION_TTL)
            self._sessions[entry.id] = entry
            return entry, True

    def session_info(self, session):
        self._ensure_active(session)
        return {'csrf_token': session.csrf,
                'expires_in': max(0, math.ceil(session.deadline - self._now(lifetime=True))),
                'canonical_origin': self.origin}

    def verify_csrf(self, session, token):
        self._ensure_active(session)
        if type(token) is not str or len(token) != len(session.csrf) or not token.isascii() or not hmac.compare_digest(token, session.csrf):
            raise APIError('csrf_invalid')

    def _oauth_for(self, provider):
        if provider not in PROVIDERS or provider not in self._oauth:
            raise APIError('oauth_unavailable')
        return self._oauth[provider]

    @staticmethod
    def _reset_source(source):
        source.generation += 1
        source.pending_state = None
        source.data = None
        source.canonical_data = None
        source.read = never_read()
        source.reading = False

    @staticmethod
    def invalidate_calendar_after_action(session):
        # A possible write makes both projections stale, even after an uncertain result.
        # Keep OAuth consent and action receipts so conditional undo remains available.
        with session.lock:
            source = session.sources['google_calendar']
            source.generation += 1
            source.data = None
            source.canonical_data = None
            source.read = never_read()
            source.reading = False
            session.live_state = None
            session.intelligence_state = None
            session.open_routing_client = None

    @staticmethod
    def _invalidate_observation(session, provider):
        session.intelligence_state = None
        session.open_routing_client = None
        if provider == 'google_calendar':
            session.live_state = None
        elif session.live_state is not None:
            # Keep explicitly obtained routes, but invalidate every Gmail-dependent result.
            session.live_state = {**session.live_state, 'revision': secrets.token_urlsafe(32),
                                  'constraints': [], 'result': None, 'planning': None}

    def begin(self, session, provider):
        oauth = self._oauth_for(provider)
        source = session.sources[provider]
        with source.operation_lock:
            with session.lock:
                self._ensure_active(session)
                self._reset_source(source)
                self._invalidate_observation(session, provider)
                session.oauth_result = None
                generation = source.generation
            if provider == 'google_calendar' and self.action_discard is not None:
                self.action_discard(session)
            try:
                result = oauth.begin(session.id)
                state = parse_qs(urlsplit(result['authorization_url']).query)['state'][0]
            except Exception:
                raise APIError('oauth_failed') from None
            with session.lock:
                self._ensure_active(session)
                if source.generation != generation:
                    raise APIError('operation_cancelled')
                source.pending_state = state
                return result

    def callback_error(self, session, code='invalid_callback', provider=None):
        error = OAuthError(code)
        with session.lock:
            self._ensure_active(session)
            session.oauth_result = {'provider': provider, 'status': 'error', 'code': error.code, 'message': str(error)}

    def callback(self, session, params):
        state = params.get('state')
        if type(state) is not str or len(state) > 256 or not state.isascii():
            self.callback_error(session, 'invalid_state')
            return
        with session.lock:
            self._ensure_active(session)
            provider = next((name for name, source in session.sources.items()
                             if source.pending_state is not None and hmac.compare_digest(source.pending_state, state)), None)
        if provider is None:
            self.callback_error(session, 'invalid_state')
            return
        source = session.sources[provider]
        with source.operation_lock:
            with session.lock:
                self._ensure_active(session)
                if source.pending_state != state:
                    self.callback_error(session, 'invalid_state', provider)
                    return
                source.pending_state = None
                generation = source.generation
            try:
                self._oauth_for(provider).complete(session.id, state=state, code=params.get('code'), error=params.get('error'))
            except OAuthError as error:
                self.callback_error(session, error.code, provider)
                return
            except Exception:
                self.callback_error(session, 'oauth_error', provider)
                return
            with session.lock:
                self._ensure_active(session)
                if source.generation != generation:
                    raise APIError('operation_cancelled')
                session.oauth_result = {'provider': provider, 'status': 'connected', 'code': 'connected',
                                        'message': 'This Google source is authorised. No data read has been requested yet.'}

    def forget(self, session, provider):
        if provider not in PROVIDERS:
            raise APIError('oauth_unavailable')
        source = session.sources[provider]
        with source.operation_lock:
            with session.lock:
                self._ensure_active(session)
                self._reset_source(source)
                self._invalidate_observation(session, provider)
                if session.oauth_result and session.oauth_result['provider'] == provider:
                    session.oauth_result = None
            if provider == 'google_calendar' and self.action_discard is not None:
                self.action_discard(session)
            oauth = self._oauth.get(provider)
            if oauth is not None:
                try:
                    oauth.forget(session.id)
                except Exception:
                    raise APIError('oauth_failed') from None
        return {'forgotten': True, 'provider': provider}

    def describe(self, session):
        self._ensure_active(session)
        providers = []
        for provider, name in PROVIDERS.items():
            source = session.sources[provider]
            # Serializes metadata against reconnect/forget, but never global I/O.
            with source.operation_lock:
                oauth = self._oauth.get(provider)
                metadata = oauth.status(session.id) if oauth else {'connected': False, 'scopes': [], 'expires_at': None}
                with session.lock:
                    self._ensure_active(session)
                    providers.append({'id': provider, 'name': name, 'configuration': self._configuration[provider],
                                      'authorized': bool(metadata['connected']), 'scopes': list(metadata['scopes']),
                                      'expires_at': metadata['expires_at'], 'read': copy.deepcopy(source.read),
                                      'data': copy.deepcopy(source.data)})
        with session.lock:
            result = copy.deepcopy(session.oauth_result)
        return {'canonical_origin': self.origin, 'providers': providers,
                'routes': {'configured': self.routes_configured, 'read_status': 'not_requested'}, 'oauth_result': result}

    @staticmethod
    def _calendar_input(body):
        try:
            if set(body) - {'time_min', 'time_max', 'timezone'}:
                raise ValueError()
            values = []
            for key in ('time_min', 'time_max'):
                raw = body[key]
                if type(raw) is not str or len(raw) > 64 or 'T' not in raw:
                    raise ValueError()
                instant = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if instant.tzinfo is None or instant.utcoffset() is None:
                    raise ValueError()
                values.append(instant.astimezone(timezone.utc))
            span = (values[1] - values[0]).total_seconds()
            if not 0 < span <= 7 * 86400:
                raise ValueError()
            zone = body.get('timezone', 'Europe/Paris')
            if type(zone) is not str or len(zone) > 100:
                raise ValueError()
            ZoneInfo(zone)
            return {'time_min': body['time_min'], 'time_max': body['time_max'], 'timezone': zone}
        except Exception:
            raise APIError('invalid_request') from None

    @staticmethod
    def _gmail_input(body):
        if set(body) != {'query'} or type(body['query']) is not str or not body['query'].strip() or len(body['query']) > 500 or any(ord(char) < 32 for char in body['query']):
            raise APIError('invalid_request')
        return body['query']

    def read(self, session, provider, body):
        arguments = self._calendar_input(body) if provider == 'google_calendar' else self._gmail_input(body)
        oauth = self._oauth_for(provider)
        source = session.sources[provider]
        with session.lock:
            self._ensure_active(session)
            if source.reading:
                raise APIError('read_busy')
            source.reading = True
            generation = source.generation
        def guard():
            with session.lock:
                self._ensure_active(session)
                if source.generation != generation:
                    raise APIError('operation_cancelled')
        try:
            try:
                token = oauth.access_token(session.id)
            except OAuthError as error:
                if error.code in ('not_connected', 'invalid_grant', 'insufficient_scope'):
                    raise APIError('not_connected') from None
                raise APIError('read_failed') from None
            guard()
            if provider == 'google_calendar':
                data, canonical = self._read_calendar(token, arguments, guard)
            else:
                data = self._read_gmail(token, arguments, guard)
            with session.lock:
                self._ensure_active(session)
                if source.generation != generation:
                    raise APIError('operation_cancelled')
                source.data = data
                if provider == 'google_calendar':
                    source.canonical_data = {'events': canonical, 'complete': data['complete'], 'read_at': data['read_at']}
                self._invalidate_observation(session, provider)
                source.read = {'status': 'succeeded' if data['complete'] else 'incomplete', 'last_read_at': data['read_at'],
                               'count': len(data['events'] if provider == 'google_calendar' else data['messages']),
                               'complete': data['complete'], 'error': None}
                return {'provider': provider, 'data': copy.deepcopy(data)}
        except Exception as error:
            public = error if isinstance(error, APIError) else APIError('read_failed')
            with session.lock:
                if session.active and source.generation == generation and public.code not in ('operation_cancelled', 'session_expired'):
                    source.read = {**source.read, 'status': 'failed', 'error': {'code': public.code, 'message': str(public)}}
            raise APIError(public.code) from None
        finally:
            with session.lock:
                if source.generation == generation:
                    source.reading = False

    def _read_calendar(self, token, arguments, guard):
        client = self._calendar_factory(token)
        events = {}
        canonical = {}
        page_token = None
        seen_pages = set()
        excluded = deleted = 0
        complete = True
        for page in range(1, 4):
            guard()
            result = client.list_events('primary', **arguments, page_token=page_token, authorized_flexible_ids=())
            if type(result) is not dict or any(type(result.get(key)) is not list for key in ('events', 'excluded_events', 'deleted_event_ids')):
                raise APIError('read_failed')
            excluded += len(result['excluded_events'])
            deleted += len(result['deleted_event_ids'])
            for event in result['events']:
                if type(event) is not dict or any(type(event.get(key)) is not str for key in ('id', 'title', 'planned_start', 'planned_end')):
                    raise APIError('read_failed')
                if len(events) >= 500 and event['id'] not in events:
                    complete = False
                    continue
                if len(event['title']) > 500:
                    complete = False
                identifier = event['id']
                if not identifier or len(identifier) > 512 or len(event['planned_start']) > 64 or len(event['planned_end']) > 64:
                    raise APIError('read_failed')
                events[identifier] = {'id': identifier, 'title': event['title'][:500],
                                      'planned_start': event['planned_start'], 'planned_end': event['planned_end'],
                                      'classification': 'fixed', 'private': event.get('private') is True,
                                      'source': {'provider': 'google_calendar', 'reference': 'google_calendar:primary:' + identifier,
                                                 'synthetic': False, 'assumption': 'Imported event; this read does not authorise flexibility or establish overrun estimates.'}}
                canonical[identifier] = copy.deepcopy(event)
            next_page = result.get('next_page_token')
            if next_page is None:
                break
            if type(next_page) is not str or not next_page or len(next_page) > 4096:
                raise APIError('read_failed')
            if next_page in seen_pages or page == 3:
                complete = False
                break
            seen_pages.add(next_page)
            page_token = next_page
        return {'synthetic': False, 'read_at': self._now(), 'complete': complete and excluded == 0,
                'pages_read': page, 'horizon': {'start': arguments['time_min'], 'end': arguments['time_max']},
                'timezone': arguments['timezone'], 'events': list(events.values()),
                'excluded_count': excluded, 'deleted_count': deleted}, list(canonical.values())

    def _read_gmail(self, token, query, guard):
        client = self._gmail_factory(token)
        guard()
        listed = client.list_messages(query, max_results=10)
        if type(listed) is not dict or type(listed.get('messages')) is not list:
            raise APIError('read_failed')
        complete = not listed.get('next_page_token') and len(listed['messages']) <= 10
        messages = []
        seen = set()
        for listed_message in listed['messages'][:10]:
            identifier = listed_message.get('id') if type(listed_message) is dict else None
            if type(identifier) is not str or not identifier or len(identifier) > 512:
                raise APIError('read_failed')
            if identifier in seen:
                complete = False
                continue
            seen.add(identifier)
            guard()
            message = client.get_message(identifier)
            if type(message) is not dict or message.get('id') != identifier:
                raise APIError('read_failed')
            for key in ('subject', 'from', 'date', 'text', 'snippet'):
                if type(message.get(key, '')) is not str:
                    raise APIError('read_failed')
            text = message.get('text') or message.get('snippet', '')
            truncated = bool(message.get('truncated')) or len(text) > 1000
            if truncated or any(len(message.get(key, '')) > 500 for key in ('subject', 'from', 'date')):
                complete = False
            messages.append({'id': identifier, 'subject': message.get('subject', '')[:500],
                             'from': message.get('from', '')[:500], 'date': message.get('date', '')[:500],
                             'excerpt': text[:1000], 'truncated': truncated,
                             'source': {'provider': 'gmail', 'reference': 'gmail:' + identifier, 'synthetic': False,
                                        'assumption': 'Untrusted text; no automatic interpretation.'}})
        return {'synthetic': False, 'read_at': self._now(), 'complete': bool(complete),
                'pages_read': 1, 'query': query, 'messages': messages}
