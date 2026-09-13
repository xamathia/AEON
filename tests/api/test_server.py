import copy
import http.client
import importlib
import json
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ApplicationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('apps.api.server')
        self.seen = []
        def simulate(payload):
            self.seen.append(copy.deepcopy(payload))
            return {'scenario_id': payload['scenario_id'], 'samples': payload['samples'],
                    'events': [], 'sources': [], 'llm_calls': 0,
                    'summary': {'probability_any_late': 0.125, 'expected_total_delay_minutes': 3.5}}
        self.engine = simulate
        self.server = self.module.create_server(port=0, engine_loader=lambda: self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        merged = {'Content-Type': 'application/json', 'X-Aeon-Request': 'demo-v1'}
        merged.update(headers or {})
        conn.request(method, path, body=body, headers=merged)
        res = conn.getresponse()
        raw = res.read()
        return res.status, res.headers, json.loads(raw) if res.headers.get_content_type() == 'application/json' else raw

    def test_status_does_not_claim_google_is_connected(self):
        status, _, data = self.request('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertTrue(data['engine_available'])
        self.assertTrue(all(i['status'] == 'not_connected' for i in data['integrations']))

    def test_engine_output_is_forwarded_without_invented_probability(self):
        status, _, data = self.request('POST', '/api/simulate', '{"traffic":"normal"}')
        self.assertEqual(status, 200)
        self.assertEqual(data['simulation']['summary']['probability_any_late'], 0.125)
        self.assertEqual(data['scenario'], self.seen[0])
        self.assertGreaterEqual(data['metrics']['elapsed_ms'], 0)

    def test_incident_is_reversible_and_does_not_accumulate(self):
        for mode in ('normal', 'incident', 'incident', 'normal'):
            self.assertEqual(self.request('POST', '/api/simulate', json.dumps({'traffic': mode}))[0], 200)
        self.assertEqual(self.seen[0], self.seen[3])
        self.assertEqual(self.seen[1], self.seen[2])
        before = self.seen[0]['travel_edges'][-1]['duration_minutes']
        after = self.seen[1]['travel_edges'][-1]['duration_minutes']
        self.assertEqual(after, {k: v + 12 for k, v in before.items()})
        self.assertTrue(self.seen[1]['travel_edges'][-1]['source']['synthetic'])

    def test_missing_engine_returns_503_without_predictions(self):
        def missing():
            raise self.module.EngineUnavailable()
        self.server.engine_loader = missing
        status, _, data = self.request('POST', '/api/simulate', '{}')
        self.assertEqual(status, 503)
        self.assertEqual(data['error']['code'], 'engine_unavailable')
        self.assertNotIn('simulation', data)

    def test_bad_requests_do_not_reach_engine(self):
        for body, headers, expected in [
            ('[]', {}, 400), ('{"traffic":"pretend-live"}', {}, 400),
            ('{broken', {}, 400), ('{"seed":1}', {}, 400),
            ('{}', {'Content-Type':'text/plain'}, 415),
            ('{}', {'Origin':'https://untrusted.example'}, 403),
            ('{}', {'X-Aeon-Request':''}, 403),
            ('x' * 65537, {}, 413),
        ]:
            with self.subTest(body=body[:25], expected=expected):
                self.assertEqual(self.request('POST', '/api/simulate', body, headers)[0], expected)
        self.assertEqual(self.seen, [])

    def test_engine_failure_does_not_leak_internal_exception(self):
        def broken(payload):
            raise RuntimeError('secret internal path')
        self.engine = broken
        status, _, data = self.request('POST', '/api/simulate', '{}')
        self.assertEqual(status, 500)
        self.assertNotIn('secret', json.dumps(data))

    def test_static_files_are_allowlisted(self):
        for path in ('/.git/config', '/docs/product/ENGINE_CONTRACT_V1.md', '/../README.md'):
            self.assertEqual(self.request('GET', path)[0], 404)
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<!doctype html>', body.lower())
        self.assertIn('default-src', headers['Content-Security-Policy'])

    def test_disallowed_host_is_rejected(self):
        self.assertEqual(self.request('GET', '/api/demo', headers={'Host':'other.example'})[0], 403)


class RealEngineHTTPTests(unittest.TestCase):
    def test_browser_api_pipeline_uses_real_engine_and_reversible_incident(self):
        module = importlib.import_module('apps.api.server')
        try:
            module.load_engine()
        except module.EngineUnavailable:
            self.skipTest('The engine is not available in this checkout.')
        server = module.create_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            outputs = []
            for traffic in ('normal', 'incident', 'normal'):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request('POST', '/api/simulate', json.dumps({'traffic': traffic}),
                                 {'Content-Type': 'application/json', 'X-Aeon-Request': 'demo-v1'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    outputs.append(json.loads(response.read()))
                finally:
                    conn.close()
            self.assertEqual(outputs[0]['simulation'], outputs[2]['simulation'])
            self.assertEqual(outputs[0]['scenario'], outputs[2]['scenario'])
            self.assertEqual(outputs[0]['simulation']['samples'], 1000)
            self.assertEqual(outputs[0]['simulation']['llm_calls'], 0)
            before, incident = [item['simulation']['events'][-1] for item in outputs[:2]]
            self.assertGreaterEqual(incident['late_arrival_probability'], before['late_arrival_probability'])
            from datetime import datetime
            before_time = datetime.fromisoformat(before['arrival']['p50'].replace('Z', '+00:00'))
            after_time = datetime.fromisoformat(incident['arrival']['p50'].replace('Z', '+00:00'))
            self.assertEqual((after_time - before_time).total_seconds(), 12 * 60)
            self.assertTrue(all(source['synthetic'] for source in outputs[0]['simulation']['sources']))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
