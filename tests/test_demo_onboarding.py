import unittest
from unittest.mock import patch

from app import create_app, db
from config import Config
from models import User, Rubro


class DemoConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    DEMO_MAX_MESSAGES_PER_SESSION = 2
    DEMO_RUBROS = [
        {
            "key": "municipio",
            "nombre": "Municipio Demo",
            "tipo_chat": "municipio",
            "rubro_clave": "municipio",
            "prompt_context": "Municipio demo que atiende reclamos y trámites digitales.",
            "resources": [
                {
                    "title": "Guía express",
                    "type": "pdf",
                    "url": "/static/demo/municipio/guia-tramites-rapidos.pdf",
                }
            ],
        },
        {
            "key": "bodega",
            "nombre": "Demo Bodega",
            "tipo_chat": "pyme",
            "rubro_clave": "bodega",
            "token": "demo-bodega-token",
            "prompt_context": "Catálogo destacado: Malbec Reserva ($18000) y Torrontés Fresco ($11500).",
            "resources": [
                {
                    "title": "Catálogo Premium 2024",
                    "type": "pdf",
                    "url": "/static/demo/bodega/catalogo-premium-2024.pdf",
                },
                {
                    "title": "Gran Malbec Reserva",
                    "type": "image",
                    "url": "/static/demo/bodega/gran-malbec-reserva.svg",
                },
            ],
        },
    ]


class DemoOnboardingTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(DemoConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.rubro_municipio = Rubro(clave="municipio", nombre="Municipio", es_publico=True)
        self.rubro_bodega = Rubro(clave="bodega", nombre="Bodega", es_publico=False)
        db.session.add_all([self.rubro_municipio, self.rubro_bodega])
        db.session.commit()

        self.muni_user = User(
            name="Demo Municipio",
            email="muni@example.com",
            password_hash="hash",
            rubro=self.rubro_municipio,
            tipo_chat="municipio",
            rol="admin",
        )
        self.bodega_user = User(
            name="Demo Bodega",
            email="bodega@example.com",
            password_hash="hash",
            token="demo-bodega-token",
            rubro=self.rubro_bodega,
            tipo_chat="pyme",
            nombre_empresa="Bodega Demo",
        )
        db.session.add_all([self.muni_user, self.bodega_user])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_initial_demo_menu_returns_options_and_cookie(self):
        session_id = "demo-session-1"
        with self.client as client:
            response = client.post(
                "/ask/pyme",
                json={"pregunta": "__INIT__"},
                headers={"X-Chat-Session-Id": session_id},
            )

            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data.get("fuente"), "demo_selector")
            options = data.get("options_list", [])
            self.assertTrue(options)
            self.assertTrue(
                any(opt.get("action_id", "").startswith("demo_select_rubro") for opt in options)
            )
            cookie_header = response.headers.get("Set-Cookie", "")
            self.assertIn("chatboc_anon_id", cookie_header)

    def test_selecting_demo_loads_owner(self):
        session_id = "demo-session-2"
        headers = {"X-Chat-Session-Id": session_id}
        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)

            with patch("routes.chat.responder_chatboc") as mock_responder:
                mock_responder.return_value = {
                    "message_body": "ok",
                    "options_list": [],
                    "message_type": "text",
                }

                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": {"action": "demo_select_rubro:bodega"}},
                    headers=headers,
                )

                self.assertEqual(response.status_code, 200)
                _, kwargs = mock_responder.call_args
                owner = kwargs.get("owner_user")
                rubro_obj = kwargs.get("rubro_obj")
                demo_metadata = kwargs.get("demo_metadata")

                self.assertIsNotNone(owner)
                self.assertEqual(owner.id, self.bodega_user.id)
                self.assertEqual(kwargs.get("tipo_chat"), "pyme")
                self.assertIsNotNone(rubro_obj)
                self.assertEqual(rubro_obj.id, self.rubro_bodega.id)
                self.assertIsNotNone(demo_metadata)
                self.assertEqual(demo_metadata.get("key"), "bodega")
                self.assertIn("Malbec", demo_metadata.get("prompt_context", ""))
                self.assertIsInstance(demo_metadata.get("resources"), list)

    def test_demo_message_limit_enforced(self):
        session_id = "demo-session-3"
        headers = {"X-Chat-Session-Id": session_id}
        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)

            with patch("routes.chat.responder_chatboc") as mock_responder:
                mock_responder.return_value = {
                    "message_body": "ok",
                    "options_list": [],
                    "message_type": "text",
                }

                client.post(
                    "/ask/pyme",
                    json={"pregunta": {"action": "demo_select_rubro:bodega"}},
                    headers=headers,
                )

                resp1 = client.post(
                    "/ask/pyme",
                    json={"pregunta": "quiero un malbec"},
                    headers=headers,
                )
                self.assertEqual(resp1.status_code, 200)

                resp2 = client.post(
                    "/ask/pyme",
                    json={"pregunta": "otro vino"},
                    headers=headers,
                )
                self.assertEqual(resp2.status_code, 200)

                resp3 = client.post(
                    "/ask/pyme",
                    json={"pregunta": "más vinos"},
                    headers=headers,
                )
                self.assertEqual(resp3.status_code, 403)
                data = resp3.get_json()
                self.assertEqual(data.get("error"), "demo_limit_reached")
                self.assertEqual(mock_responder.call_count, 3)

    def test_demo_selection_returns_curated_material(self):
        session_id = "demo-session-4"
        headers = {"X-Chat-Session-Id": session_id}
        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)

            with patch("routes.chat.responder_chatboc") as mock_responder:
                mock_responder.return_value = {
                    "message_body": "Bienvenido",
                    "options_list": [],
                    "message_type": "text",
                    "adjuntos": [],
                }

                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": {"action": "demo_select_rubro:bodega"}},
                    headers=headers,
                )

                self.assertEqual(response.status_code, 200)
                data = response.get_json()

                message = data.get("message_body") or data.get("respuesta")
                self.assertIn("Catálogo Premium", message)

                botones = data.get("botones", [])
                self.assertTrue(any(btn.get("url", "").endswith(".pdf") for btn in botones))

                adjuntos = data.get("adjuntos", [])
                self.assertTrue(any(adj.get("tipo") == "pdf" for adj in adjuntos))
                self.assertTrue(any(adj.get("tipo") == "image" for adj in adjuntos))

                mock_responder.assert_called_once()


if __name__ == "__main__":
    unittest.main()
