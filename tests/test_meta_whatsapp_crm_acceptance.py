"""Actual Flask login and CRM routes after a signed synthetic Meta claim."""
from datetime import datetime, timezone
from copy import deepcopy
import base64
import json
import secrets
from types import SimpleNamespace

import pytest

from app import create_app
from config import TestingConfig
from models import (
    AuthSession, CategoriaTicket, ChatSessionContext, DomainEffectOutbox, MunicipioTicket, ProviderConnection,
    ProviderSender, TenantProfile, TicketComentario, TicketDomainEffectReceipt, User,
    WhatsAppInboundTurn, WhatsAppOutboundAttempt, db,
)
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services import crm_intelligence as crm
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


def _native_turn(turn_id):
    db.session.expire_all()
    return WhatsAppInboundTurn.query.filter_by(turn_id=turn_id).one()


def _contact_events(tenant_id):
    return InteractionEvent.query.filter_by(tenant_id=tenant_id).order_by(InteractionEvent.id).all()


def _assert_native_event(event, *, tenant_id, contact_id, message_id, kind, content):
    assert (event.tenant_id, event.contact_id, event.channel, event.direction) == (
        tenant_id, contact_id, "whatsapp", "inbound",
    )
    assert event.content_type == kind and event.content == content
    assert event.metadata_payload["message_sid"] == message_id
    assert event.metadata_payload["source_event_id"] == message_id
    assert event.metadata_payload["provider"] == "meta"
    assert event.metadata_payload["event_type"] == "whatsapp_inbound"
    for forbidden in ("access_token", "raw_payload", "media_url", "latitude", "longitude"):
        assert forbidden not in event.metadata_payload


def test_signed_native_welcome_creates_one_contact_and_interaction_then_emits_after_commit(crm_environment, monkeypatch):
    env = crm_environment
    tenant_id, message_id = env.tenant.id, "wamid.crm/welcome-contact+="
    units, emitted = [], []
    original_record = crm.record_contact_interaction

    def record(*args, **kwargs):
        unit = kwargs["unit_of_work"]
        assert unit is not None and kwargs["conversation_turn_id"] == turn_id
        payload = original_record(*args, **kwargs)
        units.append(unit)
        assert emitted == [], "CRM must not emit while its writer transaction is open"
        return payload

    def emit(tenant, payload):
        assert units and units[-1]._phase == "committed"
        emitted.append((tenant.id, payload["contact_id"]))

    monkeypatch.setattr(crm, "record_contact_interaction", record)
    monkeypatch.setattr(crm, "emit_crm_contact_update", emit)
    assert Contact.query.count() == InteractionEvent.query.count() == 0
    turn_id = _post(env, body="Hola", message_id=message_id)
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1 and report["results"][0]["outbound_count"] == 2, report
    contact = Contact.query.filter_by(tenant_id=tenant_id).one()
    assert Contact.query.count() == 1 and contact.phone == "+" + CONTACT
    events = _contact_events(tenant_id)
    assert len(events) == 1 and InteractionEvent.query.count() == 1
    _assert_native_event(events[0], tenant_id=tenant_id, contact_id=contact.id,
                         message_id=message_id, kind="text", content="Hola")
    assert emitted == [(tenant_id, contact.id)]
    assert _native_turn(turn_id).status == "completed"
    assert WhatsAppOutboundAttempt.query.count() == 2
    assert all(row.provider == "meta" and row.status == "pending" for row in WhatsAppOutboundAttempt.query.all())
    assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False
    assert MunicipioTicket.query.count() == DomainEffectOutbox.query.count() == 0


