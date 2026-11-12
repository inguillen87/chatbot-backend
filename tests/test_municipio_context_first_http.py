"""Real webhook/router with synthetic identity, GPS and SDK output, no providers."""
import unittest
from unittest.mock import patch

from tests.test_municipio_explicit_claim_offline import ExplicitClaimOfflineHttpTest
from models import ArchivoAdjunto, MunicipioTicket, TenantTicket, db
from services.constants import CONTEXTO_MUNICIPIO


class ContextFirstHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ExplicitClaimOfflineHttpTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.category("Arbolado")
        self.sequence = 0
        geocoder = patch("services.herramientas_municipio.obtener_direccion_de_coordenadas", return_value=None)
        geocoder.start()
        self.addCleanup(geocoder.stop)
        reverse = patch("routes.whatsapp_webhook.geocodificar_inversa_llm", return_value=None)
        reverse.start()
        self.addCleanup(reverse.stop)

    def post(self, fields):
        self.sequence += 1
        response = self.fixture.fixture.client.post(
            "/webhook/whatsapp",
            data={
                "To": "whatsapp:" + self.fixture.fixture.test_whatsapp_number_str,
                "From": "whatsapp:" + self.fixture.fixture.test_user_number_str,
                "MessageSid": "SM_CONTEXT_GPS_" + str(self.sequence),
                "ProfileName": "Persona Prueba", **fields,
            },
            headers={"X-Twilio-Signature": "synthetic-fixture-signature"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(self.fixture.provider_calls, 0)
        self.fixture.complaint_extractor.assert_not_called()
        return (self.fixture.session().context_data or {}).get(CONTEXTO_MUNICIPIO, {})

    def location_post(self, caption, address="Calle de Prueba 123"):
        fields = {"Body": caption, "Latitude": "-33.14", "Longitude": "-68.49"}
        if address:
            fields["Address"] = address
        return self.post(fields)

    def test_explicit_claim_caption_and_gps_reach_draft_without_repeating_intent_or_address(self):
        caption = "quiero hacer un reclamo por un arbol caido"
        ctx = self.location_post(caption)
        flow = ctx.get("reclamo_flow_v2", {})
        self.assertNotEqual(ctx.get("estado_conversacion"), "ESPERANDO_INTENCION_UBICACION")
        self.assertIn(flow.get("state"), {"ESPERANDO_FOTO", "ESPERANDO_DATOS_CONTACTO", "ESPERANDO_CONFIRMACION"})
        self.assertEqual(flow["datos_reclamo"]["categoria"], "Arbolado")
        self.assertEqual(flow["datos_reclamo"]["descripcion"], caption)
        self.assertEqual(flow["datos_reclamo"]["direccion"], "Calle de Prueba 123")
        self.assertEqual(flow["datos_reclamo"]["coordenadas"], {"lat": -33.14, "lon": -68.49})

    def test_location_without_intent_does_not_start_or_confirm_a_claim(self):
        ctx = self.location_post("")
        self.assertNotIn("reclamo_flow_v2", ctx)
        self.assertEqual(ctx.get("estado_conversacion"), "ESPERANDO_INTENCION_UBICACION")

    def test_location_then_explicit_text_keeps_previous_turn_location(self):
        self.location_post("")
        ctx = self.post({"Body": "quiero hacer un reclamo por un arbol caido"})
        flow = ctx.get("reclamo_flow_v2", {})
        self.assertEqual(flow["datos_reclamo"]["direccion"], "Calle de Prueba 123")
        self.assertEqual(flow["datos_reclamo"]["coordenadas"], {"lat": -33.14, "lon": -68.49})
        self.assertNotIn(flow["state"], {"ESPERANDO_DIRECCION", "ESPERANDO_CATEGORIA"})

    def test_precise_gps_without_geocoded_address_then_text_uses_declared_location(self):
        self.location_post("", address=None)
        ctx = self.post({"Body": "quiero hacer un reclamo por un arbol caido"})
        fields = ctx["reclamo_flow_v2"]["datos_reclamo"]
        self.assertEqual(fields["direccion"], "Ubicación compartida")
        self.assertEqual(fields["coordenadas"], {"lat": -33.14, "lon": -68.49})
        self.assertIn("query=-33.14,-68.49", fields["map_search_url"])
        self.assertNotEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_DIRECCION")

    def audio_post(self, transcript):
        def store_synthetic_attachment(**kwargs):
            attachment = ArchivoAdjunto(
                user_id=kwargs.get("user_id"), session_id=kwargs.get("session_id"),
                filename="context-note.ogg", nombre_original="context-note.ogg",
                url="https://storage.example.test/context-note.ogg", mime="audio/ogg", tipo="audio",
            )
            db.session.add(attachment)
            db.session.flush()
            return attachment
        with (
            patch("routes.whatsapp_webhook._download_twilio_media", return_value=b"bounded-synthetic-audio") as download,
            patch("routes.whatsapp_webhook.create_attachment_with_thumbnail", side_effect=store_synthetic_attachment),
            patch("services.audio_transcription_service.transcribe_audio_bytes", return_value=transcript) as transcribe,
            patch("routes.whatsapp_webhook.clasificar_adjunto_whatsapp", return_value={}),
        ):
            ctx = self.post({"Body": "", "NumMedia": "1", "MediaUrl0": "https://media.example.test/context-note.ogg", "MediaContentType0": "audio/ogg"})
        download.assert_called_once()
        transcribe.assert_called_once()
        return ctx

    def test_location_then_synthetic_transcribed_audio_reaches_same_draft(self):
        self.location_post("")
        transcript = "quiero hacer un reclamo por un arbol caido"
        ctx = self.audio_post(transcript)
        fields = ctx["reclamo_flow_v2"]["datos_reclamo"]
        self.assertEqual(fields["descripcion"], transcript)
        self.assertEqual(fields["direccion"], "Calle de Prueba 123")
        self.assertNotEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_DIRECCION")

    def test_audio_without_transcription_does_not_invent_claim_intent(self):
        self.location_post("")
        ctx = self.audio_post(None)
        self.assertNotIn("reclamo_flow_v2", ctx)
        self.assertEqual(ctx.get("estado_conversacion"), "ESPERANDO_INTENCION_UBICACION")
