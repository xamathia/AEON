"""Local app: demo and explicitly requested Google data reads."""
import argparse
import copy
from datetime import datetime
import importlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from .configuration import load_configuration
from .connections import APIError, ConnectionService, PROVIDERS
from .live import LiveService, load_assembler, load_live_planner, create_open_routes, create_google_routes
from .intelligence import IntelligenceService
from .actions import ActionService, ActionError
from .route_budget import DailyRouteBudget
from packages.aeon_oauth import GoogleOAuth
from packages.aeon_connectors.calendar_write import GoogleCalendarWriter
from packages.aeon_connectors import GoogleRoutesClient

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'apps/web'
MAX_BODY = 64 * 1024
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
          '/favicon.svg': ('favicon.svg', 'image/svg+xml')}


class EngineUnavailable(Exception):
    pass


class PlannerUnavailable(Exception):
    pass


def load_engine():
    importlib.invalidate_caches()
    try:
        module = importlib.import_module('packages.aeon_engine')
    except ModuleNotFoundError as exc:
        if exc.name in ('packages', 'packages.aeon_engine'):
            raise EngineUnavailable() from exc
        raise
    return module.simulate_day


def load_planner():
    importlib.invalidate_caches()
    try:
        module = importlib.import_module('packages.aeon_planner')
    except ModuleNotFoundError as exc:
        if exc.name in ('packages', 'packages.aeon_planner'):
            raise PlannerUnavailable() from exc
        raise
    planner = getattr(module, 'plan_day', None)
    if not callable(planner):
        raise PlannerUnavailable()
    return planner


def scenario_for(traffic):
    scenario = json.loads((ROOT / 'fixtures/demo/day-v1.json').read_text())
    affected = []
    if traffic == 'incident':
        for edge in scenario['travel_edges']:
            if edge['duration_minutes']['max'] <= 0:
                continue
            edge['duration_minutes'] = {key: value + 12 for key, value in edge['duration_minutes'].items()}
            edge['source']['assumption'] += ' Demo Incident injected: +12 minutes for every possible travel duration.'
            affected.append([edge['from_event_id'], edge['to_event_id']])
    return scenario, affected


def planning_input(scenario):
    """Declared demo preferences, not permissions inferred from source text."""
    scenario = copy.deepcopy(scenario)
    day = datetime.fromisoformat(scenario['horizon']['start']).astimezone(ZoneInfo(scenario['timezone']))
    scenario['horizon'] = {
        'start': day.replace(hour=14, minute=0, second=0, microsecond=0).isoformat(),
        'end': day.replace(hour=21, minute=0, second=0, microsecond=0).isoformat(),
    }
    route = next(edge for edge in scenario['travel_edges']
                 if edge['from_event_id'] == 'focus' and edge['to_event_id'] == 'dinner')
    context = {
        'target_event_id': 'dinner',
        'movable_event_ids': ['focus'],
        'location_by_event_id': {'meeting': 'office', 'focus': 'office', 'dinner': 'restaurant'},
        'route_catalog': [{'from_location_id': 'office', 'to_location_id': 'restaurant',
                           'duration_minutes': copy.deepcopy(route['duration_minutes']),
                           'source': copy.deepcopy(route['source'])}],
        'step_minutes': 15,
        'max_candidates': 100,
    }
    return scenario, context


