from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import event

from database import db
from extensions import limiter
from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    TenantProfile,
    User,
)
from services.analytics_service import analytics_service
from services.analytics.models import AnalyticsModuleStatus
from routes import admin_tenant as admin_tenant_routes
from utils.auth_helpers import auth_session_version, generar_token


_TENANT_WIDE_ANALYTICS_GET_PATHS = [
    "/api/analytics/summary",
    "/api/analytics/heatmap",
    "/api/analytics/insights",
    "/api/analytics/sales",
    "/api/analytics/benchmarks",
    "/api/analytics/funnel",
    "/api/analytics/report/latest",
    "/api/v2/analytics/summary",
    "/api/v2/analytics/heatmap",
    "/api/v2/analytics/insights",
    "/api/v2/analytics/sales",
    "/api/v2/analytics/benchmarks",
    "/api/v2/analytics/report/latest",
    "/analytics/report/latest",
    "/api/v2/analytics/overview",
    "/api/v2/analytics/tickets",
    "/api/v2/analytics/funnel",
]

_SURVEY_ANALYTICS_GET_PATHS = [
    "/api/analytics/surveys/summary",
    "/api/analytics/surveys/sentiment",
    "/api/analytics/surveys/geo",
    "/api/v2/analytics/surveys/summary",
    "/api/v2/analytics/surveys/sentiment",
    "/api/v2/analytics/surveys/geo",
    "/api/v2/analytics/surveys",
]

_ANALYTICS_GET_PATHS = [
    *_TENANT_WIDE_ANALYTICS_GET_PATHS,
    *_SURVEY_ANALYTICS_GET_PATHS,
]

_ANALYTICS_POST_PATHS = [
    "/api/analytics/report/generate",
    "/api/analytics/generate-report",
    "/api/v2/analytics/report/generate",
    "/api/v2/analytics/generate-report",
    "/analytics/report/generate",
]

_ROOT_TENANT_WIDE_ANALYTICS_GET_PATHS = [
    "/analytics/summary",
    "/analytics/timeseries",
    "/analytics/breakdown",
    "/analytics/geo/heatmap",
    "/analytics/geo/points",
    "/analytics/top",
    "/analytics/operations",
    "/analytics/cohorts",
    "/analytics/whatsapp/templates",
]

_ADMIN_TENANT_WIDE_ANALYTICS_GET_PATHS = [
    "/admin/analytics/overview",
    "/admin/analytics/heatmap",
    "/admin/analytics/realtime-hub",
    "/admin/analytics/whatsapp-funnel",
    "/admin/analytics/export.csv",
    "/admin/analytics/export.pdf",
    "/admin/analytics/dashboard",
    "/admin/analytics/hub",
    "/api/admin/analytics/overview",
    "/api/admin/analytics/heatmap",
    "/api/admin/analytics/realtime-hub",
    "/api/admin/analytics/whatsapp-funnel",
    "/api/admin/analytics/export.csv",
    "/api/admin/analytics/export.pdf",
    "/api/admin/analytics/dashboard",
    "/api/admin/analytics/hub",
    "/api/v2/analytics/whatsapp-funnel",
]

_ANALYTICS_SERVICE_SENTINELS = {
    "get_summary": {"sentinel": "foreign-tenant-secret"},
    "get_heatmap_data": [{"sentinel": "foreign-tenant-secret"}],
    "get_survey_summary": {"sentinel": "foreign-tenant-secret"},
    "get_cached_report": {"sentinel": "foreign-tenant-secret"},
    "get_survey_sentiment_texts": ["foreign-tenant-secret"],
    "get_survey_geo": [{"sentinel": "foreign-tenant-secret"}],
    "get_insights": [{"sentinel": "foreign-tenant-secret"}],
    "get_commerce_analytics": {"sentinel": "foreign-tenant-secret"},
    "get_benchmarks": {"sentinel": "foreign-tenant-secret"},
    "get_funnel_analytics": {"sentinel": "foreign-tenant-secret"},
    "get_municipio_analytics": {"sentinel": "foreign-tenant-secret"},
}


