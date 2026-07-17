from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import db
from models import (
    AuditEvent,
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    MessageTemplateRegistry,
    MessagingEventLedger,
    Order,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    User,
    WhatsAppEnterpriseRule,
    WhatsAppFlowInteraction,
)
from routes.whatsapp_rules import _flow_interaction_payload, _sync_status_from_approval
from services.meta_flow_json import (
    build_claim_evidence_flow,
    build_order_checkout_flow,
    build_survey_vote_flow,
)
from services.meta_flow_management import MetaFlowManagementError
from services.whatsapp_experience import build_whatsapp_experience


META_FLOW_ID = "1232445823264765"
META_WABA_ID = "109876543210987"
FLOW_ID = "order_checkout"
CONCEPTUAL_FLOW_ID = "catalog_order_builder"
ORDER_CONTEXT = {"kind": "order", "id": "order-flow-send"}


def test_rejected_or_disabled_approval_is_never_reported_pending():
    assert _sync_status_from_approval("rejected", submitted=True) == "rejected"
    assert _sync_status_from_approval("disabled", submitted=True) == "disabled"
    assert _sync_status_from_approval("unsubmitted", submitted=True) == "created"


def test_application_registers_meta_data_exchange_endpoint_fail_closed(client):
    response = client.post(
        "/api/whatsapp/flows/data-exchange/not-configured",
        json={},
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "endpoint_not_found"


def test_flow_retry_contract_never_claims_same_key_retry_is_safe():
    payload = _flow_interaction_payload(
        SimpleNamespace(
            id=1,
            flow_id=FLOW_ID,
            meta_flow_id=META_FLOW_ID,
            content_sid="HXfailed",
            recipient_hint="***6799",
            status="failed",
            external_message_sid="SMFAILED",
            expires_at=None,
            consumed_at=None,
        )
    )

    assert payload["retry_safe"] is False
    assert payload["retry_mode"] == "new_invocation_after_review"
    assert payload["reconciliation_required"] is False


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed(*, plan: str = "full") -> tuple[User, TenantProfile]:
    admin = User(
        email=f"meta-ops-{plan}@test.com",
        name="Meta Ops Admin",
        rol="tenant_admin",
        tipo_chat="pyme",
    )
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"meta-ops-{plan}",
        nombre="Meta Ops",
        tipo="pyme",
        pyme_id=admin.id,
        plan=plan,
        configuracion={"whatsapp_phone": "+5491112345678"},
    )
    db.session.add(tenant)
    db.session.commit()
    return admin, tenant


def _prepare_ready_flow_send(app, tenant: TenantProfile):
    app.config["TWILIO_ACCOUNT_SID"] = "ACparent"
    app.config["TWILIO_AUTH_TOKEN"] = "parent-secret"
    app.config["TWILIO_SUBACCOUNT_AUTH_TOKEN_ACFLOWSEND"] = "flow-send-secret"
    app.config["WHATSAPP_FLOW_TOKEN_KEY_V1"] = (
        "test-native-flow-send-key-v1-0000000000000000000000000000"
    )
    app.config["WHATSAPP_FLOW_TOKEN_TTL_SECONDS"] = 3600
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    app.config["PUBLIC_API_BASE_URL"] = "https://api.chatboc.test"
    app.config["META_FLOW_DATA_EXCHANGE_ENDPOINTS"] = {
        "flow-send-endpoint": {
            "endpoint_id": "flow-send-endpoint",
            "tenant_id": str(tenant.id),
            "waba_id": "waba-flow-send",
            "private_key_pem": private_key_pem,
            "app_secret": "meta-flow-send-test-secret",
            "handlers": {
                "init": lambda payload, context: {"screen": "ORDER_DETAILS", "data": {}},
                "back": lambda payload, context: {"screen": "ORDER_DETAILS", "data": {}},
                "data_exchange": lambda payload, context: {
                    "screen": "ORDER_CONFIRM",
                    "data": {"order_summary": "Pedido", "total_display": "$ 1"},
                },
            },
        }
    }
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="connected",
        external_account_id="ACflowsend",
        credentials_ref="env:TWILIO_SUBACCOUNT_AUTH_TOKEN_ACFLOWSEND",
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        phone_number="+5491100000000",
        sender_id="whatsapp:+5491100000000",
        waba_id="waba-flow-send",
        status="active",
        status_callback_url="https://api.chatboc.test/twilio/whatsapp/status",
        metadata_json={"meta_flow_data_exchange_endpoint_id": "flow-send-endpoint"},
    )
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
        category="UTILITY",
        status="approved",
        content_sid="HXflowtosend",
        external_template_id=META_FLOW_ID,
        body_preview="Completa tu pedido en WhatsApp.",
        metadata_json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
            "content_family": "meta_native_flow",
            "approval_status": "approved",
            "meta_flow_status": "published",
            "flow_json_sha256": build_order_checkout_flow().content_sha256,
            "meta_flow_publication_verified": True,
            "data_contract": ["catalog_items", "cart_id", "contact_key"],
        },
    )
    order = Order(
        id=ORDER_CONTEXT["id"],
        tenant_id=tenant.id,
        buyer_name="Cliente Flow",
        status="created",
        channel="whatsapp",
        currency="ARS",
        total=12500,
    )
    db.session.add_all([sender, registry, order])
    db.session.commit()
    return sender, registry


def _prepare_ready_survey_flow_send(app, tenant: TenantProfile):
    sender, registry = _prepare_ready_flow_send(app, tenant)
    artifact = build_survey_vote_flow()
    registry.name = "chatboc_survey_vote_native_v1"
    registry.body_preview = "Participa de la votacion desde WhatsApp."
    registry.metadata_json = {
        **dict(registry.metadata_json or {}),
        "flow_id": "survey_vote",
        "flow_json_sha256": artifact.content_sha256,
        "data_contract": ["confirm_vote"],
    }
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=f"{tenant.slug}-quick-vote",
        titulo="Prioridades del barrio",
        estado="publicada",
        tipo="votacion",
        es_votacion_envivo=True,
        mostrar_resultados_envivo=True,
        politica_unicidad="por_cookie",
    )
    question = EncPregunta(
        orden=1,
        tipo="opcion_unica",
        texto="Que mejora deberia priorizarse?",
        obligatoria=True,
    )
    question.opciones = [
        EncOpcion(orden=1, texto="Iluminacion"),
        EncOpcion(orden=2, texto="Arreglo de calles"),
    ]
    survey.preguntas = [question]
    db.session.add_all([registry, survey])
    db.session.commit()
    return sender, registry, survey


