import unittest
from unittest.mock import patch

from app import create_app, db
from config import Config
from models import ChatSessionContext, QA, Rubro, User
from services.demo_registry import load_demo_rubros
from services.logic import responder_chatboc


class OfflineDemoConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    DEMO_RUBROS = [
        {
            "key": "bodega",
            "nombre": "Demo Bodega",
            "tipo_chat": "pyme",
            "rubro_clave": "bodega",
            "token": "demo-bodega-token",
            "prompt_context": "Catálogo destacado con Gran Malbec Reserva, blends y Torrontés.",
            "resources": [
                {
                    "title": "Catálogo Premium 2024",
                    "type": "pdf",
                    "url": "/static/demo/bodega/catalogo-premium-2024.pdf",
                },
                {
                    "title": "Lista de precios mayoristas",
                    "type": "pdf",
                    "url": "/static/demo/bodega/lista-precios-mayoristas.pdf",
                },
                {
                    "title": "Combos de degustación corporativa",
                    "type": "pdf",
                    "url": "/static/demo/bodega/combos-degustacion-corporativas.pdf",
                },
                {
                    "title": "Mapa de envíos boutique",
                    "type": "image",
                    "url": "/static/demo/bodega/rutas-envio-premium.svg",
                },
                {
                    "title": "Gran Malbec Reserva 2021",
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
            "prompt_context": "Pedidos mixtos con combos minoristas, precios mayoristas y logística express.",
            "resources": [
                {
                    "title": "Catálogo de combos digitales",
                    "type": "pdf",
                    "url": "/static/demo/almacen/catalogo-combos-2024.pdf",
                },
                {
                    "title": "Lista de precios mayoristas",
                    "type": "pdf",
                    "url": "/static/demo/almacen/lista-precios-mayoristas.pdf",
                },
                {
                    "title": "Mapa de logística de delivery",
                    "type": "image",
                    "url": "/static/demo/almacen/rutas-delivery.svg",
                },
            ],
        },
        {
            "key": "municipio",
            "nombre": "Municipio Demo",
            "tipo_chat": "municipio",
            "rubro_clave": "municipio",
            "prompt_context": "Municipio digital con reclamos, trámites y monitoreo en tiempo real.",
            "resources": [
                {
                    "title": "Guía de trámites express",
                    "type": "pdf",
                    "url": "/static/demo/municipio/guia-tramites-rapidos.pdf",
                },
                {
                    "title": "Plan de iluminación inteligente",
                    "type": "pdf",
                    "url": "/static/demo/municipio/plan-iluminacion-inteligente.pdf",
                },
            ],
        },
    ]


class DemoOfflineResponsesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(OfflineDemoConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Rubros (reusar si ya existen para evitar duplicados al inicializar demos)
        self.rubro_municipio = Rubro.query.filter_by(clave="municipio").first()
        self.rubro_bodega = Rubro.query.filter_by(clave="bodega").first()
        self.rubro_almacen = Rubro.query.filter_by(clave="almacen").first()

        nuevos_rubros = []
        if not self.rubro_municipio:
            self.rubro_municipio = Rubro(clave="municipio", nombre="Municipio", es_publico=True)
            nuevos_rubros.append(self.rubro_municipio)
        if not self.rubro_bodega:
            self.rubro_bodega = Rubro(clave="bodega", nombre="Bodega", es_publico=False)
            nuevos_rubros.append(self.rubro_bodega)
        if not self.rubro_almacen:
            self.rubro_almacen = Rubro(clave="almacen", nombre="Almacén", es_publico=False)
            nuevos_rubros.append(self.rubro_almacen)

        if nuevos_rubros:
            db.session.add_all(nuevos_rubros)
            db.session.commit()

        # Users associated to demos
        self.muni_user = User.query.filter_by(email="municipio@example.com").first()
        self.bodega_user = User.query.filter_by(email="bodega@example.com").first()
        self.almacen_user = User.query.filter_by(email="almacen@example.com").first()

        nuevos_users = []
        if not self.muni_user:
            self.muni_user = User(
                name="Municipio Demo",
                email="municipio@example.com",
                password_hash="hash",
                rubro=self.rubro_municipio,
                tipo_chat="municipio",
                rol="admin",
            )
            nuevos_users.append(self.muni_user)
        if not self.bodega_user:
            self.bodega_user = User(
                name="Bodega Demo",
                email="bodega@example.com",
                password_hash="hash",
                token="demo-bodega-token",
                rubro=self.rubro_bodega,
                tipo_chat="pyme",
                nombre_empresa="Bodega Cuatro Fincas",
            )
            nuevos_users.append(self.bodega_user)
        if not self.almacen_user:
            self.almacen_user = User(
                name="Almacén Demo",
                email="almacen@example.com",
                password_hash="hash",
                token="demo-almacen-token",
                rubro=self.rubro_almacen,
                tipo_chat="pyme",
                nombre_empresa="Almacén Inteligente",
            )
            nuevos_users.append(self.almacen_user)

        if nuevos_users:
            db.session.add_all(nuevos_users)
            db.session.commit()

        # FAQs for previews
        faq_muni = QA(
            question="¿Cómo registro un reclamo?",
            answer="Compartí ubicación y foto para generar el ticket.",
            rubro_id=self.rubro_municipio.id,
        )
        faq_bodega = QA(
            question="¿Tienen Gran Malbec Reserva?",
            answer="Sí, con nota a ciruela y 10% off en caja de 6.",
            rubro_id=self.rubro_bodega.id,
        )
        faq_almacen = QA(
            question="¿Arman presupuestos mayoristas?",
            answer="Sí, enviamos PDF con precios diferenciados y logística en el día.",
            rubro_id=self.rubro_almacen.id,
        )
        db.session.add_all([faq_muni, faq_bodega, faq_almacen])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_pyme_demo_catalog_response_is_offline(self):
        session_id = "offline-demo-pyme"
        headers = {"X-Chat-Session-Id": session_id}

        with self.client as client:
            # Start flow and select demo rubro
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)
            client.post(
                "/ask/pyme",
                json={"pregunta": {"action": "demo_select_rubro:bodega"}},
                headers=headers,
            )

            with patch(
                "services.pymes.llamar_llm_con_fallback",
                side_effect=AssertionError("LLM should not be called for demo"),
            ):
                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": "¿Tienen catálogo de precios?"},
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("fuente"), "demo_offline")
        self.assertIn("catálogo", data.get("message_body", "").lower())
        self.assertTrue(data.get("options_list"))

    def test_bodega_presupuesto_returns_pdf_resources(self):
        session_id = "offline-demo-bodega-presupuesto"
        headers = {"X-Chat-Session-Id": session_id}

        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)
            client.post(
                "/ask/pyme",
                json={"pregunta": {"action": "demo_select_rubro:bodega"}},
                headers=headers,
            )

            with patch(
                "services.pymes.llamar_llm_con_fallback",
                side_effect=AssertionError("LLM should not be called for demo"),
            ):
                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": "Necesito un presupuesto corporativo en PDF"},
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("fuente"), "demo_offline")
        self.assertIn("presupuesto", data.get("message_body", "").lower())
        attachments = data.get("adjuntos") or []
        self.assertTrue(any(att.get("type") == "pdf" for att in attachments))
        options = data.get("options_list") or []
        self.assertTrue(any("corpor" in (opt.get("texto") or "").lower() for opt in options))

    def test_demo_session_does_not_consume_plan_limit(self):
        session_id = "offline-demo-plan-limit"
        headers = {"X-Chat-Session-Id": session_id}

        self.bodega_user.limite_preguntas = 1
        self.bodega_user.preguntas_usadas = 1
        db.session.commit()

        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)
            client.post(
                "/ask/pyme",
                json={"pregunta": {"action": "demo_select_rubro:bodega"}},
                headers=headers,
            )

            with patch(
                "services.pymes.llamar_llm_con_fallback",
                side_effect=AssertionError("LLM should not be called for demo plan limit"),
            ):
                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": "Necesito un presupuesto rápido"},
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.bodega_user)
        self.assertEqual(self.bodega_user.preguntas_usadas, 1)

    def test_demo_token_auto_enables_offline_flow(self):
        session_id = "offline-demo-auto-token"
        headers = {"X-Chat-Session-Id": session_id, "X-Token": "demo-bodega-token"}

        with self.client as client:
            init_response = client.post(
                "/ask/pyme",
                json={"pregunta": "__INIT__"},
                headers=headers,
            )
            self.assertEqual(init_response.status_code, 200)

            with patch(
                "services.pymes.llamar_llm_con_fallback",
                side_effect=AssertionError("LLM should not be called for auto demo token"),
            ):
                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": "Necesito lista de precios"},
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("fuente"), "demo_offline")
        self.assertIn("lista de precios", data.get("message_body", "").lower())

    def test_almacen_presupuesto_includes_demo_resources(self):
        session_id = "offline-demo-almacen"
        headers = {"X-Chat-Session-Id": session_id}

        with self.client as client:
            client.post("/ask/pyme", json={"pregunta": "__INIT__"}, headers=headers)
            client.post(
                "/ask/pyme",
                json={"pregunta": {"action": "demo_select_rubro:almacen"}},
                headers=headers,
            )

            with patch(
                "services.pymes.llamar_llm_con_fallback",
                side_effect=AssertionError("LLM should not be called for demo"),
            ):
                response = client.post(
                    "/ask/pyme",
                    json={"pregunta": "Necesito un presupuesto mayorista en PDF"},
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("fuente"), "demo_offline")
        self.assertIn("presupuesto", data.get("message_body", "").lower())
        attachments = data.get("adjuntos") or []
        self.assertTrue(any(att.get("type") == "pdf" for att in attachments))
        options = data.get("options_list") or []
        self.assertTrue(any("combos" in (opt.get("texto") or "").lower() for opt in options))

    def test_municipio_demo_reclamo_uses_offline_script(self):
        # Prepare demo metadata directly from registry
        demo_entries = load_demo_rubros()
        municipio_demo = None
        for entry in demo_entries:
            if entry.key == "municipio":
                municipio_demo = entry.to_internal_dict()
                break
        self.assertIsNotNone(municipio_demo)

        chat_context = ChatSessionContext(chat_session_id="demo-muni", context_data={})
        db.session.add(chat_context)
        db.session.commit()

        with patch(
            "services.municipio_responder.llamar_llm_con_fallback",
            side_effect=AssertionError("LLM should not be called for demo"),
        ):
            response = responder_chatboc(
                pregunta="Quiero hacer un reclamo por luminaria rota",
                owner_user=self.muni_user,
                current_user=None,
                rubro_obj=self.rubro_municipio,
                chat_db_context=chat_context,
                tipo_chat="municipio",
                chat_session_uuid="demo-muni",
                demo_metadata=municipio_demo,
            )

        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("fuente"), "demo_offline")
        self.assertIn("luminaria", response.get("message_body", "").lower())
        self.assertTrue(response.get("botones"))

    def test_fallback_message_guides_to_buttons(self):
        demo_entries = load_demo_rubros()
        bodega_demo = None
        for entry in demo_entries:
            if entry.key == "bodega":
                bodega_demo = entry.to_internal_dict()
                break
        self.assertIsNotNone(bodega_demo)

        chat_context = ChatSessionContext(chat_session_id="demo-bodega", context_data={})
        db.session.add(chat_context)
        db.session.commit()

        with patch(
            "services.pymes.llamar_llm_con_fallback",
            side_effect=AssertionError("LLM should not be called for demo"),
        ):
            response = responder_chatboc(
                pregunta="Tema que no conozco",
                owner_user=self.bodega_user,
                current_user=None,
                rubro_obj=self.rubro_bodega,
                chat_db_context=chat_context,
                tipo_chat="pyme",
                chat_session_uuid="demo-bodega",
                demo_metadata=bodega_demo,
            )

        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("fuente"), "demo_offline")
        self.assertIn("demo", response.get("message_body", "").lower())
        self.assertGreaterEqual(len(response.get("options_list", [])), 1)


if __name__ == "__main__":
    unittest.main()
