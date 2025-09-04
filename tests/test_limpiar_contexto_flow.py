import unittest
from app import create_app, db
from config import TestConfig
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio, ConversationState, find_global_menu_action

class TestLimpiarContextoFlow(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        rubro = Rubro(id=1, clave='municipio', nombre='municipio')
        owner_user = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', municipio_id=1)
        owner_user.set_password('password')
        viewer_user = User(id=2, email='vecino@test.com', name='Vecino')
        viewer_user.set_password('password')
        db.session.add_all([rubro, owner_user, viewer_user])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_keyword_resets_context(self):
        owner_user = User.query.get(1)
        viewer_user = User.query.get(2)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(
            chat_session_id='test_reset_ctx',
            user_id=1,
            context_data={
                'contexto_municipio_v2': {
                    'estado_conversacion': ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name,
                    'datos_sugerencia': {'descripcion': 'algo'},
                    'contacto_usuario': {'dni': '12345678'}
                }
            }
        )
        db.session.add(chat_context)
        db.session.commit()

        self.assertEqual(find_global_menu_action("empezar de nuevo"), "limpiar_contexto")

        responder_municipio(
            pregunta_original="empezar de nuevo",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )

        ctx = chat_context.context_data['contexto_municipio_v2']
        self.assertEqual(
            ctx.get('estado_conversacion'),
            ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        )
        self.assertNotIn('datos_sugerencia', ctx)
        self.assertEqual(ctx.get('contacto_usuario', {}).get('dni'), '12345678')

if __name__ == '__main__':
    unittest.main()
