import unittest
from unittest.mock import MagicMock, patch, ANY

# Assuming BaseActionHandler and CrearReclamoAction are importable
from services.actions.base_action_handler import BaseActionHandler
from services.actions.municipio_claim_actions import CrearReclamoAction, ConsultarEstadoReclamoAction

# Mock for servicio_tickets and other dependencies
mock_servicio_tickets_module = MagicMock()
mock_db_session_module = MagicMock()
mock_archivo_service_module = MagicMock()

# Mock User object for context
class MockUser:
    def __init__(self, id, municipio_id=None, name=None, telefono=None, email=None):
        self.id = id
        self.municipio_id = municipio_id
        self.name = name
        self.telefono = telefono
        self.email = email

class MockTicket:
    def __init__(self, id, nro_ticket, asunto="Test Asunto", estado="nuevo", categoria="General", fecha=None, detalles=""):
        from datetime import datetime
        self.id = id
        self.nro_ticket = nro_ticket
        self.asunto = asunto
        self.estado = estado
        self.categoria = categoria
        self.fecha = fecha or datetime.utcnow()
        self.detalles = detalles
        self.user_id = None # For permission checks if needed
        self.anon_id = None # For permission checks if needed


class MockTicketComentario:
    def __init__(self, comentario, fecha, es_admin=True):
        from datetime import datetime
        self.comentario = comentario
        self.fecha = fecha or datetime.utcnow()
        self.es_admin = es_admin


