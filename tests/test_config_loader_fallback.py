import os
import json
import tempfile
import shutil
import unittest

from services import config_loader

class ConfigLoaderFallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)
        default_dir = os.path.join(self.temp_dir, 'municipios', 'default')
        os.makedirs(default_dir, exist_ok=True)
        with open(os.path.join(default_dir, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'fallback': True}, f)
        self._old_base = config_loader.BASE_CONFIG_PATH
        config_loader.BASE_CONFIG_PATH = os.path.join(self.temp_dir, 'municipios')

    def tearDown(self):
        config_loader.BASE_CONFIG_PATH = self._old_base

    def test_uses_default_when_specific_missing(self):
        data = config_loader.cargar_configuracion_municipio('999', 'config.json')
        self.assertTrue(data.get('fallback'))

if __name__ == '__main__':
    unittest.main()
