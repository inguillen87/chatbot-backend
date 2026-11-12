"""Actual Flask login and CRM routes after a signed synthetic Meta claim."""
from datetime import datetime, timezone
import base64
import json
import secrets
from types import SimpleNamespace

import pytest

from app import create_app
from config import TestingConfig
from models import (
    AuthSession, CategoriaTicket, DomainEffectOutbox, MunicipioTicket, ProviderConnection,
    ProviderSender, TenantProfile, TicketComentario, TicketDomainEffectReceipt, User, db,
)
from services import meta_whatsapp_credentials as vault, whatsapp_inbound_worker as worker
from services.meta_whatsapp_outbox import RUNTIME_EXTENSION
from services.meta_whatsapp_runtime import APP_SECRETS_KEY, MetaOutboundRuntime
from tests.test_meta_whatsapp_inbound_worker import (
    _confirm_code, _drain_draft, _native_claim_configuration, _post, _post_confirmation,
)
from tests.test_meta_whatsapp_outbox import APP, CONTACT, PHONE, WABA


PASSWORD = "Synthetic CRM local only"
POINT = (0, -68.30890123456)
INBOX = "/api/v2/inbox/omnichannel"


class _MetaCrmAcceptanceConfig(TestingConfig):
    META_WHATSAPP_CALLBACK_ENABLED = True
    DEFER_ANON_MIGRATION_ON_LOGIN = False


def _actor(name, role, tenant=None):
    actor = User(
        name=name, email=f"{name.lower().replace(' ', '-')}@example.invalid",
        rol=role, tipo_chat="municipio", es_empleado=role == "empleado",
    )
    if tenant is not None:
        actor.tenant_id, actor.tenant_slug, actor.municipio_id = tenant.id, tenant.slug, tenant.municipio_id
    actor.set_password(PASSWORD)
    db.session.add(actor)
    return actor


def _tenant(slug, owner):
    tenant = TenantProfile(slug=slug, nombre=slug, tipo="municipio", vertical="gobierno",
                           plan="full", municipio_id=owner.id, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, slug, owner.id
    return tenant


def _category(tenant, name):
    category = CategoriaTicket(tenant_id=tenant.id, nombre=name, tipo="ticket")
    db.session.add(category)
    db.session.flush()
    return category


def _employee(name, tenant, category):
    actor = _actor(name, "empleado", tenant)
    actor.categorias_ticket = [category]
    actor.accesibilidad = {"employee_scope": {
        "categorias": [category.nombre], "channels": ["whatsapp"], "permisos": [],
    }}
    return actor


def _meta_runtime(app, now, clock):
    def response(value):
        return SimpleNamespace(status_code=200, content=json.dumps(value).encode("utf-8"))

    def get(method, url, **_kwargs):
        assert method == "GET"
        if url.endswith("/debug_token"):
            return response({"data": {"is_valid": True, "app_id": APP, "expires_at": now + 3600,
                "data_access_expires_at": now + 3000,
                "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"],
                "granular_scopes": [
                    {"scope": "whatsapp_business_management", "target_ids": [WABA]},
                    {"scope": "whatsapp_business_messaging", "target_ids": [WABA]},
                ]}})
        if url.endswith(f"/{WABA}/phone_numbers"):
            return response({"data": [{"id": PHONE}]})
        if url.endswith(f"/{WABA}/subscribed_apps"):
            return response({"data": [{"whatsapp_business_api_data": {"id": APP}}]})
        raise AssertionError("Unexpected synthetic Graph read")

    def post(*_args, **_kwargs):
        pytest.fail("The acceptance journey must not send anything to Meta")

    return MetaOutboundRuntime(app.config, get=get, post=post, clock=lambda: clock[0])


