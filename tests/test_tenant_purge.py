from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import Session

from services.tenant_purge import purge_tenant


# --- A small schema with enforced foreign keys ---------------------------------

@pytest.fixture
def schema():
    metadata = MetaData()
    users = Table(
        "user", metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, ForeignKey("tenant_profile.id", use_alter=True), nullable=True),
        Column("tenant_slug", String(80), nullable=True),
        Column("pyme_id", Integer, nullable=True),
        Column("municipio_id", Integer, nullable=True),
        Column("empresa_id", Integer, ForeignKey("user.id"), nullable=True),
    )
    tenants = Table(
        "tenant_profile", metadata,
        Column("id", Integer, primary_key=True),
        Column("slug", String(80), nullable=False),
        Column("municipio_id", Integer, ForeignKey("user.id"), nullable=True),
        Column("pyme_id", Integer, ForeignKey("user.id"), nullable=True),
    )
    orders = Table(
        "orders", metadata,
        Column("id", Integer, primary_key=True),
        Column("pyme_id", Integer, ForeignKey("user.id"), nullable=False),
        Column("user_id", Integer, ForeignKey("user.id"), nullable=True),
    )
    order_items = Table(
        "order_item", metadata,
        Column("id", Integer, primary_key=True),
        Column("order_id", Integer, ForeignKey("orders.id"), nullable=False),
    )
    tickets = Table(
        "ticket", metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, ForeignKey("tenant_profile.id"), nullable=False),
        Column("assigned_to", Integer, ForeignKey("user.id"), nullable=True),
    )
    metrics = Table(
        "analytics_metric", metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", String(64), nullable=False),   # text: id or slug
    )
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    # Declared by the models, never migrated into this database.
    never_migrated = Table(
        "never_migrated", metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, ForeignKey("tenant_profile.id"), nullable=False),
        Column("user_id", Integer, ForeignKey("user.id"), nullable=True),
    )
    metadata.create_all(engine, tables=[t for t in metadata.sorted_tables if t is not never_migrated])
    session = Session(engine)
    yield type("Schema", (), dict(
        metadata=metadata, users=users, tenants=tenants, orders=orders,
        order_items=order_items, tickets=tickets, metrics=metrics, session=session,
    ))
    session.close()


def _user(id, **values):
    return {"id": id, "tenant_slug": None, "pyme_id": None, **values}


def _order(id, pyme_id, user_id=None):
    return {"id": id, "pyme_id": pyme_id, "user_id": user_id}


class _Tenant:
    def __init__(self, id, slug, municipio_id=None, pyme_id=None):
        self.id, self.slug, self.municipio_id, self.pyme_id = id, slug, municipio_id, pyme_id


def _seed_crossed(schema):
    """Target tenant id 10 equals the *owner user id* of the bystander tenant."""
    s = schema.session
    s.execute(schema.users.insert(), [
        _user(2, tenant_slug="target"),                       # target owner
        _user(3, pyme_id=2),                                  # target employee
        _user(10, tenant_slug="bystander"),                   # bystander owner
        _user(11, pyme_id=10, tenant_slug="bystander"),       # bystander employee
        _user(99),                                            # platform operator
    ])
    s.execute(schema.tenants.insert(), [
        {"id": 10, "slug": "target", "pyme_id": 2},
        {"id": 2, "slug": "bystander", "pyme_id": 10},
    ])
    s.execute(schema.users.update().where(schema.users.c.id == 2).values(tenant_id=10))
    s.execute(schema.users.update().where(schema.users.c.id.in_([10, 11])).values(tenant_id=2))
    s.execute(schema.orders.insert(), [
        _order(1, 2, 3),      # target's
        _order(2, 10, 11),    # bystander's; the old purge deleted it
    ])
    s.execute(schema.order_items.insert(), [{"id": 1, "order_id": 1}, {"id": 2, "order_id": 2}])
    s.execute(schema.tickets.insert(), [
        {"id": 1, "tenant_id": 10, "assigned_to": 3},
        {"id": 2, "tenant_id": 2, "assigned_to": 3},   # bystander ticket, target assignee
    ])
    s.execute(schema.metrics.insert(), [
        {"id": 1, "tenant_id": "10"}, {"id": 2, "tenant_id": "target"},
        {"id": 3, "tenant_id": "2"}, {"id": 4, "tenant_id": "bystander"},
    ])
    s.commit()
    return _Tenant(10, "target", pyme_id=2)


def _ids(schema, table):
    return sorted(schema.session.execute(select(table.c.id)).scalars())


