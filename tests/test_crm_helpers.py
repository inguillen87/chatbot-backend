import pytest
import unittest
from types import SimpleNamespace


def _obtener_clientes(resultados, tag=None):
    """Simplified helper mirroring `routes.crm._obtener_clientes`."""
    if tag:
        resultados = [c for c in resultados if tag in (c.tags or "").split(',')]
    return [
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "telefono": c.telefono,
            "acepta_marketing": c.acepta_marketing,
            "latitud": c.latitud,
            "longitud": c.longitud,
            "tags": c.tags.split(',') if c.tags else [],
        }
        for c in resultados
    ]


class CRMHelperTests(unittest.TestCase):
    def test_obtener_clientes_filtra_tag(self):
        u1 = SimpleNamespace(id=2, name='A', email='a', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='vip', empresa_id=1)
        u2 = SimpleNamespace(id=3, name='B', email='b', telefono='2', acepta_marketing=False, latitud=None, longitud=None, tags='regular', empresa_id=1)
        res = _obtener_clientes([u1, u2], tag='vip')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 2)


if __name__ == '__main__':
    unittest.main()
