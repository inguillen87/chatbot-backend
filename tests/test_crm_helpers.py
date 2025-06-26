import unittest
from types import SimpleNamespace

class DummyQuery(list):
    def filter_by(self, **kwargs):
        filtered = [u for u in self if all(getattr(u, k) == v for k, v in kwargs.items())]
        return DummyQuery(filtered)

    def filter(self, condition):
        like = condition.right.value.strip('%')
        filtered = [u for u in self if like in (u.tags or '') or like in u.name or like in u.email]
        return DummyQuery(filtered)

    def order_by(self, column):
        reverse = getattr(column, 'direction', '').upper() == 'DESC'
        key = column.element.name
        return DummyQuery(sorted(self, key=lambda u: getattr(u, key), reverse=reverse))

    def all(self):
        return list(self)

def _obtener_clientes(query_user, tag=None, q=None, acepta_marketing=None, sort=None, order=None):
    query = DummyQuery(query_user)
    if tag:
        like = tag
        query = query.filter(SimpleNamespace(right=SimpleNamespace(value=f"%{like}%")))
    if q:
        like = q
        query = query.filter(SimpleNamespace(right=SimpleNamespace(value=f"%{like}%")))
    if acepta_marketing is not None:
        val = acepta_marketing in ("1", "true", "t", "yes", "si")
        query = DummyQuery([u for u in query if u.acepta_marketing == val])
    if sort not in {"name", "email", "telefono"}:
        sort = "name"
    reverse = order == "desc"
    query = query.order_by(SimpleNamespace(element=SimpleNamespace(name=sort), direction="desc" if reverse else "asc"))
    clientes = query.all()
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
        u1 = SimpleNamespace(id=2, name='A', email='a', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='vip', empresa_id=1)
        u2 = SimpleNamespace(id=3, name='B', email='b', telefono='2', acepta_marketing=False, latitud=None, longitud=None, tags='regular', empresa_id=1)
        res = _obtener_clientes([u1, u2], tag='vip')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 2)

    def test_obtener_clientes_busqueda(self):
        u1 = SimpleNamespace(id=1, name='Alice', email='alice@example.com', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        u2 = SimpleNamespace(id=2, name='Bob', email='bob@sample.com', telefono='2', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        res = _obtener_clientes([u1, u2], q='bob')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 2)

    def test_obtener_clientes_marketing(self):
        u1 = SimpleNamespace(id=1, name='A', email='a', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        u2 = SimpleNamespace(id=2, name='B', email='b', telefono='2', acepta_marketing=False, latitud=None, longitud=None, tags='', empresa_id=1)
        res = _obtener_clientes([u1, u2], acepta_marketing='true')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

    def test_obtener_clientes_sort_desc(self):
        u1 = SimpleNamespace(id=1, name='Alice', email='a', telefono='1', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        u2 = SimpleNamespace(id=2, name='Bob', email='b', telefono='2', acepta_marketing=True, latitud=None, longitud=None, tags='', empresa_id=1)
        res = _obtener_clientes([u1, u2], sort='name', order='desc')
        self.assertEqual(res[0]['name'], 'Bob')

if __name__ == '__main__':
    unittest.main()
