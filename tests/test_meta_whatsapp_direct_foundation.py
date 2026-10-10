"""Synthetic offline contracts; no Meta account, credential or delivery acceptance."""
from __future__ import annotations

import base64
import copy
from dataclasses import replace
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace

import pytest
import requests
from flask import Flask
from sqlalchemy import select, update

from models import ProviderConnection, ProviderSender, TenantProfile, User, db
from services import meta_whatsapp_credentials as vault
from services import meta_whatsapp_cloud as cloud
from services import meta_whatsapp_webhook as webhook
from services.tenant_provider_credentials import ProviderCredentialError, public_provider_config

NOW = 1700000000
TOKEN = secrets.token_urlsafe(64)
SECRET = secrets.token_hex(32)
APP, WABA, PHONE = "100001", "200001", "300001"
KEY_CONFIG = {
    "TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID": "synthetic",
    "TENANT_PROVIDER_CREDENTIAL_KEYRING": json.dumps({
        "synthetic": base64.b64encode(secrets.token_bytes(32)).decode("ascii")}),
}


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Offline foundation must not call HTTP")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def _connection(**overrides):
    values = dict(id=1, tenant_id=1, provider="meta", channel="whatsapp",
                  environment="production", external_account_id=WABA, external_app_id=APP,
                  credentials_ref=vault.VAULT_REF, config={})
    values.update(overrides)
    return SimpleNamespace(**values)


def _install(connection):
    connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(
        connection=connection, tenant_id=connection.tenant_id, access_token=TOKEN,
        revision=1, expires_at=NOW + 3600, now=NOW, app_config=KEY_CONFIG)}
    return connection


def _authority(sender, credential):
    # Entirely synthetic observation from a fake trusted adapter, not native Meta.
    return cloud.VerifiedMetaAuthority(sender, credential.revision, "v23.0", NOW - 5,
                                       NOW + 60, True, True)


def _binding():
    sender = cloud.MetaSenderSnapshot(1, 1, 1, APP, WABA, PHONE, "production")
    credential = vault.MetaCredential(1, NOW + 3600, TOKEN)
    return cloud.VerifiedMetaBinding(sender, _authority(sender, credential), credential)


def test_vault_round_trip_and_existing_public_redaction():
    connection = _install(_connection())
    credential = vault.open_token(connection=connection, tenant_id=1, now=NOW, app_config=KEY_CONFIG)
    assert credential.access_token == TOKEN
    assert TOKEN not in repr(credential) and TOKEN not in json.dumps(connection.config)
    public = public_provider_config({"nested": [connection.config], "label": "kept"})
    assert public == {"nested": [{}], "label": "kept"}


@pytest.mark.parametrize("field,value", [
    ("id", 2), ("tenant_id", 2), ("external_app_id", "100002"),
    ("external_account_id", "200002"), ("environment", "sandbox"),
    ("provider", "twilio"), ("channel", "sms"),
])
def test_foreign_or_rebound_envelope_is_rejected(field, value):
    connection = _install(_connection())
    setattr(connection, field, value)
    with pytest.raises(ProviderCredentialError):
        vault.open_token(connection=connection, tenant_id=connection.tenant_id,
                         now=NOW, app_config=KEY_CONFIG)


@pytest.mark.parametrize("mutation", ["expiry", "revision", "ciphertext", "missing", "twilio_ref", "managed", "keyring"])
def test_vault_invalid_never_falls_back_to_environment(mutation):
    connection = _install(_connection())
    config = {**KEY_CONFIG, "META_ACCESS_TOKEN": TOKEN, "TWILIO_AUTH_TOKEN": TOKEN}
    envelope = connection.config[vault.PRIVATE_CONFIG_KEY]
    if mutation == "expiry":
        envelope["expires_at"] += 1
    elif mutation == "revision":
        envelope["revision"] += 1
    elif mutation == "ciphertext":
        envelope["ciphertext"] = base64.b64encode(b"x" * 80).decode()
    elif mutation == "missing":
        connection.config = {}
    elif mutation == "twilio_ref":
        connection.credentials_ref = "vault:twilio:auth_token:v1"
    elif mutation == "managed":
        connection.config["cutover_managed_provider_connection"] = {"enabled": True}
    else:
        config = {"META_ACCESS_TOKEN": TOKEN}
    with pytest.raises(ProviderCredentialError) as caught:
        vault.open_token(connection=connection, tenant_id=1, now=NOW, app_config=config)
    assert TOKEN not in str(caught.value)


