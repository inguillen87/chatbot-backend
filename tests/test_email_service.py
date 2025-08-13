import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from services import email_service

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

if __name__ == "__main__":
    unittest.main()
