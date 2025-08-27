import os
import json
import tempfile
import shutil
import unittest

from services import config_loader

class ConfigLoaderIntMunicipioIDTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)
        municipios_dir = os.path.join(self.temp_dir, "municipios", "1")
        os.makedirs(municipios_dir, exist_ok=True)
        with open(os.path.join(municipios_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"ok": True}, f)
        # Patch BASE_CONFIG_PATH to our temp directory
        self._old_base = config_loader.BASE_CONFIG_PATH
        config_loader.BASE_CONFIG_PATH = os.path.join(self.temp_dir, "municipios")

    def tearDown(self):
        config_loader.BASE_CONFIG_PATH = self._old_base

    def test_accepts_int_municipio_id(self):
        data = config_loader.cargar_configuracion_municipio(1, "config.json")
        self.assertEqual(data.get("ok"), True)

if __name__ == "__main__":
    unittest.main()
