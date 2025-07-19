import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import create_app, db
from models import User, Rubro
from routes.estadisticas import estadisticas_reclamos
from config import TestingConfig

class EstadisticasRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_pyme_filters_by_rubro(self):
        rows = [SimpleNamespace(rubro='bodega', total=2)]
        session = SimpleNamespace(
            execute=MagicMock(side_effect=[
                MagicMock(fetchall=MagicMock(return_value=rows)),
                MagicMock(scalar=MagicMock(return_value=5)),
                MagicMock(scalar=MagicMock(return_value=120.0)),
            ])
        )
        user = User(
            rubro=Rubro(nombre='bodega', clave='bodega'),
            rubro_id=7,
            municipio_id=None,
            empresa_id=None,
            rol='admin',
            email='test@test.com',
            name='test'
        )
        user.set_password('test')
        db.session.add(user.rubro)
        db.session.add(user)
        db.session.commit()

        with self.app.test_request_context():
            with patch('routes.estadisticas.jsonify', lambda x: x), \
                 patch('routes.estadisticas.db', SimpleNamespace(session=session)):
                resp = estadisticas_reclamos.__wrapped__.__wrapped__(user)

        self.assertEqual(resp['por_rubro'][0]['total'], 2)
        self.assertEqual(resp['por_tipo'][0]['total'], 5)
        self.assertEqual(resp['tiempo_respuesta_promedio_segundos']['pyme'], 120.0)
        for call in session.execute.call_args_list:
            params = call.kwargs.get('params')
            if params:
                self.assertEqual(params.get('rid'), 7)

    def test_municipio_filters_by_id(self):
        session = SimpleNamespace(
            execute=MagicMock(side_effect=[
                MagicMock(scalar=MagicMock(return_value=4)),
                MagicMock(scalar=MagicMock(return_value=60.0)),
            ])
        )
        user = User(
            rubro=Rubro(nombre='municipios', clave='municipios'),
            rubro_id=1,
            municipio_id=3,
            empresa_id=None,
            rol='admin',
            email='test@test.com',
            name='test'
        )
        user.set_password('test')
        db.session.add(user.rubro)
        db.session.add(user)
        db.session.commit()

        with self.app.test_request_context():
            with patch('routes.estadisticas.jsonify', lambda x: x), \
                 patch('routes.estadisticas.db', SimpleNamespace(session=session)):
                resp = estadisticas_reclamos.__wrapped__.__wrapped__(user)
        self.assertEqual(resp['por_rubro'], [])
        self.assertEqual(resp['por_tipo'][0]['total'], 4)
        self.assertEqual(resp['tiempo_respuesta_promedio_segundos']['municipio'], 60.0)
        for call in session.execute.call_args_list:
            params = call.kwargs.get('params')
            if params:
                self.assertEqual(params.get('mid'), 3)

if __name__ == '__main__':
    unittest.main()
