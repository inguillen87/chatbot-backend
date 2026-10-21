"""Offline causal admission tests against real local SQLite transactions.

Tokens, opaque subjects and addresses below are synthetic. No provider calls.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from limits.storage import MemoryStorage, RedisStorage
from limits.strategies import FixedWindowRateLimiter

from database import db
from extensions import limiter
from models import EncEncuesta, EncLink, EncRespuesta, SurveyResponseEffect, SurveyResponseReceipt, User
from models_survey_eligibility import SurveyEligibilityTerminal
import tests.test_v2_surveys as api_helpers
import tests.test_survey_governance_v2 as governance_helpers
import tests.test_encuestas_concurrency_guard as concurrency_helpers
from tests.test_encuestas_concurrency_guard import concurrency_app


def _durable_headers(user, tenant=None, *, key=None):
    # The older harness signs JWTs without the current durable session lineage.
    # Issue through the real local auth service; do not mock authentication.
    from utils.auth_helpers import generar_token

    token = generar_token(
        user.id, user.rol, user.tipo_chat, user.municipio_id, user.pyme_id,
        extra_claims={"tenant_id": user.tenant_id, "tenant_slug": user.tenant_slug},
    )
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers["X-Tenant-Slug"] = tenant.slug
    if key:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def api():
    harness = api_helpers.V2SurveysApiTest(methodName="runTest")
    harness.setUp()
    harness._auth = _durable_headers
    # Isolate participation admission; do not forge government evidence/review.
    harness.tenant_1.tipo = "pyme"
    db.session.commit()
    harness.app.config.update(
        ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=False,
        ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT=False,
    )
    try:
        yield harness
    finally:
        harness.tearDown()


@pytest.fixture
def governed():
    harness = governance_helpers.SurveyGovernanceV2Test(methodName="runTest")
    harness.setUp()
    harness._headers = _durable_headers
    harness.tenant_1.tipo = "pyme"
    db.session.commit()
    harness.app.config.update(
        ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=False,
        ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT=False,
    )
    try:
        yield harness
    finally:
        harness.tearDown()


def _strict(harness, tenant_id):
    harness.app.config.update(
        ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=True,
        SURVEY_PARTICIPATION_ASSURANCE_TENANT_IDS=str(tenant_id),
    )


@pytest.mark.parametrize("policy", ["por_phone", "por_cookie", "por_dni_o_phone", "libre"])
def test_strict_rejects_forged_identifiers_without_authoritative_grant(api, policy):
    create = {**api._create_payload(), "uniqueness_policy": policy}
    _, survey_id, token, _, answer = api._create_published_answer_context(create)
    _strict(api, api.tenant_1.id)
    for index in range(3):
        response = api._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "phone": f"+1555000000{index}", "dni": f"demo-{index}"},
            headers={"X-Anon-Id": f"rotated-cookie-{index}"},
        )
        assert response.status_code == 409, response.get_json()
        assert response.get_json()["reason_code"] == "survey_authoritative_participation_required"
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 0
    assert SurveyResponseReceipt.query.count() == 0
    assert SurveyEligibilityTerminal.query.count() == 0
    assert SurveyResponseEffect.query.count() == 0


@pytest.mark.parametrize("endpoint", ["v2", "legacy", "pwa"])
def test_strict_weak_admission_gate_is_shared_across_public_routes(api, endpoint):
    _, _, token, _, answer = api._create_published_answer_context(
        {**api._create_payload(), "uniqueness_policy": "por_cookie"}
    )
    _strict(api, api.tenant_1.id)
    path = {
        "v2": f"/api/v2/public/surveys/{token}/respond",
        "legacy": f"/api/public/encuestas/{token}/responder",
        "pwa": f"/api/pwa/public/surveys/{token}/respond?tenant={api.tenant_1.slug}",
    }[endpoint]
    result = api._post_public_response(path, json=answer, headers={"X-Anon-Id": "claimed-cookie"})
    assert result.status_code == 409, result.get_json()
    assert result.get_json()["reason_code"] == "survey_authoritative_participation_required"
    assert EncRespuesta.query.count() == 0


def test_strict_verified_accounts_share_nat_but_same_account_is_admitted_once(api):
    voter_a = api._create_user("assurance-a@test.invalid", "usuario", api.tenant_1.slug)
    voter_b = api._create_user("assurance-b@test.invalid", "usuario", api.tenant_1.slug)
    db.session.commit()
    _, survey_id, token, _, answer = api._create_published_answer_context(
        {**api._create_payload(), "uniqueness_policy": "por_usuario"}
    )
    _strict(api, api.tenant_1.id)
    path = f"/api/v2/public/surveys/{token}/respond"
    forged = api._post_public_response(path, json={**answer, "user_id": voter_a.id})
    assert forged.status_code == 401, forged.get_json()
    first = api._post_public_response(path, json=answer, headers=api._auth(voter_a))
    assert first.status_code == 201, first.get_json()
    for index in range(5):
        duplicate = api._post_public_response(
            path, json={**answer, "userId": voter_b.id, "phone": f"+155501{index}"},
            headers={**api._auth(voter_a), "X-Anon-Id": f"other-cookie-{index}"},
        )
        assert duplicate.status_code == 409, duplicate.get_json()
    # Both clients use the same test transport IP; IP does not establish a person.
    second = api._post_public_response(path, json=answer, headers=api._auth(voter_b))
    assert second.status_code == 201, second.get_json()
    rows = EncRespuesta.query.filter_by(encuesta_id=survey_id).all()
    assert len(rows) == 2
    assert {row.user_id for row in rows} == {voter_a.id, voter_b.id}
    assert SurveyResponseReceipt.query.count() == 2


def test_canary_off_preserves_legacy_rows_and_exact_receipt_after_strict_enabled(api):
    _, survey_id, token, _, answer = api._create_published_answer_context(
        {**api._create_payload(), "uniqueness_policy": "por_cookie"}
    )
    path = f"/api/v2/public/surveys/{token}/respond"
    key = "legacy-before-strict-0001"
    body = {**answer, "submission_id": key}
    headers = {"Idempotency-Key": key, "X-Anon-Id": "legacy-fixed-cookie"}
    first = api.client.post(path, json=body, headers=headers)
    assert first.status_code == 201, first.get_json()
    row = EncRespuesta.query.filter_by(encuesta_id=survey_id).one()
    original_fingerprint = row.huella_unica
    _strict(api, api.tenant_1.id)
    replay = api.client.post(path, json=body, headers=headers)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["response_id"] == first.get_json()["response_id"]
    assert EncRespuesta.query.count() == 1
    assert SurveyResponseReceipt.query.count() == 1
    assert db.session.get(EncRespuesta, row.id).huella_unica == original_fingerprint


def test_contract_exposes_legacy_or_limitation_and_strict_block_without_rewriting_counts(api):
    _, _, token, before, _ = api._create_published_answer_context(
        {**api._create_payload(), "uniqueness_policy": "por_dni_o_phone"}
    )
    contract = before["participation_assurance"]
    assert contract["strict_mode"] is False
    assert "legacy_combined_fingerprint_is_not_independent_or_match" in contract["warnings"]
    assert contract["phone_ownership_verified"] is False
    assert contract["unique_person_certified"] is False
    assert contract["ui"]["label"] == "Control básico"
    _strict(api, api.tenant_1.id)
    after = api.client.get(f"/api/v2/public/surveys/{token}").get_json()
    contract = after["participation_assurance"]
    assert contract["configuration_ready"] is False
    assert contract["ui"]["label"] == "Participación bloqueada"
    assert contract["blocked_reason_code"] == "survey_authoritative_participation_required"
    assert after["frontend_contract"]["participation_assurance"] == contract
    assert contract["historical_rows_reassessed"] is False
    assert before["resultados_envivo"] == after["resultados_envivo"]


@pytest.mark.parametrize("allowlist", ["", "1,garbage", "0", "-1"])
def test_enabled_strict_canary_misconfiguration_fails_closed(api, allowlist):
    _, _, token, _, answer = api._create_published_answer_context(
        {**api._create_payload(), "uniqueness_policy": "libre"}
    )
    api.app.config.update(
        ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=True,
        SURVEY_PARTICIPATION_ASSURANCE_TENANT_IDS=allowlist,
    )
    response = api._post_public_response(f"/api/v2/public/surveys/{token}/respond", json=answer)
    assert response.status_code == 503, response.get_json()
    assert response.get_json()["reason_code"] == "survey_participation_assurance_misconfigured"
    assert EncRespuesta.query.count() == 0


def test_strict_canary_does_not_enable_other_tenant(api):
    _, _, token, _, answer = api._create_published_answer_context(
        {**api._create_payload(), "uniqueness_policy": "libre"}
    )
    _strict(api, api.tenant_2.id)
    response = api._post_public_response(f"/api/v2/public/surveys/{token}/respond", json=answer)
    assert response.status_code == 201, response.get_json()


def _reviewed_survey(g, policy, *, anonymous=False):
    survey_id = g._create_survey()
    survey = db.session.get(EncEncuesta, survey_id)
    survey.politica_unicidad = policy
    if anonymous:
        survey.privacy_mode = "source_anonymous"
        survey.privacy_policy_version = "privacy-assurance-1"
        survey.privacy_policy_url = "https://chatboc.test/privacy"
        survey.privacy_consent_required = True
        survey.response_retention_days = 365
    db.session.commit()
    created = g._create_restricted_release(survey_id).get_json()
    g.app.config.update(
        ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1=True,
        SURVEY_ELIGIBILITY_GRANT_TENANT_IDS=str(g.tenant_1.id),
        SURVEY_ELIGIBILITY_SECRET_V1="local-eligibility-test-32-byte-secret-only",
        SURVEY_IDENTITY_HMAC_SECRET_V1="local-identity-test-32-byte-secret-only",
    )
    g._publish_release(survey_id, created["release_id"], created["snapshot_sha256"])
    link = EncLink.query.filter_by(encuesta_id=survey_id).one()
    public = g.client.get(f"/api/v2/public/surveys/{link.slug_publico}").get_json()
    question = public["preguntas"][0]
    answer = {
        "instrument_revision": public["instrument_revision"],
        "respuestas": [{"pregunta_id": question["id"], "opcion_id": question["opciones"][0]["id"]}],
        "governance": {
            "release_id": created["release_id"],
            "snapshot_sha256": created["snapshot_sha256"],
            "eligibility_policy_version": "eligibility-2026.1",
            "consent_policy_version": "consent-2026.1",
            "eligibility_acknowledged": True,
            "consent_accepted": True,
        },
    }
    if anonymous:
        answer.update(privacy_consent=True, privacy_policy_version="privacy-assurance-1")
    _strict(g, g.tenant_1.id)
    return survey_id, created["release_id"], link.slug_publico, answer


def _issue(g, survey_id, release_id, letter):
    response = g.client.post(
        f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants",
        json={"subject_ref": "subj_" + letter * 43, "review_reference": f"review:synthetic:{letter}:0001"},
        headers=g._headers(g.owner_1, g.tenant_1, key=f"issue:synthetic:{letter}:0001"),
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["credential"]


@pytest.mark.parametrize("policy", ["por_phone", "por_cookie", "por_dni_o_phone", "libre"])
def test_strict_real_grants_allow_distinct_reviewed_subjects_despite_same_claimed_identifiers(governed, policy):
    g = governed
    survey_id, release_id, token, answer = _reviewed_survey(g, policy)
    path = f"/api/v2/public/surveys/{token}/respond"
    for letter in ("A", "B"):
        credential = _issue(g, survey_id, release_id, letter)
        key = f"strict-grant-response-{letter}-0001"
        body = {**answer, "submission_id": key, "phone": "+15550000000", "dni": "synthetic-same-dni"}
        headers = {
            "Idempotency-Key": key, "X-Anon-Id": "same-browser-cookie",
            "X-Survey-Eligibility-Credential": credential,
        }
        first = g.client.post(path, json=body, headers=headers)
        assert first.status_code == 201, first.get_json()
        replay = g.client.post(path, json=body, headers=headers)
        assert replay.status_code == 200, replay.get_json()
        assert replay.get_json()["response_id"] == first.get_json()["response_id"]
        conflict = g.client.post(
            path, json={**body, "phone": "+15559999999"}, headers=headers,
        )
        assert conflict.status_code == 409, conflict.get_json()
        assert conflict.get_json()["reason_code"] == "survey_submission_id_conflict"
        changed_key = key + "-different"
        duplicate = g.client.post(
            path, json={**body, "submission_id": changed_key, "phone": "+15551111111"},
            headers={**headers, "Idempotency-Key": changed_key, "X-Anon-Id": "rotated-cookie"},
        )
        assert duplicate.status_code == 409, duplicate.get_json()
        assert duplicate.get_json()["reason_code"] == "survey_eligibility_credential_consumed"
    rows = EncRespuesta.query.filter_by(encuesta_id=survey_id).all()
    assert len(rows) == 2
    assert len({row.huella_unica for row in rows}) == 2
    assert all(row.huella_unica.startswith("reviewed-subject-hmac-v1:") for row in rows)
    assert SurveyResponseReceipt.query.count() == 2
    assert SurveyEligibilityTerminal.query.count() == 2
    assert SurveyResponseEffect.query.count() > 0


def test_strict_grant_source_anonymous_retains_hmac_not_phone_or_transport_identity(governed):
    g = governed
    survey_id, release_id, token, answer = _reviewed_survey(g, "por_dni_o_phone", anonymous=True)
    credential = _issue(g, survey_id, release_id, "C")
    key = "strict-private-grant-0001"
    response = g.client.post(
        f"/api/v2/public/surveys/{token}/respond",
        json={**answer, "submission_id": key, "phone": "+15552222222", "dni": "synthetic-doc"},
        headers={"Idempotency-Key": key, "X-Survey-Eligibility-Credential": credential},
    )
    assert response.status_code == 201, response.get_json()
    row = EncRespuesta.query.filter_by(encuesta_id=survey_id).one()
    assert row.huella_unica.startswith("reviewed-subject-hmac-v1:")
    assert row.phone is row.dni is row.ip is row.user_id is row.ua is None
    assert credential not in json.dumps(response.get_json())


def test_strict_claimed_grant_string_without_db_verification_does_not_admit(governed):
    g = governed
    survey_id, _, token, answer = _reviewed_survey(g, "por_cookie")
    key = "forged-opaque-grant-0001"
    response = g.client.post(
        f"/api/v2/public/surveys/{token}/respond", json={**answer, "submission_id": key},
        headers={"Idempotency-Key": key, "X-Survey-Eligibility-Credential": "sec1_" + "A" * 43},
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["reason_code"] == "survey_eligibility_credential_invalid"
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 0
    assert SurveyEligibilityTerminal.query.count() == 0


def test_strict_distributed_rate_rejects_actual_memory_even_if_uri_claims_redis(api):
    _, _, token, _, answer = api._create_published_answer_context(api._create_payload())
    api.app.config.update(
        ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT=True,
        RATELIMIT_STORAGE_URI="redis://localhost:6379",
    )
    with patch("services.public_survey_intake.limiter.limiter.hit") as hit:
        response = api._post_public_response(f"/api/v2/public/surveys/{token}/respond", json=answer)
    assert response.status_code == 503, response.get_json()
    assert response.get_json()["reason_code"] == "survey_rate_limit_unavailable"
    hit.assert_not_called()
    assert EncRespuesta.query.count() == 0
    assert SurveyResponseEffect.query.count() == 0


@pytest.mark.parametrize("connected", [True, False])
def test_strict_distributed_rate_uses_actual_redis_and_denies_storage_error(api, connected):
    from services.public_survey_intake import _consume_rate_limit

    api.app.config["ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT"] = True
    # Instantiation creates no socket; the two I/O operations are injected below.
    storage = RedisStorage("redis://localhost:6379")
    strategy = SimpleNamespace(
        storage=storage,
        hit=Mock(return_value=True, side_effect=None if connected else RuntimeError("offline test")),
        get_window_stats=Mock(return_value=SimpleNamespace(remaining=149, reset_time=0)),
    )
    with patch.object(limiter, "_limiter", strategy):
        result = _consume_rate_limit("synthetic-survey", client_ip="203.0.113.10")
    assert result["allowed"] is connected
    assert result["available"] is connected


def test_strict_distributed_rate_rejects_memory_fallback_from_dead_redis(api):
    from services.public_survey_intake import _consume_rate_limit

    api.app.config["ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT"] = True
    redis_strategy = SimpleNamespace(storage=RedisStorage("redis://localhost:6379"), hit=Mock())
    fallback = FixedWindowRateLimiter(MemoryStorage())
    with (
        patch.object(limiter, "_limiter", redis_strategy),
        patch.object(limiter, "_storage_dead", True),
        patch.object(limiter, "_in_memory_fallback_enabled", True),
        patch.object(limiter, "_fallback_limiter", fallback),
        patch.object(fallback, "hit") as fallback_hit,
    ):
        result = _consume_rate_limit("synthetic-survey", client_ip="203.0.113.10")
    assert result["available"] is False
    assert result["allowed"] is False
    redis_strategy.hit.assert_not_called()
    fallback_hit.assert_not_called()


def test_strict_canary_does_not_bypass_government_evidence_publication_gate(api):
    api.tenant_1.tipo = "municipio"
    db.session.commit()
    _strict(api, api.tenant_1.id)
    headers = {**api._auth(api.admin_1), "X-Tenant-Slug": api.tenant_1.slug}
    created = api.client.post(
        "/api/v2/surveys", json={**api._create_payload(), "uniqueness_policy": "por_usuario"},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    survey_id = created.get_json()["id"]
    blocked = api.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers)
    assert blocked.status_code == 409, blocked.get_json()
    assert blocked.get_json()["reason_code"] == "survey_tenant_jurisdiction_unverified"
    assert db.session.get(EncEncuesta, survey_id).estado == "borrador"
    assert EncRespuesta.query.count() == 0


def test_concurrent_distinct_keys_same_verified_account_admit_one_row_and_receipt(concurrency_app):
    from services.encuestas_service import EncuestaError, save_respuesta

    app = concurrency_app
    context = concurrency_helpers._publish_for_threads(app)
    with app.app_context():
        survey = db.session.get(EncEncuesta, context["survey_id"])
        survey.politica_unicidad = "por_usuario"
        voter = User(name="Synthetic participant", email="concurrent-assurance@test.invalid", rol="usuario")
        voter.set_password("synthetic-local-only-password")
        db.session.add(voter)
        db.session.commit()
        voter_id = voter.id
        app.config.update(
            ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=True,
            SURVEY_PARTICIPATION_ASSURANCE_TENANT_IDS=str(survey.tenant_id),
        )
    barrier = threading.Barrier(2)
    outcomes = []
    failures = []

    def submit(index):
        with app.app_context():
            try:
                key = f"concurrent-account-admission-{index}-0001"
                user = db.session.get(User, voter_id)
                barrier.wait(timeout=5)
                row = save_respuesta(
                    context["slug"],
                    {**concurrency_helpers._response_payload(context), "submission_id": key},
                    concurrency_helpers._request_context(), authenticated_user=user, submission_id=key,
                )
                outcomes.append((201, row.id))
            except EncuestaError as exc:
                db.session.rollback()
                outcomes.append((exc.status_code, (exc.payload or {}).get("reason_code")))
            except Exception as exc:
                db.session.rollback()
                failures.append(type(exc).__name__)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert sorted(status for status, _ in outcomes) == [201, 409]
    assert (409, "survey_response_duplicate") in outcomes
    with app.app_context():
        assert EncRespuesta.query.filter_by(encuesta_id=context["survey_id"]).count() == 1
        assert SurveyResponseReceipt.query.filter_by(survey_id=context["survey_id"]).count() == 1