def _token_for(actor: User) -> str:
    extra_claims = None
    if actor.rol == "super_admin":
        extra_claims = {
            "auth_provider": "clerk",
            "session_kind": "clerk",
            "clerk_sid": f"sess-analytics-{actor.id}",
            "jti": f"jti-analytics-{actor.id}",
            "sv": auth_session_version(actor),
        }
    return generar_token(
        actor.id,
        actor.rol,
        actor.tipo_chat,
        municipio_id=None,
        pyme_id=getattr(actor, "empresa_id", None),
        extra_claims=extra_claims,
    )


def _headers(actor: User, *, tenant_slug: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_token_for(actor)}"}
    if tenant_slug:
        headers["X-Tenant-Slug"] = tenant_slug
    return headers


def _set_panel_session(client, actor: User) -> None:
    with client.session_transaction() as flask_session:
        flask_session["_user_id"] = str(actor.id)
        flask_session["_fresh"] = True


def _create_tenant(slug: str) -> tuple[TenantProfile, User]:
    owner = User(
        name=f"Owner {slug}",
        email=f"owner-{slug}@test.com",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("safe-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=slug,
        tipo="pyme",
        plan="enterprise",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    db.session.commit()
    return tenant, owner


def _create_actor(*, role: str, tenant: TenantProfile, owner: User) -> User:
    if role == "admin":
        return owner
    actor = User(
        name=f"Actor {role}",
        email=f"{role}-{tenant.slug}@test.com",
        rol=role,
        tipo_chat="pyme",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        empresa_id=owner.id if role == "empleado" else None,
        es_empleado=role == "empleado",
    )
    actor.set_password("safe-password")
    db.session.add(actor)
    db.session.commit()
    return actor


@contextmanager
def _mocked_analytics_services():
    with ExitStack() as stack:
        mocks = {
            method: stack.enter_context(
                patch.object(analytics_service, method, return_value=value)
            )
            for method, value in _ANALYTICS_SERVICE_SENTINELS.items()
        }
        stack.enter_context(
            patch(
                "routes.analytics_routes._feature_enabled_for_tenant_id",
                return_value=True,
            )
        )
        mocks["v2_feature_enabled"] = stack.enter_context(
            patch("routes.v2.analytics._feature_enabled", return_value=True)
        )
        stack.enter_context(
            patch(
                "routes.analytics_routes.generate_analytics_report",
                return_value={"sentinel": "foreign-tenant-secret"},
            )
        )
        mocks["v2_generate_provider"] = stack.enter_context(
            patch(
                "routes.v2.analytics.generate_analytics_report",
                return_value={"sentinel": "foreign-tenant-secret"},
            )
        )
        yield mocks


@pytest.mark.parametrize("role", ["admin", "empleado"])
def test_all_legacy_and_v2_aliases_block_cross_tenant_before_materialization(
    client,
    role,
):
    own_tenant, owner = _create_tenant(f"analytics-all-own-{role}")
    foreign_tenant, _foreign_owner = _create_tenant(
        f"analytics-all-foreign-{role}"
    )
    actor = _create_actor(role=role, tenant=own_tenant, owner=owner)

    with _mocked_analytics_services() as service_calls:
        for path in _ANALYTICS_GET_PATHS:
            response = client.get(
                path,
                query_string={"tenant_id": foreign_tenant.id},
                headers=_headers(actor, tenant_slug=foreign_tenant.slug),
            )
            assert response.status_code == 403, path
            expected_code = (
                "employee_analytics_scope_unsupported"
                if role == "empleado" and path in _TENANT_WIDE_ANALYTICS_GET_PATHS
                else None
            )
            payload = response.get_json()
            if expected_code:
                assert payload["code"] == expected_code, path
            else:
                assert (payload.get("code") or payload.get("reason_code")) in {
                    "forbidden",
                    "forbidden_tenant",
                }, path
            assert "foreign-tenant-secret" not in response.get_data(as_text=True), path

        for path in _ANALYTICS_POST_PATHS:
            limiter.reset()
            response = client.post(
                path,
                json={"tenant_id": foreign_tenant.id, "segment": "pyme"},
                headers=_headers(actor, tenant_slug=foreign_tenant.slug),
            )
            assert response.status_code == 403, path
            expected_code = (
                "employee_analytics_scope_unsupported"
                if role == "empleado"
                else "forbidden"
            )
            assert response.get_json()["code"] == expected_code, path
            assert "foreign-tenant-secret" not in response.get_data(as_text=True), path

    assert all(not service_call.called for service_call in service_calls.values())


def test_all_legacy_endpoints_allow_same_tenant_admin_without_retargeting(client):
    tenant, owner = _create_tenant("analytics-all-same-admin")
    actor = owner

    with _mocked_analytics_services():
        for path in _ANALYTICS_GET_PATHS:
            response = client.get(
                path,
                query_string={"tenant_id": tenant.id},
                headers=_headers(actor, tenant_slug=tenant.slug),
            )
            assert response.status_code == 200, path

        for path in _ANALYTICS_POST_PATHS:
            limiter.reset()
            response = client.post(
                path,
                json={"tenant_id": tenant.id, "segment": "pyme"},
                headers=_headers(actor, tenant_slug=tenant.slug),
            )
            assert response.status_code == 200, path


def test_employee_tenant_wide_analytics_fail_closed_before_route_side_effects(
    client,
):
    tenant, owner = _create_tenant("analytics-employee-scope-guard")
    employee = _create_actor(role="empleado", tenant=tenant, owner=owner)
    request_id = "employee-analytics-scope-p0"
    headers = _headers(employee, tenant_slug=tenant.slug)
    headers["X-Request-Id"] = request_id

    all_paths = [
        *_TENANT_WIDE_ANALYTICS_GET_PATHS,
        *_ROOT_TENANT_WIDE_ANALYTICS_GET_PATHS,
        *_ADMIN_TENANT_WIDE_ANALYTICS_GET_PATHS,
    ]
    materializer_targets = [
        "routes.analytics_routes._resolve_authorized_analytics_tenant",
        "routes.analytics_routes._feature_enabled_for_tenant_id",
        "routes.analytics_routes._integration_access_for_tenant_id",
        "routes.analytics_routes._generate_report_impl",
        "routes.analytics_routes.generate_analytics_report",
        "routes.v2.analytics._resolve_tenant_or_error",
        "routes.v2.analytics._feature_enabled",
        "routes.v2.analytics.generate_analytics_report",
        "routes.analytics.parse_filters",
        "routes.analytics.get_summary",
        "routes.analytics.get_timeseries",
        "routes.analytics.get_breakdown",
        "routes.analytics.get_geo_heatmap",
        "routes.analytics.get_geo_points",
        "routes.analytics.get_top",
        "routes.analytics.get_operations_overview",
        "routes.analytics.get_cohorts",
        "routes.analytics.get_whatsapp_templates",
        "routes.admin_analytics.parse_filters",
        "routes.admin_analytics.get_summary",
        "routes.admin_analytics.get_geo_heatmap",
        "routes.admin_analytics._dashboard_response",
        "routes.admin_analytics._build_realtime_hub_payload",
        "routes.admin_analytics._build_whatsapp_funnel_payload",
    ]

    limiter.reset()
    with ExitStack() as stack:
        side_effect_calls = [
            stack.enter_context(patch(target)) for target in materializer_targets
        ]
        side_effect_calls.extend(
            stack.enter_context(patch.object(analytics_service, method))
            for method in _ANALYTICS_SERVICE_SENTINELS
        )
        side_effect_calls.append(
            stack.enter_context(patch("routes.analytics.analytics_cache.get_or_set"))
        )

        for path in all_paths:
            response = client.get(
                path,
                query_string={"tenant_id": tenant.id},
                headers=headers,
            )
            assert response.status_code == 403, path
            payload = response.get_json()
            assert payload["contract_version"] == "shared.error.v1", path
            assert payload["status_code"] == 403, path
            assert payload["code"] == "employee_analytics_scope_unsupported", path
            assert (
                payload["reason_code"]
                == "employee_analytics_scope_unsupported"
            ), path
            assert payload["retryable"] is False, path
            assert payload["ok"] is False, path
            assert payload["message"] == payload["error"]["message"], path
            assert payload["detail"], path
            assert payload["action_hint"] == "use_scoped_operations_dashboard", path
            assert payload["replacement_endpoint"] == (
                "/api/v2/analytics/operations/dashboard"
            ), path
            assert payload["request_id"] == request_id, path
            assert response.headers["X-Request-Id"] == request_id, path
            assert response.headers["Cache-Control"] == "no-store", path

        # Every alias shares the same one-per-hour limiter. Calling all paths
        # twice proves the route-local rejection happens before that quota.
        for _attempt in range(2):
            for path in _ANALYTICS_POST_PATHS:
                response = client.post(
                    path,
                    json={"tenant_id": tenant.id, "segment": "pyme"},
                    headers=headers,
                )
                assert response.status_code == 403, path
                payload = response.get_json()
                assert (
                    payload["reason_code"]
                    == "employee_analytics_scope_unsupported"
                ), path
                assert payload["replacement_endpoint"] == (
                    "/api/v2/analytics/operations/dashboard"
                ), path

    assert all(not side_effect_call.called for side_effect_call in side_effect_calls)


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/analytics/summary", "profile"),
        ("/analytics/summary", "owner"),
        ("/api/v2/analytics/overview", "none"),
    ],
)
def test_explicit_employee_bearer_overrides_stale_admin_session_on_analytics(
    client,
    path,
    query,
):
    tenant, owner = _create_tenant(f"analytics-mixed-admin-{query}")
    employee = _create_actor(role="empleado", tenant=tenant, owner=owner)
    _set_panel_session(client, owner)
    query_string = (
        {"tenant_id": tenant.id}
        if query == "profile"
        else {"tenant_id": owner.id, "scope": "pyme"}
        if query == "owner"
        else None
    )

    response = client.get(
        path,
        query_string=query_string,
        headers=_headers(employee, tenant_slug=tenant.slug),
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == (
        "employee_analytics_scope_unsupported"
    )


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/analytics/summary", "profile"),
        ("/analytics/summary", "owner"),
        ("/api/v2/analytics/overview", "none"),
    ],
)
def test_explicit_admin_bearer_overrides_stale_employee_session_on_analytics(
    client,
    path,
    query,
):
    tenant, owner = _create_tenant(f"analytics-mixed-employee-{query}")
    employee = _create_actor(role="empleado", tenant=tenant, owner=owner)
    _set_panel_session(client, employee)
    query_string = (
        {"tenant_id": tenant.id}
        if query == "profile"
        else {"tenant_id": owner.id, "scope": "pyme"}
        if query == "owner"
        else None
    )

    with _mocked_analytics_services(), patch(
        "routes.analytics.get_summary",
        return_value={"totals": {"tickets": 0}},
    ):
        response = client.get(
            path,
            query_string=query_string,
            headers=_headers(owner, tenant_slug=tenant.slug),
        )

    assert response.status_code == 200


