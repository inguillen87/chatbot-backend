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


# Causal N2 closure contracts migrated from the immutable external HTTP RED.
from config import TestingConfig
from models import PymeTicket
N2_ROUTES = ("/estadisticas/mapa_calor/datos", "/estadisticas/tickets")

@pytest.fixture
def n2_scoped_fixture(client):
    assert TestingConfig.SQLALCHEMY_DATABASE_URI == 'sqlite:///:memory:'
    assert str(db.engine.url) == 'sqlite:///:memory:'
    actor = User(name='Synthetic scoped owner', email='n2-owner@example.invalid', rol='admin', tipo_chat='pyme')
    actor.set_password('synthetic-n2-only')
    db.session.add(actor)
    db.session.flush()
    a = TenantProfile(slug='n2-synthetic-a', nombre='Synthetic A', tipo='pyme', plan='enterprise', pyme_id=actor.id, is_active=True)
    b = TenantProfile(slug='n2-synthetic-b', nombre='Synthetic B', tipo='pyme', plan='enterprise', pyme_id=actor.id, is_active=True)
    db.session.add_all([a, b])
    db.session.flush()
    actor.tenant_id, actor.tenant_slug, actor.pyme_id = (a.id, a.slug, actor.id)
    role = Role.query.filter_by(name='admin').first()
    if role is None:
        role = Role(name='admin')
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=actor.id, tenant_id=a.id, role_id=role.id))
    for index in range(10):
        selected = a if index < 3 else b
        lat, lng = (-33.0, -60.0) if index < 3 else (-34.0, -61.0)
        db.session.add(PymeTicket(tenant_id=selected.id, user_id=actor.id, nro_ticket=50000 + index, pregunta='Synthetic N2 scope marker', categoria='synthetic-category', estado='nuevo', latitud=lat, longitud=lng))
    db.session.commit()
    assert UserRole.query.filter_by(user_id=actor.id, tenant_id=b.id).count() == 0
    return (actor, a, b, role)

def _n2_observed(response):
    payload = response.get_json(silent=True)
    points = payload.get('heatmap', []) if isinstance(payload, dict) else []
    return {'status_code': response.status_code, 'heatmap_groups': len(points), 'total_weight': sum((point.get('weight', 0) for point in points)), 'has_a_marker': any((point.get('location') == {'lat': -33.0, 'lng': -60.0} for point in points)), 'has_b_marker': any((point.get('location') == {'lat': -34.0, 'lng': -61.0} for point in points))}

@pytest.mark.parametrize('path', N2_ROUTES)
@pytest.mark.parametrize('selection', ['foreign_header', 'query_b_header_a'])
def test_n2_shared_owner_does_not_authorize_other_tenant(client, n2_scoped_fixture, path, selection):
    actor, a, b, role = n2_scoped_fixture
    query = {'tipo': 'pyme'}
    headers = _headers(actor, b if selection == 'foreign_header' else a)
    if selection == 'query_b_header_a':
        query['tenant_slug'] = b.slug
    response = client.get(path, query_string=query, headers=headers)
    observed = _n2_observed(response)
    assert response.status_code == 403, observed

@pytest.mark.parametrize('path', N2_ROUTES)
def test_n2_legitimate_exact_a_and_registered_b_controls(client, n2_scoped_fixture, path):
    actor, a, b, role = n2_scoped_fixture
    local = client.get(path, query_string={'tipo': 'pyme'}, headers=_headers(actor, a))
    first = _n2_observed(local)
    assert first == {'status_code': 200, 'heatmap_groups': 1, 'total_weight': 3.0, 'has_a_marker': True, 'has_b_marker': False}
    db.session.add(UserRole(user_id=actor.id, tenant_id=b.id, role_id=role.id))
    db.session.commit()
    granted = client.get(path, query_string={'tipo': 'pyme'}, headers=_headers(actor, b))
    second = _n2_observed(granted)
    assert second == {'status_code': 200, 'heatmap_groups': 1, 'total_weight': 7.0, 'has_a_marker': False, 'has_b_marker': True}

@pytest.mark.parametrize('path', N2_ROUTES)
def test_n2_unrelated_owner_without_grant_is_denied_control(client, n2_scoped_fixture, path):
    actor, a, b, role = n2_scoped_fixture
    other = User(name='Synthetic unrelated owner', email='n2-other@example.invalid', rol='admin', tipo_chat='pyme')
    other.set_password('synthetic-n2-only')
    db.session.add(other)
    db.session.flush()
    other_tenant = TenantProfile(slug='n2-synthetic-unrelated', nombre='Synthetic unrelated', tipo='pyme', pyme_id=other.id, is_active=True)
    db.session.add(other_tenant)
    db.session.commit()
    denied = client.get(path, query_string={'tipo': 'pyme'}, headers=_headers(actor, other_tenant))
    observed = _n2_observed(denied)
    assert denied.status_code == 403
    assert not observed['has_a_marker'] and (not observed['has_b_marker'])

    # The existing outer decorator still rejects unrelated-owner grants here;
    # unlike the metrics dashboard, this closure does not widen admission.
    db.session.add(UserRole(user_id=actor.id, tenant_id=other_tenant.id, role_id=role.id))
    db.session.commit()
    still_denied = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(actor, other_tenant))
    assert still_denied.status_code == 403


@pytest.mark.parametrize("path", N2_ROUTES)
def test_n2_grant_revocation_blocks_next_read_before_materialization(client, n2_scoped_fixture, path):
    from services.ticket_service import servicio_tickets
    actor, a, b, role = n2_scoped_fixture
    grant = UserRole(user_id=actor.id, tenant_id=b.id, role_id=role.id)
    db.session.add(grant)
    db.session.commit()
    allowed = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(actor, b))
    assert _n2_observed(allowed)["total_weight"] == 7
    db.session.delete(grant)
    db.session.commit()
    with patch.object(servicio_tickets, "obtener_tickets_con_ubicacion_para_mapa", wraps=servicio_tickets.obtener_tickets_con_ubicacion_para_mapa) as source:
        denied = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(actor, b))
    assert denied.status_code == 403
    source.assert_not_called()


