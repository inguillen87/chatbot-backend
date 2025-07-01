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
models_stub.Conversacion = _DummyModel
models_stub.MunicipioTicket = _DummyModel
models_stub.TicketSatisfaccion = _DummyModel
models_stub.PymePedido = _DummyModel
models_stub.Rubro = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.User = _DummyModel
class _DummyQuery(list):
    def filter_by(self, **kwargs):
        return self
    def all(self):
        return []
    def first(self):
        return self[0] if self else None

class DummyPymeTicket(_DummyModel):
    query = _DummyQuery()

class DummyTicketComentario(_DummyModel):
    fecha = SimpleNamespace(desc=lambda: 'fecha')
    query = _DummyQuery()

models_stub.PymeTicket = DummyPymeTicket
models_stub.TicketComentario = DummyTicketComentario

class DummyCatalogoItem:
    query = _DummyQuery()

class DummyArchivoAdjunto:
    query = _DummyQuery()

models_stub.CatalogoItem = DummyCatalogoItem
models_stub.ArchivoAdjunto = DummyArchivoAdjunto
models_stub.CatalogoEmbedding = _DummyModel
models_stub.QA = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub
sys.modules.pop('services.catalogo_local', None)

flask_stub = ModuleType('flask')
flask_stub.session = {}
sys.modules['flask'] = flask_stub

sys.modules['cohere'] = ModuleType('cohere')
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
class _SAError(Exception):
    pass
sqlalchemy_exc_stub.SQLAlchemyError = _SAError
sqlalchemy_stub.exc = sqlalchemy_exc_stub
sys.modules['sqlalchemy'] = sqlalchemy_stub
sys.modules['sqlalchemy.exc'] = sqlalchemy_exc_stub
sys.modules['requests'] = ModuleType('requests')
pandas_stub = ModuleType('pandas')
class _DummyDF: pass
class _DummySeries: pass
pandas_stub.DataFrame = _DummyDF
pandas_stub.Series = _DummySeries
sys.modules['pandas'] = pandas_stub
sys.modules['spacy'] = ModuleType('spacy')
bs4_stub = ModuleType('bs4')
bs4_stub.BeautifulSoup = object
sys.modules['bs4'] = bs4_stub
qdrant_stub = ModuleType('qdrant_client')
qdrant_http_stub = ModuleType('qdrant_client.http')
qdrant_http_models_stub = ModuleType('qdrant_client.http.models')
qdrant_stub.QdrantClient = type('QdrantClient', (), {})
qdrant_stub.models = ModuleType('qdrant_client.models')
qdrant_stub.http = qdrant_http_stub
qdrant_http_stub.models = qdrant_http_models_stub
qdrant_http_models_stub.ScoredPoint = object
sys.modules['qdrant_client'] = qdrant_stub
sys.modules['qdrant_client.http'] = qdrant_http_stub
sys.modules['qdrant_client.http.models'] = qdrant_http_models_stub
twilio_rest_stub = ModuleType('twilio.rest')
class _DummyClient:
    class messages:
        @staticmethod
        def create(*a, **k):
            return SimpleNamespace(sid='dummy')
twilio_rest_stub.Client = _DummyClient
sys.modules.setdefault('twilio.rest', twilio_rest_stub)
sys.modules.setdefault('twilio', ModuleType('twilio'))

from services import pymes

class DummyTicket:
    def __init__(self, id=1, nro_ticket=654321, asunto=None, estado='nuevo'):
        self.id = id
        self.nro_ticket = nro_ticket
        self.asunto = asunto
        self.estado = estado

class DummyUser(SimpleNamespace):
    def __init__(self):
        super().__init__(
            id=1,
            rubro=SimpleNamespace(nombre='vinoteca'),
            nombre_empresa='Bodega Ejemplo',
            telefono='123456',
            direccion='Calle Falsa 123',
            email='a@b.com',
            plan='full',
            preguntas_usadas=0,
            limite_preguntas=100,
            link_web='https://empresa.test',
        )