def test_employee_survey_analytics_remain_available_on_canonical_and_alias_paths(
    client,
):
    tenant, owner = _create_tenant("analytics-employee-surveys-out-of-slice")
    employee = _create_actor(role="empleado", tenant=tenant, owner=owner)
    headers = _headers(employee, tenant_slug=tenant.slug)

    with patch.object(
        analytics_service,
        "get_survey_summary",
        return_value={"stats": {"total_votes": 0}},
    ) as summary_call, patch.object(
        analytics_service,
        "get_cached_report",
        return_value={"ok": True, "source": "survey-cache"},
    ) as cache_call, patch.object(
        analytics_service,
        "get_survey_geo",
        return_value=[],
    ) as geo_call, patch(
        "routes.analytics_routes._feature_enabled_for_tenant_id",
        return_value=True,
    ):
        for path in _SURVEY_ANALYTICS_GET_PATHS:
            response = client.get(
                path,
                query_string={"tenant_id": tenant.id},
                headers=headers,
            )
            assert response.status_code == 200, path

    assert summary_call.call_count == 3
    assert cache_call.call_count == 2
    assert geo_call.call_count == 2


def test_employee_identity_coverage_read_remains_available(client):
    tenant, owner = _create_tenant("analytics-employee-identity-coverage")
    employee = _create_actor(role="empleado", tenant=tenant, owner=owner)
    headers = _headers(employee, tenant_slug=tenant.slug)

    for path in (
        "/analytics/identity/coverage",
        "/api/analytics/identity/coverage",
    ):
        response = client.get(
            path,
            query_string={"tenant_id": owner.id, "scope": "pyme"},
            headers=headers,
        )
        assert response.status_code == 200, path
        assert response.get_json()["contract_version"] == (
            "analytics.identity_coverage.v1"
        ), path