def test_signed_native_draft_and_location_reuse_contact_and_record_one_event_each(crm_environment):
    env = crm_environment
    tenant_id = env.tenant.id
    body = "Quiero hacer un reclamo por un árbol caído"
    first_id, location_id = "wamid.crm/draft-contact+=", "wamid.crm/location-contact+="
    first = _post(env, body=body, message_id=first_id)
    chat_id, _, _ = _drain_draft(tenant_id, first)
    contact_id = Contact.query.filter_by(tenant_id=tenant_id).one().id
    assert Contact.query.count() == len(_contact_events(tenant_id)) == 1
    location = _post(env, kind="location", message_id=location_id, point=POINT)
    location_chat_id, draft, _ = _drain_draft(tenant_id, location)
    assert location_chat_id == chat_id and draft["state"] == "ESPERANDO_CONFIRMACION"
    assert Contact.query.count() == 1
    assert Contact.query.filter_by(tenant_id=tenant_id).one().id == contact_id
    events = _contact_events(tenant_id)
    assert len(events) == InteractionEvent.query.count() == 2
    _assert_native_event(events[0], tenant_id=tenant_id, contact_id=contact_id,
                         message_id=first_id, kind="text", content=body)
    _assert_native_event(events[1], tenant_id=tenant_id, contact_id=contact_id,
                         message_id=location_id, kind="location", content="")
    assert MunicipioTicket.query.count() == DomainEffectOutbox.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 2
    assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False


def test_duplicate_signed_native_callback_and_empty_drain_do_not_duplicate_crm(crm_environment):
    env = crm_environment
    tenant_id, message_id = env.tenant.id, "wamid.crm/deduplicated-contact+="
    turn_id = _post(env, body="Hola", message_id=message_id)
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["completed"] == 1
    contact_id = Contact.query.filter_by(tenant_id=tenant_id).one().id
    event_id = _contact_events(tenant_id)[0].id
    assert _post(env, body="Hola", message_id=message_id) == turn_id
    for _ in range(2):
        report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
        assert report["processed"] == 0 and report["completed"] == 0, report
    assert Contact.query.count() == InteractionEvent.query.count() == 1
    assert Contact.query.one().id == contact_id and InteractionEvent.query.one().id == event_id
    assert WhatsAppInboundTurn.query.count() == 1 and WhatsAppOutboundAttempt.query.count() == 2
    assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False


def test_signed_native_contact_isolated_from_foreign_namesake_in_normal_crm_routes(crm_environment):
    env = crm_environment
    foreign = Contact(id="00000000-0000-4000-8000-000000000042", tenant_id=env.other.id,
        phone="+" + CONTACT, whatsapp_id="+" + CONTACT,
        name="Foreign private fixture", email="foreign-private@example.invalid")
    db.session.add(foreign)
    db.session.commit()
    foreign_id, tenant_id = foreign.id, env.tenant.id
    turn_id = _post(env, body="Hola", message_id="wamid.crm/isolated-contact+=")
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["completed"] == 1
    contact = Contact.query.filter_by(tenant_id=tenant_id).one()
    contact_id = contact.id
    assert Contact.query.count() == 2 and contact_id != foreign_id
    assert contact.name != "Foreign private fixture" and contact.email != "foreign-private@example.invalid"
    assert Contact.query.filter_by(tenant_id=env.other.id).one().id == foreign_id
    assert InteractionEvent.query.filter_by(tenant_id=env.other.id).count() == 0
    assert _contact_events(tenant_id)[0].contact_id == contact_id
    owner, owner_headers = _login(env, env.owner)
    other, other_headers = _login(env, env.other_owner)
    own_base = f"/api/admin/tenants/{env.tenant.slug}/contacts"
    other_base = f"/api/admin/tenants/{env.other.slug}/contacts"
    own_list = _ok(owner.get(own_base, headers=owner_headers))
    other_list = _ok(other.get(other_base, headers=other_headers))
    assert [row["contact_id"] for row in own_list["contacts"]] == [contact_id]
    assert [row["contact_id"] for row in other_list["contacts"]] == [foreign_id]
    history = _ok(owner.get(f"{own_base}/{contact_id}/history", headers=owner_headers))
    assert history["contact"]["id"] == contact_id and len(history["interactions"]) == 1
    assert history["interactions"][0]["content"] == "Hola"
    assert "Foreign private fixture" not in json.dumps(own_list)
    assert other.get(f"{other_base}/{contact_id}/history", headers=other_headers).status_code == 404
    assert owner.get(f"{own_base}/{foreign_id}/history", headers=owner_headers).status_code == 404
    assert env.app.test_client().get(f"{own_base}/{contact_id}/history").status_code == 401
    assert _native_turn(turn_id).status == "completed"


