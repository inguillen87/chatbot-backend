from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import jwt
import pytest
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app import create_app
from config import TestConfig
from extensions import db
from models import TenantBlueprintApplication, TenantProfile, User
from services.tenant_blueprints import load_blueprint
from utils.auth_helpers import auth_session_version


@pytest.fixture()
def blueprint_context():
    app = create_app(TestConfig)
    context = app.app_context()
    context.push()
    db.create_all()

    admin_a = User(
        name="Tenant Admin A",
        email="tenant-admin-a@example.test",
        rol="admin",
        tenant_slug="government-a",
    )
    admin_a.set_password("not-used")
    admin_b = User(
        name="Tenant Admin B",
        email="tenant-admin-b@example.test",
        rol="admin",
        tenant_slug="government-b",
    )
    admin_b.set_password("not-used")
    employee = User(
        name="Operator A",
        email="operator-a@example.test",
        rol="empleado",
        tenant_slug="government-a",
        es_empleado=True,
    )
    employee.set_password("not-used")
    superadmin = User(
        name="Platform Admin",
        email="guillen.marce@gmail.com",
        rol="super_admin",
    )
    superadmin.set_password("not-used")
    db.session.add_all([admin_a, admin_b, employee, superadmin])
    db.session.flush()

    tenant_a = TenantProfile(
        slug="government-a",
        nombre="Government A",
        tipo="municipio",
        municipio_id=admin_a.id,
        is_active=True,
        configuracion={
            "government_core": {
                "operating_model": {"audit_mode": "custom"},
            },
            "capabilities": {"protected": True},
            "features": {"protected": True},
            "private_config": {"token": "must-not-leak"},
        },
        capabilities_json={"protected": True},
    )
    tenant_b = TenantProfile(
        slug="government-b",
        nombre="Government B",
        tipo="gobierno",
        municipio_id=admin_b.id,
        is_active=True,
        configuracion={},
    )
    db.session.add_all([tenant_a, tenant_b])
    db.session.flush()
    admin_a.tenant_id = tenant_a.id
    employee.tenant_id = tenant_a.id
    admin_b.tenant_id = tenant_b.id
    db.session.commit()

    def token_for(user: User, *, assurance_state: str = "valid") -> str:
        now = datetime.now(timezone.utc)
        now_epoch = int(now.timestamp())
        payload = {
            "user_id": user.id,
            "tenant_slug": user.tenant_slug,
            "exp": now + timedelta(hours=1),
        }
        if user.rol == "super_admin":
            payload.update(
                {
                    "rol": user.rol,
                    "auth_provider": "clerk",
                    "session_kind": "clerk",
                    "sid": "sess_blueprint_tests",
                    "clerk_sid": "sess_blueprint_tests",
                    "jti": "jti_blueprint_tests",
                    "sv": auth_session_version(user),
                    "iat": now,
                }
            )
            if assurance_state == "valid":
                payload["auth_assurance"] = {
                    "version": "auth.assurance.v1",
                    "source": "clerk_v2_fva",
                    "status": "verified",
                    "first_factor_verified_at": now_epoch,
                    "second_factor_verified_at": now_epoch,
                }
            elif assurance_state == "malformed":
                payload["auth_assurance"] = {
                    "version": "auth.assurance.v1",
                    "source": "clerk_v2_fva",
                    "status": "verified",
                    "first_factor_verified_at": "not-a-timestamp",
                    "second_factor_verified_at": now_epoch,
                }
            elif assurance_state == "second_factor_unverified":
                payload["auth_assurance"] = {
                    "version": "auth.assurance.v1",
                    "source": "clerk_v2_fva",
                    "status": "missing",
                    "first_factor_verified_at": None,
                    "second_factor_verified_at": None,
                }
            elif assurance_state == "stale":
                payload["auth_assurance"] = {
                    "version": "auth.assurance.v1",
                    "source": "clerk_v2_fva",
                    "status": "verified",
                    "first_factor_verified_at": now_epoch - 601,
                    "second_factor_verified_at": now_epoch - 601,
                }
            elif assurance_state != "missing":
                raise ValueError(f"Unknown assurance state: {assurance_state}")
        return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")

    yield {
        "app": app,
        "client": app.test_client(),
        "admin_a": admin_a,
        "admin_b": admin_b,
        "employee": employee,
        "superadmin": superadmin,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "token_for": token_for,
    }

    db.session.remove()
    db.drop_all()
    context.pop()