class PymeLogicTests(unittest.TestCase):
    @patch('services.pymes.robust_chat', return_value='PREGUNTA_NUEVA')
    def test_es_pregunta_nueva_llm(self, mock_llm):
        self.assertTrue(pymes.es_pregunta_nueva('hola', 'el dato solicitado'))
        mock_llm.return_value = 'RESPUESTA_VALIDA'
        self.assertFalse(pymes.es_pregunta_nueva('hola', 'el dato solicitado'))

    @patch('services.catalogo_local.buscar_catalogo_local', return_value=[])
    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme(self, mock_clf, mock_servicio, mock_buscar):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        resp = pymes.responder_pyme('Necesito hablar con un agente', user, None, viewer_user=user)
        self.assertIn('sala de chat', resp['respuesta'])

    @patch('services.catalogo_local.buscar_catalogo_local', return_value=[])
    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme_anonymous_requires_login(self, mock_clf, mock_servicio, mock_buscar):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        resp = pymes.responder_pyme('Necesito hablar con un agente', DummyUser(), None, viewer_user=None)
        self.assertIn('iniciar sesión', resp['respuesta'])

    @patch('services.pymes.detectar_small_talk_con_llm', return_value=False)
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='saludo')
    def test_greeting_with_typo(self, mock_clf, mock_small):
        user = DummyUser()
        resp = pymes.responder_pyme('holaa buenos noxes', user, None, viewer_user=user)
        self.assertIn('¿En qué puedo ayudarte hoy', resp['respuesta'])

    def test_small_talk(self):
        user = DummyUser()
        with patch('services.logic.get_cohere_response', side_effect=['SI', '¡Hola! ¿Cómo estás?']) as mock_llm:
            resp = pymes.responder_pyme('¿Cómo andas?', user, None, viewer_user=user)
            self.assertEqual(resp['fuente'], 'smalltalk_pyme_llm')
            self.assertIn('Hola', resp['respuesta'])
            self.assertEqual(mock_llm.call_count, 2)

    @patch('services.pymes.robust_chat', return_value='negativo')
    def test_sentiment_handler(self, mock_llm):
        user = DummyUser()
        resp = pymes.responder_pyme('este servicio es horrible', user, None, viewer_user=user)
        self.assertEqual(resp['fuente'], 'sentimiento_negativo')
        self.assertIn('agente', resp['respuesta'].lower())

    @patch('services.pymes.robust_chat', return_value='positivo')
    def test_sentiment_handler_positive(self, mock_llm):
        user = DummyUser()
        resp = pymes.responder_pyme('excelente servicio muchas gracias', user, None, viewer_user=user)
        self.assertEqual(resp['fuente'], 'sentimiento_positivo')
        self.assertIn('gracias', resp['respuesta'].lower())

    def test_cancel_keywords(self):
        contexto = {
            'contexto_pyme': {
                'estado_conversacion': pymes.serialize_state(pymes.PymeConversationState.ESPERANDO_PRODUCTO),
                'productos_solicitados_temp': [{'nombre': 'vino', 'cantidad': 1}],
                'monto_total_temp': 100
            },
            'user_id': 1,
            'rubro_obj': SimpleNamespace(nombre='vinoteca'),
            'user_obj': DummyUser(),
            'rubro_nombre': 'vinoteca'
        }
        handler = pymes.PedidoHandler(contexto)
        resp = handler.handle('cancelalo por favor')
        self.assertEqual(resp['fuente'], 'pedido_cancelado')

    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='iniciar_pedido')
    def test_iniciar_pedido_flujo(self, mock_clf):
        user = DummyUser()
        resp = pymes.responder_pyme('quiero comprar', user, None, viewer_user=user)
        self.assertEqual(resp['estado_respuesta'], 'pyme_pregunta_pedido')
        self.assertIn('pedido', resp['respuesta'].lower())

    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='continuar_flujo')
    def test_continuar_flujo_usar_pedido_handler(self, mock_clf):
        user = DummyUser()
        resp = pymes.responder_pyme('agregar mas productos', user, None, viewer_user=user)
        self.assertEqual(resp['estado_respuesta'], 'pyme_pregunta_pedido')
        self.assertIn('producto', resp['respuesta'].lower())

    @patch('services.pymes.sugerencias_por_rubro', return_value=[])
    @patch('services.pymes.buscar_en_faq_spacy', return_value=None)
    @patch('services.pymes.obtener_info_web', return_value={'envios': 'en el dia'})
    def test_contextual_llm_handler(self, mock_web, mock_faq, mock_sug):
        user = DummyUser()
        resp = pymes.responder_pyme('costo de envio?', user, None, viewer_user=user)
        self.assertEqual(resp['fuente'], 'llm_contextual_pyme')
        self.assertIn('en el dia', resp['respuesta'])

    @patch('services.pymes.armar_respuesta_legible', return_value='Malbec $1000')
    @patch('services.pymes.buscar_catalogo_qdrant', return_value=['vino'])
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='ver_catalogo')
    def test_admin_viewer_bypasses_login_for_catalogo(self, mock_clf, mock_buscar, mock_format):
        """El catálogo debe mostrarse al empleado sin pedir login extra."""
        owner = DummyUser()
        admin = DummyUser()
        admin.id = 2
        admin.empresa_id = owner.id
        admin.nombre_empresa = owner.nombre_empresa

        resp = pymes.responder_pyme('ver catalogo', None, owner.rubro, viewer_user=admin)
        self.assertNotIn('iniciá sesión', resp['respuesta'].lower())

    def test_ticket_status_handler(self):
        ctx = {
            'anon_id': 'anon',
            pymes.CONTEXTO_PYME: {},
            'intencion': 'consultar_estado_ticket'
        }
        DummyPymeTicket.query = type('Q', (), {
            'filter_by': lambda self, **kw: self,
            'first': lambda self: DummyTicket(nro_ticket=12345, id=1, asunto='A', estado='en_proceso')
        })()
        DummyTicketComentario.query = type('Q', (), {
            'filter_by': lambda self, **kw: self,
            'order_by': lambda self, *a, **k: self,
            'first': lambda self: None
        })()
        pymes.PymeTicket = DummyPymeTicket
        pymes.TicketComentario = DummyTicketComentario
        handler = pymes.TicketStatusHandler(ctx)
        resp = handler.handle('estado ticket 12345')
        self.assertIn('P-12345', resp['respuesta'])

