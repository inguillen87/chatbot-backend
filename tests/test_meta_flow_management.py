from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.meta_flow_json import (
    build_claim_evidence_flow,
    build_order_checkout_flow,
    canonical_flow_json,
)
from services.meta_flow_management import (
    MetaFlowGraphClient,
    MetaFlowManagementError,
    build_meta_flow_publication_readiness,
    meta_flow_category,
    resolve_meta_graph_credentials,
)


WABA_ID = "123456789012345"
META_FLOW_ID = "987654321012345"


class _Response(SimpleNamespace):
    def json(self):
        return self.payload


class _FakeMetaHttp:
    def __init__(self, document, *, remote_document=None, endpoint_uri=None):
        self.document = document
        self.remote_document = remote_document or document
        self.endpoint_uri = (
            endpoint_uri
            or "https://api.chatboc.test/api/whatsapp/flows/data-exchange/order"
        )
        self.calls = []
        self.flow_reads = 0

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = url.split("/v23.0/", 1)[1]
        if method == "POST" and path == f"{WABA_ID}/flows":
            assert kwargs["data"]["categories"] == '["OTHER"]'
            return _Response(status_code=200, payload={"id": META_FLOW_ID})
        if method == "POST" and path == f"{META_FLOW_ID}/assets":
            upload = kwargs["files"]["file"]
            assert upload[0] == "flow.json"
            assert upload[1].decode("utf-8") == canonical_flow_json(self.document)
            return _Response(
                status_code=200,
                payload={"success": True, "validation_errors": []},
            )
        if method == "POST" and path == f"{META_FLOW_ID}/publish":
            return _Response(status_code=200, payload={"success": True})
        if method == "GET" and path == META_FLOW_ID:
            self.flow_reads += 1
            status = "PUBLISHED" if self.flow_reads >= 3 else "DRAFT"
            return _Response(
                status_code=200,
                payload={
                    "id": META_FLOW_ID,
                    "name": "Pedido comercial",
                    "categories": ["OTHER"],
                    "status": status,
                    "validation_errors": [],
                    "json_version": "7.3",
                    "data_api_version": "3.0",
                    "data_channel_uri": self.endpoint_uri,
                    "whatsapp_business_account": {"id": WABA_ID},
                    "health_status": {"can_send_message": "AVAILABLE"},
                },
            )
        if method == "GET" and path == f"{META_FLOW_ID}/assets":
            return _Response(
                status_code=200,
                payload={
                    "data": [
                        {
                            "name": "flow.json",
                            "asset_type": "FLOW_JSON",
                            "download_url": "https://scontent.xx.fbcdn.net/flow.json?signed=1",
                        }
                    ]
                },
            )
        raise AssertionError((method, path))

    def get(self, url, **kwargs):
        self.calls.append(("GET_ASSET", url, kwargs))
        assert url.startswith("https://scontent.xx.fbcdn.net/")
        return _Response(status_code=200, payload=self.remote_document)


def _credentials():
    return resolve_meta_graph_credentials(
        waba_id=WABA_ID,
        app_config={
            "META_GRAPH_ACCESS_TOKEN": "system-user-token",
            "META_GRAPH_API_VERSION": "v23.0",
        },
        environ={},
    )


def test_meta_graph_readiness_is_fail_closed_and_never_exposes_token():
    missing = resolve_meta_graph_credentials(
        waba_id="",
        app_config={},
        environ={},
    )

    assert missing.ready is False
    assert missing.blockers == (
        "waba_id_not_configured",
        "meta_graph_access_token_not_configured",
    )
    assert "access_token" not in missing.public_payload()

    ready = _credentials()
    assert ready.ready is True
    assert ready.token_source == "platform"
    assert ready.public_payload()["configured"] is True
    assert "system-user-token" not in repr(ready)


def test_meta_graph_readiness_rejects_invalid_transport_configuration():
    credentials = resolve_meta_graph_credentials(
        waba_id=WABA_ID,
        app_config={
            "META_GRAPH_ACCESS_TOKEN": "system-user-token",
            "META_GRAPH_API_VERSION": "latest",
            "META_GRAPH_API_BASE_URL": "http://graph.facebook.com",
            "META_GRAPH_API_TIMEOUT_SECONDS": "not-a-number",
        },
        environ={},
    )

    assert credentials.ready is False
    assert credentials.blockers == (
        "meta_graph_api_version_invalid",
        "meta_graph_api_base_url_invalid",
        "meta_graph_api_timeout_invalid",
    )