@pytest.mark.parametrize("path", N2_ROUTES)
def test_n2_pyme_denials_have_one_body_and_no_store(client, n2_scoped_fixture, path):
    actor, a, b, role = n2_scoped_fixture
    bodies = []
    for selected, query in ((b, {"tipo": "pyme"}),
                             (a, {"tipo": "pyme", "tenant_slug": "n2-unknown-not-created"}),
                             (a, {"tipo": "pyme", "tenant_slug": b.slug})):
        response = client.get(path, query_string=query, headers=_headers(actor, selected))
        bodies.append(response.get_json())
        assert response.status_code == 403
        assert response.headers["Cache-Control"] == "no-store"
    assert bodies == [{"error": "tenant_forbidden", "reason_code": "metrics_tenant_scope_unavailable"}] * 3


@pytest.mark.parametrize("path", N2_ROUTES)
@pytest.mark.parametrize("role_name,employee_flag", [("empleado", True), ("operador", False)])
def test_n2_employee_retains_exact_tenant_and_category_boundary(client, n2_scoped_fixture, path, role_name, employee_flag):
    actor, a, b, role = n2_scoped_fixture
    for index in range(4):
        db.session.add(PymeTicket(tenant_id=a.id, user_id=actor.id, nro_ticket=51000+index,
                                 pregunta="Synthetic allowed category", categoria="synthetic-category", latitud=-33, longitud=-60))
    for index in range(5):
        db.session.add(PymeTicket(tenant_id=a.id, user_id=actor.id, nro_ticket=52000+index,
                                 pregunta="Synthetic forbidden category", categoria="out-of-scope", latitud=-32, longitud=-59))
    employee = User(name="Synthetic scoped employee", email="n2-employee@example.invalid", rol=role_name,
                    tipo_chat="pyme", es_empleado=employee_flag, pyme_id=actor.id,
                    tenant_id=a.id, tenant_slug=a.slug,
                    accesibilidad={"employee_scope": {"categorias": ["synthetic-category"]}})
    employee.set_password("synthetic-n2-only")
    db.session.add(employee)
    db.session.commit()
    allowed = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(employee, a))
    assert allowed.status_code == 200
    assert _n2_observed(allowed)["total_weight"] == 7
    assert allowed.get_json()["privacy"]["mode"] == "employee_aggregated"
    assert {point.get("categoria") for point in allowed.get_json()["heatmap"]} == {"synthetic-category"}
    denied = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(employee, b))
    assert denied.status_code == 403

    a.is_active = False
    db.session.commit()
    inactive = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(employee, a))
    # The bearer validator retires inactive-tenant identity before this wrapper.
    # Independently verify the local employee guard also requires active scope.
    from routes.estadisticas import _stats_tenant_access_allowed
    assert _stats_tenant_access_allowed(employee, a, requested_tipo="pyme", explicit_selection=True) is False
    assert inactive.status_code == 401


@pytest.mark.parametrize("path", N2_ROUTES)
def test_n2_flags_do_not_expand_outer_role_admission(client, n2_scoped_fixture, path):
    actor, a, b, role = n2_scoped_fixture
    flagged = User(name="Synthetic flagged citizen", email="n2-flagged@example.invalid", rol="usuario",
                   tipo_chat="pyme", es_empleado=True, tenant_id=a.id, tenant_slug=a.slug,
                   empresa_id=actor.id, accesibilidad={"employee_scope": {"categorias": ["synthetic-category"]}})
    flagged.set_password("synthetic-n2-only")
    db.session.add(flagged)
    actor.es_empleado = True
    db.session.commit()
    denied = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(flagged, a))
    assert denied.status_code == 403
    admitted_admin = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(actor, a))
    assert admitted_admin.status_code == 200 and _n2_observed(admitted_admin)["total_weight"] == 3


@pytest.mark.parametrize("path", N2_ROUTES)
def test_n2_legacy_unique_resolution_is_preserved_but_ambiguous_owner_is_denied(client, path):
    actor, a = _owner_and_tenant("n2-legacy-unique")
    actor.tenant_id = actor.tenant_slug = None
    db.session.add(PymeTicket(tenant_id=a.id, user_id=actor.id, nro_ticket=53000,
                             pregunta="Synthetic unique legacy", categoria="synthetic-category", latitud=-33, longitud=-60))
    db.session.commit()
    headers = _headers(actor, a)
    headers.pop("X-Tenant-Slug")
    allowed = client.get(path, query_string={"tipo": "pyme"}, headers=headers)
    assert allowed.status_code == 200 and _n2_observed(allowed)["total_weight"] == 1
    db.session.add(TenantProfile(slug="n2-legacy-ambiguous", nombre="Synthetic ambiguous",
                                 tipo="pyme", pyme_id=actor.id, is_active=True))
    db.session.commit()
    denied = client.get(path, query_string={"tipo": "pyme"}, headers=headers)
    assert denied.status_code == 403


def test_n2_unresolved_tenant_is_denied_even_in_testing(client, n2_scoped_fixture):
    from routes.estadisticas import _stats_tenant_access_allowed
    actor, a, b, role = n2_scoped_fixture
    assert _stats_tenant_access_allowed(actor, None, requested_tipo="pyme") is False


