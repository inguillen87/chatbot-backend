import unittest
from unittest.mock import patch
import importlib
import json
import os
import sys
from types import SimpleNamespace, ModuleType

from flask import Flask


project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig


class EstadisticasHeatmapRouteTest(unittest.TestCase):
    def setUp(self):
        self.original_auth_helpers = sys.modules.get('utils.auth_helpers')
        if self.original_auth_helpers is None:
            stub = ModuleType('utils.auth_helpers')
            stub.token_requerido = lambda f: f
            stub.admin_o_empleado_requerido = lambda f: f
            stub.anon_o_token_requerido = lambda f: f
            sys.modules['utils.auth_helpers'] = stub
            self._stubbed_auth_helpers = True
        else:
            self._stubbed_auth_helpers = False

        # Bypass authentication decorators for testing by patching source module
        self.token_patcher = patch('utils.auth_helpers.token_requerido', lambda f: f)
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', lambda f: f)
        self.session_patcher = patch(
            'flask_session.Session',
            lambda *args, **kwargs: SimpleNamespace(init_app=lambda app: None),
        )
        self.token_patcher.start()
        self.admin_patcher.start()
        self.session_patcher.start()

        import routes.estadisticas as estats
        importlib.reload(estats)
        self.app = Flask(__name__)
        self.app.config.from_object(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        # These auth-mocked tests exercise payload projection, not admission.
        # Give the handler an explicit synthetic scope and mock that boundary.
        self.payload_tenant_id = 101
        self.payload_owner_id = 301
        self.scope_patcher = patch(
            'routes.estadisticas._resolve_ticket_stats_tenant',
            side_effect=lambda _actor, _args, tipo: (
                SimpleNamespace(id=self.payload_tenant_id, tipo=tipo, is_active=True,
                                municipio_id=self.payload_owner_id if tipo == 'municipio' else None,
                                pyme_id=None, pyme=None),
                True,
            ),
        )
        self.tenant_access_patcher = patch(
            'routes.estadisticas._stats_tenant_access_allowed', return_value=True,
        )
        self.scope_patcher.start()
        self.tenant_access_patcher.start()

        # Payload-only stub: enforce the exact new internal service signature.
        # The independent client/DB scope test below does not use this class.
        def payload_stats(municipio_id, *, tenant_id, filters=None, actor=None):
            self.assertEqual(municipio_id, self.payload_owner_id)
            self.assertEqual(tenant_id, self.payload_tenant_id)
            return {"resumen": {}}
        self.stats_patcher = patch('routes.estadisticas.build_stats_for_municipio',
                                   side_effect=payload_stats)
        self.stats_patcher.start()

    def tearDown(self):
        self.stats_patcher.stop()
        self.tenant_access_patcher.stop()
        self.scope_patcher.stop()
        self.token_patcher.stop()
        self.admin_patcher.stop()
        self.session_patcher.stop()
        self.app_context.pop()
        if self._stubbed_auth_helpers:
            sys.modules.pop('utils.auth_helpers', None)
        elif self.original_auth_helpers is not None:
            sys.modules['utils.auth_helpers'] = self.original_auth_helpers
        import routes.estadisticas as estats
        importlib.reload(estats)

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_datos(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": 1, "lng": 2}, "weight": 3, "categoria": None}
        ]
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo_ticket=municipio'):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        self.assertEqual(len(payload.get("heatmap", [])), 1)
        point = payload["heatmap"][0]
        self.assertEqual(point["location"], {"lat": 1, "lng": 2})
        self.assertEqual(point["weight"], 3.0)
        self.assertIn("feature", point)
        self.assertIn("coordinates", point)
        self.assertIn("heatmap_geojson", payload)
        self.assertNotIn("heatmap_google", payload)
        self.assertIn("map_config", payload)
        self.assertIsInstance(payload["map_config"], dict)
        self.assertIn("map_layers", payload)
        heatmap_layer = payload["map_layers"].get("heatmap")
        self.assertIsInstance(heatmap_layer, dict)
        self.assertEqual(heatmap_layer.get("preferred_format"), "geojson")
        self.assertIn("geojson", heatmap_layer.get("supported_formats", []))
        self.assertIn("source_keys", heatmap_layer)
        self.assertIn("heatmap_cells", payload)
        self.assertTrue(payload["heatmap_cells"])
        self.assertIn("heatmap_cells_geojson", payload)
        self.assertIn("heatmap_cells", payload.get("map_layers", {}))
        grid_layer = payload["map_layers"].get("heatmap_cells")
        self.assertIsInstance(grid_layer, dict)
        self.assertEqual(grid_layer.get("kind"), "grid")
        self.assertIn("geojson", grid_layer.get("supported_formats", []))
        self.assertIn("metadata", payload)
        self.assertIn("map", payload["metadata"])
        self.assertIn("heatmap", payload["metadata"]["map"])
        heatmap_meta = payload["metadata"]["map"]["heatmap"]
        self.assertIsInstance(heatmap_meta, dict)
        self.assertIn("point_count", heatmap_meta)
        self.assertIn("cell_count", heatmap_meta)
        self.assertIn("provider_hint", heatmap_meta)
        self.assertIn("style", heatmap_meta)
        self.assertIn("category_layers", payload["metadata"])
        category_layers = payload["metadata"]["category_layers"]
        self.assertEqual(category_layers.get("provider"), "maplibre")
        self.assertIn("categories", category_layers)
        filters_meta = payload["metadata"].get("filters", {})
        self.assertIn("rangos_tiempo", filters_meta)
        self.assertTrue(filters_meta.get("rangos_tiempo"))
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            actor=None,
            municipio_id=self.payload_owner_id,
            rubro_id=None,
            tenant_id=self.payload_tenant_id,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=None,
            satisfactorio=None,
        )

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_multiple_estados(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/mapa_calor/datos?tipo_ticket=municipio&estado=nuevo&estado=en_vivo'
        ):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            actor=None,
            municipio_id=self.payload_owner_id,
            rubro_id=None,
            tenant_id=self.payload_tenant_id,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=['nuevo', 'en_vivo'],
            satisfactorio=None,
        )

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_acepta_tipo_pyme_del_frontend(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo=pyme'):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='pyme',
            actor=None,
            municipio_id=None,
            rubro_id=None,
            tenant_id=self.payload_tenant_id,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=None,
            satisfactorio=None,
        )

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_rechaza_tipos_en_conflicto(self, mock_servicio):
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/mapa_calor/datos?tipo=pyme&tipo_ticket=municipio'
        ):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'bad_request')
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_not_called()

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_rechaza_tipo_desconocido(self, mock_servicio):
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo=otro'):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'bad_request')
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_not_called()

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_acepta_varias_categorias(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/mapa_calor/datos?tipo_ticket=municipio&categoria=Arbol&categoria=Luminaria'
        ):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            actor=None,
            municipio_id=self.payload_owner_id,
            rubro_id=None,
            tenant_id=self.payload_tenant_id,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=['Arbol', 'Luminaria'],
            distrito=None,
            estado=None,
            satisfactorio=None,
        )

    @patch('routes.estadisticas.build_stats_for_municipio', return_value={"resumen": {}})
    @patch('routes.estadisticas.servicio_tickets')
    def test_employee_heatmap_is_category_scoped_rounded_and_k_safe(
        self,
        mock_servicio,
        _mock_stats,
    ):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {
                "location": {"lat": -34.60011, "lng": -68.30011},
                "weight": 2,
                "categoria": "Luminarias",
            },
            {
                "location": {"lat": -34.60024, "lng": -68.30024},
                "weight": 3,
                "categoria": "Luminarias",
            },
            {
                "location": {"lat": -34.61011, "lng": -68.31011},
                "weight": 1,
                "categoria": "Luminarias",
            },
            {
                "location": {"lat": -34.62011, "lng": -68.32011},
                "weight": 20,
                "categoria": "Baches",
            },
        ]
        employee = SimpleNamespace(
            id=41,
            rol="empleado",
            es_empleado=True,
            ticket_categorias="Luminarias",
            categorias_ticket=[],
            accesibilidad={},
            tenant_id=None,
            empresa_id=None,
            municipio_id=None,
            tipo_chat=None,
        )

        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo_ticket=municipio'):
            response = estats.mapa_calor_datos(current_user=employee)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload.get("privacy") or {}).get("mode"), "employee_aggregated")
        self.assertEqual((payload.get("privacy") or {}).get("k_min"), 5)
        self.assertEqual(
            (payload.get("privacy") or {}).get("coordinate_precision_decimals"),
            3,
        )
        self.assertEqual(len(payload.get("heatmap") or []), 1)
        point = payload["heatmap"][0]
        self.assertEqual(point["location"], {"lat": -34.6, "lng": -68.3})
        self.assertEqual(point["weight"], 5.0)
        self.assertEqual(point["categoria"], "Luminarias")
        self.assertNotEqual(point["lat"], -34.60011)
        self.assertNotEqual(point["lng"], -68.30011)
        self.assertEqual(
            (payload.get("privacy") or {}).get("suppressed", {}).get("records"),
            1,
        )
        self.assertEqual(
            (payload.get("render_contract") or {}).get("privacy_mode"),
            "employee_aggregated",
        )
        serialized = json.dumps(payload).lower()
        self.assertNotIn("baches", serialized)
        self.assertNotIn("-34.61011", serialized)
        self.assertNotIn("-68.31011", serialized)

    @patch('routes.estadisticas.build_stats_for_municipio', return_value={"resumen": {}})
    @patch('routes.estadisticas.servicio_tickets')
    def test_admin_heatmap_preserves_exact_singleton_behavior(
        self,
        mock_servicio,
        _mock_stats,
    ):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {
                "location": {"lat": -34.60011, "lng": -68.30011},
                "weight": 1,
                "categoria": "Luminarias",
            }
        ]
        admin = SimpleNamespace(
            id=40,
            rol="admin",
            es_empleado=False,
            tenant_id=None,
            empresa_id=None,
            municipio_id=None,
            tipo_chat=None,
        )

        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo_ticket=municipio'):
            response = estats.mapa_calor_datos(current_user=admin)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("privacy", payload)
        self.assertEqual(payload["heatmap"][0]["lat"], -34.60011)
        self.assertEqual(payload["heatmap"][0]["lng"], -68.30011)
        self.assertEqual(payload["heatmap"][0]["weight"], 1.0)


    @patch('routes.estadisticas.build_stats_for_municipio', return_value={"resumen": {}})
    @patch('routes.estadisticas.servicio_tickets')
    def test_employee_empty_category_scope_fails_closed(
        self,
        mock_servicio,
        _mock_stats,
    ):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {
                "location": {"lat": -34.612345, "lng": -68.312345},
                "weight": 99,
                "categoria": "Luminarias",
            }
        ]
        employee = SimpleNamespace(
            id=43,
            rol="empleado",
            es_empleado=True,
            ticket_categorias="",
            categorias_ticket=[],
            accesibilidad={"employee_scope": {"categorias": []}},
            tenant_id=None,
            empresa_id=None,
            municipio_id=None,
            tipo_chat=None,
        )

        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo_ticket=municipio'):
            response = estats.mapa_calor_datos(current_user=employee)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("heatmap"), [])
        self.assertEqual(
            (payload.get("privacy") or {}).get("category_scope"),
            "empty_fail_closed",
        )
        self.assertEqual(
            (payload.get("render_contract") or {}).get("empty_reason"),
            "employee_category_scope_empty",
        )
        self.assertNotIn("-34.612345", json.dumps(payload))


