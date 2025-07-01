import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import sys

# -- Crear stubs mínimos para dependencias pesadas --
models_stub = ModuleType('models')
class _DummyModel: pass
class _DummySession:
    def get(self, *a, **k):
        return None
    def add(self, *a, **k):
        pass
    def commit(self):
        pass
    def flush(self):
        pass
    def rollback(self):
        pass
models_stub.MunicipioTicket = _DummyModel
models_stub.PymeTicket = _DummyModel
models_stub.TicketComentario = _DummyModel
models_stub.TicketSatisfaccion = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub

twilio_rest_stub = ModuleType('twilio.rest')
class _DummyClient:
    class messages:
        @staticmethod
        def create(*a, **k):
            return SimpleNamespace(sid='dummy')
twilio_rest_stub.Client = _DummyClient
sys.modules.setdefault('twilio.rest', twilio_rest_stub)
sys.modules.setdefault('twilio', ModuleType('twilio'))
sys.modules.setdefault('cohere', ModuleType('cohere'))
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
class _SAError(Exception):
    pass
sqlalchemy_exc_stub.SQLAlchemyError = _SAError
sqlalchemy_stub.exc = sqlalchemy_exc_stub
sys.modules.setdefault('sqlalchemy', sqlalchemy_stub)
sys.modules.setdefault('sqlalchemy.exc', sqlalchemy_exc_stub)
sys.modules.setdefault('requests', ModuleType('requests'))

from services import municipios

class DummyTicket:
    def __init__(self, id=1, nro_ticket=123456):
        self.id = id
        self.nro_ticket = nro_ticket

class DummyUser(SimpleNamespace):
    def __init__(self):
        super().__init__(
            id=1,
            rubro=SimpleNamespace(nombre='municipio'),
            plan='full',
            preguntas_usadas=0,
            limite_preguntas=100,
            link_web='https://example.com',
            direccion='Avenida Siempreviva 123'
        )

class MunicipioLogicTests(unittest.TestCase):
    @patch('services.municipios.get_cohere_response', return_value='RESPUESTA_VALIDA')
    def test_es_pregunta_nueva_acknowledge(self, mock_llm):
        self.assertTrue(municipios.es_pregunta_nueva('ok', 'un número de ticket'))
        self.assertTrue(municipios.es_pregunta_nueva('gracias', 'una dirección'))

    @patch('services.municipios.get_cohere_response', return_value='RESPUESTA_VALIDA')
    def test_es_pregunta_nueva_agente(self, mock_llm):
        self.assertTrue(
            municipios.es_pregunta_nueva('Quiero hablar con un asesor', 'una dirección')
        )

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    @patch('services.municipios._clasificar_intencion_con_llm', return_value='hablar_con_agente')
    def test_human_escalation(self, mock_clf, mock_servicio, mock_llm):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        resp = municipios.responder_municipio('Hablar con un agente', user, None, viewer_user=user)
        self.assertIn('chat directa', resp['respuesta'])

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    @patch('services.municipios._clasificar_intencion_con_llm', return_value='hablar_con_agente')
    def test_human_escalation_anonymous_requires_login(self, mock_clf, mock_servicio, mock_llm):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        resp = municipios.responder_municipio('Hablar con un agente', DummyUser(), None, viewer_user=None)
        self.assertIn('iniciar sesión', resp['respuesta'])
        botones = resp.get('botones', [])
        self.assertTrue(any(b.get('action') == 'login' for b in botones))
        self.assertTrue(any(b.get('action') == 'register' for b in botones))

    def test_greeting_variation(self):
        user = DummyUser()
        resp = municipios.responder_municipio('hola buenos noches', user, None, viewer_user=user)
        self.assertIn('tu asistente digital del Municipio', resp['respuesta'])

    def test_small_talk_municipio(self):
        user = DummyUser()
        with patch('services.logic.get_cohere_response', side_effect=['SI', '¡Hola! ¿Todo bien!']) as mock_llm:
            resp = municipios.responder_municipio('¿Cómo te va?', user, None, viewer_user=user)
            self.assertIn('Hola', resp['respuesta'])
            self.assertEqual(mock_llm.call_count, 2)

    def test_tramite_selection_returns_string(self):
        """El texto de respuesta para un trámite debe ser una cadena."""
        user = DummyUser()
        # Primer paso: iniciar el flujo de trámites
        resp1 = municipios.responder_municipio('Quiero hacer un tramite', user, None, viewer_user=user)
        contexto = resp1.get('contexto_actualizado')
        self.assertIn('trámite', resp1['respuesta'].lower())
        # Seleccionamos un trámite específico
        resp2 = municipios.responder_municipio('Rentas', user, None, viewer_user=user, contexto_previo=contexto)
        self.assertIsInstance(resp2['respuesta'], str)
        self.assertIn('https://www.juninmendoza.gov.ar/vencimientos/', resp2['respuesta'])
        self.assertTrue(
            any(b.get('url') == 'https://www.juninmendoza.gov.ar/vencimientos/' for b in resp2.get('botones', []))
        )



