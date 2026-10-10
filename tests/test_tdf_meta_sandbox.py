"""Offline HTTP + persisted pilot receipts; not Meta/phone delivery evidence."""
import base64
from copy import deepcopy
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace

from flask import Flask
import pytest

from models import (db, MessagingEventLedger, ProviderConnection, ProviderSender,
                    TenantConfig, TenantProfile, User, WebhookDelivery)
from services import meta_whatsapp_cloud as cloud
from services import meta_whatsapp_credentials as vault
from services import tdf_meta_sandbox as pilot
from services.institutional_assistant_content import digest, normalize_bundle
from routes.tdf_meta_sandbox import tdf_meta_sandbox_bp
from tests.test_institutional_assistant_content import sample

NOW = 1791637200
SECRET, TOKEN = secrets.token_hex(32), secrets.token_urlsafe(64)
CONTACT = "5491112345678"
URL = "/webhook/meta/tdf-sandbox"


@pytest.fixture
def environment():
    app = Flask("tdf-meta-pilot-test")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False, CUTOVER_WRITER_FENCE_ENABLED=False,
        TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({"test": base64.b64encode(secrets.token_bytes(32)).decode()}),
        TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="test", META_TDF_SANDBOX_ENABLED=True,
        META_TDF_SANDBOX_APP_ID=pilot.TEST_APP, META_TDF_SANDBOX_WABA_ID=pilot.TEST_WABA,
        META_TDF_SANDBOX_PHONE_NUMBER_ID=pilot.TEST_PHONE, META_TDF_SANDBOX_APP_SECRET=SECRET,
        META_TDF_SANDBOX_VERIFY_TOKEN=secrets.token_hex(16), META_TDF_SANDBOX_GRAPH_VERSION="v25.0",
        META_TDF_SANDBOX_RECIPIENTS=[CONTACT])
    db.init_app(app)
    app.register_blueprint(tdf_meta_sandbox_bp)
    with app.app_context():
        assert db.engine.dialect.name == "sqlite" and db.engine.url.database == ":memory:"
        db.create_all()
        owner = User(name="Synthetic TDF", email="tdf@example.invalid", rol="admin",
                     password_hash="synthetic-not-a-login", tipo_chat="municipio")
        db.session.add(owner); db.session.flush()
        tenant = TenantProfile(id=46, slug=pilot.TENANT_SLUG, nombre="Conversa TDF", tipo="municipio",
                               municipio_id=owner.id, is_active=True)
        db.session.add(tenant); db.session.flush()
        owner.tenant_id = tenant.id
        connection = ProviderConnection(tenant_id=46, provider="meta", channel="whatsapp", environment="sandbox",
            status="connected", external_app_id=pilot.TEST_APP, external_account_id=pilot.TEST_WABA,
            credentials_ref=vault.VAULT_REF, config={})
        db.session.add(connection); db.session.flush()
        connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(connection=connection, tenant_id=46,
            access_token=TOKEN, revision=1, expires_at=NOW + 3600, now=NOW, app_config=app.config)}
        sender = ProviderSender(tenant_id=46, provider_connection_id=connection.id, channel="whatsapp",
            phone_number_id=pilot.TEST_PHONE, waba_id=pilot.TEST_WABA, status="ONLINE")
        db.session.add(sender)
        bundle = normalize_bundle(sample(46, pilot.TENANT_SLUG), tenant_id=46, tenant_slug=pilot.TENANT_SLUG)
        state = {"bundle": bundle, "bundle_hash": digest(bundle), "generation": 1, "visibility": "public"}
        state["revision"] = digest({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
        db.session.add(TenantConfig(tenant_id=46, key="institutional_assistant", channel="knowledge", json_value=state))
        db.session.commit()
        yield SimpleNamespace(app=app, client=app.test_client(), tenant=tenant, owner=owner,
                              connection=connection, sender=sender, state=state)
        db.session.rollback(); db.session.remove(); db.drop_all(); db.engine.dispose()


def document(*, mid="wamid.inbound1", contact=CONTACT, content=None, timestamp=NOW):
    message = {"from": contact, "id": mid, "timestamp": str(timestamp), "type": "text", "text": {"body": "Hola"}}
    if content:
        message.pop("text"); message.update(content)
    return {"object": "whatsapp_business_account", "entry": [{"id": pilot.TEST_WABA,
        "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp",
            "metadata": {"phone_number_id": pilot.TEST_PHONE}, "messages": [message]}}]}]}


