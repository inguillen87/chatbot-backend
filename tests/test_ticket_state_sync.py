import unittest
import json
from app import create_app, db
from models import User, MunicipioTicket, PymeTicket, Rubro

class TicketStateSyncTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create municipality admin user
        muni_admin = User(name='Muni Admin', email='muni@example.com', rol='admin', tipo_chat='municipio', municipio_id=1)
        muni_admin.set_password('pass')
        db.session.add(muni_admin)

        # Create pyme admin user and rubro
        rubro = Rubro(clave='r1', nombre='R1')
        db.session.add(rubro)
        db.session.commit()
        pyme_admin = User(name='Pyme Admin', email='pyme@example.com', rol='admin', tipo_chat='pyme', rubro_id=rubro.id)
        pyme_admin.set_password('pass')
        db.session.add(pyme_admin)
        db.session.commit()

        self.muni_admin = muni_admin
        self.pyme_admin = pyme_admin

        # Create a municipal ticket
        muni_ticket = MunicipioTicket(nro_ticket='111111', municipio_id=1, pregunta='p', consulta_pin='222222', user_id=muni_admin.id)
        db.session.add(muni_ticket)
        db.session.commit()
        self.muni_ticket = muni_ticket

        # Create a pyme ticket
        pyme_ticket = PymeTicket(nro_ticket=1, rubro_id=rubro.id, pregunta='p', user_id=pyme_admin.id)
        db.session.add(pyme_ticket)
        db.session.commit()
        self.pyme_ticket = pyme_ticket

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_admin_response_updates_public_state(self):
        res_login = self.client.post('/auth/login', data=json.dumps({'email': 'muni@example.com', 'password': 'pass'}), content_type='application/json')
        token = json.loads(res_login.data)['token']
        res = self.client.post(f'/tickets/municipio/{self.muni_ticket.id}/responder',
                               headers={'Authorization': f'Bearer {token}'},
                               data=json.dumps({'comentario': 'Hola'}),
                               content_type='application/json')
        self.assertEqual(res.status_code, 200)

        resp_public = self.client.get(f'/tickets/municipio/por_numero/{self.muni_ticket.nro_ticket}?pin={self.muni_ticket.consulta_pin}')
        self.assertEqual(resp_public.status_code, 200)
        data = json.loads(resp_public.data)
        self.assertEqual(data['estado_ticket'], 'en_proceso')

    def test_estado_change_syncs_cliente(self):
        res_login = self.client.post('/auth/login', data=json.dumps({'email': 'pyme@example.com', 'password': 'pass'}), content_type='application/json')
        token = json.loads(res_login.data)['token']
        res = self.client.put(f'/tickets/pyme/{self.pyme_ticket.id}/estado',
                              headers={'Authorization': f'Bearer {token}'},
                              data=json.dumps({'estado': 'cerrado'}),
                              content_type='application/json')
        self.assertEqual(res.status_code, 200)
        actualizado = PymeTicket.query.get(self.pyme_ticket.id)
        self.assertEqual(actualizado.estado, 'cerrado')
        self.assertEqual(actualizado.estado_cliente, 'cerrado')

if __name__ == '__main__':
    unittest.main()
