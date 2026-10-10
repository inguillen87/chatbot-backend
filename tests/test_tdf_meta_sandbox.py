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


BSUID, PARENT_BSUID = "AR.SyntheticOwner123", "AR.ENT.SyntheticParent123"
APPROVED_REPLY_INPUT = "11122233344"  # Synthetic provider input, no prefix rule.


def bsuid_document(*, uid=BSUID, parent=None, **kwargs):
    doc = document(**kwargs)
    value = doc["entry"][0]["changes"][0]["value"]
    message = value["messages"][0]
    message["from_user_id"] = uid
    contact = {"wa_id": message.get("from"), "user_id": uid,
               "profile": {"name": "Synthetic private display name", "username": "synthetic-private"}}
    if parent is not None:
        message["from_parent_user_id"] = parent
        contact["parent_user_id"] = parent
    value["contacts"] = [contact]
    return doc


def bsuid_status(state, *, uid=BSUID, parent=None, phone=CONTACT):
    doc = bsuid_document(uid=uid, parent=parent)
    value = doc["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value["statuses"] = [{"id": "wamid.reply1", "timestamp": str(NOW),
                          "recipient_id": phone, "recipient_user_id": uid, "status": state}]
    if parent is not None:
        value["statuses"][0]["recipient_parent_user_id"] = parent
    value["contacts"][0]["wa_id"] = phone
    return doc


def test_explicit_proven_owner_mapping_changes_only_outbound_to(environment):
    environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({CONTACT: APPROVED_REPLY_INPUT})
    raw, signature = encoded(bsuid_document(parent=PARENT_BSUID))
    cfg = pilot.settings(environment.app.config)
    loader = pilot.binding_loader(cfg, NOW, authority)
    event, = pilot.webhook.parse_webhook(raw_body=raw, signature=signature, app_secret=SECRET,
        app_id=pilot.TEST_APP, now=NOW, binding_resolver=lambda *_: loader(),
        contact_identity_resolver=lambda item, kind, contacts, sender:
            pilot.owner_contact_resolver(sender_scope=sender, recipients=cfg["RECIPIENTS"])(item, kind, contacts, sender))
    key = pilot._contact_key(event, cfg)
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return accepted_post(url, **kwargs)
    assert process(bsuid_document(parent=PARENT_BSUID), post=post)["accepted"] == 1
    assert calls[0]["to"] == APPROVED_REPLY_INPUT
    assert event.contact == CONTACT and event.contact_identity.user_id == BSUID
    ledger = db.session.query(MessagingEventLedger).one()
    assert ledger.request_id == key and ledger.payload is None
    stored = str(ledger.metadata_json) + ledger.request_id + str(ledger.payload)
    for private in (CONTACT, APPROVED_REPLY_INPUT, BSUID, PARENT_BSUID):
        assert private not in stored
    assert process(bsuid_document(parent=PARENT_BSUID), post=post)["replayed"] == 1
    assert len(calls) == 1
    assert process(bsuid_status("delivered", parent=PARENT_BSUID))["statuses"] == 1
    assert ledger.external_status == "delivered"


@pytest.mark.parametrize("raw", [
    "", "{}", "[]", "null", "false", "broken", "x" * 513,
    json.dumps({CONTACT: "+11122233344"}), json.dumps({CONTACT: 11122233344}),
    json.dumps({CONTACT: None}), json.dumps({"11122233344": APPROVED_REPLY_INPUT}),
    json.dumps({CONTACT: APPROVED_REPLY_INPUT, "11122233344": APPROVED_REPLY_INPUT}),
    '{"'+CONTACT+'":"11122233344","'+CONTACT+'":"22233344455"}',
])
def test_invalid_reply_mapping_has_no_claim_or_provider_post(environment, raw):
    environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = raw
    calls = []
    with pytest.raises(pilot.PilotError, match="tdf_sandbox_reply_mapping_invalid"):
        process(bsuid_document(), post=lambda *a, **kw: calls.append(1))
    assert calls == [] and db.session.query(WebhookDelivery).count() == 0
    assert db.session.query(MessagingEventLedger).count() == 0


def test_reply_mapping_requires_single_canonical_allowlist(environment):
    environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({CONTACT: APPROVED_REPLY_INPUT})
    environment.app.config["META_TDF_SANDBOX_RECIPIENTS"] = [CONTACT, "22233344455"]
    with pytest.raises(pilot.PilotError, match="tdf_sandbox_reply_mapping_invalid"):
        pilot.settings(environment.app.config)


@pytest.mark.parametrize("change", ["target", "removed", "allowlist"])
def test_mapping_changed_after_durable_intent_cannot_send(environment, monkeypatch, change):
    environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({CONTACT: APPROVED_REPLY_INPUT})
    original = cloud.send_once
    calls = []
    def send_once(**kwargs):
        assert db.session.query(MessagingEventLedger).one().external_status == "send_uncertain"
        if change == "target":
            environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({CONTACT: "22233344455"})
        elif change == "removed":
            environment.app.config.pop("META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON")
        else:
            environment.app.config["META_TDF_SANDBOX_RECIPIENTS"] = ["22233344455"]
            environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({"22233344455": APPROVED_REPLY_INPUT})
        return original(**kwargs)
    monkeypatch.setattr(cloud, "send_once", send_once)
    result = process(bsuid_document(), post=lambda *a, **kw: calls.append(1))
    assert result["accepted"] == 0 and calls == []
    assert db.session.query(MessagingEventLedger).one().external_status == "send_uncertain"
    assert db.session.query(WebhookDelivery).one().status == "failed"