def test_meta_graph_public_readiness_redacts_base_url_userinfo():
    credentials = resolve_meta_graph_credentials(
        waba_id=WABA_ID,
        app_config={
            "META_GRAPH_ACCESS_TOKEN": "system-user-token",
            "META_GRAPH_API_BASE_URL": "https://url-secret@graph.facebook.com",
        },
        environ={},
    )

    payload = credentials.public_payload()
    assert credentials.ready is False
    assert "meta_graph_api_base_url_invalid" in credentials.blockers
    assert payload["base_url"] == "https://graph.facebook.com"
    assert "url-secret" not in json.dumps(payload)
    assert "url-secret" not in repr(credentials)


def test_claim_evidence_publication_readiness_is_idempotent_and_tenant_bound():
    artifact = build_claim_evidence_flow()
    artifact_payload = {
        "publishable_flow_json": True,
        "validation": {"valid": True, "errors": []},
        "content_sha256": artifact.content_sha256,
        "flow_json_version": artifact.document["version"],
        "data_api_version": artifact.document["data_api_version"],
        "byte_size": artifact.byte_size,
        "endpoint_driven": True,
    }
    data_exchange = {
        "ready": True,
        "endpoint_url": "https://api.chatboc.test/api/whatsapp/flows/data-exchange/claims",
        "tenant_bound": True,
        "waba_bound": True,
        "blockers": [],
    }
    registry = {
        "configured": True,
        "tenant_id": "tenant-7",
        "status": "meta_published",
        "meta_flow_id": META_FLOW_ID,
        "flow_json_sha256": artifact.content_sha256,
        "waba_id": WABA_ID,
        "publication_verified": True,
        "sync_state": "complete",
        "last_sync_at": "2026-07-17T12:00:00+00:00",
    }

    readiness = build_meta_flow_publication_readiness(
        tenant_id="tenant-7",
        flow_id="claim_evidence",
        flow_name="chatboc_claim_evidence",
        blueprint_category="CUSTOMER_SUPPORT",
        artifact=artifact_payload,
        credentials=_credentials(),
        integration_access={"enabled": True},
        data_exchange=data_exchange,
        registry=registry,
    )

    assert readiness["status"] == "verification_ready"
    assert readiness["blockers"] == []
    assert readiness["operation"]["mode"] == "verify_published"
    assert readiness["operation"]["publish_write_expected"] is False
    assert readiness["idempotency"]["persisted_publication_match"] is True
    assert readiness["idempotency"]["same_confirmation_replay_allowed"] is False
    assert len(readiness["idempotency"]["operation_fingerprint"]) == 64
    assert readiness["dry_run"]["payload"]["flow_id"] == "claim_evidence"
    assert "system-user-token" not in json.dumps(readiness)

    wrong_scope = build_meta_flow_publication_readiness(
        tenant_id="tenant-8",
        flow_id="claim_evidence",
        flow_name="chatboc_claim_evidence",
        blueprint_category="CUSTOMER_SUPPORT",
        artifact=artifact_payload,
        credentials=_credentials(),
        integration_access={"enabled": True},
        data_exchange=data_exchange,
        registry=registry,
    )

    assert wrong_scope["blocked"] is True
    assert "meta_flow_registry_tenant_scope_mismatch" in wrong_scope["blockers"]
    assert wrong_scope["registry"]["meta_flow_id"] is None
    assert "meta_flow_id" not in wrong_scope["dry_run"]["payload"]


@pytest.mark.parametrize(
    ("flow_id", "category", "expected"),
    [
        ("claim_tracking", "municipal", "CUSTOMER_SUPPORT"),
        ("survey_vote", "", "SURVEY"),
        ("appointment_booking", "", "APPOINTMENT_BOOKING"),
        ("order_checkout", "commerce", "OTHER"),
    ],
)
def test_meta_flow_category_maps_chatboc_verticals(flow_id, category, expected):
    assert meta_flow_category(flow_id, category) == expected