class Handler(BaseHTTPRequestHandler):
    server_version = 'AEONLocal/1.0'

    def log_message(self, format, *args):
        # The prototype does not log request bodies or personal data.
        pass

    def respond(self, status, data, content_type='application/json; charset=utf-8', headers=None):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, code, message):
        self.respond(status, {'error': {'code': code, 'message': message}})

    def allowed_host(self):
        expected = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host') not in expected:
            self.error(403, 'host_rejected', 'Open AEON using its local address.')
            return False
        return True

    def canonical_auth_origin(self, *, post=False):
        service = self.server.connections
        origin = self.headers.get('Origin')
        if (self.headers.get('Host') != service.origin.removeprefix('http://')
                or len(self.headers.get_all('Origin', [])) > 1
                or (post and origin != service.origin)
                or (origin is not None and origin != service.origin)
                or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            self.error(403, 'origin_rejected', 'Open AEON at ' + service.origin + ' in the same browser used for Google consent.')
            return False
        return True

    def session_cookie(self):
        found = []
        for header in self.headers.get_all('Cookie', []):
            for item in header.split(';'):
                key, separator, value = item.strip().partition('=')
                if key == 'aeon_session':
                    if not separator:
                        raise APIError('session_invalid')
                    found.append(value)
        if len(found) > 1:
            raise APIError('session_invalid')
        return found[0] if found else None

    @staticmethod
    def clear_session_header():
        return [('Set-Cookie', 'aeon_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')]

    def connection_error(self, error):
        headers = self.clear_session_header() if error.code in ('session_required', 'session_invalid', 'session_expired') else []
        self.respond(error.status, {'error': {'code': error.code, 'message': str(error)}}, headers=headers)

    def auth_get(self, path):
        if not self.canonical_auth_origin():
            return
        try:
            session, created = self.server.connections.session(self.session_cookie(), create=path == '/api/session')
            if path == '/api/session':
                data = self.server.connections.session_info(session)
                headers = [('Set-Cookie', 'aeon_session=' + session.id + '; HttpOnly; SameSite=Lax; Path=/; Max-Age=3600')] if created else []
                self.respond(200, data, headers=headers)
            elif path == '/api/live/scenario':
                self.respond(200, self.server.live.describe(session))
            elif path == '/api/actions':
                self.respond(200, self.server.actions.describe(session))
            elif path == '/api/intelligence':
                self.respond(200, self.server.intelligence.describe(session))
            else:
                self.respond(200, self.server.connections.describe(session))
        except (APIError, ActionError) as error:
            self.connection_error(error)
        except Exception:
            self.connection_error(APIError('oauth_failed'))

    def oauth_callback(self):
        headers = [('Location', '/')]
        try:
            if self.headers.get('Host') != self.server.connections.origin.removeprefix('http://'):
                raise APIError('session_invalid')
            session, _ = self.server.connections.session(self.session_cookie())
            try:
                query = urlsplit(self.path).query
                if len(query) > MAX_BODY:
                    raise ValueError()
                pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=20)
                if len({key for key, _ in pairs}) != len(pairs):
                    raise ValueError()
                params = dict(pairs)
                if self.server.actions.handles_callback(session, params):
                    self.server.actions.callback(session, params)
                else:
                    self.server.connections.callback(session, params)
            except (ValueError, UnicodeError):
                self.server.connections.callback_error(session)
        except (APIError, ActionError) as error:
            if error.code.startswith('session_'):
                headers.extend(self.clear_session_header())
        except Exception:
            # Callback query, code and provider errors are never reflected or logged.
            pass
        self.respond(303, b'', 'text/plain; charset=utf-8', headers=headers)

    def read_json(self):
        if self.headers.get_content_type() != 'application/json':
            self.error(415, 'content_type', 'A JSON request body is required.')
            return None
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if length < 0 or self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            self.error(400, 'invalid_length', 'The request size is invalid.')
            return None
        if length > MAX_BODY:
            self.error(413, 'body_too_large', 'The request exceeds 64 KB.')
            return None
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        try:
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            body = json.loads(raw, object_pairs_hook=unique)
            json.dumps(body, allow_nan=False)
            if type(body) is not dict:
                raise ValueError()
            return body
        except (ValueError, UnicodeDecodeError, TimeoutError, RecursionError):
            self.error(400, 'invalid_request', 'The JSON body for this action is invalid.')
            return None

    def auth_post(self, path):
        if not self.canonical_auth_origin(post=True):
            return
        try:
            service = self.server.connections
            session, _ = service.session(self.session_cookie())
            if len(self.headers.get_all('X-Aeon-CSRF', [])) != 1:
                raise APIError('csrf_invalid')
            service.verify_csrf(session, self.headers.get('X-Aeon-CSRF'))
            body = self.read_json()
            if body is None:
                return
            if path.startswith('/api/actions/'):
                action = path.rsplit('/', 1)[1]
                result = getattr(self.server.actions, 'begin' if action == 'connect' else action)(session, body)
            elif path.startswith('/api/live/'):
                result = getattr(self.server.live, path.rsplit('/', 1)[1])(session, body)
            elif path.startswith('/api/intelligence/'):
                result = getattr(self.server.intelligence, path.rsplit('/', 1)[1])(session, body)
            elif path.startswith('/api/oauth/'):
                _, _, _, provider, action = path.split('/')
                if body:
                    raise APIError('invalid_request')
                result = service.begin(session, provider) if action == 'begin' else service.forget(session, provider)
            else:
                provider = 'google_calendar' if path == '/api/calendar/read' else 'gmail'
                result = service.read(session, provider, body)
            self.respond(200, result)
        except (APIError, ActionError) as error:
            self.connection_error(error)
        except Exception:
            self.connection_error(APIError('oauth_failed'))

    def do_GET(self):
        if not self.allowed_host():
            return
        path = urlsplit(self.path).path
        if path == '/' and '?' in self.path:
            self.oauth_callback()
            return
        if path in ('/api/session', '/api/connections', '/api/live/scenario', '/api/intelligence', '/api/actions'):
            self.auth_get(path)
            return
        if path in ASSETS:
            filename, mime = ASSETS[path]
            try:
                self.respond(200, (WEB / filename).read_bytes(), mime)
            except FileNotFoundError:
                self.error(404, 'asset_missing', 'This application file is missing.')
            return
        if path == '/api/demo':
            self.respond(200, {'scenario': scenario_for('normal')[0]})
        elif path == '/api/status':
            try:
                self.server.engine_loader()
                available = True
            except (EngineUnavailable, ImportError, AttributeError):
                available = False
            try:
                self.server.planner_loader()
                planner_available = True
            except (PlannerUnavailable, ImportError, AttributeError):
                planner_available = False
            self.respond(200, {'mode': 'synthetic', 'engine_available': available,
                              'planner_available': planner_available,
                              'integrations': [{'id': id, 'name': name, 'status': 'not_connected'}
                                               for id, name in [('google_calendar', 'Google Calendar'), ('gmail', 'Gmail'), ('google_routes', 'Google Routes')]]})
        else:
            self.error(404, 'not_found', 'This address does not exist.')

    def do_POST(self):
        if not self.allowed_host():
            return
        path = urlsplit(self.path).path
        auth_paths = {'/api/calendar/read', '/api/gmail/read'} | {'/api/oauth/' + provider + '/' + action for provider in PROVIDERS for action in ('begin', 'forget')}
        auth_paths |= {'/api/actions/' + action for action in ('connect', 'forget', 'execute', 'undo')}
        auth_paths |= {'/api/live/travel', '/api/live/simulate', '/api/live/reset', '/api/live/plans',
                       '/api/intelligence/extract', '/api/intelligence/confirm', '/api/intelligence/reset'}
        if path in auth_paths:
            self.auth_post(path)
            return
        if path not in ('/api/simulate', '/api/plans'):
            self.error(404, 'not_found', 'This action does not exist.')
            return
        origin = self.headers.get('Origin')
        if self.headers.get('X-Aeon-Request') != 'demo-v1' or (origin and origin != 'http://' + self.headers.get('Host', '')):
            self.error(403, 'origin_rejected', 'Restart this action from the AEON app.')
            return
        body = self.read_json()
        if body is None:
            return
        if set(body) - {'traffic'} or body.get('traffic', 'normal') not in ('normal', 'incident'):
            self.error(400, 'invalid_request', 'Choose normal traffic or the demo incident.')
            return
        is_planning = path == '/api/plans'
        component = 'planner' if is_planning else 'engine'
        try:
            operation = self.server.planner_loader() if is_planning else self.server.engine_loader()
        except (EngineUnavailable, PlannerUnavailable):
            self.error(503, component + '_unavailable',
                       'The alternative planner is not available yet. Try again after it is installed.' if is_planning else
                       'The simulation engine is not installed yet. Update to its merged version, then run the analysis again.')
            return
        except Exception:
            self.error(500, component + '_failed', 'The computation module could not be loaded. Check its installation.')
            return
        traffic = body.get('traffic', 'normal')
        start = time.perf_counter()
        try:
            scenario, affected = scenario_for(traffic)
            if is_planning:
                scenario, context = planning_input(scenario)
                result = operation(copy.deepcopy(scenario), copy.deepcopy(context))
            else:
                result = operation(copy.deepcopy(scenario))
            response = {'scenario': scenario, 'planning' if is_planning else 'simulation': result,
                        'metrics': {'elapsed_ms': round((time.perf_counter() - start) * 1000, 2)},
                        'intervention': {'kind': traffic, 'synthetic': True, 'affected_edges': affected}}
            # Validate serialisation before sending success headers.
            json.dumps(response, allow_nan=False)
        except Exception:
            self.error(500, component + '_failed',
                       'Alternative planning failed. No fallback plan has been created.' if is_planning else
                       'The simulation failed. No fallback result has been created.')
            return
        self.respond(200, response)


class LocalServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # No raw exception, request query or private source body in server logs.
        pass


def create_server(port=8787, engine_loader=load_engine, planner_loader=load_planner, *, configuration=None,
                  assembler_loader=load_assembler, routes_factory=create_google_routes,
                  model=None, live_planner_loader=load_live_planner, open_routes_factory=create_open_routes,
                  action_oauth_factory=GoogleOAuth, writer_factory=GoogleCalendarWriter, daily_route_budget=None, **connection_options):
    server = LocalServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.engine_loader = engine_loader
    server.planner_loader = planner_loader
    # Deliberately no implicit environment/file loading: tests and embedders are offline by default.
    server.connections = ConnectionService(server.server_port, configuration=configuration, **connection_options)
    server.live = LiveService(server.connections, engine_loader=engine_loader, assembler_loader=assembler_loader,
                              routes_factory=routes_factory, clock=connection_options.get('clock'), planner_loader=live_planner_loader,
                              open_routes_factory=open_routes_factory, daily_route_budget=daily_route_budget)
    server.intelligence = IntelligenceService(server.connections, server.live, configuration=configuration, model=model)
    server.actions = ActionService(server.connections, server.live, configuration=configuration,
                                   oauth_factory=action_oauth_factory, writer_factory=writer_factory)
    server.connections.action_discard = server.actions.discard
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    configuration = load_configuration(env_path=ROOT / '.env')
    server = create_server(args.port, configuration=configuration,
                           daily_route_budget=DailyRouteBudget(ROOT / ".aeon" / "route-budget.sqlite3"))
    print(f'ÆON — http://127.0.0.1:{server.server_port} — synthetic demo', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
