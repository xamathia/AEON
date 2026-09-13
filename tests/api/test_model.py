"""Offline transport tests: no credentials, network or local configuration."""
import copy
import json
import traceback
import unittest
from unittest.mock import patch, MagicMock

from apps.api.model import AnthropicModel, ModelError, ENDPOINT, MAX_BYTES, _HTTPTransport

REQUEST = {'schema_version': '1.0', 'prompt_version': 'deadline-extraction-v1',
           'task': 'extract_arrival_deadline', 'instructions': ['PRIVATE_INJECTION'],
           'data': {'message': {'id': 'm', 'excerpt': 'Arrive by 10:00.'},
                    'events': [{'id': 'e', 'title': 'Meeting', 'planned_start': '2026-09-14T10:00:00Z', 'planned_end': '2026-09-14T11:00:00Z'}],
                    'horizon': {'start': '2026-09-14T00:00:00Z', 'end': '2026-09-15T00:00:00Z'}, 'timezone': 'Europe/Paris'}}
ABSTAIN = {'schema_version': '1.0', 'status': 'abstain'}


def envelope(text=None, **changes):
    return {'type': 'message', 'role': 'assistant', 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps(ABSTAIN) if text is None else text}],
            'usage': {'input_tokens': 34, 'output_tokens': 12, 'cache_creation_input_tokens': 0}, **changes}


class Transport:
    def __init__(self, value=None, status=200):
        self.value = envelope() if value is None else value
        self.status = status
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.value, Exception): raise self.value
        raw = self.value if type(self.value) is bytes else json.dumps(self.value).encode()
        return self.status, raw