# Patching at the module level where these are looked up by the action handlers
@patch('services.actions.municipio_claim_actions.servicio_tickets', mock_servicio_tickets_module)
@patch('services.actions.municipio_claim_actions.global_db.session', mock_db_session_module)
class TestCrearReclamoAction(unittest.TestCase):

    def setUp(self):
        mock_servicio_tickets_module.reset_mock()
        mock_db_session_module.reset_mock()

        self.municipio_bot_owner = MockUser(id=1, municipio_id=100)
        self.chat_user_registered = MockUser(id=50, name="Test Vecino Registrado", telefono="+5491112345678", email="vecino@test.com")
        self.chat_user_anon_context = {"anon_id": "anon_test_123"}


        self.base_context_registered_user = {
            "user_obj": self.municipio_bot_owner,
            "cliente_id": self.chat_user_registered.id,
            "nombre_usuario_contexto": self.chat_user_registered.name,
            "telefono_usuario_contexto": self.chat_user_registered.telefono,
            "email_usuario_contexto": self.chat_user_registered.email,
            "channel": "web",
            "anon_id": None,
            "archivo_id_para_asociar": None,
            "foto_url": None
        }
        self.base_context_anon_user = {
            "user_obj": self.municipio_bot_owner,
            "cliente_id": None,
            "anon_id": self.chat_user_anon_context["anon_id"],
            "nombre_usuario_contexto": None, # Anon user might not have name in context yet
            "telefono_usuario_contexto": None,
            "email_usuario_contexto": None,
            "channel": "whatsapp",
            "archivo_id_para_asociar": None,
            "foto_url": None
        }

    def test_crear_reclamo_success_registered_user(self):
        action_data = {
            "categoria": "Alumbrado",
            "descripcion": "Luz quemada en poste",
            "ubicacion_original_reclamo": "Calle Falsa 123, Centro",
            # nombre_vecino, telefono, email will be taken from context if not in action_data
        }
        mock_ticket_creado = MockTicket(id=1, nro_ticket=555123)
        mock_servicio_tickets_module.crear_nuevo_ticket.return_value = mock_ticket_creado

        handler = CrearReclamoAction(context=self.base_context_registered_user)
        result = handler.execute(action_data)

        self.assertTrue(result["success"])
        self.assertIn("M-555123", result["message_to_user"])
        self.assertEqual(result["data"]["ticket_id"], 1)

        mock_servicio_tickets_module.crear_nuevo_ticket.assert_called_once()
        call_args = mock_servicio_tickets_module.crear_nuevo_ticket.call_args[1]['ticket_data'] # Get the dict

        self.assertEqual(call_args['categoria'], "Alumbrado")
        self.assertEqual(call_args['nombre_vecino'], self.chat_user_registered.name)
        self.assertEqual(call_args['email_vecino'], self.chat_user_registered.email)
        self.assertEqual(call_args['user_id'], self.chat_user_registered.id)
        self.assertIsNone(call_args.get('anon_id'))

    def test_crear_reclamo_success_anon_user_with_llm_data(self):
        action_data = {
            "categoria": "Bache",
            "descripcion": "Pozo peligroso",
            "ubicacion_original_reclamo": "San Martin 234",
            "nombre_vecino": "Vecino Anon LLM", # LLM provided this
            "telefono_vecino": "+5492619998888", # LLM provided this
        }
        mock_ticket_creado = MockTicket(id=2, nro_ticket=555124)
        mock_servicio_tickets_module.crear_nuevo_ticket.return_value = mock_ticket_creado

        handler = CrearReclamoAction(context=self.base_context_anon_user)
        result = handler.execute(action_data)

        self.assertTrue(result["success"])
        self.assertIn("M-555124", result["message_to_user"])

        mock_servicio_tickets_module.crear_nuevo_ticket.assert_called_once()
        call_args = mock_servicio_tickets_module.crear_nuevo_ticket.call_args[1]['ticket_data']

        self.assertEqual(call_args['nombre_vecino'], "Vecino Anon LLM")
        self.assertEqual(call_args['telefono_vecino'], "+5492619998888")
        self.assertIsNone(call_args.get('user_id'))
        self.assertEqual(call_args['anon_id'], self.chat_user_anon_context["anon_id"])


    def test_crear_reclamo_missing_essential_data(self):
        action_data = {"categoria": "Agua"} # Missing description and ubicacion_original_reclamo
        handler = CrearReclamoAction(context=self.base_context_registered_user)
        result = handler.execute(action_data)

        self.assertFalse(result["success"])
        self.assertIn("Faltan datos esenciales", result["message_to_user"])
        mock_servicio_tickets_module.crear_nuevo_ticket.assert_not_called()

    @patch('services.actions.municipio_claim_actions.archivo_service', mock_archivo_service_module)
    def test_crear_reclamo_with_attachment_id_association(self):
        mock_archivo_service_module.reset_mock() # Ensure clean mock for this test
        context_with_attachment = {**self.base_context_registered_user, "archivo_id_para_asociar": 777}
        action_data = {
            "categoria": "Arbolado", "descripcion": "Rama peligrosa",
            "ubicacion_original_reclamo": "Plaza Principal"
        }
        mock_ticket_creado = MockTicket(id=3, nro_ticket=555125)
        mock_servicio_tickets_module.crear_nuevo_ticket.return_value = mock_ticket_creado
        mock_archivo_service_module.asociar_archivos_a_ticket.return_value = True

        handler = CrearReclamoAction(context=context_with_attachment)
        result = handler.execute(action_data)

        self.assertTrue(result["success"])
        mock_servicio_tickets_module.crear_nuevo_ticket.assert_called_once()
        mock_archivo_service_module.asociar_archivos_a_ticket.assert_called_once_with(
            ticket_id=3, tipo_ticket="municipio", ids_archivos=[777]
        )
        # Check if context was modified (archivo_id_para_asociar removed)
        self.assertNotIn("archivo_id_para_asociar", context_with_attachment)


    def test_crear_reclamo_with_foto_url_directa_from_context(self):
        context_with_foto_url = {**self.base_context_anon_user, "foto_url": "http://example.com/image.jpg"}
        action_data = {
            "categoria": "Basura", "descripcion": "Contenedor desbordado",
            "ubicacion_original_reclamo": "Esquina Test 123",
        }
        mock_ticket_creado = MockTicket(id=4, nro_ticket=555126)
        mock_servicio_tickets_module.crear_nuevo_ticket.return_value = mock_ticket_creado

        handler = CrearReclamoAction(context=context_with_foto_url)
        result = handler.execute(action_data)

        self.assertTrue(result["success"])
        mock_servicio_tickets_module.crear_nuevo_ticket.assert_called_once()
        call_args = mock_servicio_tickets_module.crear_nuevo_ticket.call_args[1]['ticket_data']
        self.assertEqual(call_args['foto_url_directa'], "http://example.com/image.jpg")

    def test_crear_reclamo_with_foto_url_from_llm_action_data(self):
        action_data = {
            "categoria": "Vandalismo", "descripcion": "Grafiti en pared",
            "ubicacion_original_reclamo": "Parque Central",
            "foto_url_adjunta": "http://llm.example.com/grafiti.png" # LLM provides this
        }
        mock_ticket_creado = MockTicket(id=5, nro_ticket=555127)
        mock_servicio_tickets_module.crear_nuevo_ticket.return_value = mock_ticket_creado

        handler = CrearReclamoAction(context=self.base_context_registered_user) # Context has no foto_url
        result = handler.execute(action_data)

        self.assertTrue(result["success"])
        mock_servicio_tickets_module.crear_nuevo_ticket.assert_called_once()
        call_args = mock_servicio_tickets_module.crear_nuevo_ticket.call_args[1]['ticket_data']
        self.assertEqual(call_args['foto_url_directa'], "http://llm.example.com/grafiti.png")

