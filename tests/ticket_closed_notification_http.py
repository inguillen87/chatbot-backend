"""Original login/PUT/SQLite/outbox with synthetic provider boundaries only.

Run in a dedicated process after profile_acceptance_runtime.prepare_process().
No provider opt-in, external socket, production credential or real DB is used.
"""
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SECRET = 'clearly-synthetic-closure-secret-'+'x'*32
RECIPIENT = '+15005550009'
CHANGED_RECIPIENT = '+15005550010'
GLOBAL_SENDER = 'whatsapp:+15005550006'
IMAGE = 'https://brand.example.test/closure.png'


class TicketClosedNotificationHTTPTests(unittest.TestCase):
    sequence = 0

    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        from models import db, TenantProfile, User, ProviderConnection, ProviderSender
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-closure-http-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        cls.database = Path(cls.temp.name)/'profile-acceptance.sqlite'
        cls.sender_ids = {}
        with cls.app.app_context():
            for index, account in enumerate(('acceptance-a', 'acceptance-b'), 1):
                tenant = db.session.get(TenantProfile, cls.accounts[account]['tenant_id'])
                owner = db.session.get(User, cls.accounts[account]['id'])
                if account == 'acceptance-b':
                    tenant.tipo = 'pyme'
                    tenant.pyme_id = owner.id
                    tenant.municipio_id = None
                    owner.tipo_chat = 'pyme'
                    owner.pyme_id = owner.id
                    owner.municipio_id = None
                connection = ProviderConnection(tenant_id=tenant.id, provider='twilio', channel='whatsapp',
                    environment='sandbox', status='online', external_account_id='AC'+str(index)*32,
                    credentials_ref=f'env:FIXTURE_TWILIO_TOKEN_{index}')
                db.session.add(connection)
                db.session.flush()
                sender = ProviderSender(tenant_id=tenant.id, provider_connection_id=connection.id,
                    channel='whatsapp', sender_type='whatsapp_business',
                    sender_id=f'whatsapp:+1500555000{index+6}', phone_number=f'+1500555000{index+6}',
                    status='online', status_callback_url='https://callback.example.test/status')
                db.session.add(sender)
                db.session.flush()
                cls.sender_ids[account] = sender.id
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        from models import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        # This temporary directory was created by this class, outside Git.
        owned = Path(cls.temp.name).resolve()
        if owned.parent != Path(tempfile.gettempdir()).resolve() or not owned.name.startswith('chatboc-closure-http-'):
            raise RuntimeError('Disposable cleanup path is outside the fixture scope')
        cls.temp.cleanup()

    def setUp(self):
        from models import (db, DomainEffectOutbox, TicketComentario, MunicipioTicket, PymeTicket,
            WhatsAppContactState, MessagingEventLedger, WhatsAppEnterpriseRule,
            TenantProfile, User, ProviderSender)
        self.context = self.app.app_context()
        self.context.push()
        for model in (DomainEffectOutbox, TicketComentario, MunicipioTicket, PymeTicket,
                WhatsAppContactState, MessagingEventLedger, WhatsAppEnterpriseRule):
            model.query.delete()
        self.app.config.update(DOMAIN_EFFECT_OUTBOX_MODE='queue', DOMAIN_EFFECT_OUTBOX_SECRET=SECRET,
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS=','.join(str(self.accounts[a]['tenant_id'])
                for a in ('acceptance-a', 'acceptance-b')),
            ENABLE_PYME_WHATSAPP_CHAT=True, TWILIO_ACCOUNT_SID='AC'+'0'*32,
            TWILIO_AUTH_TOKEN='synthetic-global-token-never-sent', TWILIO_WHATSAPP_NUMBER=GLOBAL_SENDER)
        for index, account in enumerate(('acceptance-a', 'acceptance-b'), 1):
            tenant = db.session.get(TenantProfile, self.accounts[account]['tenant_id'])
            owner = db.session.get(User, self.accounts[account]['id'])
            owner.telefono = RECIPIENT
            owner.tenant_id = tenant.id
            tenant.logo_url = None
            tenant.configuracion = {'whatsapp_receipt_header': {'enabled': True, 'image_url': IMAGE},
                'twilio_tech_provider': {'twilio_account_sid': 'AC'+str(index)*32,
                    'twilio_subaccount_token_ref': f'FIXTURE_TWILIO_TOKEN_{index}'}}
            self.app.config[f'FIXTURE_TWILIO_TOKEN_{index}'] = 'fixture-not-a-real-token'
            sender = db.session.get(ProviderSender, self.sender_ids[account])
            sender.status = 'online'
            sender.status_callback_url = 'https://callback.example.test/status'
            db.session.add(WhatsAppContactState(tenant_id=tenant.id, provider_sender_id=sender.id,
                recipient=RECIPIENT, last_inbound_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        db.session.commit()
        self.clients = {}
        self.provider_calls = []
        self.provider_action = 'accepted'
        self.stack = ExitStack()
        self.email = self.stack.enter_context(patch('services.email_service.enviar_email_ticket_novedad', return_value=True))
        self.sms = self.stack.enter_context(patch('services.email_service.enviar_sms_ticket_novedad', return_value=True))
        self.legacy = self.stack.enter_context(patch('services.email_service.enviar_whatsapp_ticket_novedad', return_value=True))
        self.socket = self.stack.enter_context(patch('routes.ticket.emit_ticket_status_changed'))
        self.wakeup = self.stack.enter_context(patch('services.domain_effect_worker.enqueue_domain_effect_dispatch', return_value=False))
        fixture = self
        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.messages = SimpleNamespace(create=fixture.provider_create)
        self.stack.enter_context(patch('services.tenant_twilio_messaging.Client', FakeClient))
        self.stack.enter_context(patch('services.tenant_twilio_messaging.require_provider_network',
            side_effect=lambda provider: self.assertEqual(provider, 'twilio')))

    def tearDown(self):
        from models import db
        self.stack.close()
        db.session.rollback()
        db.session.remove()
        self.context.pop()

    def provider_create(self, **params):
        # Independently prove the generic engine persisted its I/O boundary.
        with closing(sqlite3.connect(self.database)) as connection:
            active = connection.execute("SELECT COUNT(*) FROM domain_effect_outbox WHERE status='processing' AND io_started_at IS NOT NULL").fetchone()[0]
            closed = connection.execute("SELECT COUNT(*) FROM ticket_comentario WHERE origen='sistema' AND estado_ticket='cerrado'").fetchone()[0]
        self.assertGreater(active, 0)
        self.assertGreater(closed, 0)
        self.provider_calls.append(dict(params))
        if self.provider_action == 'timeout':
            raise TimeoutError('synthetic timeout after I/O boundary')
        return SimpleNamespace(sid=None if self.provider_action == 'no_sid' else 'SM'+'9'*32)

    def login(self, account='acceptance-a'):
        if account not in self.clients:
            client = self.app.test_client()
            response = client.post('/api/auth/admin/login', json={
                'email': self.accounts[account]['email'], 'password': self.password})
            self.assertEqual(response.status_code, 200, response.get_json())
            token = response.get_json().get('token') or response.get_json().get('access_token')
            self.assertIsInstance(token, str)
            client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer '+token
            self.clients[account] = client
        return self.clients[account]

    def ticket(self, account='acceptance-a', *, canonical=True):
        from models import db, MunicipioTicket, PymeTicket
        type(self).sequence += 1
        tipo = 'municipio' if account == 'acceptance-a' else 'pyme'
        payload = dict(tenant_id=self.accounts[account]['tenant_id'] if canonical else None,
            user_id=self.accounts[account]['id'], pregunta='Clearly synthetic closure case',
            categoria='Fixture', estado='nuevo')
        if tipo == 'municipio':
            obj = MunicipioTicket(municipio_id=self.accounts[account]['id'],
                nro_ticket='closure-fixture-'+str(self.sequence), **payload)
        else:
            obj = PymeTicket(nro_ticket=910000+self.sequence, telefono=RECIPIENT, **payload)
        db.session.add(obj)
        db.session.commit()
        return obj

    def close(self, ticket, account='acceptance-a', *, expected='nuevo', destination='cerrado'):
        tipo = 'municipio' if ticket.__class__.__name__ == 'MunicipioTicket' else 'pyme'
        return self.login(account).put(f'/api/tickets/{tipo}/{ticket.id}/estado',
            headers={'X-Tenant-Slug': account}, json={'estado': destination, 'expected_estado': expected})

    def effects(self):
        from models import DomainEffectOutbox
        return DomainEffectOutbox.query.order_by(DomainEffectOutbox.id).all()

    def dispatch(self, account='acceptance-a'):
        from services.domain_effect_outbox import dispatch_domain_effects
        from services.ticket_domain_effects import TICKET_DOMAIN_EFFECT_REGISTRY
        return dispatch_domain_effects(registry=TICKET_DOMAIN_EFFECT_REGISTRY, intent_secret=SECRET,
            tenant_id=self.accounts[account]['tenant_id'], limit=10)

    def assert_no_legacy(self):
        self.legacy.assert_not_called()

    def test_close_stages_one_private_effect_same_transaction_and_preserves_other_channels(self):
        ticket = self.ticket()
        response = self.close(ticket)
        self.assertEqual(response.status_code, 200, response.get_json())
        rows = self.effects()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.handler_name, 'ticket.state.closed.whatsapp.requester.v1')
        self.assertEqual(set(row.payload_json), {'owner_binding', 'provider_sender_binding', 'event_binding', 'requester_binding'})
        serialized = json.dumps(row.payload_json)
        for raw in (RECIPIENT, IMAGE, ticket.pregunta, SECRET):
            self.assertNotIn(raw, serialized)
        self.assertEqual(len(self.provider_calls), 0)
        self.assert_no_legacy()
        self.assertEqual((self.email.call_count, self.sms.call_count, self.socket.call_count), (1, 1, 1))
        self.wakeup.assert_called_once_with(tenant_id=ticket.tenant_id)

    def test_worker_uses_each_tenant_sender_and_same_header_in_one_call(self):
        for account, sender in [('acceptance-a', 'whatsapp:+15005550007'),
                ('acceptance-b', 'whatsapp:+15005550008')]:
            with self.subTest(tipo=account):
                ticket = self.ticket(account)
                self.assertEqual(self.close(ticket, account).status_code, 200)
                before = len(self.provider_calls)
                summary = self.dispatch(account)
                self.assertEqual(summary.succeeded, 1)
                self.assertEqual(len(self.provider_calls), before+1)
                call = self.provider_calls[-1]
                self.assertEqual(call['from_'], sender)
                self.assertEqual(call['to'], 'whatsapp:'+RECIPIENT)
                self.assertEqual(call['media_url'], [IMAGE])
                self.assertNotIn('content_sid', call)
                self.assertIn('cerrado', call['body'])
                self.assertEqual(self.effects()[-1].result_json['delivery'], 'provider_accepted')
                self.assertIsNotNone(self.effects()[-1].provider_ref_hash)
        self.assert_no_legacy()

    def test_alias_close_then_stale_cas_does_not_duplicate_comment_effect_or_send(self):
        from models import TicketComentario
        ticket = self.ticket()
        self.assertEqual(self.close(ticket, destination='resuelto').status_code, 200)
        replay = self.close(ticket)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.get_json()['reason_code'], 'stale_ticket_state')
        self.assertEqual(TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count(), 1)
        self.assertEqual(len(self.effects()), 1)
        self.assertEqual(self.dispatch().succeeded, 1)
        self.assertEqual(self.dispatch().processed, 0)
        self.assertEqual(len(self.provider_calls), 1)
        self.assert_no_legacy()

    def test_staging_replay_is_same_effect(self):
        from models import db, TicketComentario, User
        from services.ticket_closed_notification import stage_ticket_closed_whatsapp
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        comment = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).one()
        actor = db.session.get(User, self.accounts['acceptance-a']['id'])
        self.assertTrue(stage_ticket_closed_whatsapp(comment, ticket, tipo_ticket='municipio', actor=actor))
        db.session.commit()
        self.assertEqual(len(self.effects()), 1)

    def test_failure_after_stage_rolls_back_state_comment_and_effect_without_channel(self):
        from models import db, MunicipioTicket, TicketComentario
        from services.ticket_closed_notification import stage_ticket_closed_whatsapp
        ticket = self.ticket()
        ident = ticket.id
        self.login()
        def stage_then_fail(*args, **kwargs):
            self.assertTrue(stage_ticket_closed_whatsapp(*args, **kwargs))
            raise RuntimeError('synthetic failure after staging')
        with patch('services.ticket_closed_notification.stage_ticket_closed_whatsapp', side_effect=stage_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure after staging'):
                self.close(ticket)
        db.session.rollback()
        db.session.expire_all()
        self.assertEqual(db.session.get(MunicipioTicket, ident).estado, 'nuevo')
        self.assertEqual(TicketComentario.query.count(), 0)
        self.assertEqual(len(self.effects()), 0)
        self.assertEqual(len(self.provider_calls), 0)
        self.assertEqual((self.email.call_count, self.sms.call_count, self.socket.call_count), (0, 0, 0))
        self.assert_no_legacy()

    def test_foreign_ticket_denied_without_effect_or_notification(self):
        from models import TicketComentario
        ticket = self.ticket('acceptance-b')
        response = self.close(ticket, 'acceptance-a')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.effects()), 0)
        self.assertEqual(TicketComentario.query.count(), 0)
        self.assertEqual(len(self.provider_calls), 0)
        self.assert_no_legacy()

    def test_non_closed_state_keeps_existing_notifications_without_effect(self):
        ticket = self.ticket()
        self.assertEqual(self.close(ticket, destination='en_proceso').status_code, 200)
        self.assertEqual(len(self.effects()), 0)
        self.legacy.assert_called_once()
        self.wakeup.assert_not_called()

    def test_legacy_gate_keeps_current_notice_without_new_header(self):
        self.app.config['DOMAIN_EFFECT_OUTBOX_MODE'] = 'legacy'
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        self.assertEqual(len(self.effects()), 0)
        self.legacy.assert_called_once()
        self.wakeup.assert_not_called()

    def test_missing_canonical_tenant_keeps_existing_path(self):
        ticket = self.ticket(canonical=False)
        self.assertEqual(self.close(ticket).status_code, 200)
        self.assertEqual(len(self.effects()), 0)
        self.legacy.assert_called_once()

    def test_pyme_disabled_does_not_gain_new_header_capability(self):
        self.app.config['ENABLE_PYME_WHATSAPP_CHAT'] = False
        ticket = self.ticket('acceptance-b')
        self.assertEqual(self.close(ticket, 'acceptance-b').status_code, 200)
        self.assertEqual(len(self.effects()), 0)
        self.legacy.assert_called_once()

    def test_window_unknown_expired_future_or_other_sender_cannot_gain_ownership(self):
        from models import db, WhatsAppContactState
        for mode in ('unknown', 'expired', 'future', 'other_sender'):
            with self.subTest(mode=mode):
                WhatsAppContactState.query.delete()
                if mode != 'unknown':
                    inbound = datetime.now(timezone.utc)+(
                        timedelta(hours=-25) if mode == 'expired' else
                        timedelta(hours=1) if mode == 'future' else timedelta(minutes=-1))
                    db.session.add(WhatsAppContactState(tenant_id=self.accounts['acceptance-a']['tenant_id'],
                        provider_sender_id=self.sender_ids['acceptance-b'] if mode == 'other_sender'
                            else self.sender_ids['acceptance-a'], recipient=RECIPIENT, last_inbound_at=inbound))
                db.session.commit()
                self.legacy.reset_mock()
                ticket = self.ticket()
                self.assertEqual(self.close(ticket).status_code, 200)
                self.assertEqual(len(self.effects()), 0)
                self.legacy.assert_called_once()
        self.assertEqual(len(self.provider_calls), 0)

    def test_owned_effect_recipient_change_blocks_without_legacy_fallback(self):
        from models import db, User
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        db.session.get(User, self.accounts['acceptance-a']['id']).telefono = CHANGED_RECIPIENT
        db.session.commit()
        self.assertEqual(self.dispatch().dead, 1)
        self.assertEqual(self.effects()[0].last_error_code, 'ticket_closed_requester_changed')
        self.assertEqual(len(self.provider_calls), 0)
        self.assert_no_legacy()

    def test_owned_effect_sender_callback_change_blocks_without_fallback(self):
        from models import db, ProviderSender
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        db.session.get(ProviderSender, self.sender_ids['acceptance-a']).status_callback_url = 'https://other.example.test/status'
        db.session.commit()
        self.assertEqual(self.dispatch().dead, 1)
        self.assertEqual(self.effects()[0].last_error_code, 'ticket_closed_sender_changed')
        self.assertEqual(len(self.provider_calls), 0)
        self.assert_no_legacy()

    def test_owned_effect_window_expired_before_worker_skips_without_fallback(self):
        from models import db, WhatsAppContactState
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        for state in WhatsAppContactState.query.all():
            state.last_inbound_at = datetime.now(timezone.utc)-timedelta(hours=25)
        db.session.commit()
        self.assertEqual(self.dispatch().skipped, 1)
        self.assertEqual(self.effects()[0].result_json['reason_code'], 'ticket_closed_window_expired')
        self.assertEqual(len(self.provider_calls), 0)
        self.assert_no_legacy()

    def test_system_event_content_change_is_not_a_new_human_reply(self):
        from models import db, TicketComentario
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        TicketComentario.query.one().comentario = 'Synthetic mutation of stored event'
        db.session.commit()
        self.assertEqual(self.dispatch().dead, 1)
        self.assertEqual(self.effects()[0].last_error_code, 'ticket_closed_event_binding_changed')
        self.assertEqual(len(self.provider_calls), 0)

    def test_superseded_ticket_does_not_send_old_closure(self):
        from models import db
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        # A separate domain workflow may reopen; state PUT itself disallows it.
        ticket.estado = 'en_proceso'
        db.session.commit()
        self.assertEqual(self.dispatch().skipped, 1)
        self.assertEqual(self.effects()[0].result_json['reason_code'], 'ticket_closed_event_superseded')
        self.assertEqual(len(self.provider_calls), 0)

    def test_disabled_invalid_or_inherited_header_keeps_one_text_notice(self):
        from models import db, TenantProfile
        for mode in ('disabled', 'invalid', 'inherit'):
            with self.subTest(mode=mode):
                ticket = self.ticket()
                self.assertEqual(self.close(ticket).status_code, 200)
                tenant = db.session.get(TenantProfile, ticket.tenant_id)
                config = dict(tenant.configuracion)
                config['whatsapp_receipt_header'] = {'enabled': False, 'image_url': IMAGE} if mode == 'disabled' else (
                    {'enabled': True, 'image_url': 'https://localhost/private.png'} if mode == 'invalid' else None)
                tenant.configuracion = config
                tenant.logo_url = IMAGE if mode == 'inherit' else None
                db.session.commit()
                before = len(self.provider_calls)
                self.assertEqual(self.dispatch().succeeded, 1)
                self.assertEqual(len(self.provider_calls), before+1)
                if mode == 'inherit':
                    self.assertEqual(self.provider_calls[-1]['media_url'], [IMAGE])
                else:
                    self.assertNotIn('media_url', self.provider_calls[-1])
                self.assertIn('cerrado', self.provider_calls[-1]['body'])
        self.assert_no_legacy()

    def test_enterprise_policy_blocks_before_io_and_reservation_counts_once(self):
        from models import db, WhatsAppEnterpriseRule, MessagingEventLedger
        db.session.add(WhatsAppEnterpriseRule(tenant_id=self.accounts['acceptance-a']['tenant_id'],
            max_outbound_per_hour=1, enforce_template_outside_24h=True))
        db.session.commit()
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        self.assertEqual(self.dispatch().succeeded, 1)
        self.assertEqual(self.dispatch().processed, 0)
        other = self.ticket()
        self.assertEqual(self.close(other).status_code, 200)
        self.assertEqual(self.dispatch().skipped, 1)
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual(MessagingEventLedger.query.filter_by(event_type='whatsapp_outbound_rate_limit_reserved').count(), 1)
        self.assert_no_legacy()

    def test_post_io_timeout_or_missing_sid_is_unknown_and_never_resent(self):
        for mode in ('timeout', 'no_sid'):
            with self.subTest(mode=mode):
                self.provider_action = mode
                ticket = self.ticket()
                self.assertEqual(self.close(ticket).status_code, 200)
                before = len(self.provider_calls)
                self.assertEqual(self.dispatch().unknown, 1)
                self.assertEqual(self.dispatch().processed, 0)
                self.assertEqual(len(self.provider_calls), before+1)
                self.assertEqual(self.effects()[-1].status, 'unknown')
                self.assertIsNone(self.effects()[-1].provider_ref_hash)
        self.assert_no_legacy()

    def test_wakeup_failure_keeps_durable_owner_without_legacy_fallback(self):
        self.wakeup.side_effect = RuntimeError('synthetic wakeup failure')
        ticket = self.ticket()
        self.assertEqual(self.close(ticket).status_code, 200)
        self.assertEqual(len(self.effects()), 1)
        self.assertEqual(self.effects()[0].status, 'pending')
        self.assertEqual(len(self.provider_calls), 0)
        self.assert_no_legacy()
        self.assertEqual((self.email.call_count, self.sms.call_count, self.socket.call_count), (1, 1, 1))


if __name__ == '__main__':
    raise RuntimeError('Use the isolated acceptance runner with prepare_process before app imports')
