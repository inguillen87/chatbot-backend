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
    def __init__(self, *a, **k):
        pass
    class messages:
        @staticmethod
        def create(*a, **k):
            return SimpleNamespace(sid='dummy')
twilio_rest_stub.Client = _DummyClient
sys.modules.setdefault('twilio.rest', twilio_rest_stub)
sys.modules.setdefault('twilio', ModuleType('twilio'))
twilio_validator_stub = ModuleType('twilio.request_validator')
twilio_validator_stub.RequestValidator = MagicMock()
sys.modules.setdefault('twilio.request_validator', twilio_validator_stub)
sys.modules.setdefault('cohere', ModuleType('cohere'))
# sqlalchemy_stub = ModuleType('sqlalchemy') # Removed stubbing of entire sqlalchemy module
# sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
# class _SAError(Exception):
#     pass
# sqlalchemy_exc_stub.SQLAlchemyError = _SAError
# sqlalchemy_stub.exc = sqlalchemy_exc_stub
# sys.modules.setdefault('sqlalchemy', sqlalchemy_stub) # Removed stubbing
# sys.modules.setdefault('sqlalchemy.exc', sqlalchemy_exc_stub) # Removed stubbing


from services.municipio_responder import (
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    ConversationState,
    ReclamoState,
)

class DummyTicket:
    def __init__(self, id=1, nro_ticket=123456):
        self.id = id
        self.nro_ticket = nro_ticket

class DummyUser(SimpleNamespace):
    pass

