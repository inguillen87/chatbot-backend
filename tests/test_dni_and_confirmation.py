import unittest
from unittest.mock import MagicMock, patch
from services.common_utils import extract_multiple_contact_details_regex
from services.municipio_responder import (
    ReclamoFlowHandler,
    CONTEXTO_MUNICIPIO,
    decide_flow,
    ConversationState,
    responder_municipio,
    _merge_contact,
)
from types import SimpleNamespace
from flask import Flask

class TestDniExtractionAndConfirmation(unittest.TestCase):
    def test_extracts_dni(self):
        text = "Marcelo Guillen 32877851 guillen.marce@gmail.com 2613168608"
        data = extract_multiple_contact_details_regex(text)
        self.assertEqual(data.get("dni"), "32877851")
        self.assertEqual(data.get("email"), "guillen.marce@gmail.com")

    def test_extracts_all_contact_details(self):
        text = (
            "Marcelo Guillen 32877851 guillen.marce@gmail.com 2613168608 "
            "sarmiento esquina san martin junin mendoza"
        )
        data = extract_multiple_contact_details_regex(
            text, ["nombre", "dni", "email", "direccion", "telefono"]
        )
        self.assertEqual(data.get("nombre"), "Marcelo Guillen")
        self.assertEqual(data.get("direccion"), "sarmiento esquina san martin junin mendoza")
        self.assertEqual(data.get("telefono"), "+2613168608")

    def test_enumerated_name_phone_city(self):
        text = "1. Juan Perez\n2. 5491112345678\n3. CABA"
        data = extract_multiple_contact_details_regex(text)
        self.assertEqual(data.get("nombre"), "Juan Perez")
        self.assertEqual(data.get("telefono"), "+5491112345678")
        self.assertEqual(data.get("ciudad"), "CABA")

    def test_address_with_intersection(self):
        text = "don bosco 55 esquina sarmiento ciudad de junin mendoza"
        data = extract_multiple_contact_details_regex(text, ["direccion"])
        self.assertEqual(
            data.get("direccion"),
            "don bosco 55 esquina sarmiento ciudad de junin mendoza",
        )

    def test_no_pisar_nombre_con_direccion(self):
        text = "don bosco 55 esquina sarmiento"
        data = extract_multiple_contact_details_regex(text, ["nombre", "direccion"])
        self.assertIsNone(data.get("nombre"))
        self.assertEqual(data.get("direccion"), "don bosco 55 esquina sarmiento")

    def test_merge_contact_overwrites_address_name(self):
        base = {"nombre": "don bosco"}
        nuevo = {"nombre": "Marcelo Guillen"}
        merged = _merge_contact(base, nuevo)
        self.assertEqual(merged["nombre"], "Marcelo Guillen")

    def test_text_confirmation(self):
        context = {"chat_db_context_data": {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": {
            "datos_reclamo": {
                "categoria": "Luminaria",
                "direccion": "Calle 123",
                "descripcion": "poste caido",
                "nombre": "Juan Perez",
                "dni": "12345678",
                "email": "jp@example.com",
                "telefono": "+5400000000"
            },
            "state": "ESPERANDO_CONFIRMACION"
        }}}}
        handler = ReclamoFlowHandler(context, MagicMock())
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            inst = mock_handler.return_value
            inst.execute.return_value = {"success": True, "message_to_user": "ok", "options_list": []}
            resp = handler.handle_confirmacion("confirmar", {})
            self.assertIn("ok", resp["message_body"])
            inst.execute.assert_called_once()

    def test_router_prioriza_reclamo(self):
        txt = "árbol con ramas rotas en don bosco 55 esquina sarmiento"
        self.assertEqual(decide_flow(txt), "reclamo")

    def test_flow_sugerencia_avanza_con_direccion(self):
        app = Flask(__name__)
        owner_user = SimpleNamespace(id=1, municipio_id=1, rubro=SimpleNamespace(), tipo_chat='municipio')
        chat_ctx = SimpleNamespace(
            chat_session_id='test',
            context_data={
                CONTEXTO_MUNICIPIO: {
                    'estado_conversacion': ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name,
                    'datos_sugerencia': {
                        'categoria': 'Sugerencia',
                        'descripcion': 'desc',
                        'nombre': 'Vecino',
                        'dni': '123',
                        'email': 'a@a.com',
                    },
                    'contacto_usuario': {
                        'nombre': 'Vecino',
                        'dni': '123',
                        'email': 'a@a.com',
                    },
                }
            },
        )
        with app.app_context():
            with patch('services.municipio_responder.flag_modified', lambda *a, **k: None):
                with patch('services.municipio_responder.extract_multiple_contact_details_llm', return_value={}):
                    resp = responder_municipio(
                        'don bosco 55 esquina sarmiento',
                        owner_user,
                        owner_user.rubro,
                        chat_db_context=chat_ctx,
                    )
        self.assertIn('confirmá si los datos', resp['message_body'])
        self.assertEqual(
            chat_ctx.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'],
            ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name,
        )

    def test_guard_cruce_con_y_avanza(self):
        app = Flask(__name__)
        owner_user = SimpleNamespace(id=1, municipio_id=1, rubro=SimpleNamespace(), tipo_chat='municipio')
        chat_ctx = SimpleNamespace(
            chat_session_id='test',
            context_data={
                CONTEXTO_MUNICIPIO: {
                    'estado_conversacion': ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name,
                    'datos_sugerencia': {
                        'categoria': 'Sugerencia',
                        'descripcion': 'desc',
                        'nombre': 'Vecino',
                        'dni': '123',
                        'email': 'a@a.com',
                    },
                    'contacto_usuario': {
                        'nombre': 'Vecino',
                        'dni': '123',
                        'email': 'a@a.com',
                    },
                }
            },
        )
        with app.app_context():
            with patch('services.municipio_responder.flag_modified', lambda *a, **k: None):
                with patch('services.municipio_responder.extract_multiple_contact_details_llm', return_value={}):
                    with patch('services.municipio_responder.intent_classifier.classify', return_value=({'categoria': 'horarios_municipio', 'respuesta': ''}, 50)):
                        resp = responder_municipio(
                            'don bosco y sarmiento',
                            owner_user,
                            owner_user.rubro,
                            chat_db_context=chat_ctx,
                        )
        self.assertIn('confirmá si los datos', resp['message_body'])
        self.assertEqual(
            chat_ctx.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'],
            ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name,
        )

    def test_guard_cruce_con_y_avanza_con_acentos(self):
        app = Flask(__name__)
        owner_user = SimpleNamespace(id=1, municipio_id=1, rubro=SimpleNamespace(), tipo_chat='municipio')
        chat_ctx = SimpleNamespace(
            chat_session_id='test',
            context_data={
                CONTEXTO_MUNICIPIO: {
                    'estado_conversacion': ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name,
                    'datos_sugerencia': {
                        'categoria': 'Sugerencia',
                        'descripcion': 'desc',
                        'nombre': 'Vecino',
                        'dni': '123',
                        'email': 'a@a.com',
                    },
                    'contacto_usuario': {
                        'nombre': 'Vecino',
                        'dni': '123',
                        'email': 'a@a.com',
                    },
                }
            },
        )
        with app.app_context():
            with patch('services.municipio_responder.flag_modified', lambda *a, **k: None):
                with patch('services.municipio_responder.extract_multiple_contact_details_llm', return_value={}):
                    with patch('services.municipio_responder.intent_classifier.classify', return_value=({'categoria': 'horarios_municipio', 'respuesta': ''}, 50)):
                        resp = responder_municipio(
                            'san martín y pérez',
                            owner_user,
                            owner_user.rubro,
                            chat_db_context=chat_ctx,
                        )
        self.assertIn('confirmá si los datos', resp['message_body'])
        self.assertEqual(
            chat_ctx.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'],
            ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name,
        )

if __name__ == '__main__':
    unittest.main()