def test_public_analytics_health_never_exposes_snapshot_tenant_metadata(client):
    db.session.add(
        AnalyticsModuleStatus(
            jobs_pending=1,
            jobs_running=0,
            jobs_failed=0,
            extra={
                "tenant_id": "private-tenant-42",
                "scope": "municipio-private",
                "secret_marker": "must-never-leak",
            },
        )
    )
    db.session.commit()

    response = client.get("/analytics/health")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["contract_version"] == "analytics.health.v1"
    assert payload["metadata_redacted"] is True
    assert "metadata" not in payload
    assert "extra" not in payload
    encoded = response.get_data(as_text=True)
    assert "private-tenant-42" not in encoded
    assert "municipio-private" not in encoded
    assert "must-never-leak" not in encoded


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_privileged_analytics_families_keep_tenant_wide_access(client, role):
    tenant, owner = _create_tenant(f"analytics-privileged-{role}")
    if role == "admin":
        actor = owner
    else:
        actor = User(
            name="Platform analytics admin",
            email="platform-analytics-admin@test.com",
            rol="super_admin",
            tipo_chat="platform",
        )
        actor.set_password("safe-password")
        db.session.add(actor)
        db.session.commit()

    headers = _headers(actor, tenant_slug=tenant.slug)
    with patch.object(
        analytics_service,
        "get_summary",
        return_value={"kpis": {"total_interactions": 0}},
    ), patch.object(
        analytics_service,
        "get_survey_summary",
        return_value={"stats": {"total_votes": 0}},
    ), patch(
        "routes.analytics.get_summary",
        return_value={"totals": {"tickets": 0}},
    ), patch(
        "routes.admin_analytics.get_summary",
        return_value={"totals": {"tickets": 0}},
    ):
        responses = [
            client.get(
                "/api/analytics/summary",
                query_string={"tenant_id": tenant.id},
                headers=headers,
            ),
            client.get(
                "/api/v2/analytics/overview",
                query_string={"tenant_id": tenant.id},
                headers=headers,
            ),
            client.get(
                "/analytics/summary",
                query_string={"tenant_id": owner.id, "scope": "pyme"},
                headers=headers,
            ),
            client.get(
                "/admin/analytics/overview",
                query_string={"tenant_id": owner.id, "scope": "pyme"},
                headers=headers,
            ),
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]