@pytest.mark.parametrize("path", N2_ROUTES)
def test_n2_platform_selection_needs_actual_allowlist_and_one_scope(client, n2_scoped_fixture, path, monkeypatch):
    actor, a, b, role = n2_scoped_fixture
    platform = User(name="Synthetic platform", email="n2-platform@example.invalid", rol="super_admin", tipo_chat="pyme")
    platform.set_password("synthetic-n2-only")
    db.session.add(platform)
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    allowed = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(platform, b))
    assert allowed.status_code == 200 and _n2_observed(allowed)["total_weight"] == 7
    conflict = client.get(path, query_string={"tipo": "pyme", "tenant_slug": b.slug}, headers=_headers(platform, a))
    assert conflict.status_code == 403
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "n2-other-platform@example.invalid")
    denied = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(platform, b))
    # Bearer identity rejection precedes the local role wrapper; preserve401.
    assert denied.status_code == 401


@pytest.mark.parametrize("path", N2_ROUTES)
def test_n2_ambiguous_company_reference_is_not_a_credited_employee_entitlement(client, n2_scoped_fixture, path):
    actor, a, b, role = n2_scoped_fixture
    employee = User(name="Synthetic ambiguous employee", email="n2-ambiguous-employee@example.invalid",
                    rol="empleado", tipo_chat="pyme", es_empleado=True, empresa_id=actor.id, pyme_id=actor.id,
                    tenant_id=a.id, tenant_slug=a.slug,
                    accesibilidad={"employee_scope": {"categorias": ["synthetic-category"]}})
    employee.set_password("synthetic-n2-only")
    db.session.add(employee)
    db.session.commit()
    from utils.tenant_admin_access import resolve_consistent_user_tenant
    assert resolve_consistent_user_tenant(employee) is None
    denied = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(employee, a))
    assert denied.status_code == 403


@pytest.mark.parametrize("path", N2_ROUTES)
@pytest.mark.parametrize("role_name", ["admin", "empleado", "operador"])
def test_n2_municipal_role_and_category_projection_remain_unchanged(client, path, role_name):
    from models import MunicipioTicket
    owner, tenant = _owner_and_tenant("n2-municipal", kind="municipio")
    for index in range(12):
        db.session.add(MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id,
                         pregunta="Synthetic municipal scope", categoria="allowed" if index < 7 else "forbidden",
                         latitud=-33 if index < 7 else -32, longitud=-60 if index < 7 else -59))
    actor = owner
    if role_name != "admin":
        actor = User(name="Synthetic municipal employee", email="n2-municipal-employee@example.invalid",
                     rol=role_name, tipo_chat="municipio", es_empleado=True, empresa_id=owner.id,
                     municipio_id=owner.id, tenant_id=tenant.id, tenant_slug=tenant.slug,
                     accesibilidad={"employee_scope": {"categorias": ["allowed"]}})
        actor.set_password("synthetic-n2-only")
        db.session.add(actor)
    db.session.commit()
    response = client.get(path, query_string={"tipo": "municipio"}, headers=_headers(actor, tenant))
    assert response.status_code == 200
    assert _n2_observed(response)["total_weight"] == (12 if role_name == "admin" else 7)
    if role_name != "admin":
        assert {point.get("categoria") for point in response.get_json()["heatmap"]} == {"allowed"}
        assert response.get_json()["privacy"]["mode"] == "employee_aggregated"



@pytest.mark.parametrize("path", N2_ROUTES)
@pytest.mark.parametrize("status", [400, 500])
def test_n2_role_wrapper_preserves_non403_failures(client, n2_scoped_fixture, path, status):
    actor, a, b, role = n2_scoped_fixture
    error = ValueError("Synthetic boundary failure") if status == 400 else RuntimeError("Synthetic boundary failure")
    with patch("routes.estadisticas.servicio_tickets.obtener_tickets_con_ubicacion_para_mapa", side_effect=error):
        response = client.get(path, query_string={"tipo": "pyme"}, headers=_headers(actor, a))
    assert response.status_code == status
    if status == 400:
        assert response.get_json() == {"error": "bad_request", "detail": "Synthetic boundary failure"}
    else:
        # Assert the route's own error response, with normal TESTING propagation:
        # the reserved logging key must not raise before this500 is constructed.
        assert response.get_json() == {"error": "server_error", "detail": "Error interno"}


def test_n2_role_wrapper_preserves_existing_headers_when_normalizing_only403(client, n2_scoped_fixture):
    from flask import jsonify
    from routes.estadisticas import _legacy_ticket_stats_role_access
    actor, a, b, role = n2_scoped_fixture
    def original_denial(current_user):
        return jsonify({"error": "Synthetic private detail"}), 403, {
            "X-Synthetic-Preserved": "present",
            "Access-Control-Allow-Origin": "https://panel.example.invalid",
            "Vary": "Origin",
        }
    wrapped = _legacy_ticket_stats_role_access(original_denial)
    with client.application.test_request_context("/estadisticas/tickets?tipo=pyme", headers=_headers(actor, a)):
        response = wrapped(actor)
    assert response.status_code == 403
    assert response.get_json() == {"error": "tenant_forbidden", "reason_code": "metrics_tenant_scope_unavailable"}
    assert response.headers["X-Synthetic-Preserved"] == "present"
    assert response.headers["Access-Control-Allow-Origin"] == "https://panel.example.invalid"
    assert response.headers["Vary"] == "Origin"
    assert response.headers["Cache-Control"] == "no-store"


# Requested-domain closure: external real HTTP/ORM RED preserved separately.
from sqlalchemy.exc import IntegrityError

def _n2_domain_observe(response):
    payload = response.get_json(silent=True)
    points = payload.get('heatmap', []) if isinstance(payload, dict) else []
    return {'status': response.status_code, 'groups': len(points), 'weight': sum((point.get('weight', 0) for point in points)), 'marker_a': any((point.get('location') == {'lat': -33.0, 'lng': -60.0} for point in points)), 'marker_c': any((point.get('location') == {'lat': -35.0, 'lng': -62.0} for point in points))}

