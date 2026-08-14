from types import SimpleNamespace

import pytest
from flask import g

from extensions import db
from models import EncEncuesta, EncRespuesta, TenantProfile, User
from routes import portal_api, pwa_public
from services.survey_tenant_scope import (
    SurveyTenantScopeError,
    resolve_survey_storage_tenant_profile,
    resolve_survey_tenant_scope_id,
    resolve_survey_tenant_profile_reference,
)


def _owner(*, user_id: int, email: str) -> User:
    owner = User(
        id=user_id,
        name="Owner",
        email=email,
        password_hash="hash",
        rol="admin",
        tipo_chat="municipio",
    )
    db.session.add(owner)
    return owner


def _tenant(
    *,
    tenant_id: int,
    slug: str,
    owner_id: int,
    survey_alias: int | None = None,
) -> TenantProfile:
    tenant = TenantProfile(
        id=tenant_id,
        slug=slug,
        nombre=slug,
        tipo="municipio",
        municipio_id=owner_id,
        encuestas_tenant_id=survey_alias,
    )
    db.session.add(tenant)
    return tenant


def test_scope_uses_canonical_tenant_id_and_never_owner_id(client):
    owner = _owner(user_id=7101, email="scope-canonical@example.com")
    tenant = _tenant(
        tenant_id=7201,
        slug="scope-canonical",
        owner_id=owner.id,
    )
    db.session.commit()

    assert resolve_survey_tenant_scope_id(tenant) == tenant.id
    assert resolve_survey_tenant_scope_id(tenant) != owner.id


def test_scope_keeps_canonical_id_when_legacy_alias_exists(client):
    owner = _owner(user_id=7102, email="scope-legacy@example.com")
    tenant = _tenant(
        tenant_id=7202,
        slug="scope-legacy",
        owner_id=owner.id,
        survey_alias=9702,
    )
    db.session.commit()

    assert resolve_survey_tenant_scope_id(tenant) == tenant.id
    assert resolve_survey_tenant_profile_reference(9702).id == tenant.id


@pytest.mark.parametrize(
    (
        "canonical_tenant_id",
        "canonical_owner_id",
        "canonical_slug",
        "colliding_tenant_id",
        "colliding_slug",
    ),
    [
        (3, 4, "ferreteria", 5, "clinica-horizonte"),
        (22, 4, "junin", 20, "bodega"),
    ],
)
def test_persisted_scope_uses_direct_profile_over_owner_collision(
    client,
    canonical_tenant_id,
    canonical_owner_id,
    canonical_slug,
    colliding_tenant_id,
    colliding_slug,
):
    from routes.v2.surveys import _tenant_slug_for_resolved_survey
    from socket_service import _tenant_slug_for_survey_tenant_id

    canonical_owner = _owner(
        user_id=canonical_owner_id,
        email=f"scope-storage-{canonical_slug}@example.com",
    )
    colliding_owner = _owner(
        user_id=canonical_tenant_id,
        email=f"scope-storage-{colliding_slug}@example.com",
    )
    canonical = _tenant(
        tenant_id=canonical_tenant_id,
        slug=canonical_slug,
        owner_id=canonical_owner.id,
    )
    _tenant(
        tenant_id=colliding_tenant_id,
        slug=colliding_slug,
        owner_id=colliding_owner.id,
    )
    db.session.commit()

    assert resolve_survey_storage_tenant_profile(canonical.id).id == canonical.id
    assert _tenant_slug_for_survey_tenant_id(canonical.id) == canonical.slug
    assert (
        _tenant_slug_for_resolved_survey(
            SimpleNamespace(tenant_id=canonical.id)
        )
        == canonical.slug
    )


@pytest.mark.parametrize("legacy_reference_kind", ["alias", "owner"])
def test_persisted_scope_rejects_legacy_alias_or_owner_namespace(
    client,
    legacy_reference_kind,
):
    owner_id = 7310 if legacy_reference_kind == "alias" else 7311
    legacy_scope_id = 7390 if legacy_reference_kind == "alias" else owner_id
    tenant = _tenant(
        tenant_id=7320 if legacy_reference_kind == "alias" else 7321,
        slug=f"scope-storage-legacy-{legacy_reference_kind}",
        owner_id=_owner(
            user_id=owner_id,
            email=f"scope-storage-legacy-{legacy_reference_kind}@example.com",
        ).id,
        survey_alias=legacy_scope_id if legacy_reference_kind == "alias" else None,
    )
    db.session.commit()

    with pytest.raises(SurveyTenantScopeError) as exc_info:
        resolve_survey_storage_tenant_profile(legacy_scope_id)

    assert exc_info.value.reason_code == "survey_tenant_storage_scope_not_canonical"
    assert exc_info.value.tenant_id == tenant.id
    assert exc_info.value.candidate_scope_id == legacy_scope_id