def test_employee_operations_dashboard_remains_scope_aware_and_available(client):
    tenant, owner = _create_tenant("analytics-employee-operations-ok")
    employee = _create_actor(role="empleado", tenant=tenant, owner=owner)
    payload = {
        "contract_version": "analytics.operations.v2",
        "tenant": {"id": tenant.id, "slug": tenant.slug},
        "summary": {"open_tickets": 0},
    }

    with patch(
        "routes.v2.analytics._cached_operational_dashboard",
        return_value=payload,
    ) as operations_call, patch.object(
        analytics_service,
        "get_summary",
    ) as legacy_summary_call:
        response = client.get(
            "/api/v2/analytics/operations/dashboard",
            headers=_headers(employee, tenant_slug=tenant.slug),
        )

    assert response.status_code == 200
    assert response.get_json()["contract_version"] == "analytics.operations.v2"
    operations_call.assert_called_once()
    assert operations_call.call_args.kwargs["viewer"].id == employee.id
    legacy_summary_call.assert_not_called()


def test_cross_tenant_report_rejection_does_not_consume_generation_quota(client):
    own_tenant, owner = _create_tenant("analytics-report-quota-own")
    foreign_tenant, _foreign_owner = _create_tenant("analytics-report-quota-foreign")
    limiter.reset()

    with _mocked_analytics_services():
        rejected = client.post(
            "/api/analytics/report/generate",
            json={"tenant_id": foreign_tenant.id, "segment": "pyme"},
            headers=_headers(owner),
        )
        allowed = client.post(
            "/api/analytics/report/generate",
            json={"tenant_id": own_tenant.id, "segment": "pyme"},
            headers=_headers(owner),
        )

    assert rejected.status_code == 403
    assert rejected.get_json()["code"] == "forbidden"
    assert allowed.status_code == 200
    assert allowed.get_json()["sentinel"] == "foreign-tenant-secret"


