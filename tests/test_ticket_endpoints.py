import unittest
from app import create_app, db
from models import User, MunicipioTicket, Rubro
from config import TestConfig
import json

class TicketEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Crear un rubro para municipio
        municipio_rubro = Rubro(nombre='municipios', clave='municipios')
        db.session.add(municipio_rubro)
        db.session.commit() # Commit para que el rubro tenga id

        # Crear usuario admin de municipio
        admin_user = User(
            email='admin@junin.com',
            name='Admin Junin',
            rol='admin',
            municipio_id=1, # Junín
            rubro_id=municipio_rubro.id,
            tipo_chat='municipio'
        )
        admin_user.set_password('adminpass')
        db.session.add(admin_user)

        # Crear tickets para el municipio 1
        ticket1 = MunicipioTicket(
            municipio_id=1,
            user_id=admin_user.id,
            asunto='Bache en la calle',
            categoria='calle',
            pregunta='Hay un bache grande en la calle principal.'
        )
        ticket2 = MunicipioTicket(
            municipio_id=1,
            user_id=admin_user.id,
            asunto='Luz quemada',
            categoria='alumbrado',
            pregunta='El poste de luz de la esquina no funciona.'
        )
        # Crear un segundo usuario para el municipio 2
        user_municipio_2 = User(email='admin@otro.com', name='Admin Otro', rol='admin', municipio_id=2, rubro_id=municipio_rubro.id, tipo_chat='municipio')
        user_municipio_2.set_password('adminpass')
        db.session.add(user_municipio_2)
        db.session.commit()

        # Ticket de otro municipio
        ticket3 = MunicipioTicket(
            municipio_id=2,
            user_id=user_municipio_2.id,
            asunto='Arbol caido',
            categoria='espacios verdes',
            pregunta='Un arbol se cayo sobre la vereda.'
        )
        db.session.add_all([ticket1, ticket2, ticket3])
        db.session.commit()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_get_tickets_del_usuario_admin_municipio(self):
        login_resp = self.client.post('/auth/login', json={
            'email': 'admin@junin.com',
            'password': 'adminpass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']

        headers = {'Authorization': f'Bearer {token}'}
        tickets_resp = self.client.get('/tickets/usuarios', headers=headers)
        self.assertEqual(tickets_resp.status_code, 200)

        # Verificar que la respuesta contiene los tickets correctos
        data = json.loads(tickets_resp.data)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2) # Solo los tickets del municipio 1

        asuntos = {t['asunto'] for t in data}
        self.assertIn('Bache en la calle', asuntos)
        self.assertIn('Luz quemada', asuntos)
        self.assertNotIn('Arbol caido', asuntos)

if __name__ == '__main__':
    unittest.main()
