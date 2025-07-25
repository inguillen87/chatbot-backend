import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes.auth import actualizar_me

class DummySession:
    def commit(self):
        pass
    def rollback(self):
        pass

def make_user():
    return SimpleNamespace(
        rol='usuario',
        role='usuario',
        empresa_id=None,
        name='Juan',
        telefono=None,
        direccion=None,
        ciudad=None,
        provincia=None,
        pais=None,
        latitud=None,
        longitud=None,
        link_web=None,
        plan='gratis',
        preguntas_usadas=0,
        limite_preguntas=50,
        horario=None,
        acepta_marketing=False,
        tags='',
    )

class ProfileUpdateSecurityTests(unittest.TestCase):
    def test_cannot_change_role_or_empresa(self):
        user = make_user()
        data = {
            'rol': 'empleado',
            'role': 'empleado',
            'empresa_id': 123,
            'name': 'Nuevo'
        }
        with patch('routes.auth.request', SimpleNamespace(get_json=lambda silent=True: data, method='POST', path='/me')), \
             patch('routes.auth.db', SimpleNamespace(session=DummySession())):
            resp = actualizar_me(user)
        self.assertEqual(resp.get_json()["mensaje"], "Perfil actualizado correctamente.")
        self.assertEqual(user.rol, 'usuario')
        self.assertEqual(user.empresa_id, None)
        self.assertEqual(user.name, 'Nuevo')

if __name__ == '__main__':
    unittest.main()
