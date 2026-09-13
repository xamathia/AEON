"""Chosen Gmail excerpts become constraints only after explicit confirmation."""
import copy
import hashlib
import secrets

from .connections import APIError

LIMIT = 10
DEFAULT_MODEL = 'claude-haiku-4-5-20251001'


def load_extractor():
    from packages.aeon_intelligence import DeadlineExtractor
    return DeadlineExtractor


class _MeteredModel:
    def __init__(self, service, session, state):
        self.service, self.session = service, session
        self.state = state

    def generate(self, request, *, max_output_tokens):
        session = self.session
        with session.lock:
            self.service.connections._ensure_active(session)
            if (session.intelligence_state is not self.state
                    or session.sources['gmail'].data is not self.state['gmail']
                    or session.sources['google_calendar'].canonical_data is not self.state['calendar']
                    or any(source.reading for source in session.sources.values())):
                raise APIError('revision_conflict')
            meter = session.intelligence_meter
            if meter['calls'] >= LIMIT:
                raise APIError('intelligence_budget_exhausted')
            meter['calls'] += 1
        usage = None
        try:
            result = self.service.model.generate(request, max_output_tokens=max_output_tokens)
            if type(result) is dict and type(result.get('usage')) is dict:
                candidate = result['usage']
                if all(type(candidate.get(k)) is int and candidate[k] >= 0 for k in ('input_tokens', 'output_tokens')):
                    usage = candidate
            return result
        finally:
            with session.lock:
                for key in ('input_tokens', 'output_tokens'):
                    meter[key] = meter[key] + usage[key] if usage is not None and meter[key] is not None else None


class IntelligenceService:
    def __init__(self, connections, live, *, configuration=None, model=None, extractor_loader=load_extractor):
        self.connections, self.live = connections, live
        self.model, self.model_name = model, DEFAULT_MODEL
        self.extractor_loader = extractor_loader
        if configuration is not None and configuration.valid:
            self.model_name = configuration.values.get('AEON_ANTHROPIC_MODEL') or DEFAULT_MODEL
            key = configuration.values.get('AEON_ANTHROPIC_API_KEY')
            if key and model is None:
                try:
                    from .model import AnthropicModel
                    self.model = AnthropicModel(key, model=self.model_name)
                except Exception:
                    self.model = None

    def _state(self, session):
        self.connections._ensure_active(session)
        live = self.live._state(session)
        gmail = session.sources['gmail'].data
        calendar = session.sources['google_calendar'].canonical_data
        state = session.intelligence_state
        if (state is None or state['gmail'] is not gmail or state['calendar'] is not calendar):
            state = {'gmail': gmail, 'calendar': calendar, 'gmail_revision': secrets.token_urlsafe(32),
                     'extractor': None, 'result': None}
            session.intelligence_state = state
        return live, state

    def describe(self, session):
        with session.lock:
            live, state = self._state(session)
            messages = state['gmail']['messages'] if state['gmail'] else []
            return {'revision': live['revision'] if live else None, 'gmail_revision': state['gmail_revision'],
                    'provider': 'Anthropic', 'model': self.model_name, 'configured': self.model is not None,
                    'messages': [{key: copy.deepcopy(message[key]) for key in ('id', 'subject', 'excerpt', 'source')}
                                 for message in messages], 'result': copy.deepcopy(state['result']),
                    'metrics': {**session.intelligence_meter, 'limit': LIMIT,
                                'remaining_calls': max(0, LIMIT - session.intelligence_meter['calls'])},
                    'constraints': copy.deepcopy(live.get('constraints', [])) if live else []}

    def _validate(self, session, body):
        live = self.live._validate_revision(session, body)
        _, state = self._state(session)
        if body.get('gmail_revision') != state['gmail_revision']:
            raise APIError('revision_conflict')
        if session.sources['gmail'].reading:
            raise APIError('live_busy')
        return live, state

    def extract(self, session, body):
        if type(body) is not dict or set(body) != {'revision', 'gmail_revision', 'message_id', 'event_ids', 'consent'} or body['consent'] is not True:
            raise APIError('invalid_request')
        ids = body['event_ids']
        if (type(ids) is not list or not 1 <= len(ids) <= 20 or any(type(v) is not str for v in ids)
                or len(set(ids)) != len(ids) or type(body['message_id']) is not str):
            raise APIError('invalid_request')
        with session.lock:
            live, state = self._validate(session, body)
            if self.model is None:
                raise APIError('intelligence_unavailable')
            if not state['gmail']:
                raise APIError('gmail_not_read')
            message = next((m for m in state['gmail']['messages'] if m['id'] == body['message_id']), None)
            events = {e['id']: e for e in state['calendar']['events']}
            if message is None or any(identifier not in events for identifier in ids):
                raise APIError('invalid_request')
            if session.intelligence_meter['calls'] >= LIMIT:
                raise APIError('intelligence_budget_exhausted')
            if state['extractor'] is None:
                try:
                    state['extractor'] = self.extractor_loader()(_MeteredModel(self, session, state), max_calls=LIMIT)
                except Exception:
                    raise APIError('intelligence_unavailable') from None
            args = {'message': {key: copy.deepcopy(message[key]) for key in ('id', 'excerpt', 'source')},
                    'events': [copy.deepcopy(events[i]) for i in ids],
                    'horizon': copy.deepcopy(session.sources['google_calendar'].data['horizon']),
                    'timezone': session.sources['google_calendar'].data['timezone']}
            self.live._claim(session, body)
        try:
            try:
                result = state['extractor'].extract(**args)
            except ValueError:
                raise APIError('invalid_request') from None
            except Exception:
                raise APIError('intelligence_unavailable') from None
            with session.lock:
                self.live._guard(session, live)
                if session.intelligence_state is not state or session.sources['gmail'].reading:
                    raise APIError('revision_conflict')
                result = {key: copy.deepcopy(value) for key, value in result.items() if key != 'metrics'}
                if result.get('proposal') is not None:
                    result['proposal']['proposal_id'] = secrets.token_urlsafe(32)
                state['result'] = result
            return self.describe(session)
        finally:
            with session.lock:
                session.live_busy = False

    def confirm(self, session, body):
        if type(body) is not dict or set(body) != {'revision', 'gmail_revision', 'proposal_id'}:
            raise APIError('invalid_request')
        with session.lock:
            live, state = self._validate(session, body)
            self.live._claim(session, body)
            try:
                proposal = (state['result'] or {}).get('proposal')
                if not proposal or body['proposal_id'] != proposal['proposal_id']:
                    raise APIError('revision_conflict')
                digest = hashlib.sha256((proposal['source']['reference'] + ':' + proposal['event_id']).encode()).hexdigest()
                constraint = {'id': 'gmail-deadline:' + digest, 'type': 'arrival_deadline',
                              **{key: copy.deepcopy(proposal[key]) for key in ('event_id', 'deadline', 'source')}}
                live['constraints'] = [c for c in live.get('constraints', []) if c['id'] != constraint['id']] + [constraint]
                live['result'], live['planning'] = None, None
                state['result'] = None
            finally:
                session.live_busy = False
        return self.describe(session)

    def reset(self, session, body):
        if type(body) is not dict or set(body) != {'revision'}:
            raise APIError('invalid_request')
        with session.lock:
            live = self.live._claim(session, body)
            try:
                live['constraints'], live['result'], live['planning'] = [], None, None
                if session.intelligence_state is not None:
                    session.intelligence_state['result'] = None
            finally:
                session.live_busy = False
        return self.describe(session)
