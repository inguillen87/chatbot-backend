import unittest
from unittest.mock import patch

from app import create_app, db
from config import TestConfig
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import (
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    MUNICIPIO_RESPONSE_CACHE,
)


class ImageLocationDescriptionFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        rubro = Rubro(nombre="municipio", clave="municipio")
        self.owner_user = User(
            name="Test Municipio",
            email="muni@test.com",
            password_hash="x",
            municipio_id=1,
            rubro=rubro,
        )

        self.chat_ctx = ChatSessionContext(
            chat_session_id="sess-image-loc-desc",
            user_id=1,
            anon_id="anon",
        )
        self.chat_ctx.context_data = {}

        db.session.add_all([rubro, self.owner_user, self.chat_ctx])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch("services.municipio_responder.analizar_imagen_con_fallback", return_value=None)
    @patch("services.herramientas_municipio.obtener_direccion_de_coordenadas")
    @patch(
        "services.municipio_responder.extract_reclamo_details_from_text",
        return_value={
            "categoria_sugerida": "Arreglo de calle",
            "descripcion_sugerida": "bache grande",
        },
    )
    @patch(
        "services.municipio_responder.llamar_gemini",
        return_value=(
            {
                "message_body": "Contame qué necesitás hacer con la foto.",
                "accion_backend": "no_accion",
                "datos_estructura": {},
                "pedir_info": None,
                "botones": [],
            },
            {},
        ),
    )
    def test_image_location_description_single_ticket(
        self, _mock_llm, _mock_extract, mock_reverse, _mock_analyze
    ):
        mock_reverse.return_value = {
            "formatted_address": "Calle 123",
            "localidad": "Ciudad",
        }

        # 1. User sends an image
        responder_municipio(
            pregunta_original={"es_foto": True, "foto_url": "http://img.test/foto.jpg"},
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            chat_db_context=self.chat_ctx,
            anon_id="anon",
            channel="whatsapp",
        )
        # The responder must persist the pending photo itself.
        MUNICIPIO_RESPONSE_CACHE.clear()

        # 2. User shares a location
        responder_municipio(
            pregunta_original={
                "es_ubicacion": True,
                "ubicacion_usuario": {
                    "latitude": -33.0,
                    "longitude": -68.0,
                    "address": "Calle 123",
                },
            },
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            chat_db_context=self.chat_ctx,
            anon_id="anon",
            channel="whatsapp",
        )
        MUNICIPIO_RESPONSE_CACHE.clear()

        # 3. User confirms starting a claim for that location
        responder_municipio(
            pregunta_original={"action": "iniciar_reclamo_con_ubicacion"},
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            chat_db_context=self.chat_ctx,
            anon_id="anon",
            channel="whatsapp",
        )
        MUNICIPIO_RESPONSE_CACHE.clear()

        # 4. Finally, user provides a description
        responder_municipio(
            pregunta_original="hay un bache grande",
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            chat_db_context=self.chat_ctx,
            anon_id="anon",
            channel="whatsapp",
        )
        MUNICIPIO_RESPONSE_CACHE.clear()

        flow = (
            self.chat_ctx.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]
        )
        datos = flow["datos_reclamo"]

        self.assertEqual(datos.get("foto_url"), "http://img.test/foto.jpg")
        self.assertEqual(datos.get("direccion"), "Calle 123")
        self.assertEqual(datos.get("descripcion"), "bache grande")


if __name__ == "__main__":
    unittest.main()
