from __future__ import annotations

from models import CatalogoItem, ChatSessionContext, MunicipioTicket, Rubro, TenantProfile, User, db
from services.demo_municipio_runtime import _tenant_for_owner, handle_demo_municipio_message
from tests.auth_test_utils import clerk_superadmin_headers


def _user(*, email: str, role: str = "admin", tipo_chat: str = "municipio") -> User:
    user = User(
        name="Fixture",
        email=email,
        rol=role,
        tipo_chat=tipo_chat,
        password_hash="fixture",
    )
    db.session.add(user)
    db.session.flush()
    return user


def _tenant(
    *,
    slug: str,
    owner: User,
    demo: bool,
) -> TenantProfile:
    tenant = TenantProfile(
        slug=slug,
        nombre=slug,
        tipo="municipio",
        municipio_id=owner.id,
        configuracion={"demo_mode": True} if demo else {},
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def test_verified_server_demo_tenant_recovers_owner_mismatch(app, client, monkeypatch):
    monkeypatch.setitem(
        app.config,
        "DEMO_RUBROS",
        [{"key": "municipio", "tenant_slug": "municipio", "user_id": 999999}],
    )
    legacy_owner = _user(email="legacy-demo-owner@example.invalid")
    selected_demo_owner = _user(email="selected-demo-owner@example.invalid")
    demo_tenant = _tenant(slug="municipio", owner=legacy_owner, demo=True)
    context = ChatSessionContext(
        chat_session_id="demo-owner-mismatch",
        anon_id="anon-demo-owner-mismatch",
        context_data={},
    )
    db.session.add(context)
    db.session.commit()

    failed = handle_demo_municipio_message(
        question="Quiero iniciar un reclamo por alumbrado publico.",
        owner_user=selected_demo_owner,
        chat_db_context=context,
        anon_id="anon-demo-owner-mismatch",
        chat_session_id="demo-owner-mismatch",
    )
    assert failed["accion_backend"] == "demo_tenant_scope_unavailable"
    assert MunicipioTicket.query.count() == 0

    recovered = handle_demo_municipio_message(
        question="Quiero iniciar un reclamo por alumbrado publico.",
        owner_user=selected_demo_owner,
        verified_demo_tenant=demo_tenant,
        chat_db_context=context,
        anon_id="anon-demo-owner-mismatch",
        chat_session_id="demo-owner-mismatch",
        demo_session_payload={"tenant_slug": "municipio", "kind": "demo_session"},
    )

    assert recovered["fuente"] == "demo_municipio_runtime"
    assert recovered["accion_backend"] == "demo_crear_reclamo"
    ticket = MunicipioTicket.query.one()
    assert ticket.tenant_id == demo_tenant.id
    assert ticket.municipio_id == legacy_owner.id
    assert ticket.user_id == legacy_owner.id
    assert ticket.categoria == "Alumbrado publico"
    assert "Alumbrado público" in recovered["message_body"]


def test_public_chat_uses_signed_demo_tenant_when_registry_owner_is_misaligned(
    app, client, monkeypatch
):
    rubro = Rubro(clave="municipio", nombre="Gobiernos y municipios")
    db.session.add(rubro)
    db.session.flush()
    tenant_owner = _user(email="tenant-owner-chat@example.invalid")
    registry_owner = _user(email="registry-owner-chat@example.invalid")
    registry_owner.rubro_id = rubro.id
    demo_tenant = _tenant(slug="municipio", owner=tenant_owner, demo=True)
    db.session.commit()
    monkeypatch.setitem(
        app.config,
        "DEMO_RUBROS",
        [
            {
                "key": "municipio",
                "tenant_slug": "municipio",
                "nombre": "Gobiernos y municipios",
                "tipo_chat": "municipio",
                "user_id": registry_owner.id,
                "rubro_id": rubro.id,
            }
        ],
    )

    from routes.v2.tenants import create_demo_session_token

    demo_session_id = create_demo_session_token(
        tenant_slug="municipio",
        sector="gobierno",
        rubro="municipio",
    )
    response = client.post(
        f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
        json={
            "pregunta": "Quiero iniciar un reclamo por alumbrado publico.",
            "demo_mode": True,
            "tenant_slug": "municipio",
        },
        headers={
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "signed-demo-owner-mismatch",
            "X-Request-Id": "signed-demo-owner-mismatch-1",
        },
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["fuente"] == "demo_municipio_runtime"
    assert payload["accion_backend"] == "demo_crear_reclamo"
    ticket = MunicipioTicket.query.one()
    assert ticket.tenant_id == demo_tenant.id
    assert ticket.municipio_id == tenant_owner.id
    assert ticket.user_id == tenant_owner.id


def test_real_tenant_never_uses_demo_tolerant_scope(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "DEMO_RUBROS", [])
    real_owner = _user(email="real-owner@example.invalid")
    unrelated_owner = _user(email="unrelated-owner@example.invalid")
    real_tenant = _tenant(slug="real-municipio", owner=real_owner, demo=False)
    db.session.commit()

    assert _tenant_for_owner(unrelated_owner, real_tenant) is None
    response = handle_demo_municipio_message(
        question="Quiero iniciar un reclamo por alumbrado publico.",
        owner_user=unrelated_owner,
        verified_demo_tenant=real_tenant,
        anon_id="anon-real-scope",
        chat_session_id="real-scope",
        demo_session_payload={"tenant_slug": real_tenant.slug, "kind": "demo_session"},
    )
    assert response["accion_backend"] == "demo_tenant_scope_unavailable"
    assert MunicipioTicket.query.count() == 0


def test_superadmin_diagnostic_and_idempotent_demo_repair(app, client, monkeypatch):
    legacy_owner = _user(email="legacy-diagnostic-owner@example.invalid")
    canonical_owner = _user(email="canonical-diagnostic-owner@example.invalid")
    demo_tenant = _tenant(slug="municipio", owner=legacy_owner, demo=True)
    db.session.add_all(
        [
            CatalogoItem(
                tenant_id=demo_tenant.id,
                user_id=canonical_owner.id,
                nombre="Demo item 1",
            ),
            CatalogoItem(
                tenant_id=demo_tenant.id,
                user_id=legacy_owner.id,
                nombre="Demo item 2",
            ),
        ]
    )
    superadmin = _user(
        email="platform-demo-repair@example.invalid",
        role="super_admin",
        tipo_chat="pyme",
    )
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    headers = clerk_superadmin_headers(superadmin)
    monkeypatch.setitem(
        app.config,
        "DEMO_RUBROS",
        [{"key": "municipio", "tenant_slug": "municipio", "user_id": canonical_owner.id}],
    )

    diagnostic = client.get(
        "/api/admin/tenants/municipio/demo-scope-diagnostic",
        headers=headers,
    )
    assert diagnostic.status_code == 200, diagnostic.get_json()
    body = diagnostic.get_json()
    assert body["tenant"] == {
        "id": demo_tenant.id,
        "slug": "municipio",
        "municipio_id": legacy_owner.id,
        "pyme_id": None,
    }
    assert body["owner"] == {
        "user_id": legacy_owner.id,
        "tenant_id": None,
        "tenant_reference_count": 1,
    }
    assert body["catalog_items_by_user_id"] == [
        {"user_id": legacy_owner.id, "count": 1},
        {"user_id": canonical_owner.id, "count": 1},
    ]
    serialized = str(body).lower()
    assert "email" not in serialized
    assert "name" not in serialized

    dry_run = client.post(
        "/api/admin/tenants/municipio/demo-scope-repair",
        json={"dry_run": True},
        headers=headers,
    )
    assert dry_run.status_code == 200, dry_run.get_json()
    assert dry_run.get_json()["selected_owner_user_id"] == canonical_owner.id
    db.session.refresh(demo_tenant)
    assert demo_tenant.municipio_id == legacy_owner.id
    assert canonical_owner.tenant_id is None

    applied = client.post(
        "/api/admin/tenants/municipio/demo-scope-repair",
        json={"dry_run": False},
        headers=headers,
    )
    assert applied.status_code == 200, applied.get_json()
    assert applied.get_json()["catalog_items_reassigned"] == 1
    db.session.refresh(demo_tenant)
    db.session.refresh(canonical_owner)
    assert demo_tenant.municipio_id == canonical_owner.id
    assert canonical_owner.tenant_id == demo_tenant.id
    assert {
        item.user_id
        for item in CatalogoItem.query.filter_by(tenant_id=demo_tenant.id).all()
    } == {canonical_owner.id}

    second = client.post(
        "/api/admin/tenants/municipio/demo-scope-repair",
        json={"dry_run": False},
        headers=headers,
    )
    assert second.status_code == 200, second.get_json()
    assert second.get_json()["idempotent"] is True
    assert second.get_json()["change_count"] == 0


def test_superadmin_repair_rejects_real_tenant(app, client, monkeypatch):
    owner = _user(email="real-repair-owner@example.invalid")
    real_tenant = _tenant(slug="real-repair", owner=owner, demo=False)
    superadmin = _user(
        email="platform-real-repair@example.invalid",
        role="super_admin",
        tipo_chat="pyme",
    )
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    headers = clerk_superadmin_headers(superadmin)
    monkeypatch.setitem(app.config, "DEMO_RUBROS", [])

    response = client.post(
        "/api/admin/tenants/real-repair/demo-scope-repair",
        json={"dry_run": False, "owner_user_id": owner.id},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "tenant_not_registered_as_demo"
    db.session.refresh(real_tenant)
    assert real_tenant.municipio_id == owner.id
