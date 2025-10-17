import unittest

from app import create_app
from config import TestingConfig
from models import db, User, PublicSurvey, PublicSurveyQuestion
from utils.auth_helpers import generar_token


class PublicSurveyFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            admin = User(
                id=101,
                name="Admin",
                email="admin@example.com",
                password_hash="x",
                rol="admin",
                tipo_chat="municipio",
                municipio_id=500,
            )
            db.session.add(admin)
            db.session.commit()
            self.admin_id = admin.id
            self.token = generar_token(
                admin.id,
                admin.rol,
                admin.tipo_chat,
                admin.municipio_id,
                admin.pyme_id,
            )

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_admin_can_edit_and_collect_responses(self):
        create_payload = {
            "titulo": "Consulta ciudadana",
            "descripcion": "Definí tus prioridades",
            "preguntas": [
                {
                    "titulo": "Tema central",
                    "tipo": "opcion_unica",
                    "obligatoria": True,
                    "opciones": [
                        {"texto": "Seguridad", "valor": "seguridad"},
                        {"texto": "Salud", "valor": "salud"},
                    ],
                }
            ],
            "publicar": True,
        }

        resp = self.client.post(
            "/admin/encuestas/",
            json=create_payload,
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())
        data = resp.get_json()
        encuesta_id = data["id"]
        slug = data["slug"]
        question = data["preguntas"][0]
        question_id = question["id"]
        first_option_id = question["opciones"][0]["id"]

        update_payload = {
            "titulo": "Consulta ciudadana 2025",
            "preguntas": [
                {
                    "id": question_id,
                    "titulo": "Tema central 2025",
                    "tipo": "opcion_unica",
                    "obligatoria": True,
                    "opciones": [
                        {
                            "id": first_option_id,
                            "texto": "Seguridad vecinal",
                            "valor": "seguridad",
                        },
                        {"texto": "Educación", "valor": "educacion"},
                    ],
                }
            ],
            "publicar": True,
        }

        resp = self.client.put(
            f"/admin/encuestas/{encuesta_id}",
            json=update_payload,
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        updated = resp.get_json()
        self.assertEqual(updated["estado"], "published")
        opciones = updated["preguntas"][0]["opciones"]
        second_option_id = [opt["id"] for opt in opciones if opt["valor"] == "educacion"][0]

        public_resp = self.client.post(
            f"/public/encuestas/{slug}/respuestas",
            json={
                "anon_id": "anon-test",
                "respuestas": [
                    {
                        "pregunta_id": question_id,
                        "opcion_id": second_option_id,
                    }
                ],
            },
        )
        self.assertEqual(public_resp.status_code, 201, public_resp.get_json())
        self.assertTrue(public_resp.get_json().get("success"))

        with self.app.app_context():
            stored = PublicSurvey.query.get(encuesta_id)
            self.assertIsNotNone(stored)
            self.assertEqual(len(stored.respuestas), 1)
            stored_question = PublicSurveyQuestion.query.get(question_id)
            self.assertEqual(stored_question.titulo, "Tema central 2025")
            opciones_ids = {opt.valor for opt in stored_question.opciones}
            self.assertIn("educacion", opciones_ids)


if __name__ == "__main__":
    unittest.main()

