"""Offline contract for the direct signup Graph adapter."""
import json
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from services import meta_credential_operations as ops
from services.meta_direct_signup_graph import DirectSignupGraph
from services.meta_whatsapp_runtime import APP_SECRETS_KEY, GRAPH_VERSION_KEY, MetaRuntimeError


NOW = 1_800_000_000
WABA = "8111111111"
PHONE = "8222222222"
CODE = "synthetic.single-use-code"
TOKEN = "synthetic.business-token"
SECRET = "synthetic-private-app-secret"
SCOPES = ["whatsapp_business_management", "whatsapp_business_messaging"]


@pytest.fixture
def provider():
    clock = [NOW]
    debug = {"app_id": ops.APP_ID, "is_valid": True, "scopes": SCOPES.copy(),
             "expires_at": NOW + 900, "data_access_expires_at": NOW + 800,
             "granular_scopes": [{"scope": s, "target_ids": [WABA]} for s in SCOPES]}
    phone = {"id": PHONE, "display_phone_number": "+1 (202) 555-0100", "status": "CONNECTED"}
    preexisting = {"data": []}
    replies = {
        ("POST", "oauth/access_token"): {"access_token": TOKEN, "token_type": "bearer"},
        ("POST", "debug_token"): {"data": debug},
        ("GET", WABA + "/phone_numbers"): {"data": [phone]},
        ("POST", WABA + "/subscribed_apps"): {"success": True},
        ("GET", WABA + "/subscribed_apps"): {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID}}]},
    }
    calls = []

    def request(method, url, **kwargs):
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc, parts.query, parts.fragment) == ("https", "graph.facebook.com", "", "")
        assert parts.path.startswith("/v25.0/")
        key = (method, parts.path.removeprefix("/v25.0/"))
        calls.append((key, kwargs))
        outcome = replies[key]
        if key == ("GET", WABA + "/subscribed_apps") and not any(
                name == ("POST", WABA + "/subscribed_apps") for name, _ in calls):
            outcome = preexisting
        if isinstance(outcome, Exception):
            logging.getLogger(__name__).warning("%s:%s:%s", SECRET, CODE, TOKEN)
            raise outcome
        if callable(outcome):
            outcome = outcome()
        status, body = outcome if isinstance(outcome, tuple) else (200, outcome)
        return SimpleNamespace(status_code=status, content=json.dumps(body).encode())

    graph = DirectSignupGraph({APP_SECRETS_KEY: {ops.APP_ID: SECRET}}, graph_request=request,
                              clock=lambda: clock[0])
    return SimpleNamespace(graph=graph, calls=calls, replies=replies, clock=clock, debug=debug,
                           phone=phone, preexisting=preexisting)


def test_exchange_asset_validation_and_readback_keep_secrets_off_request_targets(provider):
    p = provider
    proof = p.graph.exchange_and_verify(CODE, WABA, PHONE)
    assert (proof.phone, proof.registered) == ("+12025550100", True)
    assert proof.credential.access_token == TOKEN
    assert proof.credential.expires_at == NOW + 800
    assert TOKEN not in repr(proof.credential) + repr(proof)
    p.graph.subscribe_and_verify(WABA, proof.credential)
    assert [key for key, _ in p.calls] == [
        ("POST", "oauth/access_token"), ("POST", "debug_token"), ("GET", WABA + "/phone_numbers"),
        ("GET", WABA + "/subscribed_apps"), ("POST", WABA + "/subscribed_apps"),
        ("GET", WABA + "/subscribed_apps")]
    exchange, debug, phones, initial, subscription, listing = [kwargs for _, kwargs in p.calls]
    assert exchange["data"] == {"code": CODE, "client_id": ops.APP_ID, "client_secret": SECRET,
                                "grant_type": "authorization_code"}
    assert exchange["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert debug["data"] == {"method": "GET", "input_token": TOKEN}
    assert debug["headers"]["Authorization"] == "Bearer " + ops.APP_ID + "|" + SECRET
    assert phones["params"]["fields"] == "id,display_phone_number,status"
    assert subscription["json"] == {}
    for opts in (phones, initial, subscription, listing):
        assert opts["headers"]["Authorization"] == "Bearer " + TOKEN
    for _, opts in p.calls:
        assert opts["verify"] is True and opts["allow_redirects"] is False
        assert all(value not in repr(opts.get("params")) for value in (CODE, SECRET, TOKEN))


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("app_id", "8999999999", "meta_token_authority_invalid"),
    ("is_valid", False, "meta_token_authority_invalid"),
    ("scopes", SCOPES[:1], "meta_token_authority_invalid"),
    ("granular_scopes", [{"scope": s, "target_ids": ["8999999999"]} for s in SCOPES],
     "meta_token_granular_scope_mismatch"),
    ("granular_scopes", None, "meta_token_granular_scope_mismatch"),
    ("expires_at", NOW, "meta_token_authority_expired"),
    ("data_access_expires_at", NOW - 1, "meta_token_authority_expired"),
])
def test_authority_must_match_before_asset_request(provider, field, value, reason):
    provider.debug[field] = value
    with pytest.raises(MetaRuntimeError, match=reason):
        provider.graph.exchange_and_verify(CODE, WABA, PHONE)
    assert len(provider.calls) == 2