def _prepare_meta_management(app, tenant: TenantProfile, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    config = {
        "META_GRAPH_ACCESS_TOKEN": "meta-system-user-token",
        "META_GRAPH_API_VERSION": "v23.0",
        "PUBLIC_API_BASE_URL": "https://api.chatboc.test",
        "WHATSAPP_FLOW_TOKEN_KEY_V1": (
            "test-meta-management-flow-key-000000000000000000000000000"
        ),
        "META_FLOW_DATA_EXCHANGE_ENDPOINTS": {
            "meta-management-endpoint": {
                "endpoint_id": "meta-management-endpoint",
                "tenant_id": str(tenant.id),
                "waba_id": META_WABA_ID,
                "private_key_pem": private_key_pem,
                "app_secret": "meta-management-app-secret",
                "handlers": {
                    "init": lambda payload, context: {
                        "screen": "ORDER_DETAILS",
                        "data": {},
                    },
                    "back": lambda payload, context: {
                        "screen": "ORDER_DETAILS",
                        "data": {},
                    },
                    "data_exchange": lambda payload, context: {
                        "screen": "ORDER_CONFIRM",
                        "data": {
                            "order_summary": "Pedido",
                            "total_display": "$ 1",
                        },
                    },
                },
            }
        },
    }
    for key, value in config.items():
        monkeypatch.setitem(app.config, key, value)
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="connected",
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        phone_number="+5491100000100",
        sender_id="whatsapp:+5491100000100",
        waba_id=META_WABA_ID,
        status="active",
        metadata_json={
            "meta_flow_data_exchange_endpoint_id": "meta-management-endpoint"
        },
    )
    db.session.add(sender)
    db.session.commit()
    return sender


def test_native_flow_sync_dry_run_builds_exact_twilio_content_contract(client, app):
    admin, tenant = _seed()
    response = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert payload["ready_to_create"] is True
    assert payload["blocked"] is False
    assert payload["content_family"] == "meta_native_flow"
    assert isinstance(payload["execute_confirmation"], str)
    assert len(payload["execute_confirmation"]) > 40
    assert FLOW_ID not in payload["execute_confirmation"]
    assert payload["approval_request"]["category"] == "UTILITY"
    assert payload["flow"]["first_screen_id"] == "ORDER_DETAILS"
    assert payload["flow"]["flow_json_version"] == "7.3"
    assert payload["flow"]["data_api_version"] == "3.0"
    assert len(payload["flow"]["content_sha256"]) == 64
    assert payload["flow"]["meta_flow_json_upload_performed"] is False
    assert payload["flow"]["meta_flow_id"] == META_FLOW_ID

    create_request = payload["create_request"]
    assert create_request["variables"] == {"1": "session-demo-123456"}
    flow_type = create_request["types"]["whatsapp/flows"]
    assert flow_type["flow_id"] == META_FLOW_ID
    assert flow_type["flow_token"] == "{{1}}"
    assert flow_type["flow_first_page_id"] == "ORDER_DETAILS"
    assert flow_type["is_flow_first_page_endpoint"] is True

    invalid = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": FLOW_ID, "meta_flow_id": "demo-flow"},
    )
    assert invalid.status_code == 400


def test_claim_evidence_native_flow_is_available_to_meta_and_twilio_sync(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": "claim_evidence", "meta_flow_id": META_FLOW_ID},
    )

    assert response.status_code == 200
    payload = response.get_json()
    artifact = build_claim_evidence_flow()
    assert payload["ready_to_create"] is True
    assert payload["flow"]["first_screen_id"] == "CLAIM_EVIDENCE_LOOKUP"
    assert payload["flow"]["screen_ids"] == [
        "CLAIM_EVIDENCE_LOOKUP",
        "CLAIM_EVIDENCE_PHOTOS",
        "CLAIM_EVIDENCE_DOCUMENTS",
        "CLAIM_EVIDENCE_SUCCESS",
    ]
    assert payload["flow"]["data_contract"] == [
        "ticket_number",
        "photos",
        "documents",
    ]
    assert payload["flow"]["content_sha256"] == artifact.content_sha256
    assert payload["approval_request"]["category"] == "UTILITY"
    flow_type = payload["create_request"]["types"]["whatsapp/flows"]
    assert flow_type["flow_id"] == META_FLOW_ID
    assert flow_type["flow_first_page_id"] == "CLAIM_EVIDENCE_LOOKUP"
    assert flow_type["is_flow_first_page_endpoint"] is True

    download = client.get(
        "/api/admin/whatsapp/flows/claim_evidence/flow-json",
        headers=headers,
    )
    assert download.status_code == 200
    assert download.headers["X-Flow-Content-SHA256"] == artifact.content_sha256


def test_native_flow_sync_rejects_conceptual_design_without_compiled_flow_json(client, app):
    admin, tenant = _seed()

    response = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": CONCEPTUAL_FLOW_ID, "meta_flow_id": META_FLOW_ID},
    )

    assert response.status_code == 422
    assert "Flow JSON" in response.get_json()["message"]