def test_vault_expired_and_unbounded_expiry_fail_closed():
    connection = _install(_connection())
    with pytest.raises(ProviderCredentialError, match="expired"):
        vault.open_token(connection=connection, tenant_id=1, now=NOW + 3600, app_config=KEY_CONFIG)
    with pytest.raises(ProviderCredentialError):
        vault.seal_token(connection=connection, tenant_id=1, access_token=TOKEN,
                         revision=1, expires_at=0, now=NOW, app_config=KEY_CONFIG)


@pytest.fixture
def local_db():
    app = Flask("isolated-meta-foundation")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        assert db.engine.dialect.name == "sqlite" and db.engine.url.database == ":memory:"
        db.create_all()
        owner = User(name="Synthetic A", email="meta-a@example.test", rol="admin",
                     password_hash="synthetic-not-a-login-hash")
        other_owner = User(name="Synthetic B", email="meta-b@example.test", rol="admin",
                           password_hash="synthetic-not-a-login-hash")
        db.session.add_all([owner, other_owner]); db.session.flush()
        tenant = TenantProfile(slug="meta-a", nombre="Synthetic A", tipo="municipio", municipio_id=owner.id, is_active=True)
        other = TenantProfile(slug="meta-b", nombre="Synthetic B", tipo="municipio", municipio_id=other_owner.id, is_active=True)
        db.session.add_all([tenant, other]); db.session.flush()
        connection = ProviderConnection(tenant_id=tenant.id, provider="meta", channel="whatsapp",
            environment="production", status="connected", external_account_id=WABA,
            external_app_id=APP, credentials_ref=vault.VAULT_REF, config={})
        db.session.add(connection); db.session.flush()
        _install(connection)
        sender = ProviderSender(tenant_id=tenant.id, provider_connection_id=connection.id,
                                 channel="whatsapp", phone_number_id=PHONE, waba_id=WABA,
                                 status="ONLINE")
        db.session.add(sender); db.session.commit()
        yield SimpleNamespace(tenant=tenant, other=other, connection=connection, sender=sender)
        db.session.rollback(); db.session.remove(); db.drop_all(); db.engine.dispose()


def _resolve(**overrides):
    values = dict(phone_number_id=PHONE, app_id=APP, waba_id=WABA, environment="production",
                  now=NOW, app_config=KEY_CONFIG, authority_verifier=_authority)
    values.update(overrides)
    return cloud.resolve_sender(**values)


def test_resolve_requires_explicit_verified_authority_even_if_local_ready(local_db):
    rows = local_db
    rows.connection.status = "connected"; rows.sender.status = "ONLINE"
    rows.sender.verification_status = "verified"; db.session.commit()
    with pytest.raises(cloud.MetaContractError, match="verified_authority_required"):
        _resolve(authority_verifier=None)
    assert _resolve().sender.tenant_id == rows.tenant.id


