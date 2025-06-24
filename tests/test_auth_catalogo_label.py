import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import sys

sys.modules.setdefault('cohere', ModuleType('cohere'))
sys.modules.setdefault('pydantic', ModuleType('pydantic'))
sys.modules.setdefault('pydantic_core', ModuleType('pydantic_core'))

from routes.auth import get_current_user

class CatalogoLabelTests(unittest.TestCase):
    def _base_user(self):
        return SimpleNamespace(
            id=1,
            email='a@a',
            name='A',
            token='tok',
            rubro=None,
            nombre_empresa='E',
            rol='admin',
            empresa_id=None,
            telefono=None,
            direccion=None,
            ciudad=None,
            provincia=None,
            pais=None,
            latitud=None,
            longitud=None,
            link_web=None,
            plan='free',
            preguntas_usadas=0,
            horario_json=None,
            logo_url='',
            ticket_categorias=''
        )

    def test_pyme_label(self):
        user = self._base_user()
        with patch('utils.plan_limits.limite_para_usuario', lambda u: 5), \
             patch('routes.auth.jsonify', lambda x: x):
            resp = get_current_user.__wrapped__(user)
        self.assertEqual(resp['catalogo_label'], 'Cargar Catálogo de Productos')
        self.assertEqual(resp['tipo_chat'], 'pyme')

    def test_municipio_label(self):
        user = self._base_user()
        user.rubro = SimpleNamespace(nombre='municipio')
        with patch('utils.plan_limits.limite_para_usuario', lambda u: 5), \
             patch('routes.auth.jsonify', lambda x: x):
            resp = get_current_user.__wrapped__(user)
        self.assertEqual(resp['catalogo_label'], 'Cargar Catálogo de Trámites')
        self.assertEqual(resp['tipo_chat'], 'municipio')

if __name__ == '__main__':
    unittest.main()