def test_admin_can_download_exact_validated_meta_flow_json_artifact(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.get(
        f"/api/admin/whatsapp/flows/{FLOW_ID}/flow-json",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="{FLOW_ID}.flow.json"'
    )
    assert response.headers["X-Flow-JSON-Version"] == "7.3"
    assert response.headers["X-Flow-Data-API-Version"] == "3.0"
    content_sha256 = hashlib.sha256(response.data).hexdigest()
    assert response.headers["X-Flow-Content-SHA256"] == content_sha256
    assert response.headers["ETag"] == f'"sha256-{content_sha256}"'
    document = json.loads(response.data)
    assert document["version"] == "7.3"
    assert document["data_api_version"] == "3.0"
    assert [screen["id"] for screen in document["screens"]] == [
        "ORDER_DETAILS",
        "ORDER_CONFIRM",
    ]

    conceptual = client.get(
        f"/api/admin/whatsapp/flows/{CONCEPTUAL_FLOW_ID}/flow-json",
        headers=headers,
    )
    assert conceptual.status_code == 404


def test_claim_evidence_admin_readiness_is_discoverable_and_side_effect_free(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed()
    _prepare_meta_management(app, tenant, monkeypatch)
    headers = _auth_headers(app, admin, tenant.slug)

    with patch("routes.whatsapp_rules.MetaFlowGraphClient") as graph_client:
        response = client.get(
            "/api/admin/whatsapp/flows/meta/readiness?flow_id=claim_evidence",
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert payload["meta_graph_calls_performed"] is False
    assert payload["twilio_calls_performed"] is False
    assert payload["provider_writes_performed"] is False
    assert payload["messages_sent"] is False
    assert payload["claim_evidence_discoverable"] is True
    assert payload["summary"] == {
        "total": 1,
        "ready": 1,
        "blocked": 0,
        "verification_ready": 0,
    }
    flow = payload["flows"][0]
    assert flow["id"] == "claim_evidence"
    assert flow["flow_json_download_url"].endswith(
        "/claim_evidence/flow-json"
    )
    readiness = flow["readiness"]
    assert readiness["status"] == "publish_ready"
    assert readiness["blockers"] == []
    assert readiness["operation"]["mode"] == "create_and_publish"
    assert readiness["dry_run"]["payload"] == {
        "flow_id": "claim_evidence",
        "language": "es",
        "dry_run": True,
        "publish": True,
    }
    assert readiness["dry_run"]["meta_graph_calls_performed"] is False
    assert readiness["dry_run"]["twilio_calls_performed"] is False
    assert readiness["dry_run"]["provider_writes_performed"] is False
    assert readiness["dry_run"]["messages_sent"] is False
    assert readiness["dry_run"]["execute_confirmation_issued"] is False
    assert payload["security"]["execute_confirmation_issued"] is False
    graph_client.assert_not_called()
    assert MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_claim_evidence_native_v1",
    ).count() == 0
    serialized = json.dumps(payload)
    assert "meta-system-user-token" not in serialized
    assert "meta-management-app-secret" not in serialized
    assert "BEGIN PRIVATE KEY" not in serialized

    monkeypatch.setitem(
        app.config,
        "PUBLIC_API_BASE_URL",
        "https://base-url-secret@api.chatboc.test",
    )
    invalid_base = client.get(
        "/api/admin/whatsapp/flows/meta/readiness?flow_id=claim_evidence",
        headers=headers,
    )
    invalid_payload = invalid_base.get_json()
    invalid_readiness = invalid_payload["flows"][0]["readiness"]
    assert invalid_base.status_code == 200
    assert invalid_readiness["blocked"] is True
    assert "data_exchange_not_ready" in invalid_readiness["blockers"]
    assert invalid_readiness["dependencies"]["data_exchange"]["endpoint_uri"] is None
    assert "base-url-secret" not in json.dumps(invalid_payload)


def test_claim_evidence_readiness_uses_authenticated_tenant_without_header(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed()
    _, foreign_tenant = _seed(plan="free")
    _prepare_meta_management(app, tenant, monkeypatch)
    app.config["TENANT_DOMAIN_MAP"] = {"api.chatboc.test": foreign_tenant.slug}
    headers = _auth_headers(app, admin, tenant.slug)
    headers.pop("X-Tenant")

    response = client.get(
        "/api/admin/whatsapp/flows/meta/readiness?flow_id=claim_evidence",
        headers=headers,
        base_url="https://api.chatboc.test",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["tenant"] == {"id": tenant.id, "slug": tenant.slug}
    assert payload["security"]["tenant_scoped"] is True


def test_claim_evidence_admin_readiness_does_not_discover_foreign_registry(
    client,
    app,
):
    admin, tenant = _seed()
    _, foreign_tenant = _seed(plan="free")
    foreign_meta_flow_id = "777777777777777"
    artifact = build_claim_evidence_flow()
    db.session.add(
        MessageTemplateRegistry(
            tenant_id=foreign_tenant.id,
            provider="twilio",
            channel="whatsapp",
            name="chatboc_claim_evidence_native_v1",
            language="es",
            category="UTILITY",
            status="meta_published",
            external_template_id=foreign_meta_flow_id,
            metadata_json={
                "flow_id": "claim_evidence",
                "meta_flow_id": foreign_meta_flow_id,
                "content_family": "meta_native_flow",
                "flow_json_sha256": artifact.content_sha256,
                "meta_flow_publication_verified": True,
            },
        )
    )
    db.session.commit()

    cross_tenant = client.get(
        "/api/admin/whatsapp/flows/meta/readiness?flow_id=claim_evidence",
        headers=_auth_headers(app, admin, foreign_tenant.slug),
    )
    assert cross_tenant.status_code == 403

    response = client.get(
        "/api/admin/whatsapp/flows/meta/readiness?flow_id=claim_evidence",
        headers=_auth_headers(app, admin, tenant.slug),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["tenant"]["id"] == tenant.id
    readiness = payload["flows"][0]["readiness"]
    assert readiness["registry"]["configured"] is False
    assert readiness["registry"]["meta_flow_id"] is None
    assert "meta_flow_id" not in readiness["dry_run"]["payload"]
    assert foreign_meta_flow_id not in json.dumps(payload)


def test_claim_evidence_readiness_matches_idempotent_meta_sync_dry_run(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed()
    _prepare_meta_management(app, tenant, monkeypatch)
    headers = _auth_headers(app, admin, tenant.slug)
    artifact = build_claim_evidence_flow()
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_claim_evidence_native_v1",
        language="es",
        category="UTILITY",
        status="meta_published",
        external_template_id=META_FLOW_ID,
        last_sync_at=datetime.now(timezone.utc),
        metadata_json={
            "flow_id": "claim_evidence",
            "meta_flow_id": META_FLOW_ID,
            "content_family": "meta_native_flow",
            "flow_json_sha256": artifact.content_sha256,
            "meta_flow_waba_id": META_WABA_ID,
            "meta_flow_publication_verified": True,
            "meta_sync_state": "complete",
        },
    )
    db.session.add(registry)
    db.session.commit()

    catalog = client.get(
        "/api/admin/whatsapp/flows/meta/readiness?flow_id=claim_evidence",
        headers=headers,
    )
    assert catalog.status_code == 200
    catalog_readiness = catalog.get_json()["flows"][0]["readiness"]
    assert catalog_readiness["status"] == "verification_ready"
    assert catalog_readiness["operation"]["mode"] == "verify_published"
    assert catalog_readiness["operation"]["publish_write_expected"] is False
    assert catalog_readiness["operation"]["irreversible_publish"] is False
    assert catalog_readiness["registry"]["id"] == registry.id
    assert (
        catalog_readiness["idempotency"]["persisted_publication_match"]
        is True
    )

    with patch("routes.whatsapp_rules.MetaFlowGraphClient") as graph_client:
        preview = client.post(
            "/api/admin/whatsapp/flows/meta/sync",
            headers=headers,
            json={"flow_id": "claim_evidence"},
        )

    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["dry_run"] is True
    assert preview_payload["ready_to_sync"] is True
    assert preview_payload["management"]["operation_mode"] == "verify_published"
    assert preview_payload["management"]["irreversible_publish"] is False
    assert preview_payload["readiness"]["status"] == "verification_ready"
    assert (
        preview_payload["readiness"]["idempotency"]["operation_fingerprint"]
        == catalog_readiness["idempotency"]["operation_fingerprint"]
    )
    assert isinstance(preview_payload["execute_confirmation"], str)
    graph_client.assert_not_called()
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="whatsapp_flow.meta_published_verified",
    ).count() == 0


def test_meta_flow_management_publishes_with_remote_attestation_and_binds_wrapper(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed()
    _prepare_meta_management(app, tenant, monkeypatch)
    headers = _auth_headers(app, admin, tenant.slug)
    artifact = build_order_checkout_flow()

    preview = client.post(
        "/api/admin/whatsapp/flows/meta/sync",
        headers=headers,
        json={"flow_id": FLOW_ID},
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["ready_to_sync"] is True
    assert preview_payload["management"]["irreversible_publish"] is True
    assert preview_payload["management"]["graph"]["ready"] is True
    assert "meta-system-user-token" not in json.dumps(preview_payload)

    verification = {
        "verified": True,
        "publication_verified": True,
        "artifact_identity_verified": True,
        "meta_flow_id": META_FLOW_ID,
        "status": "PUBLISHED",
        "waba_id": META_WABA_ID,
        "flow_json_sha256": artifact.content_sha256,
        "blockers": [],
    }
    graph_client = MagicMock()
    graph_client.provision_and_publish.return_value = {
        "created": True,
        "uploaded": True,
        "published_now": True,
        "idempotent": False,
        "verification": verification,
    }
    with patch(
        "routes.whatsapp_rules.MetaFlowGraphClient",
        return_value=graph_client,
    ):
        execute = client.post(
            "/api/admin/whatsapp/flows/meta/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "dry_run": False,
                "publish": True,
                "execute_confirmation": preview_payload["execute_confirmation"],
            },
        )

    assert execute.status_code == 200
    executed = execute.get_json()
    assert executed["meta_flow_id"] == META_FLOW_ID
    assert executed["next_action"] == "create_twilio_wrapper"
    attestation = executed["meta_publication_attestation"]
    assert isinstance(attestation, str)
    assert META_FLOW_ID not in attestation
    graph_client.provision_and_publish.assert_called_once_with(
        flow_name="chatboc_order_checkout",
        category="OTHER",
        document=artifact.document,
        expected_sha256=artifact.content_sha256,
        endpoint_uri=(
            "https://api.chatboc.test/api/whatsapp/flows/data-exchange/"
            "meta-management-endpoint"
        ),
        meta_flow_id=None,
        publish=True,
        clone_published_on_change=False,
        verification_only=False,
    )

    registry = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
    ).one()
    assert registry.content_sid is None
    assert registry.external_template_id == META_FLOW_ID
    assert registry.status == "meta_published"
    assert registry.metadata_json["meta_sync_state"] == "complete"
    assert registry.metadata_json["meta_flow_publication_verified"] is True
    assert registry.metadata_json["flow_json_sha256"] == artifact.content_sha256
    assert registry.metadata_json["meta_flow_waba_id"] == META_WABA_ID
    assert (
        registry.metadata_json["meta_sync_operation_fingerprint"]
        == preview_payload["readiness"]["idempotency"]["operation_fingerprint"]
    )

    persisted_preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
        },
    )
    assert persisted_preview.status_code == 200
    assert (
        persisted_preview.get_json()["flow"]["meta_flow_publication_verified"]
        is True
    )

    wrapper_preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
            "meta_publication_attestation": attestation,
        },
    )
    assert wrapper_preview.status_code == 200
    assert (
        wrapper_preview.get_json()["flow"]["meta_flow_publication_verified"]
        is True
    )

    replay = client.post(
        "/api/admin/whatsapp/flows/meta/sync",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "dry_run": False,
            "publish": True,
            "execute_confirmation": preview_payload["execute_confirmation"],
        },
    )
    assert replay.status_code == 409
    graph_client.provision_and_publish.assert_called_once()

    audit = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="whatsapp_flow.meta_published_verified",
        resource_id=META_FLOW_ID,
    ).one()
    assert audit.details["flow_json_sha256"] == artifact.content_sha256
    assert (
        audit.details["operation_fingerprint"]
        == preview_payload["readiness"]["idempotency"]["operation_fingerprint"]
    )


