import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch, MagicMock # Added MagicMock
import sys
import importlib # Added importlib

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
models_stub.Conversacion = _DummyModel # Added missing assignment
models_stub.User = MagicMock()
models_stub.db = SimpleNamespace(session=_DummySession())
# sys.modules['models'] = models_stub # Will be handled by setUpClass/tearDownClass or setUp/tearDown

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
# sqlalchemy_stub = ModuleType('sqlalchemy') # Removed stubbing of entire sqlalchemy module
# sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
# class _SAError(Exception):
#     pass
# sqlalchemy_exc_stub.SQLAlchemyError = _SAError
# sqlalchemy_stub.exc = sqlalchemy_exc_stub
# sys.modules.setdefault('sqlalchemy', sqlalchemy_stub) # Removed stubbing
# sys.modules.setdefault('sqlalchemy.exc', sqlalchemy_exc_stub) # Removed stubbing
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
            rubro=SimpleNamespace(nombre='municipio'), # Ensure this matches expected structure if used
            plan='full',
            preguntas_usadas=0,
            limite_preguntas=100,
            link_web='https://example.com',
            direccion='Avenida Siempreviva 123',
            # Add other fields that might be accessed on owner_user in responder_municipio
            municipio_id='test_muni_id',
            nombre_empresa='Municipio Test Name'
        )

class MunicipioLogicTests(unittest.TestCase):
    original_models_module = None
    models_patchers = [] # To store patcher objects

    @classmethod
    def setUpClass(cls):
        # Instead of replacing sys.modules['models'], we will patch attributes directly within services.municipios

        # Define models from models_stub that services.municipios needs at import time
        # Use the locally defined _DummyModel or specific stubs for patching

        # Configure _DummyModel to have a mock 'query' attribute
        mock_query_for_dummy_models = MagicMock()
        mock_query_for_dummy_models.filter_by.return_value = mock_query_for_dummy_models
        mock_query_for_dummy_models.all.return_value = []
        mock_query_for_dummy_models.first.return_value = None
        mock_query_for_dummy_models.get.return_value = None
        _DummyModel.query = mock_query_for_dummy_models

        # models_stub.User is a MagicMock instance created at the module level of this test file.
        # We can configure its .query attribute directly if needed, or let MagicMock handle it.
        # For User, .query.get() is used by Flask-Login's user_loader.
        # models_stub.User.query.get.return_value = None # Default mock for User.query.get

        cls.patch_targets = {
            'services.municipios.MunicipioTicket': _DummyModel,
            'services.municipios.TicketComentario': _DummyModel,
            'services.municipios.db': models_stub.db, # models_stub.db is a SimpleNamespace with a mocked session
            'services.municipios.SitioWebInfo': _DummyModel,
            'services.municipios.Conversacion': _DummyModel,
            'services.municipios.User': models_stub.User, # Patch User in municipios namespace with the global MagicMock
            'services.municipios.CATEGORIAS_RECLAMO': [],
            'services.municipios.KEYWORD_TO_CATEGORY_MAP': {},
            'services.municipios.TOOL_REGISTRY': {},
        }


        for target_str, new_obj in cls.patch_targets.items():
            try:
                patcher = patch(target_str, new=new_obj)
                patcher.start()
                cls.models_patchers.append(patcher)
            except AttributeError:
                # This can happen if services.municipios doesn't directly import one of these
                # e.g. if it gets User via db.session.query(User) then models.User itself isn't needed in its namespace.
                print(f"Test setup: Could not patch {target_str}, it might not be directly imported or used in services.municipios at module level.")
                # If a model is only used inside functions, it might not need module-level patching here.
                # The critical ones are those causing ImportErrors at the top of services.municipios.py.

        importlib.reload(municipios) # Reload after models are patched for municipios's perspective

    @classmethod
    def tearDownClass(cls):
        for patcher in cls.models_patchers:
            patcher.stop()
        cls.models_patchers.clear()

        # Reload municipios again to restore its original imports if other test classes use it.
        # This might not be strictly necessary if tests are well-isolated or if this is the only test for municipios.
        importlib.reload(municipios)