def test_purge_scopes_by_owner_user_and_never_by_tenant_number(schema):
    target = _seed_crossed(schema)

    summary = purge_tenant(
        schema.session, schema.metadata, schema.tenants, schema.users, target,
        purge_users=True, protected_user_ids=[99],
    )
    schema.session.commit()

    assert _ids(schema, schema.tenants) == [2]
    assert _ids(schema, schema.users) == [10, 11, 99]
    # The bystander's order carries pyme_id == 10, the purged tenant's number.
    assert _ids(schema, schema.orders) == [2]
    assert _ids(schema, schema.order_items) == [2]
    assert _ids(schema, schema.tickets) == [2]
    kept_ticket = schema.session.execute(select(schema.tickets.c.assigned_to)).scalar()
    assert kept_ticket is None
    assert summary["owner_ids"] == [2]
    assert summary["user_ids"] == [2, 3]
    # A text tenant column is matched by number and by slug, never coerced.
    assert _ids(schema, schema.metrics) == [3, 4]
    assert summary["deleted"] == {
        "analytics_metric": 2, "order_item": 1, "orders": 1,
        "tenant_profile": 1, "ticket": 1, "user": 2,
    }
    assert summary["detached"] == {"ticket.assigned_to": 1}
    # A declared table this database lacks is reported, not fatal.
    assert list(summary["skipped_unreadable_tables"]) == ["never_migrated"]


def test_rolling_back_the_same_purge_is_an_exact_preview(schema):
    target = _seed_crossed(schema)

    summary = purge_tenant(
        schema.session, schema.metadata, schema.tenants, schema.users, target,
        purge_users=True,
    )
    schema.session.rollback()

    assert summary["deleted"]["user"] == 2
    assert _ids(schema, schema.tenants) == [2, 10]
    assert _ids(schema, schema.users) == [2, 3, 10, 11, 99]
    assert _ids(schema, schema.orders) == [1, 2]


def test_accounts_survive_detached_when_users_are_not_purged(schema):
    target = _seed_crossed(schema)

    summary = purge_tenant(
        schema.session, schema.metadata, schema.tenants, schema.users, target,
        purge_users=False,
    )
    schema.session.commit()

    assert _ids(schema, schema.tenants) == [2]
    assert _ids(schema, schema.users) == [2, 3, 10, 11, 99]
    owner = schema.session.execute(
        select(schema.users.c.tenant_id, schema.users.c.tenant_slug).where(schema.users.c.id == 2)
    ).one()
    assert tuple(owner) == (None, None)
    assert summary["user_ids"] == []
    assert summary["user_ids_kept"] == [2, 3]


def test_an_owner_shared_with_another_tenant_is_kept_with_its_rows(schema):
    s = schema.session
    s.execute(schema.users.insert(), [_user(5), _user(6, tenant_slug="second")])
    s.execute(schema.tenants.insert(), [
        {"id": 20, "slug": "first", "pyme_id": 5},
        {"id": 21, "slug": "second", "pyme_id": 5},
    ])
    s.execute(schema.orders.insert(), [_order(7, 5)])
    s.commit()

    summary = purge_tenant(
        s, schema.metadata, schema.tenants, schema.users,
        _Tenant(21, "second", pyme_id=5), purge_users=True,
    )
    s.commit()

    assert _ids(schema, schema.tenants) == [20]
    assert _ids(schema, schema.users) == [5]
    assert _ids(schema, schema.orders) == [7]
    assert summary["shared_owner_ids_kept"] == [5]
    assert summary["owner_ids"] == []
    assert summary["user_ids"] == [6]


def test_protected_operator_listed_under_the_slug_is_never_deleted(schema):
    s = schema.session
    s.execute(schema.users.insert(), [_user(7), _user(99, tenant_slug="solo")])
    s.execute(schema.tenants.insert(), [{"id": 30, "slug": "solo", "pyme_id": 7}])
    s.commit()

    summary = purge_tenant(
        s, schema.metadata, schema.tenants, schema.users,
        _Tenant(30, "solo", pyme_id=7), purge_users=True, protected_user_ids=[99],
    )
    s.commit()

    assert _ids(schema, schema.users) == [99]
    assert summary["user_ids"] == [7]
    assert summary["user_ids_kept"] == [99]


# --- The real endpoint on the real schema --------------------------------------

def _token(user):
    from services.auth_session_lifecycle import issue_token
    from utils.auth_helpers import auth_session_version

    now = datetime.now(timezone.utc)
    metadata = dict(user.accesibilidad or {})
    auth = dict(metadata.get("auth") or {})
    auth.update(provider="clerk", session_version=auth_session_version(user),
                clerk={"user_id": f"synthetic_tenant_purge_{user.id}"})
    metadata["auth"] = auth
    user.accesibilidad = metadata
    return issue_token({
        "user_id": user.id, "exp": now + timedelta(hours=1), "rol": user.rol,
        "auth_provider": "clerk", "session_kind": "clerk", "sid": "sess_tenant_purge",
        "clerk_sid": "sess_tenant_purge", "jti": f"jti_tenant_purge_{user.id}",
        "sv": auth_session_version(user), "iat": now,
    })


