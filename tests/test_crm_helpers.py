import unittest
from types import SimpleNamespace

def _obtener_clientes(clientes, tag=None, q=None, marketing=None):
    resultados = list(clientes)
    if tag:
        tag_l = tag.lower()
        resultados = [u for u in resultados if tag_l in (u.tags or '').lower()]
    if q:
        q_l = q.lower()
        resultados = [u for u in resultados if q_l in u.name.lower() or q_l in u.email.lower()]
    if marketing is not None:
        resultados = [u for u in resultados if u.acepta_marketing == marketing]
    resultados.sort(key=lambda u: u.name)
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
        current_user = SimpleNamespace(id=1)
        u1 = SimpleNamespace(id=2, name='A', email='a', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='vip', empresa_id=1)
        u2 = SimpleNamespace(id=3, name='B', email='b', telefono='2', acepta_marketing=False, latitud=None, longitud=None, tags='regular', empresa_id=1)
        res = _obtener_clientes([u1, u2], tag='vip')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 2)

    def test_obtener_clientes_filtra_busqueda(self):
        u1 = SimpleNamespace(id=1, name='Juan', email='juan@test.com', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        u2 = SimpleNamespace(id=2, name='Ana', email='ana@test.com', telefono='2', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        res = _obtener_clientes([u1, u2], q='juan')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

    def test_obtener_clientes_filtra_marketing(self):
        u1 = SimpleNamespace(id=1, name='J', email='j@test.com', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        u2 = SimpleNamespace(id=2, name='K', email='k@test.com', telefono='2', acepta_marketing=False, latitud=None, longitud=None, tags='', empresa_id=1)
        res = _obtener_clientes([u1, u2], marketing=True)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

if __name__ == '__main__':
    unittest.main()