@pytest.mark.parametrize("case", ["unknown", "duplicate", "sender_foreign", "connection_foreign", "tenant_inactive", "wrong_app", "wrong_waba", "wrong_env", "foreign_expected"])
def test_resolve_rejects_unknown_conflicting_or_foreign_authority(local_db, case):
    rows = local_db
    args = {}
    if case == "unknown":
        args["phone_number_id"] = "300002"
    elif case == "duplicate":
        db.session.add(ProviderSender(tenant_id=rows.other.id, channel="whatsapp",
                       phone_number_id=PHONE, waba_id=WABA)); db.session.commit()
    elif case == "sender_foreign":
        rows.sender.tenant_id = rows.other.id; db.session.commit()
    elif case == "connection_foreign":
        rows.sender.provider_connection_id = None; db.session.commit()
    elif case == "tenant_inactive":
        rows.tenant.is_active = False; db.session.commit()
    elif case == "wrong_app":
        args["app_id"] = "100002"
    elif case == "wrong_waba":
        args["waba_id"] = "200002"
    elif case == "wrong_env":
        args["environment"] = "sandbox"
    else:
        args["expected_tenant_id"] = rows.other.id
    with pytest.raises((cloud.MetaContractError, ProviderCredentialError)):
        _resolve(**args)


def test_persisted_projection_ignores_dirty_orm_and_does_not_autoflush(local_db):
    rows = local_db
    tenant_id, sender_id, other_id = rows.tenant.id, rows.sender.id, rows.other.id
    rows.connection.external_app_id = "999001"
    rows.sender.tenant_id = other_id
    rows.tenant.is_active = False
    assert _resolve().sender.tenant_id == tenant_id
    projection = db.session.execute(select(ProviderSender.tenant_id).where(
        ProviderSender.id == sender_id).execution_options(autoflush=False)).scalar_one()
    assert projection == tenant_id and db.session.dirty


def test_stale_orm_cannot_restore_revoked_committed_binding(local_db):
    rows = local_db
    assert rows.connection.external_app_id == APP
    db.session.execute(update(ProviderConnection).where(ProviderConnection.id == rows.connection.id)
                       .values(external_app_id="999001").execution_options(synchronize_session=False))
    assert rows.connection.external_app_id == APP  # intentionally stale identity
    with pytest.raises(cloud.MetaContractError, match="binding_mismatch"):
        _resolve()


@pytest.mark.parametrize("change", ["foreign", "revision", "future", "expired", "credential_expiry", "version"])
def test_authority_observation_must_match_and_be_fresh(local_db, change):
    def verifier(sender, credential):
        authority = _authority(sender, credential)
        if change == "foreign":
            return replace(authority, sender=replace(sender, tenant_id=999))
        if change == "revision":
            return replace(authority, credential_revision=2)
        if change == "future":
            return replace(authority, observed_at=NOW + 1)
        if change == "expired":
            return replace(authority, valid_until=NOW)
        if change == "credential_expiry":
            return replace(authority, valid_until=credential.expires_at + 1)
        return replace(authority, graph_version="v23.0/evil")
    with pytest.raises(cloud.MetaContractError, match="authority_binding_invalid"):
        _resolve(authority_verifier=verifier)


class _Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = data if data is not None else {"messaging_product": "whatsapp", "messages": [{"id": "wamid.synthetic"}]}
    @property
    def content(self):
        return b"invalid" if isinstance(self.data, Exception) else json.dumps(self.data).encode()


def test_single_post_fixed_https_tls_timeouts_and_accepted_is_not_delivered():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs)); return _Response()
    result = cloud.send_once(binding_loader=_binding, payload=cloud.text_payload(
        recipient="5491112345678", body="Synthetic"), now=NOW, policy_check=lambda *_: True, post=post)
    assert result.state == "accepted" and result.message_id == "wamid.synthetic"
    assert result.retry_allowed is False and len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"https://graph.facebook.com/v23.0/{PHONE}/messages" and TOKEN not in url
    assert kwargs["timeout"] == (3.0, 10.0) and kwargs["allow_redirects"] is False and kwargs["verify"] is True
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("outcome", [TimeoutError("synthetic-private-error"), ConnectionError(),
    _Response(302), _Response(500), _Response(200, {}), _Response(200, ValueError()),
    _Response(200, {"messaging_product": "whatsapp", "messages": [{"id": "bad"}]})])
