from types import SimpleNamespace

import pytest
from flask import g

from models import EncRespuesta, SurveyResponseReceipt
from routes import portal_api, pwa_public


MISSING_ID_CONTRACT = "surveys.response_receipt.v1"


def _assert_missing_submission_id(response) -> None:
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["contract_version"] == MISSING_ID_CONTRACT
    assert payload["reason_code"] == "survey_submission_id_required"
    assert payload["retryable"] is False
    assert payload["action_hint"] == "check_submission_id"


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/v2/public/surveys/canonical-survey/respond",
        "/api/public/encuestas/canonical-survey/responder",
        "/api/public/encuestas/v1/canonical-survey/respuestas",
        "/public/encuestas/canonical-survey/responder",
        "/public/encuestas/v1/canonical-survey/respuestas",
    ],
)
def test_public_http_survey_write_surfaces_require_submission_id(client, endpoint):
    response = client.post(endpoint, json={"respuestas": []})

    _assert_missing_submission_id(response)
    assert EncRespuesta.query.count() == 0
    assert SurveyResponseReceipt.query.count() == 0


def test_pwa_http_survey_write_requires_submission_id(client, monkeypatch):
    tenant = SimpleNamespace(id=41, encuestas_tenant_id=41, slug="tenant-pwa")
    monkeypatch.setattr(pwa_public, "_require_tenant", lambda: tenant)

    response = client.post(
        "/api/pwa/public/surveys/canonical-survey/respond",
        json={"respuestas": []},
    )

    _assert_missing_submission_id(response)
    assert EncRespuesta.query.count() == 0
    assert SurveyResponseReceipt.query.count() == 0


@pytest.mark.parametrize("prefix", ["/api/v1/portal", "/api/portal"])
def test_portal_http_survey_write_requires_submission_id(app, monkeypatch, prefix):
    tenant = SimpleNamespace(id=51, encuestas_tenant_id=51, slug="tenant-portal")
    viewer = SimpleNamespace(id=61)
    monkeypatch.setattr(portal_api, "_resolve_context", lambda _slug: tenant)

    path = f"{prefix}/{tenant.slug}/surveys/canonical-survey/responses"
    with app.test_request_context(path, method="POST", json={"respuestas": []}):
        g.viewer = viewer
        response, status_code = portal_api.submit_portal_survey_response(
            tenant.slug,
            "canonical-survey",
        )
        response.status_code = status_code

    _assert_missing_submission_id(response)


def test_pwa_header_and_body_submission_ids_must_match(client, monkeypatch):
    tenant = SimpleNamespace(id=41, encuestas_tenant_id=41, slug="tenant-pwa")
    monkeypatch.setattr(pwa_public, "_require_tenant", lambda: tenant)

    response = client.post(
        "/api/pwa/public/surveys/canonical-survey/respond",
        json={"submission_id": "survey-body-key-0001", "respuestas": []},
        headers={"Idempotency-Key": "survey-header-key-0002"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["contract_version"] == MISSING_ID_CONTRACT
    assert payload["reason_code"] == "survey_submission_id_mismatch"
    assert payload["retryable"] is False
