"""Real administrative HTTP/SQL checks with disposable accounts and SQLite.

Use --write-fixtures <path> to save only the approved aggregate DTOs read from
this isolated API for frontend tests. No account or response contents are saved.
"""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == "__main__": prepare_process()

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer, Browser

FIXTURE_PATH = None


class FieldworkCoverageHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database import db
        from models import EncRespuesta
        cls.runtime = SurveyAcceptanceServer()
        cls.record = cls.runtime.create_survey("publicada", responses=510, title="QA cobertura para segmentar")
        cls.empty = cls.runtime.create_survey("publicada", title="QA cobertura sin base")
        cls.foreign = cls.runtime.create_survey("publicada", account="acceptance-b", responses=7)
        cls.corrupt = cls.runtime.create_survey("publicada", responses=1)
        with cls.runtime.app.app_context():
            rows = EncRespuesta.query.filter_by(encuesta_id=cls.record["id"]).order_by(EncRespuesta.id).all()
            for index, row in enumerate(rows[:10]):
                row.canal = "whatsapp"
                row.utm_campaign = "private-campaign-value"
                row.genero = "private-gender-value"
                row.rango_etario = "25-34"
                row.barrio = "private-neighborhood-value"
                row.ciudad = "private-city-value"
                row.provincia = "private-province-value"
                row.pais = "private-country-value"
                row.lat, row.lng = ((0, 0) if index == 0 else (90, 180) if index == 1
                    else (-90, -180) if index == 2 else (-34, -58))
                row.submitted_at = datetime.now(timezone.utc) - timedelta(days=2)
            rows[8].lat = 90.01
            rows[9].lng = -180.01
            # Missing pairs and whitespace must not become recorded values.
            rows[10].canal = None
            rows[10].lat = 0
            for key in ("canal", "utm_campaign", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais"):
                setattr(rows[11], key, " \t\r\n")
            rows[11].lng = 0
            for origin in ("synthetic_demo", "synthetic_demo", "legacy_unverified"):
                db.session.add(EncRespuesta(encuesta_id=cls.record["id"], tenant_id=cls.record["tenant_id"],
                    response_origin=origin, canal="synthetic-channel", genero="synthetic-gender"))
            corrupt = EncRespuesta.query.filter_by(encuesta_id=cls.corrupt["id"]).one()
            corrupt.tenant_id = cls.foreign["tenant_id"]
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()

    def login(self, account="acceptance-a"):
        browser = Browser(self.runtime.origin)
        status, _body = browser.request("POST", "/auth/login", {
            "email": self.runtime.accounts[account]["email"], "password": self.runtime.password})
        self.assertEqual(status, 200)
        return browser

    def endpoint(self, sid=None, *, prefix="/api/admin/encuestas", suffix="resumen", extra=""):
        return f"{prefix}/{sid or self.record['id']}/analytics/{suffix}?tenant_slug=acceptance-a{extra}"

    def read(self, **kwargs):
        status, body = self.login().request("GET", self.endpoint(**kwargs))
        self.assertEqual(status, 200)
        return body

    def test_exact_all_records_and_nine_field_definitions(self):
        body = self.read()
        report = body["fieldwork_coverage"]
        self.assertEqual(report["basis"], {"selected_records": 510, "exact": True})
        rows = {row["id"]: row for row in report["dimensions"]}
        expected = dict(channel=508, campaign=10, gender=10, age_range=10,
                        neighborhood=10, city=10, province=10, country=10, coordinates=8)
        self.assertEqual({key: row["recorded_count"] for key, row in rows.items()}, expected)
        for row in rows.values():
            self.assertEqual(row["recorded_count"] + row["missing_count"], 510)
            self.assertEqual(row["coverage_percent"], round(row["recorded_count"] / 510 * 100, 2))
        self.assertEqual(body["data_provenance"]["sample_size"], 500)
        self.assertFalse(report["inference_authorized"])
        self.assertIsNone(report["response_rate"])

    def test_disabled_or_biased_detail_sample_does_not_change_coverage(self):
        expected = self.read()["fieldwork_coverage"]
        for limit in ("0", "1"):
            with self.subTest(limit=limit), patch.dict(os.environ, {"SURVEY_ANALYTICS_SAMPLE_LIMIT": limit}):
                body = self.read()
                self.assertEqual(body["data_provenance"]["sample_size"], int(limit))
                self.assertEqual(body["fieldwork_coverage"], expected)

    def test_canonical_filters_change_the_actual_denominator(self):
        for extra, total, coordinate_count in (
            ("&canal=whatsapp", 10, 8),
            ("&genero=private-gender-value", 10, 8),
            ("&utm_campaign=private-campaign-value", 10, 8),
            ("&bbox=-180,-90,180,90", 8, 8),
            ("&desde=" + datetime.now(timezone.utc).date().isoformat(), 500, 0),
        ):
            with self.subTest(extra=extra):
                report = self.read(extra=extra)["fieldwork_coverage"]
                self.assertEqual(report["basis"]["selected_records"], total)
                self.assertTrue(report["scope"]["filtered"])
                self.assertEqual(report["dimensions"][-1]["recorded_count"], coordinate_count)
                self.assertNotIn("private-", json.dumps(report))

    def test_empty_base_is_not_zero_percent(self):
        report = self.read(sid=self.empty["id"])["fieldwork_coverage"]
        self.assertEqual(report["basis"]["selected_records"], 0)
        self.assertTrue(all(row["coverage_percent"] is None for row in report["dimensions"]))

    def test_unknown_filter_value_has_no_disclosure_or_fabricated_rate(self):
        report = self.read(extra="&canal=private-no-match")["fieldwork_coverage"]
        self.assertEqual(report["basis"]["selected_records"], 0)
        self.assertNotIn("private-no-match", json.dumps(report))
        self.assertTrue(all(row["coverage_percent"] is None for row in report["dimensions"]))

    def test_explicit_synthetic_mode_is_isolated_and_labelled(self):
        report = self.read(extra="&data_mode=synthetic")["fieldwork_coverage"]
        self.assertEqual(report["scope"]["mode"], "synthetic")
        self.assertFalse(report["scope"]["filtered"])
        self.assertEqual(report["basis"]["selected_records"], 2)
        self.assertEqual(report["dimensions"][2]["recorded_count"], 2)
        self.assertEqual(report["limitations"][0]["id"], "synthetic")

    def test_all_summary_aliases_share_coverage(self):
        browser = self.login(); expected = self.read()["fieldwork_coverage"]
        for prefix in ("/api/encuestas", "/admin/encuestas", "/api/admin/encuestas", "/api/municipal/encuestas"):
            for suffix in ("summary", "resumen"):
                with self.subTest(prefix=prefix, suffix=suffix):
                    status, body = browser.request("GET", self.endpoint(prefix=prefix, suffix=suffix))
                    self.assertEqual(status, 200)
                    self.assertEqual(body["fieldwork_coverage"], expected)

    def test_dashboard_and_envelope_match_summary(self):
        expected = self.read()["fieldwork_coverage"]
        for extra in ("&fast=1", "&fast=1&envelope=1"):
            body = self.read(suffix="dashboard", extra=extra)
            bundle = body.get("data", body)
            summary = bundle["modules"]["summary"]
            self.assertEqual(summary["fieldwork_coverage"], expected)
            self.assertIn("analytics_evidence", summary)

    def test_denials_happen_before_aggregate_query(self):
        cases = ((Browser(self.runtime.origin), self.endpoint(), 401),
            (self.login("acceptance-b"), self.endpoint().replace("acceptance-a", "acceptance-b"), 403),
            (self.login("viewer"), self.endpoint(), 403),
            (self.login(), self.endpoint(2147483000), 404))
        with patch("routes.encuestas_analytics.get_fieldwork_coverage_counts", side_effect=AssertionError("must not query")):
            for browser, path, expected in cases:
                with self.subTest(status=expected):
                    status, body = browser.request("GET", path)
                    self.assertEqual(status, expected)
                    self.assertNotIn("fieldwork_coverage", body)

    def test_stored_foreign_tenant_row_cannot_enter_coverage(self):
        from database import db
        from models import EncEncuesta
        from services.encuestas_analytics_service import get_fieldwork_coverage_counts
        with self.runtime.app.app_context():
            survey = db.session.get(EncEncuesta, self.corrupt["id"])
            self.assertEqual(get_fieldwork_coverage_counts(survey)["selected_records"], 0)
        self.assertNotIn("fieldwork_coverage", self.read(sid=self.corrupt["id"]))

    def test_arrival_between_summary_and_coverage_withholds_mixed_base(self):
        from routes.encuestas_analytics import get_fieldwork_coverage_counts
        def changed(survey, filters):
            aggregate = get_fieldwork_coverage_counts(survey, filters)
            aggregate["selected_records"] += 1
            return aggregate
        with patch("routes.encuestas_analytics.get_fieldwork_coverage_counts", side_effect=changed):
            self.assertNotIn("fieldwork_coverage", self.read())
            body = self.read(suffix="dashboard", extra="&fast=1")
            self.assertNotIn("fieldwork_coverage", body["modules"]["summary"])

    def test_aggregate_is_one_sql_row_without_detail_materialization(self):
        from database import db
        from models import EncEncuesta
        from services.encuestas_analytics_service import get_fieldwork_coverage_counts
        from sqlalchemy import event
        from sqlalchemy.dialects import postgresql
        with self.runtime.app.app_context():
            survey = db.session.get(EncEncuesta, self.record["id"])
            statements = []
            def capture(state):
                statements.append(str(state.statement.compile(dialect=postgresql.dialect())))
            session = db.session()
            event.listen(session, "do_orm_execute", capture)
            try: aggregate = get_fieldwork_coverage_counts(survey)
            finally: event.remove(session, "do_orm_execute", capture)
        self.assertEqual(aggregate["selected_records"], 510)
        self.assertEqual(len(statements), 1)
        sql = statements[0].lower()
        self.assertIn("count(enc_respuesta.id)", sql)
        self.assertEqual(sql.count("sum(case when"), 9)
        self.assertIn("enc_respuesta.tenant_id =", sql)
        self.assertIn("enc_respuesta.encuesta_id =", sql)
        self.assertIn("enc_respuesta.response_origin =", sql)
        self.assertNotIn("enc_respuesta_detalle", sql)
        self.assertNotIn("limit", sql)
        self.assertNotIn("group by", sql)

    def test_private_disclosure_never_enters_shared_public_service(self):
        from services.encuestas_analytics_service import get_summary
        with self.runtime.app.test_request_context("/"):
            self.assertNotIn("fieldwork_coverage", get_summary(self.record["id"]))

    def test_read_does_not_change_responses_or_session(self):
        before = self.runtime.read_storage(self.record["id"])
        browser = self.login()
        self.assertEqual(browser.request("GET", self.endpoint())[0], 200)
        self.assertEqual(browser.request("GET", "/api/me?tenant_slug=acceptance-a")[0], 200)
        self.assertEqual(self.runtime.read_storage(self.record["id"]), before)

    def test_frontend_fixture_contract_from_disposable_http(self):
        fixtures = {}
        for name, kwargs in (("partial", {}), ("empty", {"sid": self.empty["id"]}),
                ("filtered", {"extra": "&canal=whatsapp"}), ("synthetic", {"extra": "&data_mode=synthetic"})):
            body = self.read(**kwargs)
            fixtures[name] = {key: body[key] for key in (
                "encuesta_id", "total_respuestas", "participantes_unicos", "respuestas_completas",
                "tasa_completitud", "preguntas", "data_provenance", "fieldwork_coverage")}
        self.assertTrue(all(value["fieldwork_coverage"]["basis"]["exact"] for value in fixtures.values()))
        if FIXTURE_PATH:
            path = Path(FIXTURE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if "--write-fixtures" in sys.argv:
        index = sys.argv.index("--write-fixtures")
        FIXTURE_PATH = sys.argv[index + 1]
        del sys.argv[index:index + 2]
    unittest.main(verbosity=2)