def test_uncertain_post_is_never_retried(outcome):
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    result = cloud.send_once(binding_loader=_binding, payload=cloud.text_payload(
        recipient="5491112345678", body="Synthetic"), now=NOW, policy_check=lambda *_: True, post=post)
    assert result.state == "uncertain" and result.retry_allowed is False and len(calls) == 1
    assert "synthetic-private-error" not in repr(result)


@pytest.mark.parametrize("case", ["missing_policy", "denied", "truthy_policy", "not_permitted", "not_subscribed", "expired", "extra_payload"])
def test_send_denial_is_before_post(case):
    calls = []
    binding = _binding()
    policy = lambda *_: True
    payload = cloud.text_payload(recipient="5491112345678", body="Synthetic")
    if case == "missing_policy": policy = None
    elif case == "denied": policy = lambda *_: False
    elif case == "truthy_policy": policy = lambda *_: "yes"
    elif case == "not_permitted": binding = replace(binding, authority=replace(binding.authority, messaging_permitted=False))
    elif case == "not_subscribed": binding = replace(binding, authority=replace(binding.authority, webhook_subscribed=False))
    elif case == "expired": binding = replace(binding, authority=replace(binding.authority, valid_until=NOW))
    else: payload["access_token"] = TOKEN
    with pytest.raises(cloud.MetaContractError):
        cloud.send_once(binding_loader=lambda: binding, payload=payload, now=NOW,
                        policy_check=policy, post=lambda *_args, **_kwargs: calls.append(1))
    assert calls == []


def test_template_transport_keeps_tenant_policy_mandatory_and_sanitizes_error():
    payload = cloud.template_payload(recipient="5491112345678", name="service_notice",
                                     language="es_AR", body_parameters=("Synthetic",))
    assert payload["template"]["components"][0]["parameters"] == [{"type": "text", "text": "Synthetic"}]
    result = cloud.send_once(binding_loader=_binding, payload=payload, now=NOW,
        policy_check=lambda sender, data: sender.tenant_id == 1 and data["template"]["name"] == "service_notice",
        post=lambda *_args, **_kwargs: _Response(400, {"error": {"code": 131047, "message": TOKEN}}))
    assert result.state == "rejected" and result.error_code == 131047 and result.retry_allowed is False
    assert TOKEN not in repr(result)


def _document():
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{
        "field": "messages", "value": {"messaging_product": "whatsapp", "metadata": {
            "phone_number_id": PHONE, "display_phone_number": "ignored"}, "messages": [{
            "from": "5491112345678", "id": "wamid.synthetic", "timestamp": str(NOW),
            "type": "text", "text": {"body": "Hola sintético"}}]}}]}]}


def _parse(document=None, **overrides):
    raw = json.dumps(document if document is not None else _document(), ensure_ascii=False).encode()
    values = dict(raw_body=raw, signature="sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
                  app_secret=SECRET, app_id=APP, now=NOW, binding_resolver=lambda *_: _binding())
    values.update(overrides)
    return webhook.parse_webhook(**values)


def test_exact_raw_body_signature_and_minimal_event_repr():
    event, = _parse()
    assert event.text == "Hola sintético" and event.sender.tenant_id == 1 and event.kind == "message"
    assert event.event_key.startswith("meta:")
    assert event.text not in repr(event) and event.contact not in repr(event) and event.message_id not in repr(event)


@pytest.mark.parametrize("case", ["missing", "tampered", "wrong_secret", "oversize", "duplicate_json"])
def test_bad_signature_or_json_has_no_resolution_effects(case):
    calls = []
    raw = json.dumps(_document()).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    secret = SECRET
    if case == "missing": signature = ""
    elif case == "tampered": raw += b" "
    elif case == "wrong_secret": secret = secrets.token_hex(32)
    elif case == "oversize": raw = b"x" * (webhook.MAX_BODY_BYTES + 1)
    else:
        raw = b'{"object":"whatsapp_business_account","object":"page","entry":[]}'
        signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(cloud.MetaContractError):
        _parse(raw_body=raw, signature=signature, app_secret=secret,
               binding_resolver=lambda *_: calls.append(1))
    assert calls == []


