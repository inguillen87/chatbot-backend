from __future__ import annotations

from models import AnalyticsEventV2, TenantProfile, User, db


def _create_owner(owner_id: int, *, scope: str = "municipio") -> User:
    owner = User(
        id=owner_id,
        name=f"Analytics owner {owner_id}",
        email=f"analytics-owner-{owner_id}@example.com",
        rol="admin",
        tipo_chat=scope,
        municipio_id=owner_id if scope == "municipio" else None,
        pyme_id=owner_id if scope == "pyme" else None,
    )
    owner.set_password("test")
    db.session.add(owner)
    db.session.flush()
    return owner


def _create_tenant(
    owner_id: int,
    *,
    slug: str,
    scope: str = "municipio",
    profile_id: int | None = None,
) -> TenantProfile:
    tenant = TenantProfile(
        id=profile_id,
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo=scope,
        municipio_id=owner_id if scope == "municipio" else None,
        pyme_id=owner_id if scope != "municipio" else None,
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def _debug_headers(owner_id: int) -> dict[str, str]:
    return {
        "X-Debug-Role": "operador",
        "X-Debug-Tenant": str(owner_id),
    }


def test_event_ingest_resolves_unique_legacy_owner_to_profile_id(client):
    owner_id = 81_001
    _create_owner(owner_id)
    tenant = _create_tenant(owner_id, slug="analytics-owner-normalized")
    db.session.commit()

    response = client.post(
        "/analytics/event",
        json={
            "tenant_id": owner_id,
            "tenant_type": "municipio",
            "event_name": "message_received",
        },
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["tenant_id"] == owner_id
    assert payload["tenant_profile_id"] == tenant.id
    event = AnalyticsEventV2.query.filter_by(event_name="message_received").one()
    assert event.tenant_id == tenant.id


def test_event_ingest_accepts_explicit_profile_and_slug_when_consistent(client):
    owner_id = 81_002
    _create_owner(owner_id)
    tenant = _create_tenant(owner_id, slug="analytics-explicit-profile")
    db.session.commit()

    response = client.post(
        "/analytics/event",
        query_string={"tenant_slug": tenant.slug},
        json={
            "tenant_profile_id": tenant.id,
            "tenant_type": "municipio",
            "event_name": "ticket_created",
        },
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["tenant_profile_id"] == tenant.id
    assert set(payload["tenant_resolution"]["resolution_sources"]) == {
        "tenant_profile_id",
        "tenant_slug",
    }
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="ticket_created",
    ).count() == 1


def test_event_ingest_preserves_slug_in_legacy_tenant_id_field(client):
    owner_id = 81_007
    _create_owner(owner_id)
    tenant = _create_tenant(owner_id, slug="analytics-legacy-slug-field")
    db.session.commit()

    response = client.post(
        "/analytics/event",
        json={
            "tenant_id": tenant.slug,
            "tenant_type": "municipio",
            "event_name": "portal_session_opened",
        },
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["tenant_id"] == owner_id
    assert payload["tenant_profile_id"] == tenant.id
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="portal_session_opened",
    ).count() == 1


def test_event_ingest_supports_school_vertical_with_pyme_owner_scope(client):
    owner_id = 81_008
    _create_owner(owner_id, scope="pyme")
    tenant = _create_tenant(
        owner_id,
        slug="analytics-school-vertical",
        scope="colegio",
    )
    db.session.commit()

    response = client.post(
        "/analytics/event",
        json={
            "tenant_profile_id": tenant.id,
            "tenant_type": "colegio",
            "event_name": "portal_session_opened",
        },
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["tenant_profile_id"] == tenant.id
    event = AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="portal_session_opened",
    ).one()
    assert event.tenant_type == "colegio"


def test_event_ingest_fails_closed_for_scope_incompatible_profile(client):
    owner_id = 81_003
    _create_owner(owner_id)
    tenant = _create_tenant(owner_id, slug="analytics-municipio-only")
    db.session.commit()

    response = client.post(
        "/analytics/event",
        json={
            "tenant_profile_id": tenant.id,
            "tenant_type": "pyme",
            "event_name": "order_created",
        },
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 202
    assert response.get_json()["accepted"] is False
    assert AnalyticsEventV2.query.count() == 0


def test_event_ingest_fails_closed_for_ambiguous_legacy_owner(client):
    owner_id = 81_004
    _create_owner(owner_id)
    _create_tenant(owner_id, slug="analytics-ambiguous-a")
    _create_tenant(owner_id, slug="analytics-ambiguous-b")
    db.session.commit()

    response = client.post(
        "/analytics/event",
        json={"tenant_id": owner_id, "event_name": "message_received"},
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 202
    assert response.get_json()["accepted"] is False
    assert AnalyticsEventV2.query.count() == 0


def test_event_ingest_fails_closed_for_numeric_namespace_collision(client):
    colliding_id = 81_005
    exact_owner_id = 81_006
    _create_owner(exact_owner_id)
    _create_owner(colliding_id)
    _create_tenant(
        exact_owner_id,
        slug="analytics-profile-collision",
        profile_id=colliding_id,
    )
    _create_tenant(colliding_id, slug="analytics-owner-collision")
    db.session.commit()

    response = client.post(
        "/analytics/event",
        json={"tenant_id": colliding_id, "event_name": "message_received"},
        headers=_debug_headers(colliding_id),
    )

    assert response.status_code == 202
    assert response.get_json()["accepted"] is False
    assert AnalyticsEventV2.query.count() == 0


def test_identity_coverage_reads_profile_scoped_events_from_unique_owner(client):
    owner_id = 81_009
    _create_owner(owner_id)
    tenant = _create_tenant(owner_id, slug="analytics-identity-owner")
    db.session.add(
        AnalyticsEventV2(
            tenant_id=tenant.id,
            tenant_type="municipio",
            event_name="message_received",
            channel="whatsapp",
            session_id="conversation-identity-1",
        )
    )
    db.session.commit()

    response = client.get(
        "/analytics/identity/coverage",
        query_string={"tenant_id": owner_id, "scope": "municipio"},
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["event_tenant_id"] == tenant.id
    assert payload["tenant_resolution"]["tenant_profile_id"] == tenant.id
    assert payload["tenant_resolution"]["owner_tenant_id"] == owner_id
    assert payload["sample_size"] == 1


def test_identity_coverage_rejects_ambiguous_owner_before_query(client):
    owner_id = 81_010
    _create_owner(owner_id)
    _create_tenant(owner_id, slug="analytics-identity-ambiguous-a")
    _create_tenant(owner_id, slug="analytics-identity-ambiguous-b")
    db.session.commit()

    response = client.get(
        "/analytics/identity/coverage",
        query_string={"tenant_id": owner_id, "scope": "municipio"},
        headers=_debug_headers(owner_id),
    )

    assert response.status_code == 409
    assert AnalyticsEventV2.query.count() == 0
