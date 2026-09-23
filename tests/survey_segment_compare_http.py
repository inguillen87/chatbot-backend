"""A/B comparison acceptance using real Flask, login and disposable SQLite."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == "__main__": prepare_process()

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer, Browser

FIXTURE_PATH = None


class SegmentComparisonHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database import db
        from models import EncPregunta, EncOpcion, EncRespuesta, EncRespuestaDetalle
        cls.runtime = SurveyAcceptanceServer()
        cls.record = cls.runtime.create_survey("publicada", responses=600, title="QA comparación exacta")
        cls.empty = cls.runtime.create_survey("publicada", title="QA sin preguntas de opciones")
        cls.foreign = cls.runtime.create_survey("publicada", account="acceptance-b", responses=2)
        with cls.runtime.app.app_context():
            question = EncPregunta.query.filter_by(encuesta_id=cls.record["id"]).one()
            question.tipo = "opcion_unica"; question.texto = "¿Está de acuerdo?"
            multi = EncPregunta(encuesta_id=cls.record["id"], orden=2, tipo="opcion_multiple", texto="¿Qué servicios usa?")
            foreign = EncPregunta.query.filter_by(encuesta_id=cls.foreign["id"]).one(); foreign.tipo = "opcion_unica"
            db.session.add(multi); db.session.flush()
            yes = EncOpcion(pregunta_id=question.id, orden=1, texto="Sí")
            no = EncOpcion(pregunta_id=question.id, orden=2, texto="No")
            red = EncOpcion(pregunta_id=multi.id, orden=1, texto="Servicio A")
            blue = EncOpcion(pregunta_id=multi.id, orden=2, texto="Servicio B")
            wrong = EncOpcion(pregunta_id=foreign.id, orden=1, texto="Opción ajena")
            db.session.add_all([yes, no, red, blue, wrong]); db.session.flush()
            cls.single_id, cls.multi_id = question.id, multi.id
            cls.yes_id, cls.no_id, cls.red_id, cls.blue_id = yes.id, no.id, red.id, blue.id
            def add(row, q, option):
                db.session.add(EncRespuestaDetalle(respuesta_id=row.id, pregunta_id=q.id, opcion_id=option.id))
            rows = EncRespuesta.query.filter_by(encuesta_id=cls.record["id"]).order_by(EncRespuesta.id).all()
            for index, row in enumerate(rows):
                row.canal = " WhatsApp " if index < 100 else "web"
                row.genero = "grupo-a" if index < 100 else "grupo-b"
                row.barrio = "centro" if index % 2 == 0 else "norte"
                add(row, question, yes if index < 350 else no if index < 599 else wrong)
                add(row, multi, red if index < 100 else blue)
                if index < 50: add(row, multi, blue)
                if 100 <= index < 350: add(row, multi, red)
            add(rows[0], question, no)  # Conflicting single response excluded for this question only.
            add(rows[1], question, yes); add(rows[1], multi, red)  # Historical duplicate details.
            add(rows[2], foreign, wrong)  # A foreign question cannot enter this instrument.
            for origin in ("synthetic_demo", "synthetic_demo", "legacy_unverified"):
                row = EncRespuesta(encuesta_id=cls.record["id"], tenant_id=cls.record["tenant_id"], response_origin=origin, canal="whatsapp")
                db.session.add(row); db.session.flush(); add(row, question, no)
            corrupt = EncRespuesta(encuesta_id=cls.record["id"], tenant_id=cls.foreign["tenant_id"], response_origin="real", canal="whatsapp")
            db.session.add(corrupt); db.session.flush(); add(corrupt, question, no)
            db.session.commit()

    @classmethod
    def tearDownClass(cls): cls.runtime.close()

    def login(self, account="acceptance-a"):
        browser = Browser(self.runtime.origin)
        status, _ = browser.request("POST", "/auth/login", {
            "email": self.runtime.accounts[account]["email"], "password": self.runtime.password})
        self.assertEqual(status, 200); return browser

    def endpoint(self, extra="", sid=None, suffix="compare", prefix="/api/admin/encuestas"):
        return f"{prefix}/{sid or self.record['id']}/analytics/segments/{suffix}?tenant_slug=acceptance-a{extra}"

    def read(self, extra="&a_canal=whatsapp&b_canal=web", **kwargs):
        status, body = self.login().request("GET", self.endpoint(extra, **kwargs))
        self.assertEqual(status, 200, body); return body

    def test_exact_counts_ignore_sample_and_foreign_rows(self):
        with patch.dict(os.environ, {"SURVEY_ANALYTICS_SAMPLE_LIMIT": "1"}): body = self.read()
        self.assertEqual(body["basis"], dict(selected_records=600, segment_a_records=100, segment_b_records=500, overlap_records=0, exact=True))
        self.assertEqual(body["segment_a"]["stats"]["total_respuestas"], 100)
        self.assertEqual(body["segment_a"]["stats"]["canales"], [{"label": "whatsapp", "value": 100}])
        self.assertEqual(body["data_provenance"]["raw_responses_materialized"], 0)
        self.assertEqual(body["data_provenance"]["synthetic_responses_excluded"], 2)
        self.assertFalse(body["inference_authorized"])

    def test_single_conflicts_excluded_and_duplicate_details_deduplicated(self):
        question = next(q for q in self.read()["questions"] if q["id"] == self.single_id)
        self.assertEqual((question["segment_a_answered"], question["segment_b_answered"]), (99, 499))
        self.assertEqual((question["segment_a_conflicts"], question["segment_b_conflicts"]), (1, 0))
        yes = next(o for o in question["options"] if o["id"] == self.yes_id)
        self.assertEqual((yes["segment_a_count"], yes["segment_b_count"]), (99, 250))
        self.assertEqual((yes["segment_a_percent"], yes["segment_b_percent"], yes["delta_percentage_points"]), (100, 50.1, 49.9))
        self.assertNotIn("Opción ajena", json.dumps(question))

    def test_multiple_choice_denominator_is_respondents_not_selections(self):
        q = next(q for q in self.read()["questions"] if q["id"] == self.multi_id)
        self.assertEqual((q["segment_a_answered"], q["segment_b_answered"]), (100, 500))
        self.assertEqual(sum(o["segment_a_percent"] for o in q["options"]), 150)
        self.assertEqual(sum(o["segment_b_percent"] for o in q["options"]), 150)
        red = next(o for o in q["options"] if o["id"] == self.red_id)
        self.assertEqual((red["segment_a_count"], red["segment_b_count"], red["delta_percentage_points"]), (100, 250, 50))

    def test_identical_and_overlapping_segments_have_explicit_overlap(self):
        same = self.read("&a_canal=whatsapp&b_canal=WHATSAPP")
        self.assertEqual(same["basis"]["overlap_records"], 100)
        self.assertTrue(all(o["delta_percentage_points"] == 0 for q in same["questions"] for o in q["options"]))
        overlap = self.read("&a_canal=whatsapp&b_barrio=centro")
        self.assertEqual(overlap["basis"]["overlap_records"], 50)
        self.assertEqual(overlap["basis"]["segment_b_records"], 300)

    def test_global_filter_parity_and_array_values(self):
        body = self.read("&barrio=centro&a_canal=whatsapp&b_canal=web")
        self.assertEqual(body["basis"], dict(selected_records=300, segment_a_records=50, segment_b_records=250, overlap_records=0, exact=True))
        self.assertEqual(body["scope"]["global_filters"], {"barrio": "centro"})
        array = self.read("&a_barrio=centro,norte&b_canal=web")
        self.assertEqual(array["basis"]["segment_a_records"], 600)
        self.assertEqual(array["scope"]["segment_a_filters"], {"barrio": ["centro", "norte"]})

    def test_empty_segment_never_fabricates_zero_percent_or_delta(self):
        body = self.read("&a_canal=none&b_canal=web")
        self.assertEqual(body["basis"]["segment_a_records"], 0)
        self.assertTrue(all(o["segment_a_percent"] is None and o["delta_percentage_points"] is None for q in body["questions"] for o in q["options"]))
        self.assertEqual(self.read(sid=self.empty["id"])["questions"], [])

    def test_synthetic_mode_and_legacy_exclusion(self):
        body = self.read("&data_mode=synthetic&a_canal=whatsapp&b_canal=web")
        self.assertEqual(body["basis"]["selected_records"], 2)
        self.assertEqual(body["scope"]["mode"], "synthetic")
        self.assertTrue(body["ui"]["description"].startswith("Datos sintéticos"))

    def test_invalid_filters_fail_closed(self):
        browser = self.login()
        for extra in ("&a_territorio=centro", "&a_canal=", "&a_canal=,,,", "&desde=not-a-date",
                      "&desde=2026-09-23&hasta=2026-09-01", "&bbox=no", "&data_mode=unknown",
                      "&city=centro", "&barrio=", "&a_canal=web,,whatsapp", "&a_canal=web&a_canal=whatsapp"):
            with self.subTest(extra=extra):
                status, body = browser.request("GET", self.endpoint(extra))
                self.assertEqual(status, 400, body)

    def test_auth_capability_and_tenant_denials_before_query(self):
        for suffix in ("compare", "suggestions"):
            for account, expected in ((None, 401), ("viewer", 403), ("acceptance-b", 403)):
                browser = self.login(account) if account else Browser(self.runtime.origin)
                path = self.endpoint(suffix=suffix)
                if account == "acceptance-b": path = path.replace("acceptance-a", "acceptance-b")
                with patch("routes.encuestas_analytics.get_segment_compare", side_effect=AssertionError("query prohibited")), patch(
                        "routes.encuestas_analytics.get_segment_suggestions", side_effect=AssertionError("query prohibited")):
                    self.assertEqual(browser.request("GET", path)[0], expected)

    def test_all_compare_aliases_return_same_bases(self):
        expected = self.read()["basis"]
        for prefix in ("/api/encuestas", "/admin/encuestas", "/api/admin/encuestas", "/api/municipal/encuestas"):
            self.assertEqual(self.read(prefix=prefix)["basis"], expected)

    def test_client_tenant_alias_is_accepted_only_with_coherent_authorized_scope(self):
        browser = self.login()
        base = f"/api/admin/encuestas/{self.record['id']}/analytics/segments"
        for suffix, params in (("suggestions", "data_mode=real&limit=5"),
                ("compare", "data_mode=real&a_canal=whatsapp&b_canal=web")):
            with self.subTest(suffix=suffix):
                path = f"{base}/{suffix}?{params}&tenant_slug=acceptance-a&tenant=acceptance-a"
                status, body = browser.request("GET", path)
                self.assertEqual(status, 200, body)
                self.assertEqual(body["scope"]["tenant_id"], self.record["tenant_id"])
                self.assertEqual(body["basis"]["selected_records"] if suffix == "compare" else body["total_respuestas"], 600)
                foreign = path.replace("&tenant=acceptance-a", "&tenant=acceptance-b")
                with patch("routes.encuestas_analytics.get_segment_compare", side_effect=AssertionError("query prohibited")), patch(
                        "routes.encuestas_analytics.get_segment_suggestions", side_effect=AssertionError("query prohibited")):
                    self.assertEqual(browser.request("GET", foreign)[0], 400)

    def test_suggestions_include_channel_outside_latest_500(self):
        body = self.read("", suffix="suggestions")
        self.assertEqual(body["total_respuestas"], 600)
        self.assertTrue(body["exact_aggregates"])
        self.assertEqual({row["label"]: row["count"] for row in body["dimensions"]["canal"]}, {"web": 500, "whatsapp": 100})
        filtered = self.read("&barrio=centro", suffix="suggestions")
        self.assertEqual(filtered["total_respuestas"], 300)

    def test_core_bases_and_cells_use_one_statement_snapshot(self):
        from database import db
        from services.encuestas_analytics_service import get_segment_compare
        from sqlalchemy import event
        from sqlalchemy.dialects import postgresql
        with self.runtime.app.app_context():
            statements = []
            def capture(state): statements.append(str(state.statement.compile(dialect=postgresql.dialect())))
            session = db.session(); event.listen(session, "do_orm_execute", capture)
            try: body = get_segment_compare(self.record["id"], segment_a={"canal": "whatsapp"}, segment_b={"canal": "web"})
            finally: event.remove(session, "do_orm_execute", capture)
        core = [sql for sql in statements if "UNION ALL" in sql]
        self.assertEqual(len(core), 1)
        self.assertIn("count(distinct(case", core[0].lower())
        self.assertIn("enc_opcion.pregunta_id = enc_pregunta.id", core[0])
        self.assertIn("enc_respuesta.tenant_id =", core[0])
        self.assertEqual(body["basis"]["selected_records"], 600)

    def test_fixture_real_http(self):
        fixtures = {}
        for key, params in (("main", "&a_canal=whatsapp&b_canal=web"), ("empty", "&a_canal=none&b_canal=web"),
                ("overlap", "&a_canal=whatsapp&b_barrio=centro"), ("filtered", "&barrio=centro&a_canal=whatsapp&b_canal=web"),
                ("synthetic", "&data_mode=synthetic&a_canal=whatsapp&b_canal=web")):
            fixtures[key] = self.read(params)
        if FIXTURE_PATH:
            path = Path(FIXTURE_PATH); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if "--write-fixtures" in sys.argv:
        index = sys.argv.index("--write-fixtures"); FIXTURE_PATH = sys.argv[index + 1]; del sys.argv[index:index + 2]
    unittest.main(verbosity=2)