@patch('services.municipio_responder.flag_modified', MagicMock())
class MunicipioLogicTests(unittest.TestCase):
    def setUp(self):
        import eventlet
        eventlet.monkey_patch = lambda *a, **k: None
        from app import create_app
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.owner_user = DummyUser()
        self.owner_user.id = 1
        self.owner_user.rubro = SimpleNamespace(nombre='municipio')
        self.owner_user.plan = 'full'
        self.owner_user.preguntas_usadas = 0
        self.owner_user.limite_preguntas = 100
        self.owner_user.municipio_id = 'test_muni_id'
        self.owner_user.nombre_empresa = 'Municipio Test Name'

        self.viewer_user = DummyUser()
        self.viewer_user.id = 200
        self.viewer_user.nombre = "Vecino Molesto"
        self.viewer_user.telefono = "2615550000"
        self.viewer_user.email = "vecino@example.com"
        self.viewer_user.direccion = "Av. Siempre Viva 742"
        self.viewer_user.prefers_audio = False

    def tearDown(self):
        self.app_context.pop()

    @patch('services.actions.municipio_actions.DerivarHumanoActionHandler.execute')
    @patch('services.municipio_responder.llamar_gemini')
    def test_human_escalation(self, mock_llamar_llm, mock_handoff_execute):
        mock_llamar_llm.return_value = (
            {
                "message_body": "Te estoy derivando con un agente.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {},
                "pedir_info": None,
                "botones": []
            },
            {}
        )
        mock_handoff_execute.return_value = {
            "success": True,
            "message_to_user": "Chat en vivo creado.",
            "data": {
                "ticket_id": 1,
                "chat_id": "M-123456",
                "status": "esperando_agente_en_vivo",
                "socket_room": "municipio_test_muni_id",
            },
        }

        from models import ChatSessionContext, db
        chat_context = ChatSessionContext(chat_session_id="test_session_escalation", context_data={})
        db.session.add(chat_context)
        db.session.commit()

        resp = responder_municipio(
            pregunta_original='Hablar con un agente',
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            viewer_user=self.viewer_user,
            chat_db_context=chat_context
        )

        self.assertIn("Chat en vivo creado", resp["message_body"])
        self.assertEqual(resp["data"]["ticket_id"], 1)
        mock_handoff_execute.assert_called_once()

    @patch('services.municipio_responder.ReclamoFlowHandler.start_flow')
    def test_auto_flow_triggered_by_image(self, mock_start_flow):
        mock_start_flow.return_value = {"message_body": "flujoiniciado"}

        from models import ChatSessionContext, db
        chat_context = ChatSessionContext(chat_session_id="test_session_auto", context_data={})
        db.session.add(chat_context)
        db.session.commit()

        datos_interpretados = {
            "es_reclamo": True,
            "categoria_sugerida": "luminaria",
            "descripcion_sugerida": "farola rota",
        }

        resp = responder_municipio(
            pregunta_original='',
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            viewer_user=self.viewer_user,
            chat_db_context=chat_context,
            datos_interpretados_archivo=datos_interpretados,
        )

        mock_start_flow.assert_called_once()
        self.assertEqual(resp["message_body"], "flujoiniciado")

    @patch('services.municipio_responder.ReclamoFlowHandler.start_flow')
    def test_auto_category_from_text(self, mock_start_flow):
        mock_start_flow.return_value = {"message_body": "flujoiniciado"}

        from models import ChatSessionContext, db
        chat_context = ChatSessionContext(
            chat_session_id="test_session_text_category",
            context_data={
                CONTEXTO_MUNICIPIO: {"estado_conversacion": ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name}
            },
        )
        db.session.add(chat_context)
        db.session.commit()

        resp = responder_municipio(
            pregunta_original="Necesito iniciar reclamo por luminaria, hay un poste caido",
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            viewer_user=self.viewer_user,
            chat_db_context=chat_context,
        )

        mock_start_flow.assert_called_once()
        kwargs = mock_start_flow.call_args.kwargs
        self.assertEqual(kwargs.get("categoria_inicial"), "Luminaria")
        self.assertEqual(resp["message_body"], "flujoiniciado")

    @patch('services.municipio_responder.ReclamoFlowHandler.start_flow')
    def test_auto_description_and_address(self, mock_start_flow):
        mock_start_flow.return_value = {"message_body": "flujoiniciado"}

        from models import ChatSessionContext, db
        chat_context = ChatSessionContext(
            chat_session_id="test_session_desc", context_data={}
        )
        db.session.add(chat_context)
        db.session.commit()

        message = "Quiero hacer un reclamo de luminaria, hay un poste caído en calle Sarmiento 125"

        resp = responder_municipio(
            pregunta_original=message,
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            viewer_user=self.viewer_user,
            chat_db_context=chat_context,
        )

        mock_start_flow.assert_called_once()
        kwargs = mock_start_flow.call_args.kwargs
        datos = kwargs.get("datos_iniciales")
        self.assertEqual(kwargs.get("categoria_inicial"), "Luminaria")
        self.assertEqual(datos.get("descripcion"), "hay un poste caído en calle Sarmiento 125")
        self.assertEqual(datos.get("direccion"), "calle Sarmiento 125")
        self.assertEqual(resp["message_body"], "flujoiniciado")

    @patch('services.municipio_responder.ReclamoFlowHandler.handle')
    @patch('services.municipio_responder.ReclamoFlowHandler.start_flow')
    def test_photo_in_active_flow_keeps_context(self, mock_start_flow, mock_handle):
        mock_handle.return_value = {"message_body": "ok"}

        from models import ChatSessionContext, db
        chat_context = ChatSessionContext(
            chat_session_id="test_session_photo",
            context_data={
                CONTEXTO_MUNICIPIO: {
                    "reclamo_flow_v2": {
                        "state": ReclamoState.ESPERANDO_FOTO.name,
                        "datos_reclamo": {"categoria": "Luminaria"},
                    }
                }
            },
        )
        db.session.add(chat_context)
        db.session.commit()

        datos_interpretados = {
            "es_reclamo": True,
            "categoria_sugerida": "Luminaria",
            "descripcion_sugerida": "poste caido",
        }

        resp = responder_municipio(
            pregunta_original="",
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            viewer_user=self.viewer_user,
            chat_db_context=chat_context,
            datos_interpretados_archivo=datos_interpretados,
            es_foto=True,
            foto_url="http://example.com/foto.jpg",
        )

        mock_start_flow.assert_not_called()
        mock_handle.assert_called_once()
        flow_data = chat_context.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]["datos_reclamo"]
        self.assertEqual(flow_data.get("foto_url"), "http://example.com/foto.jpg")
        self.assertEqual(resp["message_body"], "ok")


if __name__ == '__main__':
    unittest.main()