def _headers(
    ctx,
    user,
    *,
    idempotency_key: str | None = None,
    assurance_state: str = "valid",
):
    headers = {
        "Authorization": (
            f"Bearer {ctx['token_for'](user, assurance_state=assurance_state)}"
        )
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _manifest_digest(ctx) -> str:
    response = ctx["client"].get(
        "/api/v2/tenant-blueprints",
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "tenant.blueprint.manifest.v1"
    return payload["blueprints"][0]["manifest_digest"]


def test_manifest_is_generic_safe_and_deterministic():
    manifest, first_digest = load_blueprint("government-core")
    _, second_digest = load_blueprint("government-core")
    serialized = json.dumps(manifest, sort_keys=True).casefold()

    assert first_digest == second_digest
    assert len(first_digest) == 64
    for institutional_name in (
        "junin",
        "mendoza",
        "tierra del fuego",
        "ushuaia",
        "tolhuin",
    ):
        assert institutional_name not in serialized
    for forbidden_key in (
        '"capabilities"',
        '"features"',
        '"feature_flags"',
        '"credentials"',
        '"api_key"',
        '"sender_id"',
        '"phone_number"',
    ):
        assert forbidden_key not in serialized
    assert "http://" not in serialized
    assert "https://" not in serialized


def test_preview_is_tenant_scoped_and_has_no_writes(blueprint_context):
    ctx = blueprint_context
    before = json.loads(json.dumps(ctx["tenant_a"].configuracion))
    detail = ctx["client"].get(
        "/api/v2/tenants/government-a/blueprints/government-core",
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert detail.status_code == 200
    assert detail.get_json()["contract_version"] == "tenant.blueprint.detail.v1"
    assert detail.get_json()["application_receipt"] is None
    assert detail.get_json()["runtime_activation_performed"] is False

    response = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/preview",
        headers=_headers(ctx, ctx["admin_a"]),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "tenant.blueprint.preview.v1"
    assert payload["tenant"]["slug"] == "government-a"
    assert payload["write_performed"] is False
    assert payload["runtime_activation_performed"] is False
    assert payload["external_calls_performed"] is False
    assert payload["changes"]["apply_count"] > 0
    assert "must-not-leak" not in response.get_data(as_text=True)
    assert TenantBlueprintApplication.query.count() == 0
    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].configuracion == before

    denied = ctx["client"].post(
        "/api/v2/tenants/government-b/blueprints/government-core/preview",
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert denied.status_code == 403
    assert denied.get_json()["reason_code"] == "tenant_control_plane_forbidden"

    detail_denied = ctx["client"].get(
        "/api/v2/tenants/government-b/blueprints/government-core",
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert detail_denied.status_code == 403

    employee_denied = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/preview",
        headers=_headers(ctx, ctx["employee"]),
    )
    assert employee_denied.status_code == 403

    superadmin_preview_without_assurance = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/preview",
        headers=_headers(
            ctx,
            ctx["superadmin"],
            assurance_state="missing",
        ),
    )
    assert superadmin_preview_without_assurance.status_code == 200
    assert TenantBlueprintApplication.query.count() == 0


@pytest.mark.parametrize(
    "assurance_state",
    ["missing", "malformed", "stale", "second_factor_unverified"],
)
def test_apply_requires_recent_strict_mfa_before_any_write(
    blueprint_context,
    assurance_state,
    monkeypatch,
):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)
    before = json.loads(json.dumps(ctx["tenant_a"].configuracion))

    def unexpected_apply(*_args, **_kwargs):
        raise AssertionError("apply_blueprint must not run before strict MFA")

    monkeypatch.setattr(
        "routes.v2.tenant_blueprints.apply_blueprint",
        unexpected_apply,
    )

    response = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(
            ctx,
            ctx["superadmin"],
            idempotency_key=f"assurance-{assurance_state}",
            assurance_state=assurance_state,
        ),
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["contract_version"] == "auth.assurance.error.v1"
    assert payload["reason_code"] == "step_up_required"
    assert payload["retryable"] is False
    assert payload["clerk_error"] == {
        "type": "forbidden",
        "reason": "reverification-error",
        "metadata": {"reverification": "strict_mfa"},
    }
    assert TenantBlueprintApplication.query.count() == 0
    assert payload["request_id"] == response.headers["X-Request-Id"]
    assert response.headers["Cache-Control"] == "no-store"
    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].configuracion == before

    if assurance_state == "missing":
        malformed_request = ctx["client"].post(
            "/api/v2/tenants/government-a/blueprints/government-core/apply",
            data="{",
            content_type="application/json",
            headers=_headers(
                ctx,
                ctx["superadmin"],
                assurance_state="missing",
            ),
        )
        assert malformed_request.status_code == 403
        assert malformed_request.get_json()["reason_code"] == "step_up_required"


