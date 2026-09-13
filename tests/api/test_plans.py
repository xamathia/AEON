"""HTTP boundary tests; injected planner doubles never enter application code."""
import copy
from datetime import datetime
import http.client
import importlib
import json
import threading
import unittest


class PlansHTTPTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('apps.api.server')
        self.seen = []
        self.expected = {'schema_version': '1.0', 'status': 'no_better_plan',
                         'baseline': {'events': []}, 'plans': [],
                         'search': {'evaluated_candidates': 17, 'truncated': False,
                                    'rejected_reasons': ['MISSING_ROUTE']}, 'llm_calls': 0}
        def planner(payload, context):
            self.seen.append(copy.deepcopy((payload, context)))
            return copy.deepcopy(self.expected)
        self.planner = planner
        self.server = self.module.create_server(port=0, planner_loader=lambda: self.planner)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method='POST', path='/api/plans', body='{}', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        merged = {'Content-Type': 'application/json', 'X-Aeon-Request': 'demo-v1'}
        merged.update(headers or {})
        conn.request(method, path, body=body, headers=merged)
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_output_is_forwarded_including_honest_no_better_plan(self):
        status, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(data['planning'], self.expected)
        self.assertEqual(data['scenario'], self.seen[0][0])
        self.assertGreaterEqual(data['metrics']['elapsed_ms'], 0)
        self.assertEqual(data['intervention'], {'kind': 'normal', 'synthetic': True, 'affected_edges': []})

    def test_context_limits_search_to_declared_demo_afternoon(self):
        self.assertEqual(self.request()[0], 200)
        payload, context = self.seen[0]
        original = self.module.scenario_for('normal')[0]
        self.assertEqual(payload['horizon'], {'start': '2026-09-14T14:00:00+02:00',
                                             'end': '2026-09-14T21:00:00+02:00'})
        self.assertEqual({k: v for k, v in payload.items() if k != 'horizon'},
                         {k: v for k, v in original.items() if k != 'horizon'})
        self.assertEqual(context['target_event_id'], 'dinner')
        self.assertEqual(context['movable_event_ids'], ['focus'])
        self.assertEqual(context['location_by_event_id'], {'meeting': 'office', 'focus': 'office', 'dinner': 'restaurant'})
        self.assertEqual(context['step_minutes'], 15)
        self.assertEqual(context['max_candidates'], 100)
        self.assertEqual(len(context['route_catalog']), 1)
        route = context['route_catalog'][0]
        self.assertEqual((route['from_location_id'], route['to_location_id']), ('office', 'restaurant'))
        self.assertEqual(route['source'], original['travel_edges'][-1]['source'])
        self.assertEqual(route['duration_minutes'], original['travel_edges'][-1]['duration_minutes'])

    def test_incident_changes_catalog_and_scenario_without_accumulating(self):
        for traffic in ('normal', 'incident', 'incident', 'normal'):
            self.assertEqual(self.request(body=json.dumps({'traffic': traffic}))[0], 200)
        self.assertEqual(self.seen[0], self.seen[3])
        self.assertEqual(self.seen[1], self.seen[2])
        before = self.seen[0][1]['route_catalog'][0]['duration_minutes']
        payload, context = self.seen[1]
        after = context['route_catalog'][0]
        self.assertEqual(after['duration_minutes'], {k: v + 12 for k, v in before.items()})
        self.assertEqual(after['source'], payload['travel_edges'][-1]['source'])
        self.assertIn('Incident', after['source']['assumption'])

    def test_planner_mutation_cannot_change_response_or_future_requests(self):
        def mutating(payload, context):
            payload['events'].clear()
            context['route_catalog'].clear()
            return self.expected
        self.planner = mutating
        first = self.request()[1]['scenario']
        second = self.request()[1]['scenario']
        self.assertEqual(first, second)
        self.assertEqual(len(first['events']), 3)

    def test_missing_planner_is_unavailable_in_status_and_request(self):
        def missing():
            raise self.module.PlannerUnavailable()
        self.server.planner_loader = missing
        status, data = self.request('GET', '/api/status', body=None)
        self.assertEqual(status, 200)
        self.assertFalse(data['planner_available'])
        status, data = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(data['error']['code'], 'planner_unavailable')
        self.assertNotIn('planning', data)

    def test_bad_requests_cannot_inject_plans_or_permissions(self):
        for body, headers, expected in [
            ('[]', {}, 400), ('{"traffic":"live"}', {}, 400),
            ('{"movable_event_ids":["meeting"]}', {}, 400),
            ('{"scenario":{}}', {}, 400), ('{"plan_id":"invented"}', {}, 400),
            ('{}', {'Content-Type': 'text/plain'}, 415),
            ('{}', {'Origin': 'https://untrusted.example'}, 403),
            ('{}', {'X-Aeon-Request': ''}, 403),
            ('{}', {'Host': 'untrusted.example'}, 403),
            ('x' * 65537, {}, 413),
        ]:
            with self.subTest(body=body[:25], expected=expected):
                self.assertEqual(self.request(body=body, headers=headers)[0], expected)
        self.assertEqual(self.seen, [])

    def test_planner_failure_and_nonfinite_output_do_not_leak_details(self):
        def broken(payload, context):
            raise RuntimeError('secret internal credential')
        for planner in (broken, lambda *args: {'risk': float('nan')}):
            self.planner = planner
            status, data = self.request()
            self.assertEqual(status, 500)
            self.assertEqual(data['error']['code'], 'planner_failed')
            self.assertNotIn('secret', json.dumps(data))
            self.assertNotIn('planning', data)


