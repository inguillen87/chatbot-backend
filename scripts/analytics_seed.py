"""Generate synthetic analytics dataset for demos and performance tests."""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from typing import List

from faker import Faker

from app import create_app
from extensions import db
from models import (
    ArchivoAdjunto,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TicketComentario,
    TicketSatisfaccion,
    User,
)
from services.analytics.jobs import rebuild_analytics_snapshot

fake = Faker('es_AR')

CANALES = ['web', 'whatsapp', 'app', 'presencial']
CATEGORIAS = ['iluminación', 'residuos', 'tránsito', 'seguridad', 'comercio']
ESTADOS = ['nuevo', 'en_progreso', 'resuelto', 'cerrado']
PRODUCTOS = ['Delivery', 'Mantenimiento', 'Instalación', 'Soporte', 'Licencia']


def random_coords() -> tuple[float, float]:
    lat = random.uniform(-34.75, -34.45)
    lon = random.uniform(-58.6, -58.3)
    return lat, lon


def ensure_user(tenant_id: int, scope: str) -> User:
    role = 'admin'
    email = f"analytics_{scope}_{tenant_id}@example.com"
    user = User.query.filter_by(email=email).first()
    if user:
        return user
    user = User(
        name=fake.name(),
        email=email,
        rol=role,
        municipio_id=tenant_id if scope == 'municipio' else None,
        pyme_id=tenant_id if scope == 'pyme' else None,
    )
    user.set_password('demo1234')
    db.session.add(user)
    db.session.commit()
    return user


def seed_municipio(tenant_id: int, days: int, tickets: int) -> None:
    admin = ensure_user(tenant_id, 'municipio')
    start_date = datetime.utcnow() - timedelta(days=days)
    for _ in range(tickets):
        created_at = start_date + timedelta(days=random.randint(0, days), hours=random.randint(0, 23))
        lat, lon = random_coords()
        ticket = MunicipioTicket(
            municipio_id=tenant_id,
            pregunta=fake.text(max_nb_chars=140),
            categoria=random.choice(CATEGORIAS),
            canal_ingreso=random.choice(CANALES),
            estado=random.choice(ESTADOS[:-1]),
            fecha=created_at,
            latitud=lat,
            longitud=lon,
            distrito=random.choice(['Centro', 'Norte', 'Sur', 'Oeste']),
        )
        db.session.add(ticket)
        db.session.flush()

        first_reply = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario=fake.sentence(),
            fecha=created_at + timedelta(minutes=random.randint(10, 180)),
            es_admin=True,
            user_id=admin.id,
            estado_ticket='en_progreso',
        )
        db.session.add(first_reply)
        if random.random() > 0.4:
            close_comment = TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario='Ticket resuelto',
                fecha=first_reply.fecha + timedelta(hours=random.randint(2, 48)),
                es_admin=True,
                user_id=admin.id,
                estado_ticket='cerrado',
            )
            ticket.estado = 'cerrado'
            db.session.add(close_comment)
        if random.random() > 0.5:
            survey = TicketSatisfaccion(
                ticket_id=ticket.id,
                tipo='municipio',
                puntuacion=random.randint(1, 10),
                comentario=fake.sentence(),
                fecha=created_at + timedelta(days=1),
            )
            db.session.add(survey)


def seed_pyme(tenant_id: int, days: int, pedidos: int) -> None:
    admin = ensure_user(tenant_id, 'pyme')
    start_date = datetime.utcnow() - timedelta(days=days)
    for _ in range(pedidos):
        created_at = start_date + timedelta(days=random.randint(0, days), hours=random.randint(0, 23))
        lat, lon = random_coords()
        ticket = PymeTicket(
            user_id=tenant_id,
            pregunta=fake.text(max_nb_chars=120),
            categoria=random.choice(PRODUCTOS),
            estado=random.choice(ESTADOS[:-1]),
            fecha=created_at,
            latitud=lat,
            longitud=lon,
        )
        db.session.add(ticket)
        db.session.flush()

        comment = TicketComentario(
            pyme_ticket_id=ticket.id,
            comentario=fake.sentence(),
            fecha=created_at + timedelta(minutes=random.randint(5, 120)),
            es_admin=True,
            user_id=admin.id,
            estado_ticket='en_progreso',
        )
        db.session.add(comment)

        order_total = round(random.uniform(2000, 25000), 2)
        pedido = PymePedido(
            pyme_id=tenant_id,
            asunto=random.choice(PRODUCTOS),
            detalles=fake.json(data_columns={'nombre': 'word', 'qty': 'pyint', 'precio': 'pyfloat'}),
            monto_total=order_total,
            nombre_cliente=fake.name(),
            email_cliente=fake.email(),
            telefono_cliente=fake.phone_number(),
            direccion=fake.street_address(),
            latitud=lat,
            longitud=lon,
        )
        db.session.add(pedido)
        db.session.flush()

        if random.random() > 0.6:
            survey = TicketSatisfaccion(
                ticket_id=ticket.id,
                tipo='pyme',
                puntuacion=random.randint(3, 5),
                comentario='Cliente satisfecho',
                fecha=created_at + timedelta(days=2),
            )
            db.session.add(survey)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Seed analytics demo data')
    parser.add_argument('--tenant', type=int, required=True, help='ID del tenant (municipio/pyme)')
    parser.add_argument('--days', type=int, default=30, help='Cantidad de días hacia atrás')
    parser.add_argument('--tickets', type=int, default=5000, help='Tickets municipio a generar')
    parser.add_argument('--orders', type=int, default=3000, help='Pedidos PyME a generar')
    parser.add_argument('--scope', choices=['municipio', 'pyme', 'ambos'], default='ambos')
    parser.add_argument('--refresh', action='store_true', help='Ejecutar rebuild_analytics_snapshot al finalizar')
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
    app = create_app()
    with app.app_context():
        if args.scope in {'municipio', 'ambos'}:
            seed_municipio(args.tenant, args.days, args.tickets)
        if args.scope in {'pyme', 'ambos'}:
            seed_pyme(args.tenant, args.days, args.orders)
        db.session.commit()
        if args.refresh:
            rebuild_analytics_snapshot(str(args.tenant), 'municipio')
            rebuild_analytics_snapshot(str(args.tenant), 'pyme')
        print('✅ Datos generados para tenant', args.tenant)


if __name__ == '__main__':
    main()