def _n2_domain_fixture(kind='colegio', active=True):
    assert TestingConfig.SQLALCHEMY_DATABASE_URI == 'sqlite:///:memory:'
    assert str(db.engine.url) == 'sqlite:///:memory:'
    owner = User(name='Synthetic domain owner', email='domain-owner@example.invalid', rol='admin', tipo_chat='pyme')
    owner.set_password('synthetic-domain-only')
    db.session.add(owner)
    db.session.flush()
    a = TenantProfile(slug='domain-synthetic-a', nombre='Synthetic A', tipo='pyme', pyme_id=owner.id, is_active=True)
    b = TenantProfile(slug='domain-synthetic-b', nombre='Synthetic B', tipo='pyme', pyme_id=owner.id, is_active=True)
    c = TenantProfile(slug='domain-synthetic-c', nombre='Synthetic C', tipo=kind, pyme_id=owner.id, is_active=active)
    db.session.add_all([a, b, c])
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.pyme_id = (a.id, a.slug, owner.id)
    admin_role = Role.query.filter_by(name='admin').first()
    if admin_role is None:
        admin_role = Role(name='admin')
        db.session.add(admin_role)
        db.session.flush()
    db.session.add(UserRole(user_id=owner.id, tenant_id=a.id, role_id=admin_role.id))
    employee = User(name='Synthetic exact employee', email='domain-employee@example.invalid', rol='empleado', tipo_chat='pyme', es_empleado=True, tenant_id=a.id, tenant_slug=a.slug, pyme_id=owner.id, accesibilidad={'employee_scope': {'categorias': ['synthetic-category']}})
    employee.set_password('synthetic-domain-only')
    db.session.add(employee)
    db.session.flush()
    employee_role = Role.query.filter_by(name='empleado').first()
    if employee_role is None:
        employee_role = Role(name='empleado')
        db.session.add(employee_role)
        db.session.flush()
    db.session.add(UserRole(user_id=employee.id, tenant_id=a.id, role_id=employee_role.id))
    for index in range(18):
        selected = a if index < 7 else c
        lat, lng = (-33.0, -60.0) if index < 7 else (-35.0, -62.0)
        db.session.add(PymeTicket(tenant_id=selected.id, user_id=owner.id, nro_ticket=70000 + index, pregunta='Synthetic domain marker', categoria='synthetic-category', estado='nuevo', latitud=lat, longitud=lng))
    db.session.commit()
    assert UserRole.query.filter_by(tenant_id=c.id).count() == 0
    return (owner, employee, a, c)

@pytest.mark.parametrize('path', N2_ROUTES)
@pytest.mark.parametrize('actor_role', ['admin', 'empleado'])
@pytest.mark.parametrize('kind', ['colegio', 'unknown'])
@pytest.mark.parametrize('active', [True, False])
def test_n2_requested_pyme_domain_cannot_fall_through_to_legacy_owner(client, path, actor_role, kind, active):
    owner, employee, a, c = _n2_domain_fixture(kind, active)
    actor = owner if actor_role == 'admin' else employee
    response = client.get(path, query_string={'tipo': 'pyme'}, headers=_headers(actor, c))
    observed = _n2_domain_observe(response)
    assert response.status_code == 403, observed

@pytest.mark.parametrize('path', N2_ROUTES)
@pytest.mark.parametrize('actor_role', ['admin', 'empleado'])
def test_n2_requested_pyme_exact_a_legitimate_controls(client, path, actor_role):
    owner, employee, a, c = _n2_domain_fixture()
    actor = owner if actor_role == 'admin' else employee
    response = client.get(path, query_string={'tipo': 'pyme'}, headers=_headers(actor, a))
    observed = _n2_domain_observe(response)
    assert observed == {'status': 200, 'groups': 1, 'weight': 7.0, 'marker_a': True, 'marker_c': False}

def test_n2_null_profile_domain_is_not_a_persistable_variant_in_this_schema(client):
    assert TenantProfile.__table__.c.tipo.nullable is False
    with pytest.raises(IntegrityError) as error:
        with db.session.begin_nested():
            db.session.add(TenantProfile(slug='domain-null-not-persistable', nombre='Synthetic NULL control', tipo=None, is_active=True))
            db.session.flush()
    assert 'NOT NULL' in str(error.value.orig) and 'tenant_profile.tipo' in str(error.value.orig)



@pytest.mark.parametrize("path", N2_ROUTES)
@pytest.mark.parametrize("query", [
    {"tipo": " PYME "}, {"tipo_ticket": "pyme"}, {"tipo_ticket": " PYME "},
    {"tipo": "PYME", "tipo_ticket": " pyme "},
])
def test_n2_requested_domain_aliases_are_canonical_and_fail_closed(client, path, query):
    owner, employee, a, c = _n2_domain_fixture("unknown", True)
    allowed = client.get(path, query_string=query, headers=_headers(owner, a))
    assert _n2_domain_observe(allowed) == {"status": 200, "groups": 1, "weight": 7.0,
                                         "marker_a": True, "marker_c": False}
    denied = client.get(path, query_string=query, headers=_headers(owner, c))
    assert denied.status_code == 403
    assert denied.get_json() == {"error": "tenant_forbidden", "reason_code": "metrics_tenant_scope_unavailable"}
    assert denied.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", N2_ROUTES)
