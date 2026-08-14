from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import unittest
from unittest.mock import patch

from app import create_app, db
from config import Config
from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    ChatSessionContext,
    Rubro,
    TenantProfile,
    User,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.encuestas_service import EncuestaError, save_respuesta as real_save_respuesta
from services.survey_governance import create_release, publish_release
from services.municipio_responder import (
    MUNICIPIO_RESPONSE_CACHE,
    _get_encuestas_menu,
    handle_main_menu_action,
    responder_municipio,
)
from services.response_formatter import build_interactive_response
from services.whatsapp_survey_conversation import (
    WHATSAPP_SURVEY_FLOW_STATE_KEY,
    handle_whatsapp_survey_flow_turn,
    start_whatsapp_survey_flow,
)


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
            telefono="+5492901123456",
        )
        self.viewer.set_password(secrets.token_urlsafe(24))
        db.session.add_all([self.owner, self.viewer])
        db.session.flush()
        self.tenant = TenantProfile(
            slug="tierra-del-fuego-wa-qa",
            nombre="Tierra del Fuego",
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
                "policy_version": "eligibility-tdf-2026.1",
                "mode": mode,
                "declarations": (
                    ["resident_attested"] if mode == "self_attested" else []
                ),
                "human_review_required": True,
                "automated_decision": False,
            },
            "consent_policy": {
                "policy_version": "consent-tdf-2026.1",
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
        slug: str = "gestion-melella-si-no",
        eligibility_mode: str = "open",
        incompatible: bool = False,
        vote_option_count: int = 2,
    ) -> EncEncuesta:
        scoped_tenant = tenant or self.tenant
        now = datetime.now(timezone.utc)
        survey = EncEncuesta(
            tenant_id=scoped_tenant.id,
            slug=slug,
            titulo="Votación ciudadana sobre transparencia de gestión",
            descripcion=(
                "Consulta no vinculante sobre un tablero mensual de la gestión "
                "del gobernador Gustavo Melella."
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
            privacy_mode="legacy",
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
                texto="¿En qué ciudad vivís?",
                obligatoria=True,
            )
            city.opciones = [
                EncOpcion(orden=1, texto="Ushuaia", valor="ushuaia"),
                EncOpcion(orden=2, texto="Río Grande", valor="rio_grande"),
            ]
            survey.preguntas = [vote, city]
        db.session.add(survey)
        db.session.commit()

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

    def _context(
        self,
        *,
        tenant: TenantProfile | None = None,
        phone: str = "+5492901123456",
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
            self._create_governed_survey(slug=f"gestion-melella-ciudad-{index}")
        context = self._context()

        menu = _get_encuestas_menu(context)

        respond_action = self._action(menu, "encuesta_responder::")
        survey_metadata = menu["surveys"][0]
        share_action = survey_metadata["share_action_id"]
        self.assertTrue(respond_action.startswith("encuesta_responder::"))
        self.assertTrue(share_action.endswith(survey_metadata["slug"]))
        self.assertIn("https://wa.me/?text=", menu["message_body"])
        self.assertIn("tenant_slug=tierra-del-fuego-wa-qa", menu["message_body"])
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
            anon_id="+5492901123456",
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
                anon_id="+5492901123456",
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
            slug=f"gestion-melella-prioridad-{case_slug}"
        )
        chat_context = ChatSessionContext(
            chat_session_id=f"wa-survey-priority-{case_slug}",
            user_id=self.owner.id,
            tenant_id=self.tenant.id,
            anon_id="+5492901123456",
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
            anon_id="+5492901123456",
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
            "latitude": -54.8019,
            "longitude": -68.303,
            "address": "Ushuaia, Tierra del Fuego",
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
        self._assert_active_survey_state_preserved(chat_context, survey)

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

    def test_governed_yes_no_and_city_vote_pins_release_and_emits_realtime(self):
        survey = self._create_governed_survey(eligibility_mode="self_attested")
        context = self._context()

        with patch(
            "services.encuestas_service.emit_survey_response_update"
        ) as emit_update:
            receipt = self._complete_vote(context, survey)

        self.assertTrue(receipt["success"])
        self.assertTrue(receipt["response_persisted"])
        self.assertEqual(receipt["results"]["total_responses"], 1)
        self.assertIn("tenant_slug=tierra-del-fuego-wa-qa", receipt["share_url"])
        self.assertNotIn(
            WHATSAPP_SURVEY_FLOW_STATE_KEY,
            context["chat_db_context_data"][CONTEXTO_MUNICIPIO],
        )

        saved = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
        self.assertEqual(saved.tenant_id, self.tenant.id)
        self.assertEqual(saved.phone, "+5492901123456")
        self.assertEqual(saved.ciudad, "ushuaia")
        self.assertEqual(saved.canal, "whatsapp_chat")
        self.assertIsNotNone(saved.governance_release_id)
        self.assertEqual(
            saved.governance_eligibility_policy_version,
            "eligibility-tdf-2026.1",
        )
        self.assertEqual(
            saved.governance_consent_policy_version,
            "consent-tdf-2026.1",
        )
        self.assertTrue(receipt["idempotency"]["persisted"])
        emit_update.assert_called_once()

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
            slug="gestion-melella-comentario-abierto",
            incompatible=True,
        )
        restricted = self._create_governed_survey(
            slug="gestion-melella-restringida",
            eligibility_mode="manual_review",
        )
        too_many_options = self._create_governed_survey(
            slug="gestion-melella-muchas-opciones",
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
