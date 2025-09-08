import unittest
from unittest.mock import patch
import sys, os

# Ensure project root on path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.actions.municipio_actions import ConsultarInfoTramiteActionHandler

class TestConsultarInfoTramiteAction(unittest.TestCase):
    @patch('services.municipio_responder.obtener_info_tramite_web')
    def test_execute_returns_info(self, mock_info):
        mock_info.return_value = {"contenido": "Información sobre licencia", "botones": []}
        handler = ConsultarInfoTramiteActionHandler({})
        result = handler.execute({"nombre_tramite": "Licencia de Conducir"})
        self.assertTrue(result["success"])
        self.assertIn("licencia", result["message_to_user"].lower())
        self.assertEqual(result["message_type"], "interactive_buttons")
        self.assertTrue(any(btn.get("id_accion") == "info_tramite" for btn in result.get("options_list", [])))

if __name__ == '__main__':
    unittest.main()