def encoded(doc):
    raw = json.dumps(doc, separators=(",", ":")).encode()
    return raw, "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def authority(sender, credential):
    return cloud.VerifiedMetaAuthority(sender, credential.revision, "v25.0", NOW, NOW + 60, True, True)


def process(doc, *, post=None):
    raw, signature = encoded(doc)
    return pilot.process(raw, signature, now=NOW, authority=authority, post=post or accepted_post)


def accepted_post(*args, **kwargs):
    return SimpleNamespace(status_code=200,
        content=b'{"messaging_product":"whatsapp","messages":[{"id":"wamid.reply1"}]}')


def sent_body(payload):
    return payload["interactive"]["body"]["text"] if payload["type"] == "interactive" else payload["text"]["body"]


def test_default_disabled_and_challenge_is_strict(environment):
    env = environment
    env.app.config["META_TDF_SANDBOX_ENABLED"] = False
    assert env.client.get(URL).status_code == 404
    assert env.client.post(URL, json={}).status_code == 404
    env.app.config["META_TDF_SANDBOX_ENABLED"] = True
    params = {"hub.mode": "subscribe", "hub.verify_token": env.app.config["META_TDF_SANDBOX_VERIFY_TOKEN"], "hub.challenge": "123456"}
    reply = env.client.get(URL, query_string=params)
    assert reply.status_code == 200 and reply.data == b"123456"
    assert "no-store" in reply.headers["Cache-Control"]
    assert env.client.get(URL, query_string=[*params.items(), ("hub.challenge", "duplicate")]).status_code == 400
    assert db.session.query(WebhookDelivery).count() == 0


