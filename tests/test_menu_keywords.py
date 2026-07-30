import unittest
from types import SimpleNamespace
from unittest.mock import patch
from flask import Flask

from services.municipio_responder import (
    find_global_menu_action,
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    ConversationState,
    find_reclamo_category_by_input,
    find_menu_action_by_input,
    _get_reclamos_menu,
    _looks_like_free_form_input,
    _maybe_route_menu_input_to_llm,
)


class TestMenuKeywords(unittest.TestCase):
    def test_free_form_detector(self):
        self.assertTrue(
            _looks_like_free_form_input(
                "una luminaria caída en mi barrio está parpadeando y es peligroso"
            )
        )
        self.assertTrue(_looks_like_free_form_input("sin agua en casa"))
        self.assertTrue(_looks_like_free_form_input("semaforo no funciona"))
        self.assertFalse(_looks_like_free_form_input("2"))

    def test_agenda_keyword(self):
        self.assertEqual(find_global_menu_action("agenda"), "agenda_y_noticias")

    def test_availability_queries_do_not_become_administrative_appointments(self):
        for phrase in (
            "farmacias de turno",
            "farmacia de guardia",
            "veterinaria de turno",
            "comercios abiertos ahora",
            "farmacia 24 hs",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(find_global_menu_action(phrase))

    def test_administrative_appointment_phrases_keep_shortcut(self):
        for phrase in (
            "turno",
            "solicitar turno",
            "pedir turno",
            "reservar turno",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(find_global_menu_action(phrase), "solicitar_turnos")

    def test_bromatologia_keyword(self):
        self.assertEqual(find_global_menu_action("bromatologia"), "veterinaria_bromatologia")

    def test_perros_keyword(self):
        self.assertEqual(find_global_menu_action("perros"), "veterinaria_bromatologia")

    def test_vacunas_keyword(self):
        self.assertEqual(find_global_menu_action("vacunas"), "veterinaria_bromatologia")

    def test_encuesta_keyword(self):
        self.assertEqual(find_global_menu_action("encuesta"), "mostrar_menu_encuestas")
        self.assertEqual(find_global_menu_action("participacion ciudadana"), "mostrar_menu_encuestas")

    def test_keyword_overrides_location_state(self):
        owner = SimpleNamespace(municipio_id="default", id=1)
        rubro = SimpleNamespace()
        chat_ctx = SimpleNamespace(
            chat_session_id="test",
            context_data={CONTEXTO_MUNICIPIO: {"estado_conversacion": ConversationState.ESPERANDO_UBICACION_GENERAL.name}},
        )
        app = Flask(__name__)
        with app.app_context():
            with patch("services.municipio_responder.flag_modified"), \
                 patch("services.municipio_responder.db"), \
                 patch("services.municipio_responder.get_tramites_info", return_value={}), \
                 patch(
                     "services.municipio_responder.cargar_configuracion_municipio",
                     return_value={
                         "Veterinaria y Bromatologia": {
                             "nombre": "Dra. Laura Funes",
                             "telefono": "+5492634521563",
                             "horario": "Lunes a Viernes de 8:00 a 18:00 hs."
                         }
                     },
                 ):
                response = responder_municipio("vacunas", owner, rubro, chat_db_context=chat_ctx)
        self.assertIn("Veterinaria y Bromatología", response["message_body"])

    def test_numeric_selection_in_submenu(self):
        owner = SimpleNamespace(municipio_id="default", id=1)
        rubro = SimpleNamespace()
        chat_ctx = SimpleNamespace(
            chat_session_id="test",
            context_data={
                "profile_name": "Test",
                CONTEXTO_MUNICIPIO: {
                    "estado_conversacion": ConversationState.ESPERANDO_SELECCION_DE_LISTA.name,
                    "menu_opciones": [
                        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
                        {"texto": "🎭 Agenda Cultural y Noticias", "action_id": "agenda_y_noticias"},
                        {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
                        {"texto": "Cancelar", "action_id": "cancelar"},
                    ],
                }
            },
        )
        app = Flask(__name__)
        with app.app_context():
            with patch("services.municipio_responder.flag_modified"), \
                 patch("services.municipio_responder.db"), \
                 patch("services.municipio_responder.get_tramites_info", return_value={}), \
                 patch(
                     "services.municipio_responder.cargar_configuracion_municipio",
                     return_value={
                         "Veterinaria y Bromatologia": {
                             "nombre": "Dra. Laura Funes",
                             "telefono": "+5492634521563",
                             "horario": "Lunes a Viernes de 8:00 a 18:00 hs."
                         }
                     },
                 ):
                response = responder_municipio("3", owner, rubro, chat_db_context=chat_ctx, profile_name="Test")
        self.assertIn("Veterinaria y Bromatología", response["message_body"])
        self.assertEqual(
            chat_ctx.context_data[CONTEXTO_MUNICIPIO].get("estado_conversacion"),
            ConversationState.ESPERANDO_ACCION_NAVEGACION.name,
        )

    def test_sanidad_animal_keyword(self):
        self.assertEqual(find_global_menu_action("sanidad animal"), "veterinaria_bromatologia")

    def test_averia_keyword(self):
        self.assertEqual(find_global_menu_action("averia"), "mostrar_menu_reclamos")

    def test_borrar_historial_keyword(self):
        self.assertEqual(find_global_menu_action("borrar historial"), "limpiar_contexto")

    def test_poste_caido_category_detection(self):
        options = _get_reclamos_menu().get("options_list", [])
        category = find_reclamo_category_by_input("hay un poste caido", options)
        self.assertEqual(category, "Luminaria")

    def test_sugerencia_phrase(self):
        phrase = "queria hacer una sugerencia"
        self.assertEqual(find_global_menu_action(phrase), "enviar_sugerencia")

    def test_long_phrase_skips_fuzzy_match(self):
        phrase = "me gustaria que coloquen mas juegos de plaza en mi barrio"
        self.assertIsNone(find_global_menu_action(phrase))

    def test_free_form_water_problem_is_not_consumed_by_fuzzy_admin_menu(self):
        buttons = [
            {"texto": action_id, "action_id": action_id}
            for action_id in (
                "obras_privadas",
                "solicitar_turnos",
                "mostrar_menu_reclamos",
            )
        ]

        self.assertIsNone(
            find_menu_action_by_input("no tengo agua en mi casa", buttons)
        )
        self.assertIsNone(find_global_menu_action("no tengo agua en mi casa"))
        self.assertIsNone(find_global_menu_action("sin agua en casa"))
        self.assertIsNone(find_global_menu_action("semaforo no funciona"))

    def test_declared_multiword_menu_phrases_remain_deterministic(self):
        self.assertEqual(
            find_global_menu_action("participacion ciudadana"),
            "mostrar_menu_encuestas",
        )
        self.assertEqual(
            find_global_menu_action("queria hacer una sugerencia"),
            "enviar_sugerencia",
        )
        self.assertEqual(
            find_global_menu_action("borrar historial"),
            "limpiar_contexto",
        )

    def test_declared_intents_accept_controlled_natural_phrases(self):
        cases = {
            "Necesito defensa del consumidor": "defensa_del_consumidor",
            "Necesito estacionar mi auto": "buscar_estacionamiento",
            "Cuando pasa el camion de basura": "recoleccion_residuos",
            "Que obras estan haciendo": "obras",
            "Donde esta el punto limpio": "punto_limpio",
            "Quiero pagar un impuesto": "pago_de_tasas_vigentes",
            "Donde pago un tributo municipal": "pago_de_tasas_vigentes",
            "quiero ver encuestas": "mostrar_menu_encuestas",
            "Hacer un Reclamo": "mostrar_menu_reclamos",
        }

        for phrase, expected_action in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(find_global_menu_action(phrase), expected_action)

    def test_declared_tokens_do_not_consume_detailed_claims_or_addresses(self):
        free_form_inputs = (
            "quiero hacer un reclamo por una perdida de agua en mi vereda",
            "hay un problema con un poste caido en san martin 123",
            "direccion don bosco 56 esquina sarmiento plaza junin",
            "necesito ayuda porque no tengo agua en mi casa",
        )

        for phrase in free_form_inputs:
            with self.subTest(phrase=phrase):
                self.assertIsNone(find_global_menu_action(phrase))

    def test_claim_category_uses_specific_evidence_over_location_context(self):
        options = _get_reclamos_menu().get("options_list", [])

        self.assertEqual(
            find_reclamo_category_by_input(
                "hay una perdida de agua en mi vereda",
                options,
            ),
            "Pérdida de agua",
        )
        self.assertEqual(
            find_reclamo_category_by_input(
                "una luminaria caida sobre la vereda",
                options,
            ),
            "Luminaria",
        )
        self.assertEqual(
            find_reclamo_category_by_input(
                "hay una rama peligrosa sobre la calle",
                options,
            ),
            "Arbolado",
        )

    def test_claim_category_conflict_delegates_instead_of_using_dict_order(self):
        options = _get_reclamos_menu().get("options_list", [])

        self.assertIsNone(
            find_reclamo_category_by_input("hay una rotura en la calle", options)
        )
        self.assertIsNone(
            find_reclamo_category_by_input("semaforo no funciona", options)
        )

    def test_cloacas_keyword(self):
        self.assertEqual(find_global_menu_action("cloacas"), "obras")

    def test_punto_limpio_keyword(self):
        self.assertEqual(find_global_menu_action("punto limpio"), "punto_limpio")

    def test_ayuda_keyword(self):
        self.assertEqual(find_global_menu_action("ayuda"), "mostrar_menu_ayuda")

    def test_tramite_button_keyword(self):
        self.assertEqual(
            find_global_menu_action("Requisitos y costos"),
            "licencia_de_conducir",
        )

    def test_menu_helper_starts_guided_claim_without_llm(self):
        owner = SimpleNamespace(municipio_id="default", id=1)
        contexto_menu = {
            "estado_conversacion": ConversationState.ESPERANDO_SELECCION_DE_LISTA.name,
            "menu_opciones": [
                {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ],
        }
        chat_ctx = SimpleNamespace(
            chat_session_id="test",
            context_data={CONTEXTO_MUNICIPIO: contexto_menu},
        )
        context = {CONTEXTO_MUNICIPIO: contexto_menu}
        app = Flask(__name__)
        llm_response = {"message_body": "Entendido, ya registré tu reclamo."}
        with app.app_context():
            with patch("services.municipio_responder.flag_modified") as mock_flag, \
                 patch(
                     "services.municipio_responder.handle_llm_interaction",
                     return_value=(llm_response, contexto_menu),
                 ) as mock_llm:
                response = _maybe_route_menu_input_to_llm(
                    "una luminaria caida en mi barrio esta parpadeando y esta torcido",
                    contexto_menu,
                    app,
                    context,
                    viewer_user=None,
                    owner_user=owner,
                    chat_db_context=chat_ctx,
                )

        mock_llm.assert_not_called()
        mock_flag.assert_called()
        self.assertIn("direcci", response["message_body"].lower())
        self.assertEqual(contexto_menu["estado_conversacion"], "EN_FLUJO_RECLAMO")

    def test_unknown_free_form_problem_reaches_llm_instead_of_fuzzy_menu(self):
        owner = SimpleNamespace(municipio_id="default", id=1)
        contexto_menu = {
            "estado_conversacion": ConversationState.ESPERANDO_SELECCION_DE_LISTA.name,
            "menu_opciones": [
                {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ],
        }
        chat_ctx = SimpleNamespace(
            chat_session_id="test-free-form-llm",
            context_data={CONTEXTO_MUNICIPIO: contexto_menu},
        )
        context = {
            "chat_db_context_data": chat_ctx.context_data,
            CONTEXTO_MUNICIPIO: contexto_menu,
        }
        app = Flask(__name__)
        llm_response = {
            "message_body": "Entiendo el problema; necesito precisar la categoría.",
            "fuente": "test_llm",
        }

        with app.app_context():
            with patch("services.municipio_responder.flag_modified"), patch(
                "services.municipio_responder.handle_llm_interaction",
                return_value=(llm_response, contexto_menu),
            ) as mock_llm:
                response = _maybe_route_menu_input_to_llm(
                    "semaforo no funciona",
                    contexto_menu,
                    app,
                    context,
                    viewer_user=None,
                    owner_user=owner,
                    chat_db_context=chat_ctx,
                )

        mock_llm.assert_called_once()
        self.assertEqual(response, llm_response)
        self.assertEqual(
            contexto_menu["estado_conversacion"],
            ConversationState.CONVERSACION_GENERAL_LLM.name,
        )


if __name__ == "__main__":
    unittest.main()