def test_reply_mapping_never_authorizes_original_input_as_inbound(environment):
    environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({CONTACT: APPROVED_REPLY_INPUT})
    calls = []
    with pytest.raises(cloud.MetaContractError, match="phone_not_allowed"):
        process(bsuid_document(contact=APPROVED_REPLY_INPUT), post=lambda *a, **kw: calls.append(1))
    assert calls == [] and db.session.query(WebhookDelivery).count() == 0


def test_adding_reply_mapping_does_not_retry_historical_rejected_message(environment):
    calls = []
    def rejected(url, **kwargs):
        calls.append(kwargs["json"])
        return SimpleNamespace(status_code=400,content=b'{"error":{"code":131030}}')
    old = bsuid_document(mid="wamid.synthetic-rejected-history")
    assert process(old, post=rejected)["accepted"] == 0
    ledger = db.session.query(MessagingEventLedger).one()
    assert ledger.external_status == "rejected" and ledger.error_code == "131030"
    environment.app.config["META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON"] = json.dumps({CONTACT: APPROVED_REPLY_INPUT})
    accepted_calls = []
    def counted_accepted(url, **kwargs):
        accepted_calls.append(kwargs["json"])
        return accepted_post(url, **kwargs)
    assert process(old, post=counted_accepted)["replayed"] == 1
    assert accepted_calls == []
    assert len(calls) == 1 and db.session.query(MessagingEventLedger).count() == 1
    assert process(bsuid_document(mid="wamid.synthetic-new-after-mapping"), post=counted_accepted)["accepted"] == 1
    assert len(accepted_calls) == 1 and accepted_calls[0]["to"] == APPROVED_REPLY_INPUT
    assert db.session.query(MessagingEventLedger).count() == 2


@pytest.mark.parametrize("parent", [None, PARENT_BSUID])
def test_signed_bsuid_owner_association_is_preserved_and_durable_without_pii(environment, parent):
    from services.tdf_meta_contact_identity import owner_contact_resolver
    doc = bsuid_document(parent=parent)
    raw, signature = encoded(doc)
    cfg = pilot.settings(environment.app.config)
    loader = pilot.binding_loader(cfg, NOW, authority)
    event, = pilot.webhook.parse_webhook(raw_body=raw, signature=signature, app_secret=SECRET,
        app_id=pilot.TEST_APP, now=NOW, binding_resolver=lambda *_: loader(),
        contact_identity_resolver=lambda item, kind, contacts, sender:
            owner_contact_resolver(sender_scope=sender, recipients=cfg["RECIPIENTS"])(item, kind, contacts, sender))
    assert event.contact_identity.user_id == BSUID and event.contact_identity.parent_user_id == parent
    assert event.contact == CONTACT and BSUID not in repr(event) and CONTACT not in repr(event)
    posts = []
    def post(url, **kwargs):
        posts.append(kwargs["json"])
        return accepted_post(url, **kwargs)
    assert process(doc, post=post)["accepted"] == 1
    assert posts[0]["to"] == CONTACT and "recipient" not in posts[0]
    assert process(doc, post=post)["replayed"] == 1 and len(posts) == 1
    ledger = db.session.query(MessagingEventLedger).one()
    assert ledger.request_id == pilot._contact_key(event, cfg)
    assert ledger.request_id != pilot._pseudonym(CONTACT, cfg)
    serialized = json.dumps(ledger.metadata_json) + str(ledger.payload) + ledger.request_id
    delivery = db.session.query(WebhookDelivery).one()
    serialized += delivery.payload_digest + delivery.event_id + str(delivery.last_error)
    for private in (BSUID, CONTACT, PARENT_BSUID, "Synthetic private display name", "synthetic-private"):
        assert private not in serialized
    assert process(bsuid_status("delivered", parent=parent))["statuses"] == 1
    assert ledger.external_status == "delivered"


