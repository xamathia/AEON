"""HTTP action lifecycle with real OAuth state/PKCE and offline provider ports."""
import copy
import http.client
import json
import socket
import threading
import unittest
from urllib.parse import parse_qs, urlsplit, urlencode

from apps.api.configuration import Configuration
from apps.api.server import create_server
from packages.aeon_oauth import GoogleOAuth

START='2026-09-14T15:00:00Z'
END='2026-09-14T16:00:00Z'
AFTER='2026-09-14T13:00:00Z'
AFTER_END='2026-09-14T14:00:00Z'
EVENT={'id':'e','title':'Personal work','calendar_id':'primary','etag':'PRIVATE_ETAG','planned_start':START,'planned_end':END,'private':True,'organizer_self':True,'attendees_count':0,'attendees_omitted':False,'recurring_event_id':None}
PLAN={'id':'p','operations':[{'event_id':'e','before':{'planned_start':START,'planned_end':END},'after':{'planned_start':AFTER,'planned_end':AFTER_END}}]}

class ActionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.now=1000; self.tokens=[]; self.patches=[]; self.guards=[]
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        self.origin='http://127.0.0.1:'+str(port)
        owner=self
        class Tokens:
            def post_form(inner, fields):
                owner.tokens.append(fields)
                return {'access_token':'PRIVATE_TOKEN','refresh_token':'PRIVATE_REFRESH','token_type':'Bearer','expires_in':3600}
        def oauth(*args,provider,**kwargs):
            return GoogleOAuth(*args,provider=provider,transport=Tokens(),clock=lambda:owner.now,**kwargs)
        self.remote={**EVENT,'external_event_id':'e','classification':'flexible'}
        class Writer:
            def __init__(inner, token, *, authorized_event_ids, before_mutation):
                owner.assertEqual(token,'PRIVATE_TOKEN');owner.assertEqual(authorized_event_ids,['e']);inner.guard=before_mutation
                owner.guards.append(before_mutation)
            def get_event(inner,*args):return copy.deepcopy(owner.remote)
            def patch_times(inner,*args,planned_start,planned_end,if_match,send_updates):
                inner.guard();owner.assertEqual(if_match,owner.remote['etag']);owner.assertEqual(send_updates,'none')
                owner.patches.append((planned_start,planned_end))
                owner.remote.update(planned_start=planned_start,planned_end=planned_end,etag=if_match+'next')
                return copy.deepcopy(owner.remote)
        config=Configuration({'AEON_GOOGLE_CLIENT_ID':'fixture','AEON_GOOGLE_REDIRECT_URI':self.origin+'/'})
        self.server=create_server(port=port,configuration=config,oauth_factory=oauth,action_oauth_factory=oauth,writer_factory=Writer,clock=lambda:self.now)
        self.worker=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True);self.worker.start()
        self.addCleanup(self.stop)
        self.cookie=self.csrf=None
        status,headers,data=self.request('GET','/api/session')
        self.assertEqual(status,200);self.cookie=headers['Set-Cookie'].split(';')[0];self.csrf=data['csrf_token']
        self.session=self.server.connections.session(self.cookie.split('=',1)[1])[0]
    def stop(self):
        self.server.shutdown();self.server.server_close();self.worker.join(2)
    def request(self, method, path, body=None, headers=None, cookie=True):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        values={'Origin':self.origin,'Content-Type':'application/json'}
        if self.cookie and cookie:values['Cookie']=self.cookie
        if self.csrf:values['X-Aeon-CSRF']=self.csrf
        values.update(headers or {})
        try:
            client.request(method,path,body=json.dumps(body) if body is not None else None,headers=values)
            response=client.getresponse();raw=response.read()
            return response.status,response.headers,json.loads(raw) if raw else None
        finally:client.close()
    def connect(self,path='/api/actions/connect'):
        status,_,data=self.request('POST',path,{})
        self.assertEqual(status,200)
        query=parse_qs(urlsplit(data['authorization_url']).query)
        status,headers,_=self.request('GET','/?'+urlencode({'state':query['state'][0],'code':'fixture'}))
        self.assertEqual(status,303);self.assertEqual(headers['Location'],'/')
        return query
    def plan(self):
        with self.session.lock:
            self.session.sources['google_calendar'].canonical_data={'events':[copy.deepcopy(EVENT)]}
            self.session.live_state={'revision':'r','planning':{'plans':[copy.deepcopy(PLAN)]}}
    def execute(self):
        self.plan()
        return self.request('POST','/api/actions/execute',{'revision':'r','plan_id':'p','approved':True})

    def test_status_and_every_action_enforce_session_origin_csrf(self):
        self.assertEqual(self.request('GET','/api/actions',cookie=False)[0],401)
        self.assertEqual(self.request('GET','/api/actions',headers={'Origin':'https://outside.invalid'})[0],403)
        status,headers,data=self.request('GET','/api/actions');self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'no-store');self.assertFalse(data['connected'])
        for action in ('connect','forget','execute','undo'):
            path='/api/actions/'+action
            self.assertEqual(self.request('POST',path,{},headers={'X-Aeon-CSRF':'wrong'})[0],403)
            self.assertEqual(self.request('POST',path,{},headers={'Origin':'https://outside.invalid'})[0],403)
            self.assertEqual(self.request('POST',path,{},cookie=False)[0],401)
        self.assertEqual(self.tokens,[]);self.assertEqual(self.patches,[])

    def test_write_callback_is_separate_and_apply_undo_uses_real_executor(self):
        readonly=self.connect('/api/oauth/google_calendar/begin')
        self.assertEqual(readonly['scope'],['https://www.googleapis.com/auth/calendar.events.readonly'])
        self.assertFalse(self.request('GET','/api/actions')[2]['connected'])
        write=self.connect();self.assertEqual(write['scope'],['https://www.googleapis.com/auth/calendar.events'])
        self.assertEqual(self.session.oauth_result['provider'],'google_calendar_write')
        status,_,data=self.execute();self.assertEqual(status,200)
        receipt=data['receipts'][0];self.assertEqual(receipt['status'],'applied')
        self.assertEqual(self.request('POST','/api/actions/execute',{'revision':'r','plan_id':'p','approved':True})[2]['receipts'][0]['id'],receipt['id'])
        self.assertEqual(len(self.patches),1)
        status,_,data=self.request('POST','/api/actions/undo',{'receipt_id':receipt['id'],'approved':True})
        self.assertEqual(status,200);self.assertEqual(data['receipts'][0]['status'],'undone');self.assertEqual(len(self.guards),2)
        self.assertNotIn('PRIVATE',json.dumps(data));self.assertEqual(self.remote['planned_start'],START)

    def test_calendar_forget_and_reconnect_discard_write_but_gmail_does_not(self):
        self.connect()
        original=self.server.connections.action_discard;calls=[]
        def discard(session):
            self.assertFalse(session.lock._is_owned());calls.append(session.id);original(session)
        self.server.connections.action_discard=discard
        self.assertEqual(self.request('POST','/api/oauth/gmail/forget',{})[0],200)
        self.assertEqual(calls,[]);self.assertTrue(self.request('GET','/api/actions')[2]['connected'])
        self.assertEqual(self.request('POST','/api/oauth/google_calendar/forget',{})[0],200)
        self.assertEqual(len(calls),1);self.assertFalse(self.request('GET','/api/actions')[2]['connected'])
        self.connect();self.request('POST','/api/oauth/google_calendar/begin',{})
        self.assertEqual(len(calls),2);self.assertFalse(self.request('GET','/api/actions')[2]['connected'])

    def test_expiration_discards_write_oauth_outside_session_lock(self):
        self.connect();calls=[];original=self.server.connections.action_discard
        def discard(session):
            self.assertFalse(session.lock._is_owned());calls.append(session.id);original(session)
        self.server.connections.action_discard=discard
        self.now+=3601
        self.assertEqual(self.request('GET','/api/actions')[0],401)
        self.assertEqual(calls,[self.session.id]);self.assertFalse(self.server.actions._oauth.status(self.session.id)['connected'])

    def test_simple_calendar_read_preserves_write_authorization(self):
        self.connect();self.connect('/api/oauth/google_calendar/begin');self.connect()
        class Calendar:
            def __init__(self,token):pass
            def list_events(self,*args,**kwargs):return {'events':[copy.deepcopy(EVENT)],'excluded_events':[],'deleted_event_ids':[],'next_page_token':None}
        self.server.connections._calendar_factory=Calendar
        status,_,_=self.request('POST','/api/calendar/read',{'time_min':'2026-09-14T00:00:00Z','time_max':'2026-09-15T00:00:00Z','timezone':'Europe/Paris'})
        self.assertEqual(status,200);self.assertTrue(self.request('GET','/api/actions')[2]['connected'])

    def test_applied_and_undone_require_fresh_calendar_read_without_losing_undo(self):
        self.connect('/api/oauth/google_calendar/begin');self.connect()
        status,_,data=self.execute();self.assertEqual(status,200)
        receipt=data['receipts'][0]
        status,_,live=self.request('GET','/api/live/scenario')
        self.assertEqual(status,200)
        self.assertIsNone(live['calendar'])
        self.assertEqual(live['issues'][0]['code'],'CALENDAR_NOT_READ')
        source=self.session.sources['google_calendar']
        self.assertIsNone(source.canonical_data)
        providers=self.request('GET','/api/connections')[2]['providers']
        calendar=next(value for value in providers if value['id']=='google_calendar')
        self.assertTrue(calendar['authorized']);self.assertIsNone(calendar['data'])
        self.assertEqual(calendar['read']['status'],'never')
        self.assertTrue(self.request('GET','/api/actions')[2]['connected'])
        status,_,state=self.request('POST','/api/actions/undo',{'receipt_id':receipt['id'],'approved':True})
        self.assertEqual(status,200);self.assertEqual(state['receipts'][0]['status'],'undone')
        self.assertIsNone(self.request('GET','/api/live/scenario')[2]['calendar'])

    def test_unknown_write_discards_cache_but_preflight_conflict_preserves_it(self):
        from packages.aeon_actions import CalendarUnknownOutcome
        self.connect();self.remote['etag']='human-change'
        status,_,state=self.execute();self.assertEqual(status,200)
        self.assertEqual(state['receipts'][0]['status'],'conflict')
        self.assertIsNotNone(self.session.sources['google_calendar'].canonical_data)
        self.remote['etag']='PRIVATE_ETAG'
        factory=self.server.actions._writer_factory
        def uncertain(*args,**kwargs):
            writer=factory(*args,**kwargs)
            def patch_times(*args,**kwargs):
                writer.guard()
                raise CalendarUnknownOutcome()
            writer.patch_times=patch_times
            return writer
        self.server.actions._writer_factory=uncertain
        self.plan();self.session.live_state['revision']='new'
        status,_,state=self.request('POST','/api/actions/execute',{'revision':'new','plan_id':'p','approved':True})
        self.assertEqual(status,200);self.assertEqual(state['receipts'][-1]['status'],'unknown')
        self.assertIsNone(self.session.sources['google_calendar'].canonical_data)
        self.assertEqual(self.request('GET','/api/live/scenario')[2]['issues'][0]['code'],'CALENDAR_NOT_READ')

if __name__=='__main__':unittest.main()
