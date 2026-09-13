"""Explicit session-scoped Calendar writes; no HTTP or environment access."""
import copy
import hashlib
import hmac
import json
import threading
from urllib.parse import parse_qs, urlsplit

from packages.aeon_actions import AuditStore, PlanExecutor, evaluate_plan, plan_hash
from packages.aeon_oauth import GoogleOAuth
from .configuration import Configuration

PROVIDER = 'google_calendar_write'
LIMIT = 10
_ERRORS = {'invalid_request':400,'actions_unavailable':503,'not_connected':409,'action_busy':409,
           'stale_plan':409,'invalid_plan':400,'budget_exhausted':429,'unknown_receipt':404,
           'action_failed':502,'session_expired':401,'operation_cancelled':409}


class ActionError(Exception):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'action_failed'
        self.status = _ERRORS[self.code]
        super().__init__('Calendar action unavailable or refused (' + self.code + ').')


def _writer(token, **kwargs):
    from packages.aeon_connectors.calendar_write import GoogleCalendarWriter
    return GoogleCalendarWriter(token, **kwargs)


def _body(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ActionError('invalid_request')


def _identifier(value):
    if type(value) is not str or not 1 <= len(value) <= 1024 or not value.strip():
        raise ActionError('invalid_request')


def _projection(receipt):
    actions = []
    for action in receipt['actions']:
        times = lambda value: ({key: value[key] for key in ('planned_start','planned_end')} if value else None)
        actions.append({'event_id':action['event_id'], 'before':times(action.get('before')), 'after':times(action.get('after'))})
    return {'id':receipt['id'],'status':receipt['status'],'reason_codes':list(receipt['reason_codes']),'actions':actions}


class _GuardedWriter:
    def __init__(self, writer, guard): self.writer, self.guard = writer, guard
    def get_event(self, *args):
        self.guard()
        return self.writer.get_event(*args)
    def patch_times(self, *args, **kwargs):
        self.guard()
        return self.writer.patch_times(*args, **kwargs)


class ActionService:
    def __init__(self, connections, live, configuration=None, oauth_factory=GoogleOAuth, writer_factory=_writer):
        self.connections, self.live = connections, live
        self._writer_factory = writer_factory
        self._oauth = None
        config = configuration if configuration is not None else Configuration({})
        values = config.values
        self._configuration = 'missing' if config.valid else 'invalid'
        if config.valid and values.get('AEON_GOOGLE_CLIENT_ID') and values.get('AEON_GOOGLE_REDIRECT_URI'):
            try:
                redirect = values['AEON_GOOGLE_REDIRECT_URI']
                if redirect not in (connections.origin, connections.origin + '/'):
                    raise ValueError()
                self._oauth = oauth_factory(values['AEON_GOOGLE_CLIENT_ID'], redirect, provider=PROVIDER,
                                            client_secret=values.get('AEON_GOOGLE_CLIENT_SECRET') or None)
                self._configuration = 'ready'
            except Exception:
                self._configuration = 'invalid'

    def _active(self, session):
        try: self.connections._ensure_active(session)
        except Exception: raise ActionError('session_expired') from None

    def _state(self, session):
        self._active(session)
        if getattr(session, 'action_state', None) is None:
            session.action_state = {'generation':0,'pending':None,'busy':False,'auth_busy':False,
                'auth_lock':threading.RLock(),'executions':0,'audit':AuditStore(':memory:'), 'receipts':{},'keys':{}}
        return session.action_state

    def _guard(self, session, state, generation, live_state=None):
        with session.lock:
            self._active(session)
            if session.action_state is not state or state['generation'] != generation:
                raise ActionError('operation_cancelled')
            if any(source.reading for source in session.sources.values()):
                raise ActionError('action_busy')
            if live_state is not None and session.live_state is not live_state:
                raise ActionError('stale_plan')

    def _oauth_required(self):
        if self._oauth is None: raise ActionError('actions_unavailable')
        return self._oauth

    def _connected(self, session):
        oauth = self._oauth_required()
        try: connected = oauth.status(session.id)['connected'] is True
        except Exception: raise ActionError('action_failed') from None
        if not connected: raise ActionError('not_connected')

    def describe(self, session):
        with session.lock:
            state = self._state(session)
            generation = state['generation']
        try: connected = self._oauth.status(session.id)['connected'] is True if self._oauth else False
        except Exception: connected = False
        with session.lock:
            self._active(session)
            return {'provider':PROVIDER,'configuration':self._configuration,
                'connected':connected and state['generation']==generation,'pending':state['pending'] is not None,
                'busy':state['busy'] or state['auth_busy'],
                'receipts':[_projection(receipt) for receipt in state['receipts'].values()],
                'budget':{'executions':state['executions'],'limit':LIMIT,'remaining':max(0,LIMIT-state['executions'])},
                'audit':'memory_session_only'}

    def begin(self, session, body):
        _body(body, ())
        oauth = self._oauth_required()
        with session.lock: state = self._state(session)
        with state['auth_lock']:
            with session.lock:
                self._active(session)
                if state['busy'] or state['auth_busy']: raise ActionError('action_busy')
                state['generation'] += 1
                generation = state['generation']
                state['pending'] = None
                state['auth_busy'] = True
                session.oauth_result = None
            try:
                result = oauth.begin(session.id)
                pending = parse_qs(urlsplit(result['authorization_url']).query)['state'][0]
                self._guard(session,state,generation)
                with session.lock: state['pending'] = pending
                return result
            except ActionError: raise
            except Exception: raise ActionError('action_failed') from None
            finally:
                with session.lock: state['auth_busy'] = False

    def handles_callback(self, session, params):
        with session.lock:
            state = self._state(session)
            value = params.get('state') if type(params) is dict else None
            return type(value) is str and value.isascii() and len(value)<=256 and state['pending'] is not None and hmac.compare_digest(state['pending'],value)

    def callback(self, session, params):
        with session.lock: state = self._state(session)
        with state['auth_lock']:
            if not self.handles_callback(session,params): raise ActionError('invalid_request')
            with session.lock:
                generation = state['generation']; pending = state['pending']; state['pending'] = None
                state['auth_busy'] = True
            success = False
            try:
                self._oauth_required().complete(session.id,state=pending,code=params.get('code'),error=params.get('error'))
                success = True
            except Exception: pass
            finally:
                with session.lock: state['auth_busy'] = False
            self._guard(session,state,generation)
            with session.lock:
                session.oauth_result = {'provider':PROVIDER,'status':'connected' if success else 'error',
                    'code':'connected' if success else 'action_failed',
                    'message':'Calendar write consent granted.' if success else 'Calendar write consent denied or unavailable.'}
        return self.describe(session)

    def discard(self, session):
        # Called on expiration too: invalidate immediately before any OAuth lock wait.
        with session.lock:
            state = getattr(session,'action_state',None)
            if state is not None:
                state['generation'] += 1; state['pending'] = None
            session.live_state = None
            session.intelligence_state = None
        if self._oauth:
            try: self._oauth.forget(session.id)
            except Exception: pass

    def forget(self, session, body):
        _body(body, ())
        with session.lock: self._state(session)
        self.discard(session)
        return self.describe(session)

    @staticmethod
    def _plan(session, body):
        live = session.live_state
        if not live or live.get('revision') != body['revision']: raise ActionError('stale_plan')
        plans = (live.get('planning') or {}).get('plans', [])
        chosen = next((plan for plan in plans if plan.get('id') == body['plan_id']), None)
        if chosen is None: raise ActionError('stale_plan')
        plan = copy.deepcopy(chosen)
        operations = plan.get('operations')
        if type(operations) is not list or not 1 <= len(operations) <= 5: raise ActionError('invalid_plan')
        canonical = session.sources['google_calendar'].canonical_data
        if not canonical: raise ActionError('stale_plan')
        events = {event['id']:event for event in canonical['events']}
        snapshots = {}
        for operation in operations:
            if type(operation) is not dict: raise ActionError('invalid_plan')
            identifier = operation.get('event_id')
            if type(identifier) is not str or identifier not in events or identifier in snapshots: raise ActionError('invalid_plan')
            event = events[identifier]
            if (event.get('calendar_id') != 'primary' or event.get('private') is not True or event.get('organizer_self') is not True
                or type(event.get('attendees_count')) is not int or event['attendees_count'] != 0 or event.get('attendees_omitted') is not False
                or event.get('recurring_event_id') or event.get('recurrence') or event.get('originalStartTime')):
                raise ActionError('invalid_plan')
            snapshots[identifier] = {key:copy.deepcopy(event.get(key)) for key in ('calendar_id','etag','planned_start','planned_end','private','organizer_self','attendees_count')}
            snapshots[identifier].update(external_event_id=identifier,classification='flexible')
        policy = {'autonomy_level':2,'allowed_calendar_ids':['primary'],'allowed_event_ids':list(snapshots),
                  'approved_plan_hash':plan_hash(plan),'kill_switch':False}
        if not evaluate_plan(plan,snapshots,policy)['allowed']: raise ActionError('invalid_plan')
        return live, plan, snapshots, policy

    def _invalidate_after_mutation(self, session):
        session.live_state = None
        session.intelligence_state = None
        invalidate = getattr(self.connections, 'invalidate_calendar_after_action', None)
        if invalidate is not None:
            invalidate(session)

    def execute(self, session, body):
        _body(body, ('revision','plan_id','approved'))
        if body['approved'] is not True: raise ActionError('invalid_request')
        _identifier(body['revision']); _identifier(body['plan_id'])
        key = hashlib.sha256(json.dumps([session.id,body['revision'],body['plan_id']]).encode()).hexdigest()
        with session.lock:
            state = self._state(session)
            previous = state['keys'].get(key)
            if previous:
                if previous['receipt'] is None: raise ActionError('action_busy' if state['busy'] else 'action_failed')
                # A known id never authorizes changed operations on the same live revision.
                if session.live_state and session.live_state.get('revision')==body['revision']:
                    _, plan, _, _ = self._plan(session,body)
                    if plan_hash(plan) != previous['hash']: raise ActionError('stale_plan')
        if previous:
            return self.describe(session)
        self._connected(session)
        with session.lock:
            state = self._state(session)
            if state['busy'] or state['auth_busy'] or session.live_busy or any(source.reading for source in session.sources.values()): raise ActionError('action_busy')
            if key in state['keys']: raise ActionError('action_busy')
            live, plan, snapshots, policy = self._plan(session,body)
            if state['executions'] >= LIMIT: raise ActionError('budget_exhausted')
            generation = state['generation']; state['busy']=True; session.live_busy=True
            state['executions']+=1; state['keys'][key]={'receipt':None,'hash':plan_hash(plan)}
        entered = False
        try:
            try: token = self._oauth_required().access_token(session.id)
            except Exception: raise ActionError('not_connected') from None
            self._guard(session,state,generation,live)
            guard = lambda:self._guard(session,state,generation,live)
            writer = self._writer_factory(token,authorized_event_ids=list(snapshots),before_mutation=guard)
            executor = PlanExecutor(_GuardedWriter(writer,guard),state['audit'])
            entered = True
            receipt = executor.execute(plan,current_events=snapshots,policy=policy,idempotency_key=key)
            with session.lock:
                state['receipts'][receipt['id']]=receipt; state['keys'][key]['receipt']=receipt['id']
                if receipt['status'] in ('applied','unknown','partial','compensated'):
                    self._invalidate_after_mutation(session)
        except ActionError: raise
        except Exception: raise ActionError('action_failed') from None
        finally:
            with session.lock:
                if not entered: state['keys'].pop(key,None)
                state['busy']=False; session.live_busy=False
        return self.describe(session)

    def undo(self, session, body):
        _body(body, ('receipt_id','approved'))
        if body['approved'] is not True: raise ActionError('invalid_request')
        _identifier(body['receipt_id'])
        with session.lock:
            state = self._state(session)
            receipt = state['receipts'].get(body['receipt_id'])
            if receipt is None: raise ActionError('unknown_receipt')
        self._connected(session)
        with session.lock:
            self._active(session)
            if state['busy'] or state['auth_busy'] or session.live_busy or any(source.reading for source in session.sources.values()): raise ActionError('action_busy')
            finished = receipt['status'] != 'applied'
        if finished:
            return self.describe(session)
        with session.lock:
            self._active(session)
            if state['busy'] or state['auth_busy'] or session.live_busy or any(source.reading for source in session.sources.values()): raise ActionError('action_busy')
            generation=state['generation'];state['busy']=True;session.live_busy=True
            actions=receipt['actions']
            identifiers=[action['external_event_id'] for action in actions]
            policy={'autonomy_level':2,'allowed_calendar_ids':['primary'],'allowed_event_ids':[action['event_id'] for action in actions],
                    'approved_plan_hash':receipt['plan_hash'],'kill_switch':False}
        try:
            try: token=self._oauth_required().access_token(session.id)
            except Exception: raise ActionError('not_connected') from None
            self._guard(session,state,generation)
            guard=lambda:self._guard(session,state,generation)
            writer=self._writer_factory(token,authorized_event_ids=identifiers,before_mutation=guard)
            executor=PlanExecutor(_GuardedWriter(writer,guard),state['audit'])
            result=executor.undo(receipt['id'],policy=policy)
            with session.lock:
                state['receipts'][result['id']]=result
                if result['status'] in ('undone','unknown','partial'):
                    self._invalidate_after_mutation(session)
        except ActionError: raise
        except Exception: raise ActionError('action_failed') from None
        finally:
            with session.lock:state['busy']=False;session.live_busy=False
        return self.describe(session)
