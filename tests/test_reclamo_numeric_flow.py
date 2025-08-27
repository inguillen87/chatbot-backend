import unittest
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio, ConversationState, CONTEXTO_MUNICIPIO

class TestReclamoNumericFlow(unittest.TestCase):
    def setUp(self):
        self.app = create_app('config.TestConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        rubro = Rubro(id=1, clave='municipio', nombre='municipio')
        owner = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', rubro=rubro, municipio_id=1)
        owner.set_password('pass')
        db.session.add_all([rubro, owner])
        db.session.commit()
        self.owner = owner
        self.chat_ctx = ChatSessionContext(chat_session_id='numeric_flow', user_id=owner.id, context_data={})
        db.session.add(self.chat_ctx)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_numeric_selection_starts_flow(self):
        # Show reclamos menu via direct action
        payload = {"action": "mostrar_menu_reclamos"}
        response = responder_municipio(payload, self.owner, self.owner.rubro, viewer_user=self.owner, chat_db_context=self.chat_ctx, channel="whatsapp")
        self.assertIn("Elegí una opción para tu reclamo", response["message_body"])
        estado = self.chat_ctx.context_data[CONTEXTO_MUNICIPIO]["estado_conversacion"]
        self.assertEqual(estado, ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name)

        # User selects option 2 -> Luminaria
        response2 = responder_municipio("2", self.owner, self.owner.rubro, viewer_user=self.owner, chat_db_context=self.chat_ctx, channel="whatsapp")
        self.assertIn("describí brevemente", response2["message_body"])
        flow_ctx = self.chat_ctx.context_data["reclamo_flow_v2"]
        self.assertEqual(flow_ctx["datos_reclamo"]["categoria"], "Arbolado")
        self.assertEqual(flow_ctx["state"], "ESPERANDO_DESCRIPCION")

if __name__ == '__main__':
    unittest.main()
