import importlib
import os
import shutil
import tempfile
import unittest


class VeterinariaContactTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.prev_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.temp_dir
        import services.config_loader as cl
        importlib.reload(cl)
        import services.municipio_responder as mr
        importlib.reload(mr)
        self.mr = mr

    def tearDown(self):
        if self.prev_data_dir is not None:
            os.environ["DATA_DIR"] = self.prev_data_dir
        else:
            del os.environ["DATA_DIR"]
        shutil.rmtree(self.temp_dir)

    def test_veterinaria_contact_from_json(self):
        result = self.mr.handle_main_menu_action(
            "veterinaria_bromatologia",
            {"municipio_id": "default", "chat_db_context_data": {}},
            None,
        )
        self.assertIn("Información de Veterinaria y Bromatología", result["message_body"])
        self.assertIn("Dra. Laura Funes", result["message_body"])
        self.assertTrue(
            any("wa.me" in b.get("url", "") for b in result.get("options_list", []))
        )

    def test_veterinaria_contact_from_alias(self):
        """Ensure the 'bromatologia' action alias returns the same info."""
        result = self.mr.handle_main_menu_action(
            "bromatologia",
            {"municipio_id": "default", "chat_db_context_data": {}},
            None,
        )
        self.assertIn("Información de Veterinaria y Bromatología", result["message_body"])


if __name__ == "__main__":
    unittest.main()
