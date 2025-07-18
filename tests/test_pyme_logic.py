import unittest
from unittest.mock import patch
from app import create_app, db
from models import User, PymeTicket, TicketComentario, Rubro
from types import SimpleNamespace
from services import pymes

class PymeLogicTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.from_object('config.TestConfig')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.setup_database()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def setup_database(self):
        self.rubro = Rubro(nombre='vinoteca', clave='vinoteca')
        self.user = User(name='Bodega Ejemplo', email='a@b.com', password_hash='password', rubro=self.rubro, plan='full', limite_preguntas=100)
        db.session.add(self.rubro)
        db.session.add(self.user)
        db.session.commit()

    @patch('services.pymes.robust_chat', return_value='PREGUNTA_NUEVA')
    def test_es_pregunta_nueva_llm(self, mock_llm):
        self.assertTrue(pymes.es_pregunta_nueva('hola', 'el dato solicitado'))
        mock_llm.return_value = 'RESPUESTA_VALIDA'
        self.assertFalse(pymes.es_pregunta_nueva('hola', 'el dato solicitado'))

    @patch('services.catalogo_local.buscar_catalogo_local', return_value=[])
    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme(self, mock_clf, mock_servicio, mock_buscar):
        with self.app.app_context():
            mock_servicio.crear_nuevo_ticket.return_value = PymeTicket(id=1, nro_ticket=654321, pregunta='pregunta')
            mock_servicio.crear_comentario.return_value = None
            resp = pymes.responder_pyme('Necesito hablar con un agente', self.user, self.rubro, viewer_user=self.user)
            self.assertIn('sala de chat', resp['respuesta'])

    @patch('services.catalogo_local.buscar_catalogo_local', return_value=[])
    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme_anonymous_requires_login(self, mock_clf, mock_servicio, mock_buscar):
        with self.app.app_context():
            mock_servicio.crear_nuevo_ticket.return_value = PymeTicket(id=1, nro_ticket=654321, pregunta='pregunta')
            resp = pymes.responder_pyme('Necesito hablar con un agente', self.user, self.rubro, viewer_user=None)
            self.assertIn('iniciar sesión', resp['respuesta'])

    @patch('services.pymes.detectar_small_talk_con_llm', return_value=False)
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='saludo')
    def test_greeting_with_typo(self, mock_clf, mock_small):
        with self.app.app_context():
            resp = pymes.responder_pyme('holaa buenos noxes', self.user, self.rubro, viewer_user=self.user)
            self.assertIn('¿En qué puedo ayudarte hoy', resp['respuesta'])

    def test_small_talk(self):
        with self.app.app_context():
            with patch('services.logic.get_cohere_response', side_effect=['SI', '¡Hola! ¿Cómo estás?']) as mock_llm:
                resp = pymes.responder_pyme('¿Cómo andas?', self.user, self.rubro, viewer_user=self.user)
                self.assertEqual(resp['fuente'], 'smalltalk_pyme_llm')
                self.assertIn('Hola', resp['respuesta'])
                self.assertEqual(mock_llm.call_count, 2)

    @patch('services.pymes.robust_chat', return_value='negativo')
    def test_sentiment_handler(self, mock_llm):
        with self.app.app_context():
            resp = pymes.responder_pyme('este servicio es horrible', self.user, self.rubro, viewer_user=self.user)
            self.assertEqual(resp['fuente'], 'sentimiento_negativo')
            self.assertIn('agente', resp['respuesta'].lower())

    @patch('services.pymes.robust_chat', return_value='positivo')
    def test_sentiment_handler_positive(self, mock_llm):
        with self.app.app_context():
            resp = pymes.responder_pyme('excelente servicio muchas gracias', self.user, self.rubro, viewer_user=self.user)
            self.assertEqual(resp['fuente'], 'sentimiento_positivo')
            self.assertIn('gracias', resp['respuesta'].lower())

if __name__ == '__main__':
    unittest.main()