def test_meta_flow_management_clones_verified_published_flow_when_artifact_changes(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed()
    _prepare_meta_management(app, tenant, monkeypatch)
    headers = _auth_headers(app, admin, tenant.slug)
    artifact = build_order_checkout_flow()
    source_flow_id = "987654321012345"
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
        category="UTILITY",
        status="approved",
        content_sid="HXpublishedwrapper",
        external_template_id=source_flow_id,
        metadata_json={
            "flow_id": FLOW_ID,
            "meta_flow_id": source_flow_id,
            "meta_flow_status": "published",
            "meta_flow_publication_verified": True,
            "meta_flow_waba_id": META_WABA_ID,
            "flow_json_sha256": "0" * 64,
            "meta_sync_state": "complete",
        },
    )
    db.session.add(registry)
    db.session.commit()

    preview = client.post(
        "/api/admin/whatsapp/flows/meta/sync",
        headers=headers,
        json={"flow_id": FLOW_ID},
    )

    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["ready_to_sync"] is True
    assert preview_payload["blockers"] == []
    assert preview_payload["clone_flow_id"] == source_flow_id
    assert preview_payload["management"]["replacement_mode"] == "clone_published"
    assert preview_payload["management"]["steps"][0] == "clone_published_flow"

    verification = {
        "verified": True,
        "publication_verified": True,
        "artifact_identity_verified": True,
        "meta_flow_id": META_FLOW_ID,
        "status": "PUBLISHED",
        "waba_id": META_WABA_ID,
        "flow_json_sha256": artifact.content_sha256,
        "blockers": [],
    }
    graph_client = MagicMock()
    graph_client.provision_and_publish.return_value = {
        "created": True,
        "cloned_from_flow_id": source_flow_id,
        "uploaded": True,
        "published_now": True,
        "idempotent": False,
        "verification": verification,
    }
    with patch(
        "routes.whatsapp_rules.MetaFlowGraphClient",
        return_value=graph_client,
    ):
        execute = client.post(
            "/api/admin/whatsapp/flows/meta/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "dry_run": False,
                "publish": True,
                "execute_confirmation": preview_payload["execute_confirmation"],
            },
        )

    assert execute.status_code == 200
    executed = execute.get_json()
    assert executed["meta_flow_id"] == META_FLOW_ID
    assert executed["next_action"] == "replace_twilio_wrapper"
    graph_client.provision_and_publish.assert_called_once_with(
        flow_name="chatboc_order_checkout",
        category="OTHER",
        document=artifact.document,
        expected_sha256=artifact.content_sha256,
        endpoint_uri=(
            "https://api.chatboc.test/api/whatsapp/flows/data-exchange/"
            "meta-management-endpoint"
        ),
        meta_flow_id=source_flow_id,
        publish=True,
        clone_published_on_change=True,
        verification_only=False,
    )
    db.session.refresh(registry)
    assert registry.external_template_id == META_FLOW_ID
    assert registry.metadata_json["meta_flow_cloned_from_id"] == source_flow_id
    assert registry.metadata_json["flow_json_sha256"] == artifact.content_sha256