if __name__ == '__main__':
    unittest.main()

# This scope denial uses the ordinary application/client/DB fixture, rather
# than the class above whose authentication and services are payload stubs.
def test_employee_heatmap_rejects_foreign_tenant_before_query(client):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import event
    from models import MunicipioTicket, Role, TenantProfile, User, UserRole, db
    from services.auth_session_lifecycle import issue_token

    assert str(db.engine.url) == "sqlite:///:memory:"
    owners = []
    tenants = []
    for label in ("a", "b"):
        owner = User(name="Synthetic municipal owner", email="heatmap-owner-" + label + "@example.invalid",
                     rol="admin", tipo_chat="municipio")
        owner.set_password("synthetic-heatmap-only")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="heatmap-foreign-" + label, nombre="Synthetic municipality",
                               tipo="municipio", municipio_id=owner.id, is_active=True)
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
        owners.append(owner)
        tenants.append(tenant)
    employee = User(name="Synthetic exact employee", email="heatmap-employee@example.invalid",
                    rol="empleado", tipo_chat="municipio", es_empleado=True,
                    tenant_id=tenants[0].id, tenant_slug=tenants[0].slug,
                    municipio_id=owners[0].id,
                    accesibilidad={"employee_scope": {"categorias": ["synthetic-allowed"]}})
    employee.set_password("synthetic-heatmap-only")
    db.session.add(employee)
    db.session.flush()
    role = Role.query.filter_by(name="empleado").first()
    if role is None:
        role = Role(name="empleado")
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=employee.id, tenant_id=tenants[0].id, role_id=role.id))
    for tenant, owner, count, lat, lng in (
        (tenants[0], owners[0], 7, -33, -60), (tenants[1], owners[1], 11, -34, -61)):
        for _ in range(count):
            db.session.add(MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id,
                pregunta="Synthetic foreign-scope marker", categoria="synthetic-allowed",
                latitud=lat, longitud=lng))
    db.session.commit()
    token = issue_token({"user_id": employee.id, "rol": employee.rol, "tipo_chat": employee.tipo_chat,
                         "exp": datetime.now(timezone.utc) + timedelta(hours=1)})
    headers = {"Authorization": "Bearer " + token, "X-Tenant-Slug": tenants[0].slug}
    own = client.get("/estadisticas/mapa_calor/datos?tipo=municipio", headers=headers)
    assert own.status_code == 200
    assert sum(point["weight"] for point in own.get_json()["heatmap"]) == 7
    assert own.get_json()["privacy"]["mode"] == "employee_aggregated"

    reads = []
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if "select" in statement.lower() and "municipio_ticket" in statement.lower():
            reads.append(statement)
    event.listen(db.engine, "before_cursor_execute", record)
    try:
        headers["X-Tenant-Slug"] = tenants[1].slug
        denied = client.get("/estadisticas/mapa_calor/datos?tipo=municipio", headers=headers)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert denied.status_code == 403
    assert denied.get_json() == {"error": "tenant_forbidden",
                                 "reason_code": "metrics_tenant_scope_unavailable"}
    assert denied.headers["Cache-Control"] == "no-store"
    assert reads == []
