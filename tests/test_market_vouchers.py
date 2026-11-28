"""Vouchers: issued on a sent WhatsApp order, shown by code, checked once by the venue."""
from __future__ import annotations

import pytest

from app import db
from models import CatalogoItem, IntegrationEvent, MarketOrder, MarketOrderItem
from services import market_vouchers as vouchers
from tests.test_integration_fixes import _create_admin_tenant, app, client  # noqa: F401


def _order(tenant, owner):
    order = MarketOrder(tenant_id=tenant.id, status="pending", channel="whatsapp", currency="ARS",
                        contact_key="wa_flow:x")
    db.session.add(order); db.session.flush()
    db.session.add_all([
        MarketOrderItem(order_id=order.id, product_id=None, quantity=1, price_points=1500, modalidad="canje",
                        name_snapshot="Kit escolar completo"),
        MarketOrderItem(order_id=order.id, product_id=None, quantity=2, modalidad="inscripcion",
                        name_snapshot="Charla de crianza"),
        MarketOrderItem(order_id=order.id, product_id=None, quantity=1, modalidad="donacion",
                        name_snapshot="Donar útiles"),
    ])
    db.session.flush(); db.session.refresh(order)
    return order


@pytest.mark.parametrize("raw,expected", [
    ("k7mq-4xpz-2r", "K7MQ4XPZ2R"), ("K7MQ 4XPZ 2R", "K7MQ4XPZ2R"), ("K7MQ4XPZ2", None), ("O7MQ4XPZ2R", None),
])
def test_codes_are_normalized_and_unambiguous(raw, expected):
    assert vouchers.normalize(raw) == expected
    assert all(c not in vouchers.new_code() for c in "01OIL")


def test_vouchers_are_issued_shown_and_used_once(client):  # noqa: F811
    owner, tenant, headers = _create_admin_tenant(plan="free")
    issued = vouchers.issue_for_order(_order(tenant, owner))
    db.session.commit()
    assert [item["name"] for item in issued] == ["Kit escolar completo", "Charla de crianza"]
    code = issued[0]["code"]
    stored = IntegrationEvent.query.filter_by(provider=vouchers.PROVIDER).all()
    assert len(stored) == 2 and all(code not in str(row.event_id) + str(row.payload) for row in stored)

    page = client.get(f"/api/public/vouchers/{issued[0]['display'].lower()}").get_json()
    assert page["status"] == "valid" and page["name"] == "Kit escolar completo" and page["points"] == 1500
    assert client.get("/api/public/vouchers/K7MQ4XPZ2R").status_code == 404

    first = client.post(f"/api/admin/tenants/{tenant.slug}/vouchers/redeem", json={"code": code}, headers=headers)
    assert first.status_code == 200 and first.get_json()["result"] == "redeemed"
    again = client.post(f"/api/admin/tenants/{tenant.slug}/vouchers/redeem", json={"code": code}, headers=headers)
    assert again.status_code == 409 and again.get_json()["voucher"]["used_at"]
    assert client.get(f"/api/public/vouchers/{code}").get_json()["status"] == "used"
    bad = client.post(f"/api/admin/tenants/{tenant.slug}/vouchers/redeem", json={"code": "nope"}, headers=headers)
    assert bad.status_code == 404


def test_another_organization_cannot_redeem_my_voucher(client):  # noqa: F811
    from models import TenantProfile
    owner, tenant, _ = _create_admin_tenant(plan="free")
    code = vouchers.issue_for_order(_order(tenant, owner))[0]["code"]
    other = TenantProfile(slug="otra-org", nombre="Otra", tipo="pyme", pyme_id=owner.id)
    db.session.add(other); db.session.commit()
    assert vouchers.redeem(other, code, staff_user_id=owner.id) == ("invalid", None)
    assert vouchers.redeem(tenant, code, staff_user_id=owner.id)[0] == "redeemed"