@pytest.mark.parametrize("case,reason", [
    ("bsuid_only", "identity_unavailable"), ("empty_phone", "identity_unavailable"),
    ("foreign_phone", "phone_not_allowed"), ("argentine_prefix_change", "phone_not_allowed"),
    ("missing_contacts", "contact_conflict"), ("duplicate_contacts", "contact_conflict"),
    ("conflicting_wa_id", "contact_conflict"), ("conflicting_user_id", "contact_conflict"),
    ("missing_message_bsuid", "schema_invalid"), ("invalid_bsuid", "schema_invalid"),
    ("parent_only", "schema_invalid"), ("parent_mismatch", "contact_conflict"),
    ("parent_missing_contact", "contact_conflict"), ("parent_country_conflict", "schema_invalid"),
    ("unknown_item_identity", "schema_invalid"), ("group", "schema_invalid"),
])
def test_bsuid_unknown_or_conflicting_owner_identity_has_no_receipt_or_post(environment, case, reason):
    doc = bsuid_document(parent=PARENT_BSUID)
    value = doc["entry"][0]["changes"][0]["value"]
    item, contact = value["messages"][0], value["contacts"][0]
    if case == "bsuid_only": item.pop("from"); contact.pop("wa_id")
    elif case == "empty_phone": item["from"] = contact["wa_id"] = ""
    elif case == "foreign_phone": item["from"] = contact["wa_id"] = "5491112345699"
    elif case == "argentine_prefix_change": item["from"] = contact["wa_id"] = CONTACT.replace("549", "54", 1)
    elif case == "missing_contacts": value.pop("contacts")
    elif case == "duplicate_contacts": value["contacts"].append(deepcopy(contact))
    elif case == "conflicting_wa_id": contact["wa_id"] = "5491112345699"
    elif case == "conflicting_user_id": contact["user_id"] = "AR.OtherOwner123"
    elif case == "missing_message_bsuid": item.pop("from_user_id")
    elif case == "invalid_bsuid": item["from_user_id"] = contact["user_id"] = "ar.not-country-format"
    elif case == "parent_only": item.pop("from_user_id"); contact.pop("user_id")
    elif case == "parent_mismatch": contact["parent_user_id"] = "AR.ENT.OtherParent123"
    elif case == "parent_missing_contact": contact.pop("parent_user_id")
    elif case == "parent_country_conflict": item["from_parent_user_id"] = contact["parent_user_id"] = "US.ENT.OtherParent123"
    elif case == "unknown_item_identity": item["user_id"] = BSUID
    elif case == "group": item["group_id"] = "SyntheticGroup"
    calls = []
    with pytest.raises(cloud.MetaContractError, match=reason):
        process(doc, post=lambda *a, **k: calls.append(1))
    assert calls == [] and db.session.query(WebhookDelivery).count() == 0
    assert db.session.query(MessagingEventLedger).count() == 0


def test_bsuid_change_cannot_replay_or_reconcile_another_identity(environment):
    posts = []
    def post(url, **kwargs):
        posts.append(1)
        return accepted_post(url, **kwargs)
    assert process(bsuid_document(), post=post)["accepted"] == 1
    with pytest.raises(pilot.PilotError, match="event_conflict"):
        process(bsuid_document(uid="AR.ChangedOwner123"), post=post)
    assert posts == [1]
    assert process(bsuid_status("delivered", uid="AR.ChangedOwner123"))["statuses"] == 1
    assert db.session.query(MessagingEventLedger).one().external_status == "accepted"
    assert process(bsuid_status("read"))["statuses"] == 1
    assert db.session.query(MessagingEventLedger).one().external_status == "read"


def test_bsuid_adapter_is_never_called_before_signature_and_exact_test_binding(environment, monkeypatch):
    calls = []
    original = pilot.owner_contact_resolver
    monkeypatch.setattr(pilot, "owner_contact_resolver", lambda **kwargs: (calls.append(1), original(**kwargs))[1])
    doc = bsuid_document()
    raw, signature = encoded(doc)
    with pytest.raises(cloud.MetaContractError, match="signature_invalid"):
        pilot.process(raw, "sha256=" + "0" * 64, now=NOW, authority=authority)
    doc["entry"][0]["id"] = "10068207913300410"  # Existing customer WABA must never bind to the pilot.
    with pytest.raises(cloud.MetaContractError, match="binding_unavailable"):
        process(doc)
    assert calls == [] and db.session.query(WebhookDelivery).count() == 0


@pytest.mark.parametrize("kind", ["message", "status"])
def test_bsuid_denial_http_logs_only_fixed_code(environment, monkeypatch, caplog, kind):
    doc = bsuid_document(contact="5491112345699") if kind == "message" else bsuid_status("delivered", phone="5491112345699")
    monkeypatch.setattr(pilot, "GraphAuthority", lambda *_: authority)
    monkeypatch.setattr(pilot.time, "time", lambda: NOW)
    raw, signature = encoded(doc)
    response = environment.client.post(URL, data=raw, content_type="application/json",
        headers={"X-Hub-Signature-256": signature})
    assert response.status_code == 403
    assert "reason=meta_webhook_contact_phone_not_allowed" in caplog.text
    for private in (BSUID, CONTACT, "5491112345699", SECRET, TOKEN, signature):
        assert private not in caplog.text
    assert db.session.query(WebhookDelivery).count() == 0


@pytest.mark.parametrize("changed,value", [
    ("tenant_id", 1), ("app_id", "1255104043465364"), ("waba_id", "10068207913300410"),
    ("phone_number_id", "660753250460901"), ("environment", "production"),
])
def test_owner_identity_adapter_rejects_other_sender_namespaces(changed, value):
    from dataclasses import replace
    from services.tdf_meta_contact_identity import owner_contact_resolver
    sender = cloud.MetaSenderSnapshot(46, 1, 1, pilot.TEST_APP, pilot.TEST_WABA, pilot.TEST_PHONE, "sandbox")
    with pytest.raises(cloud.MetaContractError, match="schema_invalid"):
        owner_contact_resolver(sender_scope=replace(sender, **{changed: value}), recipients=[CONTACT])


