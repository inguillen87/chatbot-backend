import unittest
from unittest.mock import patch
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from flask import Flask
from services.municipios import responder_municipio, ConversationState, CONTEXTO_MUNICIPIO
from models import db, User, Rubro, ChatSessionContext


class TestContextReset(unittest.TestCase):

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

        db.init_app(self.app)

        with self.app.app_context():
            db.create_all()
            owner_user = User(id=1, name='owner', email='o@test.com', password_hash='x')
            viewer_user = User(id=2, name='viewer', email='v@test.com', password_hash='x')
            rubro = Rubro(id=1, clave='municipios', nombre='municipios')
            db.session.add_all([owner_user, viewer_user, rubro])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @patch('services.municipios.llamar_gemini')
    def test_general_query_resets_reclamo_context(self, mock_llm):
        with self.app.app_context():
            owner_user = User.query.get(1)
            viewer_user = User.query.get(2)
            rubro = Rubro.query.get(1)

            chat_session = ChatSessionContext(
                chat_session_id='session_reset',
                user_id=owner_user.id,
                context_data={
                    CONTEXTO_MUNICIPIO: {
                        'estado_conversacion': ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
                        'datos_parciales_llm_reclamo': {'ubicacion': 'Calle Falsa 123'}
                    }
                }
            )
            db.session.add(chat_session)
            db.session.commit()

            mock_llm.return_value = {
                'respuesta_usuario': 'Buscando...',
                'accion_backend': 'ejecutar_herramienta',
                'datos_estructura': {
                    'target': 'municipio',
                    'nombre_herramienta': 'buscar_negocios_cercanos',
                    'parametros_herramienta': {'tipo_negocio': 'veterinaria'}
                },
                'pedir_info': None,
                'botones': []
            }

            responder_municipio(
                pregunta_original='quiero veterinarias',
                owner_user=owner_user,
                rubro_obj=rubro,
                viewer_user=viewer_user,
                chat_db_context=chat_session,
            )

            ctx = chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(
                ctx.get('estado_conversacion'),
                ConversationState.CONVERSACION_GENERAL_LLM.name
            )
            # Ensure old complaint data was removed
            self.assertNotIn('direccion_reclamo', ctx)
            self.assertEqual(ctx.get('datos_parciales_llm_reclamo'), {})


    @patch('services.municipio_responder.accion_crear_reclamo_municipio')
    @patch('services.municipio_responder.llamar_gemini')
    def test_ticket_creation_clears_context(self, mock_llm, mock_crear_reclamo):
        with self.app.app_context():
            owner_user = User.query.get(1)
            viewer_user = User.query.get(2)
            rubro = Rubro.query.get(1)

            # 1. Setup initial context: user is in the middle of a claim
            chat_session = ChatSessionContext(
                chat_session_id='session_ticket_creation',
                user_id=owner_user.id,
                context_data={
                    CONTEXTO_MUNICIPIO: {
                        'estado_conversacion': ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
                        'esperando_info_llm_reclamo': 'ubicacion',
                        'datos_parciales_llm_reclamo': {
                            'categoria': 'Bacheo',
                            'descripcion': 'Hay un pozo muy grande'
                        },
                        'historial_llm_reclamo': [
                            {'pregunta_usuario': 'Quiero reportar un bache', 'respuesta_ia': 'Ok, ¿dónde está?'}
                        ]
                    }
                }
            )
            db.session.add(chat_session)
            db.session.commit()

            # 2. Mock the LLM response: user provides the final piece of info
            mock_llm.return_value = {
                'respuesta_usuario': '¡Gracias! Creando tu reclamo...',
                'accion_backend': 'crear_reclamo',
                'datos_estructura': {
                    'target': 'municipio',
                    'ubicacion': 'Calle Falsa 123'
                },
                'pedir_info': None, # No more info needed
                'botones': []
            }

            # 3. Mock the ticket creation action to simulate success
            mock_crear_reclamo.return_value = {
                "success": True,
                "message_to_user": "✅ Reclamo recibido! N° de Ticket: T-12345",
                "data": {"ticket_id": 123}
            }

            # 4. Call the main responder function
            from services.municipio_responder import responder_municipio
            responder_municipio(
                pregunta_original='Es en Calle Falsa 123',
                owner_user=owner_user,
                rubro_obj=rubro,
                viewer_user=viewer_user,
                chat_db_context=chat_session,
            )

            # 5. Assert that the context was cleared
            ctx = chat_session.context_data[CONTEXTO_MUNICIPIO]

            # After ticket creation, the specific context should be empty
            self.assertEqual(ctx, {})


if __name__ == '__main__':
    unittest.main()