def test_meta_flow_management_is_fail_closed_without_graph_credentials(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    preview = client.post(
        "/api/admin/whatsapp/flows/meta/sync",
        headers=headers,
        json={"flow_id": FLOW_ID},
    )

    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["ready_to_sync"] is False
    assert "waba_id_not_configured" in payload["blockers"]
    assert "meta_graph_access_token_not_configured" in payload["blockers"]
    assert "execute_confirmation" not in payload


def test_meta_flow_management_persists_uncertain_create_and_blocks_blind_retry(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed()
    _prepare_meta_management(app, tenant, monkeypatch)
    headers = _auth_headers(app, admin, tenant.slug)

    preview = client.post(
        "/api/admin/whatsapp/flows/meta/sync",
        headers=headers,
        json={"flow_id": FLOW_ID},
    )
    assert preview.status_code == 200
    confirmation = preview.get_json()["execute_confirmation"]

    graph_client = MagicMock()
    graph_client.provision_and_publish.side_effect = MetaFlowManagementError(
        "meta_graph_unreachable",
        "No se pudo confirmar si Meta creo el Flow",
        status_code=503,
    )
    with patch(
        "routes.whatsapp_rules.MetaFlowGraphClient",
        return_value=graph_client,
    ):
        execute = client.post(
            "/api/admin/whatsapp/flows/meta/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "dry_run": False,
                "publish": True,
                "execute_confirmation": confirmation,
            },
        )

    assert execute.status_code == 503
    registry = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
    ).one()
    assert registry.status == "meta_sync_uncertain"
    assert registry.external_template_id is None
    assert registry.metadata_json["meta_sync_state"] == "uncertain"
    assert registry.metadata_json["meta_sync_error_code"] == "meta_graph_unreachable"

    retry_preview = client.post(
        "/api/admin/whatsapp/flows/meta/sync",
        headers=headers,
        json={"flow_id": FLOW_ID},
    )
    assert retry_preview.status_code == 200
    retry_payload = retry_preview.get_json()
    assert retry_payload["ready_to_sync"] is False
    assert "meta_flow_sync_reconciliation_required" in retry_payload["blockers"]
    assert "execute_confirmation" not in retry_payload


def test_native_flow_sync_requires_full_plan_and_never_issues_execute_token_when_locked(client, app):
    admin, tenant = _seed(plan="free")
    headers = _auth_headers(app, admin, tenant.slug)

    preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["blocked"] is True
    assert preview_payload["ready_to_create"] is False
    assert "execute_confirmation" not in preview_payload
    assert preview_payload["operator_guardrails"]["twilio_call_allowed"] is False

    with patch("routes.whatsapp_rules.Client") as client_factory:
        execute = client.post(
            "/api/admin/whatsapp/flows/twilio-content/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "meta_flow_id": META_FLOW_ID,
                "dry_run": False,
                "execute_confirmation": f"sync_twilio_flow:{FLOW_ID}:{META_FLOW_ID}",
            },
        )
    assert execute.status_code == 403
    assert execute.get_json()["action"] == "create_twilio_native_flow"
    client_factory.assert_not_called()


