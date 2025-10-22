import unittest

from app import create_app
from config import TestConfig
from extensions import db
from models import (
    ChatSessionContext,
    MunicipioTicket,
    PublicSurvey,
    PublicSurveyResponse,
    PymeTicket,
    SugerenciaCiudadano,
    TicketComentario,
    User,
    generate_token,
)
from services.user_merge import merge_anon_into_user


class WebAuthnSupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestConfig)
        cls.app_context = cls.app.app_context()
        cls.app_context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.app_context.pop()

    def setUp(self):
        db.session.query(ChatSessionContext).delete()
        db.session.query(PublicSurveyResponse).delete()
        db.session.query(PublicSurvey).delete()
        db.session.query(SugerenciaCiudadano).delete()
        db.session.query(TicketComentario).delete()
        db.session.query(MunicipioTicket).delete()
        db.session.query(PymeTicket).delete()
        db.session.query(User).delete()
        db.session.commit()

    def test_create_or_get_by_anon_returns_same_user(self):
        anon_id = "anon-test-123"
        first = User.create_or_get_by_anon(anon_id, "Ciudadano Test")
        db.session.commit()

        self.assertEqual(first.anon_id, anon_id)
        self.assertIn("@passkey.chatboc", first.email)
        self.assertTrue(first.password_hash)

        second = User.create_or_get_by_anon(anon_id, "Otro Nombre")
        db.session.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.name, first.name)

    def test_merge_anon_into_user_reassigns_records(self):
        anon_id = "anon-merge-001"

        user = User(
            name="Vecino",
            email="vecino@example.com",
            token=generate_token(),
            rol="usuario",
        )
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()

        municipio_ticket = MunicipioTicket(
            pregunta="¿Cuándo se arregla la calle?",
            user_id=None,
            municipio_id=None,
            anon_id=anon_id,
        )
        pyme_ticket = PymeTicket(
            pregunta="¿Tienen stock?",
            anon_id=anon_id,
            nro_ticket=1,
        )
        comentario = TicketComentario(
            comentario="Envío una foto",
            municipio_ticket=municipio_ticket,
            anon_id=anon_id,
        )
        sugerencia = SugerenciaCiudadano(
            texto_sugerencia="Más contenedores verdes",
            anon_id=anon_id,
        )
        encuesta = PublicSurvey(slug="demo", titulo="Encuesta", estado="published")
        db.session.add(encuesta)
        db.session.commit()

        respuesta = PublicSurveyResponse(
            survey_id=encuesta.id,
            anon_id=anon_id,
        )
        chat_context = ChatSessionContext(anon_id=anon_id)

        db.session.add_all(
            [
                municipio_ticket,
                pyme_ticket,
                comentario,
                sugerencia,
                respuesta,
                chat_context,
            ]
        )
        db.session.commit()

        stats = merge_anon_into_user(anon_id, user)

        self.assertGreaterEqual(stats["tickets"], 2)
        self.assertEqual(stats["chat_contexts"], 1)
        self.assertEqual(stats["sugerencias"], 1)
        self.assertEqual(stats["encuestas"], 1)

        self.assertEqual(MunicipioTicket.query.first().user_id, user.id)
        self.assertIsNone(MunicipioTicket.query.first().anon_id)
        self.assertEqual(PymeTicket.query.first().user_id, user.id)
        self.assertIsNone(PymeTicket.query.first().anon_id)
        self.assertEqual(TicketComentario.query.first().user_id, user.id)
        self.assertIsNone(TicketComentario.query.first().anon_id)
        self.assertEqual(SugerenciaCiudadano.query.first().user_id, user.id)
        self.assertIsNone(SugerenciaCiudadano.query.first().anon_id)
        self.assertEqual(PublicSurveyResponse.query.first().user_id, user.id)
        self.assertIsNone(PublicSurveyResponse.query.first().anon_id)
        self.assertEqual(ChatSessionContext.query.first().user_id, user.id)
        self.assertIsNone(ChatSessionContext.query.first().anon_id)


if __name__ == "__main__":
    unittest.main()