@pytest.mark.parametrize("query", [
    {"tipo": "unknown"}, {"tipo_ticket": "unknown"},
    {"tipo": "pyme", "tipo_ticket": "municipio"},
])
def test_n2_requested_domain_invalid_or_conflicting_types_are400(client, path, query):
    owner, employee, a, c = _n2_domain_fixture()
    response = client.get(path, query_string=query, headers=_headers(owner, a))
    assert response.status_code == 400
    assert response.get_json()["error"] == "bad_request"
    assert "heatmap" not in response.get_json()



def test_n2_requested_domain_selects_its_structural_validator(client, monkeypatch):
    from routes.estadisticas import _stats_tenant_access_allowed
    from services.tenant_ticket_scope import _is_structurally_municipal_tenant
    owner, profile = _owner_and_tenant("n2-requested-municipal-policy")
    platform = User(name="Synthetic municipal policy platform", email="domain-platform@example.invalid",
                    rol="super_admin", tipo_chat="pyme")
    platform.set_password("synthetic-domain-only")
    db.session.add(platform)
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    # The requested domain selects policy; the selected profile must also
    # satisfy that domain's existing structural validator before exceptions.
    assert _stats_tenant_access_allowed(platform, profile, requested_tipo="pyme", explicit_selection=True) is True
    assert _stats_tenant_access_allowed(platform, profile, requested_tipo="municipio", explicit_selection=True) is False
    municipal_owner_id = owner.id
    profile.pyme_id = None
    profile.municipio_id = municipal_owner_id
    db.session.commit()
    assert (profile.pyme_id, profile.municipio_id, profile.tipo) == (None, municipal_owner_id, "pyme")
    assert _is_structurally_municipal_tenant(profile) is False
    assert _stats_tenant_access_allowed(platform, profile, requested_tipo="municipio", explicit_selection=True) is False
    profile.tipo = "municipio"
    db.session.commit()
    assert _is_structurally_municipal_tenant(profile) is True
    assert _stats_tenant_access_allowed(platform, profile, requested_tipo="municipio", explicit_selection=True) is True
    assert _stats_tenant_access_allowed(platform, profile, requested_tipo="pyme", explicit_selection=True) is False
    assert _stats_tenant_access_allowed(platform, None, requested_tipo="municipio") is False
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "domain-other-platform@example.invalid")
    assert _stats_tenant_access_allowed(platform, profile, requested_tipo="municipio", explicit_selection=True) is False


# Municipal delegated scope contracts. All records are synthetic and offline.
from models import MunicipioTicket
from sqlalchemy import event

MUNICIPAL_STATS_ROUTES = ("/estadisticas/dashboard", "/estadisticas/tickets", "/estadisticas/mapa_calor/datos")
_MUNICIPAL_SCOPE_DENIAL = {"error": "tenant_forbidden", "reason_code": "metrics_tenant_scope_unavailable"}


def _municipal_user(label, role="admin", **fields):
    user = User(name="Synthetic " + label, email=label + "@example.invalid",
                rol=role, tipo_chat="municipio", **fields)
    user.set_password("synthetic-municipal-only")
    db.session.add(user)
    db.session.flush()
    return user


def _municipal_role(name):
    role = Role.query.filter_by(name=name).first()
    if role is None:
        role = Role(name=name)
        db.session.add(role)
        db.session.flush()
    return role


def _municipal_marker(owner, tenant, category, count, lat, lng):
    for _ in range(count):
        db.session.add(MunicipioTicket(
            municipio_id=owner.id, tenant_id=getattr(tenant, "id", None),
            pregunta="Synthetic municipal scope marker", categoria=category,
            distrito="synthetic-district", estado="nuevo", latitud=lat, longitud=lng))


@pytest.fixture
def municipal_scope(client):
    assert str(db.engine.url) == "sqlite:///:memory:"
    owner = _municipal_user("municipal-shared-owner")
    a = TenantProfile(slug="municipal-contract-a", nombre="Synthetic A", tipo="municipio",
                      municipio_id=owner.id, plan="enterprise", is_active=True)
    b = TenantProfile(slug="municipal-contract-b", nombre="Synthetic B", tipo="municipio",
                      municipio_id=owner.id, plan="enterprise", is_active=True)
    db.session.add_all([a, b])
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.municipio_id = a.id, a.slug, owner.id
    admin = _municipal_user("municipal-admin-a", tenant_id=a.id, tenant_slug=a.slug,
                            municipio_id=owner.id)
    employee = _municipal_user("municipal-employee-a", role="empleado",
        tenant_id=a.id, tenant_slug=a.slug, municipio_id=owner.id, empresa_id=None,
        es_empleado=True, ticket_categorias="synthetic-allowed",
        accesibilidad={"employee_scope": {"categorias": ["synthetic-allowed"]}})
    db.session.add_all([
        UserRole(user_id=admin.id, tenant_id=a.id, role_id=_municipal_role("admin").id),
        UserRole(user_id=employee.id, tenant_id=a.id, role_id=_municipal_role("empleado").id)])
    for tenant, category, count, lat, lng in (
        (a, "synthetic-allowed", 7, -33, -60), (a, "synthetic-excluded", 5, -33.2, -60.2),
        (b, "synthetic-allowed", 11, -34, -61), (b, "synthetic-excluded", 6, -34.2, -61.2)):
        _municipal_marker(owner, tenant, category, count, lat, lng)
    db.session.commit()
    from utils.tenant_admin_access import resolve_consistent_user_tenant
    for actor in (admin, employee):
        assert actor.id != owner.id
        assert actor.tenant_id == a.id and actor.tenant_slug == a.slug
        assert resolve_consistent_user_tenant(actor).id == a.id
        assert UserRole.query.filter_by(user_id=actor.id, tenant_id=b.id).count() == 0
    return {"owner": owner, "a": a, "b": b, "admin": admin, "employee": employee}