def test_native_flow_sync_creates_registry_audit_and_is_idempotent(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    app.config["TWILIO_ACCOUNT_SID"] = "ACparent"
    app.config["TWILIO_AUTH_TOKEN"] = "parent-secret"
    app.config["TWILIO_SUBACCOUNT_AUTH_TOKEN_ACMETAOPS"] = "tenant-secret"
    tenant.configuracion = {
        **tenant.configuracion,
        "twilio_tech_provider": {
            "twilio_account_sid": "ACmetaops",
            "twilio_subaccount_token_ref": "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACMETAOPS",
        },
    }
    db.session.add(
        ProviderConnection(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="connected",
            external_account_id="ACmetaops",
            credentials_ref="env:TWILIO_SUBACCOUNT_AUTH_TOKEN_ACMETAOPS",
        )
    )
    db.session.commit()

    class _FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, data, headers, timeout):
            self.calls.append((method, url, data, headers, timeout))
            if url == "https://content.twilio.com/v1/Content":
                assert method == "POST"
                assert data["friendly_name"] == "chatboc_order_checkout_native_v1"
                assert data["types"]["whatsapp/flows"]["flow_id"] == META_FLOW_ID
                assert data["types"]["whatsapp/flows"]["flow_token"] == "{{1}}"
                return SimpleNamespace(status_code=201, text=json.dumps({"sid": "HXnativeflow"}))
            assert url == (
                "https://content.twilio.com/v1/Content/"
                "HXnativeflow/ApprovalRequests/whatsapp"
            )
            assert method == "POST"
            assert data == {
                "name": "chatboc_order_checkout_native_v1",
                "category": "UTILITY",
            }
            return SimpleNamespace(status_code=201, text=json.dumps({"status": "PENDING"}))

    fake_client = _FakeClient()

    missing_confirmation = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID, "dry_run": False},
    )
    assert missing_confirmation.status_code == 409

    preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
    ).get_json()

    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        response = client.post(
            "/api/admin/whatsapp/flows/twilio-content/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "meta_flow_id": META_FLOW_ID,
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["content_sid"] == "HXnativeflow"
    assert payload["registry"]["status"] == "pending_approval"
    client_factory.assert_called_once_with("ACmetaops", "tenant-secret")

    row = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
    ).one()
    assert row.external_template_id == META_FLOW_ID
    assert row.metadata_json["content_family"] == "meta_native_flow"
    assert row.metadata_json["first_screen_id"] == "ORDER_DETAILS"
    assert row.metadata_json["flow_json_version"] == "7.3"
    assert row.metadata_json["data_api_version"] == "3.0"
    assert len(row.metadata_json["flow_json_sha256"]) == 64
    assert row.metadata_json["meta_flow_json_upload_performed"] is False
    assert row.metadata_json["meta_flow_publication_verified"] is False
    audit = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="whatsapp_flow.twilio_content_synced",
        resource_id=str(row.id),
    ).one()
    assert audit.details["meta_flow_id"] == META_FLOW_ID
    assert audit.details["twilio_account_scope"] == "subaccount"
    assert len(fake_client.calls) == 2

    with patch("routes.whatsapp_rules.Client") as duplicate_client:
        duplicate = client.post(
            "/api/admin/whatsapp/flows/twilio-content/sync",
            headers=headers,
            json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID, "dry_run": False},
        )
    assert duplicate.status_code == 200
    assert duplicate.get_json()["reason"] == "already_registered"
    duplicate_client.assert_not_called()

    replacement_meta_flow_id = "987654321012345"
    conflict = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": replacement_meta_flow_id},
    ).get_json()
    assert conflict["conflict"] is True
    assert conflict["ready_to_create"] is False
    assert "execute_confirmation" not in conflict

    forced_preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": replacement_meta_flow_id, "force": True},
    ).get_json()
    assert forced_preview["conflict"] is True
    assert forced_preview["forced_replacement"] is True
    assert forced_preview["ready_to_create"] is True
    assert isinstance(forced_preview["execute_confirmation"], str)
    assert len(forced_preview["execute_confirmation"]) > 40
    assert replacement_meta_flow_id not in forced_preview["execute_confirmation"]


def test_meta_platform_contract_only_activates_persisted_capabilities(client, app):
    _, tenant = _seed()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    artifact = build_order_checkout_flow()

    def init_handler(payload, context):
        return {"screen": "ORDER_DETAILS", "data": {}}

    def back_handler(payload, context):
        return {"screen": "ORDER_DETAILS", "data": {}}

    def exchange_handler(payload, context):
        return {
            "screen": "ORDER_CONFIRM",
            "data": {"order_summary": "2 productos", "total_display": "$ 25.000"},
        }

    app_config = {
        "TWILIO_META_APP_ID": "meta-app",
        "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID": "meta-config",
        "WHATSAPP_FLOW_TOKEN_KEY_V1": "test-flow-key-v1-000000000000000000000000000000000000",
        "PUBLIC_API_BASE_URL": "https://api.chatboc.test",
        "META_FLOW_DATA_EXCHANGE_ENDPOINTS": {
            "123456789": {
                "endpoint_id": "123456789",
                "tenant_id": str(tenant.id),
                "waba_id": "123456789",
                "private_key_pem": private_key_pem,
                "app_secret": "meta-test-app-secret",
                "handlers": {
                    "init": init_handler,
                    "back": back_handler,
                    "data_exchange": exchange_handler,
                },
            }
        },
    }

    initial = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert initial["active"] is False
    assert initial["native_flows"]["configured"] is False
    assert initial["native_flows"]["active"] is False
    assert initial["business_calling"]["active"] is False
    assert initial["catalog"]["active"] is False
    assert initial["embedded_signup"]["tenant_completed"] is False
    assert initial["embedded_signup"]["status"] == "ready_to_start"

    sender = ProviderSender(
        tenant_id=tenant.id,
        channel="whatsapp",
        phone_number="+5491112345678",
        sender_sid="XEwhatsappsender",
        waba_id="123456789",
        phone_number_id="987654321",
        status="active",
        metadata_json={
            "whatsapp_business_calling_status": "approved",
            "user_initiated_calling_enabled": True,
            "business_initiated_calling_enabled": False,
            "catalog_id": "catalog-123",
            "catalog_status": "active",
        },
    )
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
        category="UTILITY",
        status="approved",
        content_sid="HXnativeflow",
        external_template_id=META_FLOW_ID,
        metadata_json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
            "content_family": "meta_native_flow",
            "approval_status": "approved",
            "meta_flow_status": "published",
            "flow_json_sha256": artifact.content_sha256,
            "meta_flow_publication_verified": True,
        },
    )
    db.session.add_all([sender, registry])
    db.session.commit()

    contract = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert contract["active"] is True
    assert contract["sender"]["waba_id_present"] is True
    assert contract["native_flows"]["configured_count"] == 1
    assert contract["native_flows"]["active_count"] == 1
    assert contract["native_flows"]["data_exchange"]["ready"] is True
    assert contract["native_flows"]["send_endpoint"] == "/api/admin/whatsapp/flows/send"
    assert contract["native_flows"]["security"]["dedicated_token_key_ready"] is True
    submission_ingestion = contract["native_flows"]["submission_ingestion"]
    assert submission_ingestion["contract_version"] == "whatsapp.twilio_flow_completion.v1"
    assert submission_ingestion["is_meta_data_exchange_endpoint"] is False
    assert submission_ingestion["status"] == "active"
    assert submission_ingestion["claimed_active"] is True
    assert submission_ingestion["rejects_invalid_payload_before_orchestration"] is True
    catalog_flow = next(item for item in contract["native_flows"]["flows"] if item["id"] == FLOW_ID)
    assert catalog_flow["active"] is True
    assert catalog_flow["meta_flow_id"] == META_FLOW_ID
    assert catalog_flow["artifact_identity_verified"] is True
    assert catalog_flow["meta_flow_publication_verified"] is True
    assert contract["business_calling"]["active"] is True
    assert contract["business_calling"]["requires_explicit_user_consent"] is True
    assert contract["business_calling"]["whatsapp_pstn_bridge_allowed"] is False
    assert contract["catalog"]["active"] is True
    assert contract["embedded_signup"]["platform_configured"] is True
    assert contract["embedded_signup"]["tenant_completed"] is True
    assert contract["embedded_signup"]["active"] is True

    sender.status = "registered"
    db.session.add(sender)
    db.session.commit()
    pending_sender = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert pending_sender["sender"]["ready"] is False
    assert pending_sender["native_flows"]["active_count"] == 0
    assert pending_sender["native_flows"]["submission_ingestion"]["claimed_active"] is False