# Patch flag_modified at the class level for MunicipioLogicTests
@patch('services.municipios.flag_modified', MagicMock())
class MunicipioLogicTests(unittest.TestCase):
    original_models_module = None
    models_patchers = [] # To store patcher objects

    @classmethod
    def setUpClass(cls):
        # Instead of replacing sys.modules['models'], we will patch attributes directly within services.municipios

        # Configure _DummyModel to have a mock 'query' attribute
        # This will apply to all classes patched with _DummyModel if they attempt to use .query
        mock_query_for_dummy_models = MagicMock()
        mock_query_for_dummy_models.filter_by.return_value = mock_query_for_dummy_models
        mock_query_for_dummy_models.all.return_value = []
        mock_query_for_dummy_models.first.return_value = None
        mock_query_for_dummy_models.get.return_value = None
        # Set .query on the class _DummyModel so all instances patched with it get this behavior
        _DummyModel.query = mock_query_for_dummy_models

        # models_stub.User is a MagicMock instance created at the module level of this test file.
        # We can configure its .query attribute directly if needed, or let MagicMock handle it.
        # For User, .query.get() is used by Flask-Login's user_loader.
        # models_stub.User.query.get = MagicMock(return_value=None) # Example if needed

        cls.patch_targets = {
            'services.municipios.MunicipioTicket': _DummyModel,
            'services.municipios.TicketComentario': _DummyModel,
            'services.municipios.db': models_stub.db, # models_stub.db is a SimpleNamespace with a mocked session
            'services.municipios.SitioWebInfo': _DummyModel,
            'services.municipios.Conversacion': _DummyModel,
            'services.municipios.User': models_stub.User, # Patch User in municipios namespace with the global MagicMock
            'services.municipios.CATEGORIAS_RECLAMO': [],
            'services.municipios.KEYWORD_TO_CATEGORY_MAP': {},
            'services.municipios.TOOL_REGISTRY': {},
        }

        for target_str, new_obj in cls.patch_targets.items():
            try:
                patcher = patch(target_str, new=new_obj)
                patcher.start()
                cls.models_patchers.append(patcher)
            except AttributeError:
                print(f"Test setup: Could not patch {target_str}, it might not be directly imported or used in services.municipios at module level.")

        importlib.reload(municipios)

    @classmethod
    def tearDownClass(cls):
        for patcher in cls.models_patchers:
            patcher.stop()
        cls.models_patchers.clear()

        importlib.reload(municipios)

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
    # Removed patch for _clasificar_intencion_con_llm
    def test_human_escalation(self, mock_servicio, mock_llm_cohere_generic): # mock_clf removed
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        # Simulate that the main Gemini call (orchestrator) set the intent
        with patch('services.gemini_bridge.llamar_gemini') as mock_main_gemini:
            mock_main_gemini.return_value = {
                "respuesta_usuario": "Te conectaré con un agente.",
                "accion_backend": "hablar_con_agente",
                "datos_estructura": {"target": "municipio"},
                "pedir_info": None, "botones": []
            }
            resp = municipios.responder_municipio(
                {'pregunta': 'Hablar con un agente', 'intencion': 'hablar_con_agente'}, # Pass intent in payload
                user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={})
            )
        self.assertIn('Hemos recibido tu solicitud', resp['message_body']) # Updated assertion
        self.assertIn('M-', resp['message_body']) # Check for ticket number pattern

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    # Removed patch for _clasificar_intencion_con_llm
    def test_human_escalation_anonymous_requires_login(self, mock_servicio, mock_llm_cohere_generic): # mock_clf removed
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        # Simulate that the main Gemini call (orchestrator) set the intent
        with patch('services.gemini_bridge.llamar_gemini') as mock_main_gemini:
            mock_main_gemini.return_value = {
                "respuesta_usuario": "Para hablar con un agente, necesitas registrarte.",
                "accion_backend": "hablar_con_agente", # LLM still identifies intent
                "datos_estructura": {"target": "municipio"},
                "pedir_info": "solicitar_registro_para_agente", "botones": []
            }
            resp = municipios.responder_municipio(
                {'pregunta': 'Hablar con un agente', 'intencion': 'hablar_con_agente'},
                DummyUser(), None, viewer_user=None, chat_db_context=SimpleNamespace(context_data={})
            )
        self.assertIn('iniciar sesión', resp['message_body']) # Changed 'respuesta' to 'message_body'
        botones = resp.get('options_list', []) # Changed 'botones' to 'options_list'
        self.assertTrue(any(b.get('action') == 'login' for b in botones))
        self.assertTrue(any(b.get('action') == 'register' for b in botones))

    def test_greeting_variation(self):
        user = DummyUser()
        resp = municipios.responder_municipio('hola buenos noches', user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={}))
        self.assertIn('tu asistente digital del Municipio', resp['message_body']) # Check new response structure

    def test_small_talk_municipio(self):
        user = DummyUser()
        with patch('services.logic.get_cohere_response', side_effect=['SI', '¡Hola! ¿Todo bien!']) as mock_llm:
            resp = municipios.responder_municipio('¿Cómo te va?', user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={}))
            self.assertIn('Hola', resp['message_body']) # Check new response structure
            self.assertEqual(mock_llm.call_count, 2)

    def test_tramite_selection_returns_string(self):
        """El texto de respuesta para un trámite debe ser una cadena."""
        user = DummyUser()
        # Primer paso: iniciar el flujo de trámites
        resp1 = municipios.responder_municipio('Quiero hacer un tramite', user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={}))
        contexto_actualizado_resp1 = resp1.get('contexto_actualizado') # Get the full updated context dict

        # The 'contexto_previo' for the next call should be the full dict that responder_municipio expects,
        # which includes the sub-dictionary under CONTEXTO_MUNICIPIO.
        # responder_municipio itself will extract context[CONTEXTO_MUNICIPIO] from chat_db_context.context_data.
        # So, we pass a SimpleNamespace simulating ChatSessionContext.
        chat_db_context_for_next_call = SimpleNamespace(context_data=contexto_actualizado_resp1 if contexto_actualizado_resp1 else {})

        self.assertIn('trámite', resp1['message_body'].lower())
        # Seleccionamos un trámite específico
        # Mockear la llamada a Gemini para la segunda interacción, ya que el flujo de trámite ahora depende de ella.
        with patch('services.gemini_bridge.llamar_gemini') as mock_main_gemini_tramite:
            mock_main_gemini_tramite.return_value = {
                "respuesta_usuario": "Aquí está la info de Rentas: ...juninmendoza.gov.ar/vencimientos/...",
                "accion_backend": "info_tramite",
                "datos_estructura": {"target": "municipio", "categoria": "Rentas"}, # Asegurar que el target y la categoría sean consistentes
                "pedir_info": None,
                "botones": [{"texto": "Ver Vencimientos", "url": "https://www.juninmendoza.gov.ar/vencimientos/"}]
            }
            resp2 = municipios.responder_municipio(
                {'pregunta': 'Rentas', 'intencion': 'consultar_tramite'}, # Simular payload con intención
                user,
                None,
                viewer_user=user,
                chat_db_context=chat_db_context_for_next_call
            )
        self.assertIsInstance(resp2.get('message_body'), str) # Changed 'respuesta' to 'message_body'
        self.assertIn('https://www.juninmendoza.gov.ar/vencimientos/', resp2.get('message_body',''))

        url_en_botones = any(b.get('url') == 'https://www.juninmendoza.gov.ar/vencimientos/' for b in resp2.get('options_list', []))
        url_en_cuerpo = 'https://www.juninmendoza.gov.ar/vencimientos/' in resp2.get('message_body','')
        self.assertTrue(url_en_botones or url_en_cuerpo, "URL de Rentas no encontrada ni en botones ni en cuerpo.")



