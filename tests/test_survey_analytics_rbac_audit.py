from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import db
from models import (
    AuditEvent,
    EncEncuesta,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    TenantProfile,
    User,
)
import routes.encuestas_analytics as analytics_routes
import routes.v2.surveys as v2_survey_routes
from services.encuestas_service import EncuestaError
from services.survey_access_policy import missing_survey_capabilities
from utils.auth_helpers import auth_session_version, generar_token


SURVEY_EXPORT = "survey.export"
SURVEY_PII_READ = "survey.pii.read"


def _user_headers(user: User, *, tenant_slug: str | None = None) -> dict[str, str]:
    extra_claims = None
    if user.rol == "super_admin":
        extra_claims = {
            "auth_provider": "clerk",
            "session_kind": "clerk",
            "clerk_sid": "sess_survey_rbac",
            "jti": "jti_survey_rbac",
            "sv": auth_session_version(user),
            "tenant_id": user.tenant_id,
            "tenant_slug": tenant_slug or user.tenant_slug,
        }
    token = generar_token(
        user.id,
        user.rol,
        user.tipo_chat,
        user.municipio_id,
        user.pyme_id,
        extra_claims=extra_claims,
    )
    headers = {"Authorization": f"Bearer {token}"}
    if tenant_slug:
        headers["X-Tenant"] = tenant_slug
    return headers


def _set_capabilities(user: User, capabilities: list[str]) -> None:
    user.accesibilidad = {
        "employee_scope": {
            "categorias": [],
            "zonas": [],
            "channels": [],
            "permisos": capabilities,
        }
    }
    db.session.add(user)
    db.session.commit()