def test_native_flow_send_preview_execute_and_idempotent_replay(client, app):
    admin, tenant = _seed()
    sender, registry = _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    request_payload = {
        "flow_id": FLOW_ID,
        "recipient": "+54 9 11 2345-6789",
        "idempotency_key": "flow-send-idem-001",
        "order_context": ORDER_CONTEXT,
    }

    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=request_payload,
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["ready_to_send"] is True
    assert preview_payload["blockers"] == []
    assert preview_payload["recipient_hint"] == "***6789"
    assert preview_payload["provider_sender_id"] == sender.id
    assert preview_payload["content_sid"] == registry.content_sid
    assert preview_payload["security"]["one_time_token"] is True
    assert preview_payload["security"]["token_exposed"] is False
    assert isinstance(preview_payload["execute_confirmation"], str)
    serialized_preview = json.dumps(preview_payload)
    assert "+5491123456789" not in serialized_preview
    assert "flow_token" not in serialized_preview

    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(sid="SMNATIVEFLOW001")

    messages = _Messages()
    fake_client = SimpleNamespace(messages=messages)
    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        execute = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **request_payload,
                "dry_run": False,
                "execute_confirmation": preview_payload["execute_confirmation"],
            },
        )
    assert execute.status_code == 201
    execute_payload = execute.get_json()
    assert execute_payload["sent"] is True
    assert execute_payload["interaction"]["status"] == "sent"
    assert execute_payload["interaction"]["recipient_hint"] == "***6789"
    assert "token" not in json.dumps(execute_payload).lower()
    client_factory.assert_called_once_with("ACflowsend", "flow-send-secret")
    assert len(messages.calls) == 1
    sent = messages.calls[0]
    assert sent["to"] == "whatsapp:+5491123456789"
    assert sent["from_"] == "whatsapp:+5491100000000"
    assert sent["content_sid"] == "HXflowtosend"
    callback = urlsplit(sent["status_callback"])
    assert callback.scheme == "https"
    assert callback.netloc == "api.chatboc.test"
    assert callback.path == "/twilio/whatsapp/status"
    assert parse_qs(callback.query)["flow_interaction_id"] == [
        str(execute_payload["interaction"]["id"])
    ]
    provider_token = json.loads(sent["content_variables"])["1"]
    assert len(provider_token) > 80
    assert "+5491123456789" not in provider_token

    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key="flow-send-idem-001",
    ).one()
    assert interaction.token_digest != provider_token
    assert interaction.recipient_hint == "***6789"
    assert interaction.metadata_json["order_context"] == ORDER_CONTEXT
    assert "+5491123456789" not in json.dumps(interaction.metadata_json)
    event = MessagingEventLedger.query.filter_by(
        tenant_id=tenant.id,
        event_type="native_flow_sent",
    ).one()
    assert event.external_message_sid == "SMNATIVEFLOW001"
    assert event.recipient == "***6789"

    with patch("routes.whatsapp_rules.Client") as replay_client:
        replay = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={**request_payload, "dry_run": False},
        )
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    replay_client.assert_not_called()


def test_native_flow_send_timeout_is_uncertain_and_never_auto_retries(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    request_payload = {
        "flow_id": FLOW_ID,
        "recipient": "+5491123456790",
        "idempotency_key": "flow-send-timeout-001",
        "order_context": ORDER_CONTEXT,
    }
    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=request_payload,
    ).get_json()
    messages = SimpleNamespace(create=MagicMock(side_effect=TimeoutError("provider timeout")))
    with patch(
        "routes.whatsapp_rules.Client",
        return_value=SimpleNamespace(messages=messages),
    ):
        execute = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **request_payload,
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )
    assert execute.status_code == 502
    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key="flow-send-timeout-001",
    ).one()
    assert interaction.status == "send_uncertain"
    assert interaction.error_code == "TimeoutError"

    with patch("routes.whatsapp_rules.Client") as retry_client:
        retry = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={**request_payload, "dry_run": False},
        )
    assert retry.status_code == 409
    assert retry.get_json()["interaction"]["retry_safe"] is False
    retry_client.assert_not_called()


def test_native_flow_send_fails_closed_without_dedicated_key(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    app.config["WHATSAPP_FLOW_TOKEN_KEY_V1"] = ""
    headers = _auth_headers(app, admin, tenant.slug)
    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456791",
            "idempotency_key": "flow-send-key-missing",
        },
    )
    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["ready_to_send"] is False
    assert "flow_token_key_not_configured" in payload["blockers"]
    assert "execute_confirmation" not in payload


def test_order_flow_send_requires_tenant_owned_order_context(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)

    missing = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456797",
            "idempotency_key": "flow-order-context-missing",
        },
    ).get_json()
    unavailable = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456797",
            "idempotency_key": "flow-order-context-other",
            "order_context": {"kind": "order", "id": "another-tenant-order"},
        },
    ).get_json()

    assert missing["ready_to_send"] is False
    assert "order_context_missing" in missing["blockers"]
    assert unavailable["ready_to_send"] is False
    assert "order_context_unavailable" in unavailable["blockers"]
    assert "execute_confirmation" not in missing
    assert "execute_confirmation" not in unavailable