@pytest.mark.parametrize("case", ["wrong_app", "wrong_waba", "not_subscribed", "expired", "unknown_sender", "duplicate_sender", "unsupported", "malformed"])
def test_entire_webhook_batch_fails_before_returning_events(local_db, case):
    document = _document()
    document["entry"][0]["changes"].append(copy.deepcopy(document["entry"][0]["changes"][0]))
    second = document["entry"][0]["changes"][1]["value"]
    binding = _binding()
    resolver = lambda *_: binding
    if case == "wrong_app": binding = replace(binding, sender=replace(binding.sender, app_id="100002"))
    elif case == "wrong_waba": binding = replace(binding, sender=replace(binding.sender, waba_id="200002"))
    elif case == "not_subscribed": binding = replace(binding, authority=replace(binding.authority, webhook_subscribed=False))
    elif case == "expired": binding = replace(binding, authority=replace(binding.authority, valid_until=NOW))
    elif case in {"unknown_sender", "duplicate_sender"}:
        if case == "unknown_sender": second["metadata"]["phone_number_id"] = "300002"
        else:
            db.session.add(ProviderSender(tenant_id=local_db.other.id, channel="whatsapp", phone_number_id=PHONE))
            db.session.commit()
        resolver = lambda phone, app, waba: _resolve(phone_number_id=phone, app_id=app, waba_id=waba)
    elif case == "unsupported": second["messages"][0]["type"] = "audio"
    else: second["messages"][0]["timestamp"] = "invalid"
    with pytest.raises((cloud.MetaContractError, ProviderCredentialError)):
        _parse(document, binding_resolver=resolver)


def test_retry_dedup_and_statuses_preserve_separate_semantics():
    document = _document()
    value = document["entry"][0]["changes"][0]["value"]
    value["messages"].append(copy.deepcopy(value["messages"][0]))
    value["statuses"] = [{"id": "wamid.synthetic", "recipient_id": "5491112345678",
                          "timestamp": str(NOW), "status": "delivered"},
                         {"id": "wamid.synthetic", "recipient_id": "5491112345678",
                          "timestamp": str(NOW), "status": "read"}]
    events = _parse(document)
    assert len(events) == 3 and len({event.event_key for event in events}) == 3
    assert [event.status for event in events] == [None, "delivered", "read"]
    value["messages"][1]["text"]["body"] = "conflicting replay"
    with pytest.raises(cloud.MetaContractError, match="duplicate_conflict"):
        _parse(document)


def test_challenge_token_is_distinct_from_post_secret_and_no_unknown_token():
    token = secrets.token_urlsafe(32)
    assert webhook.verify_challenge(mode="subscribe", token=token, challenge="12345", verify_token=token) == "12345"
    with pytest.raises(cloud.MetaContractError):
        webhook.verify_challenge(mode="subscribe", token=SECRET, challenge="12345", verify_token=token)


def test_opaque_contact_preserved_without_phone_inference_or_global_merge():
    document = _document()
    item = document["entry"][0]["changes"][0]["value"]["messages"][0]
    opaque_id = "US." + "A1" * 50
    item["from"] = opaque_id
    event, = _parse(document)
    assert event.contact == opaque_id and event.sender.tenant_id == 1
    assert opaque_id not in repr(event) and opaque_id not in event.event_key
    # Synthetic adapter input, not a claim about current live Meta field schema.
    item.pop("from"); item["from_user_id"] = opaque_id
    with pytest.raises(cloud.MetaContractError, match="contact_adapter_required"):
        _parse(document)
    event, = _parse(document, contact_resolver=lambda value, kind: value["from_user_id"])
    assert event.contact == opaque_id
    with pytest.raises(cloud.MetaContractError):
        cloud.text_payload(recipient=opaque_id, body="Blocked until current outbound BSUID schema verified")


