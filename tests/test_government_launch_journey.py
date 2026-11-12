from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import (
    ArchivoAdjunto,
    CategoriaTicket,
    MunicipioTicket,
    ProviderSender,
    PymeTicket,
    TenantBlueprintApplication,
    TenantBlueprintLaunchReceipt,
    User,
)
from services.channel_activation import build_channel_activation_payload
from tests.test_tenant_blueprint_provisioning import (
    _headers,
    _manifest_digest,
    blueprint_context,
)


def _launch_url(tenant_slug: str, action: str) -> str:
    return (
        f"/api/v2/tenants/{tenant_slug}/blueprints/government-core/"
        f"launch/mesa-unica/{action}"
    )


def _apply_base_blueprint(ctx, tenant_slug: str = "government-a") -> None:
    response = ctx["client"].post(
        f"/api/v2/tenants/{tenant_slug}/blueprints/government-core/apply",
        json={"manifest_digest": _manifest_digest(ctx)},
        headers=_headers(
            ctx,
            ctx["superadmin"],
            idempotency_key=f"base-blueprint-{tenant_slug}",
        ),
    )
    assert response.status_code in {200, 201}, response.get_json()


def _preview(ctx, tenant_slug: str = "government-a", user_key: str = "admin_a"):
    return ctx["client"].post(
        _launch_url(tenant_slug, "preview"),
        headers=_headers(ctx, ctx[user_key]),
    )


def _apply(
    ctx,
    digest: str,
    *,
    tenant_slug: str = "government-a",
    idempotency_key: str = "mesa-unica-launch-001",
    assurance_state: str = "valid",
    user_key: str = "superadmin",
    extra_headers: dict[str, str] | None = None,
):
    headers = _headers(
        ctx,
        ctx[user_key],
        idempotency_key=idempotency_key,
        assurance_state=assurance_state,
    )
    headers.update(extra_headers or {})
    return ctx["client"].post(
        _launch_url(tenant_slug, "apply"),
        json={"launch_digest": digest},
        headers=headers,
    )


def test_preview_requires_applied_blueprint_is_read_only_and_exact_tenant_scoped(
    blueprint_context,
):
    ctx = blueprint_context

    missing_base = _preview(ctx)
    assert missing_base.status_code == 409
    assert missing_base.get_json()["reason_code"] == "blueprint_not_applied"
    assert CategoriaTicket.query.count() == 0
    assert TenantBlueprintLaunchReceipt.query.count() == 0

    cross_tenant = _preview(ctx, tenant_slug="government-b", user_key="admin_a")
    assert cross_tenant.status_code == 403
    assert cross_tenant.get_json()["reason_code"] == "tenant_control_plane_forbidden"

    typo = _preview(ctx, tenant_slug="government-typo", user_key="admin_a")
    assert typo.status_code == 404
    assert typo.get_json()["reason_code"] == "tenant_not_found"

    _apply_base_blueprint(ctx)
    config_after_base = json.loads(json.dumps(ctx["tenant_a"].configuracion))
    response = _preview(ctx)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "tenant.blueprint.launch.preview.v1"
    assert payload["tenant"]["slug"] == "government-a"
    assert payload["blueprint"]["application_receipt_id"]
    assert payload["launch"]["id"] == "mesa-unica"
    assert payload["launch"]["runtime_scope"] == ["ticket_categories"]
    assert payload["changes"]["summary"] == {
        "desired": 9,
        "existing": 0,
        "create": 9,
        "preserve": 0,
        "conflict": 0,
    }
    assert len(payload["launch_digest"]) == 64
    assert payload["write_performed"] is False
    assert payload["runtime_activation_performed"] is False
    assert payload["provider_activation_performed"] is False
    assert payload["external_calls_performed"] is False
    assert payload["demo_data_created"] is False
    assert CategoriaTicket.query.count() == 0
    assert TenantBlueprintLaunchReceipt.query.count() == 0
    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].configuracion == config_after_base