class ModelTests(unittest.TestCase):
    def test_minimal_payload_and_measured_usage(self):
        port = Transport()
        model = AnthropicModel('fixture-key', transport=port)
        before = copy.deepcopy(REQUEST)
        result = model.generate(REQUEST)
        self.assertEqual(result, {'output': ABSTAIN, 'usage': {'input_tokens': 34, 'output_tokens': 12}})
        self.assertEqual(REQUEST, before)
        url, options = port.calls[0]
        self.assertEqual(url, ENDPOINT)
        self.assertEqual(options['timeout'], 20)
        self.assertEqual(options['max_bytes'], MAX_BYTES)
        self.assertEqual(options['headers']['anthropic-version'], '2023-06-01')
        payload = json.loads(options['body'])
        self.assertEqual(set(payload), {'model', 'max_tokens', 'system', 'messages', 'output_config'})
        self.assertEqual(payload['model'], 'claude-haiku-4-5-20251001')
        self.assertEqual(payload['max_tokens'], 300)
        self.assertNotIn('PRIVATE_INJECTION', payload['system'])
        self.assertEqual(json.loads(payload['messages'][0]['content']), REQUEST['data'])

    def test_requests_closed_structured_deadline_output(self):
        port = Transport()
        AnthropicModel('fixture', transport=port).generate(REQUEST)
        self.assertEqual(len(port.calls), 1)
        payload = json.loads(port.calls[0][1]['body'])
        self.assertIn('output_config', payload)
        self.assertEqual(payload['output_config'], {'format': {
            'type': 'json_schema',
            'schema': {'anyOf': [
                {'type': 'object', 'properties': {
                    'schema_version': {'type': 'string', 'const': '1.0'},
                    'status': {'type': 'string', 'const': 'abstain'}},
                 'required': ['schema_version', 'status'], 'additionalProperties': False},
                {'type': 'object', 'properties': {
                    'schema_version': {'type': 'string', 'const': '1.0'},
                    'status': {'type': 'string', 'const': 'proposed'},
                    'event_id': {'type': 'string'}, 'deadline': {'type': 'string'},
                    'evidence_quote': {'type': 'string'}},
                 'required': ['schema_version', 'status', 'event_id', 'deadline', 'evidence_quote'],
                 'additionalProperties': False}
            ]}
        }})

    def test_structured_outputs_never_strip_markdown(self):
        proposal = {'schema_version': '1.0', 'status': 'proposed', 'event_id': 'e',
                    'deadline': '2026-09-14T10:00:00Z', 'evidence_quote': '10:00'}
        for output in (ABSTAIN, proposal):
            port = Transport(envelope('```json\n' + json.dumps(output) + '\n```'))
            with self.subTest(status=output['status']), self.assertRaises(ModelError):
                AnthropicModel('fixture', transport=port).generate(REQUEST)
            self.assertEqual(len(port.calls), 1)

    def test_proposal_and_absent_usage(self):
        proposal = {'schema_version': '1.0', 'status': 'proposed', 'event_id': 'e', 'deadline': '2026-09-14T10:00:00Z', 'evidence_quote': '10:00'}
        port = Transport(envelope(json.dumps(proposal), usage=None))
        self.assertEqual(AnthropicModel('fixture', transport=port).generate(REQUEST), {'output': proposal, 'usage': None})

    def test_invalid_configuration_without_network(self):
        with patch('apps.api.model.http.client.HTTPSConnection') as connection:
            for options in ({'api_key': ''}, {'api_key': 'PRIVATE\n'}, {'api_key': 'x', 'model': 'bad/model'}, {'api_key': 'x', 'timeout': True}, {'api_key': 'x', 'timeout': 21}, {'api_key': 'x', 'timeout': float('nan')}):
                with self.subTest(options=options), self.assertRaises(ModelError): AnthropicModel(**options)
            self.assertEqual(repr(AnthropicModel('PRIVATE')), 'AnthropicModel()')
            connection.assert_not_called()

    def test_token_limit_and_context_rejected_before_io(self):
        port = Transport(); model = AnthropicModel('fixture', transport=port)
        for tokens in (0, 301, True, 1.5):
            with self.assertRaises(ModelError): model.generate(REQUEST, max_output_tokens=tokens)
        extra = copy.deepcopy(REQUEST); extra['data']['events'][0]['attendees'] = ['PRIVATE']
        with self.assertRaises(ModelError): model.generate(extra)
        self.assertEqual(port.calls, [])

    def test_http_failures_never_retry_or_expose_body(self):
        for status in (301, 302, 307, 308, 403, 429, 500):
            port = Transport(b'PRIVATE_BODY', status)
            with self.subTest(status=status), self.assertRaises(ModelError) as found:
                AnthropicModel('PRIVATE_KEY', transport=port).generate(REQUEST)
            self.assertNotIn('PRIVATE', str(found.exception))
            self.assertEqual(len(port.calls), 1)

    def test_invalid_json_and_output_schemas(self):
        for text in ('```json\n{}\n```', '{} trailing PRIVATE', '{"schema_version":"1.0","schema_version":"1.0","status":"abstain"}', '{"schema_version":"1.0","status":"abstain","x":NaN}', json.dumps({**ABSTAIN, 'permission': True}), '[]', '{"schema_version":"1.0","status":"proposed","event_id":"e","deadline":true,"evidence_quote":"x"}'):
            with self.subTest(text=text), self.assertRaises(ModelError):
                AnthropicModel('fixture', transport=Transport(envelope(text))).generate(REQUEST)

    def test_truncation_refusal_tools_and_multiple_blocks_fail(self):
        cases = [envelope(stop_reason=reason) for reason in ('max_tokens', 'refusal', 'tool_use', 'stop_sequence', None)]
        cases += [envelope(content=[{'type': 'tool_use', 'text': '{}'}]), envelope(content=envelope()['content'] * 2), envelope(content=[]), envelope(role='user')]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ModelError):
                AnthropicModel('fixture', transport=Transport(value)).generate(REQUEST)

    def test_size_utf8_and_usage_validation(self):
        cases = [b'x' * (MAX_BYTES + 1), b'\xff', envelope(usage={'input_tokens': True, 'output_tokens': 2}), envelope(usage={'input_tokens': -1, 'output_tokens': 2}), envelope('{"schema_version":"1.0","status":"abstain","x":"\\ud800"}')]
        for value in cases:
            with self.subTest(kind=type(value)), self.assertRaises(ModelError):
                AnthropicModel('fixture', transport=Transport(value)).generate(REQUEST)

    def test_provider_exception_traceback_is_redacted(self):
        model = AnthropicModel('PRIVATE_KEY', transport=Transport(TimeoutError('PRIVATE_PROVIDER')))
        try:
            model.generate(REQUEST)
        except ModelError:
            text = traceback.format_exc()
        self.assertNotIn('PRIVATE_PROVIDER', text)
        self.assertNotIn('TimeoutError', text)

    def test_standard_transport_does_not_follow_redirect_or_read_error(self):
        for status in (302, 403, 429, 500):
            connection = MagicMock(); response = connection.getresponse.return_value; response.status = status
            with patch('apps.api.model.http.client.HTTPSConnection', return_value=connection) as factory:
                self.assertEqual(_HTTPTransport().post(ENDPOINT, headers={}, body=b'{}', timeout=20, max_bytes=12), (status, b''))
            factory.assert_called_once_with('api.anthropic.com', timeout=20)
            response.read.assert_not_called(); connection.request.assert_called_once(); connection.close.assert_called_once()

    def test_standard_transport_bounds_read_and_closes_on_timeout(self):
        connection = MagicMock(); response = connection.getresponse.return_value; response.status = 200; response.read.return_value = b'{}'
        with patch('apps.api.model.http.client.HTTPSConnection', return_value=connection):
            self.assertEqual(_HTTPTransport().post(ENDPOINT, headers={}, body=b'{}', timeout=3, max_bytes=12), (200, b'{}'))
        response.read.assert_called_once_with(13); connection.close.assert_called_once()
        connection.reset_mock(); connection.request.side_effect = TimeoutError('PRIVATE')
        with patch('apps.api.model.http.client.HTTPSConnection', return_value=connection), self.assertRaises(ModelError):
            AnthropicModel('fixture').generate(REQUEST)
        connection.close.assert_called_once()


if __name__ == '__main__': unittest.main()
