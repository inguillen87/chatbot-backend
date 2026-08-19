import json
import unittest
import uuid
from unittest.mock import patch

from app import create_app
from config import TestConfig
from extensions import db
from models import ProviderSender, TenantProfile, User, WhatsappNumero
from models_voice_lifecycle import VoiceCallLifecycle, VoiceCallLifecycleEvent
from services.voice_consent_lifecycle import (
    VoiceConsentLifecycleError,
    begin_voice_consent,
    claim_voice_stream_authorization,
    record_voice_consent_decision,
    record_voice_provider_status,
    resolve_authoritative_voice_tenant,
    resolve_voice_consent_policy,
)
from services.voice_stream_envelope import create_voice_stream_envelope
from services.voice_stream_service import VoiceStreamService


SIGNING_SECRET = "voice-consent-test-secret-longer-than-thirty-two-bytes"


class _InboundSocket:
    def __init__(self, events):
        self.events = [json.dumps(event) for event in events]
        self.closed = False

    def receive(self, timeout=None):
        return self.events.pop(0) if self.events else None

    def close(self):
        self.closed = True


class VoiceConsentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config.update(
            TESTING=True,
            BACKEND_URL="https://api.chatboc.test",
            OPENAI_API_KEY="test-openai-key",
            ENABLE_VOICE_CONSENT_LIFECYCLE_V1=True,
            VOICE_STREAM_SIGNING_SECRET=SIGNING_SECRET,
            VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE=True,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self.owner_a, self.tenant_a = self._tenant("voice-consent-a", "+15551230001")
        self.owner_b, self.tenant_b = self._tenant("voice-consent-b", "+15551230002")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _tenant(slug, number):
        owner = User(
            email=f"{slug}@example.test",
            name=f"Owner {slug}",
            password_hash="test",
            rol="admin_pyme",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=slug,
            nombre=f"Tenant {slug}",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={
                "voice_consent_policy": {
                    "version": "voice.consent.v1",
                    "ai_processing": "explicit_per_call",
                    "recording": "disabled",
                }
            },
        )
        db.session.add(tenant)
        db.session.flush()
        db.session.add(
            ProviderSender(
                tenant_id=tenant.id,
                channel="voice",
                phone_number=number,
                sender_id=number,
                status="active",
            )
        )
        db.session.commit()
        return owner, tenant

    def _payload(self, *, call_sid="CA-consent-001", to_number="+15551230001"):
        return {
            "CallSid": call_sid,
            "From": "+5492613168608",
            "To": to_number,
            "Direction": "inbound",
        }

    def _whatsapp_payload(
        self,
        *,
        call_sid="CA-whatsapp-consent-001",
        to_number="whatsapp:+15551230001",
    ):
        return {
            "CallSid": call_sid,
            "From": "whatsapp:+5492613168608",
            "To": to_number,
            "Direction": "inbound",
        }

    def test_initial_webhook_requires_dtmf_without_minting_stream(self):
        with patch("routes.voice_routes.create_voice_stream_envelope") as mint_envelope:
            response = self.client.post(
                f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
                data=self._payload(),
            )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<Gather", body)
        self.assertIn('input="dtmf"', body)
        self.assertNotIn("<Connect>", body)
        mint_envelope.assert_not_called()

        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.tenant_id, self.tenant_a.id)
        self.assertEqual(lifecycle.state, "consent_pending")
        self.assertEqual(lifecycle.consent_status, "required")
        self.assertFalse(lifecycle.ai_processing_allowed)
        self.assertFalse(lifecycle.recording_allowed)
        self.assertFalse(lifecycle.recording_enabled)
        self.assertEqual(
            [event.reason_code for event in VoiceCallLifecycleEvent.query.order_by(VoiceCallLifecycleEvent.id)],
            ["provider_call_received", "explicit_consent_required"],
        )

    def test_whatsapp_business_call_uses_same_explicit_consent_boundary(self):
        call_sid = "CA-whatsapp-inbound-001"
        initial = self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._whatsapp_payload(call_sid=call_sid),
        )

        initial_body = initial.get_data(as_text=True)
        self.assertEqual(initial.status_code, 200)
        self.assertIn("<Gather", initial_body)
        self.assertNotIn("<Connect>", initial_body)

        granted = self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**self._whatsapp_payload(call_sid=call_sid), "Digits": "1"},
        )

        self.assertIn("<Connect>", granted.get_data(as_text=True))
        lifecycle = VoiceCallLifecycle.query.filter_by(
            provider_call_sid=call_sid
        ).one()
        self.assertEqual(lifecycle.tenant_id, self.tenant_a.id)
        self.assertEqual(lifecycle.state, "stream_authorized")
        self.assertTrue(lifecycle.ai_processing_allowed)

    def test_incomplete_tenant_policy_fails_closed_without_lifecycle(self):
        config = dict(self.tenant_a.configuracion or {})
        config["voice_consent_policy"] = {"version": "voice.consent.v1"}
        self.tenant_a.configuracion = config
        db.session.commit()

        with patch("routes.voice_routes.create_voice_stream_envelope") as mint_envelope:
            response = self.client.post(
                f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
                data=self._payload(call_sid="CA-policy-incomplete"),
            )

        body = response.get_data(as_text=True)
        self.assertNotIn("<Gather", body)
        self.assertNotIn("<Connect>", body)
        mint_envelope.assert_not_called()
        self.assertEqual(VoiceCallLifecycle.query.count(), 0)

    def test_same_version_policy_disable_revokes_prior_stream_grant(self):
        call_sid = "CA-policy-disabled-after-grant"
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(call_sid=call_sid),
        )
        granted = self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**self._payload(call_sid=call_sid), "Digits": "1"},
        )
        self.assertIn("<Connect>", granted.get_data(as_text=True))

        config = dict(self.tenant_a.configuracion or {})
        policy_config = dict(config["voice_consent_policy"])
        policy_config["ai_processing"] = "disabled"
        config["voice_consent_policy"] = policy_config
        self.tenant_a.configuracion = config
        db.session.commit()

        # Exercise the TwiML boundary directly: an old durable grant is not
        # enough once the current tenant policy disables AI processing.
        from routes.voice_routes import _voice_stream_twiml_response

        with self.app.test_request_context(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            method="POST",
            data={**self._payload(call_sid=call_sid), "Digits": "1"},
        ):
            blocked_twiml = _voice_stream_twiml_response(
                authoritative_tenant=self.tenant_a
            )
        self.assertNotIn("<Connect>", blocked_twiml.get_data(as_text=True))

        params = create_voice_stream_envelope(
            call_sid=call_sid,
            from_number="+5492613168608",
            to_number="+15551230001",
            tenant_slug=self.tenant_a.slug,
            vertical="pyme",
            config=self.app.config,
            nonce=f"nonce_{uuid.uuid4().hex}",
        )
        start = {
            "event": "start",
            "start": {
                "streamSid": "MZ-policy-disabled-after-grant",
                "callSid": call_sid,
                "customParameters": params,
            },
        }
        socket = _InboundSocket([start])
        service = VoiceStreamService(socket, app=self.app)
        with patch("services.voice_stream_service.ws_connect") as ws_connect, patch.object(
            service, "_resolve_context"
        ) as resolve_context:
            service.run()

        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(event_key="stream:claimed").count(),
            0,
        )
        resolve_context.assert_not_called()
        ws_connect.assert_not_called()
        self.assertTrue(socket.closed)

    def test_malformed_current_policy_revokes_prior_stream_grant(self):
        call_sid = "CA-policy-malformed-after-grant"
        policy = resolve_voice_consent_policy(self.tenant_a)
        begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid=call_sid,
            direction="inbound",
            policy=policy,
        )
        record_voice_consent_decision(
            tenant_id=self.tenant_a.id,
            call_sid=call_sid,
            decision="granted",
        )

        config = dict(self.tenant_a.configuracion or {})
        config["voice_consent_policy"] = {
            "version": policy.version,
            "ai_processing": "explicit_per_call",
        }
        self.tenant_a.configuracion = config
        db.session.commit()

        params = create_voice_stream_envelope(
            call_sid=call_sid,
            from_number="+5492613168608",
            to_number="+15551230001",
            tenant_slug=self.tenant_a.slug,
            vertical="pyme",
            config=self.app.config,
            nonce=f"nonce_{uuid.uuid4().hex}",
        )
        start = {
            "event": "start",
            "start": {
                "streamSid": "MZ-policy-malformed-after-grant",
                "callSid": call_sid,
                "customParameters": params,
            },
        }
        socket = _InboundSocket([start])
        service = VoiceStreamService(socket, app=self.app)
        with patch("services.voice_stream_service.ws_connect") as ws_connect, patch.object(
            service, "_resolve_context"
        ) as resolve_context:
            service.run()

        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(event_key="stream:claimed").count(),
            0,
        )
        resolve_context.assert_not_called()
        ws_connect.assert_not_called()
        self.assertTrue(socket.closed)

    def test_explicit_grant_is_idempotent_and_only_then_mints_stream(self):
        initial = self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(),
        )
        self.assertNotIn("<Connect>", initial.get_data(as_text=True))

        consent_url = f"/twilio/voice/consent?tenant={self.tenant_a.slug}"
        first = self.client.post(consent_url, data={**self._payload(), "Digits": "1"})
        second = self.client.post(consent_url, data={**self._payload(), "Digits": "1"})

        self.assertIn("<Connect>", first.get_data(as_text=True))
        self.assertIn("<Connect>", second.get_data(as_text=True))
        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.state, "stream_authorized")
        self.assertEqual(lifecycle.consent_status, "granted")
        self.assertTrue(lifecycle.ai_processing_allowed)
        self.assertFalse(lifecycle.recording_enabled)
        grant_events = VoiceCallLifecycleEvent.query.filter_by(
            reason_code="explicit_dtmf_consent_granted"
        ).all()
        self.assertEqual(len(grant_events), 1)
        self.assertFalse(hasattr(grant_events[0], "digits"))

    def test_decline_is_terminal_for_ai_and_never_mints_envelope(self):
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(),
        )
        consent_url = f"/twilio/voice/consent?tenant={self.tenant_a.slug}"
        with patch("routes.voice_routes.create_voice_stream_envelope") as mint_envelope:
            declined = self.client.post(
                consent_url, data={**self._payload(), "Digits": "2"}
            )
            reversal = self.client.post(
                consent_url, data={**self._payload(), "Digits": "1"}
            )

        self.assertNotIn("<Connect>", declined.get_data(as_text=True))
        self.assertNotIn("<Connect>", reversal.get_data(as_text=True))
        mint_envelope.assert_not_called()
        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.consent_status, "declined")
        self.assertEqual(lifecycle.state, "consent_pending")
        self.assertFalse(lifecycle.ai_processing_allowed)
        self.assertFalse(lifecycle.recording_enabled)

    def test_missing_consent_is_audited_without_opening_stream(self):
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(),
        )
        response = self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data=self._payload(),
        )

        self.assertNotIn("<Connect>", response.get_data(as_text=True))
        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.consent_status, "declined")
        self.assertFalse(lifecycle.ai_processing_allowed)
        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(
                reason_code="consent_missing_or_timeout"
            ).count(),
            1,
        )

        late_grant = self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**self._payload(), "Digits": "1"},
        )
        self.assertNotIn("<Connect>", late_grant.get_data(as_text=True))
        db.session.expire_all()
        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.consent_status, "declined")
        self.assertFalse(lifecycle.ai_processing_allowed)

    def test_existing_call_rejects_direction_tamper(self):
        policy = resolve_voice_consent_policy(self.tenant_a)
        lifecycle = begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid="CA-direction-001",
            direction="inbound",
            policy=policy,
        )

        with self.assertRaisesRegex(
            VoiceConsentLifecycleError, "call_direction_mismatch"
        ):
            begin_voice_consent(
                tenant_id=self.tenant_a.id,
                call_sid=lifecycle.provider_call_sid,
                direction="outbound-api",
                policy=policy,
            )

        db.session.expire_all()
        persisted = VoiceCallLifecycle.query.filter_by(id=lifecycle.id).one()
        self.assertEqual(persisted.direction, "inbound")
        self.assertEqual(persisted.state, "consent_pending")

    def test_provider_call_sid_cannot_be_rebound_to_another_tenant(self):
        call_sid = "CA-global-binding-001"
        begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid=call_sid,
            direction="inbound",
            policy=resolve_voice_consent_policy(self.tenant_a),
        )

        with self.assertRaisesRegex(
            VoiceConsentLifecycleError, "call_tenant_mismatch"
        ):
            begin_voice_consent(
                tenant_id=self.tenant_b.id,
                call_sid=call_sid,
                direction="inbound",
                policy=resolve_voice_consent_policy(self.tenant_b),
            )

        lifecycles = VoiceCallLifecycle.query.filter_by(
            provider="twilio", provider_call_sid=call_sid
        ).all()
        self.assertEqual(len(lifecycles), 1)
        self.assertEqual(lifecycles[0].tenant_id, self.tenant_a.id)

    def test_legacy_sender_fallback_never_uses_suffix_match(self):
        db.session.add(
            WhatsappNumero(
                numero_whatsapp="+9915550001234",
                user_id=self.owner_a.id,
                is_active=True,
            )
        )
        db.session.commit()

        with self.assertRaisesRegex(
            VoiceConsentLifecycleError, "sender_not_registered"
        ):
            resolve_authoritative_voice_tenant(
                from_number="+5492613168608",
                to_number="+15550001234",
                direction="inbound",
                config=self.app.config,
            )

        service = VoiceStreamService(_InboundSocket([]), app=self.app)
        self.assertFalse(
            service._resolve_context(
                "+5492613168608",
                "+15550001234",
                "CA-legacy-suffix-guard",
            )
        )
        self.assertIsNone(service.tenant_profile)

    def test_cross_tenant_consent_callback_cannot_grant_original_call(self):
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(),
        )
        private_phone = "+5492613168608"
        with self.assertLogs("routes.voice_routes", level="ERROR") as logs:
            response = self.client.post(
                f"/twilio/voice/consent?tenant={self.tenant_b.slug}",
                data={**self._payload(), "Digits": "1"},
            )

        self.assertNotIn("<Connect>", response.get_data(as_text=True))
        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.tenant_id, self.tenant_a.id)
        self.assertEqual(lifecycle.consent_status, "required")
        rendered = "\n".join(logs.output)
        self.assertIn("requested_tenant_mismatch", rendered)
        self.assertNotIn(private_phone, rendered)

    def test_transfer_requires_grant_and_exact_tenant_configured_e164_target(self):
        config = dict(self.tenant_a.configuracion or {})
        config["human_handoff_number"] = "+15559876543"
        self.tenant_a.configuracion = config
        db.session.commit()
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(call_sid="CA-transfer-001"),
        )
        self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**self._payload(call_sid="CA-transfer-001"), "Digits": "1"},
        )

        refused = self.client.post(
            f"/twilio/voice/transfer?tenant={self.tenant_a.slug}&target=%2B15550000000",
            data=self._payload(call_sid="CA-transfer-001"),
        )
        allowed = self.client.post(
            f"/twilio/voice/transfer?tenant={self.tenant_a.slug}&target=%2B15559876543",
            data=self._payload(call_sid="CA-transfer-001"),
        )

        self.assertNotIn("<Dial", refused.get_data(as_text=True))
        self.assertIn("<Dial>+15559876543</Dial>", allowed.get_data(as_text=True))

    def test_whatsapp_business_call_never_attempts_forbidden_pstn_transfer(self):
        config = dict(self.tenant_a.configuracion or {})
        config["human_handoff_number"] = "+15559876543"
        self.tenant_a.configuracion = config
        db.session.commit()
        call_sid = "CA-whatsapp-transfer-001"
        payload = self._whatsapp_payload(call_sid=call_sid)
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=payload,
        )
        self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**payload, "Digits": "1"},
        )

        with self.assertLogs("routes.voice_routes", level="WARNING") as logs:
            response = self.client.post(
                f"/twilio/voice/transfer?tenant={self.tenant_a.slug}&target=%2B15559876543",
                data=payload,
            )

        rendered_logs = "\n".join(logs.output)
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<Dial", body)
        self.assertIn("whatsapp_pstn_bridge_forbidden", rendered_logs)
        self.assertNotIn("+5492613168608", rendered_logs)

    def test_legacy_voice_process_blocks_whatsapp_handoff_before_dial(self):
        config = dict(self.tenant_a.configuracion or {})
        config["human_handoff_number"] = "+15559876543"
        self.tenant_a.configuracion = config
        db.session.commit()
        call_sid = "CA-whatsapp-process-handoff"
        payload = self._whatsapp_payload(call_sid=call_sid)
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=payload,
        )
        self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**payload, "Digits": "1"},
        )

        handoff = {
            "type": "handoff",
            "text": "Te comunico con una persona.",
            "target": "+15559876543",
        }
        with patch(
            "routes.voice_routes.handle_voice_interaction",
            return_value=handoff,
        ), self.assertLogs("routes.voice_routes", level="WARNING") as logs:
            response = self.client.post(
                f"/voice/process?tenant={self.tenant_a.slug}",
                data={
                    **payload,
                    "SpeechResult": "quiero hablar con alguien",
                    "Confidence": "0.9",
                },
            )

        rendered_logs = "\n".join(logs.output)
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<Dial", body)
        self.assertIn("whatsapp_pstn_bridge_forbidden", rendered_logs)
        self.assertNotIn("+5492613168608", rendered_logs)

    def test_legacy_voice_process_keeps_authorized_phone_handoff(self):
        config = dict(self.tenant_a.configuracion or {})
        config["human_handoff_number"] = "+15559876543"
        self.tenant_a.configuracion = config
        db.session.commit()
        call_sid = "CA-phone-process-handoff"
        payload = self._payload(call_sid=call_sid)
        self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=payload,
        )
        self.client.post(
            f"/twilio/voice/consent?tenant={self.tenant_a.slug}",
            data={**payload, "Digits": "1"},
        )

        handoff = {
            "type": "handoff",
            "text": "Te comunico con una persona.",
            "target": "+15559876543",
        }
        with patch(
            "routes.voice_routes.handle_voice_interaction",
            return_value=handoff,
        ):
            response = self.client.post(
                f"/voice/process?tenant={self.tenant_a.slug}",
                data={
                    **payload,
                    "SpeechResult": "quiero hablar con alguien",
                    "Confidence": "0.9",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("<Dial>+15559876543</Dial>", response.get_data(as_text=True))

    def test_provider_status_is_monotonic_and_does_not_invent_connected(self):
        policy = resolve_voice_consent_policy(self.tenant_a)
        lifecycle = begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid="CA-status-001",
            direction="inbound",
            policy=policy,
        )
        record_voice_consent_decision(
            tenant_id=self.tenant_a.id,
            call_sid=lifecycle.provider_call_sid,
            decision="granted",
        )
        answered = record_voice_provider_status(
            tenant_id=self.tenant_a.id,
            call_sid=lifecycle.provider_call_sid,
            direction="inbound",
            policy=policy,
            provider_status="answered",
        )
        self.assertEqual(answered.state, "stream_authorized")
        self.assertNotIn(
            "connected",
            {event.state for event in VoiceCallLifecycleEvent.query.all()},
        )

        completed = record_voice_provider_status(
            tenant_id=self.tenant_a.id,
            call_sid=lifecycle.provider_call_sid,
            direction="inbound",
            policy=policy,
            provider_status="completed",
        )
        terminal_at = completed.terminal_at
        out_of_order = record_voice_provider_status(
            tenant_id=self.tenant_a.id,
            call_sid=lifecycle.provider_call_sid,
            direction="inbound",
            policy=policy,
            provider_status="ringing",
        )
        self.assertEqual(out_of_order.state, "completed")
        self.assertEqual(out_of_order.last_provider_status, "completed")
        self.assertEqual(out_of_order.terminal_at, terminal_at)
        ignored = VoiceCallLifecycleEvent.query.filter_by(
            provider_status="ringing"
        ).one()
        self.assertEqual(ignored.reason_code, "provider_status_ignored_terminal")

    def test_terminal_status_arriving_before_inbound_cannot_be_reopened(self):
        policy = resolve_voice_consent_policy(self.tenant_a)
        record_voice_provider_status(
            tenant_id=self.tenant_a.id,
            call_sid="CA-status-before-inbound",
            direction="inbound",
            policy=policy,
            provider_status="completed",
        )

        response = self.client.post(
            f"/twilio/voice/inbound?tenant={self.tenant_a.slug}",
            data=self._payload(call_sid="CA-status-before-inbound"),
        )

        body = response.get_data(as_text=True)
        self.assertNotIn("<Gather", body)
        self.assertNotIn("<Connect>", body)
        lifecycle = VoiceCallLifecycle.query.one()
        self.assertEqual(lifecycle.state, "completed")
        self.assertIsNotNone(lifecycle.terminal_at)

    def test_audit_insert_and_state_transition_roll_back_together(self):
        policy = resolve_voice_consent_policy(self.tenant_a)
        lifecycle = begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid="CA-rollback-001",
            direction="inbound",
            policy=policy,
        )
        with patch.object(db.session, "commit", side_effect=RuntimeError("audit down")):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                record_voice_consent_decision(
                    tenant_id=self.tenant_a.id,
                    call_sid=lifecycle.provider_call_sid,
                    decision="granted",
                )

        db.session.expire_all()
        persisted = VoiceCallLifecycle.query.filter_by(id=lifecycle.id).one()
        self.assertEqual(persisted.state, "consent_pending")
        self.assertEqual(persisted.consent_status, "required")
        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(
                reason_code="explicit_dtmf_consent_granted"
            ).count(),
            0,
        )

    def test_stream_claim_audit_failure_rolls_back_and_can_retry(self):
        policy = resolve_voice_consent_policy(self.tenant_a)
        lifecycle = begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid="CA-claim-rollback",
            direction="inbound",
            policy=policy,
        )
        record_voice_consent_decision(
            tenant_id=self.tenant_a.id,
            call_sid=lifecycle.provider_call_sid,
            decision="granted",
        )

        with patch.object(db.session, "commit", side_effect=RuntimeError("audit down")):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                claim_voice_stream_authorization(
                    tenant_id=self.tenant_a.id,
                    call_sid=lifecycle.provider_call_sid,
                    policy_version=policy.version,
                )

        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(event_key="stream:claimed").count(),
            0,
        )
        claim_voice_stream_authorization(
            tenant_id=self.tenant_a.id,
            call_sid=lifecycle.provider_call_sid,
            policy_version=policy.version,
        )
        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(event_key="stream:claimed").count(),
            1,
        )

    def test_websocket_never_calls_openai_without_durable_grant(self):
        policy = resolve_voice_consent_policy(self.tenant_a)
        begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid="CA-ws-no-consent",
            direction="inbound",
            policy=policy,
        )
        params = create_voice_stream_envelope(
            call_sid="CA-ws-no-consent",
            from_number="+5492613168608",
            to_number="+15551230001",
            tenant_slug=self.tenant_a.slug,
            vertical="pyme",
            config=self.app.config,
            nonce=f"nonce_{uuid.uuid4().hex}",
        )
        start = {
            "event": "start",
            "start": {
                "streamSid": "MZ-ws-no-consent",
                "callSid": "CA-ws-no-consent",
                "customParameters": params,
            },
        }
        socket = _InboundSocket([start])
        service = VoiceStreamService(socket, app=self.app)

        with patch("services.voice_stream_service.ws_connect") as ws_connect, patch.object(
            service, "_resolve_context"
        ) as resolve_context:
            service.run()

        ws_connect.assert_not_called()
        resolve_context.assert_not_called()
        self.assertTrue(socket.closed)

    def test_second_valid_envelope_for_same_call_cannot_open_openai(self):
        call_sid = "CA-ws-single-claim"
        policy = resolve_voice_consent_policy(self.tenant_a)
        begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid=call_sid,
            direction="inbound",
            policy=policy,
        )
        record_voice_consent_decision(
            tenant_id=self.tenant_a.id,
            call_sid=call_sid,
            decision="granted",
        )
        first_params = create_voice_stream_envelope(
            call_sid=call_sid,
            from_number="+5492613168608",
            to_number="+15551230001",
            tenant_slug=self.tenant_a.slug,
            vertical="pyme",
            config=self.app.config,
            nonce=f"nonce_{uuid.uuid4().hex}",
        )
        second_params = create_voice_stream_envelope(
            call_sid=call_sid,
            from_number="+5492613168608",
            to_number="+15551230001",
            tenant_slug=self.tenant_a.slug,
            vertical="pyme",
            config=self.app.config,
            nonce=f"nonce_{uuid.uuid4().hex}",
        )

        first_service = VoiceStreamService(_InboundSocket([]), app=self.app)
        self.assertTrue(first_service._authorize_durable_voice_consent(first_params))
        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(
                event_key="stream:claimed"
            ).count(),
            1,
        )

        second_start = {
            "event": "start",
            "start": {
                "streamSid": "MZ-ws-single-claim-2",
                "callSid": call_sid,
                "customParameters": second_params,
            },
        }
        second_socket = _InboundSocket([second_start])
        second_service = VoiceStreamService(second_socket, app=self.app)
        with patch("services.voice_stream_service.ws_connect") as ws_connect, patch.object(
            second_service, "_resolve_context"
        ) as resolve_context:
            second_service.run()

        ws_connect.assert_not_called()
        resolve_context.assert_not_called()
        self.assertTrue(second_socket.closed)
        self.assertEqual(
            VoiceCallLifecycleEvent.query.filter_by(
                event_key="stream:claimed"
            ).count(),
            1,
        )

    def test_feature_flag_off_never_opens_openai_even_with_signed_envelope(self):
        self.app.config["ENABLE_VOICE_CONSENT_LIFECYCLE_V1"] = False
        params = create_voice_stream_envelope(
            call_sid="CA-ws-flag-off",
            from_number="+5492613168608",
            to_number="+15551230001",
            tenant_slug=self.tenant_a.slug,
            config=self.app.config,
            nonce=f"nonce_{uuid.uuid4().hex}",
        )
        start = {
            "event": "start",
            "start": {
                "streamSid": "MZ-ws-flag-off",
                "callSid": "CA-ws-flag-off",
                "customParameters": params,
            },
        }
        socket = _InboundSocket([start])

        with patch("services.voice_stream_service.ws_connect") as ws_connect:
            VoiceStreamService(socket, app=self.app).run()

        ws_connect.assert_not_called()
        self.assertTrue(socket.closed)

    def test_recording_constraint_rejects_direct_enable(self):
        lifecycle = begin_voice_consent(
            tenant_id=self.tenant_a.id,
            call_sid="CA-recording-001",
            direction="inbound",
            policy=resolve_voice_consent_policy(self.tenant_a),
        )
        lifecycle.recording_enabled = True
        with self.assertRaises(Exception):
            db.session.commit()
        db.session.rollback()

        persisted = VoiceCallLifecycle.query.filter_by(id=lifecycle.id).one()
        self.assertFalse(persisted.recording_allowed)
        self.assertFalse(persisted.recording_enabled)


if __name__ == "__main__":
    unittest.main()
