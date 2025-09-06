import pytest
from app import db
from models import QA, Rubro, MunicipioTicket, TicketComentario, User


def test_basic_flow(client, init_database):
    resp = client.post('/login', json={'email': 'admin@test.com', 'password': 'admin'})
    assert resp.status_code == 200

    faq = QA(question='Q', answer='A', rubro_id=1)
    db.session.add(faq)

    user = User.query.filter_by(email='admin@test.com').first()
    ticket = MunicipioTicket(
        asunto='Prueba',
        categoria='c',
        detalles='d',
        direccion='dir',
        nombre_vecino='vecino',
        telefono_vecino='123',
        email_vecino='a@b.com',
        estado='nuevo',
        user_id=user.id,
        latitud=0.0,
        longitud=0.0,
    )
    db.session.add(ticket)
    db.session.flush()

    comentario = TicketComentario(municipio_ticket_id=ticket.id, comentario='hola', user_id=user.id)
    db.session.add(comentario)
    db.session.commit()

    assert QA.query.count() == 1
    assert MunicipioTicket.query.count() == 1
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 1