def test_legacy_contacts_cannot_conflict_and_failed_without_bsuid_stays_explicit(environment):
    legacy = document()
    value = legacy["entry"][0]["changes"][0]["value"]
    value["contacts"] = [{"wa_id": "5491112345699"}]
    with pytest.raises(cloud.MetaContractError, match="contact_conflict"):
        process(legacy)
    assert db.session.query(WebhookDelivery).count() == 0
    assert process(bsuid_document())["accepted"] == 1
    failed = bsuid_status("failed")
    value = failed["entry"][0]["changes"][0]["value"]
    value.pop("contacts")
    value["statuses"][0].pop("recipient_user_id")
    assert process(failed)["statuses"] == 1
    assert db.session.query(MessagingEventLedger).one().external_status == "accepted"


def test_signed_bsuid_identity_survives_accessible_audio_and_selection(environment, monkeypatch):
    posts = []
    def post(url, **kwargs):
        posts.append(kwargs["json"])
        return accepted_post(url, **kwargs)
    assert process(bsuid_document(), post=post)["accepted"] == 1
    action = posts[0]["interactive"]["action"]["sections"][0]["rows"][0]["id"]
    assert process(bsuid_document(mid="wamid.bsuidselection", content={"type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": action}}}), post=post)["accepted"] == 1
    seen = []
    def transcribe(event, **kwargs):
        seen.append(event.contact_identity.user_id)
        return "menu"
    monkeypatch.setattr(pilot, "transcribe_meta_voice_note", transcribe)
    assert process(bsuid_document(mid="wamid.bsuidvoice", content={"type": "audio", "audio": {
        "id": "555555", "mime_type": "audio/ogg"}}), post=post)["accepted"] == 1
    assert seen == [BSUID] and len(posts) == 3
    assert all(payload["to"] == CONTACT for payload in posts)
    assert len({row.request_id for row in db.session.query(MessagingEventLedger)}) == 1


@pytest.mark.parametrize("error,expected", [
    (pilot.PilotError("tdf_sandbox_recipient_not_allowed", 403), "tdf_sandbox_recipient_not_allowed"),
    (cloud.MetaContractError("meta_webhook_signature_invalid"), "meta_webhook_signature_invalid"),
    (cloud.MetaContractError("meta_webhook_payload_invalid"), "meta_webhook_payload_invalid"),
    (cloud.MetaContractError("private exception " + CONTACT + " " + TOKEN), "authentication_or_binding_denied"),
])
def test_post_denial_log_has_only_fixed_reason(environment, monkeypatch, caplog, error, expected):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(pilot, "process", fail)
    raw, signature = encoded(document())
    response = environment.client.post(URL, data=raw, content_type="application/json",
        headers={"X-Hub-Signature-256": signature})
    assert response.status_code == 403
    assert "Meta TDF incoming POST rejected reason=" + expected in caplog.text
    assert CONTACT not in caplog.text and SECRET not in caplog.text and TOKEN not in caplog.text
    assert signature not in caplog.text and "private exception" not in caplog.text


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