def test_inbound_legacy_owner_reference_remains_ambiguous_on_direct_collision(client):
    canonical_owner = _owner(
        user_id=7330,
        email="scope-inbound-direct-owner@example.com",
    )
    colliding_owner = _owner(
        user_id=3,
        email="scope-inbound-legacy-owner@example.com",
    )
    _tenant(
        tenant_id=3,
        slug="scope-inbound-direct",
        owner_id=canonical_owner.id,
    )
    _tenant(
        tenant_id=5,
        slug="scope-inbound-owner-collision",
        owner_id=colliding_owner.id,
    )
    db.session.commit()

    with pytest.raises(SurveyTenantScopeError) as exc_info:
        resolve_survey_tenant_profile_reference(3, allow_legacy_owner=True)

    assert exc_info.value.reason_code == "survey_tenant_scope_ambiguous"


def test_whatsapp_scope_uses_tenant_profile_and_never_owner_id(client):
    from services import municipio_responder

    owner = _owner(user_id=7103, email="scope-whatsapp@example.com")
    tenant = _tenant(
        tenant_id=7203,
        slug="scope-whatsapp",
        owner_id=owner.id,
    )
    db.session.commit()

    assert (
        municipio_responder._resolve_encuestas_tenant_id(
            {
                "tenant_profile": tenant,
                "tenant_id": tenant.id,
                "municipio_id": owner.id,
                "user_obj": SimpleNamespace(id=owner.id),
            }
        )
        == tenant.id
    )
    assert (
        municipio_responder._resolve_encuestas_tenant_id(
            {
                "municipio_id": owner.id,
                "user_obj": SimpleNamespace(id=owner.id),
            }
        )
        is None
    )


def test_whatsapp_public_slug_prefers_authoritative_tenant_profile(client):
    from services import municipio_responder

    owner = _owner(user_id=7106, email="scope-whatsapp-slug@example.com")
    tenant = _tenant(
        tenant_id=7206,
        slug="municipalidad-de-junin",
        owner_id=owner.id,
    )
    db.session.commit()

    assert (
        municipio_responder._resolve_tenant_slug(
            {
                "tenant_profile": tenant,
                "tenant_id": tenant.id,
                "municipio_id": owner.id,
                "municipio_config_actual": {},
            }
        )
        == tenant.slug
    )


def test_whatsapp_scope_remains_canonical_when_legacy_alias_is_ambiguous(client):
    from services import municipio_responder

    first_owner = _owner(user_id=7104, email="scope-whatsapp-a@example.com")
    second_owner = _owner(user_id=7105, email="scope-whatsapp-b@example.com")
    first = _tenant(
        tenant_id=7204,
        slug="scope-whatsapp-a",
        owner_id=first_owner.id,
        survey_alias=9704,
    )
    _tenant(
        tenant_id=7205,
        slug="scope-whatsapp-b",
        owner_id=second_owner.id,
        survey_alias=9704,
    )
    db.session.commit()

    assert municipio_responder._resolve_encuestas_tenant_id(
        {"tenant_profile": first, "tenant_id": first.id}
    ) == first.id


@pytest.mark.parametrize("collision_kind", ["shared_alias", "canonical_id"])
def test_inbound_legacy_reference_fails_closed_when_ambiguous(client, collision_kind):
    first_owner = _owner(user_id=7110, email=f"scope-a-{collision_kind}@example.com")
    second_owner = _owner(user_id=7111, email=f"scope-b-{collision_kind}@example.com")
    if collision_kind == "shared_alias":
        first = _tenant(
            tenant_id=7210,
            slug="scope-a-shared",
            owner_id=first_owner.id,
            survey_alias=9710,
        )
        _tenant(
            tenant_id=7211,
            slug="scope-b-shared",
            owner_id=second_owner.id,
            survey_alias=9710,
        )
    else:
        first = _tenant(
            tenant_id=7220,
            slug="scope-a-canonical-collision",
            owner_id=first_owner.id,
            survey_alias=7221,
        )
        _tenant(
            tenant_id=7221,
            slug="scope-b-canonical-collision",
            owner_id=second_owner.id,
        )
    db.session.commit()

    assert resolve_survey_tenant_scope_id(first) == first.id
    with pytest.raises(SurveyTenantScopeError) as exc_info:
        resolve_survey_tenant_profile_reference(first.encuestas_tenant_id)

    assert exc_info.value.reason_code == "survey_tenant_scope_ambiguous"