@pytest.mark.parametrize(
    "assurance_state",
    ["missing", "malformed", "stale", "second_factor_unverified"],
)
def test_apply_is_superadmin_only_and_requires_recent_strict_mfa_before_writes(
    blueprint_context,
    assurance_state,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    digest = _preview(ctx).get_json()["launch_digest"]

    denied_admin = _apply(
        ctx,
        digest,
        idempotency_key="tenant-admin-denied",
        user_key="admin_a",
    )
    assert denied_admin.status_code == 403
    assert denied_admin.get_json()["reason_code"] == "superadmin_required"

    denied_assurance = _apply(
        ctx,
        digest,
        idempotency_key=f"strict-mfa-{assurance_state}",
        assurance_state=assurance_state,
    )
    assert denied_assurance.status_code == 403
    assurance_payload = denied_assurance.get_json()
    assert assurance_payload["contract_version"] == "auth.assurance.error.v1"
    assert assurance_payload["reason_code"] == "step_up_required"
    assert assurance_payload["retryable"] is False
    assert assurance_payload["clerk_error"] == {
        "type": "forbidden",
        "reason": "reverification-error",
        "metadata": {"reverification": "strict_mfa"},
    }
    assert CategoriaTicket.query.count() == 0
    assert TenantBlueprintLaunchReceipt.query.count() == 0


def test_apply_requires_idempotency_key_and_exact_payload_before_writes(
    blueprint_context,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    digest = _preview(ctx).get_json()["launch_digest"]

    missing_key = ctx["client"].post(
        _launch_url("government-a", "apply"),
        json={"launch_digest": digest},
        headers=_headers(ctx, ctx["superadmin"]),
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json()["reason_code"] == "idempotency_key_required"

    invalid_payload = ctx["client"].post(
        _launch_url("government-a", "apply"),
        json={"launch_digest": digest, "activate_provider": True},
        headers=_headers(
            ctx,
            ctx["superadmin"],
            idempotency_key="mesa-unica-invalid-payload",
        ),
    )
    assert invalid_payload.status_code == 400
    assert invalid_payload.get_json()["reason_code"] == "invalid_launch_payload"
    assert CategoriaTicket.query.count() == 0
    assert TenantBlueprintLaunchReceipt.query.count() == 0


def test_apply_materializes_only_tenant_categories_and_never_calls_external_providers(
    blueprint_context,
    monkeypatch,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    preview = _preview(ctx).get_json()
    tenant_config = json.loads(json.dumps(ctx["tenant_a"].configuracion))
    counts_before = {
        "users": User.query.count(),
        "municipio_tickets": MunicipioTicket.query.count(),
        "pyme_tickets": PymeTicket.query.count(),
        "attachments": ArchivoAdjunto.query.count(),
        "providers": ProviderSender.query.count(),
        "blueprints": TenantBlueprintApplication.query.count(),
    }

    import services.r2_service as r2_service
    import services.tenant_whatsapp_onboarding as whatsapp_onboarding
    import services.twilio_tech_provider as twilio_provider

    def unexpected_external_call(*_args, **_kwargs):
        raise AssertionError("Mesa Unica launch must not call external providers")

    monkeypatch.setattr(
        whatsapp_onboarding,
        "bootstrap_tenant_whatsapp_onboarding",
        unexpected_external_call,
    )
    monkeypatch.setattr(
        twilio_provider,
        "provision_twilio_subaccount",
        unexpected_external_call,
    )
    monkeypatch.setattr(r2_service.R2Service, "upload_file", unexpected_external_call)

    response = _apply(ctx, preview["launch_digest"])
    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload["contract_version"] == "tenant.blueprint.launch.apply.v1"
    assert payload["replayed"] is False
    assert payload["write_performed"] is True
    assert payload["runtime_activation_performed"] is False
    assert payload["runtime_activation_scope"] == ["ticket_categories"]
    assert payload["operational_defaults_materialized"] is True
    assert payload["provider_activation_performed"] is False
    assert payload["external_calls_performed"] is False
    assert payload["demo_data_created"] is False
    assert payload["changes"]["summary"] == {
        "desired": 9,
        "created": 9,
        "preserved": 0,
        "conflict": 0,
    }
    assert response.headers["X-Idempotency-Status"] == "created"
    assert "mesa-unica-launch-001" not in response.get_data(as_text=True)

    categories_a = (
        CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_a"].id)
        .order_by(CategoriaTicket.id.asc())
        .all()
    )
    assert [item.nombre for item in categories_a] == [
        "Alumbrado público",
        "Baches y calzada",
        "Arbolado",
        "Limpieza y residuos",
        "Agua y saneamiento",
        "Tránsito y señalización",
        "Espacio público",
        "Trámites y consultas",
        "Accesibilidad y apoyos",
    ]
    assert all(item.tipo == "ticket" for item in categories_a)
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_b"].id).count() == 0
    assert TenantBlueprintLaunchReceipt.query.filter_by(
        tenant_id=ctx["tenant_a"].id
    ).count() == 1
    assert {
        "users": User.query.count(),
        "municipio_tickets": MunicipioTicket.query.count(),
        "pyme_tickets": PymeTicket.query.count(),
        "attachments": ArchivoAdjunto.query.count(),
        "providers": ProviderSender.query.count(),
        "blueprints": TenantBlueprintApplication.query.count(),
    } == counts_before
    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].configuracion == tenant_config

    activation = build_channel_activation_payload(ctx["tenant_a"])
    by_id = {item["id"]: item for item in activation["channels"]}
    assert activation["counts"]["ticket_categories"] == 9
    assert activation["counts"]["routed_team_members"] == 0
    assert by_id["crm"]["status"] == "pending"
    assert by_id["crm"]["reason_code"] == "mesa_unica_routing_required"
    assert by_id["team_routing"]["status"] == "pending"


def test_unicode_spacing_case_comparison_preserves_existing_name_and_routing_completes_readiness(
    blueprint_context,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    preserved = CategoriaTicket(
        tenant_id=ctx["tenant_a"].id,
        nombre="  ALUMBRADO   PÚBLICO ",
        tipo="ticket",
    )
    foreign = CategoriaTicket(
        tenant_id=ctx["tenant_b"].id,
        nombre="Alumbrado publico",
        tipo="ticket",
    )
    db.session.add_all([preserved, foreign])
    db.session.commit()

    preview = _preview(ctx)
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["changes"]["summary"] == {
        "desired": 9,
        "existing": 1,
        "create": 8,
        "preserve": 1,
        "conflict": 0,
    }
    assert preview_payload["changes"]["already_present"][0]["category"]["name"] == (
        "  ALUMBRADO   PÚBLICO "
    )

    applied = _apply(ctx, preview_payload["launch_digest"])
    assert applied.status_code == 201
    db.session.refresh(preserved)
    assert preserved.nombre == "  ALUMBRADO   PÚBLICO "
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_a"].id).count() == 9
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_b"].id).count() == 1

    headers = _headers(ctx, ctx["admin_a"])
    headers["X-Tenant-Slug"] = ctx["tenant_a"].slug
    listed = ctx["client"].get(
        f"/api/admin/tenants/{ctx['tenant_a'].slug}/ticket-categories",
        headers=headers,
    )
    assert listed.status_code == 200
    assert len(listed.get_json()) == 9
    assert all(item["id"] != foreign.id for item in listed.get_json())

    assigned = ctx["client"].post(
        f"/api/admin/employees/{ctx['employee'].id}/categories",
        json={"category_ids": [preserved.id]},
        headers=headers,
    )
    assert assigned.status_code == 200
    assert assigned.get_json()["count"] == 1

    activation = build_channel_activation_payload(ctx["tenant_a"])
    by_id = {item["id"]: item for item in activation["channels"]}
    assert activation["counts"]["routed_team_members"] == 1
    assert by_id["crm"]["status"] == "ready"
    assert by_id["team_routing"]["status"] == "ready"


def test_apply_is_idempotent_and_rejects_same_key_with_a_different_digest(
    blueprint_context,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    digest = _preview(ctx).get_json()["launch_digest"]

    first = _apply(ctx, digest, idempotency_key="mesa-unica-stable-key")
    assert first.status_code == 201
    receipt_id = first.get_json()["receipt"]["id"]

    replay = _apply(ctx, digest, idempotency_key="mesa-unica-stable-key")
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["write_performed"] is False
    assert replay.get_json()["receipt"]["id"] == receipt_id

    conflict = _apply(ctx, "f" * 64, idempotency_key="mesa-unica-stable-key")
    assert conflict.status_code == 409
    assert conflict.get_json()["reason_code"] == "launch_idempotency_key_conflict"
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_a"].id).count() == 9
    assert TenantBlueprintLaunchReceipt.query.count() == 1


def test_apply_rejects_stale_projection_and_ambiguous_existing_categories(
    blueprint_context,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    stale_digest = _preview(ctx).get_json()["launch_digest"]
    db.session.add(
        CategoriaTicket(
            tenant_id=ctx["tenant_a"].id,
            nombre="Categoria posterior al preview",
            tipo="ticket",
        )
    )
    db.session.commit()

    stale = _apply(ctx, stale_digest, idempotency_key="mesa-unica-stale-preview")
    assert stale.status_code == 409
    assert stale.get_json()["reason_code"] == "launch_digest_mismatch"
    assert TenantBlueprintLaunchReceipt.query.count() == 0
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_a"].id).count() == 1

    _apply_base_blueprint(ctx, "government-b")
    db.session.add_all(
        [
            CategoriaTicket(
                tenant_id=ctx["tenant_b"].id,
                nombre="Alumbrado público",
                tipo="ticket",
            ),
            CategoriaTicket(
                tenant_id=ctx["tenant_b"].id,
                nombre=" ALUMBRADO   PÚBLICO ",
                tipo="ticket",
            ),
        ]
    )
    db.session.commit()
    conflict_preview = _preview(ctx, "government-b", user_key="admin_b")
    assert conflict_preview.status_code == 200
    conflict_payload = conflict_preview.get_json()
    assert conflict_payload["changes"]["summary"]["conflict"] == 1
    assert conflict_payload["changes"]["conflicts"][0]["reason_code"] == (
        "ambiguous_existing_categories"
    )

    conflict = _apply(
        ctx,
        conflict_payload["launch_digest"],
        tenant_slug="government-b",
        idempotency_key="mesa-unica-category-conflict",
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["reason_code"] == "launch_category_conflict"
    assert TenantBlueprintLaunchReceipt.query.filter_by(
        tenant_id=ctx["tenant_b"].id
    ).count() == 0
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_b"].id).count() == 2


def test_apply_cookie_auth_does_not_create_legacy_entity_token_and_typo_never_falls_back(
    blueprint_context,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    digest = _preview(ctx).get_json()["launch_digest"]
    token = ctx["token_for"](ctx["superadmin"])
    ctx["client"].set_cookie("auth_token", token)
    with ctx["client"].session_transaction() as flask_session:
        flask_session["_user_id"] = str(ctx["superadmin"].id)
        flask_session["_fresh"] = True

    response = ctx["client"].post(
        _launch_url("government-a", "apply"),
        json={"launch_digest": digest},
        headers={"Idempotency-Key": "mesa-unica-cookie-auth"},
    )
    assert response.status_code == 201
    db.session.refresh(ctx["superadmin"])
    assert ctx["superadmin"].entity_token is None

    typo = _apply(
        ctx,
        digest,
        tenant_slug="government-typo",
        idempotency_key="mesa-unica-exact-path",
        extra_headers={"X-Tenant-Slug": "government-a"},
    )
    assert typo.status_code == 404
    assert typo.get_json()["reason_code"] == "tenant_not_found"
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_b"].id).count() == 0


def test_apply_rolls_back_categories_and_receipt_together(
    blueprint_context,
    monkeypatch,
):
    ctx = blueprint_context
    _apply_base_blueprint(ctx)
    digest = _preview(ctx).get_json()["launch_digest"]

    def fail_commit():
        raise SQLAlchemyError("simulated launch commit failure")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    response = _apply(ctx, digest, idempotency_key="mesa-unica-atomic-write")

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "blueprint_launch_unavailable"
    db.session.expire_all()
    assert CategoriaTicket.query.filter_by(tenant_id=ctx["tenant_a"].id).count() == 0
    assert TenantBlueprintLaunchReceipt.query.count() == 0


def test_launch_creates_no_heatmap_points_but_categories_accept_real_ticket_evidence(
    blueprint_context,
):
    ctx = blueprint_context
    ctx["tenant_a"].slug = "junin"
    ctx["admin_a"].tenant_slug = "junin"
    ctx["employee"].tenant_slug = "junin"
    ctx["tenant_a"].plan = "full"
    db.session.commit()
    _apply_base_blueprint(ctx, "junin")
    digest = _preview(ctx, "junin").get_json()["launch_digest"]
    assert _apply(ctx, digest, tenant_slug="junin").status_code == 201
    assert MunicipioTicket.query.filter_by(tenant_id=ctx["tenant_a"].id).count() == 0

    category = CategoriaTicket.query.filter_by(
        tenant_id=ctx["tenant_a"].id,
        nombre="Alumbrado público",
    ).one()
    ticket = MunicipioTicket(
        pregunta="Luminaria apagada reportada por canal operativo",
        asunto="Alumbrado publico",
        categoria=category.nombre,
        categoria_id=category.id,
        municipio_id=ctx["admin_a"].id,
        tenant_id=ctx["tenant_a"].id,
        estado="nuevo",
        direccion="Calle institucional 100",
        latitud=-33.136,
        longitud=-68.49,
        canal_ingreso="web",
    )
    db.session.add(ticket)
    db.session.commit()

    headers = _headers(ctx, ctx["admin_a"])
    headers["X-Tenant-Slug"] = ctx["tenant_a"].slug
    heatmap = ctx["client"].get(
        "/api/v2/analytics/operations/heatmap",
        query_string={
            "range": "all",
            "source": "tickets",
            "include_ai": "0",
            "categoria": "Alumbrado público",
        },
        headers=headers,
    )
    assert heatmap.status_code == 200, heatmap.get_json()
    payload = heatmap.get_json()
    matching_points = [
        item
        for item in payload.get("points") or []
        if item.get("id") == f"municipio_ticket:{ticket.id}"
    ]
    assert matching_points, payload
    point = matching_points[0]
    assert point["record_source"] == "municipio_ticket"
    assert point["lat"] == pytest.approx(-33.136)
    assert point["lng"] == pytest.approx(-68.49)
    assert point["category"] == "luminarias"
    assert point["raw_category"] == "Alumbrado público"