def test_apply_with_recent_mfa_keeps_existing_invalid_json_contract(
    blueprint_context,
):
    ctx = blueprint_context
    response = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        data="{",
        content_type="application/json",
        headers=_headers(
            ctx,
            ctx["superadmin"],
            idempotency_key="assurance-valid-invalid-json",
        ),
    )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_json"
    assert TenantBlueprintApplication.query.count() == 0


def test_apply_does_not_fall_back_from_unknown_path_slug_to_header_tenant(
    blueprint_context,
):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)
    before = json.loads(json.dumps(ctx["tenant_a"].configuracion))
    headers = _headers(
        ctx,
        ctx["superadmin"],
        idempotency_key="exact-slug-required",
    )
    headers["X-Tenant-Slug"] = "government-a"

    response = ctx["client"].post(
        "/api/v2/tenants/government-typo/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "tenant_not_found"
    assert TenantBlueprintApplication.query.count() == 0
    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].configuracion == before


def test_apply_cookie_authentication_has_no_implicit_entity_token_write(
    blueprint_context,
):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)
    token = ctx["token_for"](ctx["superadmin"])
    ctx["client"].set_cookie("auth_token", token)
    with ctx["client"].session_transaction() as flask_session:
        flask_session["_user_id"] = str(ctx["superadmin"].id)
        flask_session["_fresh"] = True

    response = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers={"Idempotency-Key": "cookie-auth-read-only"},
    )

    assert response.status_code == 201
    db.session.refresh(ctx["superadmin"])
    assert ctx["superadmin"].entity_token is None