@pytest.mark.parametrize("writer", ["resolve_or_create_contact", "record_contact_interaction"])
def test_native_crm_writer_failure_rolls_back_context_crm_and_reply_without_emit(crm_environment, monkeypatch, writer):
    env = crm_environment
    tenant_id = env.tenant.id
    turn_id = _post(env, body="Quiero hacer un reclamo por un árbol caído",
                    message_id="wamid.crm/failed-writer-" + writer + "+=")
    row = _native_turn(turn_id)
    chat_id, inbound_id = row.chat_session_id, row.id
    context_before = deepcopy(db.session.get(ChatSessionContext, chat_id).context_data)
    calls, emitted = [], []
    original = getattr(crm, writer)

    def fail_after_real_write(*args, **kwargs):
        assert kwargs["unit_of_work"] is not None and kwargs["conversation_turn_id"] == turn_id
        original(*args, **kwargs)
        calls.append(writer)
        assert Contact.query.filter_by(tenant_id=tenant_id).count() == 1
        if writer == "record_contact_interaction":
            assert ContactSnapshot.query.count() == InteractionEvent.query.count() == 1
        assert emitted == []
        raise RuntimeError("synthetic native CRM writer failure")

    monkeypatch.setattr(crm, writer, fail_after_real_write)
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 0 and calls == [writer], report
    result = report["results"][0]
    assert (result["status"], result["error_code"]) == ("dead", "meta_inbound_crm_write_failed")
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=inbound_id).count() == 0
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
    db.session.expire_all()
    assert db.session.get(ChatSessionContext, chat_id).context_data == context_before
    assert _native_turn(turn_id).status == "dead" and emitted == []
    assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False


def test_native_survey_menu_keeps_existing_owner_chat_type_without_copying_to_crm(crm_environment, monkeypatch):
    env = crm_environment
    tenant_id, owner_id = env.tenant.id, env.owner.id
    env.owner.tipo_chat = "pyme"
    db.session.commit()
    emitted = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    assert env.tenant.tipo == "municipio" and env.tenant.municipio_id == owner_id
    message_id = "wamid.crm/survey-owner-chat-type+="
    turn_id = _post(env, body="Encuestas", message_id=message_id)
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1 and report["results"][0]["outbound_count"] == 1, report
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert emitted == []
    row = _native_turn(turn_id)
    assert row.status == "completed"
    attempt = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=row.id).one()
    assert attempt.provider == "meta" and attempt.status == "pending"
    assert "Por el momento no hay encuestas activas en este municipio." in attempt.payload_json["message"]["text"]["body"]
    owner, headers = _login(env, env.owner)
    contacts = _ok(owner.get(f"/api/admin/tenants/{env.tenant.slug}/contacts", headers=headers))["contacts"]
    assert contacts == []
    db.session.expire_all()
    assert db.session.get(User, owner_id).tipo_chat == "pyme"
    tenant = db.session.get(TenantProfile, tenant_id)
    assert tenant.tipo == "municipio" and tenant.municipio_id == owner_id
    assert MunicipioTicket.query.count() == DomainEffectOutbox.query.count() == 0
    assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False


def _survey_crm_snapshot():
    # Synthetic local rows only; retain complete values so changing an existing
    # contact's summary, tags, interaction time or snapshot also fails the test.
    return {model.__name__: [
        {column.name: deepcopy(getattr(row, column.name)) for column in model.__table__.columns}
        for row in model.query.order_by(model.id).all()
    ] for model in (Contact, ContactSnapshot, InteractionEvent)}