def test_bad_signature_is_before_graph_and_writes(environment, monkeypatch):
    monkeypatch.setattr(pilot.GraphAuthority, "__call__", lambda *args: pytest.fail("Signature must come first"))
    raw, signature = encoded(document())
    response = environment.client.post(URL, data=raw, content_type="application/json",
                                       headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert response.status_code == 403
    assert db.session.query(WebhookDelivery).count() == 0
    assert TOKEN not in response.data.decode() and SECRET not in response.data.decode()


def test_http_callback_actual_binding_knowledge_and_receipt_path(environment, monkeypatch):
    monkeypatch.setattr(pilot.time, "time", lambda: NOW)
    calls = []
    def graph(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/debug_token"):
            body = {"data": {"is_valid": True, "app_id": pilot.TEST_APP, "expires_at": NOW + 300,
                    "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"]}}
        elif url.endswith("/phone_numbers"):
            body = {"data": [{"id": pilot.TEST_PHONE}]}
        elif url.endswith("/subscribed_apps"):
            body = {"data": [{"whatsapp_business_api_data": {"id": pilot.TEST_APP}}]}
        else:
            assert method == "POST" and url.endswith("/messages")
            assert db.session.query(MessagingEventLedger).one().external_status == "send_uncertain"
            return accepted_post()
        return SimpleNamespace(status_code=200, content=json.dumps(body).encode())
    monkeypatch.setattr(pilot, "_graph_request", graph)
    # GraphAuthority's injectable adapter defaults to the function captured at
    # definition; wire this offline fake at the constructor boundary too.
    original = pilot.GraphAuthority
    monkeypatch.setattr(pilot, "GraphAuthority", lambda cfg, now: original(cfg, now, request_adapter=graph))
    raw, signature = encoded(document())
    response = environment.client.post(URL, data=raw, content_type="application/json",
                                       headers={"X-Hub-Signature-256": signature})
    assert response.status_code == 200 and response.json["accepted"] == 1
    assert response.json["delivery_verified"] is False
    assert [method for method, _ in calls] == ["GET", "GET", "GET", "POST"]
    replay = environment.client.post(URL, data=raw, content_type="application/json",
                                     headers={"X-Hub-Signature-256": signature})
    assert replay.status_code == 200 and replay.json["replayed"] == 1
    assert len([method for method, _ in calls if method == "POST"]) == 1


def test_revocation_after_answer_blocks_post_with_durable_non_retryable_receipt(environment, monkeypatch):
    original = pilot._answer
    def revoke(event, context):
        result = original(event, context)
        environment.connection.status = "disconnected"
        db.session.commit()
        return result
    monkeypatch.setattr(pilot, "_answer", revoke)
    assert process(document(), post=lambda *a, **k: pytest.fail("Revoked sender must block"))["accepted"] == 0
    assert db.session.query(MessagingEventLedger).one().external_status == "send_uncertain"
    assert db.session.query(WebhookDelivery).one().last_error == "tdf_sandbox_processing_uncertain"


def test_disabling_pilot_after_answer_prevents_send(environment, monkeypatch):
    original = pilot._answer
    def disable(event, context):
        result = original(event, context)
        environment.app.config["META_TDF_SANDBOX_ENABLED"] = False
        return result
    monkeypatch.setattr(pilot, "_answer", disable)
    assert process(document(), post=lambda *a, **k: pytest.fail("Disabled pilot must block"))["accepted"] == 0
    assert db.session.query(WebhookDelivery).count() == 1


@pytest.mark.parametrize("change", ["retire", "replace"])
def test_knowledge_retirement_or_replacement_before_post_blocks_and_cannot_replay(environment, monkeypatch, change):
    original = pilot._answer
    def withdraw(event, context):
        result = original(event, context)
        row = db.session.query(TenantConfig).filter_by(tenant_id=46, key="institutional_assistant", channel="knowledge").one()
        state = deepcopy(row.json_value)
        state["generation"] += 1
        if change == "retire": state["visibility"] = "private"
        state["revision"] = digest({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
        row.json_value = state; db.session.commit()
        return result
    monkeypatch.setattr(pilot, "_answer", withdraw)
    assert process(document(), post=lambda *a, **k: pytest.fail("Withdrawn content must never send"))["accepted"] == 0
    assert db.session.query(MessagingEventLedger).one().external_status == "send_uncertain"
    assert process(document(), post=lambda *a, **k: pytest.fail("No replay of withdrawn answer"))["replayed"] == 1


@pytest.mark.parametrize("change", ["production", "foreign_tenant", "duplicate_phone", "wrong_waba", "missing_vault", "expired"])
def test_exact_persisted_sandbox_binding_fail_closed(environment, change):
    env = environment
    if change == "production": env.connection.environment = "production"
    if change == "foreign_tenant": env.sender.tenant_id = 47
    if change == "wrong_waba": env.sender.waba_id = "999999"
    if change == "missing_vault": env.connection.credentials_ref = "env:META_ACCESS_TOKEN"
    if change == "expired":
        env.connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(connection=env.connection, tenant_id=46,
            access_token=TOKEN, revision=1, expires_at=NOW, now=NOW - 1, app_config=env.app.config)}
    if change == "duplicate_phone":
        db.session.add(ProviderSender(tenant_id=46, provider_connection_id=env.connection.id, channel="whatsapp",
            phone_number_id=pilot.TEST_PHONE, waba_id=pilot.TEST_WABA, status="ONLINE"))
    db.session.commit()
    with pytest.raises((cloud.MetaContractError, pilot.PilotError, vault.ProviderCredentialError)):
        process(document(), post=lambda *args, **kwargs: pytest.fail("No send"))
    assert db.session.query(WebhookDelivery).count() == 0


def test_unknown_contact_or_other_number_has_no_effect(environment):
    with pytest.raises(pilot.PilotError, match="recipient_not_allowed"):
        process(document(contact="5491199999999"), post=lambda *a, **k: pytest.fail("No send"))
    foreign = document(); foreign["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "660753250460901"
    with pytest.raises(cloud.MetaContractError):
        process(foreign, post=lambda *a, **k: pytest.fail("JUNI never used"))
    assert db.session.query(WebhookDelivery).count() == 0


def test_real_knowledge_menu_replay_and_numeric_choice(environment, monkeypatch):
    # Exact-node/numeric navigation must not invoke an LLM selection.
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda *args, **kwargs: pytest.fail("No LLM"))
    sent = []
    def post(url, **kwargs):
        sent.append((url, deepcopy(kwargs["json"])))
        return accepted_post()
    first = process(document(), post=post)
    assert first["accepted"] == 1 and first["delivery_verified"] is False
    assert len(sent) == 1 and sent[0][0] == "https://graph.facebook.com/v25.0/" + pilot.TEST_PHONE + "/messages"
    assert "1. " in sent_body(sent[0][1]) and "MENÚ" in sent_body(sent[0][1])
    assert sent[0][1]["type"] == "interactive"
    row = sent[0][1]["interactive"]["action"]["sections"][0]["rows"][0]
    assert row["id"] == "knowledge:" + environment.state["revision"][:16] + ":requirements"
    assert row["title"].startswith("1. ")
    assert process(document(), post=post)["replayed"] == 1 and len(sent) == 1
    choice = document(mid="wamid.inbound2", content={"type": "text", "text": {"body": "1"}})
    assert process(choice, post=post)["accepted"] == 1
    assert "Respuesta institucional de prueba" in sent_body(sent[-1][1])
    assert db.session.query(WebhookDelivery).count() == 2
    assert db.session.query(MessagingEventLedger).count() == 2
    serialized = json.dumps([row.metadata_json for row in db.session.query(MessagingEventLedger)])
    assert CONTACT not in serialized and SECRET not in serialized and TOKEN not in serialized


def test_same_message_id_changed_content_conflicts_without_second_post(environment):
    process(document())
    changed = document(content={"type": "text", "text": {"body": "texto cambiado"}})
    with pytest.raises(pilot.PilotError, match="event_conflict"):
        process(changed, post=lambda *a, **k: pytest.fail("No second POST"))


def test_interactive_and_location_are_normalized_without_operational_mutation(environment):
    sent = []
    def post(url, **kwargs):
        sent.append(sent_body(kwargs["json"])); return accepted_post()
    action = "knowledge:" + environment.state["revision"][:16] + ":requirements"
    process(document(content={"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": action}}}), post=post)
    assert "Respuesta institucional de prueba" in sent[-1]
    process(document(mid="wamid.location", content={"type": "location", "location": {"latitude": -54.8, "longitude": -68.3}}), post=post)
    assert "Ushuaia, Río Grande o Tolhuin" in sent[-1]
    receipts = [(row.payload_digest, row.last_error) for row in db.session.query(WebhookDelivery)]
    assert "-54.8" not in str(receipts)
    assert all(row.payload is None for row in db.session.query(MessagingEventLedger))
    process(document(mid="wamid.foreignaction", content={"type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "crear_reclamo"}}}), post=post)
    assert "no corresponde" in sent[-1]


@pytest.mark.parametrize("latitude,longitude", [(91, 0), (0, 181), (True, 0), ("0", 0), (float("nan"), 0)])
def test_invalid_location_is_before_receipt(environment, latitude, longitude):
    with pytest.raises(cloud.MetaContractError):
        process(document(content={"type": "location", "location": {"latitude": latitude, "longitude": longitude}}))
    assert db.session.query(WebhookDelivery).count() == 0


def test_audio_does_not_promise_or_attempt_transcription(environment):
    bodies = []
    def post(url, **kwargs):
        bodies.append(sent_body(kwargs["json"])); return accepted_post()
    process(document(content={"type": "audio", "audio": {"id": "555555", "mime_type": "audio/ogg"}}), post=post)
    assert "todavía no transcribe audio" in bodies[0]
    assert "persona" in bodies[0]


def test_knowledge_list_preserves_complete_text_and_falls_back_without_hiding_choices(environment):
    revision = environment.state["revision"]
    buttons = tuple({"action_id": "knowledge:" + revision[:16] + ":topic" + str(i),
        "texto": "Tema " + str(i), "reply_code": str(i)} for i in range(1, 11))
    result = pilot._reply_payload(CONTACT, "Menú. Respondé un número o abrí la lista.", buttons, revision)
    assert result["type"] == "interactive"
    rows = result["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 10 and rows[-1]["id"] == buttons[-1]["action_id"]
    for body, choices in [("x" * 1025, buttons), ("Menú", buttons + (buttons[0],))]:
        fallback = pilot._reply_payload(CONTACT, body, choices, revision)
        assert fallback["type"] == "text" and fallback["text"]["body"] == body
    foreign = ({"action_id": "crear_reclamo", "texto": "Reclamo", "reply_code": "1"},)
    assert pilot._reply_payload(CONTACT, "Menú", foreign, revision)["type"] == "text"


@pytest.mark.parametrize("change", ["too_many", "duplicate_id", "long_title", "long_description", "foreign_key", "control_id"])
def test_cloud_list_contract_rejects_invalid_rows(change):
    rows = [{"id": "knowledge:synthetic:start", "title": "1. Tema"}]
    if change == "too_many": rows = [{"id": str(i), "title": "Tema"} for i in range(11)]
    elif change == "duplicate_id": rows.append(deepcopy(rows[0]))
    elif change == "long_title": rows[0]["title"] = "x" * 25
    elif change == "long_description": rows[0]["description"] = "x" * 73
    elif change == "foreign_key": rows[0]["url"] = "https://untrusted.invalid"
    elif change == "control_id": rows[0]["id"] = "knowledge:\nstart"
    with pytest.raises(cloud.MetaContractError, match="list_payload_invalid"):
        cloud.list_payload(recipient=CONTACT, body="Menú", rows=tuple(rows))


def test_timeout_is_durable_uncertain_and_never_retried(environment):
    calls = []
    def post(*args, **kwargs):
        assert db.session.query(MessagingEventLedger).one().external_status == "send_uncertain"
        calls.append(1); raise TimeoutError("must never expose synthetic token " + TOKEN)
    assert process(document(), post=post)["accepted"] == 0
    assert db.session.query(MessagingEventLedger).one().external_status == "uncertain"
    assert process(document(), post=post)["replayed"] == 1 and len(calls) == 1
    assert TOKEN not in str(db.session.query(WebhookDelivery).one().last_error)


def test_signed_delivery_reconciliation_is_contact_and_sender_bound_and_monotonic(environment):
    process(document())
    environment.app.config["META_TDF_SANDBOX_RECIPIENTS"].append("5491112345679")
    def status(state, contact=CONTACT):
        doc = document(); value = doc["entry"][0]["changes"][0]["value"]
        value.pop("messages"); value["statuses"] = [{"id": "wamid.reply1", "timestamp": str(NOW), "recipient_id": contact, "status": state}]
        return doc
    assert process(status("sent", "5491112345679"))["statuses"] == 1
    assert db.session.query(MessagingEventLedger).one().external_status == "accepted"
    process(status("delivered"))
    assert db.session.query(MessagingEventLedger).one().external_status == "delivered"
    process(status("read")); process(status("failed"))
    assert db.session.query(MessagingEventLedger).one().external_status == "read"


def test_writer_fence_and_out_of_window_do_not_send(environment):
    environment.app.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
    with pytest.raises(pilot.PilotError, match="writer_fenced"):
        process(document())
    assert db.session.query(WebhookDelivery).count() == 0
    environment.app.config["CUTOVER_WRITER_FENCE_ENABLED"] = False
    assert process(document(timestamp=NOW - 86400), post=lambda *a, **k: pytest.fail("Window expired"))["accepted"] == 0
    assert db.session.query(MessagingEventLedger).count() == 0


def test_graph_authority_requires_exact_app_scope_phone_and_subscription(environment):
    cfg = pilot.settings(environment.app.config)
    sender = cloud.MetaSenderSnapshot(46, 1, 1, pilot.TEST_APP, pilot.TEST_WABA, pilot.TEST_PHONE, "sandbox")
    credential = vault.MetaCredential(1, NOW + 3600, TOKEN)
    bodies = [
        {"data": {"is_valid": True, "app_id": pilot.TEST_APP, "expires_at": NOW + 300,
                  "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"]}},
        {"data": [{"id": pilot.TEST_PHONE}]},
        {"data": [{"whatsapp_business_api_data": {"id": pilot.TEST_APP}}]},
    ]
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs)); body = bodies[len(calls) - 1]
        return SimpleNamespace(status_code=200, content=json.dumps(body).encode())
    verifier = pilot.GraphAuthority(cfg, NOW, request_adapter=request)
    result = verifier(sender, credential)
    assert result.messaging_permitted and result.webhook_subscribed and len(calls) == 3
    assert all(call[1].startswith("https://graph.facebook.com/v25.0/") for call in calls)
    assert all(call[2]["verify"] is True and call[2]["allow_redirects"] is False for call in calls)
    assert verifier(sender, credential) is result and len(calls) == 3
    bodies[0]["data"]["app_id"] = "999999"
    with pytest.raises(pilot.PilotError, match="token_authority_invalid"):
        pilot.GraphAuthority(cfg, NOW, request_adapter=lambda *a, **k: SimpleNamespace(status_code=200, content=json.dumps(bodies[0]).encode()))(sender, credential)


def test_runtime_transport_uses_tls_without_url_debug_logging_or_redirects(monkeypatch, capsys):
    calls, connections = [], []
    class Connection:
        def __init__(self, host, *, timeout, context):
            import ssl
            assert host == "graph.facebook.com" and timeout == 3.0
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            self.sock = SimpleNamespace(settimeout=lambda value: None)
            self.closed = False; connections.append(self)
        def set_debuglevel(self, value): assert value == 0
        def connect(self): pass
        def request(self, method, path, **kwargs): calls.append((method, path))
        def getresponse(self):
            chunks = iter([b'{"data":{}}', b''])
            return SimpleNamespace(status=302, read1=lambda size: next(chunks))
        def close(self): self.closed = True
    monkeypatch.setattr(pilot.http.client, "HTTPSConnection", Connection)
    response = pilot._graph_request("GET", "https://graph.facebook.com/v25.0/debug_token",
        params={"input_token": TOKEN}, headers={"Authorization": "Bearer synthetic"},
        allow_redirects=False, verify=True)
    assert response.status_code == 302 and len(calls) == 1 and connections[0].closed
    assert "input_token=" in calls[0][1]
    output = capsys.readouterr(); assert TOKEN not in output.out + output.err
    with pytest.raises(pilot.PilotError, match="authority_unavailable"):
        pilot._json_response(response)
    with pytest.raises(pilot.PilotError, match="transport_invalid"):
        pilot._graph_request("GET", "https://untrusted.invalid/v25.0/debug_token",
                             allow_redirects=False, verify=True)
    assert len(connections) == 1


def test_runtime_transport_limits_stream_and_closes_on_failure(monkeypatch):
    connections = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = SimpleNamespace(settimeout=lambda value: None)
            self.closed = False; connections.append(self)
        def set_debuglevel(self, value): assert value == 0
        def connect(self): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self):
            return SimpleNamespace(status=200, read1=lambda size: b'x' * size)
        def close(self): self.closed = True
    monkeypatch.setattr(pilot.http.client, "HTTPSConnection", Connection)
    with pytest.raises(pilot.PilotError, match="response_too_large"):
        pilot._graph_request("GET", "https://graph.facebook.com/v25.0/debug_token",
                             allow_redirects=False, verify=True)
    assert len(connections) == 1 and connections[0].closed