def test_arbitrary_adapter_errors_are_sanitized_before_post_or_events():
    def failed(*_args):
        raise RuntimeError(TOKEN)
    calls = []
    with pytest.raises(cloud.MetaContractError) as caught:
        cloud.send_once(binding_loader=failed, payload=cloud.text_payload(
            recipient="5491112345678", body="Synthetic"), now=NOW,
            policy_check=lambda *_: True, post=lambda *_args, **_kwargs: calls.append(1))
    assert TOKEN not in str(caught.value) and calls == []
    with pytest.raises(cloud.MetaContractError) as caught:
        _parse(binding_resolver=failed)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("raw", [b"x" * (cloud.MAX_RESPONSE_BYTES + 1),
    b'{"messaging_product":"whatsapp","messages":[{"id":"wamid.synthetic","id":"wamid.other"}]}'],
    ids=["oversize", "duplicate-id"])
def test_unbounded_or_ambiguous_response_remains_uncertain(raw):
    calls = []
    def post(*_args, **_kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=200, content=raw)
    result = cloud.send_once(binding_loader=_binding, payload=cloud.text_payload(
        recipient="5491112345678", body="Synthetic"), now=NOW,
        policy_check=lambda *_: True, post=post)
    assert result.state == "uncertain" and not result.retry_allowed and calls == [1]


def test_policy_callback_cannot_mutate_the_validated_transport_body():
    payload = cloud.text_payload(recipient="5491112345678", body="Synthetic")
    calls = []
    def policy(_sender, value):
        value["to"] = "5499999999999"
        value["text"]["body"] = "mutated"
        value["access_token"] = TOKEN
        return True
    def post(_url, **kwargs):
        calls.append(kwargs["json"]); return _Response()
    result = cloud.send_once(binding_loader=_binding, payload=payload, now=NOW,
                             policy_check=policy, post=post)
    assert result.state == "accepted" and calls == [payload]
    assert calls[0]["to"] == "5491112345678" and calls[0]["text"]["body"] == "Synthetic"


@pytest.mark.parametrize("row_name", ["sender", "connection"])
def test_local_status_revocation_blocks_verifier_and_send(local_db, row_name):
    rows = local_db
    getattr(rows, row_name).status = "paused" if row_name == "sender" else "disconnected"
    db.session.commit()
    calls = []
    def verifier(*_args):
        calls.append("verifier"); return _authority(*_args)
    with pytest.raises(cloud.MetaContractError, match="locally_unavailable"):
        cloud.send_once(binding_loader=lambda: _resolve(authority_verifier=verifier),
            payload=cloud.text_payload(recipient="5491112345678", body="Synthetic"), now=NOW,
            policy_check=lambda *_: True, post=lambda *_args, **_kwargs: calls.append("post"))
    assert calls == []


@pytest.mark.parametrize("row_name,model", [("sender", ProviderSender), ("connection", ProviderConnection)])
def test_stale_online_cannot_grant_after_persisted_status_revocation(local_db, row_name, model):
    rows = local_db
    row = getattr(rows, row_name)
    assert row.status in {"ONLINE", "connected"}
    db.session.execute(update(model).where(model.id == row.id).values(status="suspended")
                       .execution_options(synchronize_session=False, autoflush=False))
    assert row.status in {"ONLINE", "connected"}  # deliberately stale identity
    row.status = "active"  # dirty repair must never flush into the authorization query
    calls = []
    def verifier(*_args):
        calls.append("verifier"); return _authority(*_args)
    with pytest.raises(cloud.MetaContractError, match="locally_unavailable"):
        cloud.send_once(binding_loader=lambda: _resolve(authority_verifier=verifier),
            payload=cloud.text_payload(recipient="5491112345678", body="Synthetic"), now=NOW,
            policy_check=lambda *_: True, post=lambda *_args, **_kwargs: calls.append("post"))
    assert calls == []
    assert db.session.execute(select(model.status).where(model.id == row.id)
        .execution_options(autoflush=False)).scalar_one() == "suspended"
