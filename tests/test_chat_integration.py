import sys
import os

# Add project root to sys.path to ensure modules like 'models.py' are findable
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import unittest
import json
from unittest.mock import patch, MagicMock, ANY


from app import create_app, Config, db
from models import User
from src.models import ArchivoAdjunto, ChatSessionContext, User
from src.models import AnalisisArchivo



class TestChatIntegration:

    def setup_method(self, method):
        self.app = create_app('config.TestConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def teardown_method(self, method):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.app_context.pop()

    # Mock para la tarea Celery y servicios externos
    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') # Corrected mock target
    # boto3_client mock removed as it's not used by archivo_service.py directly in the tested flow.
    def test_chat_con_imagen_reclamo_exitoso(self, mock_interpretar_imagen_para_chat, test_user): # Corrected mock argument name
        rubro = Rubro(nombre="municipio", es_publico=True)
        test_user = User(
            name="Test User",
            email="test@example.com",
            password="password",
            rubro=rubro,
            tipo_chat="municipio",
        )
        db.session.add(rubro)
        db.session.add(test_user)
        db.session.commit()
        # 1. Simular la subida de un archivo (endpoint /archivos/subir)
        # Esto crea ArchivoAdjunto y dispara la tarea Celery (que se ejecutará síncrono)

        # Configurar el mock de `interpretar_imagen_para_chat` para que devuelva un análisis exitoso
        # El formato de retorno de interpretar_imagen_para_chat es un poco diferente,
        # incluye 'categoria_sugerida' y 'descripcion_sugerida' directamente.
        mock_interpretar_imagen_para_chat.return_value = {
            'es_reclamo': True,
            'categoria_sugerida': 'Alumbrado Público',
            'descripcion_sugerida': 'Parece una luminaria rota en la calle.',
            # 'ubicacion_sugerida': 'Calle Falsa 123 (inferido de imagen)', # No es un campo estándar de retorno
            'analisis_id': 1, # ID del AnalisisArchivo que se crearía/actualizaría
            'texto_ocr': 'Luz rota poste 123', # Ejemplo de OCR
            'analisis_interno': {
                'tipo_analisis_sugerido': 'reclamo_municipal', # o similar
                'vision_inferred_category': 'Alumbrado Público',
                'llm_complaint_extraction_from_image': {
                    'tipo_problema': 'Alumbrado Público',
                    'descripcion_problema': 'Parece una luminaria rota en la calle.'
                }
            }
        }

        # Simular la subida del archivo primero para tener un ID de ArchivoAdjunto
        # Necesitamos un endpoint que suba y devuelva el ID. Asumimos que /archivos/subir lo hace.
        # Para esta prueba, vamos a crear el ArchivoAdjunto y AnalisisArchivo manualmente
        # y luego llamar a la tarea Celery directamente (ya que está en modo eager).

        archivo_adj = ArchivoAdjunto(
            user_id=test_user.id,
            filename="test_luminaria.jpg",
            nombre_original="luminaria.jpg",
            url="/archivos/test_luminaria.jpg", # Simular URL local
            mime="image/jpeg",
            tamano=12345,
            tipo="chat" # Tipo genérico de chat
        )
        db.session.add(archivo_adj)
        db.session.commit()
        archivo_id = archivo_adj.id

        # Llamar a la tarea Celery directamente (gracias a CELERY_TASK_ALWAYS_EAGER = True)
        # Necesitamos importar la tarea
        from services.analisis_archivo_service import tarea_analizar_contenido_archivo

        # La tarea espera el ID del archivo adjunto.
        # Dentro de la tarea, se llamará a `interpretar_imagen_reclamo` (que está mockeada).
        # La tarea actualizará el `AnalisisArchivo` en la BD.
        tarea_analizar_contenido_archivo.delay(archivo_id) # .delay() se ejecuta síncrono

        # Verificar que el AnalisisArchivo fue creado y actualizado por la tarea (vía el mock)
        analisis_obj = AnalisisArchivo.query.filter_by(archivo_adjunto_id=archivo_id).first()
        assert analisis_obj is not None
        # El estado y tipo deberían ser seteados por `interpretar_imagen_reclamo`
        # Si el mock de `interpretar_imagen_reclamo` no actualiza la BD,
        # necesitamos asegurar que la tarea sí lo haga con el resultado del mock.
        # En la implementación actual de `interpretar_imagen_reclamo`, esta SI actualiza y hace commit.
        assert analisis_obj.estado_analisis == "completado"
        assert analisis_obj.tipo_analisis == "reclamo_municipal" # Updated to match mock
        assert json.loads(analisis_obj.datos_estructurados).get("llm_complaint_extraction_from_image").get("tipo_problema") == "Alumbrado Público"


        # 2. Enviar mensaje al chat con uploaded_file_info
        chat_payload = {
            "pregunta": "Adjunté una foto de un problema.", # El texto puede ser genérico
            "tipo_chat": "municipio", # Asegurar contexto municipal
            "rubro_clave": "municipio_general", # O rubro_id
            "uploaded_file_info": {
                "id": archivo_id,
                "name": "luminaria.jpg", # Nombre original
                "url": "/archivos/test_luminaria.jpg" # URL que el frontend podría tener
            }
        }

        response = client.post('/ask', json=chat_payload, headers={'Authorization': f'Bearer {test_user.token}'})
        assert response.status_code == 200
        data = response.get_json()

        # Verificar la respuesta del bot (debería pedir confirmación)
        assert "He analizado la imagen que subiste." in data["respuesta"]
        assert "Parece ser un problema de 'Alumbrado Público'" in data["respuesta"]
        assert "Parece una luminaria rota en la calle." in data["respuesta"]
        assert "¿Es esto correcto?" in data["respuesta"]

        assert any(b["texto"] == "Sí, es correcto" for b in data.get("botones", []))
        assert any(b["texto"] == "No, quiero describirlo yo" for b in data.get("botones", []))

        # Verificar que el contexto se actualizó para esperar confirmación
        contexto_actualizado = data.get("contexto_actualizado", {}).get("contexto_municipio", {})
        assert contexto_actualizado.get("estado_conversacion") == "ESPERANDO_CONFIRMACION_RECLAMO_IMAGEN"
        assert contexto_actualizado.get("tipo_sugerido_imagen") == "Alumbrado Público"
        assert contexto_actualizado.get("archivo_id_reclamo_actual") == archivo_id


    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') # Corrected mock target
    @patch('services.email_service.enviar_whatsapp_ticket_novedad') # Corrected mock target
    @patch('services.email_service.enviar_sms_ticket_novedad') # Corrected mock target
    def test_chat_con_imagen_reclamo_confirmacion_si_y_creacion_ticket(self, mock_sms_novedad, mock_whatsapp_novedad, mock_interpretar_imagen_para_chat): # Corrected mock argument names
        # --- Parte 1: Subida y primer análisis (similar al test anterior) ---
        archivo_id = 10

        mock_interpretar_imagen_para_chat.return_value = { # Corrected mock configuration
            'es_reclamo': True, 'categoria_sugerida': 'Bacheo',
            'descripcion_sugerida': 'Parece un bache grande.', 'analisis_id': archivo_id,
            'texto_ocr': 'Bache peligroso',
            'analisis_interno': {
                'tipo_analisis_sugerido': 'reclamo_municipal',
                'vision_inferred_category': 'Bacheo',
                'llm_complaint_extraction_from_image': {
                    'tipo_problema': 'Bacheo',
                    'descripcion_problema': 'Parece un bache grande.'
                }
            }
        }

        archivo_adj = ArchivoAdjunto(id=archivo_id, user_id=self.test_user.id, filename="bache_test.jpg", url="/archivos/bache_test.jpg", mime="image/jpeg", tamano=123)
        db.session.add(archivo_adj)
        db.session.commit()

        from services.analisis_archivo_service import tarea_analizar_contenido_archivo
        tarea_analizar_contenido_archivo.delay(archivo_id) # Ejecución síncrona

        chat_payload_inicial = {
            "pregunta": "Miren esta foto.", "tipo_chat": "municipio", "rubro_clave": "municipio_general",
            "uploaded_file_info": {"id": archivo_id, "name": "bache_test.jpg", "url": "/archivos/bache_test.jpg"}
        }
        response_inicial = self.client.post('/ask', json=chat_payload_inicial, headers=self.auth_headers)
        self.assertEqual(response_inicial.status_code, 200)
        data_inicial = response_inicial.get_json()
        contexto_paso1 = data_inicial.get("contexto_actualizado", {}).get("contexto_municipio", {})

        self.assertEqual(contexto_paso1.get("estado_conversacion"), "ESPERANDO_CONFIRMACION_RECLAMO_IMAGEN")
        self.assertEqual(contexto_paso1.get("tipo_sugerido_imagen"), "Bacheo")
        self.assertEqual(contexto_paso1.get("archivo_id_reclamo_actual"), archivo_id)


        # --- Parte 2: Usuario confirma los datos de la imagen ---
        chat_payload_confirmacion = {
            "pregunta": "Sí, es correcto", # O podría ser una acción de botón
            "action": "confirmar_datos_imagen", # Simular clic en botón
            "tipo_chat": "municipio", "rubro_clave": "municipio_general",
            "contexto_previo": data_inicial.get("contexto_actualizado") # Pasar contexto anterior
        }
        response_confirmacion = self.client.post('/ask', json=chat_payload_confirmacion, headers=self.auth_headers)
        self.assertEqual(response_confirmacion.status_code, 200)
        data_confirmacion = response_confirmacion.get_json()
        contexto_paso2 = data_confirmacion.get("contexto_actualizado", {}).get("contexto_municipio", {})

        # Verificar que ahora pide la dirección (ya que no se infirió de la imagen en este mock)
        self.assertIn("dirección exacta del problema", data_confirmacion["respuesta"])
        self.assertEqual(contexto_paso2.get("estado_conversacion"), "ESPERANDO_DIRECCION_RECLAMO")
        self.assertEqual(contexto_paso2.get("categoria_reclamo"), "Bacheo") # Categoría confirmada
        self.assertEqual(contexto_paso2.get("descripcion_reclamo"), "Parece un bache grande.") # Descripción confirmada


        # --- Parte 3: Usuario proporciona la dirección ---
        chat_payload_direccion = {
            "pregunta": "Calle Falsa 123, Ciudad Test",
            "tipo_chat": "municipio", "rubro_clave": "municipio_general",
            "contexto_previo": data_confirmacion.get("contexto_actualizado")
        }
        response_direccion = self.client.post('/ask', json=chat_payload_direccion, headers=self.auth_headers)
        self.assertEqual(response_direccion.status_code, 200)
        data_direccion = response_direccion.get_json()
        contexto_paso3 = data_direccion.get("contexto_actualizado", {}).get("contexto_municipio", {})

        self.assertIn("nombre completo", data_direccion["respuesta"]) # Ahora pide el nombre
        self.assertEqual(contexto_paso3.get("estado_conversacion"), "ESPERANDO_NOMBRE_VECINO")
        self.assertEqual(contexto_paso3.get("direccion_reclamo"), "Calle Falsa 123, Ciudad Test")

        # --- Continuar simulando el resto de los datos: nombre, teléfono, email ---
        # (Asumimos que la descripción ya la tenemos de la imagen)

        # Nombre
        payload_nombre = {"pregunta": "Juan Perez", "tipo_chat": "municipio", "contexto_previo": data_direccion.get("contexto_actualizado")}
        resp_nombre = self.client.post('/ask', json=payload_nombre, headers=self.auth_headers).get_json()
        self.assertIn("número de teléfono", resp_nombre["respuesta"])

        # Teléfono
        payload_tel = {"pregunta": "2615551234", "tipo_chat": "municipio", "contexto_previo": resp_nombre.get("contexto_actualizado")}
        resp_tel = self.client.post('/ask', json=payload_tel, headers=self.auth_headers).get_json()
        self.assertIn("correo electrónico", resp_tel["respuesta"])

        # Email (y luego debería pedir adjuntos o confirmación final)
        payload_email = {"pregunta": "juan@example.com", "tipo_chat": "municipio", "contexto_previo": resp_tel.get("contexto_actualizado")}
        resp_email = self.client.post('/ask', json=payload_email, headers=self.auth_headers).get_json()
        self.assertIn("adjuntar una foto o compartir tu ubicación", resp_email["respuesta"]) # Pide adjuntos
        contexto_adjuntos = resp_email.get("contexto_actualizado", {}).get("contexto_municipio", {})
        self.assertEqual(contexto_adjuntos.get("estado_conversacion"), "ESPERANDO_ADJUNTOS_RECLAMO")

        # --- Parte 4: Usuario dice que no quiere adjuntos y confirma el reclamo ---
        payload_sin_adjuntos = {"pregunta": "No, continuar", "action": "sin_adjuntos", "tipo_chat": "municipio", "contexto_previo": resp_email.get("contexto_actualizado")}
        resp_sin_adjuntos = self.client.post('/ask', json=payload_sin_adjuntos, headers=self.auth_headers).get_json()
        self.assertIn("revisá si todos los datos son correctos", resp_sin_adjuntos["respuesta"])
        contexto_confirmacion_final = resp_sin_adjuntos.get("contexto_actualizado", {}).get("contexto_municipio", {})
        self.assertEqual(contexto_confirmacion_final.get("estado_conversacion"), "ESPERANDO_CONFIRMACION_RECLAMO")

        # Confirmar reclamo
        payload_confirmar_final = {"pregunta": "Sí, confirmar reclamo", "action": "confirmar_reclamo", "tipo_chat": "municipio", "contexto_previo": resp_sin_adjuntos.get("contexto_actualizado")}
        resp_confirmar_final = self.client.post('/ask', json=payload_confirmar_final, headers=self.auth_headers).get_json()

        self.assertEqual(resp_confirmar_final.get("ticket_id"), 1) # Asumiendo que es el primer ticket creado en esta prueba
        self.assertIn("Tu reclamo ha sido registrado con el número de ticket: **M-1**", resp_confirmar_final["respuesta"])

        # Verificar en la BD
        from models import MunicipioTicket
        ticket_creado = db.session.get(MunicipioTicket, 1) # Usar el ID del ticket devuelto
        self.assertIsNotNone(ticket_creado)
        self.assertEqual(ticket_creado.categoria, "Bacheo")
        self.assertEqual(ticket_creado.detalles, "Parece un bache grande.")
        self.assertEqual(ticket_creado.direccion, "Calle Falsa 123, Ciudad Test")
        self.assertEqual(ticket_creado.nombre_vecino, "Juan Perez") # Asumiendo que el modelo MunicipioTicket tiene este campo
        self.assertEqual(ticket_creado.telefono_vecino, "2615551234") # Asumiendo que el modelo MunicipioTicket tiene este campo
        self.assertEqual(ticket_creado.email, "juan@example.com") # Asumiendo que el modelo MunicipioTicket tiene este campo

        # Verificar que el archivo está asociado
        self.assertIsNotNone(ticket_creado.archivos)
        archivos_asociados = [a.id for a in ticket_creado.archivos]
        self.assertIn(archivo_id, archivos_asociados)

        # Verificar que los mocks de notificación fueron llamados (opcional, pero bueno para cobertura)
        # mock_whatsapp.assert_called_once()
        # mock_sms.assert_called_once()

    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') # Corrected mock target
    def test_chat_con_imagen_reclamo_confirmacion_no(self, mock_interpretar_imagen_para_chat, client, test_user):
        archivo_id = 20
        mock_interpretar_imagen_para_chat.return_value = { # Corrected mock configuration
            'es_reclamo': True, 'categoria_sugerida': 'Semáforo Roto',
            'descripcion_sugerida': 'El semáforo de la esquina no funciona.', 'analisis_id': archivo_id,
            'texto_ocr': 'Semáforo apagado',
            'analisis_interno': {
                'tipo_analisis_sugerido': 'reclamo_municipal',
                 'vision_inferred_category': 'Semáforo Roto',
                'llm_complaint_extraction_from_image': {
                    'tipo_problema': 'Semáforo Roto',
                    'descripcion_problema': 'El semáforo de la esquina no funciona.'
                }
            }
        }
        archivo_adj = ArchivoAdjunto(id=archivo_id, user_id=test_user.id, filename="semaforo_roto.jpg", url="/archivos/semaforo.jpg", mime="image/jpeg")
        db.session.add(archivo_adj); db.session.commit()
        from services.analisis_archivo_service import tarea_analizar_contenido_archivo
        tarea_analizar_contenido_archivo.delay(archivo_id)

        chat_payload_inicial = {"pregunta": "Foto de semáforo", "tipo_chat": "municipio", "rubro_clave": "municipio_general", "uploaded_file_info": {"id": archivo_id}}
        response_inicial = client.post('/ask', json=chat_payload_inicial, headers={'Authorization': f'Bearer {test_user.token}'})
        data_inicial = response_inicial.get_json()

        # Usuario responde "No, quiero describirlo yo"
        chat_payload_no = {
            "pregunta": "No, quiero describirlo yo", "action": "describir_manual_reclamo",
            "tipo_chat": "municipio", "rubro_clave": "municipio_general",
            "contexto_previo": data_inicial.get("contexto_actualizado")
        }
        response_no = client.post('/ask', json=chat_payload_no, headers={'Authorization': f'Bearer {test_user.token}'})
        assert response_no.status_code == 200
        data_no = response_no.get_json()

        assert "¿Sobre qué categoría es tu reclamo?" in data_no["respuesta"]
        contexto_manual = data_no.get("contexto_actualizado", {}).get("contexto_municipio", {})
        assert contexto_manual.get("estado_conversacion") == "ESPERANDO_CATEGORIA_RECLAMO"
        assert contexto_manual.get("tipo_sugerido_imagen") is None # Debe haberse limpiado
        assert contexto_manual.get("categoria_sugerida") is None # Ensure this is also cleared

    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') # Corrected mock target
    def test_chat_analisis_imagen_en_progreso(self, mock_interpretar_imagen_para_chat): # Corrected mock argument
        archivo_id = 30
        archivo_adj = ArchivoAdjunto(id=archivo_id, user_id=self.test_user.id, filename="procesando.jpg", url="/archivos/procesando.jpg", mime="image/jpeg")
        analisis = AnalisisArchivo(archivo_adjunto_id=archivo_id, estado_analisis="procesando", tipo_analisis="reclamo_vision_v1") # Simular estado
        db.session.add_all([archivo_adj, analisis]); db.session.commit()

        # No llamamos a la tarea Celery, ya que el estado ya está "procesando"

        chat_payload = {"pregunta": "Subí una imagen", "tipo_chat": "municipio", "rubro_clave": "municipio_general", "uploaded_file_info": {"id": archivo_id}}
        response = self.client.post('/ask', json=chat_payload, headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("Estoy analizando el archivo que subiste.", data["respuesta"])
        self.assertEqual(data.get("tipo_respuesta"), "espera_analisis_archivo")

    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') # Corrected mock target
    def test_chat_analisis_imagen_error(self, mock_interpretar_imagen_para_chat): # Corrected mock argument
        archivo_id = 40
        archivo_adj = ArchivoAdjunto(id=archivo_id, user_id=self.test_user.id, filename="error_analisis.jpg", url="/archivos/error.jpg", mime="image/jpeg")
        analisis = AnalisisArchivo(archivo_adjunto_id=archivo_id, estado_analisis="error", tipo_analisis="reclamo_vision_llm_v1", error_analisis="Falla simulada de Vision")
        db.session.add_all([archivo_adj, analisis]); db.session.commit()

        chat_payload = {"pregunta": "Imagen con error", "tipo_chat": "municipio", "rubro_clave": "municipio_general", "uploaded_file_info": {"id": archivo_id}}
        response = self.client.post('/ask', json=chat_payload, headers=self.auth_headers)
        self.assertEqual(response.status_code, 200) # La API de chat responde 200, pero el contenido indica el error
        data = response.get_json()
        self.assertIn("Hubo un problema al analizar el archivo: Falla simulada de Vision", data["respuesta"])
        self.assertEqual(data.get("tipo_respuesta"), "error_analisis_archivo")

    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') # Corrected mock target
    def test_chat_analisis_imagen_no_es_reclamo(self, mock_interpretar_imagen_para_chat, client, test_user): # Corrected mock argument
        archivo_id = 50
        # Configurar el mock para que devuelva que no es un reclamo y un _mensaje_bot
        mock_interpretar_imagen_para_chat.return_value = { # Corrected mock configuration
            'es_reclamo': False,
            '_mensaje_bot': "La imagen que subiste no parece ser un reclamo. ¿Podrías describir el problema?", # This field might not be used; response comes from 'motivo' or structure
            'motivo': "La imagen que subiste no parece ser un reclamo. ¿Podrías describir el problema?", # More likely field
            'categoria_sugerida': None,
            'descripcion_sugerida': None,
            'analisis_id': archivo_id,
            'texto_ocr': 'Imagen de un gato.',
            'analisis_interno': {
                'tipo_analisis_sugerido': 'imagen_general_vision_v1'
            }
        }
        archivo_adj = ArchivoAdjunto(id=archivo_id, user_id=test_user.id, filename="no_reclamo.jpg", url="/archivos/no_reclamo.jpg", mime="image/jpeg")
        db.session.add(archivo_adj); db.session.commit()

        from services.analisis_archivo_service import tarea_analizar_contenido_archivo
        tarea_analizar_contenido_archivo.delay(archivo_id) # Esto llamará al mock

        # Verificar que el AnalisisArchivo refleje que no es un reclamo claro
        analisis_obj = AnalisisArchivo.query.filter_by(archivo_adjunto_id=archivo_id).first()
        assert analisis_obj is not None
        # `interpretar_imagen_reclamo` setea el estado a 'completado' y el tipo según el resultado
        # Si es_reclamo es False y hay motivo, el tipo podría ser 'imagen_general_vision_v1'
        # assert analisis_obj.tipo_analisis == "imagen_general_vision_v1"


        chat_payload = {"pregunta": "Esta foto no es un reclamo", "tipo_chat": "municipio", "rubro_clave": "municipio_general", "uploaded_file_info": {"id": archivo_id}}
        response = client.post('/ask', json=chat_payload, headers={'Authorization': f'Bearer {test_user.token}'})
        assert response.status_code == 200
        data = response.get_json()

        # Esta verificación depende de cómo `responder_municipio` usa el `_mensaje_bot`
        # Asumimos que `responder_municipio` ahora usa `_mensaje_bot` directamente si está presente.
        assert "La imagen que subiste no parece ser un reclamo." in data["respuesta"]
        assert data.get("contexto_actualizado", {}).get("contexto_municipio", {}).get("estado_conversacion") is None # No debería iniciar flujo de reclamo


