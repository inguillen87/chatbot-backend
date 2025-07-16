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
    def setUp(self):
        from app import create_app
        self.app = create_app()

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

    def test_es_pregunta_nueva_acknowledge(self):
        with self.app.app_context():
            self.assertTrue(municipios.es_pregunta_nueva('ok', 'un número de ticket'))
            self.assertTrue(municipios.es_pregunta_nueva('gracias', 'una dirección'))

    def test_es_pregunta_nueva_agente(self):
        with self.app.app_context():
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
        with patch('services.municipios.ChatOrchestrator.execute_action') as mock_execute_action:
            mock_execute_action.return_value = {
                "message_to_user": "Hemos recibido tu solicitud para hablar con un agente. Estamos notificando al equipo. Tu número de chat es **M-123456**. Un agente se unirá tan pronto como esté disponible.",
                "success": True,
                "fuente": "escalation_sala_creada_v2",
                "data": {"ticket_id": 1}
            }
            with self.app.app_context():
                resp = municipios.responder_municipio(
                    {'pregunta': 'Hablar con un agente'},
                    user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={})
                )
        self.assertIn('Hemos recibido tu solicitud', resp['message_body'])
        self.assertIn('M-', resp['message_body'])

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    def test_human_escalation_anonymous_requires_login(self, mock_servicio, mock_llm_cohere_generic):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        with patch('services.gemini_bridge.llamar_gemini') as mock_llamar_gemini:
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "Para hablar con un agente, necesitas registrarte o iniciar sesión.",
                "accion_backend": "derivar_humano", # Still deriving, but context should indicate anon
                "datos_estructura": {"target": "municipio", "motivo_derivacion": "Solicitud de agente anónimo"},
                "pedir_info": "solicitar_registro_para_agente", # This might be handled by EngancheAnonimo
                "botones": [
                    {"texto": "Iniciar Sesión", "id_accion": "login_enganche_risky"},
                    {"texto": "Registrarme Gratis", "id_accion": "register_enganche_risky"}
                ]
            }
            # Here, EngancheAnonimoMunicipioHandler should intercept if anon_id is passed and viewer_user is None
            # Let's simulate this by setting the context['intencion'] to 'hablar_con_agente'
            # and ensuring viewer_user is None and anon_id is present.
            # The _call_responder_municipio sets viewer_user, so we call responder_municipio directly.

            context_for_anon_escalation = SimpleNamespace(context_data={
                municipios.CONTEXTO_MUNICIPIO: {}, # Start with empty municipio context
                "intencion": "hablar_con_agente" # Set by a hypothetical previous step or main LLM
            })

            with self.app.app_context():
                resp = municipios.responder_municipio(
                    pregunta_original={'pregunta': 'Quiero hablar con una persona', 'intencion': 'hablar_con_agente'}, # Ensure intent is in context
                    owner_user=DummyUser(),
                    rubro_obj=None,
                    viewer_user=None, # Critical for anon path
                    chat_db_context=context_for_anon_escalation,
                    anon_id="test_anon_id_escalation"
                )

        self.assertIn('iniciar sesión o registrarte', resp['message_body'].lower())
        botones = resp.get('options_list', [])
        self.assertTrue(any('login' in b.get('id_accion', '').lower() for b in botones) or any('iniciar sesión' in b.get('texto', '').lower() for b in botones) )
        self.assertTrue(any('register' in b.get('id_accion', '').lower() for b in botones) or any('registrarme' in b.get('texto', '').lower() for b in botones) )


    def test_greeting_variation(self):
        user = DummyUser()
        with patch('services.gemini_bridge.llamar_gemini') as mock_llamar_gemini:
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "¡Hola! 👋 Soy tu asistente digital del Municipio. ¿En qué puedo ayudarte hoy?",
                "accion_backend": "saludar", # Or "small_talk"
                "datos_estructura": {"target": "municipio"},
                "pedir_info": None,
                "botones": [
                    {"texto": "Hacer un reclamo", "id_accion": "iniciar_reclamo"},
                    {"texto": "Consultar un trámite", "id_accion": "consultar_tramite"}
                ]
            }
            with self.app.app_context():
                resp = municipios.responder_municipio(
                    'hola buenos noches',
                    user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={})
                )
        self.assertIn('tu asistente digital del Municipio', resp['message_body'])

    def test_small_talk_municipio(self):
        user = DummyUser()
        # Old mock for services.logic.get_cohere_response is no longer relevant here.
        # We need to mock services.gemini_bridge.llamar_gemini
        with patch('services.gemini_bridge.llamar_gemini') as mock_llamar_gemini:
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "¡Hola! Todo bien por aquí. ¿En qué te puedo asistir?",
                "accion_backend": "small_talk",
                "datos_estructura": {"target": "municipio"},
                "pedir_info": None,
                "botones": [{"texto": "Hacer un reclamo"}, {"texto": "Ayuda"}]
            }
            with self.app.app_context():
                resp = municipios.responder_municipio(
                    '¿Cómo te va?',
                    user, None, viewer_user=user, chat_db_context=SimpleNamespace(context_data={})
                )
            self.assertIn('Todo bien por aquí', resp['message_body']) # Check new response structure
            mock_llamar_gemini.assert_called_once()


    def test_tramite_selection_returns_string(self):
        user = DummyUser()
        chat_context_sim = SimpleNamespace(context_data={})

        # Step 1: User says "Quiero hacer un tramite"
        with patch('services.gemini_bridge.llamar_gemini') as mock_gemini_step1:
            mock_gemini_step1.return_value = {
                "respuesta_usuario": "¿Sobre qué trámite necesitás información?",
                "accion_backend": "consultar_tramite", # LLM identifies intent
                "datos_estructura": {"target": "municipio"},
                "pedir_info": "nombre_tramite", # LLM asks for the specific trámite
                "botones": [{"texto": "Licencia de Conducir"}, {"texto": "Rentas"}]
            }
            with self.app.app_context():
                resp1 = municipios.responder_municipio(
                    'Quiero hacer un tramite', user, None, viewer_user=user, chat_db_context=chat_context_sim
                )

        self.assertIn('¿Sobre qué trámite necesitás información?', resp1['message_body'])
        # Update chat_context_sim with the context from resp1 for the next call
        if resp1.get("contexto_actualizado"):
            chat_context_sim.context_data.update(resp1["contexto_actualizado"])


        # Step 2: User selects "Rentas"
        with patch('services.gemini_bridge.llamar_gemini') as mock_gemini_step2:
            # Simulate LLM recognizing "Rentas" and triggering an action to provide info for it.
            # This might involve an "ejecutar_herramienta" if "Rentas" info is a tool,
            # or directly "info_tramite" if the LLM has the info or the action handler fetches it.
            # For this test, let's assume the action handler for "info_tramite" (or a tool) will format the response.
            mock_gemini_step2.return_value = {
                "respuesta_usuario": "Aquí está la información sobre Rentas: El pago de tasas municipales se realiza en la oficina de Rentas, de Lunes a Viernes de 8 a 13hs. Más info en https://www.juninmendoza.gov.ar/vencimientos/",
                "accion_backend": "info_tramite", # Or "ejecutar_herramienta" if Rentas is a tool
                "datos_estructura": {
                    "target": "municipio",
                    "categoria": "Rentas", # LLM identifies the category
                    # If it were a tool:
                    # "nombre_herramienta": "consultar_info_tramite",
                    # "parametros_herramienta": {"nombre_tramite": "Rentas"}
                },
                "pedir_info": None,
                "botones": [
                    {"texto": "Ver Vencimientos Online", "url": "https://www.juninmendoza.gov.ar/vencimientos/"},
                    {"texto": "Consultar otro trámite"}
                ]
            }
            with self.app.app_context():
                resp2 = municipios.responder_municipio(
                    {'pregunta': 'Rentas', 'intencion': 'consultar_tramite'}, # User selects "Rentas"
                    user,
                    None,
                    viewer_user=user,
                    chat_db_context=chat_context_sim # Pass the updated context
                )

        self.assertIsInstance(resp2.get('message_body'), str)
        self.assertIn('https://www.juninmendoza.gov.ar/vencimientos/', resp2.get('message_body',''))

        # Check if the URL is either in the message body (for WhatsApp) or as a button (for web)
        url_present = 'https://www.juninmendoza.gov.ar/vencimientos/' in resp2.get('message_body','') or \
                      any(b.get('url') == 'https://www.juninmendoza.gov.ar/vencimientos/' for b in resp2.get('options_list', []))
        self.assertTrue(url_present, "URL de Rentas no encontrada ni en botones ni en cuerpo.")


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
        # self.app_context = self.app.app_context() # Context will be managed by with self.app.app_context() in each test
        # self.app_context.push()
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
                        # Default mock for llamar_gemini if no specific override is provided in payload
                        default_llm_response = {
                            "respuesta_usuario": "Respuesta por defecto de Gemini (mock).",
                            "accion_backend": "small_talk", # Default to a benign action
                            "datos_estructura": {"target": "general"},
                            "pedir_info": None,
                            "botones": [{"texto": "Ayuda"}]
                        }

                        # Allow tests to override the mock return value via the payload
                        mock_llamar_gemini.return_value = pregunta_to_send.get('llamar_gemini_mock_return', default_llm_response)

                        # Patch flag_modified to prevent AttributeError with SimpleNamespace
                        with patch('services.municipios.flag_modified') as mock_flag_modified:
                            with self.app.app_context():
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

    @patch('services.llm_utils.robust_chat') # This mocks the internal LLM call in ReclamoHandler
    def test_complaint_llm_extracts_all_initial_details(self, mock_internal_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Quiero hacer un reclamo por una luminaria rota en Av. San Martin 123. La luz no funciona hace una semana y es peligroso. Mi nombre es Vecino Preocupado, tel 261000111, email vecino.preocupado@example.com"

        # Simulate main Gemini call extracting ALL details
        main_llm_extraction = {
            "target": "municipio",
            "categoria": "Alumbrado Público", # Assuming LLM normalizes/maps this
            "descripcion": "Luminaria rota, no funciona hace una semana y es peligroso.",
            "ubicacion": "Av. San Martin 123",
            "nombre_usuario_detectado": "Vecino Preocupado",
            "telefono_detectado": "261000111",
            "email_detectado": "vecino.preocupado@example.com"
        }

        payload = {
            "pregunta": user_initial_complaint,
            "llamar_gemini_mock_return": {
                 "respuesta_usuario": "Gracias. Hemos registrado tu reclamo por la luminaria. ¿Algo más?", # Example response
                 "accion_backend": "crear_reclamo_municipio",
                 "datos_estructura": main_llm_extraction,
                 "pedir_info": None,
                 "botones": [{"texto": "Ver estado reclamo"}, {"texto": "Nuevo reclamo"}]
            }
        }

        # The internal LLM in ReclamoHandler should NOT be called if main LLM provides all data
        mock_internal_llm_robust_chat.return_value = json.dumps({})

        response, municipio_context_state, db_mock = self._call_responder_municipio(payload, municipio_context_state)

        mock_internal_llm_robust_chat.assert_not_called() # Crucial: internal LLM should be skipped

        # Check if the `accion_crear_reclamo_municipio` was effectively called by the orchestrator
        # This is indirect; we check if a ticket was created by servicio_tickets.crear_nuevo_ticket
        # which is called by `accion_crear_reclamo_municipio`.
        # We need to patch `servicio_tickets.crear_nuevo_ticket` for this.
        with patch('services.ticket_service.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
            mock_crear_ticket.return_value = SimpleNamespace(id=999, nro_ticket="T999") # Simulate successful ticket creation

            # Re-run with the ticket creation mock active
            response, municipio_context_state, db_mock = self._call_responder_municipio(payload, {}) # Fresh context

            mock_crear_ticket.assert_called_once()
            args_call, kwargs_call = mock_crear_ticket.call_args
            ticket_data_sent = kwargs_call.get('ticket_data', {})

            self.assertEqual(ticket_data_sent.get("categoria"), "Alumbrado Público")
            self.assertEqual(ticket_data_sent.get("direccion"), "Av. San Martin 123")
            self.assertIn("Luminaria rota", ticket_data_sent.get("detalles"))
            self.assertEqual(ticket_data_sent.get("nombre_vecino"), "Vecino Preocupado")
            self.assertIsNotNone(ticket_data_sent.get("telefono_vecino")) # Check it's processed
            self.assertEqual(ticket_data_sent.get("email_vecino"), "vecino.preocupado@example.com")

        # After successful creation by LLM, context should be cleared or state reset
        # The exact state depends on `accion_crear_reclamo_municipio`'s return and `responder_municipio` logic
        self.assertIsNone(municipio_context_state.get("estado_conversacion")) # Expect cleared state
        self.assertIn("reclamo ha sido registrado", response["message_body"])



    @patch('services.llm_utils.robust_chat') # Mock for ReclamoHandler's internal LLM (should not be called much)
    def test_complaint_llm_extracts_partial_then_prompts(self, mock_internal_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Hay un árbol caído en la plaza principal."

        mock_internal_llm_robust_chat.return_value = json.dumps({}) # Default for internal, should not be relied upon heavily

        # 1. Initial complaint: Main LLM extracts some info, asks for more
        main_llm_extraction_step1 = {
            "target": "municipio",
            "categoria": "Arbol Caido", # Matched by LLM
            "descripcion": "árbol caído", # Partial description
            "ubicacion": "plaza principal"
        }
        payload_step1 = {
            "pregunta": user_initial_complaint,
            "llamar_gemini_mock_return": {
                 "respuesta_usuario": "Entendido lo del árbol caído en la plaza. Para completar el reclamo, ¿podrías darme tu nombre completo?",
                 "accion_backend": "iniciar_reclamo", # Or "crear_reclamo_municipio" if it can proceed partially
                 "datos_estructura": main_llm_extraction_step1,
                 "pedir_info": "nombre_completo",
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step1, municipio_context_state)

        self.assertEqual(municipio_context_state.get("categoria_reclamo"), "Arbol Caido")
        self.assertEqual(municipio_context_state.get("direccion_reclamo"), "plaza principal")
        self.assertEqual(municipio_context_state.get("descripcion_reclamo"), "árbol caído")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_NOMBRE_VECINO)
        self.assertIn("nombre completo", response["message_body"].lower())

        # 2. User provides name
        payload_step2 = {
            "pregunta": "Soy Ana Vecina",
            "llamar_gemini_mock_return": { # Main LLM just continues flow
                 "respuesta_usuario": "Gracias Ana. ¿Tu número de teléfono?",
                 "accion_backend": "continuar_flujo", # No new primary action, just data gathering
                 "datos_estructura": {"target": "municipio", "nombre_usuario_detectado": "Ana Vecina"}, # LLM might echo back
                 "pedir_info": "telefono",
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step2, municipio_context_state)
        self.assertEqual(municipio_context_state.get("nombre_vecino"), "Ana Vecina")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_TELEFONO_VECINO)
        self.assertIn("número de teléfono", response["message_body"].lower())

        # 3. User provides phone
        payload_step3 = {
            "pregunta": "Es 2612345678",
            "llamar_gemini_mock_return": {
                 "respuesta_usuario": "Perfecto. ¿Y tu email?",
                 "accion_backend": "continuar_flujo",
                 "datos_estructura": {"target": "municipio", "telefono_detectado": "2612345678"},
                 "pedir_info": "email",
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step3, municipio_context_state)
        self.assertTrue(validar_telefono(municipio_context_state.get("telefono_vecino")))
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_EMAIL_VECINO)
        self.assertIn("email", response["message_body"].lower())

        # 4. User provides email
        payload_step4 = {
            "pregunta": "ana@vecina.com",
             "llamar_gemini_mock_return": {
                 "respuesta_usuario": "Gracias. ¿Querés agregar algo más a la descripción del árbol caído?",
                 "accion_backend": "continuar_flujo",
                 "datos_estructura": {"target": "municipio", "email_detectado": "ana@vecina.com"},
                 "pedir_info": "descripcion_adicional_opcional", # Or could go to adjuntos / confirmacion
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step4, municipio_context_state)
        self.assertEqual(municipio_context_state.get("email_vecino"), "ana@vecina.com")
        # Assuming description was already partially filled, it might ask for adjuntos or confirmation
        # Let's assume it asks for adjuntos if description is minimal
        if not municipio_context_state.get("descripcion_reclamo") or len(municipio_context_state.get("descripcion_reclamo")) < 20:
             self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_DESCRIPCION_RECLAMO)
             self.assertIn("detalle cuál es el problema", response["message_body"].lower())
             # Now provide full description
             payload_step5_desc = {
                "pregunta": "Sí, el árbol es muy grande y está bloqueando toda la calle.",
                "llamar_gemini_mock_return": {
                     "respuesta_usuario": "Entendido. ¿Querés adjuntar una foto?",
                     "accion_backend": "continuar_flujo",
                     "datos_estructura": {"target": "municipio", "descripcion": "Sí, el árbol es muy grande y está bloqueando toda la calle."},
                     "pedir_info": "adjuntos",
                     "botones": []
                }}
             response, municipio_context_state, _ = self._call_responder_municipio(payload_step5_desc, municipio_context_state)
             self.assertEqual(municipio_context_state.get("descripcion_reclamo"), "Sí, el árbol es muy grande y está bloqueando toda la calle.")
             self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_ADJUNTOS_RECLAMO)
        else: # If description was sufficient
            self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_ADJUNTOS_RECLAMO)

        self.assertIn("adjuntar una foto", response["message_body"].lower())


    @patch('services.llm_utils.robust_chat')
    def test_complaint_no_llm_extraction_traditional_flow(self, mock_internal_llm_robust_chat):
        municipio_context_state = {}
        user_initial_complaint = "Tengo una queja."

        mock_internal_llm_robust_chat.return_value = json.dumps({}) # Internal LLM should not extract much in this flow

        # 1. Initial generic complaint
        payload_step1 = {
            "pregunta": user_initial_complaint,
            "llamar_gemini_mock_return": {
                 "respuesta_usuario": "Entendido. ¿Sobre qué categoría es tu reclamo?",
                 "accion_backend": "iniciar_reclamo",
                 "datos_estructura": {"target": "municipio"}, # Minimal extraction
                 "pedir_info": "categoria",
                 "botones": [{"texto": "Luminaria"}, {"texto": "Arbolado"}]
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step1, municipio_context_state)

        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_CATEGORIA_RECLAMO)
        self.assertIn("categoría es tu reclamo", response.get("message_body", "").lower())

        # 2. User provides category
        payload_step2 = {
            "pregunta": "luminaria", # User chooses/types category
            "llamar_gemini_mock_return": { # Main LLM just continues
                 "respuesta_usuario": "Ok, luminaria. ¿Cuál es la dirección exacta del problema?",
                 "accion_backend": "continuar_flujo",
                 "datos_estructura": {"target": "municipio", "categoria": "luminaria"}, # LLM might confirm category
                 "pedir_info": "ubicacion",
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step2, municipio_context_state)
        # In the traditional flow, ReclamoHandler itself matches "luminaria" to a known category.
        # We need to ensure municipios.CATEGORIAS_RECLAMO is populated for this test if ReclamoHandler relies on it.
        # For now, assume 'luminaria' is directly set or matched by ReclamoHandler's non-LLM logic.
        self.assertEqual(municipio_context_state.get("categoria_reclamo").lower(), "luminaria")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_DIRECCION_RECLAMO)
        self.assertIn("dirección exacta", response.get("message_body", "").lower())

        # 3. User provides address
        payload_step3 = {
            "pregunta": "Calle Luz Mala 100",
            "llamar_gemini_mock_return": {
                 "respuesta_usuario": "Anotado: Calle Luz Mala 100. ¿Tu nombre completo?",
                 "accion_backend": "continuar_flujo",
                 "datos_estructura": {"target": "municipio", "ubicacion": "Calle Luz Mala 100"},
                 "pedir_info": "nombre_completo",
                 "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload_step3, municipio_context_state)
        self.assertEqual(municipio_context_state.get("direccion_reclamo"), "Calle Luz Mala 100")
        self.assertEqual(municipio_context_state.get("estado_conversacion"), ConversationState.ESPERANDO_NOMBRE_VECINO)
        self.assertIn("nombre completo", response.get("message_body", "").lower())

    def test_invalid_pedir_info_resets_flow(self):
        municipio_context_state = {}
        payload = {
            "pregunta": "quiero hacer un reclamo",
            "llamar_gemini_mock_return": {
                "respuesta_usuario": "Necesito un dato extraño",
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {"target": "municipio"},
                "pedir_info": "dato_inexistente",
                "botones": []
            }
        }
        response, municipio_context_state, _ = self._call_responder_municipio(payload, municipio_context_state)
        self.assertIsNone(municipio_context_state.get("estado_conversacion"))
        self.assertIn("perd\xc3\xb3n, tuve un problema", response.get("message_body", "").lower())