def publish_presentation_fixture(environment, raw):
    """Synthetic in-memory published content, never a provider/production write."""
    bundle = normalize_bundle(raw, 46, pilot.TENANT_SLUG)
    state = {"bundle": bundle, "bundle_hash": digest(bundle), "generation": 2, "visibility": "public"}
    state["revision"] = digest({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
    row = TenantConfig.query.filter_by(tenant_id=46, key="institutional_assistant", channel="knowledge").one()
    row.json_value = state
    db.session.commit()
    environment.state = state
    return state


def public_presentation_fixture():
    raw = sample(46, pilot.TENANT_SLUG)
    raw["sources"]["a"].update(document_visibility="public", review_status="needs_review",
        official_url="https://example.org/public-document.pdf", provenance="INTERNAL PROVENANCE",
        native_revision="INTERNAL REVISION", origin_url="https://drive.google.com/file/d/private/view")
    raw["reference_links"] = {"tenant": raw["tenant"], "entries": [
        {"id": "official", "label": "Contacto oficial", "status": "verified_reference",
         "url": "https://example.org/contact", "review_after": "2099-01-01T00:00:00Z", "node_ids": ["requirements"]}]}
    return raw


def test_progressive_greeting_preserves_six_choices_and_shared_web_body(environment, monkeypatch):
    raw = public_presentation_fixture()
    original = deepcopy(raw["nodes"]["requirements"])
    for index in range(2, 7):
        node_id = "topic" + str(index)
        node = deepcopy(original); node["id"] = node_id
        raw["nodes"][node_id] = node
        raw["node_evidence"][node_id] = deepcopy(raw["node_evidence"]["requirements"])
        raw["nodes"]["start"]["actions"].append({"code": str(index), "label": "Tema " + str(index), "target": node_id})
    state = publish_presentation_fixture(environment, raw)
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda *a: pytest.fail("Explicit menus need no selector"))
    sent = []
    process(document(), post=lambda url, **kw: sent.append(deepcopy(kw["json"])) or accepted_post())
    body = sent_body(sent[0])
    assert "Elegí una consulta." in body and "FUENTES" in body and "revisión pendiente" in body
    assert "Documento de prueba" not in body and "Documentos y fuentes" not in body
    assert sent[0]["type"] == "interactive" and len(body) <= 1024
    rows = sent[0]["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 6
    for index, row in enumerate(rows, 1):
        assert row["title"].startswith(str(index) + ". ") and str(index) + ". " in body
        expected_target = "requirements" if index == 1 else "topic" + str(index)
        assert row["id"] == "knowledge:" + state["revision"][:16] + ":" + expected_target
    from services.institutional_assistant import maybe_handle_institutional_question
    shared = maybe_handle_institutional_question("menu", environment.owner,
        SimpleNamespace(tenant_id=46, context_data={}))
    assert "Documentos y fuentes" in shared["message_body"] and "Documento de prueba" in shared["message_body"]


def test_explicit_sources_keep_current_scope_links_and_numeric_navigation(environment, monkeypatch):
    publish_presentation_fixture(environment, public_presentation_fixture())
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda *a: pytest.fail("No selector for FUENTES"))
    sent = []
    def post(url, **kw):
        sent.append(deepcopy(kw["json"])); return accepted_post()
    process(document(), post=post)
    process(document(mid="wamid.choose", content={"type": "text", "text": {"body": "1"}}), post=post)
    assert "Respuesta institucional de prueba" in sent_body(sent[-1])
    assert "https://example.org/contact" in sent_body(sent[-1])
    assert "Documento de prueba" not in sent_body(sent[-1])
    source_doc = document(mid="wamid.sources", content={"type": "text", "text": {"body": " FUENTES "}})
    assert process(source_doc, post=post)["accepted"] == 1
    body = sent_body(sent[-1])
    assert "Documento de prueba" in body and "páginas 2" in body and "revisión pendiente" in body
    assert "https://example.org/public-document.pdf" in body and "https://example.org/contact" in body
    for private in ("INTERNAL PROVENANCE", "INTERNAL REVISION", "drive.google.com", "a" * 64):
        assert private not in body
    assert sent[-1]["type"] == "text"
    assert process(source_doc, post=lambda *a, **k: pytest.fail("Sources replay must not POST"))["replayed"] == 1
    process(document(mid="wamid.return", content={"type": "text", "text": {"body": "9"}}), post=post)
    assert "Elegí una consulta." in sent_body(sent[-1]) and "1. Requisitos" in sent_body(sent[-1])
    context = db.session.query(MessagingEventLedger).order_by(MessagingEventLedger.id.desc()).first().metadata_json["knowledge_context"]
    from services.institutional_assistant_whatsapp import SOURCE_SCOPE
    assert context[SOURCE_SCOPE] == {"tenant": {"id": 46, "slug": pilot.TENANT_SLUG},
                                    "revision": environment.state["revision"], "node_ids": ["start"]}
    assert CONTACT not in json.dumps(context) and "Documento de prueba" not in json.dumps(context)


@pytest.mark.parametrize("visibility", ["private", None])
def test_sources_never_expose_private_or_unclassified_original_metadata(environment, visibility):
    raw = public_presentation_fixture()
    raw["sources"]["a"].update(title="PRIVATE ORIGINAL TITLE", review_status="conflict")
    if visibility is None:
        raw["sources"]["a"].pop("document_visibility")
    else:
        raw["sources"]["a"]["document_visibility"] = visibility
    publish_presentation_fixture(environment, raw)
    event = SimpleNamespace(content_type="text", text="menu")
    body, context, _, _ = pilot._answer(event, {})
    assert "PRIVATE ORIGINAL TITLE" not in body and "diferencias" not in body
    event.text = "FUENTES"
    body, _, revision, _ = pilot._answer(event, context)
    assert "No hay referencias públicas" in body and revision == environment.state["revision"]
    for private in ("PRIVATE ORIGINAL TITLE", "conflict", "provenance", "drive.google.com", "public-document.pdf"):
        assert private not in body


@pytest.mark.parametrize("change", ["no_context", "foreign_tenant", "foreign_slug", "unknown_node", "wrong_last_node", "extra_node", "revision", "retired", "replaced"])
def test_sources_fail_closed_for_stale_foreign_or_missing_current_scope(environment, change):
    publish_presentation_fixture(environment, public_presentation_fixture())
    event = SimpleNamespace(content_type="text", text="menu")
    _, context, _, _ = pilot._answer(event, {})
    from services.institutional_assistant_whatsapp import SOURCE_SCOPE
    if change == "no_context": context = {}
    if change == "foreign_tenant": context[SOURCE_SCOPE]["tenant"]["id"] = 47
    if change == "foreign_slug": context[SOURCE_SCOPE]["tenant"]["slug"] = "foreign"
    if change == "unknown_node": context[SOURCE_SCOPE]["node_ids"] = ["foreign"]
    if change == "wrong_last_node": context[SOURCE_SCOPE]["node_ids"] = ["requirements"]
    if change == "extra_node": context[SOURCE_SCOPE]["node_ids"] = ["requirements", "start", "requirements", "start"]
    if change == "revision": context[SOURCE_SCOPE]["revision"] = "0" * 64
    if change in ("retired", "replaced"):
        row = TenantConfig.query.filter_by(tenant_id=46, key="institutional_assistant", channel="knowledge").one()
        state = deepcopy(row.json_value); state["generation"] += 1
        if change == "retired": state["visibility"] = "private"
        state["revision"] = digest({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
        row.json_value = state; db.session.commit()
    event.text = "FUENTES"
    body, next_context, revision, choices = pilot._answer(event, context)
    assert "Escribí MENÚ para elegir" in body and revision is None and choices == ()
    assert SOURCE_SCOPE not in next_context
    assert "Documento de prueba" not in body and "public-document.pdf" not in body


@pytest.mark.parametrize("change", ["retired", "replaced"])
def test_sources_revision_change_before_post_never_sends_or_retries(environment, monkeypatch, change):
    publish_presentation_fixture(environment, public_presentation_fixture())
    process(document())
    original = pilot._answer
    def withdraw(event, context, **kwargs):
        result = original(event, context, **kwargs)
        row = TenantConfig.query.filter_by(tenant_id=46, key="institutional_assistant", channel="knowledge").one()
        state = deepcopy(row.json_value); state["generation"] += 1
        if change == "retired": state["visibility"] = "private"
        state["revision"] = digest({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
        row.json_value = state; db.session.commit()
        return result
    monkeypatch.setattr(pilot, "_answer", withdraw)
    source_doc = document(mid="wamid.latesources", content={"type": "text", "text": {"body": "FUENTES"}})
    assert process(source_doc, post=lambda *a, **k: pytest.fail("Withdrawn sources must never send"))["accepted"] == 0
    last = db.session.query(MessagingEventLedger).order_by(MessagingEventLedger.id.desc()).first()
    assert last.external_status == "send_uncertain"
    assert process(source_doc, post=lambda *a, **k: pytest.fail("No replay of withdrawn sources"))["replayed"] == 1


def test_location_and_noncanonical_actions_clear_source_scope(environment):
    event = SimpleNamespace(content_type="text", text="menu")
    _, context, _, _ = pilot._answer(event, {})
    from services.institutional_assistant_whatsapp import SOURCE_SCOPE
    assert SOURCE_SCOPE in context
    for event in (SimpleNamespace(content_type="location"),
                  SimpleNamespace(content_type="interactive", selection="create_ticket")):
        _, next_context, revision, _ = pilot._answer(event, context)
        assert SOURCE_SCOPE not in next_context and revision is None


@pytest.mark.parametrize("question", ["FUENTES requisitos", "FUENTES 0", "FUENTES 1000", "FUENTES 1 extra", "knowledge:sources:start"])
def test_source_command_is_explicit_without_node_ids_or_intent_inference(question):
    from services.institutional_assistant_whatsapp import sources_page
    assert sources_page(question) is None


def test_sources_cover_only_all_nodes_of_last_multi_node_answer(environment, monkeypatch):
    raw = public_presentation_fixture()
    raw["sources"]["b"] = {"id": "b", "title": "Segunda referencia pública", "sha256": "b" * 64,
        "page_count": 1, "document_visibility": "public"}
    raw["node_evidence"]["requirements"] = [{"source_id": "b", "page": 1}]
    publish_presentation_fixture(environment, raw)
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda bundle, *a:
        [deepcopy(bundle["nodes"]["start"]), deepcopy(bundle["nodes"]["requirements"])])
    event = SimpleNamespace(content_type="text", text="Consulta sintética de dos temas")
    _, context, _, _ = pilot._answer(event, {})
    from services.institutional_assistant_whatsapp import SOURCE_SCOPE
    assert context[SOURCE_SCOPE]["node_ids"] == ["start", "requirements"]
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda *a: pytest.fail("FUENTES must reread IDs"))
    event.text = "FUENTES"
    body, _, revision, _ = pilot._answer(event, context)
    assert "Documento de prueba" in body and "Segunda referencia pública" in body
    assert revision == environment.state["revision"]


def test_sources_merge_every_page_when_last_nodes_cite_the_same_public_document(environment, monkeypatch):
    publish_presentation_fixture(environment, public_presentation_fixture())
    persisted_before = deepcopy(environment.state)
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda bundle, *a:
        [deepcopy(bundle["nodes"]["start"]), deepcopy(bundle["nodes"]["requirements"])])
    event = SimpleNamespace(content_type="text", text="Dos temas documentados")
    _, context, _, _ = pilot._answer(event, {})
    monkeypatch.setattr("services.institutional_assistant.select_nodes", lambda *a: pytest.fail("No new selection"))
    event.text = "FUENTES"
    body, _, revision, _ = pilot._answer(event, context)
    assert body.count("Documento de prueba") == 1 and "páginas 1, 2" in body
    assert revision == environment.state["revision"]
    row = TenantConfig.query.filter_by(tenant_id=46, key="institutional_assistant", channel="knowledge").one()
    assert row.json_value == persisted_before  # Presentation cannot mutate stored evidence.


def test_sources_paginate_without_truncating_public_references(environment):
    raw = public_presentation_fixture()
    raw["node_evidence"]["start"] = []
    raw["node_evidence"]["requirements"] = [{"source_id": "a", "page": 1}]
    for index, key in enumerate(("a", "b", "c"), 1):
        raw["sources"][key] = {"id": key, "title": "Referencia pública " + str(index), "sha256": key * 64,
            "page_count": 1, "document_visibility": "public", "official_url": "https://example.org/" + key * 1600}
        raw["node_evidence"]["start"].append({"source_id": key, "page": 1})
    publish_presentation_fixture(environment, raw)
    event = SimpleNamespace(content_type="text", text="menu")
    _, context, _, _ = pilot._answer(event, {})
    for index, key in enumerate(("a", "b", "c"), 1):
        event.text = "FUENTES" if index == 1 else "FUENTES " + str(index)
        body, context, revision, _ = pilot._answer(event, context)
        assert len(body) <= 3500 and "Referencia pública " + str(index) in body
        assert "https://example.org/" + key * 1600 in body and revision == environment.state["revision"]
        if index < 3: assert "FUENTES " + str(index + 1) in body
    event.text = "FUENTES 4"
    body, _, _, _ = pilot._answer(event, context)
    assert "Ese grupo de referencias no está disponible" in body


def test_list_titles_preserve_codes_whole_words_and_complete_text_alternative(environment):
    revision = environment.state["revision"]
    labels = ["Certificado de discapacidad y vigencia", "Certificado de discapacidad y renovación",
              "👩🏽‍🦽" * 8 + " orientación", "A\u0301" * 40 + " documento"]
    buttons = tuple({"action_id": "knowledge:" + revision[:16] + ":topic" + str(index),
        "texto": label, "reply_code": str(index)} for index, label in enumerate(labels, 1))
    body = "\n".join(str(index) + ". " + label for index, label in enumerate(labels, 1))
    result = pilot._reply_payload(CONTACT, body, buttons, revision)
    rows = result["interactive"]["action"]["sections"][0]["rows"]
    assert sent_body(result) == body and len({row["title"] for row in rows}) == 4
    for index, row in enumerate(rows, 1):
        assert row["id"] == buttons[index - 1]["action_id"] and row["title"].startswith(str(index) + ". ")
        assert len(row["title"]) <= 24 and len(row["description"]) <= 72
        assert row["title"].endswith("…")
    assert rows[0]["title"] == "1. Certificado de…" and rows[0]["description"] == labels[0]
    assert rows[2]["title"] == "3. …" and rows[3]["title"] == "4. …"
    unnumbered = tuple({key: value for key, value in button.items() if key != "reply_code"} for button in buttons[:2])
    fallback = pilot._reply_payload(CONTACT, body, unnumbered, revision)
    assert fallback["type"] == "text" and fallback["text"]["body"] == body


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


def challenge_params(environment):
    return {"hub.mode": "subscribe", "hub.verify_token": environment.app.config["META_TDF_SANDBOX_VERIFY_TOKEN"],
            "hub.challenge": "123456"}


def test_challenge_get_allows_only_bounded_inert_extras_and_logs_no_free_input(environment,caplog):
    params = challenge_params(environment)
    extras = [("inert", "https://example.invalid/?hub.verify_token=not-used"),
              ("inert", "duplicate-extra-is-inert"), ("a-private-name-not-to-log", "a-private-value-not-to-log")]
    with caplog.at_level("INFO"):
        response = environment.client.get(URL,query_string=[*params.items(),*extras])
    assert response.status_code == 200 and response.data == b"123456"
    assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert "no-store" in response.headers["Cache-Control"]
    assert "parameter_count=6" in caplog.text and "extra_count=3" in caplog.text
    assert "hub.verify_token" in caplog.text and params["hub.verify_token"] not in caplog.text
    for name,value in extras: assert name not in caplog.text and value not in caplog.text
    assert db.session.query(WebhookDelivery).count() == 0


@pytest.mark.parametrize("required",["hub.mode","hub.verify_token","hub.challenge"])
def test_challenge_get_required_fields_cannot_be_missing_or_duplicated_with_extras(environment,required):
    params=challenge_params(environment)
    for query in [[(k,v) for k,v in params.items() if k != required],
                  [*params.items(),(required,params[required])]]:
        reply=environment.client.get(URL,query_string=[*query,("inert","ignored")])
        assert reply.status_code == 400 and reply.json["reason_code"] == "tdf_meta_challenge_invalid"


def test_challenge_get_query_count_key_and_raw_size_limits_are_exact(environment):
    from routes.tdf_meta_sandbox import MAX_CHALLENGE_QUERY_BYTES
    params=list(challenge_params(environment).items())
    assert environment.client.get(URL,query_string=[*params,*[("extra","x")]*13]).status_code == 200
    assert environment.client.get(URL,query_string=[*params,*[("extra","x")]*14]).status_code == 400
    assert environment.client.get(URL,query_string=[*params,("k"*128,"x")]).status_code == 200
    assert environment.client.get(URL,query_string=[*params,("k"*129,"x")]).status_code == 400
    assert environment.client.get(URL,query_string=[*params,("","x")]).status_code == 400
    from urllib.parse import urlencode
    prefix=urlencode(params)+"&extra="
    exact=prefix+"x"*(MAX_CHALLENGE_QUERY_BYTES-len(prefix))
    assert environment.client.get(URL+"?"+exact).status_code == 200
    assert environment.client.get(URL+"?"+exact+"x").status_code == 400


@pytest.mark.parametrize("change",[{"hub.mode":"other"},{"hub.verify_token":"wrong-token"},
                                 {"hub.challenge":"non-numeric"},{"hub.challenge":"1"*257},
                                 {"hub.challenge":"١٢٣"}])
def test_challenge_get_extras_cannot_replace_existing_authentication(environment,change):
    query={**challenge_params(environment),**change,"inert":"subscribe"}
    response=environment.client.get(URL,query_string=query)
    assert response.status_code == 403 and response.json["reason_code"] == "tdf_meta_authentication_or_binding_denied"


def test_challenge_get_compatibility_does_not_allow_post_query_or_skip_signature(environment,monkeypatch):
    calls=[]
    monkeypatch.setattr(pilot,"process",lambda *args:calls.append(args))
    raw,signature=encoded(document())
    response=environment.client.post(URL,query_string={"inert":"ignored"},data=raw,
        content_type="application/json",headers={"X-Hub-Signature-256":signature})
    assert response.status_code == 400 and response.json["reason_code"] == "tdf_meta_json_required"
    assert calls==[] and db.session.query(WebhookDelivery).count() == 0


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


def test_audio_transcription_failure_preserves_text_and_person_help(environment, monkeypatch):
    def unavailable(*args, **kwargs):
        raise pilot.TdfAudioError("tdf_audio_unavailable")
    monkeypatch.setattr(pilot, "transcribe_meta_voice_note", unavailable)
    bodies = []
    def post(url, **kwargs):
        bodies.append(sent_body(kwargs["json"])); return accepted_post()
    process(document(content={"type": "audio", "audio": {"id": "555555", "mime_type": "audio/ogg"}}), post=post)
    assert "No pude leer esta nota de voz" in bodies[0]
    assert "persona" in bodies[0]


def test_audio_uses_published_menu_context_and_never_persists_transcript(environment, monkeypatch):
    # The first real knowledge reply advertises canonical code 1. A voice note
    # containing that code must resolve through the same persisted menu context.
    process(document())
    bodies, calls = [], []
    def transcribe(event, **kwargs):
        calls.append(event.media_id)
        assert event.media_id == "555555" and event.media_mime_type == "audio/ogg"
        return "1"
    monkeypatch.setattr(pilot, "transcribe_meta_voice_note", transcribe)
    def post(url, **kwargs):
        bodies.append(sent_body(kwargs["json"])); return accepted_post()
    doc = document(mid="wamid.voice1", content={"type": "audio", "audio": {
        "id": "555555", "mime_type": "audio/ogg"}})
    assert process(doc, post=post)["accepted"] == 1
    assert "Respuesta institucional de prueba" in bodies[0]
    assert "9." in bodies[0] and "PRIVATE" not in bodies[0]
    assert process(doc, post=post)["replayed"] == 1 and calls == ["555555"] and len(bodies) == 1
    for row in db.session.query(MessagingEventLedger):
        assert row.payload is None and set(row.metadata_json) == {"contract", "knowledge_context"}
        assert "555555" not in json.dumps(row.metadata_json)
    assert all("555555" not in row.payload_digest for row in db.session.query(WebhookDelivery))
    changed = deepcopy(doc); changed["entry"][0]["changes"][0]["value"]["messages"][0]["audio"]["id"] = "666666"
    with pytest.raises(pilot.PilotError, match="event_conflict"):
        process(changed, post=post)
    assert calls == ["555555"] and len(bodies) == 1


def test_audio_revision_retired_during_transcription_cannot_send(environment, monkeypatch):
    process(document())
    def transcribe(event, **kwargs):
        record = db.session.query(TenantConfig).filter_by(tenant_id=46, key="institutional_assistant").one()
        state = deepcopy(record.json_value); state["visibility"] = "private"
        state["generation"] += 1
        state["revision"] = digest({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
        record.json_value = state
        db.session.commit()
        return "1"
    monkeypatch.setattr(pilot, "transcribe_meta_voice_note", transcribe)
    before = db.session.query(MessagingEventLedger).count()
    result = process(document(mid="wamid.retiredvoice", content={"type": "audio", "audio": {
        "id": "555555", "mime_type": "audio/ogg"}}), post=lambda *a, **k: pytest.fail("retired content must not send"))
    assert result["accepted"] == 0 and db.session.query(MessagingEventLedger).count() == before


def test_audio_allowlist_window_signature_and_batch_dedupe_precede_transcription(environment, monkeypatch):
    monkeypatch.setattr(pilot, "transcribe_meta_voice_note", lambda *a, **k: pytest.fail("STT must not start"))
    content = {"type": "audio", "audio": {"id": "555555", "mime_type": "audio/ogg"}}
    with pytest.raises(pilot.PilotError, match="recipient_not_allowed"):
        process(document(contact="5491112345688", content=content))
    raw, signature = encoded(document(content=content))
    with pytest.raises(cloud.MetaContractError, match="signature_invalid"):
        pilot.process(raw, "sha256=" + "0" * 64, now=NOW, authority=authority)
    assert process(document(content=content, timestamp=NOW - 86400))["accepted"] == 0
    doc = document(mid="wamid.batchvoice", content=content)
    message = deepcopy(doc["entry"][0]["changes"][0]["value"]["messages"][0]); message["audio"]["id"] = "666666"
    doc["entry"][0]["changes"][0]["value"]["messages"].append(message)
    with pytest.raises(cloud.MetaContractError, match="duplicate_conflict"):
        process(doc)


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