@pytest.fixture
def crossed(client):
    from models import CatalogMapping, CatalogoItem, PymePedido, TenantProfile, User, db

    operator = User(email="operator@purge.test", name="Operator", rol="super_admin")
    bystander_owner = User(email="owner@bystander.test", name="Bystander", rol="admin", tipo_chat="pyme")
    target_owner = User(email="owner@target.test", name="Target", rol="admin", tipo_chat="pyme")
    for user in (operator, bystander_owner, target_owner):
        user.set_password("irrelevant")
    db.session.add_all([operator, bystander_owner, target_owner])
    db.session.flush()
    bystander_owner.pyme_id = bystander_owner.id
    target_owner.pyme_id = target_owner.id
    # The target tenant's number is the bystander owner's user id.
    target = TenantProfile(id=bystander_owner.id, slug="purge-target", nombre="Target",
                           tipo="pyme", pyme_id=target_owner.id, is_active=False)
    bystander = TenantProfile(id=bystander_owner.id + 500, slug="purge-bystander", nombre="Bystander",
                              tipo="pyme", pyme_id=bystander_owner.id, is_active=True)
    db.session.add_all([target, bystander])
    db.session.flush()
    target_owner.tenant_id, target_owner.tenant_slug = target.id, target.slug
    bystander_owner.tenant_id, bystander_owner.tenant_slug = bystander.id, bystander.slug
    db.session.add_all([
        PymePedido(pyme_id=bystander_owner.id, asunto="BYSTANDER-1", detalles="[]"),
        PymePedido(pyme_id=target_owner.id, asunto="TARGET-1", detalles="[]"),
        CatalogMapping(pyme_id=bystander_owner.id, name="bystander", mapping={}),
        CatalogoItem(user_id=bystander_owner.id, tenant_id=bystander.id, nombre="Bystander item"),
        CatalogoItem(user_id=target_owner.id, tenant_id=target.id, nombre="Target item"),
    ])
    db.session.commit()
    headers = {"Authorization": f"Bearer {_token(operator)}"}
    db.session.commit()
    return type("Crossed", (), dict(
        headers=headers, client=client, operator_id=operator.id,
        bystander_owner_id=bystander_owner.id, target_owner_id=target_owner.id,
    ))


def _state():
    from models import CatalogMapping, CatalogoItem, PymePedido, TenantProfile, User

    return {
        "tenants": sorted(t.slug for t in TenantProfile.query.all()),
        "users": sorted(u.email for u in User.query.all()),
        "orders": sorted(p.asunto for p in PymePedido.query.all()),
        "mappings": CatalogMapping.query.count(),
        "items": sorted(i.nombre for i in CatalogoItem.query.all()),
    }


def test_endpoint_requires_confirmation_or_dry_run(crossed):
    response = crossed.client.delete(
        "/api/admin/tenants/purge-target/purge", json={}, headers=crossed.headers)
    assert response.status_code == 400


def test_endpoint_refuses_a_tenant_that_is_still_active(crossed):
    before = _state()
    response = crossed.client.delete(
        "/api/admin/tenants/purge-bystander/purge", json={"confirm": True}, headers=crossed.headers)
    assert response.status_code == 409
    assert response.get_json()["reason_code"] == "tenant_purge_requires_inactive_tenant"
    assert _state() == before


def test_endpoint_dry_run_reports_and_changes_nothing(crossed):
    from models import db

    before = _state()
    response = crossed.client.delete(
        "/api/admin/tenants/purge-target/purge", json={"dry_run": True}, headers=crossed.headers)
    body = response.get_json()
    db.session.expire_all()

    assert response.status_code == 200
    assert body["dry_run"] is True and body["committed"] is False
    assert body["owner_ids"] == [crossed.target_owner_id]
    assert body["user_ids"] == [crossed.target_owner_id]
    assert body["deleted"]["tenant_profile"] == 1
    assert _state() == before


def test_endpoint_purges_the_target_and_leaves_the_crossed_tenant_whole(crossed):
    from models import db

    response = crossed.client.delete(
        "/api/admin/tenants/purge-target/purge", json={"confirm": True}, headers=crossed.headers)
    body = response.get_json()
    db.session.expire_all()

    assert response.status_code == 200, body
    assert body["committed"] is True
    assert _state() == {
        "tenants": ["purge-bystander"],
        "users": ["operator@purge.test", "owner@bystander.test"],
        "orders": ["BYSTANDER-1"],
        "mappings": 1,
        "items": ["Bystander item"],
    }


def test_deactivating_revokes_only_the_tenants_own_sessions(crossed):
    from models import TenantProfile, User, db
    from routes.super_admin import _tenant_related_users

    target = TenantProfile.query.filter_by(slug="purge-target").one()
    related = {user.id for user in _tenant_related_users(target)}
    db.session.rollback()

    assert related == {crossed.target_owner_id}
    # Before the fix the bystander owner matched ``User.pyme_id == tenant.id``.
    assert crossed.bystander_owner_id not in related
    assert User.query.count() == 3
