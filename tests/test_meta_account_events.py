"""Reconocimiento de avisos de cuenta y plantillas: sólo tras la firma, sin leer contenido."""
import hashlib
import hmac
import json

import pytest

from services import meta_account_events as events
from services.meta_whatsapp_webhook import MAX_EVENTS, MetaContractError

SECRET = "synthetic-app-secret-" + "0" * 24


def signed(document, *, secret=SECRET):
    raw = document if isinstance(document, bytes) else json.dumps(document, separators=(",", ":")).encode()
    return raw, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def delivery(*fields, extra_entry=None):
    entries = [{"id": "100000000000001", "changes": [
        {"field": field, "value": {"event": "SYNTHETIC", "message_template_name": "plantilla_privada"}}
        for field in fields]}]
    if extra_entry:
        entries.append(extra_entry)
    return {"object": "whatsapp_business_account", "entry": entries}


def recognise(document, **overrides):
    raw, signature = signed(document)
    return events.acknowledged_account_events(
        raw_body=overrides.get("raw", raw), signature=overrides.get("signature", signature),
        app_secret=overrides.get("secret", SECRET))


def test_closed_list_matches_the_fields_meta_never_routes_through_overrides():
    assert events.ACKNOWLEDGED_FIELDS == {
        "message_template_status_update", "message_template_quality_update",
        "message_template_components_update", "template_category_update",
        "account_update", "account_review_update", "account_alerts",
    }
    assert "messages" not in events.ACKNOWLEDGED_FIELDS


@pytest.mark.parametrize("field", sorted(events.ACKNOWLEDGED_FIELDS))
def test_each_documented_field_is_recognised_and_only_counted(field):
    assert recognise(delivery(field)) == {field: 1}


def test_counts_are_per_field_and_never_carry_payload_values():
    result = recognise(delivery("account_update", "account_update", "template_category_update"))
    assert result == {"account_update": 2, "template_category_update": 1}
    assert "plantilla_privada" not in json.dumps(result) and "100000000000001" not in json.dumps(result)


def test_signature_is_checked_before_anything_is_parsed():
    raw, signature = signed(delivery("account_update"))
    for bad in ("sha256=" + "0" * 64, "", "sha1=abc", signature.upper()):
        with pytest.raises(MetaContractError, match="meta_webhook_signature_invalid"):
            events.acknowledged_account_events(raw_body=raw, signature=bad, app_secret=SECRET)
    with pytest.raises(MetaContractError, match="meta_webhook_signature_invalid"):
        events.acknowledged_account_events(raw_body=raw, signature=signature, app_secret="another-secret-" + "1" * 24)
    # Un cuerpo que ni siquiera es JSON, sin firma válida, falla por firma y no por formato.
    with pytest.raises(MetaContractError, match="meta_webhook_signature_invalid"):
        events.acknowledged_account_events(raw_body=b"not json", signature="sha256=" + "0" * 64, app_secret=SECRET)


@pytest.mark.parametrize("document", [
    delivery("messages"),
    delivery("account_update", "messages"),                      # lote mixto en una entrada
    delivery("account_update", extra_entry={"id": "2", "changes": [{"field": "messages", "value": {}}]}),
    delivery("security"), delivery("phone_number_quality_update"), delivery("Account_Update"),
    delivery("account_update "), delivery(""),
    {"object": "page", "entry": delivery("account_update")["entry"]},
    {"object": "whatsapp_business_account", "entry": []},
    {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": []}]},
    {"object": "whatsapp_business_account", "entry": [{"id": "1"}]},
    {"object": "whatsapp_business_account", "entry": ["account_update"]},
    {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": ["account_update"]}]},
    {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": 7}]}]},
    {"object": "whatsapp_business_account"}, [], "account_update", 7,
])
def test_everything_else_is_left_to_the_message_pipeline(document):
    assert recognise(document) is None


def test_malformed_or_ambiguous_json_is_not_acknowledged():
    for raw in (b"{", b"\xff\xfe", b'{"object":"whatsapp_business_account","object":"whatsapp_business_account","entry":[]}',
                b'{"object":"whatsapp_business_account","entry":[{"id":"1","changes":[{"field":"account_update","field":"messages"}]}]}',
                b'{"object":"whatsapp_business_account","entry":NaN}'):
        raw_body, signature = signed(raw)
        assert events.acknowledged_account_events(raw_body=raw_body, signature=signature, app_secret=SECRET) is None


def test_oversized_batches_are_not_acknowledged():
    too_many = delivery(*["account_update"] * (MAX_EVENTS + 1))
    assert recognise(too_many) is None
    assert recognise(delivery(*["account_update"] * MAX_EVENTS)) == {"account_update": MAX_EVENTS}
    spread = {"object": "whatsapp_business_account", "entry": [
        {"id": str(index), "changes": [{"field": "account_alerts", "value": {}}] * 2} for index in range(MAX_EVENTS // 2 + 1)]}
    assert recognise(spread) is None
