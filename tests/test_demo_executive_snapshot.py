from __future__ import annotations

import json
import os
import unittest

from sqlalchemy import event

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, User
from routes.v2.demo import _stable_demo_chat_session_id
from routes.v2.tenants import create_demo_session_token


class ExecutiveSnapshotTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class DemoExecutiveSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(ExecutiveSnapshotTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _without_volatile_fields(payload):
        if isinstance(payload, dict):
            return {
                key: DemoExecutiveSnapshotTest._without_volatile_fields(value)
                for key, value in payload.items()
                if key not in {"request_id", "server_time", "inicio_at", "fin_at"}
            }
        if isinstance(payload, list):
            return [DemoExecutiveSnapshotTest._without_volatile_fields(value) for value in payload]
        return payload

    def _tenant(self, slug: str):
        owner = User(
            name=f"Municipio {slug}",
            email=f"{slug}@executive-demo.test",
            password_hash="hash",
            tipo_chat="municipio",
            rol="admin",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=slug,
            nombre=f"Municipio {slug}",
            tipo="municipio",
            municipio_id=owner.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.commit()
        return owner, tenant

    def test_standard_admin_preview_remains_backward_compatible(self):
        response = self.client.get(
            "/api/v2/demo/admin-preview",
            query_string={"sector": "gobierno", "tenant_slug": "municipio"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.admin_preview.v1")
        self.assertNotIn("presentation_mode", payload)
        self.assertNotIn("data_provenance", payload)
        self.assertNotIn("cases", payload)
        self.assertNotIn("channel_summary", payload)
        self.assertEqual(payload.get("metrics"), [])
        self.assertFalse((payload.get("map") or {}).get("enabled"))

    def test_executive_mode_returns_deterministic_truth_labeled_junin_scenario(self):
        query = {
            "sector": "gobierno",
            "tenant_slug": "junin-ejecutivo",
            "presentation_mode": "executive",
        }
        first = self.client.get("/api/v2/demo/admin-preview", query_string=query)
        second = self.client.get("/api/v2/demo/admin-preview", query_string=query)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payload = first.get_json()
        self.assertEqual(
            self._without_volatile_fields(payload),
            self._without_volatile_fields(second.get_json()),
        )
        self.assertEqual(payload.get("contract_version"), "demo.admin_preview.v1")
        self.assertEqual(payload.get("presentation_mode"), "executive")
        self.assertEqual(
            (payload.get("frontend_contract") or {}).get("presentation_mode"),
            "executive",
        )

        provenance = payload.get("data_provenance") or {}
        self.assertEqual(
            provenance.get("contract_version"),
            "demo.executive_provenance.v1",
        )
        self.assertEqual(provenance.get("mode"), "synthetic_demo_scenario")
        self.assertTrue(provenance.get("synthetic"))
        self.assertTrue(provenance.get("contains_synthetic"))
        self.assertFalse(provenance.get("municipal_truth"))
        self.assertTrue(provenance.get("suitable_for_product_demonstration"))
        self.assertFalse(provenance.get("suitable_for_government_decisions"))
        self.assertEqual(provenance.get("tenant_scope"), "junin-ejecutivo")
        self.assertEqual(provenance.get("requested_tenant_slug"), "junin-ejecutivo")
        self.assertEqual(provenance.get("scenario_tenant_slug"), "junin")
        self.assertIn("No representa datos municipales reales", provenance.get("label") or "")
        self.assertNotIn(
            "2026-08-25T12:00:00+00:00",
            json.dumps(payload, ensure_ascii=False),
        )

        self.assertEqual(
            (payload.get("operations") or {}).get("data_policy"),
            "synthetic_demo_scenario",
        )
        self.assertFalse((payload.get("session_activity") or {}).get("has_session_data"))
        self.assertEqual((payload.get("session_activity") or {}).get("items"), [])
        self.assertEqual(len(payload.get("cards") or []), 4)
        self.assertEqual(len(payload.get("metrics") or []), 4)
        self.assertEqual(len(payload.get("timeline") or []), 5)
        self.assertEqual(len(payload.get("cases") or []), 5)
        self.assertEqual(len((payload.get("map") or {}).get("points") or []), 5)
        self.assertTrue((payload.get("map") or {}).get("enabled"))
        self.assertTrue((payload.get("map") or {}).get("sample"))
        self.assertEqual((payload.get("map") or {}).get("represented_cases"), 52)
        self.assertEqual((payload.get("case_sample") or {}).get("displayed_cases"), 5)
        self.assertEqual((payload.get("case_sample") or {}).get("total_cases"), 184)
        self.assertEqual(
            (payload.get("channel_summary") or {}).get("total_interactions"),
            598,
        )
        survey_items = (payload.get("survey_voting") or {}).get("items") or []
        self.assertTrue(survey_items)
        survey_results = survey_items[0].get("results") or {}
        survey_total = survey_results.get("total_respuestas")
        survey_options = survey_results.get("options") or []
        survey_top = max(survey_options, key=lambda item: item.get("count") or 0)
        survey_metric = next(
            item for item in (payload.get("metrics") or []) if item.get("id") == "survey_valid_votes"
        )
        survey_card = next(
            item for item in (payload.get("cards") or []) if item.get("id") == "survey_participation"
        )
        self.assertEqual(survey_metric.get("value"), survey_total)
        self.assertEqual(survey_card.get("value"), str(survey_total))
        self.assertIn(survey_top.get("label"), survey_metric.get("detail") or "")
        self.assertIn(f"{survey_top.get('porcentaje'):g}%", survey_metric.get("detail") or "")
        self.assertTrue(
            all(
                item.get("data_mode") == "synthetic_demo_scenario"
                for key in ("cards", "metrics", "timeline", "cases")
                for item in (payload.get(key) or [])
            )
        )

    def test_executive_snapshot_get_performs_no_database_writes(self):
        mutations = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
            operation = str(statement or "").lstrip().split(None, 1)[0].upper()
            if operation in {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}:
                mutations.append(statement)

        event.listen(db.engine, "before_cursor_execute", record_statement)
        try:
            response = self.client.get(
                "/api/v2/demo/admin-preview",
                query_string={
                    "sector": "gobierno",
                    "tenant_slug": "junin-read-only",
                    "presentation_mode": "executive",
                },
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", record_statement)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mutations, [])

    def test_synthetic_provenance_is_tenant_scoped_without_cross_tenant_content(self):
        first = self.client.get(
            "/api/v2/demo/admin-preview",
            query_string={
                "sector": "gobierno",
                "tenant_slug": "junin-tenant-a",
                "presentation_mode": "executive",
            },
        ).get_json()
        second = self.client.get(
            "/api/v2/demo/admin-preview",
            query_string={
                "sector": "gobierno",
                "tenant_slug": "junin-tenant-b",
                "presentation_mode": "executive",
            },
        ).get_json()

        self.assertEqual(first.get("tenant_slug"), "junin-tenant-a")
        self.assertEqual(second.get("tenant_slug"), "junin-tenant-b")
        self.assertEqual(
            (first.get("data_provenance") or {}).get("tenant_scope"),
            "junin-tenant-a",
        )
        self.assertEqual(
            (second.get("data_provenance") or {}).get("tenant_scope"),
            "junin-tenant-b",
        )
        self.assertEqual(
            (first.get("data_provenance") or {}).get("scenario_tenant_slug"),
            "junin",
        )
        self.assertEqual(
            (second.get("data_provenance") or {}).get("scenario_tenant_slug"),
            "junin",
        )
        self.assertNotIn("junin-tenant-b", json.dumps(first, ensure_ascii=False))
        self.assertNotIn("junin-tenant-a", json.dumps(second, ensure_ascii=False))

    def test_non_junin_request_keeps_requested_tenant_separate_from_demo_scenario(self):
        payload = self.client.get(
            "/api/v2/demo/admin-preview",
            query_string={
                "sector": "gobierno",
                "tenant_slug": "ushuaia",
                "presentation_mode": "executive",
            },
        ).get_json()

        provenance = payload.get("data_provenance") or {}
        self.assertEqual(payload.get("tenant_slug"), "ushuaia")
        self.assertEqual(provenance.get("requested_tenant_slug"), "ushuaia")
        self.assertEqual(provenance.get("scenario_tenant_slug"), "junin")
        self.assertEqual(provenance.get("scenario_scope"), "Junín, Mendoza")
        self.assertIn("Junín", provenance.get("label") or "")
        self.assertTrue(
            all(
                str(item.get("case_code") or "").startswith("JN-DEMO-")
                for item in (payload.get("cases") or [])
            )
        )

    def test_valid_session_activity_is_preserved_without_synthetic_blending(self):
        owner, tenant = self._tenant("junin-session-real")
        demo_session_id = create_demo_session_token(
            tenant_slug=tenant.slug,
            sector="gobierno",
            rubro="gobierno-junin",
        )
        chat_session_id = _stable_demo_chat_session_id(demo_session_id)
        ticket = MunicipioTicket(
            tenant_id=tenant.id,
            municipio_id=owner.id,
            user_id=owner.id,
            nro_ticket="REAL-DEMO-7101",
            consulta_pin="pin-real-demo-7101",
            pregunta="Reclamo generado durante esta sesión demo",
            asunto="Luminaria reportada en la sesión",
            categoria="Alumbrado público",
            estado="nuevo",
            canal_ingreso="web_demo_widget",
            direccion="Ubicación aportada en la sesión",
            latitud=-34.585,
            longitud=-60.955,
            detalles=json.dumps(
                {
                    "demo_runtime": True,
                    "source": "demo_municipio_runtime",
                    "chat_session_id": chat_session_id,
                }
            ),
        )
        db.session.add(ticket)
        db.session.commit()

        mutations = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
            operation = str(statement or "").lstrip().split(None, 1)[0].upper()
            if operation in {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}:
                mutations.append(statement)

        event.listen(db.engine, "before_cursor_execute", record_statement)
        try:
            response = self.client.get(
                "/api/v2/demo/admin-preview",
                query_string={
                    "sector": "gobierno",
                    "tenant_slug": tenant.slug,
                    "demo_session_id": demo_session_id,
                    "chat_session_id": chat_session_id,
                    "presentation_mode": "executive",
                },
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", record_statement)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mutations, [])
        payload = response.get_json()
        provenance = payload.get("data_provenance") or {}
        self.assertEqual(provenance.get("mode"), "mixed_partitioned")
        self.assertFalse(provenance.get("synthetic"))
        self.assertTrue(provenance.get("contains_synthetic"))
        self.assertFalse(provenance.get("municipal_truth"))
        self.assertEqual(
            (payload.get("operations") or {}).get("data_policy"),
            "session_events_only",
        )
        self.assertEqual((payload.get("channel_summary") or {}).get("total_cases"), 1)
        self.assertEqual((payload.get("channel_summary") or {}).get("observed_cases"), 1)
        self.assertIsNone((payload.get("channel_summary") or {}).get("total_interactions"))
        self.assertTrue((payload.get("session_activity") or {}).get("has_session_data"))
        self.assertEqual(payload.get("metrics"), [])
        self.assertEqual((payload.get("cards") or [])[0].get("value"), "1")
        self.assertEqual(
            [(item.get("ticket_code")) for item in ((payload.get("map") or {}).get("points") or [])],
            ["REAL-DEMO-7101"],
        )
        self.assertEqual(
            [item.get("case_code") for item in (payload.get("cases") or [])],
            ["REAL-DEMO-7101"],
        )
        executive_projection = json.dumps(
            {
                key: payload.get(key)
                for key in (
                    "cards",
                    "metrics",
                    "timeline",
                    "map",
                    "cases",
                    "channel_summary",
                    "session_activity",
                )
            },
            ensure_ascii=False,
        )
        self.assertNotIn("synthetic_demo_scenario", executive_projection)
        self.assertNotIn("JN-DEMO-", executive_projection)
        self.assertNotIn('"value": 184', executive_projection)
        partitions = provenance.get("source_partitions") or []
        self.assertEqual(
            {partition.get("mode") for partition in partitions},
            {"session_generated_events", "synthetic_demo_scenario"},
        )


if __name__ == "__main__":
    unittest.main()
