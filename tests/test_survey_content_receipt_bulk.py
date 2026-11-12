"""Bulk list reads preserve the exact per-survey jurisdiction decision offline."""
from types import SimpleNamespace

import pytest

from database import db
from models import EncEncuesta
from models_survey_jurisdiction import SurveyContentReceipt
from services import encuestas_service as surveys
from services import survey_jurisdiction as jurisdiction
from tests.test_survey_jurisdiction_guard import (
    _approve_exact,
    _payload,
    _user_and_tenant,
)
from tests.test_survey_list_pagination_scale import (
    _add_surveys,
    _captured_selects,
    _tenant_and_headers,
)


def _receipt_selects(statements):
    return sum(" from survey_content_receipt " in statement for statement in statements)


def _approved():
    owner, tenant = _user_and_tenant(verified=True)
    survey = surveys.create_encuesta(_payload(), owner)
    _approve_exact(survey, owner, tenant)
    db.session.commit()
    return owner, tenant, survey


def test_causal_per_survey_fallback_32_queries_becomes_constant_bulk_13(client, monkeypatch):
    tenant, owner, _headers = _tenant_and_headers("receipt-bulk-causal")
    _add_surveys(tenant, owner, count=24, questions=3, options=4, governed=True)
    warm = surveys.list_encuestas_page(tenant.id, limit=2)
    surveys.build_admin_list_payload(warm["items"], tenant_id=tenant.id, tenant_slug=tenant.slug)

    def measure(limit):
        db.session.expunge_all()
        with _captured_selects() as statements:
            page = surveys.list_encuestas_page(tenant.id, limit=limit)
            payload = surveys.build_admin_list_payload(page["items"], tenant_id=tenant.id, tenant_slug=tenant.slug)
        return len(statements), _receipt_selects(statements), payload

    # None retains the original per-survey deep read. No query is made by this
    # test substitute, so it reproduces the old cause instead of adding noise.
    with monkeypatch.context() as legacy:
        legacy.setattr(surveys, "_bulk_survey_content_receipt_map", lambda rows: {
            (row.tenant_id, row.id): None for row in rows
        })
        old_one, old_receipts_one, _ = measure(1)
        old_many, old_receipts_many, old_payload = measure(20)
    new_one, new_receipts_one, _ = measure(1)
    new_many, new_receipts_many, new_payload = measure(20)
    assert (old_one, old_many, old_receipts_one, old_receipts_many) == (13, 32, 1, 20)
    assert (new_one, new_many, new_receipts_one, new_receipts_many) == (13, 13, 1, 1)
    # Evaluated timestamps are request-local; every decision remains equal.
    for old, new in zip(old_payload["encuestas"], new_payload["encuestas"]):
        assert old["public_access"] == new["public_access"]
        assert old["admin_lifecycle"]["government_survey_evidence_gate"] == new["admin_lifecycle"]["government_survey_evidence_gate"]


def test_empty_receipt_list_is_explicit_and_never_falls_back(client, monkeypatch):
    owner, tenant = _user_and_tenant(verified=True)
    survey = EncEncuesta(tenant_id=tenant.id, created_by=owner.id, slug="receipt-empty",
                        titulo="Sin revisión", estado="borrador", content_origin="manual",
                        jurisdiction_ref=tenant.jurisdiction_ref)
    db.session.add(survey); db.session.commit()
    deep = jurisdiction.jurisdiction_contract(survey)
    assert deep["reason_code"] == "survey_content_review_required"
    monkeypatch.setattr(jurisdiction, "_receipt_rows", lambda _row: pytest.fail("explicit empty list performed fallback query"))
    with _captured_selects() as statements:
        bulk = jurisdiction.jurisdiction_contract(survey, receipt_rows=[])
    assert bulk == deep and _receipt_selects(statements) == 0


def test_none_keeps_detail_deep_query_and_approved_decision(client):
    _owner, _tenant, survey = _approved()
    with _captured_selects() as statements:
        deep = jurisdiction.jurisdiction_contract(survey)
    rows = surveys._bulk_survey_content_receipt_map([survey])[(survey.tenant_id, survey.id)]
    bulk = jurisdiction.jurisdiction_contract(survey, receipt_rows=rows)
    assert deep == bulk and deep["ready"] is True
    assert _receipt_selects(statements) == 1