@pytest.mark.parametrize("phase", ["consent", "answer", "reject", "active_hello"])
def test_native_survey_turns_preserve_existing_crm_without_copying_answers(crm_environment, monkeypatch, phase):
    # Use the existing governed-survey builder, not the journey module (which
    # imports crm_environment from this acceptance module and would be cyclic).
    import tests.test_whatsapp_survey_conversation as survey_fixture
    from models import EncRespuesta
    from services.constants import CONTEXTO_MUNICIPIO
    from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY

    env = crm_environment
    env.tenant.slug = env.owner.tenant_slug = "junin"
    env.tenant.configuracion = {**(env.tenant.configuracion or {}), "encuestas_enabled": True}
    db.session.commit()
    client, headers = _login(env, env.owner)
    builder = survey_fixture.WhatsAppSurveyConversationTest(methodName="runTest")
    builder.app, builder.tenant, builder.owner, builder.client = env.app, env.tenant, env.owner, client
    builder._admin_headers = lambda: headers
    survey = builder._create_governed_survey(slug="native-meta-crm-exclusion", privacy_mode="source_anonymous")
    env.app.config.update(SURVEY_JURISDICTION_GATE_MODE="enforce_visibility",
                          SURVEY_JURISDICTION_GATE_TENANT_IDS=str(env.tenant.id))
    tenant_id = env.tenant.id
    existing = Contact(id="00000000-0000-4000-8000-000000000043", tenant_id=tenant_id,
        phone="+" + CONTACT, whatsapp_id="+" + CONTACT, name="Contacto sintético previo",
        type="neighbor", tags=["preexisting"], preferences={"last_summary": "Antes de encuesta", "marketing_opt_in": False},
        last_interaction_at=datetime.fromtimestamp(env.now, timezone.utc))
    db.session.add(existing)
    db.session.flush()
    db.session.add(ContactSnapshot(contact_id=existing.id, summary_text="Resumen previo", last_intent="other"))
    db.session.add(InteractionEvent(tenant_id=tenant_id, contact_id=existing.id, channel="whatsapp",
        direction="inbound", content_type="text", content="Consulta previa sintética",
        metadata_payload={"source": "synthetic_existing"}))
    db.session.commit()
    before = _survey_crm_snapshot()
    emitted = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))

    def send(body, suffix):
        turn_id = _post(env, body=body, message_id=f"wamid.crm/survey-exclusion-{phase}-{suffix}+=")
        report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
        assert report["completed"] == 1, report
        row = _native_turn(turn_id)
        assert row.status == "completed"
        attempts = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=row.id).order_by(WhatsAppOutboundAttempt.id).all()
        assert attempts and all(attempt.provider == "meta" and attempt.status == "pending" for attempt in attempts)
        assert _survey_crm_snapshot() == before and emitted == []
        assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False
        context = db.session.get(ChatSessionContext, row.chat_session_id)
        return context, [attempt.payload_json["message"]["text"]["body"] for attempt in attempts]

    _, listing = send("Encuestas", "list")
    assert "1." in listing[0]
    context, disclosure = send("1", "select")
    state = context.context_data[CONTEXTO_MUNICIPIO][WHATSAPP_SURVEY_FLOW_STATE_KEY]
    assert state["step"] == "consent" and state["consent_accepted"] is False
    assert "Respondé 1 para aceptar" in disclosure[-1]
    if phase in {"consent", "answer"}:
        context, question = send("1", "consent")
        state = context.context_data[CONTEXTO_MUNICIPIO][WHATSAPP_SURVEY_FLOW_STATE_KEY]
        assert state["step"] == "question" and state["consent_accepted"] is True
        assert "Pregunta 1" in question[0]
        if phase == "answer":
            context, _ = send("1", "answer")
            assert context.context_data[CONTEXTO_MUNICIPIO][WHATSAPP_SURVEY_FLOW_STATE_KEY]["question_index"] == 1
    elif phase == "reject":
        context, reply = send("2", "reject")
        assert reply
        assert WHATSAPP_SURVEY_FLOW_STATE_KEY not in context.context_data[CONTEXTO_MUNICIPIO]
    else:
        previous = deepcopy(state)
        context, reply = send("Hola", "hello")
        assert reply
        assert context.context_data[CONTEXTO_MUNICIPIO][WHATSAPP_SURVEY_FLOW_STATE_KEY] == previous
    assert _survey_crm_snapshot() == before and emitted == []
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0
    assert MunicipioTicket.query.count() == DomainEffectOutbox.query.count() == 0
