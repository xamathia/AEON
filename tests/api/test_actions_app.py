"""Session actions use genuine PlanExecutor and a memory Calendar port."""
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from apps.api.actions import ActionService, ActionError
from apps.api.configuration import Configuration
from packages.aeon_actions import CalendarConflict, CalendarUnknownOutcome

START='2026-09-14T15:00:00Z'
END='2026-09-14T16:00:00Z'
AFTER='2026-09-14T13:00:00Z'
AFTER_END='2026-09-14T14:00:00Z'
PLAN={'id':'p','operations':[{'event_id':'e','before':{'planned_start':START,'planned_end':END},'after':{'planned_start':AFTER,'planned_end':AFTER_END}}]}
EVENT={'id':'e','calendar_id':'primary','etag':'PRIVATE_ETAG','planned_start':START,'planned_end':END,'private':True,'organizer_self':True,'attendees_count':0,'attendees_omitted':False,'recurring_event_id':None}

class Connections:
    origin='http://127.0.0.1:8787'
    def _ensure_active(self, session):
        if not session.active: raise ValueError()

class OAuth:
    def __init__(self,*args,**kwargs): self.connected=False; self.tokens=0; self.release=None; self.started=None
    def status(self, session): return {'connected':self.connected}
    def begin(self,session): self.connected=False; return {'authorization_url':'https://accounts.google.com/o/oauth2/v2/auth?state=fixture'}
    def complete(self,session,**kwargs):
        if kwargs.get('error'): raise ValueError('PRIVATE')
        self.connected=True
    def forget(self,session): self.connected=False
    def access_token(self,session):
        self.tokens+=1
        if self.started: self.started.set(); self.release.wait(3)
        if not self.connected: raise ValueError('PRIVATE_TOKEN')
        return 'fixture-token'

class Writer:
    def __init__(self, token, *, authorized_event_ids, before_mutation=None):
        self.ids=authorized_event_ids
        self.before_mutation=before_mutation
    def get_event(self,calendar,event): return copy.deepcopy(self.remote[event])
    def patch_times(self,calendar,event,*,planned_start,planned_end,if_match,send_updates='none'):
        if self.before_mutation: self.before_mutation()
        self.calls.append(event)
        if self.failure: raise self.failure
        remote=self.remote[event]
        if remote['etag'] != if_match: raise CalendarConflict()
        remote.update(planned_start=planned_start,planned_end=planned_end,etag=remote['etag']+'next')
        return copy.deepcopy(remote)

