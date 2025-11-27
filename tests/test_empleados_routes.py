import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import User
from config import TestConfig
from routes.empleados import crear_empleado


from routes.auth import auth_bp

class EmpleadosRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()
        user = User(id=1, name='test', email='test@test.com', password_hash='test', rol='admin')
        user.set_password('test')
        db.session.add(user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_crear_empleado_email_existente(self):
        existing_user = User(name='existing', email='emp@e.com', password_hash='test', id=2)
        db.session.add(existing_user)
        db.session.commit()
        data = {
            "name": "Emp",
            "email": "emp@e.com",
            "password": "123",
            "categorias": ["Limpieza"],
        }
        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.post('/empleados', json=data, headers=headers)
            self.assertEqual(response.status_code, 400)

    def test_crear_empleado_con_categorias(self):
        data = {
            "name": "Nuevo",
            "email": "nuevo@e.com",
            "password": "123",
            "categorias": ["Limpieza", "Luminaria"],
        }
        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            self.assertEqual(login_response.status_code, 200)
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.post('/empleados', json=data, headers=headers)
            self.assertEqual(response.status_code, 201)
            created_user = User.query.filter_by(email="nuevo@e.com").first()
            self.assertIsNotNone(created_user)
            self.assertEqual(created_user.ticket_categorias, 'limpieza,luminaria')

    def test_crear_empleado_sin_categorias(self):
        data = {
            "name": "Nuevo",
            "email": "nuevo2@e.com",
            "password": "123",
            "categorias": [],
        }
        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.post('/empleados', json=data, headers=headers)
            self.assertEqual(response.status_code, 400)

    def test_actualizar_empleado_no_permite_cambiar_email(self):
        empleado = User(name='Empleado', email='empleado@e.com', password_hash='test', empresa_id=1, rol='empleado', ticket_categorias='Limpieza')
        db.session.add(empleado)
        db.session.commit()
        payload = {"email": "otro@e.com"}
        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.put(f'/empleados/{empleado.id}', json=payload, headers=headers)
            self.assertEqual(response.status_code, 400)

    def test_obtener_categorias_empleado(self):
        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}

            response = self.client.get('/empleados/categorias', headers=headers)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertIn('categorias', data)
            self.assertGreaterEqual(len(data['categorias']), 1)
            first = data['categorias'][0]
            self.assertIn('value', first)
            self.assertIn('label', first)


if __name__ == '__main__':
    unittest.main()
