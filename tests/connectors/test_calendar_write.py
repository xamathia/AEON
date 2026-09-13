"""Calendar writer tests use a memory transport, never a real account."""
import copy
import json
import tempfile
import traceback
import unittest
from unittest.mock import MagicMock, patch

from packages.aeon_connectors.calendar_write import GoogleCalendarWriter, CalendarWriteError, _HTTPTransport
from packages.aeon_actions import AuditStore, PlanExecutor, CalendarConflict, CalendarUnknownOutcome

START = '2026-09-14T15:00:00Z'
END = '2026-09-14T16:00:00Z'
AFTER = '2026-09-14T13:00:00Z'
AFTER_END = '2026-09-14T14:00:00Z'


def event(**changes):
    return {'id': 'focus', 'etag': '"version1"', 'status': 'confirmed', 'organizer': {'self': True},
            'start': {'dateTime': START}, 'end': {'dateTime': END}, **changes}


class Memory:
    def __init__(self, data=None):
        self.data = event() if data is None else data
        self.calls = []
        self.patch_failure = None
        self.get_failure = None
        self.after_patch = None
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        failure = self.patch_failure if method == 'PATCH' else self.get_failure
        if isinstance(failure, Exception): raise failure
        if failure is not None: return failure, b'PRIVATE_ERROR'
        if method == 'PATCH':
            if kwargs['headers']['If-Match'] != self.data['etag']: return 412, b''
            self.data.update(json.loads(kwargs['body']))
            self.data['etag'] += 'next'
            if self.after_patch is not None: return 200, self.after_patch
        return 200, json.dumps(self.data).encode()


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.port = Memory()
        self.writer = GoogleCalendarWriter('fixture-token', authorized_event_ids=['focus'], transport=self.port)
    def move(self, **changes):
        return self.writer.patch_times('primary', 'focus', **dict(planned_start=AFTER, planned_end=AFTER_END, if_match='"version1"', **changes))

    def test_snapshot_and_minimal_conditional_patch(self):
        before = self.writer.get_event('primary', 'focus')
        self.assertEqual(before, {'calendar_id': 'primary', 'external_event_id': 'focus', 'etag': '"version1"', 'planned_start': START, 'planned_end': END, 'private': True, 'classification': 'flexible', 'organizer_self': True, 'attendees_count': 0})
        after = self.move()
        method, url, options = self.port.calls[-1]
        self.assertEqual(method, 'PATCH'); self.assertTrue(url.endswith('/focus?sendUpdates=none'))
        self.assertEqual(options['headers']['If-Match'], before['etag'])
        self.assertEqual(json.loads(options['body']), {'start': {'dateTime': AFTER}, 'end': {'dateTime': AFTER_END}})
        self.assertEqual(options['timeout'], 15); self.assertEqual(options['max_bytes'], 1024*1024)
        self.assertEqual(after['planned_start'], AFTER)

    def test_unsafe_events_never_patch(self):
        for change in ({'organizer': {'self': False}}, {'attendees': [{}]}, {'attendeesOmitted': True}, {'recurrence': ['RRULE:FREQ=DAILY']}, {'recurringEventId': 'series'}, {'originalStartTime': {'dateTime': START}}, {'status': 'cancelled'}, {'status': 'tentative'}, {'start': {'date': '2026-09-14'}}, {'eventType': 'birthday'}):
            self.port.data = event(**change); self.port.calls.clear()
            with self.subTest(change=change), self.assertRaises(CalendarWriteError): self.move()
            self.assertEqual([c[0] for c in self.port.calls], ['GET'])

    def test_arguments_rejected_before_io(self):
        for calendar, identifier in [('other', 'focus'), ('primary', 'unknown'), ('primary', '../focus')]:
            with self.assertRaises(CalendarWriteError): self.writer.get_event(calendar, identifier)
        for start, end, etag, updates in [('2026-09-14T13:00:00', AFTER_END, 'x', 'none'), (AFTER_END, AFTER, 'x', 'none'), (AFTER, AFTER_END, '', 'none'), (AFTER, AFTER_END, 'x\r\nPrivate', 'none'), (AFTER, AFTER_END, 'x', 'all')]:
            with self.assertRaises(CalendarWriteError): self.writer.patch_times('primary','focus',planned_start=start,planned_end=end,if_match=etag,send_updates=updates)
        self.assertEqual(self.port.calls, [])

    def test_constructor_validation_and_redacted_repr(self):
        for identifiers in ([], ['focus']*2, ['1','2','3','4','5','6'], 'focus', ['..'], ['x\n']):
            with self.assertRaises(CalendarWriteError): GoogleCalendarWriter('fixture', authorized_event_ids=identifiers)
        self.assertNotIn('fixture-token', repr(self.writer))
        with self.assertRaises(CalendarWriteError): GoogleCalendarWriter('bad\n', authorized_event_ids=['x'])

    def test_identifier_is_percent_encoded(self):
        self.port.data = event(id='a/b ?')
        writer = GoogleCalendarWriter('fixture', authorized_event_ids=['a/b ?'], transport=self.port)
        writer.get_event('primary', 'a/b ?')
        self.assertTrue(self.port.calls[-1][1].endswith('/a%2Fb%20%3F'))

    def test_etag_changes_before_and_after_preflight_conflict(self):
        self.port.data['etag'] = '"human"'
        with self.assertRaises(CalendarConflict): self.move()
        self.assertEqual([c[0] for c in self.port.calls], ['GET'])
        self.port.data = event(); self.port.patch_failure = 412
        with self.assertRaises(CalendarConflict): self.move()
        self.assertEqual(len([c for c in self.port.calls if c[0]=='PATCH']), 1)

    def test_patch_uncertainty_never_retries(self):
        for failure in (TimeoutError('PRIVATE_TOKEN'), ConnectionResetError('PRIVATE'), 500, 503, 408):
            self.port.patch_failure = failure; self.port.calls.clear()
            with self.subTest(failure=type(failure)), self.assertRaises(CalendarUnknownOutcome): self.move()
            self.assertEqual([c[0] for c in self.port.calls], ['GET', 'PATCH'])
        for raw in (b'PRIVATE', b'x'*(1024*1024+1), json.dumps(event()).encode(), json.dumps(event(id='wrong')).encode()):
            self.port.data = event(); self.port.patch_failure = None; self.port.after_patch = raw
            with self.assertRaises(CalendarUnknownOutcome): self.move()

    def test_get_failures_and_definite_patch_rejections_are_redacted(self):
        for status in (302, 401, 403, 429):
            self.port.patch_failure = status
            with self.assertRaises(CalendarWriteError) as found: self.move()
            self.assertNotIn('PRIVATE', str(found.exception))
        self.port.get_failure = TimeoutError('PRIVATE')
        try: self.move()
        except CalendarWriteError: trace = traceback.format_exc()
        self.assertNotIn('TimeoutError', trace); self.assertNotIn('PRIVATE', trace)
        self.assertEqual(self.port.calls[-1][0], 'GET')

    def test_plan_executor_execute_and_undo(self):
        current = {'focus': self.writer.get_event('primary', 'focus')}
        plan = {'id': 'candidate', 'operations': [{'event_id':'focus', 'before': {'planned_start':START,'planned_end':END}, 'after': {'planned_start':AFTER,'planned_end':AFTER_END}}]}
        policy = {'autonomy_level':3,'allowed_calendar_ids':['primary'],'allowed_event_ids':['focus'],'approved_plan_hash':None,'kill_switch':False}
        with tempfile.TemporaryDirectory() as folder:
            store = AuditStore(folder + '/audit.sqlite')
            executor = PlanExecutor(self.writer, store)
            receipt = executor.execute(plan, current_events=current, policy=policy, idempotency_key='fixture-execution')
            self.assertEqual(receipt['status'], 'applied')
            undone = executor.undo(receipt['id'], policy=policy)
            self.assertEqual(undone['status'], 'undone')
            self.assertEqual(self.port.data['start']['dateTime'], START)
            self.assertEqual(len([c for c in self.port.calls if c[0]=='PATCH']), 2)

    def test_transport_no_redirect_and_bounded_read(self):
        for status in (200, 302, 500):
            connection = MagicMock(); response = connection.getresponse.return_value; response.status = status; response.read.return_value = b'{}'
            with patch('packages.aeon_connectors.calendar_write.http.client.HTTPSConnection', return_value=connection) as factory:
                _HTTPTransport().request('GET','https://www.googleapis.com/calendar/v3/calendars/primary/events/focus',headers={},body=None,timeout=15,max_bytes=10)
            factory.assert_called_once_with('www.googleapis.com', timeout=15)
            if status == 200: response.read.assert_called_once_with(11)
            else: response.read.assert_not_called()
            connection.request.assert_called_once(); connection.close.assert_called_once()


    def test_before_mutation_guard_runs_after_internal_get_and_blocks_patch(self):
        def guard():
            self.assertEqual([call[0] for call in self.port.calls], ['GET'])
            raise RuntimeError('revoked')
        writer = GoogleCalendarWriter('fixture', authorized_event_ids=['focus'], transport=self.port, before_mutation=guard)
        with self.assertRaises(CalendarWriteError):
            writer.patch_times('primary', 'focus', planned_start=AFTER, planned_end=AFTER_END, if_match='"version1"')
        self.assertEqual([call[0] for call in self.port.calls], ['GET'])

if __name__ == '__main__': unittest.main()