def test_bulk_keys_are_exact_pairs_and_do_not_include_neighbor_surveys(client):
    owner_a, tenant_a, survey_a = _approved()
    owner_b, tenant_b, survey_b = _approved()
    neighbor_a = surveys.create_encuesta(_payload("Vecina A"), owner_a)
    neighbor_b = surveys.create_encuesta(_payload("Vecina B"), owner_b)
    db.session.commit()
    rows = surveys._bulk_survey_content_receipt_map([survey_a, survey_b])
    assert set(rows) == {(tenant_a.id, survey_a.id), (tenant_b.id, survey_b.id)}
    assert all((row.tenant_id, row.survey_id) == key for key, receipts in rows.items() for row in receipts)
    assert {row.survey_id for receipts in rows.values() for row in receipts} == {survey_a.id, survey_b.id}
    assert neighbor_a.id not in {row.survey_id for receipts in rows.values() for row in receipts}
    assert neighbor_b.id not in {row.survey_id for receipts in rows.values() for row in receipts}


@pytest.mark.parametrize("scope", ["tenant", "survey"])
def test_foreign_receipt_rows_fail_closed_before_chain_approval(client, scope):
    owner, tenant, survey = _approved()
    if scope == "tenant":
        _other_owner, _other_tenant, other = _approved()
    else:
        other = surveys.create_encuesta(_payload("Otra encuesta"), owner)
        _approve_exact(other, owner, tenant)
    rows = surveys._bulk_survey_content_receipt_map([other])[(other.tenant_id, other.id)]
    with pytest.raises(jurisdiction.SurveyJurisdictionError) as denied:
        jurisdiction.jurisdiction_contract(survey, receipt_rows=rows)
    assert denied.value.reason_code == "survey_content_receipt_scope_mismatch"


@pytest.mark.parametrize("corruption", ["hash", "chain"])
def test_corrupt_receipt_chain_has_same_fail_closed_deep_and_bulk_decision(client, monkeypatch, corruption):
    _owner, _tenant, survey = _approved()
    rows = surveys._bulk_survey_content_receipt_map([survey])[(survey.tenant_id, survey.id)]
    copies = [SimpleNamespace(**{column.name: getattr(row, column.name) for column in SurveyContentReceipt.__table__.columns}) for row in rows]
    if corruption == "hash":
        copies[-1].receipt_json += " "
    else:
        copies[-1].previous_receipt_sha256 = "f" * 64
    monkeypatch.setattr(jurisdiction, "_receipt_rows", lambda _row: copies)
    deep = jurisdiction.jurisdiction_contract(survey)
    bulk = jurisdiction.jurisdiction_contract(survey, receipt_rows=copies)
    assert deep == bulk and bulk["ready"] is False
    assert bulk["reason_code"] == "survey_content_receipt_integrity_failed"


def test_stale_review_and_visibility_decisions_preserved(client, monkeypatch):
    _owner, tenant, survey = _approved()
    monkeypatch.setitem(client.application.config, "SURVEY_JURISDICTION_GATE_MODE", "enforce_visibility")
    monkeypatch.setitem(client.application.config, "SURVEY_JURISDICTION_GATE_TENANT_IDS", str(tenant.id))
    rows = surveys._bulk_survey_content_receipt_map([survey])[(survey.tenant_id, survey.id)]
    assert jurisdiction.survey_is_publicly_visible(survey) is True
    assert jurisdiction.survey_is_publicly_visible(survey, receipt_rows=rows) is True
    survey.titulo = "Edición sin nueva revisión"
    db.session.commit()
    assert jurisdiction.jurisdiction_contract(survey) == jurisdiction.jurisdiction_contract(survey, receipt_rows=rows)
    assert jurisdiction.jurisdiction_contract(survey, receipt_rows=rows)["reason_code"] == "survey_content_review_stale"
    assert jurisdiction.survey_is_publicly_visible(survey) is False
    assert jurisdiction.survey_is_publicly_visible(survey, receipt_rows=rows) is False


def test_foreign_actor_cannot_select_another_tenant_list_or_receipts(client):
    tenant_a, owner_a, headers_a = _tenant_and_headers("receipt-actor-a")
    tenant_b, owner_b, _headers_b = _tenant_and_headers("receipt-actor-b")
    survey_a = surveys.create_encuesta(_payload("Instrumento A"), owner_a)
    survey_b = surveys.create_encuesta(_payload("Instrumento B"), owner_b)
    own = client.get("/api/admin/encuestas", headers=headers_a)
    assert own.status_code == 200
    assert {row["id"] for row in own.get_json()["encuestas"]} == {survey_a.id}
    assert survey_b.id not in {row["id"] for row in own.get_json()["encuestas"]}
    foreign = client.get("/api/admin/encuestas", headers={**headers_a, "X-Tenant-Slug": tenant_b.slug})
    assert foreign.status_code == 403