@pytest.fixture
def survey_analytics_scope(client, monkeypatch):
    monkeypatch.setattr(analytics_routes, "FEATURE_ENCUESTAS", True)

    owner = User(
        email="survey-owner-rbac@example.com",
        name="Survey Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("secret")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="survey-rbac-tenant",
        nombre="Survey RBAC Tenant",
        tipo="municipio",
        municipio_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug

    employee = User(
        email="survey-employee-rbac@example.com",
        name="Survey Employee",
        rol="empleado",
        es_empleado=True,
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    employee.set_password("secret")
    db.session.add(employee)

    foreign_owner = User(
        email="survey-foreign-owner-rbac@example.com",
        name="Foreign Survey Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    foreign_owner.set_password("secret")
    db.session.add(foreign_owner)
    db.session.flush()
    foreign_tenant = TenantProfile(
        slug="survey-rbac-foreign",
        nombre="Survey RBAC Foreign",
        tipo="municipio",
        municipio_id=foreign_owner.id,
    )
    db.session.add(foreign_tenant)
    db.session.flush()
    foreign_owner.tenant_id = foreign_tenant.id
    foreign_owner.tenant_slug = foreign_tenant.slug

    foreign_employee = User(
        email="survey-foreign-employee-rbac@example.com",
        name="Foreign Survey Employee",
        rol="empleado",
        es_empleado=True,
        tipo_chat="municipio",
        tenant_id=foreign_tenant.id,
        tenant_slug=foreign_tenant.slug,
        accesibilidad={
            "employee_scope": {
                "permisos": [SURVEY_EXPORT, SURVEY_PII_READ],
            }
        },
    )
    foreign_employee.set_password("secret")
    db.session.add(foreign_employee)

    superadmin = User(
        email="survey-superadmin-rbac@example.com",
        name="Survey Superadmin",
        rol="super_admin",
        tipo_chat="municipio",
    )
    superadmin.set_password("secret")
    db.session.add(superadmin)

    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug="survey-rbac-audit",
        titulo="Survey RBAC audit",
        estado="publicada",
    )
    question = EncPregunta(
        encuesta=survey,
        orden=1,
        logical_ref="free-text",
        tipo="abierta",
        texto="Comentario",
        obligatoria=False,
    )
    response = EncRespuesta(
        encuesta=survey,
        tenant_id=tenant.id,
        huella_unica="sensitive-fingerprint-value",
        dni="30111222",
        phone="+5492364000000",
        ip="198.51.100.77",
        ua="sensitive-user-agent",
        lat=-34.585001,
        lng=-60.958001,
        canal="web",
    )
    detail = EncRespuestaDetalle(
        respuesta=response,
        pregunta=question,
        texto_libre="private free text secret@example.com",
    )
    db.session.add_all([survey, question, response, detail])
    db.session.commit()

    return {
        "tenant": tenant,
        "owner": owner,
        "employee": employee,
        "foreign_employee": foreign_employee,
        "superadmin": superadmin,
        "survey": survey,
    }


def _assert_capability_error(response, *, reason_code: str, missing: list[str]) -> None:
    assert response.status_code == 403
    assert response.headers.get("X-Request-Id")
    payload = response.get_json()
    assert payload["contract_version"] == "surveys.analytics.error.v1"
    assert payload["status_code"] == 403
    assert payload["reason_code"] == reason_code
    assert payload["retryable"] is False
    assert payload["missing_capabilities"] == missing


def test_survey_capability_policy_requires_explicit_employee_grants_and_supports_wildcard():
    employee = SimpleNamespace(rol="empleado", accesibilidad={})
    assert missing_survey_capabilities(
        employee,
        SURVEY_EXPORT,
        SURVEY_PII_READ,
    ) == [SURVEY_EXPORT, SURVEY_PII_READ]

    employee.accesibilidad = {
        "employee_scope": {"permissions": {SURVEY_EXPORT: True, SURVEY_PII_READ: False}}
    }
    assert missing_survey_capabilities(
        employee,
        SURVEY_EXPORT,
        SURVEY_PII_READ,
    ) == [SURVEY_PII_READ]

    employee.accesibilidad = {"employee_scope": {"permisos": ["*"]}}
    assert missing_survey_capabilities(employee, SURVEY_EXPORT, SURVEY_PII_READ) == []
    assert missing_survey_capabilities(
        SimpleNamespace(rol="admin", accesibilidad={}),
        SURVEY_EXPORT,
        SURVEY_PII_READ,
    ) == []


def test_employee_export_requires_explicit_export_and_pii_capabilities(
    client,
    monkeypatch,
    survey_analytics_scope,
):
    survey = survey_analytics_scope["survey"]
    employee = survey_analytics_scope["employee"]
    calls = {"csv": 0}

    def _export_sink(*_args, **_kwargs):
        calls["csv"] += 1
        yield "should-not-run\n"

    monkeypatch.setattr(analytics_routes, "export_csv_stream", _export_sink)
    path = f"/admin/encuestas/{survey.id}/analytics/export.csv"

    no_permissions = client.get(path, headers=_user_headers(employee))
    _assert_capability_error(
        no_permissions,
        reason_code="survey_export_capability_required",
        missing=[SURVEY_EXPORT, SURVEY_PII_READ],
    )

    _set_capabilities(employee, [SURVEY_EXPORT])
    export_only = client.get(path, headers=_user_headers(employee))
    _assert_capability_error(
        export_only,
        reason_code="survey_pii_read_capability_required",
        missing=[SURVEY_PII_READ],
    )

    _set_capabilities(employee, [SURVEY_PII_READ])
    pii_only = client.get(path, headers=_user_headers(employee))
    _assert_capability_error(
        pii_only,
        reason_code="survey_export_capability_required",
        missing=[SURVEY_EXPORT],
    )

    assert calls == {"csv": 0}
    assert AuditEvent.query.count() == 0


def test_authorized_exports_are_tenant_scoped_and_audited_without_raw_request_data(
    client,
    monkeypatch,
    survey_analytics_scope,
):
    tenant = survey_analytics_scope["tenant"]
    survey = survey_analytics_scope["survey"]
    employee = survey_analytics_scope["employee"]
    _set_capabilities(employee, [SURVEY_EXPORT, SURVEY_PII_READ])

    raw_filter_secret = "private-filter-secret@example.com"
    csv_response = client.get(
        f"/api/encuestas/{survey.id}/analytics/export.csv",
        query_string={
            "utm_source": raw_filter_secret,
            "bbox": "-60.99,-34.60,-60.90,-34.50",
        },
        headers={**_user_headers(employee), "X-Request-Id": "req-survey-csv-audit"},
    )
    assert csv_response.status_code == 200
    assert csv_response.mimetype == "text/csv"
    assert b"respuesta_id" in csv_response.data

    monkeypatch.setattr(
        analytics_routes,
        "get_summary",
        lambda *_args, **_kwargs: {
            "total_respuestas": 1,
            "participantes_unicos": 1,
            "tasa_completitud": 100,
            "preguntas": [],
        },
    )
    monkeypatch.setattr(
        analytics_routes,
        "get_heatmap",
        lambda *_args, **_kwargs: {"points": [], "cells": [], "metadata": {}},
    )
    monkeypatch.setattr(
        analytics_routes,
        "get_executive_brief",
        lambda *_args, **_kwargs: {"headline": "Aggregate only", "insights": []},
    )
    pdf_response = client.get(
        f"/api/admin/encuestas/{survey.id}/analytics/export.pdf",
        query_string={"canal": "secret-channel-value"},
        headers={**_user_headers(employee), "X-Request-Id": "req-survey-pdf-audit"},
    )
    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"

    events = AuditEvent.query.order_by(AuditEvent.id.asc()).all()
    assert len(events) == 2
    assert {event.tenant_id for event in events} == {tenant.id}
    assert {event.actor_user_id for event in events} == {employee.id}
    assert {event.resource_id for event in events} == {str(survey.id)}
    assert {event.event_type for event in events} == {"survey.analytics.export_requested"}
    assert {event.details["format"] for event in events} == {"csv", "pdf"}
    assert all(event.ip_address is None for event in events)

    audit_document = json.dumps(
        [event.details for event in events],
        sort_keys=True,
        ensure_ascii=False,
    )
    assert raw_filter_secret not in audit_document
    assert "secret-channel-value" not in audit_document
    assert "bbox" not in audit_document
    assert "utm_source" not in audit_document
    assert "198.51.100.77" not in audit_document
    assert "30111222" not in audit_document
    assert "+5492364000000" not in audit_document


def test_cross_tenant_export_is_denied_before_sink_and_does_not_audit(
    client,
    monkeypatch,
    survey_analytics_scope,
):
    survey = survey_analytics_scope["survey"]
    foreign_employee = survey_analytics_scope["foreign_employee"]
    calls = {"csv": 0}

    def _export_sink(*_args, **_kwargs):
        calls["csv"] += 1
        yield "should-not-run\n"

    monkeypatch.setattr(analytics_routes, "export_csv_stream", _export_sink)
    response = client.get(
        f"/admin/encuestas/{survey.id}/analytics/export.csv",
        headers=_user_headers(foreign_employee),
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["contract_version"] == "surveys.analytics.error.v1"
    assert payload["reason_code"] == "survey_tenant_access_denied"
    assert calls == {"csv": 0}
    assert AuditEvent.query.count() == 0


def test_export_fails_closed_when_required_audit_cannot_be_persisted(
    client,
    monkeypatch,
    survey_analytics_scope,
):
    survey = survey_analytics_scope["survey"]
    employee = survey_analytics_scope["employee"]
    _set_capabilities(employee, [SURVEY_EXPORT, SURVEY_PII_READ])
    calls = {"csv": 0}

    def _export_sink(*_args, **_kwargs):
        calls["csv"] += 1
        yield "should-not-run\n"

    def _fail_commit():
        raise RuntimeError("simulated audit storage failure")

    monkeypatch.setattr(analytics_routes, "export_csv_stream", _export_sink)
    monkeypatch.setattr(analytics_routes.db.session, "commit", _fail_commit)
    response = client.get(
        f"/admin/encuestas/{survey.id}/analytics/export.csv",
        headers=_user_headers(employee),
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["contract_version"] == "surveys.analytics.error.v1"
    assert payload["reason_code"] == "survey_export_audit_failed"
    assert payload["retryable"] is True
    assert calls == {"csv": 0}


def test_sensitive_analytics_require_pii_capability_but_aggregate_timeseries_stays_available(
    client,
    survey_analytics_scope,
):
    survey = survey_analytics_scope["survey"]
    employee = survey_analytics_scope["employee"]
    headers = _user_headers(employee)

    protected_paths = [
        f"/admin/encuestas/{survey.id}/analytics/resumen",
        f"/admin/encuestas/{survey.id}/analytics/heatmap",
        f"/admin/encuestas/{survey.id}/analytics/brief",
        f"/admin/encuestas/{survey.id}/analytics/dashboard",
        f"/admin/encuestas/{survey.id}/analytics/anomalies",
    ]
    for path in protected_paths:
        response = client.get(path, headers=headers)
        _assert_capability_error(
            response,
            reason_code="survey_pii_read_capability_required",
            missing=[SURVEY_PII_READ],
        )

    timeseries = client.get(
        f"/admin/encuestas/{survey.id}/analytics/series",
        headers=headers,
    )
    assert timeseries.status_code == 200
    serialized = json.dumps(timeseries.get_json(), sort_keys=True)
    for sensitive_value in (
        "private free text",
        "secret@example.com",
        "198.51.100.77",
        "30111222",
        "+5492364000000",
        "sensitive-fingerprint-value",
        "-34.585001",
        "-60.958001",
    ):
        assert sensitive_value not in serialized


def test_v2_dashboard_cannot_bypass_pii_capability(
    client,
    monkeypatch,
    survey_analytics_scope,
):
    tenant = survey_analytics_scope["tenant"]
    survey = survey_analytics_scope["survey"]
    employee = survey_analytics_scope["employee"]
    calls = {"dashboard": 0}

    def _dashboard_sink(survey_id, filtros=None, granularity="day"):
        calls["dashboard"] += 1
        return {"encuesta_id": survey_id, "modules": {}}

    monkeypatch.setattr(v2_survey_routes, "get_dashboard_bundle", _dashboard_sink)
    path = f"/api/v2/surveys/{survey.id}/analytics"
    headers = _user_headers(employee, tenant_slug=tenant.slug)

    denied = client.get(path, headers=headers)
    assert denied.status_code == 403
    payload = denied.get_json()
    assert payload["contract_version"] == "shared.error.v1"
    assert payload["reason_code"] == "survey_pii_read_capability_required"
    assert payload["required_capabilities"] == [SURVEY_PII_READ]
    assert payload["missing_capabilities"] == [SURVEY_PII_READ]
    assert calls == {"dashboard": 0}

    _set_capabilities(employee, [SURVEY_PII_READ])
    allowed = client.get(path, headers=headers)
    assert allowed.status_code == 200
    assert allowed.get_json()["encuesta_id"] == survey.id
    assert calls == {"dashboard": 1}


def test_admin_and_superadmin_owner_access_do_not_require_employee_capabilities(
    client,
    survey_analytics_scope,
):
    tenant = survey_analytics_scope["tenant"]
    survey = survey_analytics_scope["survey"]
    owner = survey_analytics_scope["owner"]
    superadmin = survey_analytics_scope["superadmin"]

    owner_response = client.get(
        f"/admin/encuestas/{survey.id}/analytics/export.csv",
        headers=_user_headers(owner),
    )
    assert owner_response.status_code == 200

    superadmin_response = client.get(
        f"/api/admin/encuestas/{survey.id}/analytics/export.csv",
        headers=_user_headers(superadmin, tenant_slug=tenant.slug),
    )
    assert superadmin_response.status_code == 200

    events = AuditEvent.query.order_by(AuditEvent.id.asc()).all()
    assert len(events) == 2
    assert {event.actor_user_id for event in events} == {owner.id, superadmin.id}
    assert {event.tenant_id for event in events} == {tenant.id}