class RealPlannerHTTPTests(unittest.TestCase):
    def test_plans_are_real_improvements_and_incident_reset_is_reproducible(self):
        module = importlib.import_module('apps.api.server')
        try:
            module.load_planner()
        except module.PlannerUnavailable:
            self.skipTest('The planner is not available in this checkout.')
        server = module.create_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            outputs = []
            for traffic in ('normal', 'incident', 'normal'):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
                try:
                    conn.request('POST', '/api/plans', json.dumps({'traffic': traffic}),
                                 {'Content-Type': 'application/json', 'X-Aeon-Request': 'demo-v1'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    outputs.append(json.loads(response.read()))
                finally:
                    conn.close()
            self.assertEqual(outputs[0]['planning'], outputs[2]['planning'])
            simulate = module.load_engine()
            for output in outputs:
                planning = output['planning']
                self.assertEqual(planning['status'], 'plans_found')
                self.assertEqual(planning['baseline'], simulate(module.scenario_for(output['intervention']['kind'])[0]))
                self.assertGreater(len(planning['plans']), 0)
                self.assertLessEqual(len(planning['plans']), 3)
                self.assertLessEqual(planning['search']['evaluated_candidates'], 100)
                self.assertFalse(planning['search']['truncated'])
                for plan in planning['plans']:
                    self.assertEqual(plan['simulation'], simulate(plan['scenario']))
                    self.assertLess(plan['target_risk_after'], plan['target_risk_before'])
                    self.assertEqual(plan['simulation']['llm_calls'], 0)
                    self.assertTrue(plan['requires_approval'])
                    self.assertEqual(plan['scenario']['constraints'], output['scenario']['constraints'])
                    self.assertEqual(plan['scenario']['horizon'], output['scenario']['horizon'])
                    for event in plan['scenario']['events']:
                        if event['id'] != 'focus':
                            self.assertEqual(event, next(e for e in output['scenario']['events'] if e['id'] == event['id']))
                    operation = plan['operations'][0]
                    self.assertEqual(operation['event_id'], 'focus')
                    start = datetime.fromisoformat(operation['after']['planned_start'].replace('Z', '+00:00'))
                    horizon_start = datetime.fromisoformat(output['scenario']['horizon']['start'])
                    self.assertGreaterEqual(start, horizon_start)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