def test_apply_is_superadmin_only_idempotent_and_does_not_activate_runtime(
    blueprint_context, monkeypatch
):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)

    denied = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["admin_a"], idempotency_key="tenant-apply-001"),
    )
    assert denied.status_code == 403
    assert TenantBlueprintApplication.query.count() == 0

    import services.r2_service as r2_service
    import services.tenant_whatsapp_onboarding as whatsapp_onboarding
    import services.twilio_tech_provider as twilio_provider

    def unexpected_external_call(*_args, **_kwargs):
        raise AssertionError("blueprint apply must not call external providers")

    monkeypatch.setattr(
        whatsapp_onboarding,
        "bootstrap_tenant_whatsapp_onboarding",
        unexpected_external_call,
    )
    monkeypatch.setattr(
        twilio_provider, "provision_twilio_subaccount", unexpected_external_call
    )
    monkeypatch.setattr(r2_service.R2Service, "upload_file", unexpected_external_call)

    first = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-001"),
    )
    assert first.status_code == 201
    assert first.headers["X-Idempotency-Status"] == "created"
    payload = first.get_json()
    assert payload["contract_version"] == "tenant.blueprint.apply.v1"
    assert payload["replayed"] is False
    assert payload["write_performed"] is True
    assert payload["runtime_activation_performed"] is False
    assert payload["external_calls_performed"] is False
    assert "must-not-leak" not in first.get_data(as_text=True)
    assert "tenant-apply-001" not in first.get_data(as_text=True)

    db.session.refresh(ctx["tenant_a"])
    config = ctx["tenant_a"].configuracion
    assert config["government_core"]["operating_model"]["audit_mode"] == "custom"
    assert config["government_core"]["channels"]["whatsapp"]["enabled"] is False
    assert config["government_core"]["channels"]["web_widget"]["enabled"] is False
    assert config["capabilities"] == {"protected": True}
    assert config["features"] == {"protected": True}
    assert ctx["tenant_a"].capabilities_json == {"protected": True}
    assert TenantBlueprintApplication.query.count() == 1

    replay = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-001"),
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["write_performed"] is False
    assert TenantBlueprintApplication.query.count() == 1

    stale_replay = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(
            ctx,
            ctx["superadmin"],
            idempotency_key="tenant-apply-001",
            assurance_state="stale",
        ),
    )
    assert stale_replay.status_code == 403
    assert stale_replay.get_json()["reason_code"] == "step_up_required"
    assert TenantBlueprintApplication.query.count() == 1

    other_tenant = ctx["client"].post(
        "/api/v2/tenants/government-b/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-001"),
    )
    assert other_tenant.status_code == 201
    assert other_tenant.get_json()["receipt"]["tenant_id"] == ctx["tenant_b"].id
    db.session.refresh(ctx["tenant_b"])
    assert set(ctx["tenant_b"].configuracion) == {"government_core"}
    assert ctx["tenant_b"].capabilities_json is None
    assert TenantBlueprintApplication.query.count() == 2


def test_apply_rejects_stale_digest_missing_idempotency_and_inactive_tenant(
    blueprint_context,
):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)
    url = "/api/v2/tenants/government-a/blueprints/government-core/apply"

    missing_key = ctx["client"].post(
        url,
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"]),
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json()["reason_code"] == "idempotency_key_required"

    stale = ctx["client"].post(
        url,
        json={"manifest_digest": "0" * 64},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-002"),
    )
    assert stale.status_code == 409
    assert stale.get_json()["reason_code"] == "manifest_digest_mismatch"
    assert TenantBlueprintApplication.query.count() == 0

    ctx["tenant_a"].is_active = False
    db.session.commit()
    inactive = ctx["client"].post(
        url,
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-003"),
    )
    assert inactive.status_code == 409
    assert inactive.get_json()["reason_code"] == "tenant_inactive"
    assert TenantBlueprintApplication.query.count() == 0


def test_apply_rolls_back_configuration_and_receipt_together(
    blueprint_context, monkeypatch
):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)
    before = json.loads(json.dumps(ctx["tenant_a"].configuracion))

    def fail_commit():
        raise SQLAlchemyError("simulated commit failure")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    response = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-atomic"),
    )
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "blueprint_apply_unavailable"

    db.session.expire_all()
    tenant = db.session.get(TenantProfile, ctx["tenant_a"].id)
    assert tenant.configuracion == before
    assert TenantBlueprintApplication.query.count() == 0


def test_apply_reloads_locked_tenant_before_projecting_defaults(blueprint_context):
    ctx = blueprint_context
    digest = _manifest_digest(ctx)

    # Simulate a configuration write that happened after the route resolved its
    # ORM object. synchronize_session=False intentionally leaves that object
    # stale; apply must reload the locked row before composing the JSON update.
    db.session.execute(
        update(TenantProfile)
        .where(TenantProfile.id == ctx["tenant_a"].id)
        .values(configuracion={"late_control_plane_write": {"preserve": True}}),
        execution_options={"synchronize_session": False},
    )

    response = ctx["client"].post(
        "/api/v2/tenants/government-a/blueprints/government-core/apply",
        json={"manifest_digest": digest},
        headers=_headers(ctx, ctx["superadmin"], idempotency_key="tenant-apply-refresh"),
    )
    assert response.status_code == 201
    db.session.expire_all()
    refreshed = db.session.get(TenantProfile, ctx["tenant_a"].id)
    assert refreshed.configuracion["late_control_plane_write"] == {"preserve": True}
    assert "government_core" in refreshed.configuracion