if __name__ == '__main__':
    unittest.main()

# --- Integration Tests for Municipio Reclamo Flow with LLM Detail Extraction ---
from services.municipios import ConversationState, CONTEXTO_MUNICIPIO
from unittest.mock import MagicMock, patch
import json

class MunicipioReclamoFlowTests(unittest.TestCase):
    def setUp(self):
        self.owner_user = DummyUser()
        self.viewer_user = DummyUser()
        self.viewer_user.id = 200
        self.viewer_user.nombre = "Vecino Molesto"
        self.viewer_user.telefono = "2615550000"
        self.viewer_user.email = "vecino@example.com"

        if 'flask' in sys.modules:
            sys.modules['flask'].session = {}

        self.categorias_reclamo = municipios.CATEGORIAS_RECLAMO

    def _call_responder_municipio(self, pregunta_payload, current_municipio_context_state):
        pregunta_to_send = pregunta_payload
        if isinstance(pregunta_payload, str):
            pregunta_to_send = {'pregunta': pregunta_payload}
        elif 'pregunta' not in pregunta_to_send : # Ensure 'pregunta' key if dict
             pregunta_to_send['pregunta'] = ""


        contexto_previo_arg = {CONTEXTO_MUNICIPIO: current_municipio_context_state}

        user_query_mock = MagicMock()
        user_query_mock.get.side_effect = lambda user_id: self.viewer_user if user_id == self.viewer_user.id else None

        db_session_mock = MagicMock()
        db_session_mock.add = MagicMock()
        db_session_mock.commit = MagicMock()

        original_db_session = models_stub.db.session
        models_stub.db.session = db_session_mock

        with patch('services.municipios.User.query', user_query_mock): # Assuming User model might be used
            with patch('services.municipios._clasificar_intencion_con_llm') as mock_intent_classifier:
                if 'intencion_mock' in pregunta_to_send:
                    mock_intent_classifier.return_value = pregunta_to_send['intencion_mock']
                elif isinstance(pregunta_to_send, dict) and 'pregunta' in pregunta_to_send and \
                     any(kw in pregunta_to_send['pregunta'].lower() for kw in ["reclamo", "queja", "denuncia"]):
                     mock_intent_classifier.return_value = 'iniciar_reclamo'
                else:
                    mock_intent_classifier.return_value = 'pregunta_general'

                with patch('services.municipios.detectar_small_talk_con_llm', MagicMock(return_value=False)):
                    with patch('services.municipios.get_cohere_response', MagicMock(return_value="Respuesta genérica.")):
                        # Ensure the global `municipios.CATEGORIAS_RECLAMO` is accessible or mock if needed
                        # For this test, assuming it's correctly imported and available in the municipios module scope
                        response = municipios.responder_municipio(
                            pregunta_to_send,
                            owner_user=self.owner_user,
                            rubro_obj=SimpleNamespace(nombre='municipio'),
                            viewer_user=self.viewer_user,
                            contexto_previo=contexto_previo_arg,
                            chat_session_uuid="test-complaint-session"
                        )

        models_stub.db.session = original_db_session

        if response and response.get("contexto_actualizado", {}).get(CONTEXTO_MUNICIPIO):
            ctx_updated = response["contexto_actualizado"][CONTEXTO_MUNICIPIO]
            if isinstance(ctx_updated.get("estado_conversacion"), str):
                try:
                    ctx_updated["estado_conversacion"] = ConversationState[ctx_updated["estado_conversacion"]]
                except KeyError: # pragma: no cover
                    ctx_updated["estado_conversacion"] = None
            return response, ctx_updated, db_session_mock
        return response, current_municipio_context_state, db_session_mock

    @patch('services.llm_utils.robust_chat')
    def test_complaint_llm_extracts_all_initial_details(self, mock_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Quiero hacer un reclamo por una luminaria rota en Av. San Martin 123. La luz no funciona hace una semana y es peligroso."

        llm_response_data = {
            "tipo_problema": "luminaria",
            "ubicacion_problema": "Av. San Martin 123",
            "descripcion_problema": "La luz no funciona hace una semana y es peligroso."
        }
        mock_llm_robust_chat.return_value = json.dumps(llm_response_data)

        payload = {"pregunta": user_initial_complaint, "intencion_mock": "iniciar_reclamo"}
        response, municipio_context_state, _ = self._call_responder_municipio(payload, municipio_context_state)

        mock_llm_robust_chat.assert_called_once()

        self.assertEqual(municipio_context_state.get("categoria_reclamo"), "luminaria")
        self.assertEqual(municipio_context_state.get("direccion_reclamo"), "Av. San Martin 123")
        self.assertEqual(municipio_context_state.get("descripcion_reclamo"), "La luz no funciona hace una semana y es peligroso.")

        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_NOMBRE_VECINO)
        self.assertIn("nombre completo", response["respuesta"].lower())

        mock_llm_robust_chat.reset_mock()
        mock_llm_robust_chat.return_value = json.dumps({})
        response, municipio_context_state, _ = self._call_responder_municipio("Soy Vecino Preocupado", municipio_context_state)
        self.assertEqual(municipio_context_state.get("nombre_vecino"), "Vecino Preocupado")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_TELEFONO_VECINO)
        self.assertIn("número de teléfono", response["respuesta"].lower())

    @patch('services.llm_utils.robust_chat')
    def test_complaint_llm_extracts_partial_then_prompts(self, mock_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Hay un árbol caído en la plaza principal."

        llm_response_data = {
            "tipo_problema": "arbol caido", # This needs to exactly match or be very close to one in CATEGORIAS_RECLAMO
            "ubicacion_problema": "plaza principal"
        }
        mock_llm_robust_chat.return_value = json.dumps(llm_response_data)

        payload = {"pregunta": user_initial_complaint, "intencion_mock": "iniciar_reclamo"}
        response, municipio_context_state, _ = self._call_responder_municipio(payload, municipio_context_state)

        # Assuming "arbol caido" is a valid category or gets matched.
        self.assertEqual(municipio_context_state.get("categoria_reclamo").lower(), "arbol caido")
        self.assertEqual(municipio_context_state.get("direccion_reclamo"), "plaza principal")
        self.assertIsNone(municipio_context_state.get("descripcion_reclamo"))

        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_NOMBRE_VECINO)
        self.assertIn("nombre completo", response["respuesta"].lower())

        mock_llm_robust_chat.reset_mock()
        mock_llm_robust_chat.return_value = json.dumps({})
        response, municipio_context_state, _ = self._call_responder_municipio("Ana Vecina", municipio_context_state)
        self.assertEqual(municipio_context_state.get("nombre_vecino"), "Ana Vecina")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_TELEFONO_VECINO)

        mock_llm_robust_chat.return_value = json.dumps({})
        response, municipio_context_state, _ = self._call_responder_municipio("2612345678", municipio_context_state)
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_EMAIL_VECINO)

        mock_llm_robust_chat.return_value = json.dumps({})
        response, municipio_context_state, _ = self._call_responder_municipio("ana@vecina.com", municipio_context_state)

        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_DESCRIPCION_RECLAMO)
        self.assertIn("descripción del problema", response["respuesta"].lower())

        response, municipio_context_state, _ = self._call_responder_municipio("El árbol es grande y bloquea el paso.", municipio_context_state)
        self.assertEqual(municipio_context_state.get("descripcion_reclamo"), "El árbol es grande y bloquea el paso.")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_ADJUNTOS_RECLAMO)
        self.assertIn("adjuntar una foto", response["respuesta"].lower())

    @patch('services.llm_utils.robust_chat')
    def test_complaint_no_llm_extraction_traditional_flow(self, mock_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Tengo una queja."

        mock_llm_robust_chat.return_value = json.dumps({})

        payload = {"pregunta": user_initial_complaint, "intencion_mock": "iniciar_reclamo"}
        response, municipio_context_state, _ = self._call_responder_municipio(payload, municipio_context_state)

        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_CATEGORIA_RECLAMO)
        self.assertIn("categoría es tu reclamo", response["respuesta"].lower())

        mock_llm_robust_chat.return_value = json.dumps({})
        # Assuming 'luminaria' is a category that will be matched by the non-LLM logic in ReclamoHandler
        response, municipio_context_state, _ = self._call_responder_municipio("luminaria", municipio_context_state)
        self.assertEqual(municipio_context_state.get("categoria_reclamo"), "luminaria")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_DIRECCION_RECLAMO)
        self.assertIn("dirección exacta", response["respuesta"].lower())

        mock_llm_robust_chat.return_value = json.dumps({})
        response, municipio_context_state, _ = self._call_responder_municipio("Calle Luz Mala 100", municipio_context_state)
        self.assertEqual(municipio_context_state.get("direccion_reclamo"), "Calle Luz Mala 100")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_NOMBRE_VECINO)
        self.assertIn("nombre completo", response["respuesta"].lower())