def _municipal_observed(response):
    body = response.get_json(silent=True) or {}
    points = body.get("heatmap", [])
    return {
        "status": response.status_code,
        "weight": sum(point.get("weight", 0) for point in points),
        "a": any(point.get("location") == {"lat": -33, "lng": -60} for point in points),
        "a_excluded": any(point.get("location") == {"lat": -33.2, "lng": -60.2} for point in points),
        "b": any(point.get("location") == {"lat": -34, "lng": -61} for point in points),
        "b_excluded": any(point.get("location") == {"lat": -34.2, "lng": -61.2} for point in points),
        "privacy": body.get("privacy", {}).get("mode"),
    }


def _municipal_get(client, path, actor, tenant, *, query=None, headers=None):
    return client.get(path, query_string=query or {"tipo": "municipio"},
                      headers=headers or _headers(actor, tenant))


def _municipal_denied(client, path, actor, tenant, *, query=None, headers=None, allow_identity_rejection=False):
    municipal_reads = []
    def record(_conn, _cursor, statement, _params, _context, _many):
        sql = statement.lstrip().lower()
        if sql.startswith("select") and "municipio_ticket" in sql:
            municipal_reads.append(statement)
    event.listen(db.engine, "before_cursor_execute", record)
    try:
        response = _municipal_get(client, path, actor, tenant, query=query, headers=headers)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    accepted_statuses = {401, 403} if allow_identity_rejection else {403}
    assert response.status_code in accepted_statuses, _municipal_observed(response)
    assert municipal_reads == []
    if response.status_code == 403:
        assert response.get_json() == _MUNICIPAL_SCOPE_DENIAL
        assert response.headers["Cache-Control"] == "no-store"
    return response


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
@pytest.mark.parametrize("role", ["admin", "employee"])
def test_municipal_delegated_scope_never_inherits_shared_owner(client, municipal_scope, path, role):
    scope = municipal_scope
    _municipal_denied(client, path, scope[role], scope["b"])


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_exact_actor_and_category_controls(client, municipal_scope, path):
    scope = municipal_scope
    for role in ("admin", "employee"):
        response = _municipal_get(client, path, scope[role], scope["a"])
        observed = _municipal_observed(response)
        assert observed["status"] == 200 and observed["a"] and not observed["b"], observed
        assert not observed["b_excluded"]
        assert observed["weight"] == (12 if role == "admin" else 7), observed
        assert observed["a_excluded"] is (role == "admin")
        if role == "employee":
            assert observed["privacy"] == "employee_aggregated"


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_true_owner_and_platform_explicit_b_controls(client, municipal_scope, path, monkeypatch):
    scope = municipal_scope
    platform = _municipal_user("municipal-platform", role="super_admin")
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    for actor in (scope["owner"], platform):
        response = _municipal_get(client, path, actor, scope["b"])
        observed = _municipal_observed(response)
        assert observed["status"] == 200 and observed["weight"] == 17 and observed["b"], observed
        assert not observed["a"] and not observed["a_excluded"]
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "municipal-other-platform@example.invalid")
    denied = _municipal_get(client, path, platform, scope["b"])
    assert denied.status_code == 401


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_exact_grants_revoke_and_do_not_promote_employee(client, municipal_scope, path):
    scope = municipal_scope
    for role_name in ("admin", "analytics_viewer"):
        role = _municipal_role(role_name)
        grant = UserRole(user_id=scope["admin"].id, tenant_id=scope["b"].id, role_id=role.id)
        db.session.add(grant)
        db.session.commit()
        response = _municipal_get(client, path, scope["admin"], scope["b"])
        observed = _municipal_observed(response)
        assert observed["status"] == 200 and observed["b"] and observed["weight"] == 17, observed
        assert not observed["a"] and not observed["a_excluded"]
        db.session.delete(grant)
        db.session.commit()
        _municipal_denied(client, path, scope["admin"], scope["b"])
        employee_grant = UserRole(user_id=scope["employee"].id, tenant_id=scope["b"].id, role_id=role.id)
        db.session.add(employee_grant)
        db.session.commit()
        _municipal_denied(client, path, scope["employee"], scope["b"])
        db.session.delete(employee_grant)
        db.session.commit()


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_unique_legacy_and_ambiguous_delegates(client, municipal_scope, path):
    scope = municipal_scope
    owner = _municipal_user("municipal-unique-owner")
    tenant = TenantProfile(slug="municipal-unique-profile", nombre="Synthetic unique",
                           tipo="municipio", municipio_id=owner.id, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
    legacy = _municipal_user("municipal-legacy-delegate", municipio_id=owner.id)
    _municipal_marker(owner, tenant, "synthetic-allowed", 7, -35, -62)
    _municipal_marker(owner, None, "synthetic-allowed", 5, -35.2, -62.2)
    db.session.commit()
    response = _municipal_get(client, path, legacy, tenant)
    assert response.status_code == 200 and _municipal_observed(response)["weight"] == 12
    other = TenantProfile(slug="municipal-legacy-ambiguous", nombre="Synthetic ambiguous",
                          tipo="municipio", municipio_id=owner.id, is_active=True)
    db.session.add(other)
    db.session.commit()
    _municipal_denied(client, path, legacy, tenant)
    _municipal_denied(client, path, legacy, other)
    response = _municipal_get(client, path, owner, tenant)
    assert response.status_code == 200 and _municipal_observed(response)["weight"] == 7
    # A company reference to a multi-profile owner is not a credited exact identity.
    company_delegate = _municipal_user("municipal-company-delegate", tenant_id=scope["a"].id,
        tenant_slug=scope["a"].slug, municipio_id=scope["owner"].id, empresa_id=scope["owner"].id)
    db.session.commit()
    _municipal_denied(client, path, company_delegate, scope["b"], allow_identity_rejection=True)


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_selector_denials_are_opaque_before_sources(client, municipal_scope, path, monkeypatch):
    scope = municipal_scope
    platform = _municipal_user("municipal-selector-platform", role="super_admin")
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    for actor in (scope["admin"], scope["owner"], platform):
        _municipal_denied(client, path, actor, scope["a"],
            query={"tipo": "municipio", "tenant_slug": scope["b"].slug})
        unknown_headers = _headers(actor, scope["a"])
        unknown_headers["X-Tenant-Slug"] = "municipal-missing-profile"
        _municipal_denied(client, path, actor, scope["a"], headers=unknown_headers)
    for actor in (scope["owner"], platform):
        no_hint = _headers(actor, scope["a"])
        no_hint.pop("X-Tenant-Slug")
        _municipal_denied(client, path, actor, scope["a"], headers=no_hint)


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_outer_boundary_still_denies_unrelated_grant(client, municipal_scope, path):
    scope = municipal_scope
    outsider = _municipal_user("municipal-unrelated-owner")
    tenant = TenantProfile(slug="municipal-unrelated-profile", nombre="Synthetic unrelated",
                           tipo="municipio", municipio_id=outsider.id, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    outsider.tenant_id, outsider.tenant_slug = tenant.id, tenant.slug
    db.session.add(UserRole(user_id=scope["admin"].id, tenant_id=tenant.id,
                           role_id=_municipal_role("admin").id))
    db.session.commit()
    _municipal_denied(client, path, scope["admin"], tenant)

# V2 service/domain contracts, separate from the immutable V1 execution records.
from models import SugerenciaCiudadano
from services.tenant_ticket_scope import TicketTenantScopeError


def _municipal_v2_unique(suggestion_count=0):
    owner = _municipal_user("municipal-v2-unique-owner")
    tenant = TenantProfile(slug="municipal-v2-unique", nombre="Synthetic unique",
                           tipo="municipio", municipio_id=owner.id, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
    _municipal_marker(owner, tenant, "synthetic-allowed", 2, -36, -63)
    _municipal_marker(owner, None, "synthetic-allowed", 3, -36, -63)
    for _ in range(suggestion_count):
        db.session.add(SugerenciaCiudadano(municipio_id=owner.id,
            texto_sugerencia="Synthetic uniquely attributable suggestion", categoria="synthetic-unique"))
    db.session.commit()
    return owner, tenant


def _municipal_v2_request_with_suggestion_reads(client, path, actor, tenant):
    reads = []
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if "select" in statement.lower() and "sugerencia_ciudadano" in statement.lower():
            reads.append(statement)
    event.listen(db.engine, "before_cursor_execute", record)
    try:
        response = _municipal_get(client, path, actor, tenant)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    return response, reads


def test_municipal_v2_explicit_service_uses_selected_shared_owner_profile(client, municipal_scope):
    from services.municipal_stats import build_stats_for_municipio
    scope = municipal_scope
    stats = build_stats_for_municipio(scope["owner"].id, tenant_id=scope["b"].id,
                                     actor=scope["owner"])
    assert stats["resumen"]["total"] == 17
    assert sum(row["total"] for row in stats["por_categoria"]) == 17


@pytest.mark.parametrize("invalid", ["zero", "negative", "boolean", "string", "missing", "owner_mismatch", "inactive"])
def test_municipal_v2_explicit_invalid_scope_never_falls_back_or_zero(client, municipal_scope, invalid):
    from services.municipal_stats import build_stats_for_municipio
    scope = municipal_scope
    tenant_id, owner_id = scope["b"].id, scope["owner"].id
    if invalid == "zero":
        tenant_id = 0
    elif invalid == "negative":
        tenant_id = -1
    elif invalid == "boolean":
        tenant_id = True
    elif invalid == "string":
        tenant_id = str(tenant_id)
    elif invalid == "missing":
        tenant_id = 1000000000
        assert db.session.get(TenantProfile, tenant_id) is None
    elif invalid == "owner_mismatch":
        owner_id = _municipal_user("municipal-v2-other-owner").id
        db.session.commit()
    else:
        scope["b"].is_active = False
        db.session.commit()
    with pytest.raises(TicketTenantScopeError):
        build_stats_for_municipio(owner_id, tenant_id=tenant_id, actor=scope["owner"])


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_v2_shared_suggestions_are_unattributable_without_select(client, municipal_scope, path):
    scope = municipal_scope
    for _ in range(4):
        db.session.add(SugerenciaCiudadano(municipio_id=scope["owner"].id,
            texto_sugerencia="Synthetic ambiguous-owner suggestion", categoria="synthetic-unattributable"))
    db.session.commit()
    response, reads = _municipal_v2_request_with_suggestion_reads(client, path, scope["owner"], scope["b"])
    assert response.status_code == 200
    suggestions = response.get_json()["stats"]["sugerencias"]
    assert suggestions["total"] is None
    assert suggestions["available"] is False
    assert suggestions["reason_code"] == "legacy_owner_scope_unattributable"
    assert suggestions["por_estado"] == [] and suggestions["por_categoria"] == []
    assert suggestions["tendencia_mensual"] == []
    assert reads == []
    assert response.get_json()["summary"]["total"] == 17


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
@pytest.mark.parametrize("count", [0, 2])
def test_municipal_v2_unique_suggestions_are_observed_counts(client, path, count):
    owner, tenant = _municipal_v2_unique(count)
    response, reads = _municipal_v2_request_with_suggestion_reads(client, path, owner, tenant)
    assert response.status_code == 200
    suggestions = response.get_json()["stats"]["sugerencias"]
    assert suggestions["total"] == count
    assert suggestions["available"] is True and suggestions["reason_code"] is None
    assert len(reads) == 4
    assert response.get_json()["summary"]["total"] == 5


def test_municipal_v2_no_id_keeps_unique_and_ambiguous_legacy_contract(client, municipal_scope):
    from services.municipal_stats import build_stats_for_municipio
    owner, tenant = _municipal_v2_unique(2)
    stats = build_stats_for_municipio(owner.id, actor=owner)
    assert stats["resumen"]["total"] == 5 and stats["sugerencias"]["total"] == 2
    assert set(stats["sugerencias"]) == {"total", "por_estado", "por_categoria", "tendencia_mensual"}
    shared = municipal_scope["owner"]
    legacy = build_stats_for_municipio(shared.id, actor=shared)
    assert legacy["resumen"]["total"] == 0
    assert set(legacy["sugerencias"]) == {"total", "por_estado", "por_categoria", "tendencia_mensual"}


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
def test_municipal_v2_inactive_target_is_denied_before_platform(client, municipal_scope, path, monkeypatch):
    scope = municipal_scope
    platform = _municipal_user("municipal-v2-inactive-platform", role="super_admin",
                               municipio_id=scope["owner"].id)
    scope["b"].is_active = False
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    _municipal_denied(client, path, platform, scope["b"])


@pytest.fixture
def municipal_structural_scope(client, monkeypatch):
    from services.tenant_ticket_scope import _is_structurally_municipal_tenant
    assert str(db.engine.url) == "sqlite:///:memory:"
    owner = _municipal_user("municipal-structural-owner")
    good_owner = _municipal_user("municipal-structural-good-owner")
    invalid = TenantProfile(slug="municipal-structural-invalid", nombre="Synthetic invalid municipal structure",
        tipo="pyme", municipio_id=owner.id, pyme_id=None, is_active=True, plan="enterprise")
    valid = TenantProfile(slug="municipal-structural-valid", nombre="Synthetic valid municipal structure",
        tipo="municipio", municipio_id=good_owner.id, pyme_id=None, is_active=True, plan="enterprise")
    db.session.add_all([invalid, valid])
    db.session.flush()
    for actor, tenant in ((owner, invalid), (good_owner, valid)):
        actor.tenant_id, actor.tenant_slug, actor.municipio_id = tenant.id, tenant.slug, actor.id
    _municipal_marker(owner, invalid, "synthetic-structural-invalid", 4, -34, -60)
    _municipal_marker(good_owner, valid, "synthetic-structural-valid", 3, -33, -60)
    for actor in (owner, good_owner):
        db.session.add(SugerenciaCiudadano(municipio_id=actor.id,
            texto_sugerencia="Synthetic structural suggestion", categoria="synthetic-structural"))
    platform = _municipal_user("municipal-structural-platform", role="super_admin")
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
    assert (invalid.tipo, invalid.pyme_id, invalid.municipio_id) == ("pyme", None, owner.id)
    assert _is_structurally_municipal_tenant(invalid) is False
    assert _is_structurally_municipal_tenant(valid) is True
    assert MunicipioTicket.query.filter_by(tenant_id=invalid.id).count() == 4
    return {"owner": owner, "good_owner": good_owner, "invalid": invalid, "valid": valid, "platform": platform}


@pytest.mark.parametrize("path", MUNICIPAL_STATS_ROUTES)
@pytest.mark.parametrize("actor_name", ["owner", "platform"])
def test_municipal_v3_structure_rejected_before_owner_or_platform(client, municipal_structural_scope, path, actor_name):
    scope = municipal_structural_scope
    valid_actor = scope["good_owner"] if actor_name == "owner" else scope["platform"]
    control = _municipal_get(client, path, valid_actor, scope["valid"])
    assert control.status_code == 200
    body = control.get_json()
    assert body["summary"]["total"] == 3
    assert sum(point["weight"] for point in body["heatmap"]) == 3
    assert body["stats"]["sugerencias"]["total"] == 1
    assert body["stats"]["sugerencias"]["available"] is True
    reads = []
    def collect(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lstrip().lower()
        if sql.startswith("select") and any(table in sql for table in ("municipio_ticket", "sugerencia_ciudadano")):
            reads.append(statement)
    event.listen(db.engine, "before_cursor_execute", collect)
    try:
        response = _municipal_get(client, path, scope[actor_name], scope["invalid"])
    finally:
        event.remove(db.engine, "before_cursor_execute", collect)
    assert response.status_code == 403
    assert response.get_json() == _MUNICIPAL_SCOPE_DENIAL
    assert response.headers["Cache-Control"] == "no-store"
    assert reads == []


def test_municipal_v3_explicit_service_rejects_nonmunicipal_structure(municipal_structural_scope):
    from services.municipal_stats import build_stats_for_municipio
    scope = municipal_structural_scope
    reads = []
    def collect(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lstrip().lower()
        if sql.startswith("select") and any(table in sql for table in ("municipio_ticket", "sugerencia_ciudadano")):
            reads.append(statement)
    event.listen(db.engine, "before_cursor_execute", collect)
    try:
        with pytest.raises(TicketTenantScopeError):
            build_stats_for_municipio(scope["owner"].id, tenant_id=scope["invalid"].id, actor=scope["owner"])
    finally:
        event.remove(db.engine, "before_cursor_execute", collect)
    assert reads == []


def test_municipal_v3_explicit_service_valid_structure_control(municipal_structural_scope):
    from services.municipal_stats import build_stats_for_municipio
    scope = municipal_structural_scope
    stats = build_stats_for_municipio(scope["good_owner"].id, tenant_id=scope["valid"].id, actor=scope["good_owner"])
    assert stats["resumen"]["total"] == 3
    assert stats["sugerencias"]["total"] == 1
    assert stats["sugerencias"]["available"] is True
