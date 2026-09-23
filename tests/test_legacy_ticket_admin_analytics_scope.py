from datetime import datetime, timedelta, timezone

import jwt

from app import db
from models import (
    AnalyticsEvent,
    MunicipioTicket,
    TenantProfile,
    TicketComentario,
    TicketRealtimeState,
    User,
)
from services.analytics_service import (
    MUNICIPIO_CONSULTANT_REPORT_TYPE,
    MUNICIPIO_TICKET_SCOPE_CACHE_CONTRACT,
    analytics_service,
)


def _headers(app, user: User) -> dict[str, str]:
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _owner(*, suffix: str, tipo_chat: str) -> User:
    user = User(
        name=f"Owner {suffix}",
        email=f"owner-{suffix}@test.com",
        rol="admin",
        tipo_chat=tipo_chat,
    )
    user.set_password("scope-test-secret")
    db.session.add(user)
    db.session.flush()
    return user


def _municipio_tenant(*, suffix: str) -> tuple[User, TenantProfile]:
    owner = _owner(suffix=suffix, tipo_chat="municipio")
    tenant = TenantProfile(
        slug=f"municipio-{suffix}",
        nombre=f"Municipio {suffix}",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    return owner, tenant


def test_pyme_tenant_quarantines_cross_domain_municipio_ticket_from_admin_reads(
    client,
    app,
):
    owner = _owner(suffix="pyme-quarantine", tipo_chat="pyme")
    tenant = TenantProfile(
        slug="pyme-quarantine",
        nombre="PYME quarantine",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id

    employee = User(
        name="PYME employee",
        email="pyme-quarantine-employee@test.com",
        rol="empleado",
        tipo_chat="pyme",
        es_empleado=True,
        tenant_id=tenant.id,
    )
    employee.set_password("scope-test-secret")
    db.session.add(employee)
    db.session.flush()

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner.id,
        pregunta="PYME_CROSS_DOMAIN_SECRET",
        asunto="PYME_CROSS_DOMAIN_SECRET",
        categoria="pyme_cross_domain_secret",
        distrito="zona_cross_domain_secret",
        latitud=-54.80123,
        longitud=-68.30123,
        estado="nuevo",
        asignado_a_id=employee.id,
    )
    db.session.add(ticket)
    db.session.flush()
    db.session.add(
        TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario="PYME_CROSS_DOMAIN_COMMENT",
            es_admin=False,
        )
    )
    db.session.add(
        TicketRealtimeState(
            ticket_type="municipio",
            ticket_id=ticket.id,
            viewer_key=f"user:{employee.id}",
            viewer_user_id=employee.id,
            viewer_role="empleado",
            presence_status="active",
            last_read_comment_id=0,
        )
    )
    db.session.commit()

    headers = _headers(app, owner)
    responses = {
        "leads": client.get(
            f"/api/admin/tenants/{tenant.slug}/leads",
            headers=headers,
        ),
        "unread": client.get(
            f"/api/admin/tenants/{tenant.slug}/tickets/unread-summary",
            headers=headers,
        ),
        "bundle": client.get(
            f"/api/admin/tenants/{tenant.slug}/dashboard-bundle",
            headers=headers,
        ),
        "heatmap": client.get(
            f"/api/admin/tenants/{tenant.slug}/heatmap-summary",
            headers=headers,
        ),
    }

    assert {name: response.status_code for name, response in responses.items()} == {
        "leads": 200,
        "unread": 200,
        "bundle": 200,
        "heatmap": 200,
    }
    serialized = {name: response.get_json() for name, response in responses.items()}
    assert serialized["leads"]["total"] == 0
    assert serialized["unread"]["total_tickets_with_unread"] == 0
    assert serialized["bundle"]["summary"]["total_leads"] == 0
    assert serialized["bundle"]["summary"]["tickets_with_unread"] == 0
    assert serialized["bundle"]["summary"]["active_viewers"] == 0
    assert serialized["heatmap"]["total"] == 0
    assert "PYME_CROSS_DOMAIN" not in str(serialized)
    assert "zona_cross_domain_secret" not in str(serialized)

    auto_assign = client.post(
        f"/api/admin/tenants/{tenant.slug}/tickets/municipio/{ticket.id}/auto-assign",
        headers=headers,
    )
    update_stage = client.patch(
        f"/api/admin/tenants/{tenant.slug}/leads/municipio/{ticket.id}/stage",
        json={"stage": "ganado"},
        headers=headers,
    )
    assert auto_assign.status_code == 404
    assert update_stage.status_code == 404
    db.session.expire_all()
    persisted = db.session.get(MunicipioTicket, ticket.id)
    assert persisted.estado == "nuevo"
    assert persisted.asignado_a_id == employee.id