def test_meta_graph_client_creates_uploads_publishes_and_verifies_exact_asset():
    artifact = build_order_checkout_flow()
    http = _FakeMetaHttp(artifact.document)
    result = MetaFlowGraphClient(_credentials(), http=http).provision_and_publish(
        flow_name="Pedido comercial",
        category="OTHER",
        document=artifact.document,
        expected_sha256=artifact.content_sha256,
        endpoint_uri="https://api.chatboc.test/api/whatsapp/flows/data-exchange/order",
    )

    assert result["created"] is True
    assert result["uploaded"] is True
    assert result["published_now"] is True
    assert result["verification"]["publication_verified"] is True
    assert result["verification"]["artifact_identity_verified"] is True
    assert result["verification"]["flow_json_sha256"] == artifact.content_sha256
    assert [call[0] for call in http.calls].count("POST") == 3


def test_meta_graph_client_clones_changed_published_flow_before_upload():
    artifact = build_order_checkout_flow()
    source_flow_id = "111111111111111"
    client = MetaFlowGraphClient(_credentials(), http=_FakeMetaHttp(artifact.document))
    client.get_flow = MagicMock(
        side_effect=[
            {
                "id": source_flow_id,
                "status": "PUBLISHED",
                "whatsapp_business_account": {"id": WABA_ID},
            },
            {
                "id": META_FLOW_ID,
                "status": "DRAFT",
                "validation_errors": [],
                "data_channel_uri": "https://api.chatboc.test/api/whatsapp/flows/data-exchange/order",
            },
            {
                "id": META_FLOW_ID,
                "status": "DRAFT",
                "validation_errors": [],
            },
        ]
    )
    client.verify_flow = MagicMock(
        side_effect=[
            {
                "verified": False,
                "flow_json_sha256": "0" * 64,
                "blockers": ["meta_flow_json_hash_mismatch"],
            },
            {
                "verified": True,
                "meta_flow_id": META_FLOW_ID,
                "status": "PUBLISHED",
                "flow_json_sha256": artifact.content_sha256,
                "blockers": [],
            },
        ]
    )
    client.create_flow = MagicMock(return_value=META_FLOW_ID)
    client.upload_flow_json = MagicMock(return_value={"success": True})
    client.publish_flow = MagicMock()

    result = client.provision_and_publish(
        flow_name="Pedido comercial",
        category="OTHER",
        document=artifact.document,
        expected_sha256=artifact.content_sha256,
        endpoint_uri="https://api.chatboc.test/api/whatsapp/flows/data-exchange/order",
        meta_flow_id=source_flow_id,
        clone_published_on_change=True,
    )

    assert result["created"] is True
    assert result["cloned_from_flow_id"] == source_flow_id
    assert result["uploaded"] is True
    assert result["published_now"] is True
    assert result["idempotent"] is False
    client.create_flow.assert_called_once_with(
        name="Pedido comercial",
        category="OTHER",
        endpoint_uri="https://api.chatboc.test/api/whatsapp/flows/data-exchange/order",
        clone_flow_id=source_flow_id,
    )
    client.upload_flow_json.assert_called_once()
    client.publish_flow.assert_called_once_with(META_FLOW_ID)