# --- Integration Tests for Pyme Pedido Flow with LLM Contact Extraction ---
from services.pymes import PymeConversationState, CONTEXTO_PYME
from unittest.mock import MagicMock

class PymePedidoFlowTests(unittest.TestCase):
    def setUp(self):
        self.owner_user = DummyUser()
        self.viewer_user = DummyUser()
        self.viewer_user.id = 100
        self.viewer_user.name = "Cliente Registrado"
        self.viewer_user.telefono = "999888777" # Assume this is a valid format for storage
        self.viewer_user.email = "cliente@example.com"

        # Reset flask session for each test
        # flask_stub is defined globally in this file
        flask_stub.session = {}

    def _call_responder_pyme(self, pregunta, current_pyme_context_state_dict):
        # Helper to simulate calling responder_pyme and updating context
        # The actual flask.session is mocked via flask_stub at the top of the file
        flask_stub.session[CONTEXTO_PYME] = current_pyme_context_state_dict

        user_query_mock = MagicMock()
        # Ensure User.query.get returns the correct user for pre-fill, or None
        user_query_mock.get.side_effect = lambda user_id: self.viewer_user if user_id == self.viewer_user.id else None

        # Mock the database session for PymePedido creation
        db_session_mock = MagicMock()
        db_session_mock.add = MagicMock()
        db_session_mock.commit = MagicMock()
        db_session_mock.flush = MagicMock()
        db_session_mock.rollback = MagicMock()

        # Ensure that when services.pymes.db.session is accessed, it returns our mock
        # This is tricky because 'db' is imported from 'models' which is already stubbed.
        # We need to ensure models_stub.db.session is our mock.
        original_db_session = models_stub.db.session
        models_stub.db.session = db_session_mock

        with patch('services.pymes.User.query', user_query_mock):
            # We also need to ensure that robust_chat used by es_producto_valido_llm is handled
            # if it's called during the PedidoHandler's item addition phase (which we are skipping here mostly)
            with patch('services.pymes.es_producto_valido_llm', MagicMock(return_value=True)):
                with patch('services.pymes.extraer_productos_llm', MagicMock(return_value=[])): # simplify product extraction
                    response = pymes.responder_pyme(
                        pregunta,
                        owner_user=self.owner_user,
                        rubro_obj=self.owner_user.rubro, # Make sure rubro_obj is passed
                        viewer_user=self.viewer_user,
                        chat_session_uuid="test-session-uuid"
                    )

        models_stub.db.session = original_db_session # Restore original mock

        if response and response.get("contexto_actualizado", {}).get(CONTEXTO_PYME):
            return response, response["contexto_actualizado"][CONTEXTO_PYME], db_session_mock
        return response, current_pyme_context_state_dict, db_session_mock

    @patch('services.llm_utils.robust_chat') # Patching where robust_chat is defined for llm_utils
    def test_full_contact_details_provided_at_once_by_llm(self, mock_llm_robust_chat):
        pyme_context_state = {}

        # Simulate adding an item to cart and being in CONFIRMANDO_PEDIDO state
        pyme_context_state = {
            "carrito": [{"nombre": "Vino Tinto", "cantidad_pedido": 1, "precio_unitario_catalogo": 150.00}],
            "estado_conversacion": pymes.serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO)
        }

        # User confirms order ("si"), bot should ask for name
        response, pyme_context_state, _ = self._call_responder_pyme("si", pyme_context_state)
        self.assertIn("nombre completo", response["respuesta"].lower())
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE))

        # User provides all details in one go when asked for name
        user_full_details_input = "Soy Pedro Completo, mi teléfono es 1133445566, la dirección es Av. Siempreviva 742, y mi email es pedro@example.com"

        # Mock the response from extract_multiple_contact_details_llm (which uses robust_chat)
        llm_response_data = {
            "nombre_cliente": "Pedro Completo",
            "telefono_cliente": "1133445566",
            "direccion_cliente": "Av. Siempreviva 742",
            "email_cliente": "pedro@example.com"
        }
        mock_llm_robust_chat.return_value = json.dumps(llm_response_data)

        response, pyme_context_state, db_mock = self._call_responder_pyme(user_full_details_input, pyme_context_state)

        # Check if robust_chat was called (it's used by extract_multiple_contact_details_llm)
        mock_llm_robust_chat.assert_called()

        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS))
        self.assertIn("revisemos todo antes de finalizar", response["respuesta"].lower())
        self.assertIn("pedro completo", response["respuesta"].lower())
        self.assertIn("1133445566", response["respuesta"])
        self.assertIn("av. siempreviva 742", response["respuesta"].lower())
        self.assertIn("pedro@example.com", response["respuesta"].lower())

        # Final confirmation
        response, pyme_context_state, db_mock = self._call_responder_pyme("si, todo correcto", pyme_context_state)
        self.assertIn("pedido fue registrado", response["respuesta"].lower())
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_FEEDBACK))
        db_mock.add.assert_called()
        db_mock.commit.assert_called()

    @patch('services.llm_utils.robust_chat')
    def test_partial_details_by_llm_then_single_prompts(self, mock_llm_robust_chat):
        pyme_context_state = {
            "carrito": [{"nombre": "Vino Blanco", "cantidad_pedido": 2, "precio_unitario_catalogo": 120.00}],
            "estado_conversacion": pymes.serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO)
        }
        response, pyme_context_state, _ = self._call_responder_pyme("si", pyme_context_state) # Confirm order
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE))

        user_name_phone_input = "Soy Ana Parcial, mi teléfono es 2244668800"
        mock_llm_robust_chat.return_value = json.dumps({
            "nombre_cliente": "Ana Parcial", "telefono_cliente": "2244668800"
        })
        response, pyme_context_state, _ = self._call_responder_pyme(user_name_phone_input, pyme_context_state)

        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION))
        self.assertIn("dirección de entrega", response["respuesta"].lower())
        self.assertEqual(pyme_context_state.get("nombre_cliente"), "Ana Parcial")
        # Assuming validar_telefono returns the number if valid, or formats it.
        self.assertEqual(pyme_context_state.get("telefono_cliente"), "2244668800")

        user_address_input = "Es en Calle Ejemplo 456, Ciudad Test. Mi email es ana.p@ejemplo.net"
        mock_llm_robust_chat.return_value = json.dumps({
            "direccion_cliente": "Calle Ejemplo 456, Ciudad Test", "email_cliente": "ana.p@ejemplo.net"
        })
        response, pyme_context_state, db_mock = self._call_responder_pyme(user_address_input, pyme_context_state)

        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS))
        self.assertIn("ana parcial", response["respuesta"].lower())
        self.assertIn("2244668800", response["respuesta"])
        self.assertIn("calle ejemplo 456", response["respuesta"].lower())
        self.assertIn("ana.p@ejemplo.net", response["respuesta"].lower())

        response, pyme_context_state, db_mock = self._call_responder_pyme("si", pyme_context_state)
        self.assertIn("pedido fue registrado", response["respuesta"].lower())
        db_mock.add.assert_called()
        db_mock.commit.assert_called()

    @patch('services.llm_utils.robust_chat')
    def test_profile_prefill_and_llm_override(self, mock_llm_robust_chat):
        pyme_context_state = {
            "carrito": [{"nombre": "Producto X", "cantidad_pedido": 1, "precio_unitario_catalogo": 50.00}],
            "estado_conversacion": pymes.serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO)
        }

        response, pyme_context_state, _ = self._call_responder_pyme("si", pyme_context_state) # Confirm order
        # Profile: name, phone, email are pre-filled. Bot asks for address.
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION))
        self.assertIn("dirección de entrega", response["respuesta"].lower())
        self.assertEqual(pyme_context_state.get("nombre_cliente"), self.viewer_user.name)
        self.assertEqual(pyme_context_state.get("telefono_cliente"), self.viewer_user.telefono)
        self.assertEqual(pyme_context_state.get("email_cliente"), self.viewer_user.email)

        user_address_new_phone_input = "Entregar en Av. Libertad 987. Ah, y mi nuevo teléfono es 555123123."
        mock_llm_robust_chat.return_value = json.dumps({
            "direccion_cliente": "Av. Libertad 987", "telefono_cliente": "555123123"
        })
        response, pyme_context_state, db_mock = self._call_responder_pyme(user_address_new_phone_input, pyme_context_state)

        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS))
        self.assertIn(self.viewer_user.name.lower(), response["respuesta"].lower())
        self.assertIn("555123123", response["respuesta"]) # New phone
        self.assertIn("av. libertad 987", response["respuesta"].lower())
        self.assertIn(self.viewer_user.email.lower(), response["respuesta"].lower())

        response, pyme_context_state, db_mock = self._call_responder_pyme("todo ok", pyme_context_state)
        self.assertIn("pedido fue registrado", response["respuesta"].lower())
        db_mock.add.assert_called()
        db_mock.commit.assert_called()

        # Verify PymePedido data
        # Access the first argument of the first call to db_mock.add
        if db_mock.add.call_args_list:
            called_with_pedido = db_mock.add.call_args_list[0][0][0]
            self.assertEqual(called_with_pedido.telefono_cliente, "555123123")
            self.assertEqual(called_with_pedido.nombre_cliente, self.viewer_user.name)
            self.assertEqual(called_with_pedido.direccion, "Av. Libertad 987")
            self.assertEqual(called_with_pedido.email_cliente, self.viewer_user.email)
        else:
            self.fail("db.session.add was not called with PymePedido object")

    @patch('services.llm_utils.robust_chat')
    def test_traditional_single_field_input_flow(self, mock_llm_robust_chat):
        # Test the flow when user provides one piece of info at a time, and LLM doesn't extract extras.
        pyme_context_state = {
            "carrito": [{"nombre": "Producto Tradicional", "cantidad_pedido": 1, "precio_unitario_catalogo": 75.00}],
            "estado_conversacion": pymes.serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO)
        }

        # 1. Confirm order -> Ask for Name
        response, pyme_context_state, _ = self._call_responder_pyme("si", pyme_context_state)
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE))

        # 2. Provide Name -> Ask for Phone
        mock_llm_robust_chat.return_value = json.dumps({}) # LLM extracts nothing extra
        response, pyme_context_state, _ = self._call_responder_pyme("Juan Solo", pyme_context_state)
        self.assertEqual(pyme_context_state.get("nombre_cliente"), "Juan Solo")
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO))
        self.assertIn("número de teléfono", response["respuesta"].lower())

        # 3. Provide Phone -> Ask for Address
        mock_llm_robust_chat.return_value = json.dumps({})
        response, pyme_context_state, _ = self._call_responder_pyme("1234567890", pyme_context_state)
        self.assertEqual(pyme_context_state.get("telefono_cliente"), "1234567890")
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION))
        self.assertIn("dirección de entrega", response["respuesta"].lower())

        # 4. Provide Address -> Ask for Final Confirmation (assuming email is optional or prefilled and valid)
        # For this test, let's assume email is NOT prefilled and not provided, making it truly optional.
        self.viewer_user.email = None # Temporarily remove email from profile for this test path
        mock_llm_robust_chat.return_value = json.dumps({})
        response, pyme_context_state, db_mock = self._call_responder_pyme("Calle Unica 321", pyme_context_state)
        self.assertEqual(pyme_context_state.get("direccion_cliente"), "Calle Unica 321")
        self.assertEqual(pyme_context_state.get("estado_conversacion"), pymes.serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS))
        self.assertIn("revisemos todo antes de finalizar", response["respuesta"].lower())
        self.assertNotIn("email:", response["respuesta"].lower()) # Email should not be in the summary if not provided/prefilled

        # 5. Final Confirmation -> Order registered
        response, pyme_context_state, db_mock = self._call_responder_pyme("si", pyme_context_state)
        self.assertIn("pedido fue registrado", response["respuesta"].lower())
        db_mock.add.assert_called()
        db_mock.commit.assert_called()
        self.viewer_user.email = "cliente@example.com" # Restore email for other tests


if __name__ == '__main__':
    unittest.main()