if __name__ == '__main__':
    unittest.main()

# --- Integration Tests for Municipio Reclamo Flow with LLM Detail Extraction ---
from services.municipios import ConversationState, CONTEXTO_MUNICIPIO
from unittest.mock import MagicMock, patch
import json

class MunicipioReclamoFlowTests(unittest.TestCase):
    def setUp(self):
        # Create a new app for each test to ensure isolation and context
        from app import create_app # Import create_app locally
        self.app = create_app() # You might need a TestConfig here if not default
        self.app_context = self.app.app_context()
        self.app_context.push()
        # If your tests involve DB operations that need to be clean,
        # you might also want db.create_all() here and db.drop_all() in tearDown.
        # However, _call_responder_municipio heavily mocks DB interactions.

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

        # Corrected patch target from 'services.municipios.User.query' to 'models.User.query'
        # This assumes that if User.query is used within municipios.py, it's via an import of models.User
        with patch('models.User.query', user_query_mock):
            with patch('services.municipios.detectar_small_talk_con_llm', MagicMock(return_value=False)):
                with patch('services.municipios.get_cohere_response', MagicMock(return_value="Respuesta genérica.")):
                    with patch('services.gemini_bridge.llamar_gemini') as mock_llamar_gemini:
                        # Simulate a generic Gemini response
                        simulated_gemini_intent = pregunta_to_send.get('intencion_mock', 'pregunta_general')
                        mock_llamar_gemini.return_value = {
                            "respuesta_usuario": "Respuesta simulada de Gemini.",
                            "accion_backend": simulated_gemini_intent,
                            "datos_estructura": pregunta_to_send.get("datos_accion_mock", {
                                "target": "municipio", "categoria": None, "descripcion": None, "ubicacion": None
                            }),
                            "pedir_info": None, "botones": []
                        }
                        if 'llamar_gemini_mock_return' in pregunta_to_send:
                            mock_llamar_gemini.return_value = pregunta_to_send['llamar_gemini_mock_return']

                        # Patch flag_modified to prevent AttributeError with SimpleNamespace
                        with patch('services.municipios.flag_modified') as mock_flag_modified:
                            response = municipios.responder_municipio(
                                pregunta_to_send,
                                owner_user=self.owner_user,
                                rubro_obj=SimpleNamespace(nombre='municipio'),
                                viewer_user=self.viewer_user,
                                chat_db_context=SimpleNamespace(context_data=contexto_previo_arg),
                                chat_session_uuid="test-complaint-session"
                            )
                            # mock_flag_modified.assert_called() # Optionally assert it was called

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
        # This mock is for the ReclamoHandler's internal call to extract_complaint_details_llm (via robust_chat)
        mock_llm_robust_chat.return_value = json.dumps(llm_response_data)

        # Simulate main Gemini call providing only the intent and basic text,
        # forcing ReclamoHandler to use its internal LLM call for details.
        payload = {
            "pregunta": user_initial_complaint,
            "intencion_mock": "iniciar_reclamo", # Fallback for IntentClassifier if main LLM is too generic
            "llamar_gemini_mock_return": { # Mock for the main Gemini call in responder_municipio
                 "respuesta_usuario": "Entendido. ¿Podrías darme más detalles sobre la luminaria?",
                 "accion_backend": "iniciar_reclamo", # Main LLM confirms it's a claim
                 "datos_estructura": { # But provides no specific extracted data initially
                    "target": "municipio",
                    "categoria": None, # Force ReclamoHandler to ask or deduce
                    "descripcion": user_initial_complaint, # Pass the original text as description
                    "ubicacion": None
                 },
                 "pedir_info": "categoria", # Example: main LLM asks for category first
                 "botones": []
            }
        }
        # The first call to responder_municipio will now use the above ^ mock for llamar_gemini.
        # ReclamoHandler should then enter a state like ESPERANDO_CATEGORIA_RECLAMO or ESPERANDO_DIRECCION_RECLAMO.
        # If the user then provides the full text again (or if pregunta_str is still user_initial_complaint),
        # ReclamoHandler's internal LLM call (mocked by mock_llm_robust_chat) should be triggered.

        # To ensure ReclamoHandler's internal LLM is called, we need to simulate the flow:
        # 1. Initial call recognizes intent, asks for category (state becomes ESPERANDO_CATEGORIA_RECLAMO)
        # 2. User provides category "luminaria".
        # 3. Bot asks for address (state becomes ESPERANDO_DIRECCION_RECLAMO).
        # 4. User provides address "Av. San Martin 123".
        #    Now, `pregunta_str` for this call to ReclamoHandler will be "Av. San Martin 123".
        #    The internal LLM in ReclamoHandler should try to extract details from this.
        #    However, the test is set up to check if extract_complaint_details_llm is called with user_initial_complaint.
        #    This test needs rethinking if the main LLM is now the primary extractor.

        # For this test to assert mock_llm_robust_chat.assert_called_once(),
        # the ReclamoHandler's internal LLM call must be triggered with a relevant pregunta_str.
        # Let's adjust the scenario: Assume main LLM identifies intent, and ReclamoHandler
        # is in ESPERANDO_DESCRIPCION_RECLAMO, and the user provides the full text again.

        # Simulate initial steps to get to a state where ReclamoHandler might call its internal LLM
        # This is complex to set up perfectly without running the multi-turn.
        # Let's assume the main LLM did not extract details, and ReclamoHandler is now asking.
        # The 'pregunta' in the payload to _call_responder_municipio will be what ReclamoHandler's LLM sees.

        # Redesigned approach for this test:
        # We want to test if ReclamoHandler's internal LLM call (extract_multiple_contact_details_llm)
        # works when it's in a state like ESPERANDO_DIRECCION_RECLAMO and the user provides text.

        # Simulate being in ESPERANDO_DIRECCION_RECLAMO, with categoria already set
        municipio_context_state = {
            "estado_conversacion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
            "categoria_reclamo": "luminaria" # Pre-set from a hypothetical previous step
        }

        # The user's input when asked for address is the full original complaint again
        # This is the text that ReclamoHandler's internal LLM will process.
        payload_for_internal_llm_test = {
            "pregunta": user_initial_complaint, # This text will be processed by ReclamoHandler's internal LLM
            "intencion": "continuar_flujo", # No new intent from main LLM
             "llamar_gemini_mock_return": { # Main LLM just continues flow
                 "respuesta_usuario": "¿Cuál es la dirección?", # Irrelevant here as we check internal
                 "accion_backend": "continuar_flujo",
                 "datos_estructura": {"target": "municipio"},
                 "pedir_info": None, "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_for_internal_llm_test, municipio_context_state)

        mock_llm_robust_chat.assert_called_once() # This should now be called by ReclamoHandler

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

    def tearDown(self):
        if hasattr(self, 'app_context') and self.app_context:
            self.app_context.pop()

    @patch('services.llm_utils.robust_chat')
    def test_complaint_llm_extracts_partial_then_prompts(self, mock_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Hay un árbol caído en la plaza principal."

        llm_response_data = {
            "tipo_problema": "arbol caido", # This needs to exactly match or be very close to one in CATEGORIAS_RECLAMO
            "ubicacion_problema": "plaza principal"
        }
        mock_llm_robust_chat.return_value = json.dumps(llm_response_data) # This mock might not be hit if main Gemini provides data

        # Simulate main Gemini call extracting category and location
        datos_accion_simulados = {
            "target": "municipio",
            "categoria": "Arbol Caido", # Ensure exact match with CATEGORIAS_RECLAMO or normalization
            "descripcion": None, # LLM might not get a full description initially
            "ubicacion": "plaza principal"
        }
        payload = {
            "pregunta": user_initial_complaint,
            "intencion_mock": "iniciar_reclamo", # For old IntentClassifier if hit
            "llamar_gemini_mock_return": { # Mock for the main Gemini call
                 "respuesta_usuario": "Entendido lo del árbol en la plaza. ¿Podrías darme más detalles?",
                 "accion_backend": "iniciar_reclamo",
                 "datos_estructura": datos_accion_simulados,
                 "pedir_info": "descripcion_mas_detallada", # Example
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload, municipio_context_state)

        # Now ReclamoInteligenteHandler should pick up "Arbol Caido" and "plaza principal"
        # from datos_accion (via llamar_gemini_mock_return)
        self.assertEqual(municipio_context_state.get("categoria_reclamo"), "Arbol Caido")
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
        self.assertIn("categoría es tu reclamo", response.get("message_body", "").lower())

        mock_llm_robust_chat.return_value = json.dumps({})
        # Assuming 'luminaria' is a category that will be matched by the non-LLM logic in ReclamoHandler
        response, municipio_context_state, _ = self._call_responder_municipio("luminaria", municipio_context_state)
        self.assertEqual(municipio_context_state.get("categoria_reclamo"), "luminaria")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_DIRECCION_RECLAMO)
        self.assertIn("dirección exacta", response.get("message_body", "").lower())

        mock_llm_robust_chat.return_value = json.dumps({})
        response, municipio_context_state, _ = self._call_responder_municipio("Calle Luz Mala 100", municipio_context_state)
        self.assertEqual(municipio_context_state.get("direccion_reclamo"), "Calle Luz Mala 100")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_NOMBRE_VECINO)
        self.assertIn("nombre completo", response.get("message_body", "").lower())
