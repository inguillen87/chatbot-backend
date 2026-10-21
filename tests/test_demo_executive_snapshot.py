from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy import event

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, User
from routes.v2.demo import _stable_demo_chat_session_id
from routes.v2.tenants import create_demo_session_token
from services.demo_surveys import build_demo_public_survey_payload
import services.demo_survey_participation as demo_participation


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
        metrics_by_id = {
            item.get("id"): item for item in (payload.get("metrics") or [])
        }
        self.assertEqual(
            (metrics_by_id.get("claims_received") or {}).get("denominator"),
            {"label": "Casos del escenario", "value": 184},
        )
        self.assertEqual(
            (metrics_by_id.get("sla_compliance_pct") or {}).get("numerator"),
            {"label": "Casos dentro del objetivo", "value": 160},
        )
        self.assertEqual(
            (metrics_by_id.get("sla_compliance_pct") or {}).get("denominator"),
            {"label": "Casos con SLA evaluado", "value": 184},
        )
        self.assertEqual(
            (metrics_by_id.get("whatsapp_first_response_minutes") or {}).get("denominator"),
            {"label": "Conversaciones WhatsApp", "value": 326},
        )
        self.assertEqual(len(payload.get("timeline") or []), 5)
        self.assertEqual(len(payload.get("cases") or []), 5)
        self.assertEqual(len((payload.get("map") or {}).get("points") or []), 5)
        map_points = {
            point.get("id"): (point.get("lat"), point.get("lng"))
            for point in ((payload.get("map") or {}).get("points") or [])
        }
        # These two southern samples previously crossed the official Junin-Rivadavia
        # boundary. Pin the independently verified in-jurisdiction coordinates.
        self.assertEqual(map_points.get("synthetic-junin-03"), (-33.1463, -68.4786))
        self.assertEqual(map_points.get("synthetic-junin-04"), (-33.148, -68.4899))
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
        self.assertEqual(
            survey_metric.get("denominator"),
            {"label": "Respuestas incluidas", "value": survey_total},
        )
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

    def test_executive_snapshot_reconciles_seed_and_durable_demo_participation(self):
        query = {
            "sector": "gobierno",
            "tenant_slug": "junin",
            "presentation_mode": "executive",
        }
        baseline = self.client.get(
            "/api/v2/demo/admin-preview",
            query_string=query,
        ).get_json()
        baseline_item = ((baseline.get("survey_voting") or {}).get("items") or [])[0]
        slug = baseline_item.get("slug")
        public_survey = build_demo_public_survey_payload(slug)
        self.assertIsNotNone(public_survey)
        question = (public_survey.get("preguntas") or [])[0]
        selected_option = (question.get("opciones") or [])[0]
        enabled_gate = {
            "contract_version": "demo.survey.participation_gate.v1",
            "enabled": True,
            "reason": "enabled",
        }

        with patch.object(
            demo_participation,
            "demo_survey_participation_gate",
            return_value=enabled_gate,
        ):
            for submission_id in (
                "018f4c8e-1e56-7f38-a4df-83fd68394870",
                "018f4c8e-1e56-7f38-a4df-83fd68394871",
            ):
                demo_participation.persist_demo_survey_participation(
                    slug,
                    {
                        "submission_id": submission_id,
                        "source": "preview_e2e",
                        "respuestas": [
                            {
                                "pregunta_id": question.get("id"),
                                "opcion_id": selected_option.get("id"),
                            }
                        ],
                    },
                    submission_id=submission_id,
                )

            mutations = []

            def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
                operation = str(statement or "").lstrip().split(None, 1)[0].upper()
                if operation in {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}:
                    mutations.append(statement)

            with patch.object(
                demo_participation,
                "build_durable_demo_live_results_payload",
                wraps=demo_participation.build_durable_demo_live_results_payload,
            ) as durable_live_read:
                event.listen(db.engine, "before_cursor_execute", record_statement)
                try:
                    response = self.client.get(
                        "/api/v2/demo/admin-preview",
                        query_string=query,
                    )
                finally:
                    event.remove(db.engine, "before_cursor_execute", record_statement)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mutations, [])
        payload = response.get_json()
        voting = payload.get("survey_voting") or {}
        self.assertTrue(voting.get("durable_demo_participation"))
        self.assertFalse(voting.get("municipal_truth"))
        self.assertEqual(voting.get("verified_citizen_responses"), 0)
        items = voting.get("items") or []
        all_items = voting.get("all_items") or []
        self.assertGreaterEqual(len(items), 2)
        unique_slugs = {
            entry.get("slug")
            for entry in [*items, *all_items]
            if entry.get("slug")
        }
        self.assertEqual(durable_live_read.call_count, len(unique_slugs))
        self.assertEqual(len(items), 5)
        self.assertEqual(len(all_items), voting.get("total_available"))
        self.assertEqual(len(unique_slugs), voting.get("total_available"))
        items_by_slug = {entry.get("slug"): entry for entry in items}
        all_items_by_slug = {entry.get("slug"): entry for entry in all_items}
        self.assertTrue(set(items_by_slug).issubset(all_items_by_slug))
        for item_slug, visible_item in items_by_slug.items():
            complete_item = all_items_by_slug[item_slug]
            self.assertEqual(
                {
                    key: visible_item.get(key)
                    for key in (
                        "seeded_responses",
                        "interactive_demo_responses",
                        "total_respuestas",
                        "verified_citizen_responses",
                    )
                },
                {
                    key: complete_item.get(key)
                    for key in (
                        "seeded_responses",
                        "interactive_demo_responses",
                        "total_respuestas",
                        "verified_citizen_responses",
                    )
                },
            )
        item = items[0]
        results = item.get("results") or {}
        for source in (item, results, item.get("analytics_summary") or {}):
            self.assertEqual(source.get("seeded_responses"), 100)
            self.assertEqual(source.get("interactive_demo_responses"), 2)
            self.assertEqual(source.get("verified_citizen_responses"), 0)
        self.assertEqual(item.get("total_respuestas"), 102)
        self.assertEqual(results.get("total_respuestas"), 102)
        self.assertEqual((item.get("analytics_summary") or {}).get("responses"), 102)
        self.assertEqual(results.get("segment_scope"), "seeded_synthetic_responses_only")
        self.assertEqual(results.get("unsegmented_interactive_demo_responses"), 2)
        self.assertFalse(item.get("municipal_truth"))
        self.assertEqual(
            ((item.get("demo_data_composition") or {}).get("verified_citizen_responses")),
            0,
        )
        untouched_item = items[1]
        self.assertEqual(untouched_item.get("seeded_responses"), 100)
        self.assertEqual(untouched_item.get("interactive_demo_responses"), 0)
        self.assertEqual(untouched_item.get("total_respuestas"), 100)

        survey_card = next(
            entry for entry in (payload.get("cards") or [])
            if entry.get("id") == "survey_participation"
        )
        survey_metric = next(
            entry for entry in (payload.get("metrics") or [])
            if entry.get("id") == "survey_valid_votes"
        )
        survey_timeline = next(
            entry for entry in (payload.get("timeline") or [])
            if entry.get("channel") == "survey"
        )
        expected_narrative = (
            "100 base sintética + 2 participaciones demo = 102 total; "
            "0 respuestas ciudadanas verificadas"
        )
        self.assertEqual(survey_card.get("value"), "102")
        self.assertIn(expected_narrative, survey_card.get("detail") or "")
        self.assertEqual(survey_metric.get("value"), 102)
        self.assertIn(expected_narrative, survey_metric.get("detail") or "")
        self.assertIn(expected_narrative, survey_timeline.get("detail") or "")
        for source in (survey_card, survey_metric, survey_timeline):
            self.assertEqual(source.get("seeded_responses"), 100)
            self.assertEqual(source.get("interactive_demo_responses"), 2)
            self.assertEqual(source.get("total_respuestas"), 102)
            self.assertEqual(source.get("verified_citizen_responses"), 0)

    def test_admin_preview_preserves_truthful_seed_if_durable_read_fails(self):
        query = {
            "sector": "gobierno",
            "tenant_slug": "junin",
            "presentation_mode": "executive",
        }
        with patch.object(
            demo_participation,
            "durable_demo_survey_participation_enabled",
            return_value=True,
        ), patch.object(
            demo_participation,
            "enrich_demo_survey_voting_with_durable_participation",
            side_effect=RuntimeError("sensitive database diagnostic"),
        ):
            response = self.client.get(
                "/api/v2/demo/admin-preview",
                query_string=query,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("sensitive database diagnostic", json.dumps(payload))
        voting = payload.get("survey_voting") or {}
        self.assertFalse(voting.get("durable_demo_participation"))
        self.assertEqual(
            voting.get("durable_demo_participation_state"),
            "read_unavailable_synthetic_baseline",
        )
        self.assertFalse(voting.get("municipal_truth"))
        self.assertEqual(voting.get("verified_citizen_responses"), 0)
        item = (voting.get("items") or [])[0]
        self.assertNotIn("interactive_demo_responses", item)
        self.assertEqual((item.get("results") or {}).get("seeded_responses"), 100)
        self.assertEqual((item.get("results") or {}).get("total_respuestas"), 100)
        survey_card = next(
            entry for entry in (payload.get("cards") or [])
            if entry.get("id") == "survey_participation"
        )
        self.assertEqual(survey_card.get("value"), "100")
        self.assertIn("Base sintética", survey_card.get("detail") or "")

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

    def test_valid_session_activity_exposes_partitioned_auditable_metrics(self):
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
                    "media": [
                        {"id": "photo-7101-a"},
                        {"id": "photo-7101-b"},
                    ],
                }
            ),
        )
        ticket_without_location = MunicipioTicket(
            tenant_id=tenant.id,
            municipio_id=owner.id,
            user_id=owner.id,
            nro_ticket="REAL-DEMO-7102",
            consulta_pin="pin-real-demo-7102",
            pregunta="Segundo reclamo generado durante esta sesión demo",
            asunto="Bache reportado en la sesión",
            categoria="Calles",
            estado="nuevo",
            canal_ingreso="web_demo_widget",
            detalles=json.dumps(
                {
                    "demo_runtime": True,
                    "source": "demo_municipio_runtime",
                    "chat_session_id": chat_session_id,
                    "media": [{"id": "photo-7102-a"}],
                }
            ),
        )
        db.session.add_all([ticket, ticket_without_location])
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
            "partitioned_session_and_synthetic_survey",
        )
        self.assertEqual((payload.get("channel_summary") or {}).get("total_cases"), 2)
        self.assertEqual((payload.get("channel_summary") or {}).get("observed_cases"), 2)
        self.assertIsNone((payload.get("channel_summary") or {}).get("total_interactions"))
        self.assertTrue((payload.get("session_activity") or {}).get("has_session_data"))
        self.assertEqual(
            (payload.get("session_activity") or {}).get("data_mode"),
            "session_generated_events",
        )

        metrics_by_id = {
            item.get("id"): item for item in (payload.get("metrics") or [])
        }
        self.assertEqual(
            set(metrics_by_id),
            {
                "session_claims_observed",
                "session_geolocated_claims",
                "session_evidence_files",
                "survey_valid_votes",
            },
        )
        session_metric_expectations = {
            "session_claims_observed": 2,
            "session_geolocated_claims": 1,
            "session_evidence_files": 3,
        }
        for metric_id, expected_value in session_metric_expectations.items():
            metric = metrics_by_id[metric_id]
            self.assertEqual(metric.get("value"), expected_value)
            self.assertEqual(metric.get("data_mode"), "session_generated_events")
            self.assertEqual(
                metric.get("denominator"),
                {"label": "Reclamos observados en esta sesión", "value": 2},
            )
            metric_provenance = metric.get("provenance") or {}
            self.assertEqual(
                metric_provenance.get("contract_version"),
                "demo.metric_provenance.v1",
            )
            self.assertEqual(
                metric_provenance.get("source_contract"),
                "demo.session_activity.v1",
            )
            self.assertTrue(metric_provenance.get("observed"))
            self.assertFalse(metric_provenance.get("synthetic"))

        survey_metric = metrics_by_id["survey_valid_votes"]
        self.assertEqual(survey_metric.get("data_mode"), "synthetic_demo_scenario")
        self.assertIn("Encuesta demo", survey_metric.get("label") or "")
        self.assertIn("Partición sintética independiente", survey_metric.get("detail") or "")
        self.assertEqual(survey_metric.get("verified_citizen_responses"), 0)
        self.assertEqual(
            (survey_metric.get("provenance") or {}).get("data_mode"),
            "synthetic_demo_scenario",
        )
        self.assertTrue((survey_metric.get("provenance") or {}).get("synthetic"))

        cards = payload.get("cards") or []
        self.assertEqual(
            {item.get("id") for item in cards},
            set(session_metric_expectations),
        )
        self.assertTrue(
            all(item.get("data_mode") == "session_generated_events" for item in cards)
        )
        self.assertEqual((cards or [])[0].get("value"), "2")
        self.assertEqual(
            [(item.get("ticket_code")) for item in ((payload.get("map") or {}).get("points") or [])],
            ["REAL-DEMO-7101"],
        )
        self.assertCountEqual(
            [item.get("case_code") for item in (payload.get("cases") or [])],
            ["REAL-DEMO-7101", "REAL-DEMO-7102"],
        )
        session_projection = json.dumps(
            {
                key: payload.get(key)
                for key in (
                    "cards",
                    "timeline",
                    "map",
                    "cases",
                    "channel_summary",
                    "session_activity",
                )
            },
            ensure_ascii=False,
        )
        self.assertNotIn("synthetic_demo_scenario", session_projection)
        self.assertNotIn("JN-DEMO-", session_projection)
        self.assertNotIn('"value": 184', session_projection)
        self.assertTrue(
            all(
                item.get("data_mode") == "session_generated_events"
                for item in (payload.get("timeline") or [])
            )
        )
        map_payload = payload.get("map") or {}
        self.assertEqual(map_payload.get("data_mode"), "session_generated_events")
        self.assertEqual(map_payload.get("displayed_points"), 1)
        self.assertEqual(map_payload.get("represented_cases"), 1)
        self.assertEqual(map_payload.get("total_cases"), 2)
        self.assertTrue(
            all(
                item.get("data_mode") == "session_generated_events"
                for item in (map_payload.get("points") or [])
            )
        )
        self.assertEqual(
            (payload.get("survey_voting") or {}).get("data_mode"),
            "synthetic_demo_scenario",
        )
        partitions = provenance.get("source_partitions") or []
        self.assertEqual(
            {partition.get("mode") for partition in partitions},
            {"session_generated_events", "synthetic_demo_scenario"},
        )
        session_partition = next(
            item
            for item in partitions
            if item.get("mode") == "session_generated_events"
        )
        synthetic_partition = next(
            item
            for item in partitions
            if item.get("mode") == "synthetic_demo_scenario"
        )
        self.assertEqual(
            set(session_partition.get("metric_ids") or []),
            set(session_metric_expectations),
        )
        self.assertEqual(
            synthetic_partition.get("metric_ids"),
            ["survey_valid_votes"],
        )


if __name__ == "__main__":
    unittest.main()
