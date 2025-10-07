import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from services import email_service


class DummyTicket(SimpleNamespace):
    pass

class EmailServiceAdminTests(unittest.TestCase):
    def setUp(self):
        self.pedido = SimpleNamespace(
            nro_pedido=1,
            detalles="items",
            nombre_cliente="Juan",
            email_cliente="juan@example.com",
            telefono_cliente="123",
            monto_total=100.0,
        )
        self.ticket = SimpleNamespace(
            nro_ticket=1,
            asunto="Asunto",
            categoria="Cat",
            pregunta="?",
            email="cli@example.com",
        )

    def test_no_admin_email_pedido(self):
        mock_app = SimpleNamespace(config={}, logger=MagicMock())
        with patch("services.email_service.current_app", mock_app):
            with patch.object(email_service, "enviar_email") as mock_send:
                result = email_service.enviar_email_pedido_admin(self.pedido)
        self.assertFalse(result)
        mock_send.assert_not_called()

    def test_no_admin_email_ticket(self):
        mock_app = SimpleNamespace(config={}, logger=MagicMock())
        with patch("services.email_service.current_app", mock_app):
            with patch.object(email_service, "enviar_email") as mock_send:
                result = email_service.enviar_email_ticket_admin(self.ticket)
        self.assertFalse(result)
        mock_send.assert_not_called()

    def test_enviar_email_ticket_admin_uses_admin_user(self):
        mock_app = SimpleNamespace(config={"ADMIN_EMAIL": "fallback@example.com", "APP_BASE_URL": "https://app.test"})
        admin_user = SimpleNamespace(email="admin@example.com", name="Sofía", rubro=None)
        ticket = DummyTicket(
            nro_ticket="123456",
            municipio_id=5,
            categoria="Alumbrado",
            asunto="Farola sin luz",
            detalles="La farola frente a mi casa no funciona.",
            direccion="Calle Falsa 123",
            consulta_pin="654321",
            canal_ingreso="web",
            nombre_vecino="Juan",
            email_vecino="juan@example.com",
            telefono_vecino="12345",
            dni_vecino="12345678",
            fecha=None,
            id=42,
        )

        with patch("services.email_service.current_app", mock_app), \
             patch("services.email_service.render_template", return_value="<html>") as mock_render, \
             patch.object(email_service, "enviar_email", return_value=True) as mock_send:
            result = email_service.enviar_email_ticket_admin(ticket, admin_user=admin_user, tipo_ticket="municipio")

        self.assertTrue(result)
        mock_render.assert_called_once()
        mock_send.assert_called_once()

    def test_enviar_email_ticket_cliente_envia_correo(self):
        mock_app = SimpleNamespace(config={"APP_BASE_URL": "https://app.test"})
        admin_user = SimpleNamespace(telefono="555-1234", horario="9 a 18", link_web="https://mi.muni")
        ticket = DummyTicket(
            id=99,
            nro_ticket="7890",
            municipio_id=7,
            categoria="Bache",
            asunto="Bache en la calle",
            detalles="Hay un bache grande frente a la escuela.",
            direccion="Av. Siempre Viva 742",
            nombre_vecino="María",
            email_vecino="maria@example.com",
            consulta_pin="112233",
        )

        with patch("services.email_service.current_app", mock_app), \
             patch("services.email_service.render_template", return_value="<html>") as mock_render, \
             patch.object(email_service, "enviar_email", return_value=True) as mock_send:
            result = email_service.enviar_email_ticket_cliente(ticket, admin_user=admin_user, tipo_ticket="municipio")

        self.assertTrue(result)
        mock_render.assert_called_once()
        mock_send.assert_called_once()

if __name__ == "__main__":
    unittest.main()