@pytest.fixture
def crm_environment():
    app = create_app(_MetaCrmAcceptanceConfig)
    now = int(datetime.now(timezone.utc).timestamp())
    with app.app_context():
        assert str(db.engine.url) == "sqlite:///:memory:"
        db.create_all()
        owner = _actor("Municipal owner", "admin")
        other_owner = _actor("Other owner", "admin")
        db.session.flush()
        tenant = _tenant("native-municipal-crm", owner)
        other = _tenant("other-municipal-crm", other_owner)
        arbolado = _category(tenant, "Arbolado")
        employee = _employee("Arbolado primary", tenant, arbolado)
        replacement = _employee("Arbolado replacement", tenant, arbolado)
        out_of_category = _employee("Bacheo employee", tenant, _category(tenant, "Bacheo"))
        foreign_employee = _employee("Foreign employee", other, _category(other, "Arbolado"))
        app.config.update(
            CUTOVER_WRITER_FENCE_ENABLED=False,
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_QUEUE_TENANT_IDS=str(tenant.id),
            WHATSAPP_INBOUND_HASH_SECRET=secrets.token_hex(32),
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=secrets.token_hex(32),
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET=secrets.token_hex(32),
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(tenant.id),
            META_WHATSAPP_INBOUND_WORKER_ENABLED=True,
            META_WHATSAPP_OUTBOUND_ENABLED=False,
            META_WHATSAPP_GRAPH_VERSION="v25.0",
            META_WHATSAPP_CALLBACK_VERIFY_TOKENS={APP: secrets.token_hex(16)},
            META_WHATSAPP_CALLBACK_CANARY_RECIPIENTS_JSON=json.dumps([{
                "app_id": APP, "waba_id": WABA, "phone_number_id": PHONE,
                "tenant_id": tenant.id, "contact": CONTACT,
            }]),
            TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="synthetic",
            TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({
                "synthetic": base64.b64encode(secrets.token_bytes(32)).decode(),
            }),
            **{APP_SECRETS_KEY: {APP: secrets.token_hex(32)}},
        )
        connection = ProviderConnection(tenant_id=tenant.id, provider="meta", channel="whatsapp",
            environment="production", status="connected", external_app_id=APP,
            external_account_id=WABA, credentials_ref=vault.VAULT_REF, config={})
        db.session.add(connection)
        db.session.flush()
        connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=connection, tenant_id=tenant.id, access_token=secrets.token_urlsafe(64),
            revision=1, expires_at=now + 3600, now=now, app_config=app.config)}
        db.session.add(ProviderSender(
            tenant_id=tenant.id, provider_connection_id=connection.id, channel="whatsapp",
            phone_number_id=PHONE, waba_id=WABA, status="ONLINE",
        ))
        db.session.commit()
        clock = [now]
        app.extensions[RUNTIME_EXTENSION] = _meta_runtime(app, now, clock)
        env = SimpleNamespace(app=app, tenant=tenant, other=other, owner=owner,
            other_owner=other_owner, employee=employee, replacement=replacement,
            out_of_category=out_of_category, foreign_employee=foreign_employee,
            category_id=arbolado.id, now=now, clock=clock,
            event=SimpleNamespace(message_id="wamid.crm/start+="))
        _native_claim_configuration(env)
        try:
            yield env
        finally:
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def _login(environment, actor):
    client = environment.app.test_client()
    response = client.post("/auth/login", json={"email": actor.email, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["id"] == actor.id and payload["tenant_slug"] == actor.tenant_slug
    assert AuthSession.query.filter_by(actor_id=actor.id, provider="native").count() == 1
    return client, {"Authorization": "Bearer " + payload["token"], "X-Tenant-Slug": actor.tenant_slug}


def _ok(response):
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_signed_meta_ticket_is_visible_assignable_and_audited_in_authenticated_crm(crm_environment):
    env = crm_environment
    tenant_id = env.tenant.id
    first = _post(env, body="Quiero hacer un reclamo por un árbol caído")
    _drain_draft(tenant_id, first)
    location = _post(env, kind="location", message_id="wamid.crm/location+=", point=POINT)
    _, draft, confirmation = _drain_draft(tenant_id, location)
    assert draft["state"] == "ESPERANDO_CONFIRMACION"
    _post_confirmation(env, _confirm_code(confirmation), "crm-claim")
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert result["completed"] == 1 and result["results"][0]["outbound_count"] == 2, result
    ticket = MunicipioTicket.query.one()
    ticket_id = ticket.id
    assert ticket.categoria_id == env.category_id and ticket.asignado_a_id is None
    assert (ticket.latitud, ticket.longitud, ticket.direccion) == (*POINT, None)

    owner, owner_headers = _login(env, env.owner)
    employee, employee_headers = _login(env, env.employee)
    out_of_category, out_of_category_headers = _login(env, env.out_of_category)
    foreign, foreign_headers = _login(env, env.other_owner)
    detail = f"{INBOX}/{ticket_id}?source_model=MunicipioTicket"
    legacy_detail = f"/api/tickets/municipio/{ticket_id}"
    actions = f"{INBOX}/{ticket_id}/actions"
    for client, headers in ((owner, owner_headers), (employee, employee_headers)):
        items = _ok(client.get(INBOX, headers=headers))["items"]
        assert [item["ticket_id"] for item in items] == [ticket_id]
        item = _ok(client.get(detail, headers=headers))["item"]
        assert item["ticket_id"] == ticket_id and item["source_model"] == "MunicipioTicket"
        assert item["channel"] == "whatsapp" and item["category"] == "Arbolado"
        assert item["location"] == {"lat": POINT[0], "lng": POINT[1], "address": None, "district": None}
        assert item["map"]["can_render"] is True and item["assignee"] is None
        assert "consulta_pin" not in json.dumps(item, ensure_ascii=False)

    legacy = _ok(owner.get(legacy_detail, headers=owner_headers))
    assert legacy["id"] == ticket_id and legacy["categoria"] == "Arbolado"
    for client, headers in ((foreign, foreign_headers), (out_of_category, out_of_category_headers)):
        assert _ok(client.get(INBOX, headers=headers))["items"] == []
        assert client.get(detail, headers=headers).status_code == 404
        assert client.get(legacy_detail, headers=headers).status_code == 404
        assert client.post(actions, json={"action": "claim", "source_model": "MunicipioTicket"},
                           headers=headers).status_code == 404
    assert env.app.test_client().get(detail).status_code == 401
    db.session.expire_all()
    assert db.session.get(MunicipioTicket, ticket_id).asignado_a_id is None

    claim = {"action": "claim", "source_model": "MunicipioTicket"}
    initial = _ok(employee.post(actions, json=claim, headers=employee_headers))["delivery"]
    replay = _ok(employee.post(actions, json=claim, headers=employee_headers))["delivery"]
    assert initial["status"] == "claimed" and replay["status"] == "already_owned"
    assert initial["audit"]["storage"] == "ticket_comment" and initial["audit"]["event_recorded"] is True
    assert replay["audit"]["event_recorded"] is False and replay["audit"]["replay_deduplicated"] is True
    first_audit = TicketComentario.query.filter_by(municipio_ticket_id=ticket_id, origen="admin_panel").one()
    assert first_audit.user_id == env.employee.id and first_audit.es_admin
    assert CONTACT not in first_audit.comentario and "tomado" in first_audit.comentario

    assign = {"action": "assign", "source_model": "MunicipioTicket", "expected_assignee_id": env.employee.id}
    wrong = owner.post(actions, json={**assign, "assignee_id": env.foreign_employee.id}, headers=owner_headers)
    assert wrong.status_code == 404 and wrong.get_json()["reason_code"] == "assignee_not_found"
    replacement = {**assign, "assignee_id": env.replacement.id}
    assigned = _ok(owner.post(actions, json=replacement, headers=owner_headers))["delivery"]["assignment"]
    repeated = _ok(owner.post(actions, json=replacement, headers=owner_headers))["delivery"]["assignment"]
    assert assigned["ticket_id"] == ticket_id and assigned["assignee_id"] == env.replacement.id
    assert assigned["replayed"] is False and repeated["replayed"] is True
    updated = _ok(owner.get(detail, headers=owner_headers))["item"]
    assert updated["assignee"]["id"] == env.replacement.id and updated["status"] == "en_proceso"
    assert (updated["location"]["lat"], updated["location"]["lng"]) == POINT
    audits = TicketComentario.query.filter_by(municipio_ticket_id=ticket_id, origen="admin_panel").order_by(
        TicketComentario.id,
    ).all()
    assert [audit.user_id for audit in audits] == [env.employee.id, env.owner.id]
    assert all(audit.es_admin and CONTACT not in audit.comentario for audit in audits)
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 1
    assert DomainEffectOutbox.query.count() == 3