def test_survey_flow_send_authorizes_published_context_and_persists_scope(client, app):
    admin, tenant = _seed()
    _, _, survey = _prepare_ready_survey_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    request_payload = {
        "flow_id": "survey_vote",
        "recipient": "+5491123456711",
        "idempotency_key": "flow-survey-context-001",
        "survey_context": {"survey_slug": survey.slug},
    }

    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=request_payload,
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["ready_to_send"] is True
    assert preview_payload["blockers"] == []
    assert preview_payload["survey_context"] == {
        "required": True,
        "ready": True,
        "id": str(survey.id),
        "slug": survey.slug,
    }

    messages = SimpleNamespace(
        create=MagicMock(return_value=SimpleNamespace(sid="SMNATIVESURVEY001"))
    )
    with patch(
        "routes.whatsapp_rules.Client",
        return_value=SimpleNamespace(messages=messages),
    ):
        execute = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **request_payload,
                "dry_run": False,
                "execute_confirmation": preview_payload["execute_confirmation"],
            },
        )

    assert execute.status_code == 201
    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key="flow-survey-context-001",
    ).one()
    assert interaction.flow_id == "survey_vote"
    assert interaction.data_contract == ["confirm_vote"]
    assert interaction.metadata_json["survey_context"] == {
        "id": str(survey.id),
        "slug": survey.slug,
    }
    sent_variables = json.loads(messages.create.call_args.kwargs["content_variables"])
    assert survey.slug not in json.dumps(sent_variables)


def test_survey_flow_send_blocks_missing_and_non_native_survey_context(client, app):
    admin, tenant = _seed()
    _, _, survey = _prepare_ready_survey_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)

    missing = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": "survey_vote",
            "recipient": "+5491123456712",
            "idempotency_key": "flow-survey-missing-001",
        },
    ).get_json()
    assert missing["ready_to_send"] is False
    assert missing["survey_context"]["ready"] is False
    assert "survey_context_missing" in missing["blockers"]
    assert "execute_confirmation" not in missing

    survey.preguntas[0].tipo = "abierta"
    db.session.commit()
    incompatible = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": "survey_vote",
            "recipient": "+5491123456712",
            "idempotency_key": "flow-survey-incompatible-001",
            "survey_context": {"slug": survey.slug},
        },
    ).get_json()
    assert incompatible["ready_to_send"] is False
    assert incompatible["survey_context"]["ready"] is False
    assert "survey_question_type_unsupported" in incompatible["blockers"]
    assert "execute_confirmation" not in incompatible


def test_native_flow_send_requires_verified_publication_and_data_exchange(client, app):
    admin, tenant = _seed()
    _, registry = _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    metadata = dict(registry.metadata_json)
    metadata["meta_flow_publication_verified"] = False
    registry.metadata_json = metadata
    db.session.add(registry)
    db.session.commit()

    publication_blocked = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456798",
            "idempotency_key": "flow-publication-missing-001",
        },
    ).get_json()
    assert publication_blocked["ready_to_send"] is False
    assert "meta_flow_publication_not_verified" in publication_blocked["blockers"]

    metadata["meta_flow_publication_verified"] = True
    registry.metadata_json = metadata
    app.config["META_FLOW_DATA_EXCHANGE_ENDPOINTS"] = {}
    db.session.add(registry)
    db.session.commit()
    endpoint_blocked = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456798",
            "idempotency_key": "flow-endpoint-missing-001",
        },
    ).get_json()
    assert endpoint_blocked["ready_to_send"] is False
    assert "data_exchange_not_ready" in endpoint_blocked["blockers"]


def test_native_flow_send_requires_explicit_e164_and_boolean_dry_run(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)

    invalid_phone = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "5491123456792",
            "idempotency_key": "flow-invalid-e164-001",
        },
    )
    invalid_boolean = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456792",
            "idempotency_key": "flow-invalid-dry-run-001",
            "dry_run": "false",
        },
    )
    repeated_plus = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+54+91123456792",
            "idempotency_key": "flow-invalid-e164-plus-001",
        },
    )

    assert invalid_phone.status_code == 400
    assert invalid_boolean.status_code == 400
    assert repeated_plus.status_code == 400


def test_twilio_sync_rejects_string_boolean_controls(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    requests = (
        (
            "/api/admin/templates/twilio-content/sync",
            {"template_id": "order_checkout"},
        ),
        (
            "/api/admin/whatsapp/flows/twilio-content/sync",
            {"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
        ),
    )

    for endpoint, base_payload in requests:
        for field in ("submit_for_approval", "force"):
            response = client.post(
                endpoint,
                headers=headers,
                json={**base_payload, field: "false"},
            )
            assert response.status_code == 400
            assert field in response.get_json()["message"]


def test_native_flow_send_counts_durable_invocations_against_hourly_limit(client, app):
    admin, tenant = _seed()
    first_sender, _ = _prepare_ready_flow_send(app, tenant)
    second_sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=first_sender.provider_connection_id,
        channel="whatsapp",
        phone_number="+5491100000001",
        sender_id="whatsapp:+5491100000001",
        waba_id="waba-flow-send",
        status="active",
        status_callback_url="https://api.chatboc.test/twilio/whatsapp/status",
        metadata_json={"meta_flow_data_exchange_endpoint_id": "flow-send-endpoint"},
    )
    db.session.add(second_sender)
    headers = _auth_headers(app, admin, tenant.slug)
    db.session.add(
        WhatsAppEnterpriseRule(
            tenant_id=tenant.id,
            max_outbound_per_hour=1,
        )
    )
    db.session.commit()
    first_payload = {
        "flow_id": FLOW_ID,
        "recipient": "+5491123456793",
        "idempotency_key": "flow-hourly-limit-001",
        "order_context": ORDER_CONTEXT,
    }
    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=first_payload,
    ).get_json()
    messages = SimpleNamespace(create=MagicMock(return_value=SimpleNamespace(sid="SMFLOWLIMIT001")))
    with patch("routes.whatsapp_rules.Client", return_value=SimpleNamespace(messages=messages)):
        sent = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **first_payload,
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )

    blocked = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456794",
            "idempotency_key": "flow-hourly-limit-002",
            "provider_sender_id": second_sender.id,
            "order_context": ORDER_CONTEXT,
        },
    )

    assert sent.status_code == 201
    assert blocked.status_code == 200
    assert blocked.get_json()["ready_to_send"] is False
    assert "rate_limited" in blocked.get_json()["blockers"]
    messages.create.assert_called_once()