@pytest.mark.parametrize("role", ["admin", "empleado"])
@pytest.mark.parametrize(
    ("path", "service_method"),
    [
        ("summary", "get_survey_summary"),
        ("sentiment", "get_cached_report"),
        ("geo", "get_survey_geo"),
    ],
)
def test_survey_analytics_cross_tenant_fails_before_service(
    client,
    role,
    path,
    service_method,
):
    own_tenant, owner = _create_tenant(f"analytics-own-{role}-{path}")
    foreign_tenant, _foreign_owner = _create_tenant(
        f"analytics-foreign-{role}-{path}"
    )
    actor = _create_actor(role=role, tenant=own_tenant, owner=owner)

    with patch.object(analytics_service, service_method) as service_call, patch(
        "routes.analytics_routes._feature_enabled_for_tenant_id",
        return_value=True,
    ) as feature_call:
        response = client.get(
            f"/api/analytics/surveys/{path}",
            query_string={"tenant_id": foreign_tenant.id},
            headers=_headers(actor),
        )

    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"
    service_call.assert_not_called()
    feature_call.assert_not_called()


@pytest.mark.parametrize("role", ["admin", "empleado"])
def test_survey_analytics_actor_without_tenant_fails_closed(client, role):
    target_tenant, _target_owner = _create_tenant(f"analytics-target-{role}")
    actor = User(
        name=f"Tenantless {role}",
        email=f"tenantless-{role}@test.com",
        rol=role,
        tipo_chat="pyme",
    )
    actor.set_password("safe-password")
    db.session.add(actor)
    db.session.commit()

    with patch.object(analytics_service, "get_survey_summary") as summary_call:
        response = client.get(
            "/api/analytics/surveys/summary",
            query_string={"tenant_id": target_tenant.id},
            headers=_headers(actor),
        )

    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"
    summary_call.assert_not_called()


@pytest.mark.parametrize("role", ["admin", "empleado"])
def test_survey_analytics_same_tenant_uses_canonical_actor_tenant(client, role):
    tenant, owner = _create_tenant(f"analytics-same-{role}")
    actor = _create_actor(role=role, tenant=tenant, owner=owner)

    with patch.object(
        analytics_service,
        "get_survey_summary",
        return_value={"active_survey": {"title": "Sentinel propio"}},
    ) as summary_call:
        response = client.get(
            "/api/analytics/surveys/summary",
            query_string={"tenant_id": tenant.id},
            headers=_headers(actor),
        )

    assert response.status_code == 200
    assert response.get_json()["active_survey"]["title"] == "Sentinel propio"
    summary_call.assert_called_once_with(tenant_id=tenant.id)