class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.session=SimpleNamespace(id='fixture-session',active=True,lock=threading.RLock(),live_busy=False,live_state={'revision':'r','planning':{'plans':[copy.deepcopy(PLAN)]}},sources={'google_calendar':SimpleNamespace(reading=False,canonical_data={'events':[copy.deepcopy(EVENT)]})},oauth_result=None)
        Writer.remote={'e':{**EVENT,'external_event_id':'e','classification':'flexible'}}; Writer.calls=[]; Writer.failure=None
        self.service=ActionService(Connections(),None,Configuration({'AEON_GOOGLE_CLIENT_ID':'fixture','AEON_GOOGLE_REDIRECT_URI':'http://127.0.0.1:8787/'}),oauth_factory=OAuth,writer_factory=Writer)
        self.body={'revision':'r','plan_id':'p','approved':True}
    def connect(self):
        self.service.begin(self.session,{})
        self.assertTrue(self.service.handles_callback(self.session,{'state':'fixture'}))
        self.service.callback(self.session,{'state':'fixture','code':'fixture'})
    def assert_code(self,code,fn):
        with self.assertRaises(ActionError) as found: fn()
        self.assertEqual(found.exception.code,code)
    def test_apply_undo_and_duplicate_after_live_invalidated(self):
        self.connect(); state=self.service.execute(self.session,self.body)
        receipt=state['receipts'][0]; self.assertEqual(receipt['status'],'applied')
        self.assertIsNone(self.session.live_state)
        self.assertEqual(self.service.execute(self.session,self.body)['receipts'][0]['id'],receipt['id'])
        self.assertEqual(Writer.calls,['e'])
        final=self.service.undo(self.session,{'receipt_id':receipt['id'],'approved':True})
        self.assertEqual(final['receipts'][0]['status'],'undone'); self.assertEqual(Writer.remote['e']['planned_start'],START)
        self.assertNotIn('PRIVATE_ETAG',json.dumps(final)); self.assertNotIn('fixture-token',json.dumps(final))
    def test_absent_consent_unknown_plan_and_forged_body(self):
        self.assert_code('not_connected',lambda:self.service.execute(self.session,self.body))
        self.connect()
        self.assert_code('invalid_request',lambda:self.service.execute(self.session,{**self.body,'operations':[]}))
        self.assert_code('stale_plan',lambda:self.service.execute(self.session,{**self.body,'revision':'old'}))
        self.assert_code('stale_plan',lambda:self.service.execute(self.session,{**self.body,'plan_id':'forged'}))
        self.assertEqual(Writer.calls,[])
    def test_invitation_and_recurrence_rejected(self):
        self.connect()
        for change in ({'private':False},{'organizer_self':False},{'attendees_count':1},{'attendees_omitted':True},{'recurring_event_id':'series'}):
            self.session.sources['google_calendar'].canonical_data={'events':[{**EVENT,**change}]}
            self.assert_code('invalid_plan',lambda:self.service.execute(self.session,self.body))
        self.assertEqual(Writer.calls,[])
    def test_unknown_never_replays(self):
        self.connect(); Writer.failure=CalendarUnknownOutcome()
        state=self.service.execute(self.session,self.body)
        self.assertEqual(state['receipts'][0]['status'],'unknown')
        self.service.execute(self.session,self.body); self.assertEqual(Writer.calls,['e'])
    def test_external_conflict_and_receipt_ownership(self):
        self.connect(); Writer.remote['e']['etag']='changed'
        state=self.service.execute(self.session,self.body)
        self.assertEqual(state['receipts'][0]['status'],'conflict'); self.assertEqual(Writer.calls,[])
        self.assert_code('unknown_receipt',lambda:self.service.undo(self.session,{'receipt_id':'foreign','approved':True}))
    def test_discard_during_token_refresh_prevents_write(self):
        self.connect(); oauth=self.service._oauth; oauth.started=threading.Event(); oauth.release=threading.Event()
        errors=[]
        def run():
            try:self.service.execute(self.session,self.body)
            except ActionError as error:errors.append(error.code)
        worker=threading.Thread(target=run);worker.start();self.assertTrue(oauth.started.wait(2))
        self.service.discard(self.session);oauth.release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(Writer.calls,[]);self.assertTrue(errors)
    def test_read_busy_and_double_action(self):
        self.connect();self.session.sources['google_calendar'].reading=True
        self.assert_code('action_busy',lambda:self.service.execute(self.session,self.body))
        self.assertEqual(Writer.calls,[])
    def test_denied_callback_and_forget_preserve_budget_receipts(self):
        self.service.begin(self.session,{})
        self.service.callback(self.session,{'state':'fixture','error':'access_denied'})
        self.assertFalse(self.service.describe(self.session)['connected'])
        self.connect();self.service.execute(self.session,self.body)
        final=self.service.forget(self.session,{})
        self.assertFalse(final['connected']);self.assertEqual(final['budget']['executions'],1);self.assertEqual(len(final['receipts']),1)

    def test_budget_is_session_bound_and_undo_does_not_charge(self):
        self.connect()
        for index in range(10):
            Writer.remote['e']={**EVENT,'external_event_id':'e','classification':'flexible'}
            revision='r'+str(index)
            self.session.live_state={'revision':revision,'planning':{'plans':[copy.deepcopy(PLAN)]}}
            state=self.service.execute(self.session,{'revision':revision,'plan_id':'p','approved':True})
        self.assertEqual(state['budget'],{'executions':10,'limit':10,'remaining':0})
        self.assertEqual(len(state['receipts']),10)
        self.session.live_state={'revision':'last','planning':{'plans':[copy.deepcopy(PLAN)]}}
        self.assert_code('budget_exhausted',lambda:self.service.execute(self.session,{'revision':'last','plan_id':'p','approved':True}))
        self.assertEqual(len(Writer.calls),10)

    def test_inflight_execution_blocks_a_second_action(self):
        self.connect();oauth=self.service._oauth;oauth.started=threading.Event();oauth.release=threading.Event()
        errors=[]
        def run():
            try:self.service.execute(self.session,self.body)
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=run);worker.start();self.assertTrue(oauth.started.wait(2))
        self.assert_code('action_busy',lambda:self.service.execute(self.session,self.body))
        oauth.release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[]);self.assertEqual(Writer.calls,['e'])

    def test_cached_receipt_does_not_falsely_report_disconnected(self):
        self.connect();self.service.execute(self.session,self.body)
        self.assertTrue(self.service.execute(self.session,self.body)['connected'])

    def test_ports_never_run_under_session_lock(self):
        self.connect();oauth=self.service._oauth
        for name in ('status','access_token'):
            original=getattr(oauth,name)
            def unlocked(*args, original=original, **kwargs):
                self.assertFalse(self.session.lock._is_owned())
                return original(*args,**kwargs)
            setattr(oauth,name,unlocked)
        self.service.execute(self.session,self.body)
        self.service.execute(self.session,self.body)

    def test_gmail_read_in_progress_blocks_execution(self):
        self.connect()
        self.session.sources['gmail']=SimpleNamespace(reading=True)
        self.assert_code('action_busy',lambda:self.service.execute(self.session,self.body))
        self.assertEqual(Writer.calls,[])

    def test_forget_during_real_writers_internal_preflight_never_patches(self):
        from packages.aeon_connectors.calendar_write import GoogleCalendarWriter
        self.connect()
        service=self.service
        session=self.session
        methods=[]
        class Port:
            def request(self,method,url,**kwargs):
                methods.append(method)
                if len(methods)==2:
                    service.discard(session)
                raw={'id':'e','etag':'PRIVATE_ETAG','status':'confirmed','organizer':{'self':True},'start':{'dateTime':START},'end':{'dateTime':END}}
                return 200,json.dumps(raw).encode()
        self.service._writer_factory=lambda token,**kwargs:GoogleCalendarWriter(token,transport=Port(),**kwargs)
        state=self.service.execute(session,self.body)
        self.assertEqual(methods,['GET','GET'])
        self.assertNotEqual(state['receipts'][0]['status'],'applied')

if __name__=='__main__':unittest.main()
