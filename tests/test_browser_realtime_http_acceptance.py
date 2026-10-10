"""Disposable full-app owner voice HTTP/SQL contracts. No real provider or mic.

Run only after profile_acceptance_runtime.prepare_process in a fresh process.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from tests.profile_acceptance_runtime import create_disposable_app

OFFER='v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'
STATE={'revision':'offline-published-revision','bundle':{'nodes':{'root':{
    'id':'root','title':'Información pública','text':'Usá el chat para consultar.',
    'sources':[],'links':[],'actions':[]}}}}


class BrowserVoiceHTTPAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory(prefix='chatboc-voice-http-offline-')
        cls.app,cls.accounts,cls.password=create_disposable_app(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():db.session.remove();db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from database import db
        from models import TenantProfile,AuditEvent
        self.db,self.Tenant,self.Audit=db,TenantProfile,AuditEvent
        self.context=self.app.app_context();self.context.push()
        db.session.query(AuditEvent).delete()
        for tenant in TenantProfile.query.filter(TenantProfile.slug.in_(['acceptance-a','acceptance-b'])):
            tenant.configuracion={};tenant.is_active=True
        db.session.commit()
        self.app.config['OPENAI_API_KEY']='synthetic-offline-key'
        self.endpoint='/api/admin/tenants/acceptance-a/realtime/browser'

    def tearDown(self):
        self.db.session.rollback();self.db.session.remove();self.context.pop()

    def login(self,account='acceptance-a'):
        client=self.app.test_client()
        response=client.post('/auth/login',json={'email':self.accounts[account]['email'],'password':self.password})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json.get('token'))
        return client

    def test_real_auth_requires_owner_and_defaults_to_disabled_without_calls(self):
        with patch('routes.browser_realtime.provider_request') as provider:
            self.assertEqual(self.app.test_client().get(self.endpoint+'/capabilities').status_code,401)
            owner=self.login()
            response=owner.get(self.endpoint+'/capabilities')
            self.assertEqual(response.status_code,200)
            self.assertIs(response.json['enabled'],False)
            self.assertEqual(response.json['tenant'],{'id':self.accounts['acceptance-a']['tenant_id'],'slug':'acceptance-a'})
            for account in ('acceptance-b','second','viewer','delegated'):
                self.assertEqual(self.login(account).get(self.endpoint+'/capabilities').status_code,403)
            provider.assert_not_called()
            self.assertEqual(self.db.session.query(self.Audit).count(),0)

    def test_real_login_and_sql_issue_and_stop_one_scoped_session(self):
        from services import browser_realtime as voice
        tenant=self.db.session.get(self.Tenant,self.accounts['acceptance-a']['tenant_id'])
        tenant.configuracion={'browser_realtime_voice':{'enabled':True,'max_sessions_per_hour':1,
            'max_total_sessions':1,'trial_expires_at':int((datetime.now(timezone.utc)+timedelta(hours=1)).timestamp())}}
        self.db.session.commit()
        create=Mock(return_value={'call_id':'rtc_synthetic','sdp':OFFER})
        hangup=Mock(return_value={'stopped':True})
        original_stop=voice.VoiceLedger.stop
        def stop(ledger,*args,**kwargs):return original_stop(ledger,*args,provider=hangup,**kwargs)
        with patch('routes.browser_realtime.read_state',return_value=STATE),patch('routes.browser_realtime.provider_request',create),patch.object(voice.VoiceLedger,'stop',stop):
            client=self.login()
            capability=client.get(self.endpoint+'/capabilities')
            self.assertEqual(capability.status_code,200);self.assertIs(capability.json['enabled'],True)
            command={'consent':True,'sdp':OFFER,'revision':STATE['revision']}
            response=client.post(self.endpoint+'/sessions',json=command)
            self.assertEqual(response.status_code,200)
            identifier=response.json['session_id']
            self.assertNotIn('rtc_synthetic',response.get_data(as_text=True))
            self.assertNotIn('synthetic-offline-key',response.get_data(as_text=True))
            self.assertEqual(client.post(self.endpoint+'/sessions',json=command).status_code,409)
            self.assertEqual(self.login('acceptance-b').post(self.endpoint+'/sessions/'+identifier+'/stop',json={}).status_code,403)
            self.assertEqual(client.post(self.endpoint+'/sessions/'+identifier+'/stop',json={}).status_code,200)
            self.assertEqual(client.post(self.endpoint+'/sessions/'+identifier+'/stop',json={}).status_code,200)
            exhausted=client.get(self.endpoint+'/capabilities')
            self.assertIs(exhausted.json['enabled'],False)
            self.assertEqual(exhausted.json['reason_code'],'browser_voice_total_cap')
            self.assertEqual(exhausted.json['admission'],{'total_sessions_reserved':1,'total_sessions_remaining':0})
            self.assertEqual(client.post(self.endpoint+'/sessions',json=command).status_code,429)
            create.assert_called_once();hangup.assert_called_once()
            events=self.db.session.query(self.Audit).filter_by(resource_type=voice.CONTRACT).all()
            self.assertEqual([event.event_type for event in events],[voice.EVENT+kind for kind in ('intent','accepted','stop_intent','stopped')])
            self.assertTrue(all(event.tenant_id==tenant.id and event.actor_user_id==self.accounts['acceptance-a']['id'] for event in events))
            self.assertTrue(all('sdp' not in event.details and 'transcript' not in event.details for event in events))

    def test_real_http_legacy_or_expired_trial_cannot_reserve_or_call(self):
        tenant=self.db.session.get(self.Tenant,self.accounts['acceptance-a']['tenant_id'])
        client=self.login()
        command={'consent':True,'sdp':OFFER,'revision':STATE['revision']}
        variants=[({'enabled':True,'max_sessions_per_hour':1},'browser_voice_total_cap_required',503),
            ({'enabled':True,'max_sessions_per_hour':1,'max_total_sessions':1,
              'trial_expires_at':1},'browser_voice_trial_expired',410)]
        with patch('routes.browser_realtime.read_state',return_value=STATE),patch('routes.browser_realtime.provider_request') as provider:
            for config,reason,status in variants:
                with self.subTest(reason=reason):
                    tenant.configuracion={'browser_realtime_voice':config};self.db.session.commit()
                    response=client.get(self.endpoint+'/capabilities')
                    self.assertEqual(response.status_code,200)
                    self.assertIs(response.json['enabled'],False)
                    self.assertEqual(response.json['reason_code'],reason)
                    self.assertEqual(client.post(self.endpoint+'/sessions',json=command).status_code,status)
            provider.assert_not_called()
            self.assertEqual(self.db.session.query(self.Audit).count(),0)

    def test_real_http_active_call_can_be_stopped_after_trial_expiry(self):
        from services import browser_realtime as voice
        tenant=self.db.session.get(self.Tenant,self.accounts['acceptance-a']['tenant_id'])
        tenant.configuracion={'browser_realtime_voice':{'enabled':True,'max_sessions_per_hour':1,
            'max_total_sessions':1,'trial_expires_at':int((datetime.now(timezone.utc)+timedelta(hours=1)).timestamp())}}
        self.db.session.commit()
        create=Mock(return_value={'call_id':'rtc_synthetic','sdp':OFFER})
        hangup=Mock(return_value={'stopped':True})
        original_stop=voice.VoiceLedger.stop
        def stop(ledger,*args,**kwargs):return original_stop(ledger,*args,provider=hangup,**kwargs)
        with patch('routes.browser_realtime.read_state',return_value=STATE),patch('routes.browser_realtime.provider_request',create),patch.object(voice.VoiceLedger,'stop',stop):
            client=self.login()
            command={'consent':True,'sdp':OFFER,'revision':STATE['revision']}
            response=client.post(self.endpoint+'/sessions',json=command)
            self.assertEqual(response.status_code,200)
            identifier=response.json['session_id']
            tenant.configuracion={'browser_realtime_voice':{'enabled':True,'max_sessions_per_hour':1,
                'max_total_sessions':1,'trial_expires_at':1}};self.db.session.commit()
            self.assertEqual(client.post(self.endpoint+'/sessions/'+identifier+'/stop',json={}).status_code,200)
            self.assertEqual(client.post(self.endpoint+'/sessions/'+identifier+'/stop',json={}).status_code,200)
            self.assertEqual(client.post(self.endpoint+'/sessions',json=command).status_code,410)
            create.assert_called_once();hangup.assert_called_once()
