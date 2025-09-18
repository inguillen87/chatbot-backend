import unittest
from unittest.mock import patch

from app import create_app, db
from config import Config
from models import QA, Rubro, User


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
        {
            "key": "almacen",
            "nombre": "Demo Almacén",
            "tipo_chat": "pyme",
            "rubro_clave": "almacen",
            "token": "demo-almacen-token",
            "prompt_context": "Almacén digital con combos familiares, envíos en el día y precios mayoristas.",
            "welcome_message": "Bienvenido al demo del almacén. ¿Buscás algo para tu pedido?",
            "resources": [],
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
        self.rubro_almacen = Rubro(clave="almacen", nombre="Almacén", es_publico=False)
        db.session.add_all([self.rubro_municipio, self.rubro_bodega, self.rubro_almacen])
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
        self.almacen_user = User(
            name="Demo Almacén",
            email="almacen@example.com",
            password_hash="hash",
            token="demo-almacen-token",
            rubro=self.rubro_almacen,
            tipo_chat="pyme",
            nombre_empresa="ByM Almacén",
        )
        db.session.add_all([self.muni_user, self.bodega_user, self.almacen_user])
        db.session.commit()

        # Simula entornos donde el usuario demo pertenece a una empresa (empresa_id != None)
        self.bodega_user.empresa_id = self.muni_user.id
        db.session.add(self.bodega_user)
        db.session.commit()

        faq_muni = QA(
            question="¿Cómo registro un reclamo?",
            answer="Ingresá al portal y completá los datos con ubicación y contacto.",
            rubro_id=self.rubro_municipio.id,
        )
        faq_pyme = QA(
            question="¿Tienen Gran Malbec Reserva?",
            answer="Sí, contamos con Gran Malbec Reserva 2021 a $18.500 la botella.",
            rubro_id=self.rubro_bodega.id,
        )
        faq_pyme_envio = QA(
            question="¿Hacen envíos en Mendoza?",
            answer="Envío sin cargo en Gran Mendoza para pedidos superiores a $45.000.",
            rubro_id=self.rubro_bodega.id,
        )
        faq_almacen = QA(
            question="¿Tienen combos familiares?",
            answer="Sí, armamos combos semanales con bebidas y snacks listos para envío rápido.",
            rubro_id=self.rubro_almacen.id,
        )
        db.session.add_all([faq_muni, faq_pyme, faq_pyme_envio, faq_almacen])
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
                faq_preview = demo_metadata.get("faq_preview")
                self.assertTrue(faq_preview)
                self.assertIn("Malbec", faq_preview[0].get("respuesta", ""))

    def test_unrecognized_demo_selection_emits_socket_message(self):
        session_id = "demo-session-emit-1"
        headers = {"X-Chat-Session-Id": session_id}
        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)

            with patch("routes.chat.socketio.emit") as mock_emit:
                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": {"action": "demo_select_rubro:inexistente"}},
                    headers=headers,
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload.get("fuente"), "demo_selector")

            mock_emit.assert_called_once()
            args, kwargs = mock_emit.call_args
            self.assertEqual(args[0], "message")
            self.assertEqual(args[1], payload)
            self.assertEqual(kwargs.get("room"), session_id)

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

                with patch("routes.chat.socketio.emit") as mock_emit:
                    resp3 = client.post(
                        "/ask/pyme",
                        json={"pregunta": "más vinos"},
                        headers=headers,
                    )

                self.assertEqual(resp3.status_code, 403)
                data = resp3.get_json()
                self.assertEqual(data.get("error"), "demo_limit_reached")

                mock_emit.assert_called_once()
                args, kwargs = mock_emit.call_args
                self.assertEqual(args[0], "message")
                self.assertEqual(args[1], data)
                self.assertEqual(kwargs.get("room"), session_id)

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
                self.assertIn("Preguntas frecuentes destacadas", message)
                self.assertIn("Envío sin cargo", message)

                botones = data.get("botones", [])
                self.assertTrue(any(btn.get("url", "").endswith(".pdf") for btn in botones))

                adjuntos = data.get("adjuntos", [])
                self.assertTrue(any(adj.get("tipo") == "pdf" for adj in adjuntos))
                self.assertTrue(any(adj.get("tipo") == "image" for adj in adjuntos))

                mock_responder.assert_called_once()

    def test_demo_intro_without_resources_uses_faqs(self):
        session_id = "demo-session-5"
        headers = {"X-Chat-Session-Id": session_id}
        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)

            with patch("routes.chat.responder_chatboc") as mock_responder:
                mock_responder.return_value = {
                    "message_body": "Base response",
                    "options_list": [],
                    "message_type": "text",
                }

                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": {"action": "demo_select_rubro:almacen"}},
                    headers=headers,
                )

            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            message = data.get("message_body") or data.get("respuesta") or ""
            self.assertIn("Preguntas frecuentes destacadas", message)
            self.assertIn("combos semanales", message)
            self.assertEqual(data.get("message_type"), "text")

    def test_rubros_endpoint_exposes_demo_metadata(self):
        with self.client as client:
            response = client.get("/rubros/")

        self.assertEqual(response.status_code, 200)
        rubros = response.get_json()
        bodega_entry = next((r for r in rubros if r.get("clave") == "bodega"), None)
        self.assertIsNotNone(bodega_entry)
        demo = bodega_entry.get("demo")
        self.assertIsNotNone(demo)
        self.assertEqual(demo.get("key"), "bodega")
        self.assertTrue(demo.get("resources"))
        self.assertTrue(demo.get("faq_preview"))
        self.assertIn("Malbec", demo["faq_preview"][0].get("respuesta", ""))


if __name__ == "__main__":
    unittest.main()
