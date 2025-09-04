import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app, db
from config import TestConfig
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio, ConversationState

class TestSugerenciaFlow(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        rubro = Rubro(id=1, clave='municipio', nombre='municipio')
        owner_user = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', rubro=rubro, municipio_id=1)
        owner_user.set_password('password')
        viewer_user = User(id=2, email='vecino@test.com', name='Vecino', direccion='Calle 123', telefono='+5491111111')
        viewer_user.set_password('password')
        db.session.add_all([rubro, owner_user, viewer_user])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_sugerencia_flow(self):
        owner_user = User.query.get(1)
        viewer_user = User.query.get(2)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_sugerencia_session', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        # 1. User starts suggestion flow
        response = responder_municipio(
            pregunta_original={"action": "enviar_sugerencia"},
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )
        self.assertIn("escribí tu sugerencia", response["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
        )

        # 2. User sends suggestion text and is asked for missing DNI
        sugerencia_texto = "Sería bueno que pongan más bancos en la plaza."
        response_2 = responder_municipio(
            pregunta_original=sugerencia_texto,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )
        self.assertIn("Faltan: dni", response_2["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
        )

        # 3. User provides contact details including DNI; ensure LLM is not called
        contact_msg = "Marcelo Guillen 32877851 2613168608 sarmiento 125 Junin Mendoza"
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
             patch('services.municipio_responder.extract_multiple_contact_details_llm') as mock_llm:
            response_3 = responder_municipio(
                pregunta_original=contact_msg,
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=viewer_user,
                chat_db_context=chat_context
            )
            mock_crear_ticket.assert_not_called()
            mock_llm.assert_not_called()

        self.assertIn("confirmá si los datos", response_3["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
        )
        datos = chat_context.context_data['contexto_municipio_v2']['datos_sugerencia']
        self.assertEqual(datos.get('dni'), '32877851')

        # 4. User confirms and ticket is created
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
            mock_crear_ticket.return_value = {"id": 99, "nro_ticket": "S-12345"}
            response_4 = responder_municipio(
                pregunta_original={"action": "confirmar_sugerencia_si"},
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=viewer_user,
                chat_db_context=chat_context
            )
            mock_crear_ticket.assert_called_once()
            call_kwargs = mock_crear_ticket.call_args.kwargs
            self.assertEqual(call_kwargs['ticket_data']['categoria'], 'Sugerencia')
            self.assertEqual(call_kwargs['ticket_data']['detalles'], sugerencia_texto)
            self.assertEqual(call_kwargs['ticket_data']['municipio_id'], owner_user.municipio_id)
            self.assertTrue(response_4.get("success"))
            self.assertIn("Hemos recibido tu sugerencia", response_4.get("message_to_user", ""))
            self.assertIn("S-12345", response_4.get("message_to_user", ""))

    def test_sugerencia_reutiliza_contacto(self):
        owner_user = User.query.get(1)
        viewer_user = User.query.get(2)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_sugerencia_reuse', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        # Primer sugerencia para almacenar datos de contacto
        responder_municipio(
            pregunta_original={"action": "enviar_sugerencia"},
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )
        responder_municipio(
            pregunta_original="Faltan árboles en la plaza",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )
        contact_msg = "Marcelo Guillen 32877851 guillen@test.com sarmiento 125 Junin Mendoza"
        with patch('services.municipio_responder.extract_multiple_contact_details_llm') as mock_llm:
            responder_municipio(
                pregunta_original=contact_msg,
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=viewer_user,
                chat_db_context=chat_context
            )
            mock_llm.assert_not_called()
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
            mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "S-1"}
            responder_municipio(
                pregunta_original={"action": "confirmar_sugerencia_si"},
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=viewer_user,
                chat_db_context=chat_context
            )

        # Verificar que los datos de contacto quedaron guardados para reutilización
        contacto = chat_context.context_data['contexto_municipio_v2'].get('contacto_usuario', {})
        self.assertEqual(contacto.get('dni'), '32877851')
        self.assertEqual(contacto.get('email'), 'guillen@test.com')

if __name__ == '__main__':
    unittest.main()
