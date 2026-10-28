"""Synthetic local regressions for the legacy metrics boundary."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from models import PymePedido, Role, TenantProfile, User, UserRole, db
from services.auth_session_lifecycle import issue_token


def _headers(actor, tenant):
    claims = {
        "user_id": actor.id, "rol": actor.rol, "tipo_chat": actor.tipo_chat,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if actor.rol == "super_admin":
        from utils.auth_helpers import auth_session_version
        claims.update(auth_provider="clerk", session_kind="clerk",
                      clerk_sid=f"synthetic-metrics-session-{actor.id}",
                      sv=auth_session_version(actor))
    token = issue_token(claims)
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant.slug}


def _owner_and_tenant(suffix, *, kind="pyme"):
    owner = User(name=f"Synthetic {suffix}", email=f"metrics-{suffix}@example.test",
                 rol="admin", tipo_chat=kind)
    owner.set_password("local-synthetic-metrics")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug=f"metrics-{suffix}", nombre=f"Synthetic {suffix}",
                           tipo=kind, plan="enterprise", is_active=True,
                           **{f"{kind}_id": owner.id})
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
    setattr(owner, f"{kind}_id", owner.id)
    return owner, tenant


def _order(owner, tenant_id, amount):
    order = PymePedido(pyme_id=owner.id, tenant_id=tenant_id,
                       asunto="Synthetic metrics fixture", detalles="[]",
                       monto_total=amount, user_id=owner.id, moneda="ARS")
    db.session.add(order)
    return order


@pytest.mark.parametrize("path", ["/api/metrics/summary", "/estadisticas/dashboard?tipo=pyme"])
def test_metrics_exact_tenant_quarantines_same_owner_and_ambiguous_legacy(client, path):
    owner, selected = _owner_and_tenant("shared-selected")
    other = TenantProfile(slug="metrics-shared-other", nombre="Synthetic other",
                          tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(other)
    db.session.flush()
    _order(owner, selected.id, 100)
    _order(owner, other.id, 900)
    _order(owner, None, 500)
    db.session.commit()
    response = client.get(path, headers=_headers(owner, selected))
    assert response.status_code == 200
    payload = response.get_json()
    if path.startswith("/api/metrics"):
        assert payload["total_sales"] == 100
        assert payload["total_orders"] == 1
        assert payload["new_customers"] == 1
    else:
        assert payload["summary"]["total_ventas"] == 100
        assert payload["summary"]["total_pedidos"] == 1


def test_metrics_unique_owner_keeps_only_its_unbound_legacy_orders(client):
    owner, selected = _owner_and_tenant("unique-selected")
    _, foreign = _owner_and_tenant("unique-foreign")
    _order(owner, selected.id, 100)
    _order(owner, None, 50)
    _order(owner, foreign.id, 900)
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, selected))
    assert response.status_code == 200
    assert response.get_json()["total_sales"] == 150
    assert response.get_json()["total_orders"] == 2


@pytest.mark.parametrize("path", ["/api/municipal/metrics/summary", "/api/municipal/metricas/summary"])
@pytest.mark.parametrize("role", ["empleado", "operador"])
def test_municipal_employee_metrics_denied_before_materialization(client, path, role):
    owner, tenant = _owner_and_tenant(f"municipal-{role}", kind="municipio")
    employee = User(name="Synthetic operator", email=f"metrics-{role}@example.test",
                    rol=role, tipo_chat="municipio", es_empleado=True,
                    tenant_id=tenant.id, tenant_slug=tenant.slug,
                    municipio_id=owner.id, empresa_id=owner.id,
                    accesibilidad={"employee_scope": {"categorias": ["luminarias"]}})
    employee.set_password("local-synthetic-metrics")
    db.session.add(employee)
    db.session.commit()
    headers = {**_headers(employee, tenant), "X-Request-Id": "metrics-category-boundary"}
    with patch("routes.metricas.MunicipioMetricasService") as service:
        for method, value in {"get_total_tickets": 123, "get_open_tickets": 100,
                              "get_closed_tickets": 23, "get_unique_citizens": 99,
                              "get_resolution_rate": 0.19}.items():
            getattr(service.return_value, method).return_value = value
        response = client.get(path, headers=headers)
    assert response.status_code == 403
    payload = response.get_json()
    assert payload["reason_code"] == "employee_analytics_scope_unsupported"
    assert payload["replacement_endpoint"] == "/api/v2/analytics/operations/dashboard"
    assert payload["request_id"] == "metrics-category-boundary"
    assert response.headers["X-Request-Id"] == "metrics-category-boundary"
    assert response.headers["Cache-Control"] == "no-store"
    service.assert_not_called()


def test_business_metrics_exposes_measured_counts_and_unknown_rates(client):
    owner, tenant = _owner_and_tenant("measured-kpis")
    _order(owner, tenant.id, 100)
    _order(owner, tenant.id, 50)
    db.session.commit()
    headers = _headers(owner, tenant)
    for path in ("/api/metrics/summary", "/api/metrics/kpis"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data["contract_version"] == "business.metrics.v1"
        assert data["total_sales"] == 150
        assert data["total_orders"] == 2
        assert data["unique_customers"] == data["new_customers"] == 1
        assert data["avg_order_value"] == 75
        assert data["currency"] == "ARS"
        assert data["conversion_rate"] is None
        assert data["trend"] is None
        assert data["availability"]["conversion_rate"]["reason_code"] == "missing_conversion_denominator"
        assert data["availability"]["total_sales"]["definition"] == "sum_order_amount"
        if path.endswith("kpis"):
            assert data["sales_growth"] is None
            assert data["customer_acquisition_cost"] is None
            assert data["customer_lifetime_value"] is None


@pytest.mark.parametrize("resource,reason", [
    ("sales-over-time", "sales_timeseries_not_supported"),
    ("top-products", "product_sales_not_supported"),
    ("sales-by-region", "regional_sales_not_supported"),
])
def test_unsupported_business_series_are_explicitly_unavailable(client, resource, reason):
    owner, tenant = _owner_and_tenant(f"unavailable-{resource}")
    db.session.commit()
    response = client.get(f"/api/metrics/{resource}", headers=_headers(owner, tenant))
    assert response.status_code == 200
    assert response.get_json() == []
    assert response.headers["X-Metrics-Availability"] == "unavailable"
    assert response.headers["X-Metrics-Reason"] == reason


def test_dashboard_marks_unsupported_modules_and_conversion_unavailable(client):
    owner, tenant = _owner_and_tenant("dashboard-unavailable")
    _order(owner, tenant.id, 100)
    db.session.commit()
    response = client.get("/estadisticas/dashboard?tipo=pyme", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    assert data["summary"]["tasa_conversion"] is None
    assert all(card["label"] != "Tasa de Conversión" for card in data["cards"])
    for module in ("ventas_over_time", "top_productos", "ventas_por_region"):
        assert data["stats"][module] == []
        assert data["stats"]["availability"][module]["state"] == "unavailable"


@pytest.mark.parametrize("currency", [None, "USD", "INVALID"])
def test_money_with_unknown_or_mixed_currency_is_not_summed(client, currency):
    owner, tenant = _owner_and_tenant("currency-limits")
    _order(owner, tenant.id, 100)
    order = _order(owner, tenant.id, 50)
    order.moneda = currency
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_orders"] == 2
    assert data["unique_customers"] == 1
    assert data["total_sales"] is None
    assert data["avg_order_value"] is None
    assert data["currency"] is None
    assert data["availability"]["total_sales"]["reason_code"] == "mixed_or_unknown_currency"


def test_missing_amount_does_not_become_complete_total(client):
    owner, tenant = _owner_and_tenant("missing-amount")
    _order(owner, tenant.id, 100)
    _order(owner, tenant.id, None)
    db.session.commit()
    response = client.get("/api/metrics/kpis", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_orders"] == 2
    assert data["total_sales"] is None
    assert data["avg_order_value"] is None
    assert data["availability"]["total_sales"]["reason_code"] == "missing_order_amount"


def test_no_orders_preserves_observed_zero_but_has_no_average(client):
    owner, tenant = _owner_and_tenant("no-orders")
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_sales"] == data["total_orders"] == data["unique_customers"] == 0
    assert data["avg_order_value"] is None
    assert data["currency"] is None
    assert data["availability"]["avg_order_value"]["reason_code"] == "no_orders"


def test_metrics_selector_cannot_retarget_another_profile_with_shared_owner(client):
    owner, selected = _owner_and_tenant("retarget-selected")
    other = TenantProfile(slug="metrics-retarget-other", nombre="Synthetic other",
                          tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(other)
    db.session.commit()
    headers = {**_headers(owner, selected), "X-Tenant-Slug": other.slug}
    with patch("routes.metricas.MetricasService") as service:
        response = client.get("/api/metrics/summary", headers=headers)
    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "metrics_tenant_scope_unavailable"
    service.assert_not_called()


def test_ambiguous_owner_without_exact_membership_does_not_get_zero_metrics(client):
    owner, selected = _owner_and_tenant("ambiguous-without-membership")
    other = TenantProfile(slug="metrics-ambiguous-other", nombre="Synthetic other",
                          tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(other)
    owner.tenant_id = owner.tenant_slug = None
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, selected))
    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "metrics_tenant_scope_unavailable"


def test_dashboard_shared_owner_cannot_override_exact_membership(client):
    owner, selected = _owner_and_tenant("dashboard-retarget-selected")
    other = TenantProfile(slug="metrics-dashboard-retarget-other", nombre="Synthetic other",
                          tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(other)
    db.session.flush()
    _order(owner, selected.id, 100)
    _order(owner, other.id, 900)
    db.session.commit()
    headers = {**_headers(owner, selected), "X-Tenant-Slug": other.slug}
    with patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa", return_value=[]) as materializer:
        response = client.get("/estadisticas/dashboard?tipo=pyme", headers=headers)
    assert response.status_code == 403
    materializer.assert_not_called()


@pytest.mark.parametrize("path", ["/api/metrics/summary", "/estadisticas/dashboard?tipo=pyme"])
def test_registered_tenant_admin_grant_is_preserved(client, path):
    actor, primary = _owner_and_tenant("registered-primary")
    owner, granted = _owner_and_tenant("registered-granted")
    role = Role.query.filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin")
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=actor.id, role_id=role.id, tenant_id=granted.id))
    _order(actor, primary.id, 100)
    _order(owner, granted.id, 900)
    db.session.commit()
    headers = {**_headers(actor, primary), "X-Tenant-Slug": granted.slug}
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    amount = payload["total_sales"] if path.startswith("/api/metrics") else payload["summary"]["total_ventas"]
    assert amount == 900


def test_allowlisted_platform_admin_explicitly_reads_only_selected_tenant(client, monkeypatch):
    owner, selected = _owner_and_tenant("platform-selected")
    other = TenantProfile(slug="metrics-platform-other", nombre="Synthetic other",
                          tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(other)
    db.session.flush()
    _order(owner, selected.id, 100)
    _order(owner, other.id, 900)
    platform = User(name="Synthetic platform admin", email="metrics-platform@example.test",
                    rol="super_admin", tipo_chat="pyme")
    platform.set_password("local-synthetic-metrics")
    db.session.add(platform)
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    response = client.get("/estadisticas/dashboard?tipo=pyme", headers=_headers(platform, other))
    assert response.status_code == 200
    assert response.get_json()["summary"]["total_ventas"] == 900


def test_legacy_unique_owner_without_exact_membership_still_works(client):
    owner, tenant = _owner_and_tenant("legacy-unique-membership")
    owner.tenant_id = owner.tenant_slug = None
    _order(owner, tenant.id, 100)
    db.session.commit()
    for path in ("/api/metrics/summary", "/estadisticas/dashboard?tipo=pyme"):
        response = client.get(path, headers=_headers(owner, tenant))
        assert response.status_code == 200


@pytest.mark.parametrize("amount,reason", [
    (float("inf"), "invalid_order_amount"),
    (9007199254740992, "unsafe_order_amount"),
])
def test_nonfinite_or_javascript_unsafe_amount_is_unavailable(client, amount, reason):
    owner, tenant = _owner_and_tenant("unsafe-amount")
    _order(owner, tenant.id, amount)
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_sales"] is None
    assert data["avg_order_value"] is None
    assert data["availability"]["total_sales"]["reason_code"] == reason


def test_metrics_storage_failure_is_not_returned_as_zero(client):
    owner, tenant = _owner_and_tenant("storage-error")
    db.session.commit()
    headers = _headers(owner, tenant)
    with patch("services.metricas_service.MetricasService._order_query", side_effect=RuntimeError("synthetic storage failure")):
        with pytest.raises(RuntimeError, match="synthetic storage failure"):
            client.get("/api/metrics/summary", headers=headers)


def test_metrics_unavailable_headers_are_readable_from_existing_allowed_origin(client):
    owner, tenant = _owner_and_tenant("cors-metadata")
    db.session.commit()
    origin = "https://www.chatboc.ar"
    response = client.get("/api/metrics/sales-over-time", headers={
        **_headers(owner, tenant), "Origin": origin,
    })
    assert response.status_code == 200
    assert response.get_json() == []
    assert response.headers.getlist("Access-Control-Allow-Origin") == [origin]
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    exposed = {value.strip().lower() for value in response.headers["Access-Control-Expose-Headers"].split(",")}
    assert {"x-metrics-availability", "x-metrics-reason"}.issubset(exposed)
    assert response.headers["X-Metrics-Availability"] == "unavailable"


def test_municipal_dashboard_keeps_existing_role_denial_before_ticket_reads(client):
    _, tenant = _owner_and_tenant("municipal-role-boundary", kind="municipio")
    citizen = User(name="Synthetic citizen", email="metrics-citizen@example.test",
                   rol="usuario", tipo_chat="municipio", tenant_id=tenant.id,
                   tenant_slug=tenant.slug)
    citizen.set_password("local-synthetic-metrics")
    db.session.add(citizen)
    db.session.commit()
    with patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa") as source:
        response = client.get("/estadisticas/dashboard?tipo=municipio", headers=_headers(citizen, tenant))
    assert response.status_code == 403
    source.assert_not_called()


def test_non_allowlisted_platform_role_does_not_reach_business_reads(client, monkeypatch):
    _, tenant = _owner_and_tenant("forged-platform-target")
    platform = User(name="Synthetic unauthorized platform role", email="metrics-not-allowed@example.test",
                    rol="super_admin", tipo_chat="pyme")
    platform.set_password("local-synthetic-metrics")
    db.session.add(platform)
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "metrics-allowed@example.test")
    with patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa") as source:
        response = client.get("/estadisticas/dashboard?tipo=pyme", headers=_headers(platform, tenant))
    assert response.status_code in {401, 403}
    source.assert_not_called()


@pytest.mark.parametrize("has_second_currency", [False, True])
def test_currency_case_and_whitespace_normalization_preserves_mixed_detection(client, has_second_currency):
    owner, tenant = _owner_and_tenant("normalized-currencies")
    for amount, currency in ((100, "ARS"), (50, "ars"), (25, " ARS ")):
        order = _order(owner, tenant.id, amount)
        order.moneda = currency
    if has_second_currency:
        order = _order(owner, tenant.id, 10)
        order.moneda = " usd "
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    if has_second_currency:
        assert data["total_sales"] is None
        assert data["availability"]["total_sales"]["reason_code"] == "mixed_or_unknown_currency"
    else:
        assert data["total_sales"] == 175
        assert data["currency"] == "ARS"


@pytest.mark.parametrize("selector", ["tenant_slug", "tenant", "tenant_id"])
def test_dashboard_rejects_conflicting_query_and_header_before_aggregate_reads(client, selector):
    from services.metricas_service import MetricasService
    actor, primary = _owner_and_tenant("conflict-primary")
    owner, granted = _owner_and_tenant("conflict-granted")
    role = Role.query.filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin")
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=actor.id, role_id=role.id, tenant_id=granted.id))
    _order(actor, primary.id, 100)
    _order(owner, granted.id, 900)
    db.session.commit()
    value = granted.id if selector == "tenant_id" else granted.slug
    with patch("routes.estadisticas.MetricasService", wraps=MetricasService) as aggregates, \
         patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa", return_value=[]) as heatmap:
        response = client.get("/estadisticas/dashboard", query_string={"tipo": "pyme", selector: value},
                              headers=_headers(actor, primary))
    assert response.status_code == 403
    aggregates.assert_not_called()
    heatmap.assert_not_called()


def test_dashboard_single_query_granted_scope_still_reads_only_that_profile(client):
    actor, primary = _owner_and_tenant("single-query-primary")
    owner, granted = _owner_and_tenant("single-query-granted")
    role = Role.query.filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin")
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=actor.id, role_id=role.id, tenant_id=granted.id))
    _order(actor, primary.id, 100)
    _order(owner, granted.id, 900)
    db.session.commit()
    headers = _headers(actor, primary)
    headers.pop("X-Tenant-Slug")
    response = client.get("/estadisticas/dashboard?tipo=pyme&tenant_slug=" + granted.slug, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["summary"]["total_ventas"] == 900



@pytest.mark.parametrize("padding", ["\t", "\n", "\r", "\u00a0"])
def test_currency_padding_cannot_collapse_a_bounded_distinct_result(client, padding):
    owner, tenant = _owner_and_tenant("currency-padding")
    _order(owner, tenant.id, 100)
    padded = _order(owner, tenant.id, 50)
    padded.moneda = "ARS" + padding
    foreign = _order(owner, tenant.id, 1000)
    foreign.moneda = "USD"
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_orders"] == 3
    assert data["total_sales"] is None
    assert data["avg_order_value"] is None
    assert data["currency"] is None
    assert data["availability"]["total_sales"]["reason_code"] == "mixed_or_unknown_currency"


def test_unknown_tab_padded_currency_remains_unavailable(client):
    owner, tenant = _owner_and_tenant("unknown-tab-currency")
    row = _order(owner, tenant.id, 100)
    row.moneda = "ARS\t"
    db.session.commit()
    response = client.get("/api/metrics/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200
    assert response.get_json()["total_sales"] is None
    assert response.get_json()["currency"] is None


def test_dashboard_no_resolved_tenant_fails_closed_before_reads(client):
    from services.metricas_service import MetricasService
    owner, tenant = _owner_and_tenant("no-resolved-tenant")
    _order(owner, tenant.id, 100)
    db.session.commit()
    headers = _headers(owner, tenant)
    headers.pop("X-Tenant-Slug")
    with patch("routes.estadisticas._resolve_tenant_profile_or_error", return_value=None), \
         patch("routes.estadisticas.MetricasService", wraps=MetricasService) as aggregates, \
         patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa", return_value=[]) as heatmap:
        response = client.get("/estadisticas/dashboard?tipo=pyme", headers=headers)
    assert response.status_code == 403
    aggregates.assert_not_called()
    heatmap.assert_not_called()


def test_dashboard_legacy_summary_does_not_claim_the_canonical_v1_contract(client):
    owner, tenant = _owner_and_tenant("legacy-summary-contract")
    _order(owner, tenant.id, 100)
    _order(owner, tenant.id, 50)
    db.session.commit()
    response = client.get("/estadisticas/dashboard?tipo=pyme", headers=_headers(owner, tenant))
    assert response.status_code == 200
    payload = response.get_json()
    legacy = payload["summary"]
    assert "contract_version" not in legacy
    assert "availability" not in legacy
    assert legacy["total_ventas"] == 150
    assert legacy["total_pedidos"] == 2
    canonical = payload["stats"]["kpis"]
    assert canonical["contract_version"] == "business.metrics.v1"
    assert canonical["scope"]["tenant_id"] == tenant.id
    assert canonical["total_sales"] == 150
    assert canonical["total_orders"] == 2
    assert canonical["unique_customers"] == 1
    assert canonical["avg_order_value"] == 75
    assert canonical["availability"]["total_sales"]["definition"] == "sum_order_amount"


@pytest.mark.parametrize("path", ["/api/metrics/summary", "/api/metrics/kpis"])
def test_metrics_grant_revocation_blocks_the_next_read_before_materialization(client, path):
    from services.metricas_service import MetricasService
    actor, primary = _owner_and_tenant("revoke-primary")
    owner, granted = _owner_and_tenant("revoke-target")
    role = Role.query.filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin")
        db.session.add(role)
        db.session.flush()
    grant = UserRole(user_id=actor.id, role_id=role.id, tenant_id=granted.id)
    db.session.add(grant)
    _order(owner, granted.id, 900)
    db.session.commit()
    headers = {**_headers(actor, primary), "X-Tenant-Slug": granted.slug}
    allowed = client.get(path, headers=headers)
    assert allowed.status_code == 200 and allowed.get_json()["total_sales"] == 900
    db.session.delete(grant)
    db.session.commit()
    with patch("routes.metricas.MetricasService", wraps=MetricasService) as aggregates:
        denied = client.get(path, headers=headers)
    assert denied.status_code == 403
    aggregates.assert_not_called()


@pytest.mark.parametrize("path", ["/api/metrics/summary", "/api/metrics/kpis"])
def test_metrics_rejects_query_header_conflict_even_with_a_registered_grant(client, path):
    from services.metricas_service import MetricasService
    actor, primary = _owner_and_tenant("api-conflict-primary")
    _, granted = _owner_and_tenant("api-conflict-granted")
    role = Role.query.filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin")
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=actor.id, role_id=role.id, tenant_id=granted.id))
    db.session.commit()
    with patch("routes.metricas.MetricasService", wraps=MetricasService) as aggregates:
        response = client.get(path, query_string={"tenant_slug": granted.slug}, headers=_headers(actor, primary))
    assert response.status_code == 403
    aggregates.assert_not_called()



@pytest.mark.parametrize("path", ["/api/metrics/summary", "/api/metrics/kpis"])
def test_metrics_ambiguous_owner_without_any_selector_is_denied_before_reads(client, path):
    from services.metricas_service import MetricasService
    owner, selected = _owner_and_tenant("no-selector-ambiguous")
    other = TenantProfile(slug="metrics-no-selector-other", nombre="Synthetic other",
                          tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(other)
    owner.tenant_id = owner.tenant_slug = None
    db.session.commit()
    headers = _headers(owner, selected)
    headers.pop("X-Tenant-Slug")
    with patch("routes.metricas.MetricasService", wraps=MetricasService) as aggregates:
        response = client.get(path, headers=headers)
    assert response.status_code == 403
    aggregates.assert_not_called()



@pytest.mark.parametrize("selector", ["query", "header"])
def test_dashboard_pyme_denials_do_not_disclose_profile_existence(client, selector):
    from services.metricas_service import MetricasService
    actor, primary = _owner_and_tenant("opaque-primary")
    _, foreign = _owner_and_tenant("opaque-foreign")
    db.session.commit()
    bodies = []
    with patch("routes.estadisticas.MetricasService", wraps=MetricasService) as aggregates, \
         patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa", return_value=[]) as heatmap:
        for slug in (foreign.slug, "metrics-opaque-not-created"):
            headers = _headers(actor, primary)
            query = {"tipo": "pyme"}
            if selector == "query":
                headers.pop("X-Tenant-Slug")
                query["tenant_slug"] = slug
            else:
                headers["X-Tenant-Slug"] = slug
            response = client.get("/estadisticas/dashboard", query_string=query, headers=headers)
            assert response.status_code == 403
            body = response.get_json()
            body.pop("request_id", None)
            bodies.append(body)
        aggregates.assert_not_called()
        heatmap.assert_not_called()
    assert bodies[0] == bodies[1]
    assert bodies[0] == {"error": "tenant_forbidden", "reason_code": "metrics_tenant_scope_unavailable"}