# Test ConsultarEstadoReclamoAction
# We need to patch MunicipioTicket and TicketComentario queries
@patch('services.actions.municipio_claim_actions.MunicipioTicket')
@patch('services.actions.municipio_claim_actions.TicketComentario')
class TestConsultarEstadoReclamoAction(unittest.TestCase):
    def setUp(self):
        self.base_context = {"channel": "web"} # Minimal context for this action

    def test_consultar_estado_success(self, MockTicketComentario, MockMunicipioTicket):
        from datetime import datetime, timedelta
        mock_ticket = MockTicket(id=10, nro_ticket=900100, asunto="Fuga de agua", estado="en_proceso", fecha=datetime.utcnow() - timedelta(days=2))
        MockMunicipioTicket.query.filter_by.return_value.first.return_value = mock_ticket

        mock_comentario = MockTicketComentario(comentario="Equipo técnico asignado.", fecha=datetime.utcnow() - timedelta(days=1))
        MockTicketComentario.query.filter.return_value.order_by.return_value.first.return_value = mock_comentario

        handler = ConsultarEstadoReclamoAction(context=self.base_context)
        result = handler.execute({"nro_ticket": "900100"})

        self.assertTrue(result["success"])
        self.assertIn("M-900100", result["message_to_user"])
        self.assertIn("en_proceso", result["message_to_user"])
        self.assertIn("Equipo técnico asignado", result["message_to_user"])
        self.assertEqual(result["data"]["nro_ticket"], 900100)
        MockMunicipioTicket.query.filter_by.assert_called_once_with(nro_ticket=900100)

    def test_consultar_estado_with_m_prefix(self, MockTicketComentario, MockMunicipioTicket):
        mock_ticket = MockTicket(id=11, nro_ticket=900101, estado="nuevo")
        MockMunicipioTicket.query.filter_by.return_value.first.return_value = mock_ticket
        MockTicketComentario.query.filter.return_value.order_by.return_value.first.return_value = None # No comments

        handler = ConsultarEstadoReclamoAction(context=self.base_context)
        result = handler.execute({"nro_ticket": "M-900101"})

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["nro_ticket"], 900101)
        MockMunicipioTicket.query.filter_by.assert_called_once_with(nro_ticket=900101)


    def test_consultar_estado_not_found(self, MockTicketComentario, MockMunicipioTicket):
        MockMunicipioTicket.query.filter_by.return_value.first.return_value = None

        handler = ConsultarEstadoReclamoAction(context=self.base_context)
        result = handler.execute({"nro_ticket": "00000"})

        self.assertFalse(result["success"])
        self.assertIn("No se encontró el ticket M-00000", result["message_to_user"])

    def test_consultar_estado_invalid_ticket_number(self, MockTicketComentario, MockMunicipioTicket):
        handler = ConsultarEstadoReclamoAction(context=self.base_context)
        result = handler.execute({"nro_ticket": "INVALID"})

        self.assertFalse(result["success"])
        self.assertIn("no parece válido", result["message_to_user"])

    def test_consultar_estado_no_ticket_number_provided(self, MockTicketComentario, MockMunicipioTicket):
        handler = ConsultarEstadoReclamoAction(context=self.base_context)
        result = handler.execute({}) # Empty action_data

        self.assertFalse(result["success"])
        self.assertIn("Necesito el número de ticket", result["message_to_user"])


if __name__ == '__main__':
    unittest.main()
