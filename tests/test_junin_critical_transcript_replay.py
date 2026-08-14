import secrets
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app import create_app, db
from config import TestConfig
from models import ChatSessionContext, Rubro, TenantProfile, User
from services.municipio_responder import (
    CONTEXTO_MUNICIPIO,
    MUNICIPIO_RESPONSE_CACHE,
    ConversationState,
    responder_municipio,
)


class TestJuninCriticalTranscriptReplay(unittest.TestCase):
    """Replay the WhatsApp regression against the real municipal responder."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        MUNICIPIO_RESPONSE_CACHE.clear()

        rubro = Rubro(clave="municipio", nombre="Municipio")
        db.session.add(rubro)
        db.session.flush()

        self.owner = User(
            tipo_chat="municipio",
            rol="admin",
            email="qa-junin-owner@example.test",
            name="Municipalidad de Junín",
            rubro=rubro,
            municipio_id=1,
        )
        self.owner.set_password(secrets.token_urlsafe(24))
        self.viewer = User(
            email="qa-junin-citizen@example.test",
            name="Marcelo",
            direccion="Sarmiento 155, Junín Centro",
            telefono="+5492613168608",
        )
        self.viewer.set_password(secrets.token_urlsafe(24))
        db.session.add_all([self.owner, self.viewer])
        db.session.flush()

        self.tenant = TenantProfile(
            slug="junin-qa-regression",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=self.owner.id,
            is_active=True,
            configuracion={
                "assistant_name": "JUNI",
                "nombre": "Municipalidad de Junín",
                "ciudad": "Junín",
                "provincia": "Mendoza",
                "encuestas_enabled": True,
                "encuestas_base_url": "https://www.chatboc.ar",
                "base_chat_url": "https://www.chatboc.ar/chat",
            },
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        self.owner.tenant_slug = self.tenant.slug
        db.session.commit()

        def unexpected_external_call(*_args, **_kwargs):
            raise AssertionError("El replay no debe consultar LLM, red ni proveedores")

        self.external_guards = ExitStack()
        self.external_guards.enter_context(
            patch(
                "services.municipio_responder.cargar_configuracion_municipio",
                return_value=dict(self.tenant.configuracion),
            )
        )
        self.external_guards.enter_context(
            patch(
                "services.municipio_responder.list_public_encuestas_for_tenant",
                return_value=[],
            )
        )
        self.external_guards.enter_context(
            patch(
                "services.municipio_responder._resolve_encuestas_menu_media_urls",
                return_value=(None, []),
            )
        )
        for target in (
            "services.municipio_responder.handle_llm_interaction",
            "services.municipio_responder.llamar_gemini",
            "services.municipio_responder.extract_multiple_contact_details_llm",
            "services.municipio_responder.google_search",
        ):
            self.external_guards.enter_context(
                patch(target, side_effect=unexpected_external_call)
            )
        self.external_guards.enter_context(
            patch("services.promo_service.build_ticket_promo_section", return_value=None)
        )

    def tearDown(self):
        self.external_guards.close()
        MUNICIPIO_RESPONSE_CACHE.clear()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _new_chat_context(self, suffix: str) -> ChatSessionContext:
        chat_context = ChatSessionContext(
            chat_session_id=f"junin-critical-replay-{suffix}",
            user_id=self.owner.id,
            tenant_id=self.tenant.id,
            context_data={
                "profile_name": "Marcelo",
                CONTEXTO_MUNICIPIO: {
                    "contacto_usuario": {
                        "nombre": "Marcelo",
                        "dni": "32877851",
                        "email": "qa-junin-citizen@example.test",
                        "direccion": "Sarmiento 155, Junín Centro",
                        "telefono": "+5492613168608",
                    }
                },
            },
        )
        db.session.add(chat_context)
        db.session.commit()
        return chat_context

    def _respond(self, message, chat_context: ChatSessionContext):
        return responder_municipio(
            pregunta_original=message,
            owner_user=self.owner,
            rubro_obj=self.owner.rubro,
            viewer_user=self.viewer,
            chat_db_context=chat_context,
            channel="whatsapp",
            profile_name="Marcelo",
        )

    def test_transcript_routes_survey_typo_phrase_and_five_to_participation(self):
        chat_context = self._new_chat_context("surveys")

        greeting = self._respond("hola", chat_context)
        survey_main_option = next(
            option
            for option in greeting.get("options_list", [])
            if option.get("action_id") == "mostrar_menu_encuestas"
        )
        self.assertIn("Participación Ciudadana", survey_main_option["texto"])

        for turn in ("encuetas", "encuestas", "encuestas quiero", "5"):
            with self.subTest(turn=turn):
                response = self._respond(turn, chat_context)
                body = response.get("message_body", "")

                self.assertEqual(response.get("fuente"), "submenu_encuestas_v1")
                self.assertIn("participaci", body.casefold())
                self.assertNotIn("escribí tu sugerencia", body.casefold())
                self.assertNotIn("gracias por tu iniciativa", body.casefold())

                municipal_context = chat_context.context_data[CONTEXTO_MUNICIPIO]
                self.assertEqual(
                    municipal_context.get("estado_conversacion"),
                    ConversationState.ESPERANDO_SELECCION_DE_LISTA.name,
                )
                self.assertNotIn("datos_sugerencia", municipal_context)

    def test_suggestion_keeps_contact_address_and_returns_canonical_tracking(self):
        chat_context = self._new_chat_context("suggestion")
        expected_address = "Sarmiento 155, Junín Centro"
        suggestion = "Pintar banquitos en las plazas de Sarmiento y San Martín"

        start = self._respond({"action": "enviar_sugerencia"}, chat_context)
        self.assertEqual(start.get("fuente"), "handler_enviar_sugerencia")

        confirmation = self._respond(suggestion, chat_context)
        self.assertEqual(confirmation.get("fuente"), "pide_confirmacion_sugerencia")
        self.assertIn(expected_address, confirmation.get("message_body", ""))

        municipal_context = chat_context.context_data[CONTEXTO_MUNICIPIO]
        stored = municipal_context["datos_sugerencia"]
        self.assertEqual(stored.get("descripcion"), suggestion)
        self.assertEqual(stored.get("direccion"), expected_address)
        self.assertNotEqual(stored.get("direccion"), suggestion)

        with patch(
            "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket",
            return_value={
                "id": 897013,
                "nro_ticket": "897013",
                "consulta_pin": "115474",
            },
        ) as create_ticket:
            receipt = self._respond(
                {"action": "confirmar_sugerencia_si"},
                chat_context,
            )

        ticket_data = create_ticket.call_args.kwargs["ticket_data"]
        self.assertEqual(ticket_data["detalles"], suggestion)
        self.assertEqual(ticket_data["direccion_contacto"], expected_address)
        self.assertNotEqual(ticket_data["direccion_contacto"], suggestion)

        expected_url = (
            "https://www.chatboc.ar/tracking/claim/897013#pin=115474"
        )
        body = receipt.get("message_body", "")
        self.assertTrue(receipt.get("success"))
        self.assertEqual(receipt.get("fuente"), "sugerencia_confirmada")
        self.assertIn(expected_url, body)
        self.assertIn("115474", body)
        self.assertNotIn("/chat/chat/", body)


if __name__ == "__main__":
    unittest.main()