def test_pwa_surveys_use_canonical_scope_without_legacy_alias(client, monkeypatch):
    owner = _owner(user_id=7130, email="scope-pwa@example.com")
    tenant = _tenant(
        tenant_id=7230,
        slug="scope-pwa",
        owner_id=owner.id,
    )
    db.session.commit()
    captured = {}

    monkeypatch.setattr(pwa_public, "_require_tenant", lambda: tenant)

    def _list(scope_id, *, limit):
        captured.update(scope_id=scope_id, limit=limit)
        return []

    monkeypatch.setattr(pwa_public, "list_public_encuestas_for_tenant", _list)

    response = client.get("/api/pwa/public/surveys")

    assert response.status_code == 200
    assert response.get_json() == []
    assert captured == {"scope_id": tenant.id, "limit": 25}


def test_pwa_surveys_ignore_ambiguous_legacy_alias_and_use_canonical(client, monkeypatch):
    first_owner = _owner(user_id=7140, email="scope-pwa-a@example.com")
    second_owner = _owner(user_id=7141, email="scope-pwa-b@example.com")
    first = _tenant(
        tenant_id=7240,
        slug="scope-pwa-a",
        owner_id=first_owner.id,
        survey_alias=9740,
    )
    _tenant(
        tenant_id=7241,
        slug="scope-pwa-b",
        owner_id=second_owner.id,
        survey_alias=9740,
    )
    db.session.commit()
    monkeypatch.setattr(pwa_public, "_require_tenant", lambda: first)

    captured = {}
    monkeypatch.setattr(
        pwa_public,
        "list_public_encuestas_for_tenant",
        lambda scope_id, *, limit: captured.update(scope_id=scope_id, limit=limit) or [],
    )

    response = client.get("/api/pwa/public/surveys")

    assert response.status_code == 200
    assert captured["scope_id"] == first.id


def test_portal_available_surveys_matches_responses_in_canonical_scope(client, monkeypatch):
    owner = _owner(user_id=7150, email="scope-portal-owner@example.com")
    viewer = _owner(user_id=7151, email="scope-portal-viewer@example.com")
    viewer.rol = "usuario"
    tenant = _tenant(
        tenant_id=7250,
        slug="scope-portal",
        owner_id=owner.id,
        survey_alias=9750,
    )
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug="scope-portal-survey",
        titulo="Consulta institucional",
        tipo="opinion",
        estado="publicada",
    )
    db.session.add(survey)
    db.session.flush()
    db.session.add(
        EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=tenant.id,
            user_id=viewer.id,
            huella_unica="scope-portal-viewer-response",
        )
    )
    db.session.commit()
    captured = {}

    def _list(scope_id, *, limit):
        captured.update(scope_id=scope_id, limit=limit)
        return [(survey, survey.slug)]

    monkeypatch.setattr(portal_api, "list_public_encuestas_for_tenant", _list)

    assert portal_api._portal_available_surveys(tenant, viewer) == []
    assert captured["scope_id"] == tenant.id


def test_portal_submission_binds_canonical_scope_explicitly(client, monkeypatch):
    owner = _owner(user_id=7160, email="scope-submit-owner@example.com")
    viewer = _owner(user_id=7161, email="scope-submit-viewer@example.com")
    viewer.rol = "usuario"
    tenant = _tenant(
        tenant_id=7260,
        slug="scope-submit",
        owner_id=owner.id,
        survey_alias=9760,
    )
    db.session.commit()
    captured = {}

    monkeypatch.setattr(portal_api, "_resolve_context", lambda _slug: tenant)
    monkeypatch.setattr(portal_api, "survey_response_receipt_contract", lambda _response: None)

    def _save(slug, payload, request_ctx, **kwargs):
        captured.update(
            slug=slug,
            payload=payload,
            request_ctx=request_ctx,
            kwargs=kwargs,
        )
        return SimpleNamespace(
            id=991,
            submission_replayed=False,
            instrument_revision=1,
        )

    monkeypatch.setattr(portal_api, "save_respuesta", _save)
    monkeypatch.setattr(
        "services.survey_governance.response_governance_contract",
        lambda _response: {},
    )

    path = f"/api/v1/portal/{tenant.slug}/surveys/scope-vote/responses"
    with client.application.test_request_context(
        path,
        method="POST",
        json={"respuestas": []},
        headers={"Idempotency-Key": "scope-submit-0001"},
    ):
        g.viewer = viewer
        response, status_code = portal_api.submit_portal_survey_response(
            tenant.slug,
            "scope-vote",
        )

    assert status_code == 201
    assert response.get_json()["persisted"] is True
    assert captured["kwargs"]["preferred_tenant_id"] == tenant.id
    assert captured["kwargs"]["require_tenant_match"] is True
