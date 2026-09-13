"""Explicit live forecasts using server-owned observations and bounded route calls."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import secrets
import time
import urllib.request

from packages.aeon_connectors.transport import HttpTransport, ConnectorError

from packages.aeon_connectors import GoogleRoutesClient, route_to_prior
from .connections import APIError

ROUTE_LIMIT = 20
ROUTE_CACHE_SECONDS = 300


def load_assembler():
    from packages.aeon_scenario import assemble_scenario
    return assemble_scenario


def load_live_engine():
    from packages.aeon_engine import simulate_day
    return simulate_day


def load_live_planner():
    from packages.aeon_planner import plan_live_day
    return plan_live_day


def create_open_routes():
    from packages.aeon_connectors.open_routing import OpenRoutingClient
    return OpenRoutingClient()


class _NoRouteRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectorError('google_routes', 'unsafe_redirect', False, 'Route redirects are disabled.')


def create_google_routes(key):
    # One transport request per explicit call, with no redirects or retry loop.
    transport = HttpTransport()
    transport._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRouteRedirect())
    return GoogleRoutesClient(key, transport=transport)


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError()
    return result.astimezone(timezone.utc)


class LiveService:
    def __init__(self, connections, *, assembler_loader=load_assembler, engine_loader=load_live_engine,
                 routes_factory=create_google_routes, clock=None, planner_loader=load_live_planner,
                 open_routes_factory=create_open_routes, daily_route_budget=None):
        self.connections = connections
        self._assembler_loader = assembler_loader
        self._engine_loader = engine_loader
        self._routes_factory = routes_factory
        self._open_routes_factory = open_routes_factory
        self._daily_route_budget = daily_route_budget
        self._planner_loader = planner_loader
        self._clock = clock if clock is not None else time.time

    def _state(self, session):
        self.connections._ensure_active(session)
        source = session.sources['google_calendar']
        if source.canonical_data is None:
            return None
        if session.live_state is None:
            session.live_state = {'revision': secrets.token_urlsafe(32), 'edges': {}, 'result': None,
                                  'constraints': [], 'planning': None, 'route_cache': {}}
        return session.live_state

    @staticmethod
    def _events(source):
        try:
            return sorted(source.canonical_data['events'], key=lambda event: (instant(event['planned_start']), event['id']))
        except Exception:
            raise APIError('live_unavailable') from None

    def _assemble(self, session, state):
        source = session.sources['google_calendar']
        if len(source.canonical_data['events']) > 100:
            return {'status': 'incomplete', 'scenario': None,
                    'issues': [{'code': 'CALENDAR_TOO_LARGE', 'event_ids': []}]}
        try:
            assembler = self._assembler_loader()
        except Exception:
            return {'status': 'incomplete', 'scenario': None,
                    'issues': [{'code': 'ASSEMBLER_UNAVAILABLE', 'event_ids': []}]}
        try:
            edges = [{'from_event_id': pair[0], 'to_event_id': pair[1],
                      'duration_minutes': item['duration_minutes'], 'source': item['source']}
                     for pair, item in state['edges'].items()]
            return assembler(scenario_id='live:' + state['revision'], timezone=source.data['timezone'],
                             horizon=copy.deepcopy(source.data['horizon']), calendar=copy.deepcopy(source.canonical_data),
                             travel_edges=copy.deepcopy(edges), constraints=copy.deepcopy(state.get('constraints', [])), seed=42, samples=1000)
        except Exception:
            return {'status': 'incomplete', 'scenario': None,
                    'issues': [{'code': 'INVALID_CALENDAR_DATA', 'event_ids': []}]}

    def describe(self, session):
        with session.lock:
            state = self._state(session)
            result = {'revision': None, 'status': 'incomplete', 'calendar': None, 'travel_pairs': [],
                      'routes': {'configured': bool(self.connections._routes_key), 'open_routing': True, 'attempts': session.route_attempts,
                                 'limit': ROUTE_LIMIT, 'remaining': max(0, ROUTE_LIMIT - session.route_attempts)},
                      'issues': [{'code': 'CALENDAR_NOT_READ', 'event_ids': []}], 'result': None,
                      'constraints': [], 'planning': None}
            if state is None:
                return result
            source = session.sources['google_calendar']
            events = self._events(source)
            projection = {item['id']: item for item in source.data['events']}
            result['revision'] = state['revision']
            result['constraints'] = copy.deepcopy(state.get('constraints', []))
            if state.get('planning') is not None:
                result['planning'] = copy.deepcopy(state['planning'])
                for plan in result['planning']['plans']:
                    plan.pop('scenario', None)
            result['calendar'] = {key: copy.deepcopy(source.data[key]) for key in ('read_at', 'complete', 'horizon', 'timezone')}
            result['calendar'].update(events=[copy.deepcopy(projection[item['id']]) for item in events],
                                      last_read_failed=source.read['status'] == 'failed')
            # Large calendars are visible, but do not create hundreds of editable pairs.
            if len(events) <= 100:
                for left, right in zip(events, events[1:]):
                    pair = (left['id'], right['id'])
                    result['travel_pairs'].append({'from_event_id': pair[0], 'to_event_id': pair[1],
                        'from_title': projection[pair[0]]['title'], 'to_title': projection[pair[1]]['title'],
                        'departure_time': left['planned_end'], 'travel': copy.deepcopy(state['edges'].get(pair))})
            assembled = self._assemble(session, state)
            result['issues'] = copy.deepcopy(assembled['issues'])
            result['status'] = assembled['status']
            if assembled['status'] == 'ready' and state['result'] is not None:
                result['status'], result['result'] = 'simulated', copy.deepcopy(state['result'])
            return result

    def _validate_revision(self, session, body):
        if type(body) is not dict or type(body.get('revision')) is not str:
            raise APIError('invalid_request')
        state = self._state(session)
        if state is None or body['revision'] != state['revision']:
            raise APIError('revision_conflict')
        return state

    def _claim(self, session, body):
        state = self._validate_revision(session, body)
        if session.live_busy or any(source.reading for source in session.sources.values()):
            raise APIError('live_busy')
        session.live_busy = True
        return state

    def _guard(self, session, state):
        self.connections._ensure_active(session)
        if session.live_state is not state or session.sources['google_calendar'].canonical_data is None:
            raise APIError('revision_conflict')
        if any(source.reading for source in session.sources.values()):
            raise APIError('live_busy')

    @staticmethod
    def _address(value):
        if type(value) is not str or not 1 <= len(value.strip()) <= 500 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise APIError('invalid_request')
        value = value.strip()
        if value.lower().startswith(('http:', 'https:')):
            raise APIError('invalid_request')
        return {'address': value}

    def travel(self, session, body):
        if type(body) is not dict or type(body.get('kind')) is not str:
            raise APIError('invalid_request')
        kind = body['kind']
        keys = {'revision', 'from_event_id', 'to_event_id', 'kind'}
        if kind in ('google_routes', 'osrm'):
            keys |= {'origin_address', 'destination_address'}
        if kind not in ('same_location', 'google_routes', 'osrm') or set(body) != keys:
            raise APIError('invalid_request')
        if any(type(body.get(key)) is not str or not body[key] or len(body[key]) > 512 for key in ('from_event_id', 'to_event_id')):
            raise APIError('invalid_request')
        origin = self._address(body['origin_address']) if kind != 'same_location' else None
        destination = self._address(body['destination_address']) if kind != 'same_location' else None
        with session.lock:
            state = self._validate_revision(session, body)
            events = self._events(session.sources['google_calendar'])
            if len(events) > 100:
                raise APIError('invalid_request')
            pair = (body['from_event_id'], body['to_event_id'])
            left = next((left for left, right in zip(events, events[1:]) if (left['id'], right['id']) == pair), None)
            if left is None:
                raise APIError('invalid_request')
            departure = left['planned_end']
            cache_key, cached = None, None
            if kind != 'same_location':
                now = self._clock()
                if type(now) not in (int, float) or not math.isfinite(now):
                    raise APIError('live_unavailable')
                cache_key = hashlib.sha256(json.dumps([kind, origin, destination, departure], sort_keys=True).encode()).hexdigest()
                entry = state.setdefault('route_cache', {}).get(cache_key)
                if entry is not None and 0 <= now - entry['time'] < ROUTE_CACHE_SECONDS:
                    cached = copy.deepcopy(entry['travel'])
            if kind == 'google_routes':
                if not self.connections._routes_key:
                    raise APIError('routes_unavailable')
                now = self._clock()
                if type(now) not in (int, float) or not math.isfinite(now):
                    raise APIError('live_unavailable')
                if instant(departure).timestamp() <= now:
                    raise APIError('departure_in_past')
            if kind != 'same_location' and cached is None and session.route_attempts >= ROUTE_LIMIT:
                raise APIError('routes_budget_exhausted')
            state = self._claim(session, body)
            if kind != 'same_location' and cached is None:
                session.route_attempts += 1
        try:
            if kind == 'same_location':
                digest = hashlib.sha256(json.dumps([state['revision'], *pair]).encode()).hexdigest()
                travel = {'kind': kind, 'duration_minutes': {'min': 0, 'mode': 0, 'max': 0}, 'observed_at': None,
                          'source': {'provider': 'user', 'reference': 'user:same-location:' + digest, 'synthetic': False,
                                     'assumption': 'Explicitly declared same location; zero travel time modelled.'}}
            elif cached is not None:
                travel = {**cached, 'cached': True}
            else:
                try:
                    with session.lock:
                        self._guard(session, state)
                        if kind == 'osrm':
                            # Cache addresses per session, discarded with source observations.
                            if session.open_routing_client is None:
                                session.open_routing_client = self._open_routes_factory()
                            open_routes = session.open_routing_client
                    if kind == 'osrm':
                        travel = {**open_routes.compute_route(origin['address'], destination['address']), 'kind': kind}
                    else:
                        client = self._routes_factory(self.connections._routes_key)
                        if self._daily_route_budget is not None:
                            try:
                                self._daily_route_budget.claim()
                            except Exception:
                                raise APIError('routes_budget_exhausted') from None
                        with session.lock:
                            self._guard(session, state)
                        observation = client.compute_route(origin, destination, departure)
                        travel = {**route_to_prior(observation), 'kind': kind, 'observed_at': observation['observed_at']}
                    travel['cached'] = False
                except APIError:
                    raise
                except Exception:
                    raise APIError('routes_failed') from None
            with session.lock:
                self._guard(session, state)
                if cache_key is not None and cached is None:
                    state['route_cache'][cache_key] = {'time': now, 'travel': copy.deepcopy(travel)}
                state['edges'][pair] = travel
                state['result'] = None
                state['planning'] = None
            return self.describe(session)
        finally:
            with session.lock:
                session.live_busy = False

    def simulate(self, session, body):
        if type(body) is not dict or set(body) != {'revision'}:
            raise APIError('invalid_request')
        with session.lock:
            state = self._claim(session, body)
        try:
            with session.lock:
                self._guard(session, state)
                assembled = self._assemble(session, state)
            if assembled['status'] != 'ready':
                return self.describe(session)
            try:
                result = self._engine_loader()(assembled['scenario'])
            except Exception:
                raise APIError('live_unavailable') from None
            with session.lock:
                self._guard(session, state)
                state['result'] = copy.deepcopy(result)
            return self.describe(session)
        finally:
            with session.lock:
                session.live_busy = False

    def reset(self, session, body):
        if type(body) is not dict or set(body) != {'revision'}:
            raise APIError('invalid_request')
        with session.lock:
            previous = self._validate_revision(session, body)
            constraints = copy.deepcopy(previous.get('constraints', []))
            session.live_state = None
            session.intelligence_state = None
            self._state(session)['constraints'] = constraints
        return self.describe(session)

    def plans(self, session, body):
        if type(body) is not dict or set(body) != {'revision', 'target_event_id', 'movable_event_ids', 'window'}:
            raise APIError('invalid_request')
        ids = body['movable_event_ids']
        if (type(ids) is not list or not 1 <= len(ids) <= 5 or any(type(i) is not str for i in ids)
                or len(set(ids)) != len(ids) or type(body['target_event_id']) is not str
                or body['target_event_id'] in ids):
            raise APIError('invalid_request')
        with session.lock:
            state = self._claim(session, body)
        try:
            with session.lock:
                self._guard(session, state)
                assembled = self._assemble(session, state)
                if assembled['status'] != 'ready':
                    return self.describe(session)
                payload = assembled['scenario']
                events = {event['id']: event for event in payload['events']}
                if body['target_event_id'] not in events or any(i not in events or events[i].get('private') is not True for i in ids):
                    raise APIError('invalid_request')
                for identifier in ids:
                    event = events[identifier]
                    if event.get('organizer_self') is not True or event.get('attendees_count', 0) != 0 or event.get('attendees_omitted') is True:
                        raise APIError('invalid_request')
                    event['classification'] = 'flexible'
                selection = {key: copy.deepcopy(body[key]) for key in ('target_event_id', 'movable_event_ids', 'window')}
                selection.update(step_minutes=15, max_candidates=60)
            try:
                planner = self._planner_loader()
            except Exception:
                raise APIError('planning_unavailable') from None
            try:
                result = planner(payload, selection)
            except ValueError:
                raise APIError('invalid_request') from None
            except Exception:
                raise APIError('planning_unavailable') from None
            with session.lock:
                self._guard(session, state)
                state['planning'], state['result'] = copy.deepcopy(result), copy.deepcopy(result['baseline'])
            return self.describe(session)
        finally:
            with session.lock:
                session.live_busy = False
