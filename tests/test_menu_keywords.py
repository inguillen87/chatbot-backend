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
        self.assertFalse(_looks_like_free_form_input("2"))

    def test_agenda_keyword(self):
        self.assertEqual(find_global_menu_action("agenda"), "agenda_y_noticias")

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
        self.assertIsNone(chat_ctx.context_data[CONTEXTO_MUNICIPIO].get("estado_conversacion"))

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

    def test_menu_helper_escalates_to_llm(self):
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

        mock_llm.assert_called_once()
        mock_flag.assert_called_once()
        self.assertEqual(response, llm_response)
        self.assertEqual(
            contexto_menu["estado_conversacion"],
            ConversationState.CONVERSACION_GENERAL_LLM.name,
        )


if __name__ == "__main__":
    unittest.main()