def test_meta_graph_client_dry_run_modes_block_remote_drift_without_writes():
    artifact = build_claim_evidence_flow()
    client = MetaFlowGraphClient(
        _credentials(),
        http=_FakeMetaHttp(artifact.document),
    )
    client.get_flow = MagicMock(
        return_value={
            "id": META_FLOW_ID,
            "status": "DRAFT",
            "whatsapp_business_account": {"id": WABA_ID},
        }
    )
    client.create_flow = MagicMock()
    client.upload_flow_json = MagicMock()
    client.publish_flow = MagicMock()

    with pytest.raises(MetaFlowManagementError) as caught:
        client.provision_and_publish(
            flow_name="chatboc_claim_evidence",
            category="CUSTOMER_SUPPORT",
            document=artifact.document,
            expected_sha256=artifact.content_sha256,
            endpoint_uri=(
                "https://api.chatboc.test/api/whatsapp/flows/data-exchange/claims"
            ),
            meta_flow_id=META_FLOW_ID,
            publish=True,
            verification_only=True,
        )

    assert caught.value.code == "meta_flow_verification_only_state_mismatch"

    with pytest.raises(MetaFlowManagementError) as clone_caught:
        client.provision_and_publish(
            flow_name="chatboc_claim_evidence",
            category="CUSTOMER_SUPPORT",
            document=artifact.document,
            expected_sha256=artifact.content_sha256,
            endpoint_uri=(
                "https://api.chatboc.test/api/whatsapp/flows/data-exchange/claims"
            ),
            meta_flow_id=META_FLOW_ID,
            publish=True,
            clone_published_on_change=True,
        )

    assert clone_caught.value.code == "meta_flow_clone_source_state_mismatch"
    client.create_flow.assert_not_called()
    client.upload_flow_json.assert_not_called()
    client.publish_flow.assert_not_called()


def test_meta_graph_client_rejects_published_asset_hash_mismatch():
    artifact = build_order_checkout_flow()
    wrong_document = {
        **artifact.document,
        "screens": [
            {
                **artifact.document["screens"][0],
                "title": "Contenido remoto adulterado",
            },
            artifact.document["screens"][1],
        ],
    }
    http = _FakeMetaHttp(artifact.document, remote_document=wrong_document)

    with pytest.raises(MetaFlowManagementError) as caught:
        MetaFlowGraphClient(_credentials(), http=http).provision_and_publish(
            flow_name="Pedido comercial",
            category="OTHER",
            document=artifact.document,
            expected_sha256=artifact.content_sha256,
            endpoint_uri="https://api.chatboc.test/api/whatsapp/flows/data-exchange/order",
        )

    assert caught.value.code == "meta_flow_verification_failed"
    assert "meta_flow_json_hash_mismatch" in caught.value.details["blockers"]


def test_meta_graph_client_rejects_non_meta_asset_host():
    artifact = build_order_checkout_flow()
    http = _FakeMetaHttp(artifact.document)
    client = MetaFlowGraphClient(_credentials(), http=http)

    with pytest.raises(MetaFlowManagementError) as caught:
        client._validate_asset_url("https://attacker.example/flow.json")

    assert caught.value.code == "meta_flow_asset_url_invalid"


def test_meta_graph_client_rejects_redirect_to_non_meta_asset_host():
    artifact = build_order_checkout_flow()
    http = _FakeMetaHttp(artifact.document)

    def redirecting_get(url, **kwargs):
        http.calls.append(("GET_ASSET", url, kwargs))
        return _Response(
            status_code=302,
            payload={},
            headers={"Location": "https://attacker.example/stolen-flow.json"},
        )

    http.get = redirecting_get
    client = MetaFlowGraphClient(_credentials(), http=http)

    with pytest.raises(MetaFlowManagementError) as caught:
        client.download_flow_json(META_FLOW_ID)

    assert caught.value.code == "meta_flow_asset_url_invalid"
    assert [call[0] for call in http.calls].count("GET_ASSET") == 1


def test_meta_graph_client_blocks_endpoint_mismatch_before_irreversible_publish():
    artifact = build_order_checkout_flow()
    http = _FakeMetaHttp(
        artifact.document,
        endpoint_uri="https://api.chatboc.test/wrong-tenant-endpoint",
    )

    with pytest.raises(MetaFlowManagementError) as caught:
        MetaFlowGraphClient(_credentials(), http=http).provision_and_publish(
            flow_name="Pedido comercial",
            category="OTHER",
            document=artifact.document,
            expected_sha256=artifact.content_sha256,
            endpoint_uri="https://api.chatboc.test/api/whatsapp/flows/data-exchange/order",
        )

    assert caught.value.code == "meta_flow_endpoint_uri_mismatch"
    assert caught.value.details["meta_flow_id"] == META_FLOW_ID
    assert not any(
        call[0] == "POST" and call[1].endswith(f"/{META_FLOW_ID}/publish")
        for call in http.calls
    )