@pytest.mark.parametrize(
    ("path", "service_method", "service_value"),
    [
        ("summary", "get_survey_summary", {"active_survey": {"id": 91}}),
        ("geo", "get_survey_geo", [{"lat": -54.8, "lng": -68.3}]),
    ],
)
def test_authorized_superadmin_can_target_explicit_tenant(
    client,
    path,
    service_method,
    service_value,
):
    tenant, _owner = _create_tenant(f"analytics-super-{path}")
    superadmin = User(
        name="Platform superadmin",
        email="platform-superadmin@test.com",
        rol="super_admin",
        tipo_chat="platform",
    )
    superadmin.set_password("safe-password")
    db.session.add(superadmin)
    db.session.commit()

    with patch.object(
        analytics_service,
        service_method,
        return_value=service_value,
    ) as service_call, patch(
        "routes.analytics_routes._feature_enabled_for_tenant_id",
        return_value=True,
    ):
        response = client.get(
            f"/api/analytics/surveys/{path}",
            query_string={"tenant_id": tenant.id},
            headers=_headers(superadmin),
        )

    assert response.status_code == 200
    service_call.assert_called_once_with(tenant_id=tenant.id)


@contextmanager
def _captured_sql():
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(str(statement).lower().split()))

    event.listen(db.engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture)


def test_survey_analytics_scale_contract_filters_origin_and_limits_in_sql(client):
    tenant, _owner = _create_tenant("analytics-scale")
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug="survey-scale",
        titulo="Survey scale",
        estado="publicada",
        tipo="opinion",
    )
    db.session.add(survey)
    db.session.flush()
    question = EncPregunta(
        encuesta_id=survey.id,
        orden=1,
        tipo="opcion_unica",
        texto="¿Cómo evalúa el servicio?",
    )
    db.session.add(question)
    db.session.flush()
    option = EncOpcion(
        pregunta_id=question.id,
        orden=1,
        texto="Bien",
    )
    db.session.add(option)
    db.session.flush()

    for index in range(40):
        origin = (
            "real"
            if index < 25
            else ("synthetic_demo" if index < 35 else "legacy_unverified")
        )
        response = EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=tenant.id,
            response_origin=origin,
            huella_unica=f"scale-{index}",
            canal="web",
            lat=-54.8 + index / 1000,
            lng=-68.3 - index / 1000,
        )
        db.session.add(response)
        db.session.flush()
        db.session.add(
            EncRespuestaDetalle(
                respuesta_id=response.id,
                pregunta_id=question.id,
                opcion_id=option.id,
                texto_libre=f"Comentario {index}",
            )
        )
    db.session.commit()
    tenant_id = tenant.id

    with _captured_sql() as summary_sql:
        summary = analytics_service.get_survey_summary(tenant_id)
    with _captured_sql() as sentiment_sql:
        texts = analytics_service.get_survey_sentiment_texts(tenant_id, limit=7)
    with _captured_sql() as geo_sql:
        points = analytics_service.get_survey_geo(tenant_id)

    assert summary["stats"]["total_votes"] == 25
    assert summary["stats"]["results_by_option"] == [
        {"option": "Bien", "count": 25}
    ]
    assert summary["stats"]["response_provenance"][
        "synthetic_responses_excluded"
    ] == 10
    assert summary["stats"]["response_provenance"][
        "unverified_responses_excluded"
    ] == 5
    assert len(texts) == 7
    assert all(text != "Comentario 39" for text in texts)
    assert len(points) == 25

    all_sql = [*summary_sql, *sentiment_sql, *geo_sql]
    response_selects = [
        statement
        for statement in all_sql
        if statement.startswith("select") and "enc_respuesta" in statement
    ]
    assert response_selects
    assert all("response_origin" in statement for statement in response_selects)
    assert all("enc_respuesta.id in (" not in statement for statement in all_sql)
    assert any(" limit " in statement for statement in sentiment_sql)
    assert any(" limit " in statement for statement in geo_sql)
    assert len(summary_sql) <= 4
    assert len(sentiment_sql) == 1
    assert len(geo_sql) == 1


