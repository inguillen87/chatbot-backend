import sys
import os

# Add project root to sys.path to ensure modules like 'models.py' are findable
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import unittest
from unittest.mock import patch, MagicMock, ANY


from services.interpretacion_imagen_service import interpretar_imagen_para_chat, _descargar_imagen
import models
from models import db, User, ArchivoAdjunto, AnalisisArchivo

# --- Configuración de App Flask para Pruebas con BD en Memoria ---
from flask import Flask

# Necesitamos crear una app Flask mínima para inicializar SQLAlchemy para pruebas
# Esto es crucial si las funciones bajo prueba interactúan con db.session
# Como `interpretar_imagen_reclamo` lo hace.

def create_test_app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    # Usar SQLite en memoria para pruebas rápidas y aisladas
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app

# --------------------------------------------------------------------

class TestInterpretacionImagenService(unittest.TestCase):

    def setUp(self):
        """Configura una app Flask y una base de datos en memoria para cada prueba."""
        self.app = create_test_app()
        self.app_context = self.app.app_context()
        self.app_context.push() # Activa el contexto de la aplicación
        db.create_all() # Crea todas las tablas en la BD en memoria

        # Crear un usuario de prueba si es necesario para las funciones
        self.test_user = User(name="Test User", email="test@example.com", password_hash="test")
        db.session.add(self.test_user)
        db.session.commit()


    def tearDown(self):
        """Limpia la base de datos y el contexto de la aplicación después de cada prueba."""
        db.session.remove()
        db.drop_all()
        self.app_context.pop() # Desactiva el contexto de la aplicación

    @patch('services.interpretacion_imagen_service.requests.get')
    @patch('os.path.exists', return_value=False)
    def test_descargar_imagen_fallback_public_base(self, mock_exists, mock_get):
        self.app.config['APP_PUBLIC_BASE_URL'] = 'https://cdn.example.com'
        self.app.config['TWILIO_ACCOUNT_SID'] = 'sid'
        self.app.config['TWILIO_AUTH_TOKEN'] = 'token'
        mock_get.return_value = MagicMock(content=b'data', raise_for_status=lambda: None)
        content = _descargar_imagen('/static/uploads/img.jpg')
        mock_get.assert_called_with('https://cdn.example.com/static/uploads/img.jpg', auth=('sid','token'), timeout=10)
        self.assertEqual(content, b'data')

    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_smart')
    @patch('services.interpretacion_imagen_service.extract_complaint_details_llm')
    def test_interpretar_imagen_reclamo_exito_total(self, mock_extract_llm, mock_analyze_vision, mock_descargar):
        # --- Configuración de Mocks ---
        mock_descargar.return_value = b"fake_image_bytes_downloaded"

        mock_analyze_vision.return_value = {
            "objects": [{"name": "traffic light", "confidence": 0.9}],
            "labels": [{"description": "street", "confidence": 0.8}],
            "full_text_annotation": {"description": "AYUDA SEMAFORO CAIDO"}
        }

        mock_extract_llm.return_value = {
            "tipo_problema": "Rotura de semaforo",
            "descripcion_problema": "Un semáforo parece estar caído y dañado.",
            "ubicacion_problema": "Inferido de texto: AYUDA SEMAFORO CAIDO"
        }

        # --- Datos de Prueba ---
        archivo_adjunto = ArchivoAdjunto(
            id=1, # ID explícito para que AnalisisArchivo pueda referenciarlo
            user_id=self.test_user.id,
            filename="test_semaforo.jpg",
            url="http://example.com/semaforo.jpg",
            mime="image/jpeg"
        )
        db.session.add(archivo_adjunto)
        db.session.commit() # Guardar para que la función lo encuentre si lo busca por ID

        # --- Ejecución ---
        # Call with tipo_interpretacion="reclamo_municipal" or "reclamo_auto_descripcion_categoria"
        resultado = interpretar_imagen_para_chat(archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None) # pyme_user is None for municipal claims

        # --- Verificaciones ---
        self.assertEqual(resultado.get("kind"), "image")
        self.assertEqual(resultado.get("categoria_sugerida"), "rotura de semaforo")
        self.assertIn("semáforo parece estar caído", resultado.get("descripcion_sugerida"))
        self.assertIsNotNone(resultado.get("analisis_id"))

        analisis_guardado = db.session.get(AnalisisArchivo, resultado["analisis_id"])
        self.assertIsNotNone(analisis_guardado)
        self.assertEqual(analisis_guardado.estado_analisis, "completado")
        self.assertEqual(analisis_guardado.tipo_analisis, "reclamo_vision_llm_v1")
        self.assertIn("traffic light", str(analisis_guardado.datos_estructurados))
        self.assertIn("llm_raw", analisis_guardado.datos_estructurados)
        self.assertEqual(analisis_guardado.texto_extraido, "AYUDA SEMAFORO CAIDO")

        mock_descargar.assert_called_once_with("http://example.com/semaforo.jpg")
        mock_analyze_vision.assert_called_once_with(b"fake_image_bytes_downloaded")
        # Verificar que el LLM fue llamado con una descripción que incluye info de Vision y OCR
        mock_extract_llm.assert_called_once()
        args_llm, _ = mock_extract_llm.call_args
        self.assertIn("Objetos principales detectados: traffic light", args_llm[0])
        self.assertIn("Aspectos generales de la imagen: street", args_llm[0])
        self.assertIn("Texto en imagen: 'AYUDA SEMAFORO CAIDO'", args_llm[0])


    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_smart')
    @patch('services.interpretacion_imagen_service.extract_complaint_details_llm')
    def test_interpretar_imagen_reclamo_sin_keywords_ni_texto_ocr(self, mock_extract_llm, mock_analyze_vision, mock_descargar):
        mock_descargar.return_value = b"imagen_sin_nada_relevante"
        mock_analyze_vision.return_value = {
            "objects": [{"name": "sky", "confidence": 0.9}],
            "labels": [{"description": "blue", "confidence": 0.8}],
            "text_annotations": []
        }
        mock_extract_llm.return_value = {"tipo_problema": "", "descripcion_problema": ""}
        archivo_adjunto = ArchivoAdjunto(id=2, user_id=self.test_user.id, filename="cielo.jpg", url="http://example.com/cielo.jpg", mime="image/jpeg")
        db.session.add(archivo_adjunto)
        db.session.commit()

        resultado = interpretar_imagen_para_chat(archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None)

        self.assertIsNone(resultado.get("categoria_sugerida"))
        self.assertIn("El análisis por IA no pudo confirmar un reclamo específico", resultado.get("motivo", ""))
        analisis_guardado = db.session.get(AnalisisArchivo, resultado["analisis_id"])
        self.assertEqual(analisis_guardado.estado_analisis, "completado")
        self.assertEqual(analisis_guardado.tipo_analisis, "reclamo_vision_llm_v1")


    @patch('services.interpretacion_imagen_service._descargar_imagen')
    def test_interpretar_imagen_falla_descarga(self, mock_descargar):
        mock_descargar.return_value = None # Simula fallo de descarga
        archivo_adjunto = ArchivoAdjunto(id=3, user_id=self.test_user.id, filename="error.jpg", url="http://example.com/error.jpg", mime="image/jpeg")
        db.session.add(archivo_adjunto)
        db.session.commit()

        resultado = interpretar_imagen_para_chat(archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None)

        self.assertIsNone(resultado.get("categoria_sugerida"))
        self.assertEqual(resultado.get("error"), "Fallo al descargar la imagen.")
        analisis_guardado = db.session.get(AnalisisArchivo, resultado["analisis_id"])
        self.assertEqual(analisis_guardado.estado_analisis, "error")
        self.assertEqual(analisis_guardado.error_analisis, "Fallo al descargar la imagen.")

    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_smart')
    def test_interpretar_imagen_error_vision_api(self, mock_analyze_vision, mock_descargar):
        mock_descargar.return_value = b"bytes_imagen"
        mock_analyze_vision.return_value = {"error": "Error de Vision simulado"}
        archivo_adjunto = ArchivoAdjunto(id=4, user_id=self.test_user.id, filename="vision_error.jpg", url="http://example.com/vision_error.jpg", mime="image/jpeg")
        db.session.add(archivo_adjunto)
        db.session.commit()

        resultado = interpretar_imagen_para_chat(archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None)

        self.assertIsNone(resultado.get("categoria_sugerida"))
        self.assertIn("Error de Vision API: Error de Vision simulado", resultado.get("error", ""))
        analisis_guardado = db.session.get(AnalisisArchivo, resultado["analisis_id"])
        self.assertEqual(analisis_guardado.estado_analisis, "error")
        self.assertIn("Error de Vision API: Error de Vision simulado", analisis_guardado.error_analisis)

    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_smart')
    @patch('services.interpretacion_imagen_service.extract_complaint_details_llm')
    def test_interpretar_imagen_keywords_pero_llm_no_confirma(self, mock_extract_llm, mock_analyze_vision, mock_descargar):
        mock_descargar.return_value = b"imagen_ambigua"
        mock_analyze_vision.return_value = {
            "objects": [{"name": "socavon", "confidence": 0.7}],
            "labels": [], "text_annotations": []
        }
        mock_extract_llm.return_value = {
            "tipo_problema": "",
            "descripcion_problema": "",
        }

        archivo_adjunto = ArchivoAdjunto(id=5, user_id=self.test_user.id, filename="ambigua.jpg", url="http://example.com/ambigua.jpg", mime="image/jpeg")
        db.session.add(archivo_adjunto)
        db.session.commit()

        resultado = interpretar_imagen_para_chat(archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None)

        self.assertIsNotNone(resultado.get("categoria_sugerida"))
        self.assertEqual(resultado.get("categoria_sugerida"), "arreglo de calle")
        analisis_guardado = db.session.get(AnalisisArchivo, resultado["analisis_id"])
        self.assertEqual(analisis_guardado.estado_analisis, "completado")
        self.assertEqual(analisis_guardado.tipo_analisis, "reclamo_vision_llm_v1")

    @patch('services.interpretacion_imagen_service._descargar_imagen')
    def test_interpretar_imagen_reclamo_archivo_no_valido(self, mock_descargar):
        # Caso 1: archivo_adjunto es None
        resultado_none = interpretar_imagen_para_chat(None, tipo_interpretacion="reclamo_municipal", pyme_user=None)
        self.assertIsNone(resultado_none.get("categoria_sugerida"))
        self.assertEqual(resultado_none.get("error"), "Tipo de archivo_adjunto no válido.") # Updated error message

        # Caso 2: archivo_adjunto no tiene URL
        archivo_sin_url = ArchivoAdjunto(id=6, user_id=self.test_user.id, filename="sin_url.jpg", url="http://example.com/sin_url.jpg", mime="image/jpeg")
        db.session.add(archivo_sin_url)
        db.session.commit()
        mock_descargar.return_value = None
        resultado_sin_url = interpretar_imagen_para_chat(archivo_sin_url, tipo_interpretacion="reclamo_municipal", pyme_user=None)
        self.assertIsNone(resultado_sin_url.get("categoria_sugerida"))
        self.assertEqual(resultado_sin_url.get("error"), "Fallo al descargar la imagen.") # Updated error message
        # Verificar que no se creó un AnalisisArchivo innecesariamente
        analisis_para_sin_url = AnalisisArchivo.query.filter_by(archivo_adjunto_id=6).first()
        self.assertIsNotNone(analisis_para_sin_url)
        self.assertEqual(analisis_para_sin_url.estado_analisis, "error")


    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_smart')
    @patch('services.interpretacion_imagen_service.extract_complaint_details_llm')
    def test_interpretar_imagen_reclamo_analisis_existente(self, mock_extract_llm, mock_analyze_vision, mock_descargar):
        # --- Configuración de Mocks (similar al test de éxito) ---
        mock_descargar.return_value = b"fake_image_bytes_updated"
        mock_analyze_vision.return_value = {
            "objects": [{"name": "pothole", "confidence": 0.95}],
            "labels": [{"description": "road damage", "confidence": 0.9}],
            "text_annotations": []
        }
        mock_extract_llm.return_value = {
            "tipo_problema": "Arreglo de calle",
            "descripcion_problema": "Un bache peligroso en la calle.",
        }

        # --- Datos de Prueba ---
        archivo_adjunto = ArchivoAdjunto(
            id=7, user_id=self.test_user.id, filename="bache.jpg",
            url="http://example.com/bache.jpg", mime="image/jpeg"
        )
        db.session.add(archivo_adjunto)

        # Crear un AnalisisArchivo preexistente para este ArchivoAdjunto
        analisis_previo = AnalisisArchivo(
            archivo_adjunto_id=7,
            estado_analisis="pendiente", # O cualquier otro estado no final
            tipo_analisis="inicial",
            datos_estructurados={"info_previa": "algo"}
        )
        db.session.add(analisis_previo)
        db.session.commit()
        id_analisis_previo = analisis_previo.id

        # --- Ejecución ---
        resultado = interpretar_imagen_para_chat(archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None)

        # --- Verificaciones ---
        self.assertIsNotNone(resultado.get("categoria_sugerida"))
        self.assertEqual(resultado.get("categoria_sugerida"), "arreglo de calle")
        self.assertEqual(resultado.get("analisis_id"), id_analisis_previo)

        analisis_actualizado = db.session.get(AnalisisArchivo, id_analisis_previo)
        self.assertIsNotNone(analisis_actualizado)
        self.assertEqual(analisis_actualizado.estado_analisis, "completado")
        self.assertEqual(analisis_actualizado.tipo_analisis, "reclamo_vision_llm_v1")
        self.assertNotIn("info_previa", str(analisis_actualizado.datos_estructurados))
        self.assertIn("pothole", str(analisis_actualizado.datos_estructurados))
        self.assertIn("Arreglo de calle", str(analisis_actualizado.datos_estructurados['llm_raw']))


if __name__ == '__main__':
    unittest.main()
