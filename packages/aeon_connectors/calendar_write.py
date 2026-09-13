"""Conditional writes for explicitly authorized personal Calendar blocks."""
from datetime import datetime, timezone
import http.client
import json
import math
from urllib.parse import quote, urlsplit

from packages.aeon_actions import CalendarConflict, CalendarUnknownOutcome

MAX_BYTES = 1024 * 1024
BASE = 'https://www.googleapis.com/calendar/v3/calendars/primary/events/'


class CalendarWriteError(Exception):
    def __init__(self):
        super().__init__('Calendar operation refused or unavailable.')


class _HTTPTransport:
    def request(self, method, url, *, headers, body, timeout, max_bytes):
        parsed = urlsplit(url)
        if method not in ('GET', 'PATCH') or parsed.scheme != 'https' or parsed.netloc != 'www.googleapis.com' or not url.startswith(BASE) or parsed.fragment:
            raise CalendarWriteError()
        connection = http.client.HTTPSConnection('www.googleapis.com', timeout=timeout)
        try:
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                return response.status, b''
            return response.status, response.read(max_bytes + 1)
        finally:
            connection.close()


def _text(value, maximum=1024):
    if type(value) is not str or not value.strip() or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CalendarWriteError()
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise CalendarWriteError() from None
    return value


def _instant(value):
    _text(value, 64)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except Exception:
        raise CalendarWriteError() from None


def _times(start, end):
    before, after = _instant(start), _instant(end)
    if after <= before:
        raise CalendarWriteError()
    return before, after


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(value):
    raise ValueError()


class GoogleCalendarWriter:
    def __init__(self, access_token, *, authorized_event_ids, transport=None, timeout=15, before_mutation=None):
        _text(access_token, 8192)
        if any(ord(char) < 33 or ord(char) > 126 for char in access_token):
            raise CalendarWriteError()
        if type(authorized_event_ids) not in (list, tuple, set, frozenset) or not 1 <= len(authorized_event_ids) <= 5:
            raise CalendarWriteError()
        identifiers = [_text(value) for value in authorized_event_ids]
        if len(set(identifiers)) != len(identifiers) or any(value in ('.', '..') for value in identifiers):
            raise CalendarWriteError()
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 15:
            raise CalendarWriteError()
        if transport is not None and not callable(getattr(transport, 'request', None)):
            raise CalendarWriteError()
        if before_mutation is not None and not callable(before_mutation):
            raise CalendarWriteError()
        self._before_mutation = before_mutation
        self._token = access_token
        self._authorized = frozenset(identifiers)
        self._timeout = timeout
        self._transport = _HTTPTransport() if transport is None else transport

    def __repr__(self):
        return 'GoogleCalendarWriter()'

    def _url(self, calendar_id, event_id):
        if calendar_id != 'primary' or type(calendar_id) is not str:
            raise CalendarWriteError()
        _text(event_id)
        if event_id not in self._authorized:
            raise CalendarWriteError()
        return BASE + quote(event_id, safe='')

    def _request(self, method, url, body=None, etag=None):
        headers = {'Authorization': 'Bearer ' + self._token}
        if method == 'PATCH':
            headers.update({'Content-Type': 'application/json', 'If-Match': etag})
        return self._transport.request(method, url, headers=headers, body=body,
                                       timeout=self._timeout, max_bytes=MAX_BYTES)

    @staticmethod
    def _snapshot(raw, event_id):
        if type(raw) is not bytes or len(raw) > MAX_BYTES:
            raise CalendarWriteError()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
        if type(value) is not dict or value.get('id') != event_id or value.get('status') != 'confirmed':
            raise CalendarWriteError()
        if any(key in value for key in ('recurrence', 'recurringEventId', 'originalStartTime')) or value.get('eventType', 'default') != 'default':
            raise CalendarWriteError()
        organizer = value.get('organizer')
        if type(organizer) is not dict or organizer.get('self') is not True:
            raise CalendarWriteError()
        if value.get('attendees', []) != [] or value.get('attendeesOmitted', False) is not False:
            raise CalendarWriteError()
        start, end = value.get('start'), value.get('end')
        if type(start) is not dict or type(end) is not dict or 'date' in start or 'date' in end:
            raise CalendarWriteError()
        first, last = start.get('dateTime'), end.get('dateTime')
        _times(first, last)
        etag = _text(value.get('etag'), 1024)
        if any(ord(char) > 126 for char in etag):
            raise CalendarWriteError()
        return {'calendar_id': 'primary', 'external_event_id': event_id, 'etag': etag,
                'planned_start': first, 'planned_end': last, 'private': True,
                'classification': 'flexible', 'organizer_self': True, 'attendees_count': 0}

    def get_event(self, calendar_id, event_id):
        url = self._url(calendar_id, event_id)
        try:
            status, raw = self._request('GET', url)
            if type(status) is not int or status != 200:
                raise CalendarWriteError()
            return self._snapshot(raw, event_id)
        except Exception:
            raise CalendarWriteError() from None

    def patch_times(self, calendar_id, event_id, *, planned_start, planned_end, if_match, send_updates='none'):
        url = self._url(calendar_id, event_id)
        requested = _times(planned_start, planned_end)
        _text(if_match, 1024)
        if any(ord(char) > 126 for char in if_match) or send_updates != 'none':
            raise CalendarWriteError()
        # Local argument failures and failed preflight reads cannot have written.
        before = self.get_event(calendar_id, event_id)
        if before['etag'] != if_match:
            raise CalendarConflict('The Calendar version changed.')
        body = json.dumps({'start': {'dateTime': planned_start}, 'end': {'dateTime': planned_end}}).encode('utf-8')
        if self._before_mutation is not None:
            try:
                self._before_mutation()
            except Exception:
                raise CalendarWriteError() from None
        try:
            status, raw = self._request('PATCH', url + '?sendUpdates=none', body, if_match)
        except Exception:
            raise CalendarUnknownOutcome('The Calendar write outcome is unknown.') from None
        if type(status) is int and status == 412:
            raise CalendarConflict('The Calendar version changed.')
        if type(status) is int and 300 <= status < 500 and status != 408:
            raise CalendarWriteError()
        if type(status) is not int or status != 200:
            raise CalendarUnknownOutcome('The Calendar write outcome is unknown.')
        try:
            after = self._snapshot(raw, event_id)
            if _times(after['planned_start'], after['planned_end']) != requested or after['etag'] == before['etag']:
                raise CalendarWriteError()
            return after
        except Exception:
            raise CalendarUnknownOutcome('The Calendar write outcome is unknown.') from None