@pytest.mark.parametrize("payload", [
    {"data": [{"id": "8333333333"}]},
    {"data": [{"id": PHONE}, {"id": PHONE}]},
    {"data": [{"id": PHONE}], "paging": {"next": "redacted"}},
    {"data": [{"id": PHONE, "display_phone_number": "not a number"}]},
    {"data": [{"id": PHONE, "display_phone_number": "+00001234"}]},
])
def test_unverified_or_incomplete_phone_is_rejected(provider, payload):
    provider.replies["GET", WABA + "/phone_numbers"] = payload
    with pytest.raises(MetaRuntimeError, match="meta_phone_membership_unverified"):
        provider.graph.exchange_and_verify(CODE, WABA, PHONE)
    assert len(provider.calls) == 3


@pytest.mark.parametrize("status", ["PENDING", None])
def test_registration_requires_positive_status_from_graph(provider, status):
    provider.phone.pop("status")
    if status is not None:
        provider.phone["status"] = status
    assert provider.graph.exchange_and_verify(CODE, WABA, PHONE).registered is False


@pytest.mark.parametrize("config", [
    {"META_TDF_SANDBOX_APP_SECRET": SECRET},
    {APP_SECRETS_KEY: {"8333333333": SECRET}},
    {APP_SECRETS_KEY: {ops.APP_ID: SECRET}, GRAPH_VERSION_KEY: "https://invalid.test"},
])
def test_unconfigured_platform_app_rejects_before_exchange(config):
    with pytest.raises(MetaRuntimeError, match="meta_app_authority_unconfigured"):
        DirectSignupGraph(config)


@pytest.mark.parametrize(("post", "listing", "calls"), [
    ({"success": 1}, None, 1),
    ({"success": False}, None, 1),
    ({"success": True}, {"data": [{"whatsapp_business_api_data": {"id": "8333333333"}}]}, 2),
    ({"success": True}, {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID}}],
                         "paging": {"next": "redacted"}}, 2),
    ({"success": True}, {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID},
                                   "override_callback_uri": "https://example.invalid/alternate"}]}, 2),
])
def test_subscription_requires_ack_and_complete_app_specific_readback(provider, post, listing, calls):
    proof = provider.graph.exchange_and_verify(CODE, WABA, PHONE)
    provider.replies["POST", WABA + "/subscribed_apps"] = post
    provider.replies["GET", WABA + "/subscribed_apps"] = listing
    with pytest.raises(MetaRuntimeError, match="meta_app_subscription_unverified"):
        provider.graph.subscribe_and_verify(WABA, proof.credential)
    assert len(provider.calls) == 4 + calls


@pytest.mark.parametrize("preexisting", [
    {"data": [], "paging": {"next": "redacted"}},
    {"data": [{"unexpected": "shape"}]},
    {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID}}] * 2},
    {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID},
               "override_callback_uri": "https://example.invalid/other-destination"}]},
])
def test_inconclusive_preexisting_subscription_never_posts(provider, preexisting):
    proof = provider.graph.exchange_and_verify(CODE, WABA, PHONE)
    provider.preexisting.update(preexisting)
    with pytest.raises(MetaRuntimeError, match="meta_app_subscription_unverified"):
        provider.graph.subscribe_and_verify(WABA, proof.credential)
    assert len(provider.calls) == 4 and provider.calls[-1][0][0] == "GET"


def test_resumption_rechecks_without_exchange_or_duplicate_subscription(provider):
    proof = provider.graph.exchange_and_verify(CODE, WABA, PHONE)
    provider.calls.clear()
    provider.debug["expires_at"] = NOW + 600
    provider.preexisting.update({"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID}}]})
    verified = provider.graph.verify_credential(WABA, PHONE, proof.credential)
    provider.graph.subscribe_and_verify(WABA, verified.credential)
    assert verified.credential.expires_at == NOW + 600
    assert [key for key, _ in provider.calls] == [
        ("POST", "debug_token"), ("GET", WABA + "/phone_numbers"), ("GET", WABA + "/subscribed_apps")]


@pytest.mark.parametrize("outcome", [
    (400, {"error": {"message": CODE + TOKEN + SECRET}}),
    RuntimeError(CODE + TOKEN + SECRET),
])
def test_single_exchange_and_provider_failures_are_redacted(provider, outcome, caplog):
    provider.replies["POST", "oauth/access_token"] = outcome
    with pytest.raises(MetaRuntimeError) as caught:
        provider.graph.exchange_and_verify(CODE, WABA, PHONE)
    assert len(provider.calls) == 1
    assert all(secret not in caplog.text + str(caught.value) for secret in (CODE, TOKEN, SECRET))


def test_verification_cannot_outlive_provider_deadline(provider):
    provider.debug["expires_at"] = NOW + 5

    def listed_after_expiry():
        provider.clock[0] += 6
        return {"data": [provider.phone]}

    provider.replies["GET", WABA + "/phone_numbers"] = listed_after_expiry
    with pytest.raises(MetaRuntimeError, match="meta_token_authority_expired"):
        provider.graph.exchange_and_verify(CODE, WABA, PHONE)


def test_invalid_asset_id_does_not_consume_code(provider):
    with pytest.raises(MetaRuntimeError, match="meta_binding_invalid"):
        provider.graph.exchange_and_verify(CODE, WABA, "../invalid")
    assert not provider.calls