def test_municipal_admin_and_analytics_include_unique_legacy_but_exclude_foreign(
    client,
    app,
):
    owner, tenant = _municipio_tenant(suffix="legacy-visible")
    foreign_owner, foreign_tenant = _municipio_tenant(suffix="foreign-hidden")
    now = datetime.now(timezone.utc)

    explicit = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner.id,
        pregunta="Explicit municipal",
        categoria="explicit_allowed",
        distrito="centro",
        latitud=-34.60,
        longitud=-58.40,
        estado="resuelto",
        fecha=now,
    )
    legacy = MunicipioTicket(
        tenant_id=None,
        municipio_id=owner.id,
        pregunta="Unique legacy municipal",
        categoria="legacy_allowed",
        distrito="norte",
        latitud=-34.61,
        longitud=-58.41,
        estado="nuevo",
        fecha=now,
    )
    foreign = MunicipioTicket(
        tenant_id=foreign_tenant.id,
        municipio_id=foreign_owner.id,
        pregunta="FOREIGN_MUNICIPAL_SECRET",
        categoria="foreign_hidden",
        distrito="foreign_hidden",
        latitud=-34.62,
        longitud=-58.42,
        estado="nuevo",
        fecha=now,
    )
    db.session.add_all([explicit, legacy, foreign])
    db.session.flush()
    db.session.add(
        TicketComentario(
            municipio_ticket_id=legacy.id,
            comentario="Unique legacy unread",
            es_admin=False,
            fecha=now,
        )
    )
    db.session.commit()

    headers = _headers(app, owner)
    leads = client.get(
        f"/api/admin/tenants/{tenant.slug}/leads",
        headers=headers,
    )
    unread = client.get(
        f"/api/admin/tenants/{tenant.slug}/tickets/unread-summary",
        headers=headers,
    )
    bundle = client.get(
        f"/api/admin/tenants/{tenant.slug}/dashboard-bundle",
        headers=headers,
    )
    heatmap = client.get(
        f"/api/admin/tenants/{tenant.slug}/heatmap-summary",
        headers=headers,
    )

    assert [response.status_code for response in (leads, unread, bundle, heatmap)] == [
        200,
        200,
        200,
        200,
    ]
    expected_ids = {explicit.id, legacy.id}
    assert {item["ticket_id"] for item in leads.get_json()["items"]} == expected_ids
    assert {item["ticket_id"] for item in unread.get_json()["items"]} == {legacy.id}
    assert {
        item["ticket_id"] for item in bundle.get_json()["leads"]["items"]
    } == expected_ids
    assert {
        point["ticket_id"]
        for point in heatmap.get_json()["heatmap_points"]
        if point.get("source") == "ticket"
    } == expected_ids
    assert "FOREIGN_MUNICIPAL_SECRET" not in str(
        [leads.get_json(), unread.get_json(), bundle.get_json(), heatmap.get_json()]
    )

    start = now - timedelta(days=1)
    end = now + timedelta(days=1)
    summary = analytics_service.get_summary(
        tenant.id,
        start,
        end,
        context="municipio",
    )
    analytics = analytics_service.get_municipio_analytics(tenant.id, start, end)
    assert {row["category"]: row["count"] for row in summary["top_categories"]} == {
        "explicit_allowed": 1,
        "legacy_allowed": 1,
    }
    assert analytics["total_claims"] == 2
    assert analytics["resolved_claims"] == 1
    assert {row["category"]: row["count"] for row in analytics["claims_by_category"]} == {
        "explicit_allowed": 1,
        "legacy_allowed": 1,
    }


def test_pyme_tenant_cannot_reclassify_municipio_tickets_in_legacy_analytics(client):
    del client
    owner = _owner(suffix="pyme-analytics", tipo_chat="pyme")
    tenant = TenantProfile(
        slug="pyme-analytics",
        nombre="PYME analytics",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner.id,
        pregunta="PYME_ANALYTICS_SECRET",
        categoria="pyme_analytics_secret",
        distrito="pyme_analytics_secret",
        estado="resuelto",
        fecha=datetime.now(timezone.utc),
    )
    db.session.add(ticket)
    db.session.commit()

    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    summary = analytics_service.get_summary(
        tenant.id,
        start,
        end,
        context="municipio",
    )
    analytics = analytics_service.get_municipio_analytics(tenant.id, start, end)

    assert summary["top_categories"] == []
    assert analytics == {
        "total_claims": 0,
        "resolved_claims": 0,
        "resolution_rate": 0,
        "suggestions_count": 0,
        "claims_by_category": [],
        "claims_by_zone": [],
        "claims_by_hour": [],
    }


