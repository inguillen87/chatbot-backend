import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio, ConversationState, CONTEXTO_MUNICIPIO

class TestSimulatedClaimFlow(unittest.TestCase):

    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Mock owner and viewer users
        self.owner_user = User(id=1, nombre_empresa="Test Muni", tipo_chat="municipio")
        self.viewer_user = User(id=2, name="Test User")
        self.rubro_obj = Rubro(id=1, nombre="municipio", es_publico=True)
        db.session.add_all([self.owner_user, self.viewer_user, self.rubro_obj])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_claim_flow_step_by_step(self):
        """
        Simulates a step-by-step claim creation process, verifying context and state transitions.
        """
        chat_session = ChatSessionContext(chat_session_id="claim_flow_test_1")
        db.session.add(chat_session)
        db.session.commit()

        # Step 1: User initiates a claim
        with patch('services.municipio_responder.llamar_gemini') as mock_llm:
            mock_llm.return_value = {
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {"categoria": "Alumbrado"},
                "pedir_info": "ubicacion",
                "message_body": "Entendido, reclamo de Alumbrado. ¿Dónde es?"
            }
            response = responder_municipio(
                pregunta_original="Luz quemada",
                owner_user=self.owner_user,
                viewer_user=self.viewer_user,
                rubro_obj=self.rubro_obj,
                chat_db_context=chat_session
            )
            self.assertIn("¿Dónde es?", response['message_body'])
            context = chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(context['estado_conversacion'], 'ESPERANDO_INFO_RECLAMO_LLM')
            self.assertEqual(context['datos_parciales_llm_reclamo']['categoria'], 'Alumbrado')

        # Step 2: User provides location
        with patch('services.municipio_responder.llamar_gemini') as mock_llm:
            mock_llm.return_value = {
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"ubicacion": "Calle Falsa 123"},
                "pedir_info": "nombre_completo",
                "message_body": "OK. ¿Tu nombre?"
            }
            response = responder_municipio(
                pregunta_original="Calle Falsa 123",
                owner_user=self.owner_user,
                viewer_user=self.viewer_user,
                rubro_obj=self.rubro_obj,
                chat_db_context=chat_session
            )
            self.assertIn("¿Tu nombre?", response['message_body'])
            context = chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(context['datos_parciales_llm_reclamo']['ubicacion'], 'Calle Falsa 123')

        # Step 3: User provides name, ticket is created
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_create_ticket, \
             patch('services.municipio_responder.llamar_gemini') as mock_llm:

            mock_ticket = MagicMock()
            mock_ticket.nro_ticket = "T-54321"
            mock_create_ticket.return_value = mock_ticket

            mock_llm.return_value = {
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"nombre_usuario_detectado": "Juan Perez"},
                "pedir_info": None,
                "message_body": "¡Gracias, Juan! Tu reclamo ha sido creado."
            }

            response = responder_municipio(
                pregunta_original="Juan Perez",
                owner_user=self.owner_user,
                viewer_user=self.viewer_user,
                rubro_obj=self.rubro_obj,
                chat_db_context=chat_session
            )
            self.assertIn("T-54321", response['message_body'])
            mock_create_ticket.assert_called_once()
            context = chat_session.context_data.get(CONTEXTO_MUNICIPIO, {})
            self.assertNotIn('datos_parciales_llm_reclamo', context) # Context should be cleared

    def test_claim_flow_all_info_at_once(self):
        """
        Tests creating a claim when the user provides all information in a single message.
        """
        chat_session = ChatSessionContext(chat_session_id="claim_flow_test_2")
        db.session.add(chat_session)
        db.session.commit()

        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_create_ticket, \
             patch('services.municipio_responder.llamar_gemini') as mock_llm:

            mock_ticket = MagicMock()
            mock_ticket.nro_ticket = "T-12345"
            mock_create_ticket.return_value = mock_ticket

            mock_llm.return_value = {
                "accion_backend": "crear_reclamo",
                "datos_estructura": {
                    "categoria": "Bache",
                    "ubicacion": "Av. Siempreviva 742",
                    "descripcion": "Agujero gigante",
                    "nombre_usuario_detectado": "Homero Simpson"
                },
                "pedir_info": None,
                "message_body": "Reclamo creado."
            }

            response = responder_municipio(
                pregunta_original="Quiero reportar un bache en Av. Siempreviva 742, es un agujero gigante. Soy Homero Simpson.",
                owner_user=self.owner_user,
                viewer_user=self.viewer_user,
                rubro_obj=self.rubro_obj,
                chat_db_context=chat_session
            )
            self.assertIn("T-12345", response['message_body'])
            mock_create_ticket.assert_called_once()
            call_args = mock_create_ticket.call_args[1]
            self.assertEqual(call_args['ticket_data']['categoria'], 'Bache')
            self.assertEqual(call_args['ticket_data']['nombre_vecino'], 'Homero Simpson')

if __name__ == '__main__':
    unittest.main()
