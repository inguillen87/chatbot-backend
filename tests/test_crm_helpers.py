import unittest
from types import SimpleNamespace

class DummyQuery(list):
    def filter_by(self, **kwargs):
        filtered = [u for u in self if all(getattr(u, k) == v for k, v in kwargs.items())]
        return DummyQuery(filtered)

    def filter(self, condition):
        like = condition.right.value.strip('%')
        filtered = [u for u in self if like in (u.tags or '')]
        return DummyQuery(filtered)

    def order_by(self, *a, **k):
        return self

    def all(self):
        return list(self)

def _obtener_clientes(query_user, tag=None):
    query = DummyQuery(query_user)
    if tag:
        like = tag
        query = query.filter(SimpleNamespace(right=SimpleNamespace(value=f"%{like}%")))
    clientes = query.order_by(None).all()
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
        for c in clientes
    ]


class CRMHelperTests(unittest.TestCase):
    def test_obtener_clientes_filtra_tag(self):
        current_user = SimpleNamespace(id=1)
        u1 = SimpleNamespace(id=2, name='A', email='a', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='vip', empresa_id=1)
        u2 = SimpleNamespace(id=3, name='B', email='b', telefono='2', acepta_marketing=False, latitud=None, longitud=None, tags='regular', empresa_id=1)
        res = _obtener_clientes([u1, u2], tag='vip')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 2)

if __name__ == '__main__':
    unittest.main()