def test_municipal_report_cache_rejects_pre_scope_payloads_and_preserves_pyme_cache(
    client,
):
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=7)
    period_end = now
    municipal_owner, municipal_tenant = _municipio_tenant(suffix="cache-scope")
    pyme_owner = _owner(suffix="cache-pyme", tipo_chat="pyme")
    pyme_tenant = TenantProfile(
        slug="cache-pyme",
        nombre="Cache PYME",
        tipo="pyme",
        pyme_id=pyme_owner.id,
    )
    db.session.add(pyme_tenant)
    db.session.flush()

    base_payload = {
        "source": "ad_hoc",
        "period_start": analytics_service._canonical_report_period(period_start),
        "period_end": analytics_service._canonical_report_period(period_end),
        "report": {"sentinel": "pre_scope_leak"},
    }
    old_ad_hoc = AnalyticsEvent(
        tenant_id=municipal_tenant.id,
        event_type=f"ai_report_{MUNICIPIO_CONSULTANT_REPORT_TYPE}",
        channel="system",
        timestamp=now,
        payload={
            **base_payload,
            "contract_version": "analytics.report_cache.v1",
            "report_type": MUNICIPIO_CONSULTANT_REPORT_TYPE,
        },
    )
    old_weekly = AnalyticsEvent(
        tenant_id=municipal_tenant.id,
        event_type=f"weekly_ai_report_{MUNICIPIO_CONSULTANT_REPORT_TYPE}",
        channel="system",
        timestamp=now,
        payload={
            **base_payload,
            "contract_version": "weekly.analytics_report_cache.v1",
            "source": "scheduled_weekly",
            "report_type": MUNICIPIO_CONSULTANT_REPORT_TYPE,
        },
    )
    pyme_legacy = AnalyticsEvent(
        tenant_id=pyme_tenant.id,
        event_type="ai_report_consultant_pyme",
        channel="system",
        timestamp=now,
        payload={"sentinel": "pyme_legacy_compatible"},
    )
    db.session.add_all([old_ad_hoc, old_weekly, pyme_legacy])
    db.session.commit()

    assert (
        analytics_service.get_cached_report(
            municipal_tenant.id,
            MUNICIPIO_CONSULTANT_REPORT_TYPE,
            max_age_hours=24,
        )
        is None
    )
    assert (
        analytics_service.get_cached_weekly_report(
            municipal_tenant.id,
            MUNICIPIO_CONSULTANT_REPORT_TYPE,
            max_age_hours=24,
        )
        is None
    )
    pre_scope_route = client.get(
        "/api/analytics/report/latest",
        query_string={"segment": "municipio"},
        headers=_headers(client.application, municipal_owner),
    )
    assert pre_scope_route.status_code == 200
    assert "pre_scope_leak" not in str(pre_scope_route.get_json())
    assert pre_scope_route.get_json()["available"] is False
    assert pre_scope_route.get_json()["reason_code"] == "report_not_generated"
    assert analytics_service.get_cached_report(
        pyme_tenant.id,
        "consultant_pyme",
        max_age_hours=24,
    )["sentinel"] == "pyme_legacy_compatible"

    analytics_service.cache_report(
        municipal_tenant.id,
        MUNICIPIO_CONSULTANT_REPORT_TYPE,
        {"sentinel": "scoped_ad_hoc"},
        source="ad_hoc",
        period_start=period_start,
        period_end=period_end,
    )
    new_weekly = AnalyticsEvent(
        tenant_id=municipal_tenant.id,
        event_type=f"weekly_ai_report_{MUNICIPIO_CONSULTANT_REPORT_TYPE}",
        channel="system",
        timestamp=now + timedelta(seconds=1),
        payload={
            **base_payload,
            "contract_version": "weekly.analytics_report_cache.v1",
            "source": "scheduled_weekly",
            "report_type": MUNICIPIO_CONSULTANT_REPORT_TYPE,
            "municipio_ticket_scope_contract": (
                MUNICIPIO_TICKET_SCOPE_CACHE_CONTRACT
            ),
            "report": {"sentinel": "scoped_weekly"},
        },
    )
    db.session.add(new_weekly)
    db.session.commit()

    assert analytics_service.get_cached_report(
        municipal_tenant.id,
        MUNICIPIO_CONSULTANT_REPORT_TYPE,
        max_age_hours=24,
    )["sentinel"] == "scoped_ad_hoc"
    assert analytics_service.get_cached_weekly_report(
        municipal_tenant.id,
        MUNICIPIO_CONSULTANT_REPORT_TYPE,
        max_age_hours=24,
    )["sentinel"] == "scoped_weekly"
    scoped_route = client.get(
        "/api/analytics/report/latest",
        query_string={"segment": "municipio"},
        headers=_headers(client.application, municipal_owner),
    )
    assert scoped_route.status_code == 200
    assert scoped_route.get_json()["sentinel"] == "scoped_weekly"
    assert scoped_route.get_json()["_cached"] is True
