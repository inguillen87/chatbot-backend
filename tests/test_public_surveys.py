import unittest
from datetime import datetime, timedelta

from app import create_app
from config import TestingConfig
from models import db, User, EncEncuesta, EncPregunta
from services.encuestas_service import save_respuesta
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
            self.admin_municipio_id = admin.municipio_id
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

    def _create_admin_for_municipio(self, user_id: int, email: str, municipio_id: int):
        with self.app.app_context():
            admin = User(
                id=user_id,
                name=f"Admin {municipio_id}",
                email=email,
                password_hash="x",
                rol="admin",
                tipo_chat="municipio",
                municipio_id=municipio_id,
            )
            db.session.add(admin)
            db.session.commit()
            token = generar_token(
                admin.id,
                admin.rol,
                admin.tipo_chat,
                admin.municipio_id,
                admin.pyme_id,
            )
        return admin, token

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
        }

        resp = self.client.put(
            f"/admin/encuestas/{encuesta_id}",
            json=update_payload,
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        updated = resp.get_json()
        self.assertEqual(updated["estado"], "borrador")

        publish_resp = self.client.post(
            f"/admin/encuestas/{encuesta_id}/publicar",
            headers=self._auth_headers(),
        )
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_json())
        published_data = publish_resp.get_json()
        self.assertTrue(published_data.get("ok"))
        public_slug = published_data.get("slug_publico") or slug
        opciones = updated["preguntas"][0]["opciones"]
        second_option_id = [opt["id"] for opt in opciones if opt["valor"] == "educacion"][0]

        with self.app.app_context():
            service_response = save_respuesta(
                public_slug,
                {
                    "anon_id": "anon-test",
                    "respuestas": [
                        {
                            "pregunta_id": question_id,
                            "opcion_ids": [second_option_id],
                        }
                    ],
                },
                {},
            )
            self.assertIsNotNone(service_response.id)

        with self.app.app_context():
            stored = EncEncuesta.query.get(encuesta_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.respuestas.count(), 1)
            stored_question = EncPregunta.query.get(question_id)
            self.assertEqual(stored_question.texto, "Tema central 2025")
            opciones_ids = {opt.valor for opt in stored_question.opciones}
            self.assertIn("educacion", opciones_ids)

    def test_slug_is_normalized_on_create_and_update(self):
        create_payload = {
            "titulo": "Encuesta Slug",
            "slug": " Participacion-PRUEBA ",
            "preguntas": [
                {
                    "titulo": "Pregunta única",
                    "tipo": "opcion_unica",
                    "opciones": [{"texto": "Sí", "valor": "si"}],
                }
            ],
        }

        create_resp = self.client.post(
            "/admin/encuestas/",
            json=create_payload,
            headers=self._auth_headers(),
        )
        self.assertEqual(create_resp.status_code, 201, create_resp.get_json())
        data = create_resp.get_json()
        encuesta_id = data["id"]
        self.assertEqual(data["slug"], "participacion-prueba")

        update_resp = self.client.put(
            f"/admin/encuestas/{encuesta_id}",
            json={"slug": "PARTICIPACION-PRUEBA", "titulo": "Encuesta Slug 2025"},
            headers=self._auth_headers(),
        )
        self.assertEqual(update_resp.status_code, 200, update_resp.get_json())
        updated = update_resp.get_json()
        self.assertEqual(updated["slug"], "participacion-prueba")

    def test_slug_conflict_respects_normalization(self):
        first_resp = self.client.post(
            "/admin/encuestas/",
            json={
                "titulo": "Primera encuesta",
                "slug": "sondeo-municipal",
                "preguntas": [
                    {
                        "titulo": "Pregunta 1",
                        "tipo": "opcion_unica",
                        "opciones": [{"texto": "A", "valor": "a"}],
                    }
                ],
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(first_resp.status_code, 201, first_resp.get_json())
        first_slug = first_resp.get_json()["slug"]
        self.assertEqual(first_slug, "sondeo-municipal")

        second_resp = self.client.post(
            "/admin/encuestas/",
            json={
                "titulo": "Segunda encuesta",
                "slug": "consulta-ciudadana",
                "preguntas": [
                    {
                        "titulo": "Pregunta 2",
                        "tipo": "opcion_unica",
                        "opciones": [{"texto": "B", "valor": "b"}],
                    }
                ],
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(second_resp.status_code, 201, second_resp.get_json())
        second_id = second_resp.get_json()["id"]

        conflict_resp = self.client.put(
            f"/admin/encuestas/{second_id}",
            json={"slug": " SONDEO-MUNICIPAL ", "titulo": "Actualizada"},
            headers=self._auth_headers(),
        )
        self.assertEqual(conflict_resp.status_code, 409, conflict_resp.get_json())

    def test_listado_filtrado_por_municipio(self):
        primera_resp = self.client.post(
            "/admin/encuestas/",
            json={
                "titulo": "Encuesta municipio 500",
                "preguntas": [
                    {
                        "titulo": "Pregunta 1",
                        "tipo": "opcion_unica",
                        "opciones": [{"texto": "A", "valor": "a"}],
                    }
                ],
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(primera_resp.status_code, 201, primera_resp.get_json())
        slug_municipio_500 = primera_resp.get_json()["slug"]

        _, token_muni_501 = self._create_admin_for_municipio(
            202,
            "admin501@example.com",
            501,
        )

        segunda_resp = self.client.post(
            "/admin/encuestas/",
            json={
                "titulo": "Encuesta municipio 501",
                "preguntas": [
                    {
                        "titulo": "Pregunta 2",
                        "tipo": "opcion_unica",
                        "opciones": [{"texto": "B", "valor": "b"}],
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token_muni_501}"},
        )
        self.assertEqual(segunda_resp.status_code, 201, segunda_resp.get_json())
        slug_municipio_501 = segunda_resp.get_json()["slug"]

        listado_500 = self.client.get(
            "/admin/encuestas/",
            headers=self._auth_headers(),
        )
        self.assertEqual(listado_500.status_code, 200, listado_500.get_json())
        data_500 = listado_500.get_json()["encuestas"]
        self.assertEqual(len(data_500), 1)
        self.assertEqual(data_500[0]["slug"], slug_municipio_500)

        listado_501 = self.client.get(
            "/admin/encuestas/",
            headers={"Authorization": f"Bearer {token_muni_501}"},
        )
        self.assertEqual(listado_501.status_code, 200, listado_501.get_json())
        data_501 = listado_501.get_json()["encuestas"]
        self.assertEqual(len(data_501), 1)
        self.assertEqual(data_501[0]["slug"], slug_municipio_501)


    def test_live_results_includes_trend_heatmap_and_ai_summary(self):
        now = datetime.now().astimezone()
        create_payload = {
            "titulo": "Pulso en vivo",
            "es_votacion_envivo": True,
            "mostrar_resultados_envivo": True,
            "inicio_at": (now - timedelta(hours=2)).isoformat(),
            "fin_at": (now + timedelta(days=2)).isoformat(),
            "publicar": True,
            "preguntas": [
                {
                    "titulo": "¿Qué prioridad debe liderar?",
                    "tipo": "opcion_unica",
                    "obligatoria": True,
                    "opciones": [
                        {"texto": "Seguridad", "valor": "seguridad"},
                        {"texto": "Transporte", "valor": "transporte"},
                    ],
                }
            ],
        }

        resp = self.client.post(
            "/admin/encuestas/",
            json=create_payload,
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())
        data = resp.get_json()
        encuesta_id = data["id"]

        publish_resp = self.client.post(
            f"/admin/encuestas/{encuesta_id}/publicar",
            headers=self._auth_headers(),
        )
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_json())
        publish_data = publish_resp.get_json()
        slug = publish_data.get("slug_publico") or data["slug"]

        with self.app.app_context():
            encuesta = EncEncuesta.query.get(encuesta_id)
            encuesta.inicio_at = None
            encuesta.fin_at = None
            db.session.commit()

        pregunta = data["preguntas"][0]
        pregunta_id = pregunta["id"]
        seguridad_id = pregunta["opciones"][0]["id"]

        for i in range(6):
            submission_id = f"public-live-results-{i:04d}"
            payload = {
                "submission_id": submission_id,
                "anon_id": f"seed-live-{i}",
                # Keep the six submissions in one privacy-safe aggregate cell
                # (public coordinates are hidden until k >= 5).
                "lat": -32.889 + (i * 0.00001),
                "lng": -68.845 + (i * 0.00001),
                "respuestas": [{"pregunta_id": pregunta_id, "opcion_ids": [seguridad_id]}],
            }
            submit_resp = self.client.post(
                f"/api/public/encuestas/{slug}/responder",
                json=payload,
                headers={"Idempotency-Key": submission_id},
            )
            self.assertEqual(submit_resp.status_code, 201, submit_resp.get_json())

        live_resp = self.client.get(f"/api/public/encuestas/{slug}/live-results")
        self.assertEqual(live_resp.status_code, 200, live_resp.get_json())
        live_data = live_resp.get_json()

        self.assertEqual(live_data["slug"], slug)
        self.assertEqual(live_data["total_respuestas"], 6)
        self.assertIn("ai_summary", live_data)
        self.assertTrue(live_data["ai_summary"])
        self.assertIn("momentum", live_data)
        self.assertIn(live_data["momentum"]["trend"], {"subiendo", "estable", "bajando"})
        self.assertIn("delta", live_data["momentum"])
        self.assertIn("window_minutes", live_data["momentum"])

        self.assertIn("kpis", live_data)
        self.assertIn("participation_per_minute", live_data["kpis"])
        self.assertIn("responses_last_hour", live_data["kpis"])

        self.assertTrue(live_data["timeline_minute"])
        primera_pregunta = live_data["preguntas"][0]
        self.assertEqual(primera_pregunta["total_votos"], 6)
        self.assertTrue(primera_pregunta["opciones"])
        self.assertIn("porcentaje", primera_pregunta["opciones"][0])

        heatmap = live_data["heatmap"]
        self.assertTrue(heatmap["enabled"])
        self.assertIn("metadata", heatmap)
        self.assertGreaterEqual(heatmap["metadata"]["points_count"], 1)
        self.assertIn("ai_insights", live_data)
        self.assertTrue(live_data["ai_insights"])


    def test_live_results_supports_lightweight_query_options(self):
        create_payload = {
            "titulo": "Pulso liviano",
            "publicar": True,
            "es_votacion_envivo": True,
            "mostrar_resultados_envivo": True,
            "preguntas": [
                {
                    "titulo": "¿Te parece útil?",
                    "tipo": "opcion_unica",
                    "obligatoria": True,
                    "opciones": [
                        {"texto": "Sí", "valor": "si"},
                        {"texto": "No", "valor": "no"},
                    ],
                }
            ],
        }
        resp = self.client.post("/admin/encuestas/", json=create_payload, headers=self._auth_headers())
        self.assertEqual(resp.status_code, 201, resp.get_json())
        data = resp.get_json()
        encuesta_id = data["id"]

        publish_resp = self.client.post(
            f"/admin/encuestas/{encuesta_id}/publicar",
            headers=self._auth_headers(),
        )
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_json())
        slug = publish_resp.get_json().get("slug_publico") or data["slug"]

        with self.app.app_context():
            encuesta = EncEncuesta.query.get(encuesta_id)
            encuesta.inicio_at = None
            encuesta.fin_at = None
            db.session.commit()

        live_resp = self.client.get(
            f"/api/public/encuestas/{slug}/live-results?include_heatmap=0&window_minutes=20&max_points=100&max_cells=50"
        )
        self.assertEqual(live_resp.status_code, 200, live_resp.get_json())
        payload = live_resp.get_json()
        self.assertFalse(payload["heatmap"]["enabled"])
        self.assertEqual(payload["heatmap"]["points"], [])
        self.assertEqual(payload["momentum"]["window_minutes"], 20)
        self.assertEqual(payload["render_contract"]["preferred_visualization"], "live_vote_command_center")
        self.assertTrue(payload["empty_state"]["is_empty"])
        self.assertFalse(payload["live_telemetry"]["has_responses"])
        self.assertEqual(payload["live_telemetry"]["responses_total"], 0)

    def test_live_results_hidden_when_admin_does_not_publish_partial_results(self):
        create_payload = {
            "titulo": "Pulso privado",
            "publicar": True,
            "es_votacion_envivo": True,
            "mostrar_resultados_envivo": False,
            "preguntas": [
                {
                    "titulo": "Â¿Te parece Ãºtil?",
                    "tipo": "opcion_unica",
                    "obligatoria": True,
                    "opciones": [
                        {"texto": "SÃ­", "valor": "si"},
                        {"texto": "No", "valor": "no"},
                    ],
                }
            ],
        }
        resp = self.client.post("/admin/encuestas/", json=create_payload, headers=self._auth_headers())
        self.assertEqual(resp.status_code, 201, resp.get_json())
        data = resp.get_json()
        encuesta_id = data["id"]

        publish_resp = self.client.post(
            f"/admin/encuestas/{encuesta_id}/publicar",
            headers=self._auth_headers(),
        )
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_json())
        slug = publish_resp.get_json().get("slug_publico") or data["slug"]

        with self.app.app_context():
            encuesta = EncEncuesta.query.get(encuesta_id)
            encuesta.inicio_at = None
            encuesta.fin_at = None
            db.session.commit()

        live_resp = self.client.get(f"/api/public/encuestas/{slug}/live-results")
        self.assertEqual(live_resp.status_code, 403)
        self.assertEqual(live_resp.get_json().get("reason_code"), "live_results_hidden")

    def test_admin_no_puede_modificar_otra_municipalidad(self):
        create_resp = self.client.post(
            "/admin/encuestas/",
            json={
                "titulo": "Encuesta propia",
                "preguntas": [
                    {
                        "titulo": "Pregunta",
                        "tipo": "opcion_unica",
                        "opciones": [{"texto": "Opcion", "valor": "opcion"}],
                    }
                ],
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(create_resp.status_code, 201, create_resp.get_json())
        encuesta_id = create_resp.get_json()["id"]

        _, token_muni_777 = self._create_admin_for_municipio(
            303,
            "admin777@example.com",
            777,
        )

        detalle_otro = self.client.get(
            f"/admin/encuestas/{encuesta_id}",
            headers={"Authorization": f"Bearer {token_muni_777}"},
        )
        self.assertEqual(detalle_otro.status_code, 403)

        actualizacion = self.client.put(
            f"/admin/encuestas/{encuesta_id}",
            json={"titulo": "No deberia"},
            headers={"Authorization": f"Bearer {token_muni_777}"},
        )
        self.assertEqual(actualizacion.status_code, 403)


if __name__ == "__main__":
    unittest.main()

