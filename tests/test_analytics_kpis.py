from unittest.mock import patch

import pytest

from database import db
from models import TenantProfile, User
from utils.auth_helpers import generar_token


def _create_actor(*, email: str, role: str, tenant: TenantProfile, owner: User) -> tuple[User, str]:
    actor = User(
        name=email.split("@", 1)[0],
        email=email,
        rol=role,
        tipo_chat="pyme",
        tenant_id=tenant.id,
        empresa_id=owner.id if role == "empleado" else None,
    )
    actor.set_password("safe-password")
    db.session.add(actor)
    db.session.commit()
    token = generar_token(
        actor.id,
        actor.rol,
        actor.tipo_chat,
        municipio_id=None,
        pyme_id=owner.id,
    )
    return actor, token


@pytest.fixture
def analytics_tenant(client):
    owner = User(
        name="Analytics owner",
        email="analytics-owner@test.com",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("safe-password")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="analytics-tenant",
        nombre="Analytics tenant",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    db.session.commit()
    return owner, tenant


@pytest.mark.parametrize("role", ["admin", "empleado"])
def test_operational_roles_can_read_own_tenant_kpis(client, analytics_tenant, role):
    owner, tenant = analytics_tenant
    _actor, token = _create_actor(
        email=f"{role}@analytics.test",
        role=role,
        tenant=tenant,
        owner=owner,
    )

    with patch(
        "routes.analytics_kpis.analytics_kpi_service.get_operational_metrics",
        return_value={"chat": {"handoff_rate_percent": 15.5}},
    ) as get_metrics:
        response = client.get(
            f"/api/analytics/kpis?tenant_id={tenant.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.get_json()["chat"]["handoff_rate_percent"] == 15.5
    get_metrics.assert_called_once_with(tenant.id, 30)


@pytest.mark.parametrize("role", ["usuario", "lead"])
@pytest.mark.parametrize("endpoint", ["kpis", "costs"])
def test_customer_roles_cannot_read_tenant_analytics(
    client, analytics_tenant, role, endpoint
):
    owner, tenant = analytics_tenant
    _actor, token = _create_actor(
        email=f"{role}-{endpoint}@analytics.test",
        role=role,
        tenant=tenant,
        owner=owner,
    )

    service_method = (
        "get_operational_metrics" if endpoint == "kpis" else "get_cost_metrics"
    )
    with patch(
        f"routes.analytics_kpis.analytics_kpi_service.{service_method}"
    ) as analytics_call:
        response = client.get(
            f"/api/analytics/{endpoint}?tenant_id={tenant.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    analytics_call.assert_not_called()


def test_costs_require_tenant_id(client, analytics_tenant):
    owner, tenant = analytics_tenant
    _actor, token = _create_actor(
        email="cost-admin@analytics.test",
        role="admin",
        tenant=tenant,
        owner=owner,
    )

    response = client.get(
        "/api/analytics/costs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
