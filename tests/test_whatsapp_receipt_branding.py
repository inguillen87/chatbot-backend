"""Campaign headers use organization configuration, not citizen attachments."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import unittest

from extensions import db
from models import MunicipioTicket, WhatsAppContactState
from routes import whatsapp_webhook as webhook
from services.outbox_execution_budget import OutboxExecutionBudgetExceeded
from services.whatsapp_receipt_branding import (
    public_whatsapp_receipt_image_url,
    resolve_whatsapp_receipt_header,
)
from services.whatsapp_receipts import build_claim_created_template_pre_message
from tests import test_whatsapp_welcome_optional_audio as welcome_fixture


CAMPAIGN = "https://brand.example.test/campaign.png"
LOGO = "https://brand.example.test/logo.jpg"
PRIVATE_ATTACHMENT = "https://private.example.test/claim-photo.jpg?signature=fixture-private"


class ReceiptBrandingResolverTestCase(unittest.TestCase):
    def tenant(self, config=None, **kwargs):
        return SimpleNamespace(id=1, configuracion=config or {}, logo_url=kwargs.get("logo", LOGO))

    def test_explicit_header_then_legacy_tenant_promo_then_logo(self):
        for config, expected in (
            ({"whatsapp_receipt_header": {"image_url": CAMPAIGN}, "promo_image_url": "https://brand.example.test/legacy.png"}, CAMPAIGN),
            ({"promo_image_url": CAMPAIGN}, CAMPAIGN),
            ({"promo_section": {"image_url": CAMPAIGN}}, CAMPAIGN),
            ({}, LOGO),
        ):
            with self.subTest(config=config):
                self.assertEqual(resolve_whatsapp_receipt_header(self.tenant(config)), expected)

    def test_explicit_disable_blocks_promo_and_logo(self):
        for header in (False, {"enabled": False}, {"enabled": "false"}, "invalid"):
            with self.subTest(header=header):
                tenant = self.tenant({"whatsapp_receipt_header": header, "promo_image_url": CAMPAIGN})
                self.assertIsNone(resolve_whatsapp_receipt_header(tenant))

    def test_legacy_null_header_inherits_tenant_promo_or_logo(self):
        tenant = self.tenant({"whatsapp_receipt_header": None, "promo_image_url": CAMPAIGN})
        self.assertEqual(resolve_whatsapp_receipt_header(tenant), CAMPAIGN)
        tenant.configuracion = {"whatsapp_receipt_header": None}
        self.assertEqual(resolve_whatsapp_receipt_header(tenant), LOGO)

    def test_missing_tenant_or_no_public_brand_is_text_only(self):
        for tenant in (None, SimpleNamespace(id=None), SimpleNamespace(id=True), SimpleNamespace(id="1"), self.tenant(logo=None)):
            with self.subTest(tenant=tenant):
                self.assertIsNone(resolve_whatsapp_receipt_header(tenant))

    def test_another_organization_does_not_inherit_campaign_or_default(self):
        tenant_a = self.tenant({"promo_section": {"image_url": CAMPAIGN}})
        tenant_b = SimpleNamespace(id=2, configuracion={}, logo_url=None, image_url=PRIVATE_ATTACHMENT)
        self.assertEqual(resolve_whatsapp_receipt_header(tenant_a), CAMPAIGN)
        self.assertIsNone(resolve_whatsapp_receipt_header(tenant_b))

    def test_private_capability_and_non_image_URL_shapes_are_rejected(self):
        invalid = (
            PRIVATE_ATTACHMENT, "http://brand.example.test/logo.jpg", "https://user:secret@brand.example.test/logo.jpg",
            "https://brand.example.test/logo.jpg#secret", "file:///private/claim.jpg", "data:image/png;base64,fixture",
            "https://localhost/logo.jpg", "https://127.0.0.1/logo.jpg", "https://10.0.0.1/logo.jpg",
            "https://brand.internal/logo.jpg", "https://brand.example.test:8443/logo.jpg", "https://brand.example.test/logo.svg",
            "https://brand.example.test/audio.mp3", "https://brand.example.test/../logo.jpg", "https://brand.example.test/bad\nlogo.jpg",
        )
        for image in invalid:
            with self.subTest(image=image):
                self.assertIsNone(resolve_whatsapp_receipt_header(self.tenant({"promo_image_url": image}, logo=None)))

    def test_invalid_campaign_can_fall_back_to_owned_public_logo(self):
        self.assertEqual(resolve_whatsapp_receipt_header(self.tenant({"promo_image_url": PRIVATE_ATTACHMENT})), LOGO)

    def test_canonical_host_and_decoded_path_validation_is_shared(self):
        invalid = (
            "https://localhost./logo.jpg", "https://nested.localhost./logo.jpg",
            "https://127.0.0.1./logo.jpg", "https://127.1/logo.jpg",
            "https://127.000.000.001/logo.jpg", "https://0x7f.0.0.1/logo.jpg",
            "https://2130706433/logo.jpg", "https://brand.invalid/logo.jpg",
            "https://brand.example.test/%2e%2e/logo.jpg",
            "https://brand.example.test/%2e/logo.jpg",
            "https://brand.example.test/dir%2f..%2flogo.jpg",
            "https://brand.example.test/dir%5clogo.jpg",
            "https://brand.example.test/%00logo.jpg",
            "https://brand.example.test/logo.jpg?token=fixture",
            "https://brand.example.test/logo.jpg#fixture",
            "\nhttps://brand.example.test/logo.jpg",
        )
        for image in invalid:
            with self.subTest(image=image):
                self.assertIsNone(public_whatsapp_receipt_image_url(image))
                tenant = self.tenant({"promo_image_url": image}, logo=None)
                self.assertIsNone(resolve_whatsapp_receipt_header(tenant))
        self.assertEqual(public_whatsapp_receipt_image_url(CAMPAIGN), CAMPAIGN)

    def test_shared_writer_validator_requires_absolute_url(self):
        self.assertIsNone(public_whatsapp_receipt_image_url(
            "/static/campaign.png", public_base_url="https://api.example.test"
        ))
        self.assertEqual(public_whatsapp_receipt_image_url(
            "/static/campaign.png", public_base_url="https://api.example.test",
            allow_relative_static=True,
        ), "https://api.example.test/static/campaign.png")

    def test_invalid_explicit_override_does_not_fall_back_to_campaign_or_logo(self):
        invalid_headers = (
            {}, {"enabled": True}, {"image_url": PRIVATE_ATTACHMENT},
            {"enabled": True, "image_url": ""},
            {"enabled": True, "image_url": "https://127.1/campaign.png"},
            {"enabled": True, "image_url": CAMPAIGN, "unexpected": "fixture"},
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                tenant = self.tenant({"whatsapp_receipt_header": header,
                                      "promo_image_url": CAMPAIGN})
                self.assertIsNone(resolve_whatsapp_receipt_header(tenant))

    def test_explicit_override_requires_absolute_url_even_with_public_origin(self):
        tenant = self.tenant({"whatsapp_receipt_header": {"enabled": True, "image_url": "/static/campaign.png"},
                              "promo_image_url": CAMPAIGN})
        self.assertIsNone(resolve_whatsapp_receipt_header(tenant, public_base_url="https://api.example.test"))

    def test_legacy_static_asset_requires_public_HTTPS_origin(self):
        tenant = self.tenant({"promo_image_url": "/static/campaign.png"}, logo=None)
        self.assertEqual(resolve_whatsapp_receipt_header(tenant, public_base_url="https://api.example.test"), "https://api.example.test/static/campaign.png")
        for origin in ("", "http://localhost", "https://api.example.test/api", "https://api.example.test?secret=fixture"):
            with self.subTest(origin=origin):
                self.assertIsNone(resolve_whatsapp_receipt_header(tenant, public_base_url=origin))


class ReceiptBrandingSignedRouteTestCase(unittest.TestCase):
    _signature = staticmethod(welcome_fixture.WelcomeOptionalAudioSignedRouteTestCase._signature)
    _payload = staticmethod(welcome_fixture.WelcomeOptionalAudioSignedRouteTestCase._payload)
    _post = welcome_fixture.WelcomeOptionalAudioSignedRouteTestCase._post
    setUp = welcome_fixture.WelcomeOptionalAudioSignedRouteTestCase.setUp
    tearDown = welcome_fixture.WelcomeOptionalAudioSignedRouteTestCase.tearDown

    def _configure_campaign(self, header=None):
        config = dict(self.tenant.configuracion)
        config["whatsapp_receipt_header"] = {"enabled": True, "image_url": CAMPAIGN} if header is None else header
        self.tenant.configuracion = config
        db.session.commit()

    def _reply(self, response_payload, sid="SM_fixture_receipt", *, template=None):
        payload = self._payload(sid, body="Solicito informacion municipal")
        payload["ProfileName"] = "Persona Fixture"
        with patch.object(webhook, "responder_chatboc", return_value=response_payload), patch.object(webhook, "_resolve_approved_whatsapp_template_sid", return_value=template):
            response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        return [call.kwargs for call in self.provider.messages.create.call_args_list]

    def _generic_receipt(self, body):
        return {"whatsapp_receipt": {"body_text": body, "media_url": PRIVATE_ATTACHMENT}, "image_url": PRIVATE_ATTACHMENT, "skip_audio_generation": True, "generar_audio": False}

    def _compact_receipt(self):
        primary = build_claim_created_template_pre_message(ticket_nro="M-12345", categoria="Fixture", consulta_pin="123456", base_chat_url="https://app.example.test/chat", nombre="Private fixture name", direccion="Private fixture address", within_24h_window=True)
        return {"_twilio_pre_messages": [primary], "message_body": "Segui agregando evidencia al mismo reclamo.", "skip_audio_generation": True, "generar_audio": False}

    def _register_window(self, hours_ago=0, recipient=None):
        db.session.add(WhatsAppContactState(tenant_id=self.tenant.id, provider_sender_id=self.sender.id, recipient=recipient or "+15550909090", last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago)))
        db.session.commit()

    def _dispatch_template_direct(self):
        payload = self._compact_receipt()
        payload["_twilio_pre_messages"][0]["content_sid"] = "HX_fixture_approved_text_receipt"
        webhook._dispatch_twilio_pre_messages(self.provider, "whatsapp:+15550103030", "whatsapp:+15550909090", payload, lambda value: value, tenant_profile=self.tenant, provider_sender=self.sender)

    def test_short_receipt_has_only_the_tenant_campaign_image(self):
        self._configure_campaign()
        sent = self._reply(self._generic_receipt("Comprobante sintetico"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["media_url"], [CAMPAIGN])
        self.assertNotIn("fixture-private", str(sent))
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_long_first_chunk_keeps_image_once_and_rest_stays_text(self):
        self._configure_campaign()
        sent = self._reply(self._generic_receipt("Detalle sintetico del comprobante. " * 90))
        self.assertEqual(sent[0]["media_url"], [CAMPAIGN])
        self.assertEqual(sum(bool(row.get("media_url")) for row in sent), 1)
        self.assertTrue(all("media_url" not in row for row in sent[1:]))
        self.assertNotIn("fixture-private", str(sent))

    def test_no_tenant_campaign_ignores_receipt_photo_and_shared_default(self):
        sent = self._reply(self._generic_receipt("Comprobante sin campaña"))
        self.assertTrue(all("media_url" not in row for row in sent))
        self.assertNotIn("fixture-private", str(sent))
        self.assertNotIn("Punto Limpio", str(sent))

    def test_confirmed_pyme_order_uses_its_own_brand_without_private_product_image(self):
        self.owner.tipo_chat = "pyme"
        self.owner.rol = "empresa"
        self.tenant.tipo = "pyme"
        self.tenant.pyme_id = self.owner.id
        self.tenant.municipio_id = None
        self.tenant.logo_url = LOGO
        db.session.commit()
        response_payload = {"fuente": "pyme_pedido_confirmado", "success": True, "message_body": "Pedido de prueba confirmado", "message_type": "text", "options_list": [], "data": {"pedido_id": 55}, "image_url": PRIVATE_ATTACHMENT, "skip_audio_generation": True}
        sent = self._reply(response_payload)
        # The existing order adapter restores its interactive catalog buttons.
        # Its image belongs in the interactive header, not a second attachment.
        interactive = json.loads(sent[0]["persistent_action"][0].removeprefix("whatsapp:"))
        self.assertEqual(interactive["header"]["image"]["link"], LOGO)
        self.assertNotIn("media_url", sent[0])
        self.assertIn("Pedido de prueba", sent[0]["body"])
        self.assertNotIn("fixture-private", str(sent))
        self.assertNotIn("Punto Limpio", str(sent))

    def test_disabled_header_keeps_receipt_text_without_media(self):
        self._configure_campaign(False)
        self.tenant.logo_url = LOGO
        db.session.commit()
        sent = self._reply(self._generic_receipt("Comprobante sin cabecera"))
        self.assertTrue(all("media_url" not in row for row in sent))

    def test_compact_freeform_receipt_is_pii_free_image_caption_before_followup(self):
        self._configure_campaign()
        sent = self._reply(self._compact_receipt())
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["media_url"], [CAMPAIGN])
        self.assertIn("Reclamo registrado", sent[0]["body"])
        self.assertIn("#pin=123456", sent[0]["body"])
        self.assertNotIn("Private fixture", str(sent))
        self.assertNotIn("media_url", sent[1])
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_approved_template_gets_separate_image_first_inside_window(self):
        self._configure_campaign()
        sent = self._reply(self._compact_receipt(), template="HX_fixture_approved_text_receipt")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0]["media_url"], [CAMPAIGN])
        self.assertEqual(sent[0]["body"], "")
        self.assertEqual(sent[1]["content_sid"], "HX_fixture_approved_text_receipt")
        self.assertNotIn("media_url", sent[1])
        self.assertNotIn("body", sent[1])
        self.assertNotIn("media_url", sent[2])
        self.assertNotIn("Private fixture", str(sent))

    def test_expired_or_future_window_does_not_create_template_media_capability(self):
        self._configure_campaign()
        for hours_ago in (25, -1):
            with self.subTest(hours_ago=hours_ago):
                WhatsAppContactState.query.delete()
                self._register_window(hours_ago)
                self.provider.messages.create.reset_mock()
                self._dispatch_template_direct()
                self.assertEqual(self.provider.messages.create.call_count, 1)
                self.assertIn("content_sid", self.provider.messages.create.call_args.kwargs)
                self.assertNotIn("media_url", self.provider.messages.create.call_args.kwargs)

    def test_wrong_sender_or_recipient_cannot_use_another_contact_window(self):
        self._configure_campaign()
        self._register_window()
        other_sender = SimpleNamespace(id=self.sender.id + 1, tenant_id=self.tenant.id + 1)
        self.assertIsNone(webhook._receipt_public_header_for_window(self.tenant, other_sender, "whatsapp:+15550909090"))
        self.assertIsNone(webhook._receipt_public_header_for_window(self.tenant, self.sender, "whatsapp:+15550000000"))
        self.assertEqual(webhook._receipt_public_header_for_window(self.tenant, self.sender, "whatsapp:+15550909090"), CAMPAIGN)

    def test_header_failure_preserves_the_existing_template_and_logs_type_only(self):
        self._configure_campaign()
        self._register_window()
        with patch.object(webhook, "_send_twilio_message", side_effect=[RuntimeError("private signed URL must not be logged"), SimpleNamespace(sid="SM_fixture_template")]) as send:
            with self.assertLogs(self.app.logger, level="WARNING") as captured:
                self._dispatch_template_direct()
        self.assertEqual(send.call_count, 2)
        self.assertIn("content_sid", send.call_args.kwargs)
        self.assertIn("RuntimeError", "\n".join(captured.output))
        self.assertNotIn("private signed URL", "\n".join(captured.output))

    def test_header_budget_control_propagates_before_template_submission(self):
        self._configure_campaign()
        self._register_window()
        with patch.object(webhook, "_send_twilio_message", side_effect=OutboxExecutionBudgetExceeded("outbox_execution_budget_exhausted")) as send:
            with self.assertRaises(OutboxExecutionBudgetExceeded):
                self._dispatch_template_direct()
        self.assertEqual(send.call_count, 1)

    def test_policy_rejection_skips_header_but_keeps_approved_template(self):
        self._configure_campaign()
        self._register_window()
        with patch.object(webhook, "_can_send_whatsapp_freeform_pre_message", return_value=(False, "quiet_hours")):
            self._dispatch_template_direct()
        self.assertEqual(self.provider.messages.create.call_count, 1)
        self.assertIn("content_sid", self.provider.messages.create.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
