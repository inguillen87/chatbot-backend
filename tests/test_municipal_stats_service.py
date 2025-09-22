import unittest
from datetime import datetime, timedelta, timezone

from flask import Flask

from config import TestConfig
from database import db
from models import (
    MunicipioTicket,
    SugerenciaCiudadano,
    TicketComentario,
    TicketSatisfaccion,
    User,
)
from services.municipal_stats import build_stats_for_municipio, StatsFilters


class MunicipalStatsServiceTest(unittest.TestCase):
    def setUp(self):
        self.app: Flask = Flask(__name__)
        self.app.config.from_object(TestConfig)
        db.init_app(self.app)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.admin = User(
            name="Municipio",
            email="muni@example.com",
            rol="admin",
            tipo_chat="municipio",
        )
        self.admin.set_password("secret")
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_build_stats_for_municipio(self):
        base_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        ticket_recent = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="nuevo",
            categoria="Alumbrado",
            distrito="Centro",
            canal_ingreso="WhatsApp",
            fecha=base_time - timedelta(hours=10),
            latitud=-34.60,
            longitud=-58.38,
        )
        ticket_closed = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="cerrado",
            categoria="Alumbrado",
            distrito="Centro",
            canal_ingreso="Web",
            fecha=base_time - timedelta(days=2),
            ultima_actividad=base_time - timedelta(days=1),
            latitud=-34.61,
            longitud=-58.39,
        )
        ticket_in_progress = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="en_proceso",
            categoria="Limpieza",
            distrito="Norte",
            canal_ingreso="App",
            fecha=base_time - timedelta(days=4),
        )
        ticket_long_open = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="en_vivo",
            categoria="Limpieza",
            distrito=None,
            canal_ingreso="WhatsApp",
            fecha=base_time - timedelta(days=10),
        )

        db.session.add_all(
            [ticket_recent, ticket_closed, ticket_in_progress, ticket_long_open]
        )
        db.session.commit()

        comentarios = [
            TicketComentario(
                municipio_ticket_id=ticket_recent.id,
                comentario="Respuesta",
                es_admin=True,
                fecha=ticket_recent.fecha + timedelta(hours=2),
            ),
            TicketComentario(
                municipio_ticket_id=ticket_closed.id,
                comentario="Respuesta",
                es_admin=True,
                fecha=ticket_closed.fecha + timedelta(hours=1),
            ),
            TicketComentario(
                municipio_ticket_id=ticket_in_progress.id,
                comentario="Seguimiento",
                es_admin=True,
                fecha=ticket_in_progress.fecha + timedelta(hours=30),
            ),
        ]
        db.session.add_all(comentarios)

        encuesta = TicketSatisfaccion(
            ticket_id=ticket_closed.id,
            tipo="municipio",
            puntuacion=5,
        )
        db.session.add(encuesta)

        sugerencias = [
            SugerenciaCiudadano(
                municipio_id=self.admin.id,
                texto_sugerencia="Mas luces",
                estado="nueva",
                categoria="Alumbrado",
                fecha=base_time - timedelta(days=1),
            ),
            SugerenciaCiudadano(
                municipio_id=self.admin.id,
                texto_sugerencia="Recorridos",
                estado="implementada",
                categoria="Transporte",
                fecha=base_time - timedelta(days=40),
            ),
        ]
        db.session.add_all(sugerencias)
        db.session.commit()

        stats = build_stats_for_municipio(self.admin.id, now=base_time)

        self.assertEqual(stats["resumen"]["total"], 4)
        self.assertEqual(stats["resumen"]["abiertos"], 3)
        self.assertEqual(stats["resumen"]["cerrados"], 1)
        self.assertAlmostEqual(stats["resumen"]["tasa_resolucion"], 25.0)
        self.assertAlmostEqual(stats["resumen"]["promedio_satisfaccion"], 5.0)
        self.assertEqual(stats["backlog"], {
            "menos_72h": 1,
            "entre_3_y_7_dias": 1,
            "mas_7_dias": 1,
        })
        self.assertEqual(stats["geolocalizacion"]["con_coordenadas"], 2)
        self.assertEqual(stats["sugerencias"]["total"], 2)
        self.assertTrue(any(punto["categoria"] == "Alumbrado" for punto in stats["por_categoria"]))
        self.assertTrue(stats["tiempos_respuesta"]["promedio_horas"] > 0)
        self.assertTrue(stats["tiempos_cierre"]["promedio_horas"] > 0)

    def test_build_stats_filters_by_date(self):
        base_time = datetime(2024, 5, 10, tzinfo=timezone.utc)

        recent = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="nuevo",
            categoria="Alumbrado",
            fecha=base_time - timedelta(hours=6),
        )
        old = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="cerrado",
            categoria="Alumbrado",
            fecha=base_time - timedelta(days=30),
        )
        db.session.add_all([recent, old])
        db.session.commit()

        filtros = StatsFilters(
            fecha_inicio=base_time - timedelta(days=1),
            fecha_fin=base_time + timedelta(days=1),
        )

        stats = build_stats_for_municipio(self.admin.id, filters=filtros, now=base_time)

        self.assertEqual(stats["resumen"]["total"], 1)
        self.assertEqual(stats["resumen"]["nuevos"], 1)
        self.assertTrue(all(categoria["categoria"] == "Alumbrado" for categoria in stats["por_categoria"]))

    def test_build_stats_filters_by_categoria(self):
        base_time = datetime(2024, 6, 1, tzinfo=timezone.utc)

        alumbrado = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="nuevo",
            categoria="Alumbrado",
            fecha=base_time - timedelta(days=1),
        )
        limpieza = MunicipioTicket(
            municipio_id=self.admin.id,
            estado="en_proceso",
            categoria="Limpieza",
            fecha=base_time - timedelta(days=1),
        )
        db.session.add_all([alumbrado, limpieza])
        db.session.commit()

        filtros = StatsFilters(categorias=("Alumbrado",))

        stats = build_stats_for_municipio(self.admin.id, filters=filtros, now=base_time)

        self.assertEqual(stats["resumen"]["total"], 1)
        self.assertEqual(len(stats["por_categoria"]), 1)
        self.assertEqual(stats["por_categoria"][0]["categoria"], "Alumbrado")


if __name__ == '__main__':
    unittest.main()
