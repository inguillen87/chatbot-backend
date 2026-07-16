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
from models import CatalogoItem, MunicipioTicket, TenantProfile, User
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
        user = User(
            name='test',
            email='test@test.com',
            password_hash='test',
            rol='admin',
        )
        user.set_password('test')
        db.session.add(user)
        db.session.commit()
        self.admin_id = user.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_crear_empleado_email_existente(self):
        existing_user = User(name='existing', email='emp@e.com', password_hash='test')
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
        empleado = User(name='Empleado', email='empleado@e.com', password_hash='test', empresa_id=self.admin_id, rol='empleado', ticket_categorias='Limpieza')
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

            api_response = self.client.get('/api/empleados/categorias', headers=headers)
            self.assertEqual(api_response.status_code, 200)
            self.assertIn('categorias', api_response.get_json())

    def test_obtener_categorias_incluye_catalogo_pyme(self):
        admin_pyme = db.session.get(User, self.admin_id)
        admin_pyme.tipo_chat = 'pyme'
        db.session.commit()

        db.session.add_all([
            CatalogoItem(user_id=admin_pyme.id, nombre='Producto', categoria='software'),
            CatalogoItem(user_id=admin_pyme.id, nombre='Servicio', categoria='consultoria'),
        ])
        db.session.commit()

        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}

            response = self.client.get('/empleados/categorias', headers=headers)
            self.assertEqual(response.status_code, 200)
            categorias = response.get_json().get('categorias', [])
            values = {c['value'] for c in categorias}
            self.assertIn('software', values)
            self.assertIn('consultoria', values)

    def test_listar_empleados_incluye_empleados_tenant_v2(self):
        admin = db.session.get(User, self.admin_id)
        admin.tipo_chat = 'municipio'
        tenant = TenantProfile(slug="municipio-test", nombre="Municipio Test", tipo="municipio", municipio_id=admin.id)
        db.session.add(tenant)
        db.session.flush()
        admin.tenant_id = tenant.id
        empleado = User(
            name="Alumbrado",
            email="alumbrado@test.com",
            password_hash="test",
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            rol="empleado",
            es_empleado=True,
            accesibilidad={"employee_scope": {"categorias": ["alumbrado"], "channels": ["whatsapp"], "permisos": ["tickets_update"]}},
        )
        db.session.add(empleado)
        db.session.commit()

        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.get('/empleados', headers=headers)

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            emails = {item["email"] for item in payload}
            self.assertIn("alumbrado@test.com", emails)
            item = next(row for row in payload if row["email"] == "alumbrado@test.com")
            self.assertEqual(item["scope"]["categorias"], ["alumbrado"])
            self.assertTrue(item["es_empleado"])

    def test_crear_empleado_sincroniza_scope_tenant(self):
        admin = db.session.get(User, self.admin_id)
        admin.tipo_chat = 'municipio'
        tenant = TenantProfile(
            slug="municipio-create",
            nombre="Municipio Create",
            tipo="municipio",
            municipio_id=admin.id,
            configuracion={"employee_categories": ["Alumbrado"]},
        )
        db.session.add(tenant)
        db.session.flush()
        admin.tenant_id = tenant.id
        db.session.commit()

        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.post(
                '/empleados',
                json={
                    "name": "Mesa Alumbrado",
                    "email": "mesa-alumbrado@test.com",
                    "password": "123",
                    "categorias": ["Alumbrado"],
                    "scope": {"zonas": ["Centro"], "channels": ["whatsapp"]},
                },
                headers=headers,
            )

            self.assertEqual(response.status_code, 201)
            created_user = User.query.filter_by(email="mesa-alumbrado@test.com").first()
            self.assertIsNotNone(created_user)
            self.assertEqual(created_user.tenant_id, tenant.id)
            self.assertTrue(created_user.es_empleado)
            self.assertEqual(created_user.accesibilidad["employee_scope"]["categorias"], ["alumbrado"])

    def test_obtener_categorias_incluye_dimensiones_tenant_municipio(self):
        admin = db.session.get(User, self.admin_id)
        admin.tipo_chat = 'municipio'
        tenant = TenantProfile(slug="municipio-dim", nombre="Municipio Dim", tipo="municipio", municipio_id=admin.id)
        db.session.add(tenant)
        db.session.flush()
        admin.tenant_id = tenant.id
        db.session.add(
            MunicipioTicket(
                pregunta="Bache",
                categoria="baches",
                municipio_id=admin.id,
                estado="nuevo",
                canal_ingreso="whatsapp",
            )
        )
        db.session.commit()

        with self.client:
            login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
            token = login_response.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}
            response = self.client.get('/empleados/categorias', headers=headers)

            self.assertEqual(response.status_code, 200)
            values = {c['value'] for c in response.get_json().get('categorias', [])}
            self.assertIn('baches', values)


if __name__ == '__main__':
    unittest.main()
