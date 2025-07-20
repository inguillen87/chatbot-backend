from unittest.mock import patch, MagicMock
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.services.actions.municipio_actions import CrearReclamoActionHandler
from src.models import User
from src.extensions import db


def test_accion_crear_reclamo_exito_completo_llm(app):
    with app.app_context():
        with patch('src.services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
             patch('src.services.actions.municipio_actions.formatear_telefono_e164') as mock_formatear_telefono, \
             patch('src.services.actions.municipio_actions.validar_telefono') as mock_validar_telefono, \
             patch('src.services.actions.municipio_actions.validar_email') as mock_validar_email, \
             patch('src.services.actions.municipio_actions.parse_direccion_completa') as mock_parse_direccion, \
             patch('src.services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla') as mock_enviar_whatsapp:
            mock_ticket_simulado = MagicMock()
            mock_ticket_simulado.nro_ticket = "12345"
            mock_ticket_simulado.id = 1
            mock_crear_ticket.return_value = mock_ticket_simulado

            mock_validar_telefono.return_value = True
            mock_formatear_telefono.return_value = "+5491122334455"
            mock_validar_email.return_value = True

            mock_parse_direccion.return_value = {
                "calle": "Calle Falsa", "numero": "123", "localidad": "Springfield"
            }

            datos_llm = {
                "categoria": "Alumbrado", "descripcion": "Poste de luz caído y chispas.",
                "ubicacion": "Calle Falsa 123, Springfield",
                "coordenadas": {"lat": -32.8908, "lon": -68.8272},
                "usuario": "Homero Simpson", "telefono": "91122334455", "email": "homero@example.com"
            }

            mock_viewer_user = MagicMock(spec=User)
            mock_viewer_user.id = 100; mock_viewer_user.nombre = "Homero J. Simpson"
            mock_viewer_user.telefono = "2615550000"; mock_viewer_user.email = "hsimpson@springfield.com"

            mock_owner_user = MagicMock(spec=User)
            mock_owner_user.id = 1; mock_owner_user.municipio_id = "springfield_municipio"

            context = {
                "viewer_user_obj": mock_viewer_user, "user_obj": mock_owner_user, "anon_id": None,
                "municipio_config_actual": {"ejemplo_direccion": "Av. Siempreviva 742"},
                "chat_session_uuid": "test-session-uuid-123",
                "chat_db_context_data": {"processed_idempotency_keys": {}}
            }

            handler = CrearReclamoActionHandler(context)
            respuesta = handler.execute(datos_llm)

            assert respuesta["success"]
            assert "message_to_user" in respuesta
            assert mock_ticket_simulado.nro_ticket in respuesta["message_to_user"]
            assert respuesta["data"]["ticket_id"] == mock_ticket_simulado.id
            mock_crear_ticket.assert_called_once()
            datos_ticket_enviados = mock_crear_ticket.call_args[1]['ticket_data']
            assert datos_ticket_enviados["nombre_vecino"] == "Homero Simpson"
            assert datos_ticket_enviados["telefono_vecino"] == "+5491122334455"
            assert datos_ticket_enviados["email_vecino"] == "homero@example.com"
            mock_enviar_whatsapp.assert_called_once_with("+5491122334455", "Homero Simpson", "12345", "Alumbrado")

def test_accion_crear_reclamo_sin_descripcion_llm(app):
    with app.app_context():
        with patch('src.services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
             patch('src.services.actions.municipio_actions.validar_telefono', return_value=True), \
             patch('src.services.actions.municipio_actions.formatear_telefono_e164', return_value="+1234567890"):
            datos_llm = {"categoria": "Basura", "ubicacion": "Calle Siempre Viva 742", "usuario": "Test User"}
            context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
            handler = CrearReclamoActionHandler(context)
            respuesta = handler.execute(datos_llm)
            assert not respuesta["success"]
            assert "No pude entender la descripción" in respuesta["message_to_user"]
            mock_crear_ticket.assert_not_called()

def test_accion_crear_reclamo_sin_ubicacion_llm(app):
    with app.app_context():
        with patch('src.services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
            datos_llm = {"categoria": "Alumbrado", "descripcion": "Luz parpadea mucho", "usuario": "Test User"}
            context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
            handler = CrearReclamoActionHandler(context)
            respuesta = handler.execute(datos_llm)
            assert not respuesta["success"]
            assert "No pude entender la ubicación" in respuesta["message_to_user"]
            mock_crear_ticket.assert_not_called()

def test_accion_crear_reclamo_contacto_llm_invalido_usa_perfil(app):
    with app.app_context():
        with patch('src.services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
             patch('src.services.actions.municipio_actions.validar_telefono') as mock_validar_telefono_func, \
             patch('src.services.actions.municipio_actions.validar_email', return_value=True), \
             patch('src.services.actions.municipio_actions.parse_direccion_completa') as mock_parse_direccion, \
             patch('src.services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla') as mock_enviar_whatsapp, \
             patch('src.services.actions.municipio_actions.formatear_telefono_e164') as mock_formatear_tel:

            mock_ticket_simulado = MagicMock(); mock_ticket_simulado.nro_ticket = "67890"; mock_ticket_simulado.id = 2
            mock_crear_ticket.return_value = mock_ticket_simulado
            mock_parse_direccion.return_value = {"calle": "Avenida Falsa", "numero": "456", "localidad": "Testville"}

            # Teléfono del LLM inválido, teléfono del perfil válido
            mock_validar_telefono_func.side_effect = [False, True]
            mock_formatear_tel.return_value = "+549876543210" # Formato del teléfono del perfil

            datos_llm = {
                "categoria": "Varios", "descripcion": "Problema general", "ubicacion": "Avenida Falsa 456",
                "usuario": "Usuario LLM", "telefono": "tel_invalido_llm", "email": "email_invalido_llm@llm.bad"
            }

            mock_viewer_user = MagicMock(spec=User)
            mock_viewer_user.id = 200; mock_viewer_user.nombre = "Usuario Perfil Valido"
            mock_viewer_user.telefono = "9876543210"; mock_viewer_user.email = "perfil_valido@example.com"

            context = {
                "viewer_user_obj": mock_viewer_user, "user_obj": MagicMock(id=1, municipio_id="testmuni"),
                "anon_id": None, "municipio_config_actual": {}
            }
            handler = CrearReclamoActionHandler(context)
            respuesta = handler.execute(datos_llm)

            assert respuesta["success"]
            mock_crear_ticket.assert_called_once()
            datos_ticket_enviados = mock_crear_ticket.call_args[1]['ticket_data']

            assert datos_ticket_enviados["nombre_vecino"] == "Usuario LLM"
            assert datos_ticket_enviados["telefono_vecino"] == "+549876543210"
            assert datos_ticket_enviados["email_vecino"] == "perfil_valido@example.com"

            mock_enviar_whatsapp.assert_called_once_with(
                "+549876543210", "Usuario LLM", "67890", "Varios"
            )
