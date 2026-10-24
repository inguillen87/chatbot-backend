import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from services import crm_people_directory as directory
from tests.test_crm_people_directory import _contact, _headers, _seed_directory


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _directory_page(client, app):
    actor, other_actor, tenant, other_tenant = _seed_directory()
    now = datetime.now(timezone.utc)
    for index in range(4):
        _contact(tenant, f"Synthetic contact {index}", f"+549261700000{index}", seen=now - timedelta(minutes=index))
    _contact(other_tenant, "Synthetic foreign contact", "+5492617999999", seen=now)
    db.session.commit()
    headers = _headers(app, actor, tenant)
    response = client.get("/api/v2/crm/people?limit=1", headers=headers)
    assert response.status_code == 200
    page = response.get_json()
    cursor = page["page"]["next_cursor"]
    assert isinstance(cursor, str) and cursor
    return cursor, headers, page, actor, other_actor, tenant, other_tenant


def _unused_bit_alias(segment):
    unused_bits = {2: 4, 3: 2}[len(segment) % 4]
    index = ALPHABET.index(segment[-1])
    assert index & ((1 << unused_bits) - 1) == 0
    alias = segment[:-1] + ALPHABET[index | 1]
    assert base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)) == base64.urlsafe_b64decode(alias + "=" * (-len(alias) % 4))
    return alias


def _variant(cursor, kind):
    payload, signature = cursor.rsplit(".", 1)
    if kind == "signature-unused-bits":
        return payload + "." + _unused_bit_alias(signature)
    if kind == "signature-padding-one":
        return cursor + "="
    if kind == "signature-padding-two":
        return cursor + "=="
    if kind == "signature-padding-three":
        return cursor + "==="
    if kind == "payload-padding":
        return payload + "=." + signature
    if kind == "signature-ignored-symbol":
        return payload + "." + signature[:4] + "$" + signature[4:]
    if kind == "signature-ignored-newline":
        return payload + "." + signature[:4] + "\n" + signature[4:]
    if kind == "signature-space":
        return payload + "." + signature[:4] + " " + signature[4:]
    if kind == "signature-unicode":
        return cursor + "é"
    if kind == "signature-standard-plus":
        return payload + ".+" + signature[1:]
    if kind == "signature-standard-slash":
        return payload + "./" + signature[1:]
    if kind == "outer-leading-space":
        return " " + cursor
    if kind == "outer-trailing-space":
        return cursor + " "
    if kind == "whitespace-only":
        return "   "
    if kind == "extra-compression-marker":
        return "." + cursor
    if kind == "empty-payload":
        return "." + signature
    if kind == "empty-signature":
        return payload + "."
    if kind == "truncated-base64-signature":
        return payload + ".A"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", [
    "signature-unused-bits", "signature-padding-one", "signature-padding-two", "signature-padding-three",
    "payload-padding", "signature-ignored-symbol", "signature-ignored-newline", "signature-space",
    "signature-unicode", "signature-standard-plus", "signature-standard-slash", "outer-leading-space",
    "outer-trailing-space", "whitespace-only", "extra-compression-marker", "empty-payload",
    "empty-signature", "truncated-base64-signature",
])
def test_actual_people_route_rejects_noncanonical_cursor(client, app, kind):
    cursor, headers, *_ = _directory_page(client, app)
    response = client.get("/api/v2/crm/people", query_string={"cursor": _variant(cursor, kind)}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_people_cursor"


def test_actual_emitted_cursor_keeps_keyset_pagination_and_empty_cursor_behavior(client, app):
    cursor, headers, first, *_ = _directory_page(client, app)
    assert "=" not in cursor
    second = client.get("/api/v2/crm/people", query_string={"cursor": cursor, "limit": 1}, headers=headers)
    assert second.status_code == 200
    page = second.get_json()
    assert page["page"]["total"] == first["page"]["total"] == 4
    assert page["page"]["has_more"] is True
    assert first["items"][0]["id"] != page["items"][0]["id"]
    empty = client.get("/api/v2/crm/people", query_string={"cursor": "", "limit": 1}, headers=headers)
    assert empty.status_code == 200
    assert empty.get_json()["items"][0]["id"] == first["items"][0]["id"]


def test_canonical_signature_still_requires_matching_hmac_actor_tenant_and_filters(client, app):
    cursor, headers, _, _, other_actor, tenant, other_tenant = _directory_page(client, app)
    payload, signature = cursor.rsplit(".", 1)
    # A significant signature bit changes the decoded bytes, while its encoding
    # remains canonical. This must still fail HMAC verification.
    changed_signature = ALPHABET[ALPHABET.index(signature[0]) ^ 16] + signature[1:]
    cases = [
        ({"cursor": payload + "." + changed_signature}, headers, "invalid_people_cursor"),
        ({"cursor": cursor}, _headers(app, other_actor, other_tenant), "invalid_people_cursor"),
        ({"cursor": cursor, "channel": "email"}, headers, "people_cursor_filter_mismatch"),
        ({"cursor": cursor, "q": "Synthetic"}, headers, "people_cursor_filter_mismatch"),
    ]
    for query, request_headers, reason in cases:
        response = client.get("/api/v2/crm/people", query_string=query, headers=request_headers)
        assert response.status_code == 400
        assert response.get_json()["reason_code"] == reason


def test_existing_canonical_compressed_and_uncompressed_signed_payloads_roundtrip(client, app):
    cursor, _, _, actor, _, tenant, _ = _directory_page(client, app)
    serializer = directory._serializer()
    payload = serializer.loads(cursor)
    assert cursor.startswith(".")  # The existing emitted cursor uses compression.
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    uncompressed = serializer.make_signer().sign(base64.urlsafe_b64encode(raw).rstrip(b"=")).decode("ascii")
    assert not uncompressed.startswith(".")
    expected = directory._decode_cursor(cursor, tenant_id=tenant.id, actor_id=actor.id, filters_hash=payload["filters_hash"])
    assert directory._decode_cursor(uncompressed, tenant_id=tenant.id, actor_id=actor.id, filters_hash=payload["filters_hash"]) == expected
