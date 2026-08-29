import unittest
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import patch, MagicMock
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Stub models and DB session template
models_stub = ModuleType('models')
class _DummySession:
    def __init__(self):
        self.added = []
    def add(self, obj):
        self.added.append(obj)
    def commit(self):
        pass
    def rollback(self):
        pass
    def get(self, model, obj_id):
        return None
models_stub.MunicipioTicket = type('MunicipioTicket', (), {})
models_stub.PymeTicket = type('PymeTicket', (), {})
models_stub.TicketComentario = type('TicketComentario', (), {})
models_stub.TicketDomainEffectReceipt = type('TicketDomainEffectReceipt', (), {})
models_stub.Conversacion = type('Conversacion', (), {})
class DummySurvey(SimpleNamespace):
    pass
models_stub.TicketSatisfaccion = DummySurvey
models_stub.db = SimpleNamespace(session=_DummySession())
models_stub.db.func = SimpleNamespace(lower=lambda x: x)
class DummyUser:
    email = 'existing@example.com'
    name = 'Existing'
    telefono = '2636160364'
    id = 2
models_stub.User = DummyUser

import services.ticket_service as ts
ServicioTickets = ts.ServicioTickets

class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        # Patch only the collaborators used by this module.  Replacing
        # ``sys.modules['models']`` process-wide is unsafe: if setup raises
        # before unittest calls tearDown, the fake module leaks into the rest
        # of the suite and later ``create_app`` calls cannot import real model
        # classes (for example EncEncuesta).
        self.models_patch = patch.multiple(
            ts,
            MunicipioTicket=models_stub.MunicipioTicket,
            PymeTicket=models_stub.PymeTicket,
            TicketComentario=models_stub.TicketComentario,
            TicketDomainEffectReceipt=models_stub.TicketDomainEffectReceipt,
            TicketSatisfaccion=models_stub.TicketSatisfaccion,
            Conversacion=models_stub.Conversacion,
            User=models_stub.User,
            db=models_stub.db,
        )
        self.models_patch.start()
        self.addCleanup(self.models_patch.stop)
        models_stub.db.session.flush = MagicMock()
        self.email_admin_patch = patch("services.email_service.enviar_email_ticket_admin", return_value=True)
        self.email_cliente_patch = patch("services.email_service.enviar_email_ticket_cliente", return_value=True)
        self.email_admin_patch.start()
        self.addCleanup(self.email_admin_patch.stop)
        self.email_cliente_patch.start()
        self.addCleanup(self.email_cliente_patch.stop)
        self.ticket_scope_resolution_patch = patch.object(
            ts,
            "resolve_unique_tenant_for_owner",
            side_effect=lambda owner_id: SimpleNamespace(
                status="unique",
                tenant=SimpleNamespace(id=owner_id, municipio_id=owner_id, pyme_id=None),
            ),
        )
        self.ticket_scope_query_patch = patch.object(
            ts,
            "scoped_municipio_ticket_query",
            side_effect=lambda tenant, query=None: (
                query or models_stub.MunicipioTicket.query
            ).filter_by(municipio_id=tenant.municipio_id),
        )
        self.ticket_scope_resolution_patch.start()
        self.addCleanup(self.ticket_scope_resolution_patch.stop)
        self.ticket_scope_query_patch.start()
        self.addCleanup(self.ticket_scope_query_patch.stop)

    def test_guardar_encuesta_crea_objeto(self):
        service = ServicioTickets()
        encuesta = service.guardar_encuesta(1, 'municipio', 5, 'ok')
        self.assertIsNotNone(encuesta)
        self.assertEqual(encuesta.puntuacion, 5)

    def test_mapa_filtra_por_municipio(self):
        DummyTicket = SimpleNamespace

        # Mocking the structure that obtener_tickets_con_ubicacion_para_mapa would query
        # This test needs to be adapted if the internal logic of the method changes significantly.
        # For now, we assume it queries and filters.
        class DummyQuery(list):
            def filter_by(self, **kwargs):
                # Simple filter_by mock
                return DummyQuery([t for t in self if all(getattr(t, k, None) == v for k, v in kwargs.items())])
            def filter(self, *criterion): # Add basic filter mock
                # This is a very basic mock for .filter(Model.latitud.isnot(None), ...)
                # It won't actually evaluate complex SQLAlchemy criterion.
                return self
            def all(self):
                return list(self)

        # Sample data that would be in the DB
        from datetime import datetime # Needed for fecha
        t1 = DummyTicket(id=1, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t2 = DummyTicket(id=2, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None) # Same location as t1
        t3 = DummyTicket(id=3, estado='abierto', latitud=11.0, longitud=21.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t4 = DummyTicket(id=4, estado='abierto', latitud=12.0, longitud=22.0, municipio_id=6, fecha=datetime.utcnow(), categoria=None, asunto=None) # Different municipio

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        query = DummyQuery([
            SimpleNamespace(id=1, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=2, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=3, estado='abierto', latitud=11.0, longitud=21.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=4, estado='abierto', latitud=12.0, longitud=22.0, municipio_id=6, fecha=datetime.utcnow(), categoria=None, asunto=None)
        ])
        DummyModel.query = query

        with patch.object(ts, 'MunicipioTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(tipo_ticket='municipio', municipio_id=5)

        # The method now returns a list of dicts with "location", "weight" and "categoria"
        # We expect two distinct locations for municipio_id=5: (10.0, 20.0) with weight 2, and (11.0, 21.0) with weight 1
        self.assertEqual(len(res), 2)

        found_loc1 = False
        found_loc2 = False
        for item in res:
            self.assertIn('lat', item)
            self.assertIn('lng', item)
            # Rounding might occur in the service, so compare with tolerance or ensure mock data uses expected precision
            if (
                abs(item['location']['lat'] - 10.0) < 0.0001
                and abs(item['location']['lng'] - 20.0) < 0.0001
                and item['weight'] == 2
                and item.get('categoria') is None
            ):
                found_loc1 = True
            if (
                abs(item['location']['lat'] - 11.0) < 0.0001
                and abs(item['location']['lng'] - 21.0) < 0.0001
                and item['weight'] == 1
                and item.get('categoria') is None
            ):
                found_loc2 = True

        self.assertTrue(found_loc1, "Location (10.0, 20.0) with weight 2 not found")
        self.assertTrue(found_loc2, "Location (11.0, 21.0) with weight 1 not found")

    def test_mapa_filtra_varias_categorias(self):
        from datetime import datetime

        DummyTicket = SimpleNamespace

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery(
                    [
                        t
                        for t in self
                        if all(getattr(t, k, None) == v for k, v in kwargs.items())
                    ]
                )

            def filter(self, *criterion):
                return self

            def all(self):
                return list(self)

        tickets = [
            DummyTicket(
                id=1,
                estado='nuevo',
                latitud=10.0,
                longitud=20.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria='Baches',
                asunto=None,
            ),
            DummyTicket(
                id=2,
                estado='nuevo',
                latitud=11.0,
                longitud=21.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria='Arbol',
                asunto=None,
            ),
            DummyTicket(
                id=3,
                estado='nuevo',
                latitud=12.0,
                longitud=22.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria='luminaria',
                asunto=None,
            ),
        ]

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True
            categoria = MagicMock()

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        DummyModel.query = DummyQuery(tickets)

        with patch.object(ts, 'MunicipioTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(
                tipo_ticket='municipio',
                municipio_id=5,
                categoria=['Baches', 'Luminaria'],
            )

        categorias = {item.get('categoria') for item in res}
        self.assertEqual(categorias, {'Baches', 'luminaria'})
        self.assertNotIn('Arbol', categorias)

    def test_preserves_user_provided_phone_when_user_exists(self):
        service = ServicioTickets()
        class DummyCreator:
            def __init__(self):
                self.last_ticket_data = None
            def create(self, td):
                self.last_ticket_data = td
                class DummyTicket(SimpleNamespace):
                    def __getattr__(self, name):
                        return None
                return DummyTicket(id=1, nro_ticket=123, asunto='a')

        dummy_creator = DummyCreator()
        service.creators['municipio'] = dummy_creator

        existing_user = SimpleNamespace(id=2, name='Existing', email='existing@example.com', telefono='2636160364')
        class DummyQuery:
            def filter(self, *args, **kwargs):
                return self
            def first(self):
                return existing_user

        models_stub.User.query = DummyQuery()

        ticket_data = {
            'email_vecino': 'existing@example.com',
            'telefono_vecino': '351122395',
            'categoria': 'General',
            'detalles': 'algo'
        }

        # This unit exercises phone preservation, while tenant ownership is
        # covered by the dedicated tenant-scope tests.
        with patch.object(
            ts,
            "normalize_municipio_ticket_write_scope",
            side_effect=lambda data: {**data, "tenant_id": 9, "municipio_id": 9},
        ):
            service.crear_nuevo_ticket('municipio', ticket_data)
        self.assertEqual(dummy_creator.last_ticket_data['telefono_vecino'], '351122395')

    def test_ticket_creation_log_excludes_citizen_pii_and_tracking_pin(self):
        service = ts.ServicioTickets()

        class DummyCreator:
            def create(self, ticket_data):
                class DummyTicket(SimpleNamespace):
                    def __getattr__(self, name):
                        return None

                return DummyTicket(
                    id=41,
                    nro_ticket=772401,
                    asunto="Poste caido frente a mi domicilio",
                    categoria="Luminaria",
                    estado="nuevo",
                    tenant_id=9,
                    municipio_id=9,
                    nombre_vecino="Persona Privada",
                    telefono_vecino="+5492613999999",
                    email_vecino="persona-privada@example.com",
                    dni_vecino="32999999",
                    consulta_pin="654321",
                )

        service.creators["municipio"] = DummyCreator()
        ticket_data = {
            "categoria": "Luminaria",
            "municipio_id": 9,
            "nombre_vecino": "Persona Privada",
            "telefono_vecino": "+5492613999999",
            "email_vecino": "persona-privada@example.com",
            "dni_vecino": "32999999",
            "consulta_pin": "654321",
        }

        with (
            patch.object(
                ts,
                "normalize_municipio_ticket_write_scope",
                side_effect=lambda data: {**data, "tenant_id": 9, "municipio_id": 9},
            ),
            patch.object(ts.logger, "info") as info_log,
        ):
            service.crear_nuevo_ticket("municipio", ticket_data)

        rendered_logs = " ".join(
            " ".join(str(value) for value in call.args)
            for call in info_log.call_args_list
        )
        self.assertIn("Ticket persisted", rendered_logs)
        for secret_value in (
            "Persona Privada",
            "+5492613999999",
            "persona-privada@example.com",
            "32999999",
            "654321",
            "Poste caido frente a mi domicilio",
        ):
            self.assertNotIn(secret_value, rendered_logs)


    def test_mapa_pyme_filtra_por_tenant(self):
        DummyTicket = SimpleNamespace
        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery([t for t in self if all(getattr(t, k, None) == v for k, v in kwargs.items())])
            def filter(self, *criterion):
                return self
            def all(self):
                return list(self)

        from datetime import datetime # Needed for fecha
        t1 = DummyTicket(id=1, estado='abierto', latitud=10.0, longitud=20.0, tenant_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t2 = DummyTicket(id=2, estado='abierto', latitud=11.0, longitud=21.0, tenant_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t3 = DummyTicket(id=3, estado='abierto', latitud=12.0, longitud=22.0, tenant_id=7, fecha=datetime.utcnow(), categoria=None, asunto=None)

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        query = DummyQuery([
            SimpleNamespace(id=1, estado='abierto', latitud=10.0, longitud=20.0, tenant_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=2, estado='abierto', latitud=11.0, longitud=21.0, tenant_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=3, estado='abierto', latitud=12.0, longitud=22.0, tenant_id=7, fecha=datetime.utcnow(), categoria=None, asunto=None)
        ])
        DummyModel.query = query
        with patch.object(ts, 'PymeTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(tipo_ticket='pyme', tenant_id=5)

        # Two points belong to tenant 5; tenant 7 must remain isolated.
        self.assertEqual(len(res), 2)
        self.assertTrue(
            any(
                'lat' in d
                and 'lng' in d
                and abs(d['location']['lat'] - 10.0) < 0.0001
                and abs(d['location']['lng'] - 20.0) < 0.0001
                and d['weight'] == 1
                and d.get('categoria') is None
                for d in res
            )
        )
        self.assertTrue(
            any(
                'lat' in d
                and 'lng' in d
                and abs(d['location']['lat'] - 11.0) < 0.0001
                and abs(d['location']['lng'] - 21.0) < 0.0001
                and d['weight'] == 1
                and d.get('categoria') is None
                for d in res
            )
        )


    def test_mapa_preserves_only_explicit_territory_through_aggregation(self):
        from datetime import datetime, timezone

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery(
                    [
                        ticket
                        for ticket in self
                        if all(
                            getattr(ticket, key, None) == value
                            for key, value in kwargs.items()
                        )
                    ]
                )

            def filter(self, *criterion):
                return self

            def all(self):
                return list(self)

        common = {
            "estado": "nuevo",
            "latitud": -34.5901,
            "longitud": -60.9492,
            "municipio_id": 5,
            "fecha": datetime.now(timezone.utc),
            "categoria": "Luminarias",
            "asunto": None,
        }
        tickets = [
            SimpleNamespace(
                id=1,
                distrito="Distrito Norte",
                datos_extra={
                    "barrio": "Los Olivos",
                    "localidad": "Junin",
                    "distrito": "Distrito desactualizado",
                },
                **common,
            ),
            SimpleNamespace(
                id=2,
                distrito="Distrito Norte",
                datos_extra={
                    "barrio": "Los Olivos",
                    "localidad": "Junin",
                    "distrito": "Distrito desactualizado",
                },
                **common,
            ),
            SimpleNamespace(
                id=3,
                distrito=None,
                # Free-form address-like values are not trusted territory.
                datos_extra={"direccion": "Barrio Inventado, Junin", "zone": "Centro"},
                **common,
            ),
            SimpleNamespace(
                id=4,
                distrito="Distrito Ajeno",
                datos_extra={"barrio": "Otro tenant", "localidad": "Otra ciudad"},
                **{**common, "municipio_id": 6},
            ),
        ]

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True
            distrito = MagicMock()

        DummyModel.query = DummyQuery(tickets)

        with patch.object(ts, "MunicipioTicket", DummyModel):
            result = ServicioTickets().obtener_tickets_con_ubicacion_para_mapa(
                tipo_ticket="municipio",
                municipio_id=5,
            )

        self.assertEqual(len(result), 2)
        labelled = next(point for point in result if point.get("barrio") == "Los Olivos")
        unlabelled = next(point for point in result if point.get("barrio") is None)
        self.assertEqual(labelled["weight"], 2)
        self.assertEqual(labelled["localidad"], "Junin")
        self.assertEqual(labelled["distrito"], "Distrito Norte")
        self.assertEqual(
            labelled["feature"]["properties"],
            {
                "weight": 2.0,
                "intensity": 1.0,
                "categoria": "Luminarias",
                "barrio": "Los Olivos",
                "localidad": "Junin",
                "distrito": "Distrito Norte",
            },
        )
        self.assertEqual(unlabelled["weight"], 1)
        for key in ("barrio", "localidad", "distrito"):
            self.assertNotIn(key, unlabelled)
            self.assertNotIn(key, unlabelled["feature"]["properties"])
        self.assertNotIn("Otro tenant", repr(result))
        self.assertNotIn("Distrito desactualizado", repr(result))

    def test_mapa_pyme_territory_remains_isolated_by_tenant(self):
        from datetime import datetime, timezone

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery(
                    [
                        ticket
                        for ticket in self
                        if all(
                            getattr(ticket, key, None) == value
                            for key, value in kwargs.items()
                        )
                    ]
                )

            def filter(self, *criterion):
                return self

            def all(self):
                return list(self)

        tickets = [
            SimpleNamespace(
                id=1,
                estado="nuevo",
                latitud=-34.6001,
                longitud=-60.9602,
                tenant_id=5,
                fecha=datetime.now(timezone.utc),
                categoria="Soporte",
                datos_extra={
                    "barrio": "Centro",
                    "localidad": "Junin",
                    "distrito": "Distrito Comercial",
                },
            ),
            SimpleNamespace(
                id=2,
                estado="nuevo",
                latitud=-34.6001,
                longitud=-60.9602,
                tenant_id=7,
                fecha=datetime.now(timezone.utc),
                categoria="Soporte",
                datos_extra={
                    "barrio": "Barrio privado ajeno",
                    "localidad": "Otra ciudad",
                    "distrito": "Otro distrito",
                },
            ),
        ]

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True

        DummyModel.query = DummyQuery(tickets)

        with patch.object(ts, "PymeTicket", DummyModel):
            result = ServicioTickets().obtener_tickets_con_ubicacion_para_mapa(
                tipo_ticket="pyme",
                tenant_id=5,
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["barrio"], "Centro")
        self.assertEqual(result[0]["localidad"], "Junin")
        self.assertEqual(result[0]["distrito"], "Distrito Comercial")
        self.assertNotIn("Barrio privado ajeno", repr(result))

    def test_mapa_district_filter_uses_the_same_explicit_territory_contract(self):
        from datetime import datetime, timezone

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery(
                    [
                        ticket
                        for ticket in self
                        if all(
                            getattr(ticket, key, None) == value
                            for key, value in kwargs.items()
                        )
                    ]
                )

            def filter(self, *criterion):
                return self

            def all(self):
                return list(self)

        tickets = [
            SimpleNamespace(
                id=1,
                estado="nuevo",
                latitud=-34.6001,
                longitud=-60.9602,
                tenant_id=5,
                fecha=datetime.now(timezone.utc),
                categoria="Soporte",
                datos_extra={"distrito": "Norte"},
            ),
            SimpleNamespace(
                id=2,
                estado="nuevo",
                latitud=-34.6011,
                longitud=-60.9612,
                tenant_id=5,
                fecha=datetime.now(timezone.utc),
                categoria="Soporte",
                datos_extra={"distrito": "Sur"},
            ),
        ]

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True

        DummyModel.query = DummyQuery(tickets)

        with patch.object(ts, "PymeTicket", DummyModel):
            result = ServicioTickets().obtener_tickets_con_ubicacion_para_mapa(
                tipo_ticket="pyme",
                tenant_id=5,
                distrito="Norte",
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["distrito"], "Norte")
        self.assertNotIn("Sur", repr(result))

    def test_mapa_district_sql_prefilter_has_no_whitespace_false_negatives(self):
        from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, create_engine, select
        from sqlalchemy.dialects import postgresql

        metadata = MetaData()
        ticket_table = Table(
            "ticket_map_filter_test",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("distrito", String(100), nullable=True),
            Column("datos_extra", JSON, nullable=True),
        )

        class SqlModel:
            __table__ = ticket_table

        predicate = ts._explicit_ticket_district_sql_predicate(SqlModel, "Norte")
        self.assertIsNotNone(predicate)

        engine = create_engine("sqlite://")
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                ticket_table.insert(),
                [
                    {
                        "id": 1,
                        "distrito": "\tNorte\n",
                        "datos_extra": {"distrito": "Sur"},
                    },
                    {
                        "id": 2,
                        "distrito": None,
                        "datos_extra": {"distrito": "\u00a0Norte\u2003"},
                    },
                    {
                        "id": 3,
                        "distrito": "Sur",
                        "datos_extra": {"distrito": "Norte"},
                    },
                    {
                        "id": 4,
                        "distrito": "\r\n\t",
                        "datos_extra": {"distrito": " Norte\t"},
                    },
                    {
                        "id": 5,
                        "distrito": None,
                        "datos_extra": {"direccion": "Distrito Norte"},
                    },
                ],
            )
            matching_ids = connection.execute(
                select(ticket_table.c.id)
                .where(predicate)
                .order_by(ticket_table.c.id)
            ).scalars().all()

        # ID 3 is an intentional SQL false positive: direct territory has
        # precedence over JSON and the exact Python check removes it. A
        # conservative prefilter must never remove IDs 1, 2 or 4 first.
        self.assertEqual(matching_ids, [1, 2, 3, 4])
        exact_rows = [
            SimpleNamespace(distrito="\tNorte\n", datos_extra={"distrito": "Sur"}),
            SimpleNamespace(distrito=None, datos_extra={"distrito": "\u00a0Norte\u2003"}),
            SimpleNamespace(distrito="Sur", datos_extra={"distrito": "Norte"}),
            SimpleNamespace(distrito="\r\n\t", datos_extra={"distrito": " Norte\t"}),
        ]
        exact_ids = [
            index
            for index, row in enumerate(exact_rows, start=1)
            if ts._explicit_ticket_territory(row).get("distrito") == "Norte"
        ]
        self.assertEqual(exact_ids, [1, 2, 4])
        compiled_postgres = str(
            predicate.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("datos_extra", compiled_postgres)
        self.assertIn("like", compiled_postgres.lower())

    def test_estado_resuelto_includes_cerrado(self):
        from datetime import datetime

        DummyTicket = SimpleNamespace

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery(
                    [
                        t
                        for t in self
                        if all(getattr(t, k, None) == v for k, v in kwargs.items())
                    ]
                )

            def filter(self, *criterion):
                return self

            def all(self):
                return list(self)

        tickets = [
            DummyTicket(
                id=1,
                estado='cerrado',
                latitud=10.0,
                longitud=20.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria=None,
                asunto=None,
                distrito='Junin',
            ),
            DummyTicket(
                id=2,
                estado='resuelto',
                latitud=11.0,
                longitud=21.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria=None,
                asunto=None,
                distrito='Junin',
            ),
            DummyTicket(
                id=3,
                estado='en_proceso',
                latitud=12.0,
                longitud=22.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria=None,
                asunto=None,
                distrito='Junin',
            ),
        ]

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True
            estado = MagicMock()
            estado.in_ = MagicMock(return_value=True)
            distrito = MagicMock()

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        DummyModel.query = DummyQuery(tickets)

        with patch.object(ts, 'MunicipioTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(
                tipo_ticket='municipio', municipio_id=5, estado='resuelto'
            )

        self.assertEqual(len(res), 2)
        self.assertFalse(
            any(
                abs(item['location']['lat'] - 12.0) < 0.0001
                and abs(item['location']['lng'] - 22.0) < 0.0001
                for item in res
            )
        )

    def test_distrito_filter_trims_value(self):
        from datetime import datetime

        DummyTicket = SimpleNamespace

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery(
                    [
                        t
                        for t in self
                        if all(getattr(t, k, None) == v for k, v in kwargs.items())
                    ]
                )

            def filter(self, *criterion):
                return self

            def all(self):
                return list(self)

        tickets = [
            DummyTicket(
                id=1,
                estado='nuevo',
                latitud=10.0,
                longitud=20.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria=None,
                asunto=None,
                distrito='Junin',
            ),
            DummyTicket(
                id=2,
                estado='nuevo',
                latitud=11.0,
                longitud=21.0,
                municipio_id=5,
                fecha=datetime.utcnow(),
                categoria=None,
                asunto=None,
                distrito='Godoy Cruz',
            ),
        ]

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True
            estado = MagicMock()
            estado.in_ = MagicMock(return_value=True)
            distrito = MagicMock()

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        DummyModel.query = DummyQuery(tickets)

        with patch.object(ts, 'MunicipioTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(
                tipo_ticket='municipio', municipio_id=5, distrito=' Junin '
            )

        self.assertEqual(len(res), 1)
        self.assertTrue(
            abs(res[0]['location']['lat'] - 10.0) < 0.0001
            and abs(res[0]['location']['lng'] - 20.0) < 0.0001
        )


if __name__ == '__main__':
    unittest.main()
