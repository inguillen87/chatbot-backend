from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import unittest
from unittest.mock import patch

import jwt
from sqlalchemy import event

from app import create_app, db
from config import Config
from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    ChatSessionContext,
    Rubro,
    TenantProfile,
    User,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.encuestas_service import EncuestaError, save_respuesta as real_save_respuesta
from services.survey_governance import create_release, publish_release
from services.survey_response_provenance import (
    SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
    is_trusted_demo_seed_response,
)
from services.municipio_responder import (
    MUNICIPIO_RESPONSE_CACHE,
    _get_encuestas_menu,
    handle_main_menu_action,
    responder_municipio,
)
from services.response_formatter import build_interactive_response
from services.whatsapp_survey_conversation import (
    WHATSAPP_SURVEY_FLOW_STATE_KEY,
    _live_results,
    _load_instrument,
    _shared_location_from_context,
    handle_whatsapp_survey_flow_turn,
    start_whatsapp_survey_flow,
)
from tests.junin_product_flow_support import mark_junin_jurisdiction_verified


class WhatsAppSurveyConversationTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    PUBLIC_ENCUESTAS_CANONICAL_BASE_URL = "https://www.chatboc.ar"


class WhatsAppSurveyConversationTest(unittest.TestCase):
    """Exercise the real governed models and production persistence service."""

    def setUp(self):
        self.app = create_app(WhatsAppSurveyConversationTestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        rubro = Rubro(clave="municipio", nombre="Municipio")
        db.session.add(rubro)
        db.session.flush()
        self.owner = User(
            name="Administración municipal",
            email="owner-whatsapp-survey@example.test",
            rol="admin",
            tipo_chat="municipio",
            rubro=rubro,
        )
        self.owner.set_password(secrets.token_urlsafe(24))
        self.viewer = User(
            name="Ciudadana de prueba",
            email="citizen-whatsapp-survey@example.test",
            telefono="+5492634123456",
        )
        self.viewer.set_password(secrets.token_urlsafe(24))
        db.session.add_all([self.owner, self.viewer])
        db.session.flush()
        self.tenant = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junín QA",
            tipo="municipio",
            municipio_id=self.owner.id,
            is_active=True,
            configuracion={"encuestas_enabled": True},
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        self.owner.tenant_slug = self.tenant.slug
        db.session.commit()

    def _admin_headers(self) -> dict[str, str]:
        token = jwt.encode(
            {
                "user_id": self.owner.id,
                "rol": self.owner.rol,
                "tenant_slug": self.tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    def tearDown(self):
        MUNICIPIO_RESPONSE_CACHE.clear()
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @staticmethod
    def _release_policy(*, mode: str = "open") -> dict:
        public_text = (
            "Declaro que participo voluntariamente en esta consulta ciudadana no "
            "vinculante y autorizo el uso agregado de mi respuesta."
        )
        return {
            "eligibility_policy": {
                "policy_version": "eligibility-junin-wa-2026.1",
                "mode": mode,
                "declarations": (
                    ["resident_attested"] if mode == "self_attested" else []
                ),
                "human_review_required": True,
                "automated_decision": False,
            },
            "consent_policy": {
                "policy_version": "consent-junin-wa-2026.1",
                "public_text": public_text,
                "text_sha256": hashlib.sha256(public_text.encode("utf-8")).hexdigest(),
                "required": True,
            },
            "decision_rules": {
                "quorum": {"type": "none", "value": None},
                "tie": {"procedure": "human_review"},
                "challenge": {
                    "enabled": True,
                    "window_hours": 72,
                    "procedure": "human_review",
                },
                "human_review_required": True,
                "declarative_only": True,
            },
        }

    def _create_governed_survey(
        self,
        *,
        tenant: TenantProfile | None = None,
        slug: str = "gestion-junin-si-no",
        eligibility_mode: str = "open",
        incompatible: bool = False,
        vote_option_count: int = 2,
        privacy_mode: str = "legacy",
    ) -> EncEncuesta:
        scoped_tenant = tenant or self.tenant
        now = datetime.now(timezone.utc)
        source_anonymous = privacy_mode == "source_anonymous"
        if source_anonymous:
            self.app.config["SURVEY_IDENTITY_HMAC_SECRET_V1"] = (
                "whatsapp-source-anonymous-test-secret-v1"
            )
        survey = EncEncuesta(
            tenant_id=scoped_tenant.id,
            slug=slug,
            titulo="Votación ciudadana sobre transparencia de gestión",
            descripcion=(
                "Consulta no vinculante sobre un tablero mensual de la gestión "
                "de la Municipalidad de Junín, Mendoza."
            ),
            tipo="votacion",
            estado="borrador",
            # SQLite drops tzinfo; keep enough margin for the application's
            # Argentina-local naive datetime compatibility rule.
            inicio_at=now - timedelta(days=1),
            fin_at=now + timedelta(days=10),
            politica_unicidad="por_phone",
            anonimo_permitido=True,
            es_votacion_envivo=True,
            mostrar_resultados_envivo=True,
            privacy_mode=privacy_mode,
            privacy_policy_version=(
                "privacy-whatsapp-2026.1" if source_anonymous else None
            ),
            privacy_policy_url=(
                "https://www.chatboc.ar/privacidad/privacy-whatsapp-2026.1"
                if source_anonymous
                else None
            ),
            privacy_consent_required=source_anonymous,
            response_retention_days=365 if source_anonymous else None,
            puntos_recompensa=0,
        )
        if incompatible:
            survey.preguntas = [
                EncPregunta(
                    orden=1,
                    tipo="abierta",
                    texto="¿Qué mejorarías?",
                    obligatoria=True,
                )
            ]
        else:
            vote = EncPregunta(
                orden=1,
                logical_ref="vote:monthly-dashboard",
                tipo="opcion_unica",
                texto=(
                    "¿Estás de acuerdo con publicar un tablero mensual de compromisos "
                    "e indicadores?"
                ),
                obligatoria=True,
            )
            vote.opciones = (
                [
                    EncOpcion(orden=1, texto="Sí", valor="si"),
                    EncOpcion(orden=2, texto="No", valor="no"),
                ]
                if vote_option_count == 2
                else [
                    EncOpcion(
                        orden=index,
                        texto=f"Alternativa ciudadana número {index}",
                        valor=f"alternativa_{index}",
                    )
                    for index in range(1, vote_option_count + 1)
                ]
            )
            city = EncPregunta(
                orden=2,
                logical_ref="demographic:city",
                tipo="opcion_unica",
                texto="¿En qué distrito vivís?",
                obligatoria=True,
            )
            city.opciones = [
                EncOpcion(orden=1, texto="Junín", valor="junin"),
                EncOpcion(orden=2, texto="La Colonia", valor="la_colonia"),
            ]
            survey.preguntas = [vote, city]
        db.session.add(survey)
        db.session.commit()

        self._review_junin_survey_for_publication(
            survey,
            tenant=scoped_tenant,
        )

        if eligibility_mode in {"institution_attested", "manual_review"}:
            self.app.config["ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1"] = True
            current = str(self.app.config.get("SURVEY_ELIGIBILITY_GRANT_TENANT_IDS") or "")
            ids = {item for item in current.split(",") if item}
            ids.add(str(scoped_tenant.id))
            self.app.config["SURVEY_ELIGIBILITY_GRANT_TENANT_IDS"] = ",".join(sorted(ids))
            self.app.config["SURVEY_ELIGIBILITY_SECRET_V1"] = "s" * 48

        release, replayed = create_release(
            tenant_id=scoped_tenant.id,
            survey_id=survey.id,
            actor_user_id=self.owner.id,
            payload=self._release_policy(mode=eligibility_mode),
            idempotency_key=f"release.create.{scoped_tenant.id}.{survey.id}",
        )
        self.assertFalse(replayed)
        published, replayed = publish_release(
            tenant_id=scoped_tenant.id,
            survey_id=survey.id,
            release_id=release.id,
            actor_user_id=self.owner.id,
            idempotency_key=f"release.publish.{scoped_tenant.id}.{survey.id}",
            expected_snapshot_sha256=release.snapshot_sha256,
        )
        self.assertFalse(replayed)
        self.assertEqual(published.status, "published")
        db.session.refresh(survey)
        return survey

    def _review_junin_survey_for_publication(
        self,
        survey: EncEncuesta,
        *,
        tenant: TenantProfile,
    ) -> None:
        """Bind and approve the exact survey content against Junín's reviewed boundary."""

        mark_junin_jurisdiction_verified(
            tenant,
            reviewer_user_id=self.owner.id,
        )
        headers = self._admin_headers()
        initial = self.client.get(
            f"/api/v2/surveys/{survey.id}/content-review",
            headers=headers,
        )
        self.assertEqual(initial.status_code, 200, initial.get_json())
        initial_payload = initial.get_json()
        initial_hash = initial_payload["jurisdiction"]["content_sha256"]
        self.assertIsNone(
            initial_payload["jurisdiction"].get("survey_jurisdiction_ref")
        )

        bound = self.client.post(
            f"/api/v2/surveys/{survey.id}/content-review",
            json={
                "decision": "bind",
                "expected_content_sha256": initial_hash,
                "evidence_ref": f"qa-review:whatsapp-survey-{survey.id}",
            },
            headers={
                **headers,
                "Idempotency-Key": f"whatsapp-survey-{survey.id}:bind:0001",
            },
        )
        self.assertEqual(bound.status_code, 201, bound.get_json())
        bound_payload = bound.get_json()
        self.assertEqual(
            bound_payload.get("action_hint"),
            "reload_jurisdiction_readiness_then_review",
        )
        self.assertNotEqual(
            bound_payload["jurisdiction"]["content_sha256"],
            initial_hash,
        )

        reloaded = self.client.get(
            f"/api/v2/surveys/{survey.id}/content-review",
            headers=headers,
        )
        self.assertEqual(reloaded.status_code, 200, reloaded.get_json())
        reloaded_hash = reloaded.get_json()["jurisdiction"]["content_sha256"]
        self.assertEqual(
            reloaded_hash,
            bound_payload["jurisdiction"]["content_sha256"],
        )

        approved = self.client.post(
            f"/api/v2/surveys/{survey.id}/content-review",
            json={
                "decision": "approve",
                "expected_content_sha256": reloaded_hash,
                "evidence_ref": f"qa-review:whatsapp-survey-{survey.id}",
            },
            headers={
                **headers,
                "Idempotency-Key": f"whatsapp-survey-{survey.id}:approve:0001",
            },
        )
        self.assertEqual(approved.status_code, 201, approved.get_json())
        approved_payload = approved.get_json()
        self.assertTrue(approved_payload.get("review_completed"))
        self.assertTrue(approved_payload["jurisdiction"].get("ready"))

    def _context(
        self,
        *,
        tenant: TenantProfile | None = None,
        phone: str = "+5492634123456",
    ) -> dict:
        scoped_tenant = tenant or self.tenant
        return {
            "tenant_profile": scoped_tenant,
            "tenant_id": scoped_tenant.id,
            "viewer_user_obj": self.viewer,
            "anon_id": phone,
            "channel": "whatsapp",
            "municipio_config_actual": {
                "slug": scoped_tenant.slug,
                "encuestas_enabled": True,
                "encuestas_base_url": "https://www.chatboc.ar",
            },
            "chat_db_context_data": {
                CONTEXTO_MUNICIPIO: {
                    "contacto_usuario": {"telefono": phone},
                }
            },
        }

    def test_municipal_menu_offers_native_response_and_preserves_share(self):
        survey = self._create_governed_survey()
        for index in range(10):
            self._create_governed_survey(slug=f"gestion-junin-distrito-{index}")
        context = self._context()

        menu = _get_encuestas_menu(context)

        respond_action = self._action(menu, "encuesta_responder::")
        survey_metadata = menu["surveys"][0]
        share_action = survey_metadata["share_action_id"]
        self.assertTrue(respond_action.startswith("encuesta_responder::"))
        self.assertTrue(share_action.endswith(survey_metadata["slug"]))
        self.assertIn("https://wa.me/?text=", menu["message_body"])
        self.assertIn("tenant_slug=junin", menu["message_body"])
        self.assertLessEqual(len(menu["options_list"]), 10)
        self.assertTrue(
            all(
                len(str(option.get("texto") or "")) <= 24
                and len(str(option.get("action_id") or "")) <= 200
                for option in menu["options_list"]
            )
        )
        self.assertTrue(
            any(
                option.get("action_id") == "mostrar_menu_encuestas::2"
                for option in menu["options_list"]
            )
        )
        self.assertTrue(
            any(
                option.get("action_id") == "menu_principal"
                for option in menu["options_list"]
            )
        )

        middle_page = _get_encuestas_menu(context, page=2)
        middle_ids = {
            option.get("action_id") for option in middle_page["options_list"]
        }
        self.assertLessEqual(len(middle_page["options_list"]), 10)
        self.assertIn("mostrar_menu_encuestas::1", middle_ids)
        self.assertIn("mostrar_menu_encuestas::3", middle_ids)
        self.assertIn("menu_principal", middle_ids)
        with patch("services.response_formatter.WHATSAPP_FORCE_TEXT", True):
            formatted = build_interactive_response(
                options=middle_page["options_list"],
                body_text=middle_page["message_body"],
                channel="whatsapp",
                message_type=middle_page["message_type"],
                original_bot_response=middle_page,
            )
        transported_options = formatted["contexto_actualizado"]["last_options_sent"]
        transported_ids = {
            option.get("action_id") for option in transported_options
        }
        self.assertLessEqual(len(transported_options), 10)
        self.assertIn("mostrar_menu_encuestas::1", transported_ids)
        self.assertIn("mostrar_menu_encuestas::3", transported_ids)
        self.assertIn("menu_principal", transported_ids)
        self.assertIn("cancelar", transported_ids)

        consent = handle_main_menu_action(respond_action, context, None)
        self.assertEqual(consent["fuente"], "encuesta_whatsapp_consentimiento_v1")
        self.assertIn(
            WHATSAPP_SURVEY_FLOW_STATE_KEY,
            context["chat_db_context_data"][CONTEXTO_MUNICIPIO],
        )

    def test_real_municipal_responder_consumes_active_vote_before_llm(self):
        survey = self._create_governed_survey()
        chat_context = ChatSessionContext(
            chat_session_id="wa-survey-conversation-real-responder",
            user_id=self.owner.id,
            tenant_id=self.tenant.id,
            anon_id="+5492634123456",
            context_data=self._context()["chat_db_context_data"],
        )
        db.session.add(chat_context)
        db.session.commit()
        start_context = self._context()
        start_context["chat_db_context_data"] = chat_context.context_data
        start_action = self._action(_get_encuestas_menu(start_context), "encuesta_responder::")
        consent = handle_main_menu_action(start_action, start_context, chat_context)

        def _respond(payload):
            return responder_municipio(
                pregunta_original=payload,
                owner_user=self.owner,
                rubro_obj=self.owner.rubro,
                viewer_user=self.viewer,
                chat_db_context=chat_context,
                anon_id="+5492634123456",
                channel="whatsapp",
                tenant_profile=self.tenant,
                tenant_id=self.tenant.id,
                profile_name="Ciudadana de prueba",
            )

        def unexpected_llm(*_args, **_kwargs):
            raise AssertionError("El voto estructurado no debe llegar al LLM")

        with patch(
            "services.municipio_responder.cargar_configuracion_municipio",
            return_value={
                "encuestas_enabled": True,
                "encuestas_base_url": "https://www.chatboc.ar",
            },
        ), patch(
            "services.municipio_responder.handle_llm_interaction",
            side_effect=unexpected_llm,
        ), patch(
            "services.municipio_responder.llamar_gemini",
            side_effect=unexpected_llm,
        ):
            question = _respond(
                {
                    "pregunta": "",
                    "action": self._action(
                        consent,
                        "encuesta_wa::consent_accept::",
                    ),
                }
            )
            self.assertEqual(question["fuente"], "encuesta_whatsapp_pregunta_v1")
            city = _respond({"pregunta": "Sí"})
            receipt = _respond(
                {
                    "pregunta": "",
                    "action": self._action(city, "encuesta_wa::answer::"),
                }
            )

        self.assertEqual(receipt["fuente"], "encuesta_whatsapp_confirmada_v1")
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 1)

    def _start_active_real_responder_survey(
        self,
        *,
        case_slug: str,
    ) -> tuple[EncEncuesta, ChatSessionContext]:
        survey = self._create_governed_survey(
            slug=f"gestion-junin-prioridad-{case_slug}"
        )
        chat_context = ChatSessionContext(
            chat_session_id=f"wa-survey-priority-{case_slug}",
            user_id=self.owner.id,
            tenant_id=self.tenant.id,
            anon_id="+5492634123456",
            context_data=self._context()["chat_db_context_data"],
        )
        db.session.add(chat_context)
        db.session.commit()

        start_context = self._context()
        start_context["chat_db_context_data"] = chat_context.context_data
        start_action = self._action(
            _get_encuestas_menu(start_context),
            "encuesta_responder::",
        )
        consent = handle_main_menu_action(start_action, start_context, chat_context)
        self.assertEqual(
            consent["fuente"],
            "encuesta_whatsapp_consentimiento_v1",
        )
        return survey, chat_context

    def _respond_active_survey(
        self,
        chat_context: ChatSessionContext,
        payload: dict,
    ) -> dict:
        return responder_municipio(
            pregunta_original=payload,
            owner_user=self.owner,
            rubro_obj=self.owner.rubro,
            viewer_user=self.viewer,
            chat_db_context=chat_context,
            anon_id="+5492634123456",
            channel="whatsapp",
            tenant_profile=self.tenant,
            tenant_id=self.tenant.id,
            profile_name="Ciudadana de prueba",
        )

    def _assert_active_survey_state_preserved(
        self,
        chat_context: ChatSessionContext,
        survey: EncEncuesta,
    ) -> None:
        municipal = chat_context.context_data[CONTEXTO_MUNICIPIO]
        self.assertEqual(
            municipal.get("estado_conversacion"),
            "EN_FLUJO_ENCUESTA_WHATSAPP",
        )
        self.assertIn(WHATSAPP_SURVEY_FLOW_STATE_KEY, municipal)
        self.assertEqual(
            EncRespuesta.query.filter_by(encuesta_id=survey.id).count(),
            0,
        )

    def test_active_survey_hello_is_reprompted_without_greeting_reset(self):
        survey, chat_context = self._start_active_real_responder_survey(
            case_slug="hola"
        )

        with patch(
            "services.municipio_responder.GreetingHandler.handle",
            side_effect=AssertionError("hola no debe resetear la encuesta activa"),
        ), patch(
            "services.municipio_responder.handle_whatsapp_survey_flow_turn",
            wraps=handle_whatsapp_survey_flow_turn,
        ) as survey_turn:
            response = self._respond_active_survey(
                chat_context,
                {"pregunta": "hola"},
            )

        self.assertEqual(
            response["fuente"],
            "encuesta_whatsapp_respuesta_ambigua_v1",
        )
        self.assertEqual(survey_turn.call_args.kwargs["text"], "hola")
        self._assert_active_survey_state_preserved(chat_context, survey)

    def test_active_survey_emoji_is_reprompted_without_global_shortcut(self):
        survey, chat_context = self._start_active_real_responder_survey(
            case_slug="emoji"
        )

        with patch(
            "services.municipio_responder._try_handle_emoji_shortcut",
            side_effect=AssertionError("el emoji no debe salir del estado gobernado"),
        ), patch(
            "services.municipio_responder.handle_whatsapp_survey_flow_turn",
            wraps=handle_whatsapp_survey_flow_turn,
        ) as survey_turn:
            response = self._respond_active_survey(
                chat_context,
                {"pregunta": "💡"},
            )

        self.assertEqual(
            response["fuente"],
            "encuesta_whatsapp_respuesta_ambigua_v1",
        )
        self.assertEqual(survey_turn.call_args.kwargs["text"], "💡")
        self._assert_active_survey_state_preserved(chat_context, survey)

    def test_active_survey_photo_keeps_media_context_without_claim_routing(self):
        survey, chat_context = self._start_active_real_responder_survey(
            case_slug="foto"
        )
        photo_url = "https://cdn.example.test/encuesta/foto.jpg"
        inbound_content = {
            "contract_version": "whatsapp.inbound_content.v1",
            "kind": "image",
            "durable_message_kind": "image",
            "is_language_input": False,
            "has_media": True,
            "media_mime_type": "image/jpeg",
            "evidence_policy": "exact_active_context_only",
            "reason": None,
        }

        with patch(
            "services.municipio_responder.handle_whatsapp_survey_flow_turn",
            wraps=handle_whatsapp_survey_flow_turn,
        ) as survey_turn:
            response = self._respond_active_survey(
                chat_context,
                {
                    "pregunta": "",
                    "es_foto": True,
                    "foto_url": photo_url,
                    "whatsapp_inbound_content": inbound_content,
                },
            )

        self.assertEqual(
            response["fuente"],
            "encuesta_whatsapp_respuesta_ambigua_v1",
        )
        survey_context = survey_turn.call_args.args[0]
        self.assertTrue(survey_context["es_foto"])
        self.assertEqual(survey_context["foto_url"], photo_url)
        self.assertEqual(
            survey_context["whatsapp_inbound_content"],
            inbound_content,
        )
        self.assertEqual(chat_context.context_data.get("foto_url"), photo_url)
        self._assert_active_survey_state_preserved(chat_context, survey)

    def test_active_survey_location_pin_keeps_coordinates_without_proactive_routing(self):
        survey, chat_context = self._start_active_real_responder_survey(
            case_slug="ubicacion"
        )
        location = {
            "latitude": -33.136,
            "longitude": -68.49,
            "address": "Ubicación sintética QA, Junín, Mendoza",
        }
        inbound_content = {
            "contract_version": "whatsapp.inbound_content.v1",
            "kind": "location",
            "durable_message_kind": "location",
            "is_language_input": False,
            "has_media": False,
            "media_mime_type": None,
            "evidence_policy": "not_applicable",
            "reason": None,
        }

        with patch(
            "services.municipio_responder.handle_whatsapp_survey_flow_turn",
            wraps=handle_whatsapp_survey_flow_turn,
        ) as survey_turn:
            response = self._respond_active_survey(
                chat_context,
                {
                    "pregunta": "",
                    "es_ubicacion": True,
                    "ubicacion_usuario": location,
                    "whatsapp_inbound_content": inbound_content,
                },
            )

        self.assertEqual(
            response["fuente"],
            "encuesta_whatsapp_respuesta_ambigua_v1",
        )
        survey_context = survey_turn.call_args.args[0]
        self.assertTrue(survey_context["es_ubicacion"])
        self.assertEqual(survey_context["ubicacion_usuario"], location)
        self.assertEqual(
            survey_context["whatsapp_inbound_content"],
            inbound_content,
        )
        state = chat_context.context_data[CONTEXTO_MUNICIPIO][
            WHATSAPP_SURVEY_FLOW_STATE_KEY
        ]
        self.assertEqual(
            state["shared_location"],
            {"lat": -33.136, "lng": -68.49},
        )
        self._assert_active_survey_state_preserved(chat_context, survey)

        vote_question = self._respond_active_survey(
            chat_context,
            {
                "pregunta": "",
                "action": self._action(
                    response,
                    "encuesta_wa::consent_accept::",
                ),
            },
        )
        city_question = self._respond_active_survey(
            chat_context,
            {
                "pregunta": "",
                "action": self._action(
                    vote_question,
                    "encuesta_wa::answer::",
                ),
            },
        )
        receipt = self._respond_active_survey(
            chat_context,
            {
                "pregunta": "",
                "action": self._action(
                    city_question,
                    "encuesta_wa::answer::",
                ),
            },
        )

        self.assertEqual(receipt["fuente"], "encuesta_whatsapp_confirmada_v1")
        persisted = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
        self.assertAlmostEqual(persisted.lat, -33.136)
        self.assertAlmostEqual(persisted.lng, -68.49)

    def test_shared_survey_location_rejects_partial_or_invalid_coordinates(self):
        self.assertEqual(
            _shared_location_from_context(
                {
                    "es_ubicacion": True,
                    "ubicacion_usuario": {"latitude": -33.136},
                }
            ),
            {},
        )
        self.assertEqual(
            _shared_location_from_context(
                {
                    "es_ubicacion": True,
                    "ubicacion_usuario": {
                        "latitude": 91,
                        "longitude": -68.49,
                    },
                }
            ),
            {},
        )
        self.assertEqual(
            _shared_location_from_context(
                {
                    "es_ubicacion": True,
                    "ubicacion_usuario": {"lat": 0, "lng": 0},
                }
            ),
            {"lat": 0.0, "lng": 0.0},
        )

    def test_source_anonymous_survey_does_not_retain_shared_coordinates(self):
        survey = self._create_governed_survey(
            slug="gestion-junin-anonima-ubicacion",
            privacy_mode="source_anonymous",
        )
        context = self._context()
        start = start_whatsapp_survey_flow(context, survey.slug)

        response = handle_whatsapp_survey_flow_turn(
            {
                **context,
                "es_ubicacion": True,
                "ubicacion_usuario": {
                    "latitude": -33.136,
                    "longitude": -68.49,
                },
            },
            text="",
        )

        self.assertEqual(response["fuente"], "encuesta_whatsapp_respuesta_ambigua_v1")
        state = context["chat_db_context_data"][CONTEXTO_MUNICIPIO][
            WHATSAPP_SURVEY_FLOW_STATE_KEY
        ]
        self.assertNotIn("shared_location", state)
        self.assertTrue(
            self._action(start, "encuesta_wa::consent_accept::")
        )

    @staticmethod
    def _action(payload: dict, prefix: str) -> str:
        return next(
            option["action_id"]
            for option in payload["options_list"]
            if str(option.get("action_id") or "").startswith(prefix)
        )

    def _complete_vote(self, context: dict, survey: EncEncuesta) -> dict:
        start = start_whatsapp_survey_flow(context, survey.slug)
        consent_action = self._action(start, "encuesta_wa::consent_accept::")
        vote_question = handle_whatsapp_survey_flow_turn(
            context,
            text="",
            action_id=consent_action,
        )
        city_question = handle_whatsapp_survey_flow_turn(
            context,
            text="Sí",
        )
        city_action = self._action(city_question, "encuesta_wa::answer::")
        return handle_whatsapp_survey_flow_turn(
            context,
            text="",
            action_id=city_action,
        )

    def _add_source_anonymous_result_row(
        self,
        survey: EncEncuesta,
        *,
        origin: str = "real",
        sequence: int,
    ) -> EncRespuesta:
        submitted_at = datetime.now(timezone.utc) + timedelta(seconds=sequence)
        response = EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=survey.tenant_id,
            canal="whatsapp_chat",
            response_origin=origin,
            submitted_at=submitted_at,
            privacy_mode="source_anonymous",
            privacy_policy_version=survey.privacy_policy_version,
            privacy_consent_recorded_at=submitted_at,
            retention_expires_at=submitted_at + timedelta(days=365),
        )
        response.detalles = [
            EncRespuestaDetalle(
                pregunta_id=question.id,
                opcion_id=question.opciones[0].id,
            )
            for question in survey.preguntas
        ]
        db.session.add(response)
        db.session.commit()
        return response

    def _assert_source_anonymous_small_cohort_hidden(self, payload: dict) -> None:
        self.assertEqual(
            payload,
            {
                "results_available": False,
                "privacy": {
                    "contract_version": "surveys.public_count_privacy.v1",
                    "privacy_mode": "source_anonymous",
                    "minimum_cell_size": 5,
                    "results_final": False,
                    "count": None,
                    "bucket": "withheld_until_close",
                    "suppressed": True,
                    "reason_code": "source_anonymous_results_withheld_until_close",
                },
            },
        )
        self.assertNotIn("total_responses", payload)
        self.assertNotIn("questions", payload)
        self.assertNotIn("data_provenance", payload)

    def test_source_anonymous_live_snapshot_is_identical_from_zero_through_four(self):
        survey = self._create_governed_survey(
            slug="gestion-source-anonymous-small-cohort",
            privacy_mode="source_anonymous",
        )
        instrument = _load_instrument(self._context(), survey.slug)

        snapshots = [_live_results(instrument)]
        for sequence in range(1, 5):
            self._add_source_anonymous_result_row(
                survey,
                sequence=sequence,
            )
            snapshots.append(_live_results(instrument))
        self._add_source_anonymous_result_row(
            survey,
            origin="synthetic_demo",
            sequence=5,
        )
        self._add_source_anonymous_result_row(
            survey,
            origin="legacy_unverified",
            sequence=6,
        )
        snapshots.append(_live_results(instrument))

        for snapshot in snapshots:
            self._assert_source_anonymous_small_cohort_hidden(snapshot)
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))

    def test_source_anonymous_k_five_releases_only_real_safe_snapshot(self):
        survey = self._create_governed_survey(
            slug="gestion-source-anonymous-k-five",
            privacy_mode="source_anonymous",
        )
        instrument = _load_instrument(self._context(), survey.slug)
        for sequence in range(1, 6):
            self._add_source_anonymous_result_row(
                survey,
                sequence=sequence,
            )
        self._add_source_anonymous_result_row(
            survey,
            origin="synthetic_demo",
            sequence=6,
        )
        self._add_source_anonymous_result_row(
            survey,
            origin="legacy_unverified",
            sequence=7,
        )
        survey.estado = "cerrada"
        db.session.commit()

        snapshot = _live_results(instrument)

        self.assertTrue(snapshot["results_available"])
        self.assertEqual(snapshot["total_responses"], 5)
        self.assertEqual(snapshot["questions"][0]["total"], 5)
        self.assertEqual(snapshot["questions"][0]["options"][0]["votes"], 5)
        self.assertEqual(snapshot["questions"][0]["options"][0]["percentage"], 100.0)
        provenance = snapshot["data_provenance"]
        self.assertEqual(provenance["real_responses_included"], 5)
        self.assertEqual(provenance["synthetic_responses_excluded"], 1)
        self.assertEqual(provenance["unverified_responses_excluded"], 1)
        self.assertFalse(provenance["contains_synthetic"])

        first_real = EncRespuesta.query.filter_by(
            encuesta_id=survey.id,
            response_origin="real",
        ).order_by(EncRespuesta.id.asc()).first()
        first_real.detalles[0].opcion_id = survey.preguntas[0].opciones[1].id
        db.session.commit()
        split_snapshot = _live_results(instrument)
        self.assertEqual(split_snapshot["total_responses"], 5)
        self.assertEqual(split_snapshot["questions"][0]["total"], 5)
        self.assertEqual(split_snapshot["questions"][0]["options"], [])
        self.assertTrue(split_snapshot["questions"][0]["privacy"]["suppressed"])

    def test_source_anonymous_completion_duplicate_and_replay_hide_small_cohort(self):
        survey = self._create_governed_survey(
            slug="gestion-source-anonymous-receipt",
            privacy_mode="source_anonymous",
        )
        context = self._context()
        with patch("services.encuestas_service.emit_survey_response_update"):
            completion = self._complete_vote(context, survey)

        self.assertTrue(completion["response_persisted"])
        self.assertIsInstance(completion["receipt_id"], int)
        self.assertIn("Recibo de participación:", completion["message_body"])
        self.assertNotIn("Respuestas registradas:", completion["message_body"])
        self._assert_source_anonymous_small_cohort_hidden(completion["results"])

        saved = EncRespuesta.query.filter_by(
            encuesta_id=survey.id,
            response_origin="real",
        ).one()
        for field in (
            "user_id",
            "dni",
            "phone",
            "ip",
            "ua",
            "lat",
            "lng",
            "utm_source",
            "utm_campaign",
            "edad",
            "anio_nacimiento",
            "metadata_payload",
        ):
            self.assertIsNone(getattr(saved, field), field)

        duplicate = self._complete_vote(self._context(), survey)
        self.assertTrue(duplicate["duplicate_prevented"])
        self.assertNotIn("Respuestas registradas:", duplicate["message_body"])
        self._assert_source_anonymous_small_cohort_hidden(duplicate["results"])

        replay_survey = self._create_governed_survey(
            slug="gestion-source-anonymous-replay",
            privacy_mode="source_anonymous",
        )
        replay_context = self._context(phone="+5492634123499")
        start = start_whatsapp_survey_flow(replay_context, replay_survey.slug)
        consent_action = self._action(start, "encuesta_wa::consent_accept::")
        handle_whatsapp_survey_flow_turn(
            replay_context,
            text="",
            action_id=consent_action,
        )
        city_question = handle_whatsapp_survey_flow_turn(replay_context, text="1")
        city_action = self._action(city_question, "encuesta_wa::answer::")

        def commit_then_report_uncertain(*args, **kwargs):
            real_save_respuesta(*args, **kwargs)
            raise EncuestaError(
                "confirmación incierta",
                status_code=503,
                payload={"reason_code": "provider_confirmation_uncertain"},
            )

        with patch(
            "services.whatsapp_survey_conversation.save_respuesta",
            side_effect=commit_then_report_uncertain,
        ), patch("services.encuestas_service.emit_survey_response_update"):
            uncertain = handle_whatsapp_survey_flow_turn(
                replay_context,
                text="",
                action_id=city_action,
            )
        retry_action = self._action(uncertain, "encuesta_wa::retry::")
        replay = handle_whatsapp_survey_flow_turn(
            replay_context,
            text="",
            action_id=retry_action,
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["idempotency"]["disposition"], "replayed")
        self.assertNotIn("Respuestas registradas:", replay["message_body"])
        self._assert_source_anonymous_small_cohort_hidden(replay["results"])
        for payload in (completion, duplicate, replay):
            rendered = repr(payload)
            self.assertNotIn("+5492634123456", rendered)
            self.assertNotIn("+5492634123499", rendered)
            self.assertNotIn(self.viewer.email, rendered)

    def test_governed_yes_no_and_city_vote_pins_release_and_emits_realtime(self):
        survey = self._create_governed_survey(eligibility_mode="self_attested")
        context = self._context()
        db.session.add(
            EncRespuesta(
                encuesta_id=survey.id,
                tenant_id=self.tenant.id,
                canal="seed",
                response_origin="synthetic_demo",
                metadata_payload={
                    "is_demo_seed": True,
                    "demo_seed_contract_version": (
                        SURVEY_DEMO_SEEDING_CONTRACT_VERSION
                    ),
                    "demo_batch_id": (
                        f"seed-{survey.id}-1755680400000-abcdef123456"
                    ),
                },
                submitted_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

        with patch(
            "services.encuestas_service.emit_survey_response_update"
        ) as emit_update:
            receipt = self._complete_vote(context, survey)

        self.assertTrue(receipt["success"])
        self.assertTrue(receipt["response_persisted"])
        self.assertEqual(receipt["results"]["total_responses"], 1)
        self.assertEqual(receipt["results"]["data_provenance"]["mode"], "real")
        self.assertEqual(
            receipt["results"]["data_provenance"][
                "synthetic_responses_excluded"
            ],
            1,
        )
        self.assertIn("tenant_slug=junin", receipt["share_url"])
        self.assertNotIn(
            WHATSAPP_SURVEY_FLOW_STATE_KEY,
            context["chat_db_context_data"][CONTEXTO_MUNICIPIO],
        )

        saved = next(
            row
            for row in EncRespuesta.query.filter_by(encuesta_id=survey.id).all()
            if not is_trusted_demo_seed_response(row)
        )
        self.assertEqual(saved.tenant_id, self.tenant.id)
        self.assertEqual(saved.phone, "+5492634123456")
        self.assertEqual(saved.ciudad, "junin")
        self.assertEqual(saved.canal, "whatsapp_chat")
        self.assertIsNotNone(saved.governance_release_id)
        self.assertEqual(
            saved.governance_eligibility_policy_version,
            "eligibility-junin-wa-2026.1",
        )
        self.assertEqual(
            saved.governance_consent_policy_version,
            "consent-junin-wa-2026.1",
        )
        self.assertTrue(receipt["idempotency"]["persisted"])
        emit_update.assert_called_once()

        instrument = _load_instrument(context, survey.slug)
        statements = []

        def _capture_sql(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(db.engine, "before_cursor_execute", _capture_sql)
        try:
            live_results = _live_results(instrument)
        finally:
            event.remove(db.engine, "before_cursor_execute", _capture_sql)

        self.assertEqual(live_results["total_responses"], 1)
        survey_sql = [
            statement
            for statement in statements
            if "enc_respuesta" in statement
        ]
        self.assertTrue(survey_sql, statements)
        self.assertTrue(
            all("response_origin" in statement for statement in survey_sql),
            survey_sql,
        )
        self.assertFalse(
            any(
                "enc_respuesta_detalle.respuesta_id in" in statement
                for statement in survey_sql
            ),
            survey_sql,
        )
        self.assertFalse(
            any("metadata_payload" in statement for statement in survey_sql),
            survey_sql,
        )

    def test_rejecting_consent_records_nothing(self):
        survey = self._create_governed_survey()
        context = self._context()
        start = start_whatsapp_survey_flow(context, survey.slug)
        disclosure = start["_whatsapp_required_disclosure"]
        self.assertIn("participo voluntariamente", disclosure["body"])
        self.assertEqual(
            disclosure["sha256"],
            hashlib.sha256(disclosure["body"].encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("participo voluntariamente", start["message_body"])
        self.assertIn("participo voluntariamente", start["audio_text"])

        rejected = handle_whatsapp_survey_flow_turn(
            context,
            text="No acepto",
        )

        self.assertEqual(
            rejected["fuente"],
            "encuesta_whatsapp_consentimiento_rechazado_v1",
        )
        self.assertFalse(rejected["response_persisted"])
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 0)
        self.assertNotIn(
            WHATSAPP_SURVEY_FLOW_STATE_KEY,
            context["chat_db_context_data"][CONTEXTO_MUNICIPIO],
        )

    def test_duplicate_is_prevented_and_uncertain_retry_replays_same_submission(self):
        survey = self._create_governed_survey()
        context = self._context()
        start = start_whatsapp_survey_flow(context, survey.slug)
        consent_action = self._action(start, "encuesta_wa::consent_accept::")
        handle_whatsapp_survey_flow_turn(context, text="", action_id=consent_action)
        city_question = handle_whatsapp_survey_flow_turn(context, text="1")
        city_action = self._action(city_question, "encuesta_wa::answer::")

        def commit_then_report_uncertain(*args, **kwargs):
            real_save_respuesta(*args, **kwargs)
            raise EncuestaError(
                "confirmación incierta",
                status_code=503,
                payload={"reason_code": "provider_confirmation_uncertain"},
            )

        with patch(
            "services.whatsapp_survey_conversation.save_respuesta",
            side_effect=commit_then_report_uncertain,
        ), patch("services.encuestas_service.emit_survey_response_update") as emit_update:
            uncertain = handle_whatsapp_survey_flow_turn(
                context,
                text="",
                action_id=city_action,
            )
            self.assertTrue(uncertain["retryable"])
            self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 1)
            retry_action = self._action(uncertain, "encuesta_wa::retry::")

        with patch(
            "services.whatsapp_survey_conversation.save_respuesta",
            side_effect=AssertionError("un texto libre no debe ejecutar el reintento"),
        ):
            still_pending = handle_whatsapp_survey_flow_turn(
                context,
                text="sí, reintentar",
            )
        self.assertTrue(still_pending["retryable"])
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 1)

        with patch("services.encuestas_service.emit_survey_response_update") as replay_emit:
            replay = handle_whatsapp_survey_flow_turn(
                context,
                text="",
                action_id=retry_action,
            )

        self.assertTrue(replay["success"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["idempotency"]["disposition"], "replayed")
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 1)
        replay_emit.assert_not_called()

        duplicate_context = self._context()
        duplicate = self._complete_vote(duplicate_context, survey)
        self.assertTrue(duplicate["duplicate_prevented"])
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 1)

    def test_cross_tenant_start_fails_closed_without_exposing_foreign_form(self):
        survey = self._create_governed_survey()
        other_owner = User(
            name="Otro municipio",
            email="other-wa-survey@example.test",
            rol="admin",
        )
        other_owner.set_password(secrets.token_urlsafe(24))
        db.session.add(other_owner)
        db.session.flush()
        other_tenant = TenantProfile(
            slug="otro-municipio-wa-qa",
            nombre="Otro municipio",
            tipo="municipio",
            municipio_id=other_owner.id,
            is_active=True,
        )
        db.session.add(other_tenant)
        db.session.commit()

        response = start_whatsapp_survey_flow(
            self._context(tenant=other_tenant),
            survey.slug,
            public_url=f"https://www.chatboc.ar/e/{survey.slug}",
        )

        self.assertEqual(response["fuente"], "encuesta_whatsapp_web_fallback_v1")
        self.assertFalse(response["response_persisted"])
        self.assertNotIn(survey.slug, response["message_body"])
        self.assertFalse(
            any(option.get("type") == "url" for option in response["options_list"])
        )
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 0)

        active_context = self._context()
        consent = start_whatsapp_survey_flow(active_context, survey.slug)
        active_context["tenant_profile"] = other_tenant
        active_context["tenant_id"] = other_tenant.id
        active_context["municipio_config_actual"]["slug"] = other_tenant.slug
        continued = handle_whatsapp_survey_flow_turn(
            active_context,
            text="",
            action_id=self._action(consent, "encuesta_wa::consent_accept::"),
        )

        self.assertFalse(continued["response_persisted"])
        self.assertNotIn(survey.slug, continued["message_body"])
        self.assertFalse(
            any(option.get("type") == "url" for option in continued["options_list"])
        )
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey.id).count(), 0)

    def test_incompatible_question_and_restricted_eligibility_use_honest_web_fallback(self):
        incompatible = self._create_governed_survey(
            slug="gestion-junin-comentario-abierto",
            incompatible=True,
        )
        restricted = self._create_governed_survey(
            slug="gestion-junin-restringida",
            eligibility_mode="manual_review",
        )
        too_many_options = self._create_governed_survey(
            slug="gestion-junin-muchas-opciones",
            vote_option_count=9,
        )

        for survey, reason in (
            (incompatible, "survey_whatsapp_question_type_unsupported"),
            (restricted, "survey_whatsapp_restricted_eligibility"),
            (too_many_options, "survey_whatsapp_option_count_unsupported"),
        ):
            with self.subTest(reason=reason):
                response = start_whatsapp_survey_flow(
                    self._context(),
                    survey.slug,
                    public_url=f"https://www.chatboc.ar/e/{survey.slug}",
                )
                self.assertEqual(response["reason_code"], reason)
                self.assertFalse(response["response_persisted"])
                self.assertIn("No registramos ninguna respuesta", response["message_body"])
                self.assertTrue(
                    any(option.get("type") == "url" for option in response["options_list"])
                )
                self.assertEqual(
                    EncRespuesta.query.filter_by(encuesta_id=survey.id).count(),
                    0,
                )


if __name__ == "__main__":
    unittest.main()