def test_admin_tenant_survey_surfaces_use_constant_aggregate_query_plans(client):
    tenant, owner = _create_tenant("admin-tenant-survey-scale")
    expected_real = 0
    expected_synthetic = 0
    expected_unverified = 0
    for survey_index in range(12):
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug=f"admin-scale-{survey_index}",
            titulo=f"Admin scale {survey_index}",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(survey)
        db.session.flush()
        for response_index, origin in enumerate(
            ["real", "real", "real", "synthetic_demo", "legacy_unverified"]
        ):
            response = EncRespuesta(
                encuesta_id=survey.id,
                tenant_id=tenant.id,
                response_origin=origin,
                huella_unica=f"admin-scale-{survey_index}-{response_index}",
                canal="web",
                barrio="centro",
                lat=-32.9 + survey_index / 1000,
                lng=-68.8 - survey_index / 1000,
            )
            db.session.add(response)
            expected_real += origin == "real"
            expected_synthetic += origin == "synthetic_demo"
            expected_unverified += origin == "legacy_unverified"
    db.session.commit()

    with _captured_sql() as bundle_sql:
        bundle = admin_tenant_routes._build_tenant_dashboard_bundle_payload(
            tenant,
            viewer=owner,
            surveys_limit=20,
        )
    with _captured_sql() as heatmap_sql:
        heatmap = admin_tenant_routes._build_tenant_heatmap_summary_payload(
            tenant,
            viewer=owner,
            limit_points=500,
        )
    with _captured_sql() as overview_sql:
        overview_response = client.get(
            f"/api/admin/tenants/{tenant.slug}/encuestas/overview?limit=20",
            headers=_headers(owner),
        )

    assert bundle["surveys"]["total_responses"] == expected_real
    assert bundle["surveys"]["response_provenance"][
        "synthetic_responses_excluded"
    ] == expected_synthetic
    assert bundle["surveys"]["response_provenance"][
        "unverified_responses_excluded"
    ] == expected_unverified
    assert heatmap["response_provenance"]["real_responses_included"] == expected_real
    assert heatmap["response_provenance"][
        "unverified_responses_excluded"
    ] == expected_unverified
    assert overview_response.status_code == 200
    overview = overview_response.get_json()
    assert overview["total_responses"] == expected_real
    assert overview["response_provenance"][
        "unverified_responses_excluded"
    ] == expected_unverified

    def _response_selects(statements):
        return [
            statement
            for statement in statements
            if statement.startswith("select") and "enc_respuesta" in statement
        ]

    bundle_response_sql = _response_selects(bundle_sql)
    heatmap_response_sql = _response_selects(heatmap_sql)
    overview_response_sql = _response_selects(overview_sql)
    assert len(bundle_response_sql) == 1
    assert len(overview_response_sql) == 1
    assert len(heatmap_response_sql) == 3
    assert "group by enc_respuesta.encuesta_id" in bundle_response_sql[0]
    assert "group by enc_respuesta.encuesta_id" in overview_response_sql[0]
    assert any(" limit " in statement for statement in heatmap_response_sql)

    all_response_sql = [
        *bundle_response_sql,
        *heatmap_response_sql,
        *overview_response_sql,
    ]
    assert all("response_origin" in statement for statement in all_response_sql)
    assert all("enc_respuesta.id in (" not in statement for statement in all_response_sql)
    assert all("enc_respuesta.metadata_payload" not in statement for statement in all_response_sql)
    assert all("enc_respuesta.huella_unica" not in statement for statement in all_response_sql)
